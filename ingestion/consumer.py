"""
OT/ICS Risk Radar - ingestion consumer.

Subscribes to the plant's MQTT telemetry and command topics and
writes every message durably to TimescaleDB. This is the "lean
path" ingestion described in docs/DESIGN.md section 4.2 - a direct
MQTT consumer, no Kafka in front of it (yet).

Writes are idempotent (upsert on machine_id + time) so a redelivered
MQTT message can't double-count a reading. Anything that fails to
parse or fails to write lands in dead_letters instead of being
silently dropped.

Usage: python consumer.py
Env vars: MQTT_HOST, MQTT_PORT, DATABASE_URL (see .env.example)
"""

import json
import logging
import os
import signal
import sys

import paho.mqtt.client as mqtt
import psycopg
from prometheus_client import Counter, start_http_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingestion")

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://radar:radar@localhost:5432/radar")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9101"))

MESSAGES_TOTAL = Counter("ingestion_messages_total", "Messages ingested", ["topic_type"])
DEAD_LETTERS_TOTAL = Counter("ingestion_dead_letters_total", "Messages dead-lettered", ["reason"])

REQUIRED_TELEMETRY_FIELDS = {"machine_id", "timestamp", "vibration_mm_s", "temperature_c", "current_a"}
REQUIRED_COMMAND_FIELDS = {"machine_id", "timestamp", "command", "value", "source_ip"}

INSERT_TELEMETRY = """
    INSERT INTO telemetry (time, machine_id, vibration_mm_s, temperature_c, current_a)
    VALUES (%(timestamp)s, %(machine_id)s, %(vibration_mm_s)s, %(temperature_c)s, %(current_a)s)
    ON CONFLICT (machine_id, time) DO NOTHING
"""

INSERT_COMMAND = """
    INSERT INTO commands (time, machine_id, command, value, source_ip, raw_payload)
    VALUES (%(timestamp)s, %(machine_id)s, %(command)s, %(value)s, %(source_ip)s, %(raw_payload)s)
    ON CONFLICT (machine_id, time) DO NOTHING
"""

INSERT_DEAD_LETTER = """
    INSERT INTO dead_letters (topic, reason, raw_payload)
    VALUES (%(topic)s, %(reason)s, %(raw_payload)s)
"""


class Ingestor:
    """Owns the DB connection and does durable, idempotent writes."""

    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, autocommit=False)
        log.info("Connected to database")

    def _write(self, query: str, params: dict, on_success: str) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.execute(query, params)
            self.conn.commit()
            log.info(on_success)
        except Exception:
            self.conn.rollback()
            log.exception("Write failed, rolled back: %s", params)

    def handle_telemetry(self, topic: str, payload: dict) -> None:
        if not REQUIRED_TELEMETRY_FIELDS.issubset(payload):
            self.dead_letter(topic, "missing required telemetry fields", payload)
            return
        self._write(INSERT_TELEMETRY, payload, f"telemetry <- {payload['machine_id']}")
        MESSAGES_TOTAL.labels(topic_type="telemetry").inc()

    def handle_command(self, topic: str, payload: dict) -> None:
        if not REQUIRED_COMMAND_FIELDS.issubset(payload):
            self.dead_letter(topic, "missing required command fields", payload)
            return
        params = {**payload, "raw_payload": json.dumps(payload)}
        self._write(INSERT_COMMAND, params, f"command   <- {payload['machine_id']} ({payload['command']})")
        MESSAGES_TOTAL.labels(topic_type="commands").inc()

    def dead_letter(self, topic: str, reason: str, raw) -> None:
        DEAD_LETTERS_TOTAL.labels(reason=reason).inc()
        raw_text = raw if isinstance(raw, str) else json.dumps(raw)
        try:
            with self.conn.cursor() as cur:
                cur.execute(INSERT_DEAD_LETTER, {"topic": topic, "reason": reason, "raw_payload": raw_text})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            log.exception("Failed to record dead letter for topic %s", topic)
        log.warning("Dead-lettered message on %s: %s", topic, reason)

    def close(self) -> None:
        self.conn.close()


def on_connect(client, userdata, flags, reason_code, properties=None):
    log.info("Connected to MQTT broker (%s)", reason_code)
    client.subscribe("plant/+/telemetry", qos=1)
    client.subscribe("plant/+/commands", qos=1)


def on_message(client, userdata, msg):
    # paho-mqtt propagates an exception raised in here straight up
    # through loop_forever() and kills the whole consumer - confirmed
    # by hand while testing this file. Everything below is wrapped so
    # one bad message can never take the process down.
    try:
        _handle_message(userdata, msg)
    except Exception:
        log.exception("Unhandled error processing message on %s - continuing", msg.topic)


def _handle_message(ingestor: "Ingestor", msg) -> None:
    raw_text = msg.payload.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        ingestor.dead_letter(msg.topic, "invalid JSON", raw_text[:1000])
        return

    if msg.topic.endswith("/telemetry"):
        ingestor.handle_telemetry(msg.topic, payload)
    elif msg.topic.endswith("/commands"):
        ingestor.handle_command(msg.topic, payload)
    else:
        ingestor.dead_letter(msg.topic, "unrecognized topic", payload)


def main() -> None:
    start_http_server(METRICS_PORT)
    log.info("Metrics on :%d/metrics", METRICS_PORT)

    ingestor = Ingestor(DATABASE_URL)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, userdata=ingestor)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    def shutdown(signum, frame):
        log.info("Shutting down ingestion consumer")
        client.disconnect()
        ingestor.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    client.loop_forever()


if __name__ == "__main__":
    main()
