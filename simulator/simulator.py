"""
OT/ICS Risk Radar - telemetry simulator entry point.

Publishes synthetic plant telemetry and occasional anomalous commands
to an MQTT broker. Start the broker first (docker compose up -d).

Usage: python simulator.py
"""

import json
import time

import paho.mqtt.client as mqtt

from machines import build_plant

MQTT_HOST = "localhost"
MQTT_PORT = 1883
TELEMETRY_INTERVAL_SECONDS = 2


def main() -> None:
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    plant = build_plant()
    print(f"Simulating {len(plant)} machines -> {MQTT_HOST}:{MQTT_PORT}")

    try:
        while True:
            for machine in plant:
                reading = machine.next_reading()
                client.publish(
                    f"plant/{machine.machine_id}/telemetry", json.dumps(reading)
                )

                command, is_anomalous = machine.maybe_command()
                if command:
                    client.publish(
                        f"plant/{machine.machine_id}/commands", json.dumps(command)
                    )
                    if is_anomalous:
                        print(f"[anomalous command] {command}")

            time.sleep(TELEMETRY_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopping simulator.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
