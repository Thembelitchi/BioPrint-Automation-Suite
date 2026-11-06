"""Unit tests for the 10 Hz PLC simulator (FR-1). No database needed."""

from datetime import datetime, timedelta, timezone

import pytest
from plc_simulator import (
    PRESSURE_LIMIT_BAR,
    TARGET_PRESSURE_BAR,
    TARGET_TEMP_C,
    TEMP_LIMIT_C,
    PLCExtruderSimulator,
    classify,
)


@pytest.mark.parametrize(
    ("temp", "pressure", "expected"),
    [
        (230.0, 45.0, "OPTIMAL"),
        (TEMP_LIMIT_C, PRESSURE_LIMIT_BAR, "OPTIMAL"),  # on the limit is still in spec
        (TEMP_LIMIT_C + 0.01, 45.0, "ANOMALY_WARNING"),
        (230.0, PRESSURE_LIMIT_BAR + 0.01, "ANOMALY_WARNING"),
    ],
)
def test_classify_envelope(temp: float, pressure: float, expected: str) -> None:
    assert classify(temp, pressure) == expected


def test_same_seed_is_reproducible() -> None:
    a = PLCExtruderSimulator(seed=7)
    b = PLCExtruderSimulator(seed=7)
    for _ in range(500):
        ra, rb = a.read_registers(), b.read_registers()
        assert (ra.nozzle_temp, ra.pump_pressure) == (rb.nozzle_temp, rb.pump_pressure)


def test_simulated_clock_steps_one_period() -> None:
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)
    sim = PLCExtruderSimulator(seed=0, sample_rate_hz=10.0, start_time=t0)
    stamps = [sim.read_registers().timestamp for _ in range(3)]
    assert stamps == [t0, t0 + timedelta(seconds=0.1), t0 + timedelta(seconds=0.2)]
    assert sim.sample_period_s == pytest.approx(0.1)


def test_status_matches_values() -> None:
    sim = PLCExtruderSimulator(seed=1)
    for _ in range(5000):
        r = sim.read_registers()
        assert r.status == classify(r.nozzle_temp, r.pump_pressure)


def test_walk_stays_centred_on_setpoints() -> None:
    sim = PLCExtruderSimulator(seed=3)
    readings = [sim.read_registers() for _ in range(20000)]
    mean_temp = sum(r.nozzle_temp for r in readings) / len(readings)
    mean_pressure = sum(r.pump_pressure for r in readings) / len(readings)
    assert abs(mean_temp - TARGET_TEMP_C) < 0.5
    assert abs(mean_pressure - TARGET_PRESSURE_BAR) < 0.3


def test_anomalies_are_rare_at_10hz() -> None:
    """Out-of-envelope samples should be rare events (upsets), not noise."""
    sim = PLCExtruderSimulator(seed=5)
    n = 20000
    rate = sum(sim.read_registers().status == "ANOMALY_WARNING" for _ in range(n)) / n
    assert 0.0002 < rate < 0.005


@pytest.mark.parametrize(
    "kwargs", [{"reversion": 1.5}, {"upset_probability": -0.1}, {"sample_rate_hz": 0}]
)
def test_rejects_invalid_parameters(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        PLCExtruderSimulator(**kwargs)
