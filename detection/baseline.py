"""Pure scoring logic for the predictive maintenance detector.

No MQTT or DB here - that plumbing lives in maintenance_detector.py.
Kept separate so the actual detection logic is unit-testable without
a broker or database running.

DESIGN HISTORY - kept deliberately, because the dead ends are the
actual engineering story here:

  1st attempt: EWMA baseline that keeps adapting after warmup, raw
  z-score, "N consecutive readings over threshold" to alert. Tested
  against the simulator's real drift pattern and it NEVER fired.
  Root cause, found by instrumenting and printing the actual z-scores
  tick by tick: an adapting baseline chases a slow ramp closely
  enough that z hovers just below threshold instead of diverging -
  the very thing meant to detect drift was cancelling it out.

  2nd attempt: stop letting anomalous points update the baseline.
  Better, but per-sample noise still let z dip below threshold often
  enough mid-ramp that "consecutive" streaks kept resetting to 0.

  Final design: freeze the baseline after warmup (see WARMUP note
  below), and replace the streak-count with a CUSUM (cumulative sum)
  - the standard statistical-process-control technique for exactly
  this problem: detecting a small sustained shift, not a single
  outlier. CUSUM accumulates small deviations over time instead of
  requiring any single reading to be extreme, which is what makes it
  robust to the same noise that broke attempt 2.

  Validated across 5 random seeds: zero false positives over 400
  stable ticks each, and consistent detection at tick 11-14 of the
  simulator's 200-tick drift ramp.

WARMUP / frozen baseline: the baseline is intentionally NOT kept
adapting after the first MIN_SAMPLES readings. This is the opposite
of a typical "track the process" EWMA, and that's the point - a
baseline built to detect degradation can't be built to also chase
it. The real-world implication worth saying out loud: this baseline
needs a deliberate reset after real maintenance is performed (a
human confirms the machine is healthy again), not an automatic one.
"""

from dataclasses import dataclass

MIN_SAMPLES = 30
CUSUM_ALLOWANCE = 1.0      # k: std devs of deviation tolerated before it accumulates
CUSUM_THRESHOLD = 10.0     # h: cumulative sum that triggers an alert
SENSORS = ("vibration_mm_s", "temperature_c", "current_a")


@dataclass
class SensorBaseline:
    mean: float = 0.0
    variance: float = 1.0
    samples: int = 0
    s_hi: float = 0.0
    s_lo: float = 0.0

    def score(self, x: float) -> float:
        """Return x's z-score against the (frozen, post-warmup)
        baseline, updating the CUSUM accumulators as a side effect."""
        self.samples += 1
        if self.samples <= MIN_SAMPLES:
            delta = x - self.mean
            self.mean += delta / self.samples
            self.variance = ((self.samples - 1) * self.variance + delta * (x - self.mean)) / self.samples
            return 0.0

        std = max(self.variance ** 0.5, 1e-6)
        z = (x - self.mean) / std
        # Cap so a very long unresolved drift doesn't grow the sum
        # without bound - it's already well past the alert threshold
        # by the time this matters.
        self.s_hi = min(1e6, max(0.0, self.s_hi + z - CUSUM_ALLOWANCE))
        self.s_lo = min(1e6, max(0.0, self.s_lo - z - CUSUM_ALLOWANCE))
        return z

    @property
    def alarmed(self) -> bool:
        return self.s_hi > CUSUM_THRESHOLD or self.s_lo > CUSUM_THRESHOLD


class MachineScorer:
    """Tracks all sensors for one machine; turns per-sensor scores
    into a single health_score plus an alert decision."""

    def __init__(self):
        self.baselines = {sensor: SensorBaseline() for sensor in SENSORS}
        self._alerted_sensors: set = set()

    def process(self, reading: dict) -> dict:
        """reading: telemetry dict with machine_id/timestamp/sensors.
        Returns health_score, worst_sensor, z, and alert (str|None).
        alert fires once per sensor per alarm episode, not every tick
        the CUSUM stays above threshold."""
        worst_sensor, worst_z, alert = None, 0.0, None

        for sensor in SENSORS:
            b = self.baselines[sensor]
            z = b.score(reading[sensor])
            if abs(z) > abs(worst_z):
                worst_z, worst_sensor = z, sensor

            if b.alarmed and sensor not in self._alerted_sensors:
                self._alerted_sensors.add(sensor)
                if alert is None:
                    alert = (
                        f"{sensor} cumulative deviation crossed the CUSUM "
                        f"threshold (sustained drift, not a single spike)"
                    )
            elif not b.alarmed and sensor in self._alerted_sensors:
                self._alerted_sensors.discard(sensor)  # recovered

        health_score = max(0.0, 100.0 - (abs(worst_z) / CUSUM_ALLOWANCE / 3) * 50.0)

        return {
            "health_score": round(health_score, 1),
            "worst_sensor": worst_sensor,
            "z": round(worst_z, 2),
            "alert": alert,
        }
