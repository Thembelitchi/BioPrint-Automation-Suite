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


def _count(twin, query: str, **params) -> int:
    with twin._driver.session(database=twin._database) as s:
        return s.run(query, params).single()[0]


def _layers(n: int, seed: int = 42, raw_policy: str = "anomaly"):
    from database import PRINTER_ID, WALL_ID
    from layer_aggregator import summarise_layer
    from plc_simulator import PLCExtruderSimulator

    sim = PLCExtruderSimulator(seed=seed)
    return [
        summarise_layer(
            [sim.read_registers() for _ in range(600)], PRINTER_ID, WALL_ID, i, 0.1, raw_policy
        )
        for i in range(n)
    ]


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


def test_layers_are_linked_chained_and_traceable(twin) -> None:
    layers = _layers(10)
    ids = [twin.ingest_layer(lay) for lay in layers]

    result = twin.verify_run(ids)
    assert result["found"] == result["linked"] == 10
    assert result["chained"] >= 9
    assert result["samples"] == 6000

    anomalous = {lay.id for lay in layers if lay.status == "ANOMALY_WARNING"}
    traced = {row["layer_id"]: row for row in twin.trace_anomalies()}
    assert anomalous, "seed 42 should produce at least one anomalous layer"
    assert anomalous <= traced.keys()
    for lid in anomalous:
        row = traced[lid]
        assert row["batch"] == "BATCH-rPETG-GF-001"
        assert row["recycle_generation"] == 1
        assert len(row["wall_ifc_guid"]) == 22
        assert row["raw_stored"] is True and row["anomaly_samples"] == len(row["anomaly_offsets"])


def test_reingest_updates_instead_of_duplicating(twin) -> None:
    layers = _layers(3)
    for _ in range(3):  # retried / replayed writes
        for lay in layers:
            twin.ingest_layer(lay)
    lid = layers[1].id
    assert _count(twin, "MATCH (b:LayerTelemetry {id: $lid}) RETURN count(b)", lid=lid) == 1
    assert (
        _count(
            twin,
            "MATCH (:RoboticPrinter)-[r:GENERATED]->(:LayerTelemetry {id: $lid}) RETURN count(r)",
            lid=lid,
        )
        == 1
    )
    assert (
        _count(
            twin, "MATCH (:LayerTelemetry {id: $lid})-[r:NEXT_LAYER]->() RETURN count(r)", lid=lid
        )
        == 1
    )


def test_raw_arrays_only_on_anomalous_layers(twin) -> None:
    for lay in _layers(10):
        twin.ingest_layer(lay)
    mismatched = _count(
        twin,
        "MATCH (b:LayerTelemetry) WHERE b.raw_stored <> (b.status = 'ANOMALY_WARNING') "
        "RETURN count(b)",
    )
    assert mismatched == 0
    clean_with_arrays = _count(
        twin,
        "MATCH (b:LayerTelemetry {status: 'OPTIMAL'}) WHERE b.raw_temps IS NOT NULL "
        "RETURN count(b)",
    )
    assert clean_with_arrays == 0


def test_ingest_into_unknown_wall_fails(twin) -> None:
    from database import TopologyMissingError
    from layer_aggregator import summarise_layer
    from plc_simulator import PLCExtruderSimulator

    sim = PLCExtruderSimulator()
    lay = summarise_layer([sim.read_registers()], "Printer_01", "No_Such_Wall", 0, 0.1)
    with pytest.raises(TopologyMissingError):
        twin.ingest_layer(lay)
