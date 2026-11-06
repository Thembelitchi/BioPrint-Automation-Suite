"""Layer bucketing: one graph node per printed layer instead of one per sample."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from layer_aggregator import layer_id, summarise_layer
from plc_simulator import PLCExtruderSimulator, TelemetryReading, classify

T0 = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)


def readings(values: list[tuple[float, float]]) -> list[TelemetryReading]:
    return [
        TelemetryReading(t, p, classify(t, p), timestamp=T0 + timedelta(seconds=0.1 * i))
        for i, (t, p) in enumerate(values)
    ]


def test_statistics() -> None:
    lay = summarise_layer(readings([(229.0, 44.0), (230.0, 45.0), (231.0, 46.0)]), "P", "W", 3, 0.1)
    assert lay.n_samples == 3
    assert lay.temp_mean == pytest.approx(230.0)
    assert (lay.temp_min, lay.temp_max) == (229.0, 231.0)
    assert lay.temp_sd == pytest.approx(1.0)
    assert lay.temp_cv_pct == pytest.approx(100 / 230, abs=1e-3)
    assert lay.pressure_mean == pytest.approx(45.0)
    assert lay.status == "OPTIMAL" and lay.anomaly_count == 0


def test_anomaly_offsets_and_status() -> None:
    lay = summarise_layer(readings([(230, 45), (236, 45), (230, 45), (230, 49)]), "P", "W", 0, 0.1)
    assert lay.status == "ANOMALY_WARNING"
    assert lay.anomaly_offsets == (1, 3)
    assert lay.sample_time(3) == T0 + timedelta(seconds=0.3)  # implicit timestamps


@pytest.mark.parametrize(
    ("policy", "clean_raw", "anomalous_raw"),
    [("anomaly", False, True), ("all", True, True), ("none", False, False)],
)
def test_raw_policy(policy: str, clean_raw: bool, anomalous_raw: bool) -> None:
    clean = summarise_layer(readings([(230, 45)] * 5), "P", "W", 0, 0.1, policy)
    bad = summarise_layer(readings([(230, 45)] * 4 + [(240, 45)]), "P", "W", 1, 0.1, policy)
    assert clean.raw_stored is clean_raw
    assert bad.raw_stored is anomalous_raw
    if anomalous_raw:
        assert bad.raw_temps == (230, 230, 230, 230, 240)


def test_properties_clear_raw_keys_when_not_kept() -> None:
    props = summarise_layer(readings([(230, 45)] * 5), "P", "W", 0, 0.1).to_properties()
    assert props["raw_temps"] is None and props["raw_pressures"] is None
    assert props["raw_stored"] is False
    assert "timestamps" not in props  # timestamps are implicit, never stored


def test_ids_are_deterministic_and_chained() -> None:
    lay = summarise_layer(readings([(230, 45)]), "Printer_01", "Wall_A", 7, 0.1)
    assert lay.id == "Printer_01/Wall_A/L0007" == layer_id("Printer_01", "Wall_A", 7)
    assert lay.previous_id == "Printer_01/Wall_A/L0006"
    first = summarise_layer(readings([(230, 45)]), "Printer_01", "Wall_A", 0, 0.1)
    assert first.previous_id is None


def test_compression_on_simulated_layers() -> None:
    """10 layers × 600 samples become 10 nodes; seed 42 gives both clean and anomalous layers."""
    sim = PLCExtruderSimulator(seed=42)
    layers = [
        summarise_layer([sim.read_registers() for _ in range(600)], "P", "W", i, 0.1)
        for i in range(10)
    ]
    assert sum(lay.n_samples for lay in layers) == 6000
    statuses = {lay.status for lay in layers}
    assert statuses == {"OPTIMAL", "ANOMALY_WARNING"}
    for lay in layers:  # default policy: raw only where it matters
        assert lay.raw_stored == (lay.status == "ANOMALY_WARNING")
    assert np.isclose(np.mean([lay.temp_mean for lay in layers]), 230.0, atol=0.6)


def test_rejects_empty_layer_and_bad_policy() -> None:
    with pytest.raises(ValueError):
        summarise_layer([], "P", "W", 0, 0.1)
    with pytest.raises(ValueError):
        summarise_layer(readings([(230, 45)]), "P", "W", 0, 0.1, "sometimes")  # type: ignore[arg-type]
