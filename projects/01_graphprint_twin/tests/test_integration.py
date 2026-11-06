"""End-to-end test against a live Neo4j. Runs only when NEO4J_URI is set (CI does this)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("NEO4J_URI"), reason="NEO4J_URI not set; no live Neo4j available"
)


@pytest.fixture(scope="module")
def twin():
    from database import DigitalTwinBridge

    bridge = DigitalTwinBridge.from_env()
    bridge.wait_until_ready()
    bridge.initialize_factory_topology()
    yield bridge
    bridge.close()


def _count(twin, query: str) -> int:
    with twin._driver.session(database=twin._database) as s:
        return s.run(query).single()[0]


def test_topology_is_idempotent(twin) -> None:
    twin.initialize_factory_topology()
    twin.initialize_factory_topology()
    assert _count(twin, "MATCH (n:RoboticPrinter {id: 'Printer_01'}) RETURN count(n)") == 1
    assert (
        _count(twin, "MATCH (n:Component {id: 'Printer_01/StepperMotor_X'}) RETURN count(n)") == 1
    )
    assert (
        _count(twin, "MATCH (:WallSegment)-[r:USES_MATERIAL]->(:MaterialBatch) RETURN count(r)")
        == 1
    )


def test_isa95_hierarchy_and_standards_ids(twin) -> None:
    path = (
        "MATCH (:Site)-[:CONTAINS]->(:Area)-[:CONTAINS]->(:WorkCenter)-[:CONTAINS]->"
        "(p:RoboticPrinter {id: 'Printer_01'})-[:HAS_COMPONENT]->(c:Component) "
        "WHERE p.aas_id STARTS WITH 'urn:' AND c.aas_id STARTS WITH 'urn:' RETURN count(*)"
    )
    assert _count(twin, path) == 1
    wall = "MATCH (w:WallSegment) WHERE size(w.ifc_guid) = 22 RETURN count(w)"
    assert _count(twin, wall) == 1


def test_ingested_readings_are_linked_and_traceable(twin) -> None:
    from database import PRINTER_ID, WALL_ID
    from plc_simulator import PLCExtruderSimulator

    sim = PLCExtruderSimulator(seed=42)
    readings = [sim.read_registers() for _ in range(10)]
    ids = [twin.ingest_telemetry(PRINTER_ID, WALL_ID, r) for r in readings]

    assert twin.verify_run(ids) == {"expected": 10, "found": 10, "linked": 10}

    anomalous = {r.id for r in readings if r.status == "ANOMALY_WARNING"}
    traced = {row["log_id"]: row for row in twin.trace_anomalies()}
    assert anomalous, "seed 42 should produce at least one anomaly"
    assert anomalous <= traced.keys()
    assert all(traced[i]["batch"] == "BATCH-rPETG-GF-001" for i in anomalous)
    assert all(traced[i]["recycle_generation"] == 1 for i in anomalous)
    assert all(len(traced[i]["wall_ifc_guid"]) == 22 for i in anomalous)


def test_ingest_into_unknown_wall_fails(twin) -> None:
    from database import TopologyMissingError
    from plc_simulator import PLCExtruderSimulator

    with pytest.raises(TopologyMissingError):
        twin.ingest_telemetry("Printer_01", "No_Such_Wall", PLCExtruderSimulator().read_registers())
