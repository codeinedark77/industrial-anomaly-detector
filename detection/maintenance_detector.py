"""
OT/ICS Risk Radar - predictive maintenance detector.

Subscribes to the telemetry stream, scores every reading through
baseline.py's CUSUM detector, and writes a continuous health_score
plus 'maintenance' alerts to storage. See baseline.py's module
docstring for the design history - two earlier approaches were
tested against the simulator's real drift pattern and failed before
landing on this one.

Usage: python maintenance_detector.py
"""

import json
import logging
import os
import signal
import sys

import paho.mqtt.client as mqtt
import psycopg
from prometheus_client import Counter, start_http_server

from baseline import MachineScorer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("maintenance-detector")

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://radar:radar@localhost:5432/radar")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9102"))

READINGS_TOTAL = Counter("maintenance_readings_processed_total", "Telemetry readings scored")
ALERTS_TOTAL = Counter("maintenance_alerts_total", "Maintenance alerts raised", ["sensor"])

INSERT_HEALTH_SCORE = """
    INSERT INTO health_scores (time, machine_id, health_score)
    VALUES (%(timestamp)s, %(machine_id)s, %(health_score)s)
    ON CONFLICT (machine_id, time) DO NOTHING
"""

INSERT_ALERT = """
    INSERT INTO alerts (machine_id, alert_type, severity, description, raw_payload)
    VALUES (%(machine_id)s, 'maintenance', 'warning', %(description)s, %(raw_payload)s)
"""


class Detector:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, autocommit=False)
        self.scorers: dict[str, MachineScorer] = {}
        log.info("Connected to database")

    def handle_telemetry(self, payload: dict) -> None:
        machine_id = payload["machine_id"]
        scorer = self.scorers.setdefault(machine_id, MachineScorer())
        result = scorer.process(payload)
        READINGS_TOTAL.inc()

        try:
            with self.conn.cursor() as cur:
                cur.execute(INSERT_HEALTH_SCORE, {
                    "timestamp": payload["timestamp"],
                    "machine_id": machine_id,
                    "health_score": result["health_score"],
                })
                if result["alert"]:
                    cur.execute(INSERT_ALERT, {
                        "machine_id": machine_id,
                        "description": result["alert"],
                        "raw_payload": json.dumps({**payload, "z": result["z"]}),
                    })
            self.conn.commit()
            if result["alert"]:
                ALERTS_TOTAL.labels(sensor=result["worst_sensor"] or "unknown").inc()
                log.warning("ALERT machine=%s %s", machine_id, result["alert"])
        except Exception:
            self.conn.rollback()
            log.exception("Failed to write health score / alert for %s", machine_id)

    def close(self) -> None:
        self.conn.close()


def on_connect(client, userdata, flags, reason_code, properties=None):
    log.info("Connected to MQTT broker (%s)", reason_code)
    client.subscribe("plant/+/telemetry", qos=1)


def on_message(client, userdata, msg):
    detector: Detector = userdata
    try:
        _handle(detector, msg)
    except Exception:
        log.exception("Unhandled error processing message on %s - continuing", msg.topic)


def _handle(detector: Detector, msg) -> None:
    try:
        payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        log.warning("Dropped malformed payload on %s", msg.topic)
        return
    detector.handle_telemetry(payload)


def main() -> None:
    start_http_server(METRICS_PORT)
    log.info("Metrics on :%d/metrics", METRICS_PORT)

    detector = Detector(DATABASE_URL)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, userdata=detector)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    def shutdown(signum, frame):
        log.info("Shutting down maintenance detector")
        client.disconnect()
        detector.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    client.loop_forever()


if __name__ == "__main__":
    main()
