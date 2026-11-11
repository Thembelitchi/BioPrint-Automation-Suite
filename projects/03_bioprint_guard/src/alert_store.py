"""Maintenance-alert sinks for BioPrint-Guard (FR-4).

:class:`Neo4jAlertSink` writes into the digital twin that GraphPrint-Twin owns
(schema: ``docs/graph-schema.md``). The rules are the same as in the Twin:

* **Parameterised only.** Values travel as query parameters.
* **Managed transactions.** Writes go through ``execute_write`` and are retried
  on transient errors.
* **Idempotent.** The printer and component are ``MERGE``d (Guard bootstraps
  them if the Twin has not run yet, with the same property values), and a
  component never has more than one ``OPEN`` alert: a new score either creates
  the alert or raises the peak of the open one.

:class:`InMemoryAlertSink` applies the same semantics without a database. The
gateway uses it when ``NEO4J_URI`` is not set, and the unit tests use it too.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("bioprint_guard.alerts")

PRINTER_ID = "Printer_01"
COMPONENT_ID = "Printer_01/StepperMotor_X"

CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT printer_id IF NOT EXISTS FOR (n:RoboticPrinter) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT component_id IF NOT EXISTS FOR (n:Component) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT alert_id IF NOT EXISTS FOR (n:MaintenanceAlert) REQUIRE n.id IS UNIQUE",
)

# Same ids and ON CREATE values as GraphPrint-Twin's topology, so whichever
# project runs first, the result is one printer and one component.
BOOTSTRAP_QUERY = """
MERGE (p:RoboticPrinter {id: $printer_id})
  ON CREATE SET p.type = $printer_type, p.isa95_level = "WorkUnit", p.aas_id = $printer_aas_id
MERGE (c:Component {id: $component_id})
  ON CREATE SET c.name = $component_name, c.axis = $component_axis,
                c.isa88_level = "ControlModule", c.aas_id = $component_aas_id
MERGE (p)-[:HAS_COMPONENT]->(c)
"""

# Raise or update, in one transaction:
#   * SET on the component first: this takes its write lock, so two gateways
#     scoring the same component cannot both see "no open alert" and create two.
#   * create the alert only if none is OPEN; otherwise raise its peak. Severity
#     and peak only ever go up while an alert is open.
RAISE_ALERT_QUERY = """
MATCH (c:Component {id: $component_id})
SET c.last_alert_at = datetime()
WITH c
OPTIONAL MATCH (c)-[:REQUIRES_INSPECTION]->(open:MaintenanceAlert {status: "OPEN"})
WITH c, open ORDER BY open.created_at LIMIT 1
FOREACH (_ IN CASE WHEN open IS NULL THEN [1] ELSE [] END |
  CREATE (c)-[:REQUIRES_INSPECTION]->(:MaintenanceAlert {
    id: $alert_id, status: "OPEN", severity: $severity, peak_risk_score: $risk,
    source: $source, created_at: datetime(), updated_at: datetime()}))
FOREACH (_ IN CASE WHEN open IS NOT NULL AND $risk > open.peak_risk_score THEN [1] ELSE [] END |
  SET open.severity = $severity, open.peak_risk_score = $risk, open.updated_at = datetime())
WITH c, open IS NULL AS created
MATCH (c)-[:REQUIRES_INSPECTION]->(a:MaintenanceAlert {status: "OPEN"})
RETURN a.id AS id, a.severity AS severity, a.peak_risk_score AS peak_risk_score, created
"""

CLOSE_ALERTS_QUERY = """
MATCH (:Component {id: $component_id})-[:REQUIRES_INSPECTION]->(a:MaintenanceAlert {status: "OPEN"})
SET a.status = "CLOSED", a.closed_at = datetime(), a.updated_at = datetime()
RETURN count(a) AS closed
"""

ALERT_STATE_QUERY = """
OPTIONAL MATCH (c:Component {id: $component_id})
OPTIONAL MATCH (c)-[:REQUIRES_INSPECTION]->(a:MaintenanceAlert {status: "OPEN"})
RETURN count(DISTINCT c) AS components, count(DISTINCT a) AS open_alerts
"""


@dataclass(frozen=True)
class AlertResult:
    """State of the component's open alert after a raise."""

    id: str
    severity: str
    peak_risk_score: float
    created: bool  # True if this call opened the alert, False if it updated one


class AlertSink(Protocol):
    """Where the gateway sends maintenance alerts."""

    async def prepare(self) -> None: ...

    async def raise_alert(self, component_id: str, risk: float, severity: str) -> AlertResult: ...

    async def alert_state(self, component_id: str) -> dict[str, int]: ...

    async def close(self) -> None: ...


def new_alert_id() -> str:
    return f"ALERT-{uuid.uuid4().hex[:12].upper()}"


class InMemoryAlertSink:
    """Alert sink with the graph's semantics and no database."""

    def __init__(self) -> None:
        self.open: dict[str, AlertResult] = {}
        self.writes = 0

    async def prepare(self) -> None:
        return None

    async def raise_alert(self, component_id: str, risk: float, severity: str) -> AlertResult:
        self.writes += 1
        current = self.open.get(component_id)
        if current is None:
            result = AlertResult(new_alert_id(), severity, risk, True)
        elif risk > current.peak_risk_score:
            result = AlertResult(current.id, severity, risk, False)
        else:
            result = AlertResult(current.id, current.severity, current.peak_risk_score, False)
        self.open[component_id] = result
        return result

    async def alert_state(self, component_id: str) -> dict[str, int]:
        return {"components": 1, "open_alerts": 1 if component_id in self.open else 0}

    async def close_open_alerts(self, component_id: str) -> int:
        return 1 if self.open.pop(component_id, None) else 0

    async def close(self) -> None:
        return None


class Neo4jAlertSink:
    """Alert sink backed by the shared Neo4j digital twin (async Bolt driver).

    Args:
        uri: Bolt URI, e.g. ``bolt://localhost:7687``.
        user: Username, or ``None`` when Neo4j runs with ``NEO4J_AUTH=none``.
        password: Password, or ``None``.
        database: Target database name. ``None`` uses the server default.
        printer_id: Printer that owns the monitored component (for the bootstrap).
        component_id: Component to bootstrap.
    """

    def __init__(
        self,
        uri: str,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        printer_id: str = PRINTER_ID,
        component_id: str = COMPONENT_ID,
    ) -> None:
        from neo4j import AsyncGraphDatabase

        auth = (user, password or "") if user else None
        self._driver = AsyncGraphDatabase.driver(uri, auth=auth)
        self._database = database
        self.uri = uri
        self.printer_id = printer_id
        self.component_id = component_id

    @classmethod
    def from_env(cls, **kwargs: Any) -> Neo4jAlertSink:
        """Build a sink from the NEO4J_URI / _USER / _PASSWORD / _DATABASE env vars."""
        return cls(
            uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_USER") or None,
            password=os.getenv("NEO4J_PASSWORD") or None,
            database=os.getenv("NEO4J_DATABASE") or None,
            **kwargs,
        )

    async def wait_until_ready(self, timeout_s: float | None = None, poll_s: float = 2.0) -> None:
        """Wait until Neo4j accepts connections (``NEO4J_WAIT_SECONDS``, 60 s if unset)."""
        from neo4j.exceptions import ServiceUnavailable

        if timeout_s is None:
            timeout_s = float(os.getenv("NEO4J_WAIT_SECONDS", "60"))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            try:
                await self._driver.verify_connectivity()
                log.info("Connected to Neo4j at %s", self.uri)
                return
            except (ServiceUnavailable, OSError) as exc:
                if loop.time() >= deadline:
                    raise ServiceUnavailable(
                        f"Neo4j at {self.uri} not reachable after {timeout_s:.0f}s"
                    ) from exc
                log.info("Waiting for Neo4j at %s ...", self.uri)
                await asyncio.sleep(poll_s)

    async def prepare(self) -> None:
        """Wait for Neo4j, create constraints and make sure the component exists."""
        await self.wait_until_ready()
        params = {
            "printer_id": self.printer_id,
            "printer_type": "Gantry-3D",
            "printer_aas_id": f"urn:bioprint:aas:{self.printer_id}",
            "component_id": self.component_id,
            "component_name": "StepperMotor",
            "component_axis": "X",
            "component_aas_id": f"urn:bioprint:aas:{self.component_id}",
        }
        async with self._driver.session(database=self._database) as session:
            for statement in CONSTRAINTS:
                await (await session.run(statement)).consume()

            async def bootstrap(tx: Any) -> None:
                await (await tx.run(BOOTSTRAP_QUERY, params)).consume()

            await session.execute_write(bootstrap)
        log.info("Twin ready: %s -[:HAS_COMPONENT]-> %s", self.printer_id, self.component_id)

    async def raise_alert(self, component_id: str, risk: float, severity: str) -> AlertResult:
        params = {
            "component_id": component_id,
            "alert_id": new_alert_id(),
            "risk": float(risk),
            "severity": severity,
            "source": "BioPrint-Guard",
        }

        async def work(tx: Any) -> dict[str, Any] | None:
            record = await (await tx.run(RAISE_ALERT_QUERY, params)).single()
            return dict(record) if record else None

        async with self._driver.session(database=self._database) as session:
            row = await session.execute_write(work)
        if row is None:
            raise LookupError(f"No Component '{component_id}' in the graph; run prepare() first")
        return AlertResult(row["id"], row["severity"], row["peak_risk_score"], row["created"])

    async def alert_state(self, component_id: str) -> dict[str, int]:
        """How many nodes carry this component id, and how many OPEN alerts it has."""

        async def work(tx: Any) -> dict[str, int]:
            result = await tx.run(ALERT_STATE_QUERY, {"component_id": component_id})
            return dict(await result.single())

        async with self._driver.session(database=self._database) as session:
            return await session.execute_read(work)

    async def close_open_alerts(self, component_id: str) -> int:
        """Mark the component's open alerts CLOSED (the inspection was done)."""

        async def work(tx: Any) -> int:
            result = await tx.run(CLOSE_ALERTS_QUERY, {"component_id": component_id})
            return (await result.single())["closed"]

        async with self._driver.session(database=self._database) as session:
            return await session.execute_write(work)

    async def close(self) -> None:
        await self._driver.close()
