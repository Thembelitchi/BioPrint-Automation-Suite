"""Unit tests for DigitalTwinBridge using a fake driver (no Neo4j server needed)."""

from __future__ import annotations

import re
from typing import Any

import database
import pytest
from database import (
    LAYER_INGEST_QUERY,
    TOPOLOGY_PARAMS,
    TOPOLOGY_QUERY,
    DigitalTwinBridge,
    TopologyMissingError,
)
from layer_aggregator import summarise_layer
from plc_simulator import PLCExtruderSimulator


def make_layer(index: int = 0, printer: str = "Printer_01", wall: str = "Studio_Unit_BackWall"):
    sim = PLCExtruderSimulator(seed=index)
    return summarise_layer([sim.read_registers() for _ in range(50)], printer, wall, index, 0.1)


class FakeRecord(dict):
    def data(self) -> dict[str, Any]:
        return dict(self)


class FakeResult:
    def __init__(self, records: list[FakeRecord]) -> None:
        self._records = records

    def single(self) -> FakeRecord | None:
        return self._records[0] if self._records else None

    def consume(self) -> None:
        return None

    def __iter__(self):
        return iter(self._records)


class FakeTx:
    def __init__(self, calls: list, rows: list[FakeRecord]) -> None:
        self.calls = calls
        self.rows = rows

    def run(self, query: str, params: dict | None = None) -> FakeResult:
        self.calls.append((query, params or {}))
        return FakeResult(self.rows)


class FakeSession:
    def __init__(self, driver: FakeDriver) -> None:
        self.driver = driver

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def run(self, query: str, params: dict | None = None) -> FakeResult:
        self.driver.calls.append((query, params or {}))
        return FakeResult([])

    def execute_write(self, fn, *args):
        return fn(FakeTx(self.driver.calls, self.driver.rows), *args)

    execute_read = execute_write


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.rows: list[FakeRecord] = []

    def session(self, database: str | None = None) -> FakeSession:
        return FakeSession(self)

    def close(self) -> None:
        return None


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> tuple[DigitalTwinBridge, FakeDriver]:
    fake = FakeDriver()
    monkeypatch.setattr(database.GraphDatabase, "driver", lambda uri, auth=None: fake)
    return DigitalTwinBridge("bolt://fake:7687"), fake


def _placeholders(query: str) -> set[str]:
    return set(re.findall(r"\$(\w+)", query))


def test_queries_are_fully_parameterised() -> None:
    assert _placeholders(LAYER_INGEST_QUERY) == {
        "printer_id",
        "wall_id",
        "layer_id",
        "prev_layer_id",
        "props",
    }
    assert _placeholders(TOPOLOGY_QUERY) == set(TOPOLOGY_PARAMS)
    # No Python formatting artefacts left in the Cypher text.
    assert "{}" not in LAYER_INGEST_QUERY and "%s" not in LAYER_INGEST_QUERY


def test_topology_is_merge_only() -> None:
    assert "CREATE " not in TOPOLOGY_QUERY.replace("ON CREATE", "")


def test_layer_ingest_is_idempotent() -> None:
    """Retried writes must update, never duplicate: no CREATE anywhere in the layer query."""
    assert "CREATE" not in LAYER_INGEST_QUERY
    assert "MERGE (b:LayerTelemetry {id: $layer_id})" in LAYER_INGEST_QUERY
    assert "MERGE (p)-[:GENERATED]->(b)" in LAYER_INGEST_QUERY
    assert "MERGE (prev)-[:NEXT_LAYER]->(b)" in LAYER_INGEST_QUERY


def test_initialize_creates_constraints_then_topology(bridge) -> None:
    twin, fake = bridge
    twin.initialize_factory_topology()
    queries = [q for q, _ in fake.calls]
    assert sum(q.startswith("CREATE CONSTRAINT") for q in queries) == len(database.CONSTRAINTS)
    assert queries[-1] == TOPOLOGY_QUERY


def test_ingest_sends_layer_as_parameters(bridge) -> None:
    twin, fake = bridge
    fake.rows = [FakeRecord(id="Printer_01/Studio_Unit_BackWall/L0003")]
    layer = make_layer(3)

    assert twin.ingest_layer(layer) == layer.id
    query, params = fake.calls[-1]
    assert query == LAYER_INGEST_QUERY
    assert params["layer_id"] == layer.id
    assert params["prev_layer_id"] == "Printer_01/Studio_Unit_BackWall/L0002"
    assert params["props"]["n_samples"] == 50
    assert params["props"]["temp_mean"] == layer.temp_mean


def test_ingest_raises_when_topology_missing(bridge) -> None:
    twin, fake = bridge
    fake.rows = []  # MATCH found nothing, so nothing was written
    with pytest.raises(TopologyMissingError):
        twin.ingest_layer(make_layer(0, printer="Ghost", wall="Nowhere"))


def test_prune_rejects_bad_window(bridge) -> None:
    twin, _ = bridge
    with pytest.raises(ValueError):
        twin.prune_raw_arrays(0)


def test_main_loop_writes_one_bucket_per_layer(monkeypatch) -> None:
    """Drive main.run() end to end with an in-memory bridge."""
    import main

    written = []

    class MemoryBridge:
        def ingest_layer(self, layer):
            written.append(layer)
            return layer.id

    ids = main.run(
        MemoryBridge(),
        PLCExtruderSimulator(seed=42),
        layers=10,
        samples_per_layer=600,
        interval_s=0.0,
    )
    assert len(ids) == len(written) == 10
    assert sum(lay.n_samples for lay in written) == 6000
    assert [lay.layer_index for lay in written] == list(range(10))
    assert written[1].start_time > written[0].end_time  # continuous simulated clock
