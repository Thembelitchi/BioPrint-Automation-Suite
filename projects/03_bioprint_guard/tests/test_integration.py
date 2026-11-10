"""End-to-end tests against a live Neo4j: OPC UA server -> gateway -> shared digital twin (FR-4).

They run only when NEO4J_URI is set; scripts/verify.sh does this when Docker is available.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("NEO4J_URI"), reason="NEO4J_URI not set; no live Neo4j available"
)

COMPONENT = "Printer_01/StepperMotor_X"


async def _query(sink, query: str, **params):
    async with sink._driver.session(database=sink._database) as session:
        result = await session.run(query, params)
        return [record.data() async for record in result]


async def _run_once(close_first: bool = False):
    from alert_store import Neo4jAlertSink
    from app import run_scenario

    sink = Neo4jAlertSink.from_env()
    try:
        if close_first:
            await sink.prepare()
            await sink.close_open_alerts(COMPONENT)
        report, _, _ = await run_scenario(speed=20.0, sink=sink)
        state = await sink.alert_state(COMPONENT)
        alerts = await _query(
            sink,
            "MATCH (p:RoboticPrinter)-[:HAS_COMPONENT]->(c:Component {id: $id})"
            "-[:REQUIRES_INSPECTION]->(a:MaintenanceAlert) "
            "RETURN p.id AS printer, a.id AS id, a.status AS status, a.severity AS severity, "
            "a.peak_risk_score AS peak, a.created_at <= a.updated_at AS ordered",
            id=COMPONENT,
        )
        nodes = await _query(
            sink,
            "MATCH (c:Component {id: $id}) "
            "OPTIONAL MATCH (p:RoboticPrinter)-[r:HAS_COMPONENT]->(c) "
            "RETURN count(DISTINCT c) AS components, count(r) AS links, "
            "collect(DISTINCT c.aas_id) AS aas_ids",
            id=COMPONENT,
        )
        return report, state, alerts, nodes[0]
    finally:
        await sink.close()


def test_fault_opens_exactly_one_alert_on_the_shared_component() -> None:
    report, state, alerts, nodes = asyncio.run(_run_once(close_first=True))
    assert state == {"components": 1, "open_alerts": 1}
    open_alerts = [a for a in alerts if a["status"] == "OPEN"]
    assert len(open_alerts) == 1
    alert = open_alerts[0]
    assert alert["printer"] == "Printer_01"
    assert alert["id"] == report.alert.id
    assert alert["severity"] == "CRITICAL"
    assert alert["peak"] == pytest.approx(max(r.scored.score for r in report.records))
    assert alert["ordered"]
    assert nodes == {
        "components": 1,
        "links": 1,
        "aas_ids": ["urn:bioprint:aas:Printer_01/StepperMotor_X"],
    }


def test_a_second_run_updates_the_open_alert_instead_of_duplicating_it() -> None:
    _, _, before, _ = asyncio.run(_run_once(close_first=True))
    report, state, after, nodes = asyncio.run(_run_once())
    assert state == {"components": 1, "open_alerts": 1}
    open_before = [a["id"] for a in before if a["status"] == "OPEN"]
    open_after = [a["id"] for a in after if a["status"] == "OPEN"]
    assert open_after == open_before
    assert report.alert.created is False
    assert nodes["components"] == 1 and nodes["links"] == 1


def test_closed_alerts_are_kept_as_history() -> None:
    _, _, alerts, _ = asyncio.run(_run_once(close_first=True))
    assert sum(a["status"] == "OPEN" for a in alerts) == 1
    assert sum(a["status"] == "CLOSED" for a in alerts) >= 1


def test_cross_project_query_runs_against_the_shared_schema() -> None:
    from alert_store import Neo4jAlertSink

    async def scenario():
        sink = Neo4jAlertSink.from_env()
        try:
            return await _query(
                sink,
                "MATCH (p:RoboticPrinter)-[:HAS_COMPONENT]->(c:Component)"
                "-[:REQUIRES_INSPECTION]->(a:MaintenanceAlert {status: 'OPEN'}) "
                "OPTIONAL MATCH (p)-[:GENERATED]->(:LayerTelemetry)-[:MONITORS]->(w:WallSegment) "
                "RETURN c.id AS component, a.severity AS severity, "
                "collect(DISTINCT w.id) AS walls",
            )
        finally:
            await sink.close()

    rows = asyncio.run(scenario())
    assert [r["component"] for r in rows] == [COMPONENT]
    assert isinstance(rows[0]["walls"], list)  # filled once GraphPrint-Twin has run
