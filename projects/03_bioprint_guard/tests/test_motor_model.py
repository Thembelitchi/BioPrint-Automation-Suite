"""Unit tests for the simulated stepper motor."""

from __future__ import annotations

import numpy as np
import pytest
from motor_model import SIM_EPOCH, StepperMotorSimulator


def read(sim: StepperMotorSimulator, n: int):
    return [sim.read() for _ in range(n)]


def test_same_seed_gives_same_samples() -> None:
    a = read(StepperMotorSimulator(seed=7), 200)
    b = read(StepperMotorSimulator(seed=7), 200)
    assert a == b
    assert a != read(StepperMotorSimulator(seed=8), 200)


def test_clock_is_simulated_and_10_hz() -> None:
    samples = read(StepperMotorSimulator(), 11)
    assert [s.index for s in samples] == list(range(11))
    assert samples[10].t == pytest.approx(1.0)
    assert (samples[10].timestamp - SIM_EPOCH).total_seconds() == pytest.approx(1.0)
    assert len({s.timestamp for s in samples}) == 11  # timestamps identify a reading


def test_healthy_motor_stays_at_its_operating_point() -> None:
    sim = StepperMotorSimulator(seed=3, fault_after_s=None)
    samples = read(sim, 3000)
    p = sim.params
    assert all(s.wear == 0.0 for s in samples)
    assert np.mean([s.motor_current for s in samples]) == pytest.approx(
        p.current_nominal_a, abs=0.01
    )
    assert np.mean([s.housing_temp for s in samples]) == pytest.approx(p.temp_nominal_c, abs=0.05)
    assert np.std([s.vibration_frequency for s in samples]) == pytest.approx(
        p.vibration_noise_hz, rel=0.1
    )


def test_wear_ramps_from_onset_to_one() -> None:
    sim = StepperMotorSimulator(fault_after_s=15.0, wear_ramp_s=10.0)
    assert sim.wear_at(14.9) == 0.0
    assert sim.wear_at(20.0) == pytest.approx(0.5)
    assert sim.wear_at(25.0) == 1.0
    assert sim.wear_at(99.0) == 1.0


def test_wear_raises_all_three_signals_and_temperature_lags() -> None:
    sim = StepperMotorSimulator(seed=1)
    samples = read(sim, 300)
    healthy, worn = samples[:150], samples[250:]
    p = sim.params
    for name in ("motor_current", "housing_temp", "vibration_frequency"):
        assert np.mean([getattr(s, name) for s in worn]) > np.mean(
            [getattr(s, name) for s in healthy]
        )
    assert np.mean([s.motor_current for s in worn]) == pytest.approx(
        p.current_nominal_a + p.current_wear_gain_a, abs=0.02
    )
    # The housing has thermal mass: at full wear it has not yet reached its final rise.
    assert samples[-1].housing_temp < p.temp_nominal_c + p.temp_wear_gain_c - 1.0


@pytest.mark.parametrize("kwargs", [{"sample_period_s": 0}, {"wear_ramp_s": 0}])
def test_invalid_arguments_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        StepperMotorSimulator(**kwargs)
