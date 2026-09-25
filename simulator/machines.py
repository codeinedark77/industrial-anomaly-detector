"""Virtual machine models for the OT/ICS risk radar simulator.

Each Machine produces realistic sensor noise around a baseline, with
two kinds of injected anomalies:

  - gradual drift in vibration + temperature (simulated bearing wear),
    for the predictive-maintenance detector to catch as a trend
  - occasional out-of-envelope commands, for the OT-security detector
    to catch as a point anomaly

Revised for Milestone 4: commands are now mostly NORMAL traffic
(safe setpoint range, known source IPs) with anomalies mixed in
(out-of-range value, unrecognized IP) - the security detector needs
real normal traffic to distinguish anomalies from, otherwise every
rule trivially "catches" 100% of a stream that's anomalous by
construction.
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

DRIFT_TRIGGER_PROBABILITY = 0.0005
DRIFT_DURATION_TICKS = 200

NORMAL_COMMAND_PROBABILITY = 0.04
ANOMALOUS_COMMAND_PROBABILITY = 0.01
SAFE_SETPOINT_RANGE = (20.0, 80.0)
ANOMALOUS_SETPOINT_RANGE = (150.0, 200.0)


def _random_ip() -> str:
    return f"10.0.{random.randint(1, 254)}.{random.randint(1, 254)}"


@dataclass
class Machine:
    machine_id: str
    baseline_vibration_mm_s: float
    baseline_temp_c: float
    baseline_current_a: float
    known_ips: tuple = field(default_factory=tuple)
    drift_active: bool = False
    drift_ticks: int = 0

    def next_reading(self) -> dict:
        vibration = self.baseline_vibration_mm_s + random.gauss(0, 0.05)
        temp = self.baseline_temp_c + random.gauss(0, 0.3)
        current = self.baseline_current_a + random.gauss(0, 0.15)

        if self.drift_active:
            self.drift_ticks += 1
            progress = min(self.drift_ticks / DRIFT_DURATION_TICKS, 1.0)
            vibration += progress * 2.5
            temp += progress * 8.0
            if self.drift_ticks >= DRIFT_DURATION_TICKS:
                self.drift_active = False
                self.drift_ticks = 0
        elif random.random() < DRIFT_TRIGGER_PROBABILITY:
            self.drift_active = True

        return {
            "machine_id": self.machine_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "vibration_mm_s": round(vibration, 3),
            "temperature_c": round(temp, 2),
            "current_a": round(current, 3),
        }

    def maybe_command(self):
        """Returns (command_dict, is_anomalous) or (None, False).

        is_anomalous is ground truth for the simulator's own console
        logging only - it is never published over MQTT, so the
        security detector has to earn its detections from value +
        source_ip alone, same as a real system would.
        """
        roll = random.random()
        if roll < ANOMALOUS_COMMAND_PROBABILITY:
            command = self._make_command(
                value=round(random.uniform(*ANOMALOUS_SETPOINT_RANGE), 1),
                source_ip=_random_ip(),
            )
            return command, True
        if roll < ANOMALOUS_COMMAND_PROBABILITY + NORMAL_COMMAND_PROBABILITY:
            command = self._make_command(
                value=round(random.uniform(*SAFE_SETPOINT_RANGE), 1),
                source_ip=random.choice(self.known_ips) if self.known_ips else _random_ip(),
            )
            return command, False
        return None, False

    def _make_command(self, value: float, source_ip: str) -> dict:
        return {
            "machine_id": self.machine_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": "set_setpoint",
            "value": value,
            "source_ip": source_ip,
        }


def build_plant() -> list[Machine]:
    return [
        Machine("press-01", 1.2, 42.0, 8.5, known_ips=("10.0.1.10", "10.0.1.11")),
        Machine("conveyor-02", 0.8, 35.0, 4.2, known_ips=("10.0.1.10", "10.0.1.12")),
        Machine("pump-03", 1.5, 48.0, 6.0, known_ips=("10.0.1.11", "10.0.1.13")),
        Machine("compressor-04", 2.0, 55.0, 11.0, known_ips=("10.0.1.10", "10.0.1.13")),
    ]

