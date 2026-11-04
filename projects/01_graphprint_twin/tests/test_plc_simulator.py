"""Unit tests for the PLC simulator (FR-1). No database needed."""

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
    for _ in range(50):
        ra, rb = a.read_registers(), b.read_registers()
        assert (ra.nozzle_temp, ra.pump_pressure) == (rb.nozzle_temp, rb.pump_pressure)


def test_status_matches_values() -> None:
    sim = PLCExtruderSimulator(seed=1)
    for _ in range(500):
        r = sim.read_registers()
        assert r.status == classify(r.nozzle_temp, r.pump_pressure)


def test_walk_stays_centred_on_setpoints() -> None:
    sim = PLCExtruderSimulator(seed=3)
    readings = [sim.read_registers() for _ in range(2000)]
    mean_temp = sum(r.nozzle_temp for r in readings) / len(readings)
    mean_pressure = sum(r.pump_pressure for r in readings) / len(readings)
    # One-reading upsets nudge the mean up slightly; the walk itself stays centred.
    assert abs(mean_temp - TARGET_TEMP_C) < 1.5
    assert abs(mean_pressure - TARGET_PRESSURE_BAR) < 1.0


def test_default_ci_run_contains_both_states() -> None:
    """The seeded 10-cycle CI run should exercise both OPTIMAL and ANOMALY_WARNING paths."""
    sim = PLCExtruderSimulator(seed=42)
    statuses = {sim.read_registers().status for _ in range(10)}
    assert statuses == {"OPTIMAL", "ANOMALY_WARNING"}


def test_reading_params_have_unique_ids() -> None:
    sim = PLCExtruderSimulator(seed=0)
    ids = {sim.read_registers().to_params()["log_id"] for _ in range(100)}
    assert len(ids) == 100


@pytest.mark.parametrize("kwargs", [{"reversion": 1.5}, {"upset_probability": -0.1}])
def test_rejects_invalid_parameters(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        PLCExtruderSimulator(**kwargs)
