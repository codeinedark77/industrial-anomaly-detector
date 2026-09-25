"""Pure rule logic for the OT security detector.

No MQTT or DB here - see security_detector.py for that plumbing.
Kept separate so the rules are unit-testable without a broker.

Deliberately rule-based, not learned: the safe value range and the
known-source-IP allowlist are declared configuration (what an asset
owner / security team would define from engineering specs and
network inventory), not something inferred by watching traffic.
Learning "normal" from observed traffic is a real anti-pattern here
- it would let an attacker's IP get quietly allow-listed just by
issuing enough commands before the detector started watching.
"""

from dataclasses import dataclass

# In a real deployment this comes from an asset inventory / engineering
# spec, not from watching traffic - the detector is never told which
# commands the simulator considers "normal" vs "anomalous" ground
# truth, it only sees the same values and IPs a real system would.
MACHINE_CONFIG = {
    "press-01":      {"safe_range": (20.0, 80.0), "known_ips": {"10.0.1.10", "10.0.1.11"}},
    "conveyor-02":   {"safe_range": (20.0, 80.0), "known_ips": {"10.0.1.10", "10.0.1.12"}},
    "pump-03":       {"safe_range": (20.0, 80.0), "known_ips": {"10.0.1.11", "10.0.1.13"}},
    "compressor-04": {"safe_range": (20.0, 80.0), "known_ips": {"10.0.1.10", "10.0.1.13"}},
}
DEFAULT_SAFE_RANGE = (20.0, 80.0)
DEFAULT_KNOWN_IPS: set = set()


@dataclass
class SecurityCheckResult:
    is_anomalous: bool
    reasons: list


def check_command(command: dict) -> SecurityCheckResult:
    machine_id = command["machine_id"]
    value = command["value"]
    source_ip = command["source_ip"]

    config = MACHINE_CONFIG.get(machine_id)
    safe_range = config["safe_range"] if config else DEFAULT_SAFE_RANGE
    known_ips = config["known_ips"] if config else DEFAULT_KNOWN_IPS

    lo, hi = safe_range
    reasons = []
    if not (lo <= value <= hi):
        reasons.append(f"value {value} outside safe range [{lo}, {hi}]")
    if source_ip not in known_ips:
        reasons.append(f"source_ip {source_ip} not in known allowlist for {machine_id}")

    return SecurityCheckResult(is_anomalous=bool(reasons), reasons=reasons)
