"""Unit tests for the alert sinks (FR-4), without a Neo4j server."""

from __future__ import annotations

import asyncio
import re

import alert_store
from alert_store import (
    BOOTSTRAP_QUERY,
    CLOSE_ALERTS_QUERY,
    COMPONENT_ID,
    CONSTRAINTS,
    RAISE_ALERT_QUERY,
    InMemoryAlertSink,
)


def test_in_memory_sink_keeps_one_open_alert_and_only_raises_the_peak() -> None:
    async def scenario():
        sink = InMemoryAlertSink()
        first = await sink.raise_alert(COMPONENT_ID, 0.90, "HIGH")
        higher = await sink.raise_alert(COMPONENT_ID, 0.97, "CRITICAL")
        lower = await sink.raise_alert(COMPONENT_ID, 0.88, "HIGH")
        state = await sink.alert_state(COMPONENT_ID)
        return first, higher, lower, state

    first, higher, lower, state = asyncio.run(scenario())
    assert first.created and not higher.created and not lower.created
    assert first.id == higher.id == lower.id
    assert (lower.severity, lower.peak_risk_score) == ("CRITICAL", 0.97)  # never downgraded
    assert state == {"components": 1, "open_alerts": 1}


def test_closing_an_alert_lets_the_next_fault_open_a_new_one() -> None:
    async def scenario():
        sink = InMemoryAlertSink()
        first = await sink.raise_alert(COMPONENT_ID, 0.90, "HIGH")
        closed = await sink.close_open_alerts(COMPONENT_ID)
        second = await sink.raise_alert(COMPONENT_ID, 0.86, "HIGH")
        return first, closed, second

    first, closed, second = asyncio.run(scenario())
    assert closed == 1
    assert second.created and second.id != first.id


def test_queries_are_parameterised_constants() -> None:
    for query in (BOOTSTRAP_QUERY, RAISE_ALERT_QUERY, CLOSE_ALERTS_QUERY):
        assert "{}" not in query and "%s" not in query  # no string formatting hooks
        assert re.search(r"\$component_id\b", query)
    for literal in ("Printer_01", "StepperMotor", "ALERT-"):
        assert literal not in RAISE_ALERT_QUERY


def test_raise_query_is_idempotent_by_construction() -> None:
    query = RAISE_ALERT_QUERY
    assert query.count("CREATE") == 1  # the alert, guarded by "no OPEN alert"
    assert "CASE WHEN open IS NULL THEN [1] ELSE [] END" in query
    assert "$risk > open.peak_risk_score" in query  # peak and severity only go up
    assert query.index("SET c.last_alert_at") < query.index("OPTIONAL MATCH")  # lock first


def test_bootstrap_merges_and_matches_the_twin_topology() -> None:
    assert "CREATE" not in BOOTSTRAP_QUERY.replace("ON CREATE", "")
    assert BOOTSTRAP_QUERY.count("MERGE") == 3
    assert "MERGE (p)-[:HAS_COMPONENT]->(c)" in BOOTSTRAP_QUERY
    assert alert_store.COMPONENT_ID == "Printer_01/StepperMotor_X"
    assert all("IF NOT EXISTS" in statement for statement in CONSTRAINTS)
