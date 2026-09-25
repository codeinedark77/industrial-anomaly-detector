"""Tests for the OT security rule logic. Run: python test_security_rules.py

test_matches_simulator_ground_truth is an integration check: it runs
the REAL simulator's Machine.maybe_command() (which knows internally
whether a command is anomalous) and confirms these rules - which
never see that internal flag - arrive at the same answer from value
and source_ip alone.
"""

import os
import sys

from security_rules import check_command, MACHINE_CONFIG


def test_normal_command_not_anomalous():
    result = check_command({
        "machine_id": "press-01", "value": 55.0, "source_ip": "10.0.1.10",
    })
    assert result.is_anomalous is False


def test_out_of_range_value_is_anomalous():
    result = check_command({
        "machine_id": "press-01", "value": 178.3, "source_ip": "10.0.1.10",
    })
    assert result.is_anomalous is True
    assert any("outside safe range" in r for r in result.reasons)


def test_unknown_ip_is_anomalous_even_with_safe_value():
    result = check_command({
        "machine_id": "press-01", "value": 55.0, "source_ip": "203.0.113.7",
    })
    assert result.is_anomalous is True
    assert any("not in known allowlist" in r for r in result.reasons)


def test_both_reasons_reported_when_both_fail():
    result = check_command({
        "machine_id": "press-01", "value": 178.3, "source_ip": "203.0.113.7",
    })
    assert result.is_anomalous is True
    assert len(result.reasons) == 2


def test_unrecognized_machine_defaults_to_no_known_ips():
    result = check_command({
        "machine_id": "unregistered-machine-99", "value": 50.0, "source_ip": "10.0.1.10",
    })
    assert result.is_anomalous is True  # nothing is "known" for an unregistered asset


def test_matches_simulator_ground_truth():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "simulator"))
    from machines import build_plant

    plant = build_plant()
    checked = 0
    agreements = 0
    for _ in range(20000):
        for machine in plant:
            command, sim_says_anomalous = machine.maybe_command()
            if command is None:
                continue
            checked += 1
            rules_say_anomalous = check_command(command).is_anomalous
            if rules_say_anomalous == sim_says_anomalous:
                agreements += 1
            else:
                print(f"  MISMATCH: sim={sim_says_anomalous} rules={rules_say_anomalous} cmd={command}")

    assert checked > 50, f"Too few commands generated to be a meaningful check ({checked})"
    assert agreements == checked, f"{agreements}/{checked} agreed with simulator ground truth"
    print(f"  ({checked} real simulator commands, rules agreed with ground truth on all of them)")


if __name__ == "__main__":
    test_normal_command_not_anomalous()
    print("test_normal_command_not_anomalous passed")
    test_out_of_range_value_is_anomalous()
    print("test_out_of_range_value_is_anomalous passed")
    test_unknown_ip_is_anomalous_even_with_safe_value()
    print("test_unknown_ip_is_anomalous_even_with_safe_value passed")
    test_both_reasons_reported_when_both_fail()
    print("test_both_reasons_reported_when_both_fail passed")
    test_unrecognized_machine_defaults_to_no_known_ips()
    print("test_unrecognized_machine_defaults_to_no_known_ips passed")
    test_matches_simulator_ground_truth()
    print("test_matches_simulator_ground_truth passed")
    print("All tests passed.")
