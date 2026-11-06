"""Neo4j bridge for GraphPrint-Twin (FR-2, FR-3, FR-4).

:class:`DigitalTwinBridge` owns the connection to Neo4j and every Cypher
statement the twin runs. Three rules apply throughout:

* **Parameterised only.** Values always travel as query parameters and are
  never formatted into query text. That avoids injection and lets Neo4j cache
  query plans.
* **Managed transactions.** Writes go through ``session.execute_write``, which
  retries transient errors (leader switches, deadlocks) automatically.
* **Idempotent topology.** Constraints use ``IF NOT EXISTS`` and topology uses
  ``MERGE``, so start-up can run any number of times without duplicates.

The schema is documented in ``docs/graph-schema.md`` at the repo root.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from typing import Any

from neo4j import Driver, GraphDatabase, ManagedTransaction
from neo4j.exceptions import ServiceUnavailable
from plc_simulator import TelemetryReading

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Baseline factory topology (see docs/graph-schema.md)
# ---------------------------------------------------------------------------
PLC_ID = "PLC_01"
PRINTER_ID = "Printer_01"
COMPONENT_ID = "Printer_01/StepperMotor_X"
WALL_ID = "Studio_Unit_BackWall"
BATCH_ID = "BATCH-rPETG-GF-001"

CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT plc_id IF NOT EXISTS FOR (n:PLC_Controller) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT printer_id IF NOT EXISTS FOR (n:RoboticPrinter) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT component_id IF NOT EXISTS FOR (n:Component) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT wall_id IF NOT EXISTS FOR (n:WallSegment) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT batch_id IF NOT EXISTS FOR (n:MaterialBatch) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT log_id IF NOT EXISTS FOR (n:TelemetryLog) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT alert_id IF NOT EXISTS FOR (n:MaintenanceAlert) REQUIRE n.id IS UNIQUE",
)

TOPOLOGY_QUERY = """
MERGE (plc:PLC_Controller {id: $plc_id})
  ON CREATE SET plc.ip = $plc_ip
MERGE (p:RoboticPrinter {id: $printer_id})
  ON CREATE SET p.type = $printer_type
MERGE (c:Component {id: $component_id})
  ON CREATE SET c.name = $component_name, c.axis = $component_axis
MERGE (w:WallSegment {id: $wall_id})
  ON CREATE SET w.required_layers = $required_layers
MERGE (m:MaterialBatch {id: $batch_id})
  ON CREATE SET m.vendor = $batch_vendor, m.polymer = $batch_polymer, m.fibre_pct = $batch_fibre_pct
MERGE (plc)-[:CONTROLS]->(p)
MERGE (p)-[:HAS_COMPONENT]->(c)
MERGE (w)-[:USES_MATERIAL]->(m)
"""

TOPOLOGY_PARAMS: dict[str, Any] = {
    "plc_id": PLC_ID,
    "plc_ip": "192.168.10.21",
    "printer_id": PRINTER_ID,
    "printer_type": "Gantry-3D",
    "component_id": COMPONENT_ID,
    "component_name": "StepperMotor",
    "component_axis": "X",
    "wall_id": WALL_ID,
    "required_layers": 160,
    "batch_id": BATCH_ID,
    "batch_vendor": "Simulated Polymers (Pty) Ltd",
    "batch_polymer": "rPETG",
    "batch_fibre_pct": 15,
}

# MATCH first, then CREATE: a reading is only written if its printer and wall
# exist, so no TelemetryLog can float unattached (FR-2).
INGEST_QUERY = """
MATCH (p:RoboticPrinter {id: $printer_id})
MATCH (w:WallSegment {id: $wall_id})
CREATE (t:TelemetryLog {
    id: $log_id,
    timestamp: $timestamp,
    nozzle_temp: $nozzle_temp,
    pump_pressure: $pump_pressure,
    status: $status
})
CREATE (p)-[:GENERATED]->(t)
CREATE (t)-[:MONITORS]->(w)
RETURN t.id AS id
"""

TRACEBACK_QUERY = """
MATCH (p:RoboticPrinter)-[:GENERATED]->(t:TelemetryLog {status: "ANOMALY_WARNING"})
      -[:MONITORS]->(w:WallSegment)-[:USES_MATERIAL]->(m:MaterialBatch)
RETURN t.id AS log_id,
       toString(t.timestamp) AS timestamp,
       t.nozzle_temp AS nozzle_temp,
       t.pump_pressure AS pump_pressure,
       p.id AS printer,
       w.id AS wall_segment,
       m.id AS batch,
       m.vendor AS vendor
ORDER BY t.timestamp
"""

# Counts used to verify a run: logs written by this run and how many are fully linked.
RUN_INTEGRITY_QUERY = """
UNWIND $log_ids AS log_id
OPTIONAL MATCH (t:TelemetryLog {id: log_id})
OPTIONAL MATCH linked = (:RoboticPrinter)-[:GENERATED]->(t)-[:MONITORS]->(:WallSegment)
RETURN count(DISTINCT t) AS found, count(DISTINCT linked) AS linked
"""

PRUNE_QUERY = """
MATCH (t:TelemetryLog {status: "OPTIMAL"})
WHERE t.timestamp < datetime() - duration({days: $days})
CALL { WITH t DETACH DELETE t } IN TRANSACTIONS OF 1000 ROWS
"""


class TopologyMissingError(RuntimeError):
    """Raised when a reading targets a printer or wall that isn't in the graph."""


class DigitalTwinBridge:
    """Connection to the Neo4j digital twin and all of GraphPrint-Twin's Cypher.

    Args:
        uri: Bolt URI, e.g. ``bolt://localhost:7687``.
        user: Username, or ``None`` when Neo4j runs with ``NEO4J_AUTH=none``.
        password: Password, or ``None``.
        database: Target database name. ``None`` uses the server default.
    """

    def __init__(
        self,
        uri: str,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
    ) -> None:
        auth = (user, password or "") if user else None
        self._driver: Driver = GraphDatabase.driver(uri, auth=auth)
        self._database = database
        self.uri = uri

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def from_env(cls) -> DigitalTwinBridge:
        """Build a bridge from the NEO4J_URI / _USER / _PASSWORD / _DATABASE env vars."""
        return cls(
            uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_USER") or None,
            password=os.getenv("NEO4J_PASSWORD") or None,
            database=os.getenv("NEO4J_DATABASE") or None,
        )

    def wait_until_ready(self, timeout_s: float | None = None, poll_s: float = 2.0) -> None:
        """Block until Neo4j accepts connections, or raise after ``timeout_s``.

        Useful in CI and docker-compose, where the database may still be starting up.
        The default timeout comes from ``NEO4J_WAIT_SECONDS`` (60 s if unset).
        """
        if timeout_s is None:
            timeout_s = float(os.getenv("NEO4J_WAIT_SECONDS", "60"))
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self._driver.verify_connectivity()
                log.info("Connected to Neo4j at %s", self.uri)
                return
            except (ServiceUnavailable, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise ServiceUnavailable(
                        f"Neo4j at {self.uri} not reachable after {timeout_s:.0f}s"
                    ) from exc
                log.info("Waiting for Neo4j at %s ...", self.uri)
                time.sleep(poll_s)

    def close(self) -> None:
        """Close the driver and its connection pool."""
        self._driver.close()

    def __enter__(self) -> DigitalTwinBridge:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- schema & topology (FR-2) -------------------------------------------

    def create_constraints(self) -> None:
        """Create the uniqueness constraints on every ``id`` field (idempotent)."""
        with self._driver.session(database=self._database) as session:
            for statement in CONSTRAINTS:
                session.run(statement).consume()
        log.info("Constraints in place (%d)", len(CONSTRAINTS))

    @staticmethod
    def _merge_topology(tx: ManagedTransaction) -> None:
        tx.run(TOPOLOGY_QUERY, TOPOLOGY_PARAMS).consume()

    def initialize_factory_topology(self) -> None:
        """Create constraints and MERGE the baseline factory layout (safe to re-run)."""
        self.create_constraints()
        with self._driver.session(database=self._database) as session:
            session.execute_write(self._merge_topology)
        log.info(
            "Topology ready: %s -> %s -> %s; %s -> %s",
            PLC_ID,
            PRINTER_ID,
            COMPONENT_ID,
            WALL_ID,
            BATCH_ID,
        )

    # -- ingestion (FR-3) ------------------------------------------------------

    @staticmethod
    def _ingest(tx: ManagedTransaction, params: dict[str, Any]) -> str | None:
        record = tx.run(INGEST_QUERY, params).single()
        return record["id"] if record else None

    def ingest_telemetry(self, printer_id: str, wall_id: str, reading: TelemetryReading) -> str:
        """Write one reading and link it to its printer and wall segment.

        Returns:
            The id of the created ``TelemetryLog`` node.

        Raises:
            TopologyMissingError: If the printer or wall segment doesn't exist.
        """
        params = {"printer_id": printer_id, "wall_id": wall_id, **reading.to_params()}
        with self._driver.session(database=self._database) as session:
            log_id = session.execute_write(self._ingest, params)
        if log_id is None:
            raise TopologyMissingError(
                f"No RoboticPrinter '{printer_id}' or WallSegment '{wall_id}' in the graph; "
                "run initialize_factory_topology() first"
            )
        return log_id

    # -- analysis (FR-4) -------------------------------------------------------

    def trace_anomalies(self) -> list[dict[str, Any]]:
        """Forensic traceback: every ANOMALY_WARNING reading with its wall, batch and vendor."""
        with self._driver.session(database=self._database) as session:
            return session.execute_read(lambda tx: [r.data() for r in tx.run(TRACEBACK_QUERY)])

    def verify_run(self, log_ids: Iterable[str]) -> dict[str, int]:
        """Check that every reading from a run exists and is linked to printer and wall."""
        ids = list(log_ids)
        with self._driver.session(database=self._database) as session:
            record = session.execute_read(
                lambda tx: tx.run(RUN_INTEGRITY_QUERY, {"log_ids": ids}).single()
            )
        found = record["found"] if record else 0
        linked = record["linked"] if record else 0
        return {"expected": len(ids), "found": found, "linked": linked}

    # -- housekeeping (NFR 5.1) ------------------------------------------------

    def prune_optimal_logs(self, days: int = 30) -> None:
        """Delete OPTIMAL readings older than ``days``. Anomalies are always kept."""
        if days < 1:
            raise ValueError("days must be >= 1")
        with self._driver.session(database=self._database) as session:
            # CALL ... IN TRANSACTIONS must run in an auto-commit (implicit) transaction.
            session.run(PRUNE_QUERY, {"days": days}).consume()
        log.info("Pruned OPTIMAL telemetry older than %d days", days)
