"""Unit tests for the streaming anomaly detector (FR-3)."""

from __future__ import annotations

import math

import pytest
from anomaly_detector import (
    ALERT_THRESHOLD,
    ScoredSample,
    StreamingAnomalyDetector,
    severity_for,
)
from motor_model import StepperMotorSimulator


def score_run(seed: int, n: int = 300, fault_after_s: float | None = 15.0):
    sim = StepperMotorSimulator(seed=seed, fault_after_s=fault_after_s)
    detector = StreamingAnomalyDetector()
    out: list[tuple[float, ScoredSample]] = []
    for _ in range(n):
        sample = sim.read()
        out.append((sample.t, detector.update(sample.values())))
    return out


def test_score_is_zero_until_the_baseline_is_fixed() -> None:
    sim = StepperMotorSimulator(seed=0)
    detector = StreamingAnomalyDetector(baseline_samples=100)
    results = [detector.update(sim.read().values()) for _ in range(100)]
    assert all(r.score == 0.0 and not r.ready for r in results)
    assert detector.ready
    assert detector.update(sim.read().values()).ready


def test_baseline_limits_match_the_healthy_signal_and_are_then_frozen() -> None:
    sim = StepperMotorSimulator(seed=0)
    detector = StreamingAnomalyDetector(baseline_samples=100)
    for _ in range(100):
        detector.update(sim.read().values())
    mean, sd = detector.baseline_mean, detector.baseline_sd
    assert mean[0] == pytest.approx(1.80, abs=0.02)
    assert mean[1] == pytest.approx(45.0, abs=0.1)
    assert mean[2] == pytest.approx(120.0, abs=0.5)
    for _ in range(200):  # includes the fault: limits must not move with it
        detector.update(sim.read().values())
    assert detector.baseline_mean == mean
    assert detector.baseline_sd == sd


def test_fr3_healthy_below_half_then_above_alert_threshold() -> None:
    results = score_run(seed=42)
    healthy = [r.score for t, r in results if t < 15.0]
    faulty = [r.score for t, r in results if t >= 15.0]
    assert max(healthy) < 0.5
    assert max(faulty) > ALERT_THRESHOLD
    assert faulty[-1] > 0.99


@pytest.mark.parametrize("seed", range(30))
def test_fr3_holds_across_seeds_and_detects_before_full_wear(seed: int) -> None:
    results = score_run(seed)
    assert max(r.score for t, r in results if t < 15.0) < 0.5
    first = next(t for t, r in results if t >= 15.0 and r.score > ALERT_THRESHOLD)
    assert first < 22.0  # wear reaches 1.0 at 25 s; the alert comes well before that


def test_no_false_alert_over_ten_healthy_minutes() -> None:
    results = score_run(seed=5, n=6000, fault_after_s=None)
    assert max(r.score for _, r in results) < 0.5


def test_one_outlier_does_not_raise_an_alert_but_a_sustained_shift_does() -> None:
    sim = StepperMotorSimulator(seed=2, fault_after_s=None)
    detector = StreamingAnomalyDetector()
    for _ in range(150):
        detector.update(sim.read().values())
    current, temp, vibration = sim.read().values()
    spike = detector.update((current + 0.3, temp, vibration))  # one 10-SD current spike
    assert spike.distance > 4.0
    assert spike.score < 0.5
    last = spike
    for _ in range(40):
        current, temp, vibration = sim.read().values()
        last = detector.update((current + 0.3, temp, vibration))
    assert last.score > ALERT_THRESHOLD


def test_flat_baseline_does_not_divide_by_zero() -> None:
    detector = StreamingAnomalyDetector(baseline_samples=10)
    for _ in range(10):
        detector.update((1.0, 2.0, 3.0))
    result = detector.update((1.0, 2.0, 3.5))
    assert math.isfinite(result.score)
    assert result.score > ALERT_THRESHOLD


def test_bad_input_is_rejected() -> None:
    detector = StreamingAnomalyDetector(baseline_samples=10)
    with pytest.raises(ValueError):
        detector.update((1.0, float("nan"), 3.0))
    detector.update((1.0, 2.0, 3.0))
    with pytest.raises(ValueError):
        detector.update((1.0, 2.0))
    for kwargs in ({"baseline_samples": 5}, {"ewma_alpha": 0.0}, {"steepness": 0.0}):
        with pytest.raises(ValueError):
            StreamingAnomalyDetector(**kwargs)


def test_severity_bands_match_the_graph_schema() -> None:
    assert severity_for(0.84) is None
    assert severity_for(0.85) == "HIGH"
    assert severity_for(0.949) == "HIGH"
    assert severity_for(0.95) == "CRITICAL"
    assert severity_for(1.0) == "CRITICAL"
