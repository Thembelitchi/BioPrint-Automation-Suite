"""Unit tests for DigitalTwinBridge using a fake driver (no Neo4j server needed)."""

from __future__ import annotations

import re
from typing import Any

import database
import pytest
from database import (
    INGEST_QUERY,
    TOPOLOGY_PARAMS,
    TOPOLOGY_QUERY,
    DigitalTwinBridge,
    TopologyMissingError,
)
from plc_simulator import PLCExtruderSimulator


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
    reading = PLCExtruderSimulator(seed=1).read_registers()
    ingest_params = {"printer_id", "wall_id", *reading.to_params()}
    assert _placeholders(INGEST_QUERY) == ingest_params
    assert _placeholders(TOPOLOGY_QUERY) == set(TOPOLOGY_PARAMS)
    # No Python formatting artefacts left in the Cypher text.
    assert "{}" not in INGEST_QUERY and "%s" not in INGEST_QUERY


def test_topology_is_merge_only() -> None:
    assert "CREATE " not in TOPOLOGY_QUERY.replace("ON CREATE", "")


def test_initialize_creates_constraints_then_topology(bridge) -> None:
    twin, fake = bridge
    twin.initialize_factory_topology()
    queries = [q for q, _ in fake.calls]
    assert sum(q.startswith("CREATE CONSTRAINT") for q in queries) == len(database.CONSTRAINTS)
    assert queries[-1] == TOPOLOGY_QUERY


def test_ingest_sends_reading_as_parameters(bridge) -> None:
    twin, fake = bridge
    fake.rows = [FakeRecord(id="abc")]
    reading = PLCExtruderSimulator(seed=2).read_registers()

    assert twin.ingest_telemetry("Printer_01", "Studio_Unit_BackWall", reading) == "abc"
    query, params = fake.calls[-1]
    assert query == INGEST_QUERY
    assert params["nozzle_temp"] == reading.nozzle_temp
    assert params["status"] == reading.status
    assert params["printer_id"] == "Printer_01"


def test_ingest_raises_when_topology_missing(bridge) -> None:
    twin, fake = bridge
    fake.rows = []  # MATCH found nothing, so nothing was created
    with pytest.raises(TopologyMissingError):
        twin.ingest_telemetry("Ghost", "Nowhere", PLCExtruderSimulator(seed=0).read_registers())


def test_prune_rejects_bad_window(bridge) -> None:
    twin, _ = bridge
    with pytest.raises(ValueError):
        twin.prune_optimal_logs(0)
