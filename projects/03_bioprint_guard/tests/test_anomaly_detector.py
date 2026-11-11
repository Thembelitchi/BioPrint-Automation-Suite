"""Unit tests for the streaming anomaly detector (FR-3)."""

from __future__ import annotations

import math

import pytest
from anomaly_detector import (
    ALERT_THRESHOLD,
    IsolationForestDetector,
    ScoredSample,
    StreamingAnomalyDetector,
    make_detector,
    severity_for,
)
from motor_model import StepperMotorSimulator


def score_run(
    seed: int,
    n: int = 300,
    fault_after_s: float | None = 15.0,
    detector=None,
    **motor_kwargs,
):
    sim = StepperMotorSimulator(seed=seed, fault_after_s=fault_after_s, **motor_kwargs)
    detector = detector or StreamingAnomalyDetector()
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


def test_frozen_baseline_matches_the_healthy_signal_and_never_moves() -> None:
    sim = StepperMotorSimulator(seed=0)
    detector = StreamingAnomalyDetector(baseline_samples=100, baseline="frozen")
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
    for kwargs in (
        {"baseline_samples": 5},
        {"ewma_alpha": 0.0},
        {"steepness": 0.0},
        {"baseline": "sliding"},
        {"lag_samples": -1},
        {"hold_distance": 4.0},
    ):
        with pytest.raises(ValueError):
            StreamingAnomalyDetector(**kwargs)


def test_severity_bands_match_the_graph_schema() -> None:
    assert severity_for(0.84) is None
    assert severity_for(0.85) == "HIGH"
    assert severity_for(0.949) == "HIGH"
    assert severity_for(0.95) == "CRITICAL"
    assert severity_for(1.0) == "CRITICAL"


# -- rolling vs frozen baseline ----------------------------------------------------------


def test_rolling_is_the_default_baseline() -> None:
    assert StreamingAnomalyDetector().baseline == "rolling"
    assert make_detector().baseline == "rolling"


@pytest.mark.parametrize("baseline", ["rolling", "frozen"])
def test_fr3_holds_for_both_baseline_modes(baseline: str) -> None:
    for seed in range(10):
        results = score_run(seed, detector=StreamingAnomalyDetector(baseline=baseline))
        assert max(r.score for t, r in results if t < 15.0) < 0.5
        first = next(t for t, r in results if t >= 15.0 and r.score > ALERT_THRESHOLD)
        assert first < 22.0
        assert results[-1][1].score > 0.99  # the fault is never learned as normal


def test_rolling_window_follows_benign_drift_and_frozen_limits_do_not() -> None:
    """The factory warms the housing by 2 C over 10 minutes. That is not a fault."""
    drift = {"fault_after_s": None, "temp_drift_c_per_min": 0.2}
    rolling = score_run(3, n=6000, detector=StreamingAnomalyDetector(baseline="rolling"), **drift)
    frozen = score_run(3, n=6000, detector=StreamingAnomalyDetector(baseline="frozen"), **drift)
    assert max(r.score for _, r in rolling) < 0.5  # no false alert
    assert max(r.score for _, r in frozen) > ALERT_THRESHOLD  # 2 C is ~13 baseline SDs


def test_rolling_window_is_held_during_a_fault_and_its_limits_stay_healthy() -> None:
    sim = StepperMotorSimulator(seed=4)
    detector = StreamingAnomalyDetector(baseline="rolling")
    for _ in range(150):  # healthy up to the wear onset at 15 s
        detector.update(sim.read().values())
    assert not detector.holding
    for _ in range(150):  # the whole fault
        detector.update(sim.read().values())
    assert detector.holding
    current, temp, vibration = detector.baseline_mean
    assert current == pytest.approx(1.80, abs=0.03)  # not dragged towards 2.25 A
    assert temp == pytest.approx(45.0, abs=0.3)
    assert vibration == pytest.approx(120.0, abs=1.5)


def test_rolling_window_lags_the_present() -> None:
    detector = StreamingAnomalyDetector(baseline_samples=20, lag_samples=10)
    for _ in range(20):
        detector.update((1.0, 2.0, 3.0))
    before = detector.baseline_mean
    for _ in range(10):  # these wait in the lag buffer
        detector.update((1.0, 2.0, 3.0000001))
    assert detector.baseline_mean == before
    detector.update((1.0, 2.0, 3.0000001))  # the first waiting reading is admitted
    assert detector.baseline_mean != before


def test_wear_slower_than_the_window_is_the_rolling_baselines_blind_spot() -> None:
    """A fault that takes 20 minutes to develop: frozen limits catch it, rolling ones adapt."""
    slow = {"fault_after_s": 15.0, "wear_ramp_s": 1200.0}
    rolling = score_run(1, n=3000, detector=StreamingAnomalyDetector(baseline="rolling"), **slow)
    frozen = score_run(1, n=3000, detector=StreamingAnomalyDetector(baseline="frozen"), **slow)
    assert max(r.score for _, r in frozen) > ALERT_THRESHOLD
    assert max(r.score for _, r in rolling) < ALERT_THRESHOLD


# -- Isolation Forest (optional detector) --------------------------------------------------


def test_isolation_forest_meets_fr3_across_seeds() -> None:
    pytest.importorskip("sklearn")
    for seed in range(8):
        results = score_run(seed, detector=IsolationForestDetector())
        assert max(r.score for t, r in results if t < 15.0) < 0.5
        first = next(t for t, r in results if t >= 15.0 and r.score > ALERT_THRESHOLD)
        assert first < 22.0
        assert results[-1][1].score > 0.95


def test_isolation_forest_raises_no_alert_on_a_healthy_motor() -> None:
    pytest.importorskip("sklearn")
    results = score_run(5, n=3000, fault_after_s=None, detector=IsolationForestDetector())
    assert not results[99][1].ready and results[100][1].ready
    assert max(r.score for _, r in results) < 0.5


def test_isolation_forest_is_reproducible_and_validates_input() -> None:
    pytest.importorskip("sklearn")
    a = [r.score for _, r in score_run(2, n=200, detector=IsolationForestDetector())]
    b = [r.score for _, r in score_run(2, n=200, detector=IsolationForestDetector())]
    assert a == b
    detector = IsolationForestDetector(baseline_samples=10)
    detector.update((1.0, 2.0, 3.0))
    with pytest.raises(ValueError):
        detector.update((1.0, 2.0))
    with pytest.raises(ValueError):
        detector.update((1.0, float("inf"), 3.0))


def test_make_detector_maps_cli_choices() -> None:
    assert make_detector("zscore", "frozen", 50).baseline == "frozen"
    assert make_detector("zscore", "frozen", 50).baseline_samples == 50
    with pytest.raises(ValueError):
        make_detector("lstm")
    pytest.importorskip("sklearn")
    assert isinstance(make_detector("iforest"), IsolationForestDetector)
