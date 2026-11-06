"""FR-1: discrete PID behaviour and anti-windup."""

import numpy as np
import pytest
from pid_controller import DiscretePID, PIDGains


def test_output_is_clamped() -> None:
    pid = DiscretePID(PIDGains(50.0, 5.0, 0.0))
    assert pid.update(230.0, 25.0) == 100.0
    assert pid.update(25.0, 230.0) == 0.0


def test_proportional_only() -> None:
    pid = DiscretePID(PIDGains(2.0, 0.0, 0.0))
    assert pid.update(10.0, 7.0) == pytest.approx(6.0)


def test_integral_accumulates_when_not_saturated() -> None:
    pid = DiscretePID(PIDGains(0.0, 1.0, 0.0), dt=0.1)
    for _ in range(10):
        pid.update(5.0, 0.0)  # error 5, 1 s of integration
    assert pid.integral == pytest.approx(5.0)


def test_anti_windup_freezes_integral_during_saturation() -> None:
    gains = PIDGains(2.0, 1.0, 0.0)
    with_aw = DiscretePID(gains, anti_windup=True)
    without = DiscretePID(gains, anti_windup=False)
    for _ in range(100):  # 10 s pinned at 100 % with a 205 °C error
        with_aw.update(230.0, 25.0)
        without.update(230.0, 25.0)
    assert with_aw.integral <= 100.0
    assert without.integral > 1000.0  # classic windup


def test_derivative_on_measurement_has_no_setpoint_kick() -> None:
    pid = DiscretePID(PIDGains(0.0, 0.0, 5.0))
    pid.update(100.0, 50.0)
    out = pid.update(200.0, 50.0)  # setpoint jumps, measurement unchanged
    assert out == pytest.approx(0.0)


def test_derivative_opposes_rising_measurement() -> None:
    pid = DiscretePID(PIDGains(0.0, 0.0, 1.0), out_min=-100.0)
    pid.update(0.0, 0.0)
    pid.update(0.0, 1.0)
    assert pid.last_terms[2] < 0


@pytest.mark.parametrize("kwargs", [{"dt": 0.0}, {"out_min": 10.0, "out_max": 5.0}])
def test_rejects_bad_configuration(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        DiscretePID(PIDGains(1.0, 0.0, 0.0), **kwargs)


def test_output_always_finite() -> None:
    rng = np.random.default_rng(0)
    pid = DiscretePID(PIDGains(8.0, 2.5, 2.0))
    for _ in range(1000):
        out = pid.update(230.0, float(rng.normal(230.0, 50.0)))
        assert np.isfinite(out) and 0.0 <= out <= 100.0
