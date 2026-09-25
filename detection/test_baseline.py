"""Tests for the maintenance detector's scoring logic.
Run: python test_baseline.py

These are the actual tests that drove the design in baseline.py's
docstring - test_stable_signal_never_alerts and
test_drift_eventually_alerts are what caught the first two failed
designs before this one.
"""

import random

from baseline import MachineScorer, MIN_SAMPLES


def make_reading(vibration, temp, current):
    return {"vibration_mm_s": vibration, "temperature_c": temp, "current_a": current}


def run_stable(seed: int, ticks: int = 400) -> bool:
    """Returns True if any alert fired on a pure-noise, no-drift signal."""
    scorer = MachineScorer()
    random.seed(seed)
    fired = False
    for _ in range(ticks):
        r = make_reading(
            1.2 + random.gauss(0, 0.05),
            42.0 + random.gauss(0, 0.3),
            8.5 + random.gauss(0, 0.15),
        )
        if scorer.process(r)["alert"]:
            fired = True
    return fired


def run_drift(seed: int):
    """Returns the tick (1-200) the drift ramp first alerts, or None."""
    scorer = MachineScorer()
    random.seed(seed)
    for _ in range(MIN_SAMPLES + 10):
        scorer.process(make_reading(
            1.2 + random.gauss(0, 0.05), 42.0 + random.gauss(0, 0.3), 8.5 + random.gauss(0, 0.15)
        ))
    for tick in range(1, 201):
        progress = min(tick / 200, 1.0)
        r = make_reading(
            1.2 + random.gauss(0, 0.05) + progress * 2.5,
            42.0 + random.gauss(0, 0.3) + progress * 8.0,
            8.5 + random.gauss(0, 0.15),
        )
        if scorer.process(r)["alert"]:
            return tick
    return None


def test_stable_signal_never_alerts_across_seeds():
    for seed in range(5):
        assert not run_stable(seed + 100), f"False positive on stable signal, seed {seed}"


def test_drift_reliably_alerts_across_seeds():
    fire_ticks = []
    for seed in range(5):
        tick = run_drift(seed)
        assert tick is not None, f"Detector never alerted during drift ramp, seed {seed}"
        fire_ticks.append(tick)
    # Sanity band, not just "did it fire": too early (<5) would mean
    # it is basically noise-triggered; too late (>100) would mean it
    # is not much of a *predictive* detector.
    assert all(5 <= t <= 100 for t in fire_ticks), fire_ticks
    print(f"  (fired at ticks {fire_ticks} across 5 seeds)")


def test_health_score_drops_during_drift():
    scorer = MachineScorer()
    random.seed(2)
    for _ in range(MIN_SAMPLES + 10):
        scorer.process(make_reading(
            1.2 + random.gauss(0, 0.05), 42.0 + random.gauss(0, 0.3), 8.5 + random.gauss(0, 0.15)
        ))
    baseline_score = scorer.process(make_reading(1.2, 42.0, 8.5))["health_score"]
    for tick in range(1, 60):
        progress = tick / 200
        scorer.process(make_reading(1.2 + progress * 2.5, 42.0 + progress * 8.0, 8.5))
    drifted_score = scorer.process(make_reading(1.2 + 0.75, 42.0 + 2.4, 8.5))["health_score"]
    assert drifted_score < baseline_score
    print(f"  (health_score {baseline_score} -> {drifted_score})")


def test_alert_fires_once_per_episode_not_every_tick():
    scorer = MachineScorer()
    random.seed(3)
    for _ in range(MIN_SAMPLES + 10):
        scorer.process(make_reading(
            1.2 + random.gauss(0, 0.05), 42.0 + random.gauss(0, 0.3), 8.5 + random.gauss(0, 0.15)
        ))
    alert_count = 0
    for tick in range(1, 201):
        progress = min(tick / 200, 1.0)
        r = make_reading(
            1.2 + random.gauss(0, 0.05) + progress * 2.5,
            42.0 + random.gauss(0, 0.3) + progress * 8.0,
            8.5 + random.gauss(0, 0.15),
        )
        if scorer.process(r)["alert"]:
            alert_count += 1
    # Should fire once (maybe twice if both sensors cross independently),
    # not dozens of times while the CUSUM sits above threshold.
    assert 1 <= alert_count <= 2, f"Expected 1-2 alert episodes, got {alert_count}"


if __name__ == "__main__":
    test_stable_signal_never_alerts_across_seeds()
    print("test_stable_signal_never_alerts_across_seeds passed")
    test_drift_reliably_alerts_across_seeds()
    print("test_drift_reliably_alerts_across_seeds passed")
    test_health_score_drops_during_drift()
    print("test_health_score_drops_during_drift passed")
    test_alert_fires_once_per_episode_not_every_tick()
    print("test_alert_fires_once_per_episode_not_every_tick passed")
    print("All tests passed.")
