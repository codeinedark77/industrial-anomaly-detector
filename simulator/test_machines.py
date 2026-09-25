"""Quick checks for the machine simulation logic. Run: python test_machines.py"""

from machines import Machine, build_plant, SAFE_SETPOINT_RANGE, ANOMALOUS_SETPOINT_RANGE


def test_reading_shape():
    m = Machine("test-01", 1.0, 40.0, 5.0)
    reading = m.next_reading()
    assert reading["machine_id"] == "test-01"
    assert "timestamp" in reading
    assert isinstance(reading["vibration_mm_s"], float)


def test_drift_increases_vibration_and_temp():
    m = Machine("test-02", 1.0, 40.0, 5.0)
    m.drift_active = True
    baseline_reading = m.next_reading()
    for _ in range(150):
        m.next_reading()
    drifted_reading = m.next_reading()
    assert drifted_reading["vibration_mm_s"] > baseline_reading["vibration_mm_s"]
    assert drifted_reading["temperature_c"] > baseline_reading["temperature_c"]


def test_drift_ends_after_duration():
    m = Machine("test-03", 1.0, 40.0, 5.0)
    m.drift_active = True
    for _ in range(200):
        m.next_reading()
    assert m.drift_active is False
    assert m.drift_ticks == 0


def test_build_plant_returns_four_unique_machines():
    plant = build_plant()
    assert len(plant) == 4
    ids = {m.machine_id for m in plant}
    assert len(ids) == 4


def test_normal_commands_use_known_ip_and_safe_range():
    m = Machine("test-04", 1.0, 40.0, 5.0, known_ips=("10.0.9.9",))
    seen_normal = False
    for _ in range(500):
        command, is_anomalous = m.maybe_command()
        if command and not is_anomalous:
            seen_normal = True
            assert command["source_ip"] == "10.0.9.9"
            assert SAFE_SETPOINT_RANGE[0] <= command["value"] <= SAFE_SETPOINT_RANGE[1]
    assert seen_normal, "No normal command generated in 500 ticks"


def test_anomalous_commands_use_out_of_range_value():
    m = Machine("test-05", 1.0, 40.0, 5.0, known_ips=("10.0.9.9",))
    seen_anomalous = False
    for _ in range(2000):
        command, is_anomalous = m.maybe_command()
        if command and is_anomalous:
            seen_anomalous = True
            assert ANOMALOUS_SETPOINT_RANGE[0] <= command["value"] <= ANOMALOUS_SETPOINT_RANGE[1]
    assert seen_anomalous, "No anomalous command generated in 2000 ticks"


if __name__ == "__main__":
    test_reading_shape()
    test_drift_increases_vibration_and_temp()
    test_drift_ends_after_duration()
    test_build_plant_returns_four_unique_machines()
    test_normal_commands_use_known_ip_and_safe_range()
    test_anomalous_commands_use_out_of_range_value()
    print("All tests passed.")

