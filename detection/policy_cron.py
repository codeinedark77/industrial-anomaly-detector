import json
import logging
import os
import subprocess
import sys
import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("policy-cron")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://radar:radar@localhost:5432/radar")
SCANNER_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../static_policy_scanner"))
INVENTORY_FILE = "factory_inventory.yaml"
SARIF_OUT = "findings.sarif"

def generate_inventory_yaml():
    yaml_content = """
zones:
  - id: ot-l1
    name: OT Level 1 - Control
    criticality: safety

conduits: []

assets:
  - id: press-01
    name: Press Machine 01
    asset_type: plc
    zone_id: ot-l1
    has_authentication: true
    internet_facing: false

  - id: conveyor-02
    name: Conveyor Belt 02
    asset_type: plc
    zone_id: ot-l1
    has_authentication: false
    protocols: [modbus_tcp]

  - id: pump-03
    name: Coolant Pump 03
    asset_type: plc
    zone_id: ot-l1
    internet_facing: true
    uses_default_credentials: true

  - id: compressor-04
    name: Air Compressor 04
    asset_type: plc
    zone_id: ot-l1
    firmware_version: "1.0.0"
    firmware_latest_known: "2.1.0"
"""
    with open(INVENTORY_FILE, "w") as f:
        f.write(yaml_content)
    log.info("Generated %s", INVENTORY_FILE)


def run_scanner():
    log.info("Running static policy scanner...")
    env = os.environ.copy()
    env["PYTHONPATH"] = SCANNER_PATH
    
    cmd = [
        sys.executable, "-m", "ics_risk_radar.cli", "scan",
        INVENTORY_FILE,
        "--sarif-out", SARIF_OUT
    ]
    
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0 and not os.path.exists(SARIF_OUT):
        log.error("Scanner failed: %s", result.stderr)
        return False
    return True


def ingest_sarif():
    if not os.path.exists(SARIF_OUT):
        log.error("SARIF output not found.")
        return

    with open(SARIF_OUT, "r") as f:
        data = json.load(f)

    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    
    INSERT_ALERT = """
        INSERT INTO alerts (machine_id, alert_type, severity, description, raw_payload)
        VALUES (%s, 'security', %s, %s, %s)
    """
    
    try:
        with conn.cursor() as cur:
            # First, clean out old static alerts to avoid duplicates on every cron run
            cur.execute("DELETE FROM alerts WHERE alert_type = 'security' AND description LIKE 'Policy Violation:%%'")
            
            runs = data.get("runs", [])
            for run in runs:
                results = run.get("results", [])
                for res in results:
                    rule_id = res.get("ruleId")
                    message = res.get("message", {}).get("text", "")
                    # Extract target from location
                    machine_id = "unknown"
                    locations = res.get("locations", [])
                    if locations:
                        logical = locations[0].get("logicalLocations", [])
                        if logical:
                            machine_id = logical[0].get("name")
                    
                    level = res.get("level", "warning")
                    # Map SARIF levels to our schema severities (info, warning, critical)
                    severity = "critical" if level == "error" else "warning"
                    
                    desc = f"Policy Violation: [{rule_id}] {message}"
                    
                    cur.execute(INSERT_ALERT, (
                        machine_id,
                        severity,
                        desc,
                        json.dumps(res)
                    ))
                    log.info("Ingested SARIF finding for %s: %s", machine_id, rule_id)
    except Exception as e:
        log.error("Failed to ingest SARIF: %s", e)
    finally:
        conn.close()


def main():
    generate_inventory_yaml()
    if run_scanner():
        ingest_sarif()
        log.info("Static policy audit complete.")

if __name__ == "__main__":
    main()
