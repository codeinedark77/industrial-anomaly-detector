"""
OT/ICS Risk Radar - OT security detector.

Subscribes to the commands stream and runs every command through
security_rules.py. Validated against the real simulator: agrees
with its internal ground truth on every one of 4000+ generated
commands (see test_security_rules.py).

Usage: python security_detector.py
"""

import json
import logging
import os
import signal
import sys

import paho.mqtt.client as mqtt
import psycopg
from prometheus_client import Counter, start_http_server

from security_rules import check_command

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("security-detector")

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://radar:radar@localhost:5432/radar")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9103"))

COMMANDS_TOTAL = Counter("security_commands_processed_total", "Commands evaluated")
ALERTS_TOTAL = Counter("security_alerts_total", "Security alerts raised", ["severity"])

INSERT_ALERT = """
    INSERT INTO alerts (machine_id, alert_type, severity, description, raw_payload)
    VALUES (%(machine_id)s, 'security', %(severity)s, %(description)s, %(raw_payload)s)
"""


class SecurityDetector:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, autocommit=False)
        log.info("Connected to database")

    def handle_command(self, payload: dict) -> None:
        result = check_command(payload)
        COMMANDS_TOTAL.inc()
        if not result.is_anomalous:
            return

        # Two rule violations at once (bad value AND unknown source)
        # is a stronger signal than either alone - a legitimate
        # engineer fat-fingering a setpoint is still coming from a
        # known IP.
        severity = "critical" if len(result.reasons) > 1 else "warning"
        description = "; ".join(result.reasons)
        ALERTS_TOTAL.labels(severity=severity).inc()

        try:
            with self.conn.cursor() as cur:
                cur.execute(INSERT_ALERT, {
                    "machine_id": payload["machine_id"],
                    "severity": severity,
                    "description": description,
                    "raw_payload": json.dumps(payload),
                })
            self.conn.commit()
            log.warning("ALERT machine=%s severity=%s %s", payload["machine_id"], severity, description)
        except Exception:
            self.conn.rollback()
            log.exception("Failed to write security alert for %s", payload.get("machine_id"))

    def close(self) -> None:
        self.conn.close()


def on_connect(client, userdata, flags, reason_code, properties=None):
    log.info("Connected to MQTT broker (%s)", reason_code)
    client.subscribe("plant/+/commands", qos=1)


def on_message(client, userdata, msg):
    detector: SecurityDetector = userdata
    try:
        _handle(detector, msg)
    except Exception:
        log.exception("Unhandled error processing message on %s - continuing", msg.topic)


def _handle(detector: SecurityDetector, msg) -> None:
    try:
        payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        log.warning("Dropped malformed payload on %s", msg.topic)
        return
    detector.handle_command(payload)


def main() -> None:
    start_http_server(METRICS_PORT)
    log.info("Metrics on :%d/metrics", METRICS_PORT)

    detector = SecurityDetector(DATABASE_URL)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, userdata=detector)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    def shutdown(signum, frame):
        log.info("Shutting down security detector")
        client.disconnect()
        detector.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    client.loop_forever()


if __name__ == "__main__":
    main()
