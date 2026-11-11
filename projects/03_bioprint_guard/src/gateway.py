"""Async OPC UA gateway: subscribe, score, raise maintenance alerts (FR-2, FR-3, FR-4).

Three ``asyncio`` tasks joined by queues, so a slow stage never blocks the one
before it::

    OPC UA subscription ──> sample queue ──> scorer ──> alert queue ──> alert writer ──> Neo4j
       (intake task)                      (scorer task)                 (writer task)

* **Intake** keeps one session with **one subscription** carrying all three
  variables. If the server is unreachable or the session drops, it reconnects
  with exponential backoff. Per-variable notifications are reassembled into
  synchronous readings by their shared source timestamp.
* **Scorer** runs the streaming detector on every reading and decides when an
  alert is due: when the score first reaches 0.85, and again only when the peak
  rises, so the database sees a handful of writes instead of one per sample.
* **Writer** sends alerts to the sink. If writes back up, it merges the pending
  alerts into one write carrying the highest peak.

Run it against a server that is already up (from the project folder)::

    python src/gateway.py --duration 30
    NEO4J_URI=bolt://localhost:7687 python src/gateway.py --samples 300

Without ``NEO4J_URI`` alerts are kept in memory and logged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from alert_store import COMPONENT_ID, AlertResult, AlertSink, InMemoryAlertSink, Neo4jAlertSink
from anomaly_detector import ScoredSample, StreamingAnomalyDetector, severity_for
from asyncua import Client
from motor_model import SIGNALS
from opcua_server import NAMESPACE_URI, NODE_PATH, STATUS_FIELD

DEFAULT_ENDPOINT = "opc.tcp://localhost:4840/bioprint/"

log = logging.getLogger("bioprint_guard.gateway")


class Backoff:
    """Exponential backoff with a ceiling: 0.5, 1, 2, 4 ... up to ``maximum`` seconds."""

    def __init__(self, initial: float = 0.5, factor: float = 2.0, maximum: float = 10.0) -> None:
        if initial <= 0 or factor < 1 or maximum < initial:
            raise ValueError("need initial > 0, factor >= 1 and maximum >= initial")
        self.initial = initial
        self.factor = factor
        self.maximum = maximum
        self._next = initial

    def next_delay(self) -> float:
        delay = self._next
        self._next = min(self.maximum, self._next * self.factor)
        return delay

    def reset(self) -> None:
        self._next = self.initial


@dataclass(frozen=True)
class Reading:
    """One synchronous reading of all signals, reassembled from notifications."""

    timestamp: datetime
    values: tuple[float, ...]


class SampleAssembler:
    """Group per-variable notifications into readings by source timestamp.

    A reading is emitted once all signals with the same timestamp have arrived.
    Timestamps at or before the last emitted one are ignored, which drops the
    values a server replays when a subscription is re-created after a reconnect.
    """

    def __init__(self, signals: tuple[str, ...] = SIGNALS, max_pending: int = 64) -> None:
        self.signals = signals
        self.max_pending = max_pending
        self._pending: dict[datetime, dict[str, float]] = {}
        self._last_emitted: datetime | None = None
        self.incomplete_dropped = 0

    def add(self, signal: str, timestamp: datetime, value: float) -> Reading | None:
        if self._last_emitted is not None and timestamp <= self._last_emitted:
            return None
        partial = self._pending.setdefault(timestamp, {})
        partial[signal] = value
        if len(partial) < len(self.signals):
            if len(self._pending) > self.max_pending:
                del self._pending[min(self._pending)]
                self.incomplete_dropped += 1
            return None
        del self._pending[timestamp]
        # Anything older than a completed reading can no longer complete in order.
        for stale in [ts for ts in self._pending if ts < timestamp]:
            del self._pending[stale]
            self.incomplete_dropped += 1
        self._last_emitted = timestamp
        return Reading(timestamp, tuple(partial[name] for name in self.signals))


@dataclass
class ScoreRecord:
    """One scored reading, kept for the run report."""

    index: int
    timestamp: datetime
    values: tuple[float, ...]
    scored: ScoredSample


@dataclass
class GatewayReport:
    """What a gateway run did."""

    records: list[ScoreRecord] = field(default_factory=list)
    connections: int = 0
    connect_failures: int = 0
    subscriptions_open: int = 0  # subscriptions held at once (FR-2: must be 1)
    monitored_items: int = 0
    alert_writes: int = 0
    alert: AlertResult | None = None
    first_alert_index: int | None = None
    max_queue_depth: int = 0
    incomplete_dropped: int = 0

    @property
    def samples(self) -> int:
        return len(self.records)


class _SubscriptionHandler:
    """asyncua callback target: called in the client's event loop for every data change."""

    def __init__(self, gateway: OpcUaGateway) -> None:
        self._gateway = gateway

    def datachange_notification(self, node: Any, value: Any, data: Any) -> None:
        self._gateway._on_data_change(node, value, data)


class OpcUaGateway:
    """Subscribe to the motor's OPC UA variables, score them and route alerts.

    Args:
        endpoint: OPC UA endpoint of the motor server.
        sink: Where alerts go.
        detector: Streaming scorer. A default one is created if omitted.
        component_id: Graph id of the monitored component.
        publish_interval_ms: Subscription publishing interval.
        backoff: Reconnect schedule.
        connect_timeout_s: Timeout for OPC UA requests.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        sink: AlertSink | None = None,
        detector: StreamingAnomalyDetector | None = None,
        component_id: str = COMPONENT_ID,
        publish_interval_ms: float = 50.0,
        backoff: Backoff | None = None,
        connect_timeout_s: float = 4.0,
    ) -> None:
        self.endpoint = endpoint
        self.sink: AlertSink = sink or InMemoryAlertSink()
        self.detector = detector or StreamingAnomalyDetector()
        self.component_id = component_id
        self.publish_interval_ms = publish_interval_ms
        self.backoff = backoff or Backoff()
        self.connect_timeout_s = connect_timeout_s
        self.report = GatewayReport()
        self._assembler = SampleAssembler()
        self._samples: asyncio.Queue[Reading] = asyncio.Queue()
        self._alerts: asyncio.Queue[float] = asyncio.Queue()
        self._node_signals: dict[Any, str] = {}
        self._stop = asyncio.Event()
        self._peak_seen = 0.0  # highest alerting score scored so far
        self._peak_queued = 0.0  # highest score handed to the writer
        self._peak_written = 0.0  # highest score the sink has confirmed

    # -- intake (FR-2) -----------------------------------------------------------

    def _on_data_change(self, node: Any, value: Any, data: Any) -> None:
        data_value = data.monitored_item.Value
        status = getattr(data_value, STATUS_FIELD)
        if status is not None and not status.is_good():
            return  # e.g. BadWaitingForInitialData before the first sample
        timestamp = data_value.SourceTimestamp
        signal = self._node_signals.get(node.nodeid)
        if timestamp is None or signal is None or value is None:
            return
        reading = self._assembler.add(signal, timestamp, float(value))
        if reading is not None:
            self._samples.put_nowait(reading)
            self.report.max_queue_depth = max(self.report.max_queue_depth, self._samples.qsize())

    async def _subscribe(self, client: Client) -> Any:
        namespace = await client.get_namespace_index(NAMESPACE_URI)
        path = [f"{namespace}:{name}" for name in NODE_PATH]
        motor = await client.nodes.objects.get_child(path)
        nodes = [await motor.get_child(f"{namespace}:{signal}") for signal in SIGNALS]
        self._node_signals = {node.nodeid: s for node, s in zip(nodes, SIGNALS, strict=True)}
        subscription = await client.create_subscription(
            self.publish_interval_ms, _SubscriptionHandler(self)
        )
        # One call, one subscription, three monitored items. The queue holds every
        # value written between two publishes, so no sample is overwritten.
        handles = await subscription.subscribe_data_change(nodes, queuesize=256)
        self.report.subscriptions_open = 1
        self.report.monitored_items = len(handles)
        return subscription

    async def _intake(self) -> None:
        while not self._stop.is_set():
            try:
                client = Client(self.endpoint, timeout=self.connect_timeout_s)
                async with client:
                    await self._subscribe(client)
                    self.report.connections += 1
                    self.backoff.reset()
                    log.info(
                        "Connected to %s: 1 subscription, %d monitored items (%s)",
                        self.endpoint,
                        self.report.monitored_items,
                        "/".join(NODE_PATH),
                    )
                    while not self._stop.is_set():
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                        await client.check_connection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any transport or protocol failure means "reconnect"
                if self._stop.is_set():
                    break
                self.report.connect_failures += 1
                self.report.subscriptions_open = 0
                delay = self.backoff.next_delay()
                log.warning(
                    "OPC UA connection to %s lost or unavailable (%s: %s); retrying in %.1f s",
                    self.endpoint,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)

    # -- scoring (FR-3) and alert routing (FR-4) -------------------------------------

    def _score(self, reading: Reading) -> None:
        scored = self.detector.update(reading.values)
        index = len(self.report.records)
        self.report.records.append(ScoreRecord(index, reading.timestamp, reading.values, scored))
        if severity_for(scored.score) is None:
            return
        self._peak_seen = max(self._peak_seen, scored.score)
        # Alert when the threshold is first crossed, then only when the peak rises
        # by a visible step, so the graph is not written to on every sample. The
        # exact final peak is written once more when the run ends.
        if self._peak_queued == 0.0 or scored.score >= self._peak_queued + 0.01:
            if self.report.first_alert_index is None:
                self.report.first_alert_index = index
            self._peak_queued = scored.score
            self._alerts.put_nowait(scored.score)

    async def _scorer(self, max_samples: int | None) -> None:
        while True:
            self._score(await self._samples.get())
            if max_samples is not None and self.report.samples >= max_samples:
                self._stop.set()
                return

    async def _write_alert(self, risk: float) -> None:
        severity = severity_for(risk) or "HIGH"
        result = await self.sink.raise_alert(self.component_id, risk, severity)
        self._peak_written = max(self._peak_written, risk)
        self.report.alert_writes += 1
        self.report.alert = result
        log.warning(
            "%s MaintenanceAlert %s on %s: severity %s, peak risk %.3f",
            "Opened" if result.created else "Updated",
            result.id,
            self.component_id,
            result.severity,
            result.peak_risk_score,
        )

    async def _writer(self) -> None:
        while True:
            risk = await self._alerts.get()
            while not self._alerts.empty():  # coalesce: later entries carry a higher peak
                risk = max(risk, self._alerts.get_nowait())
            await self._write_alert(risk)

    # -- lifecycle ---------------------------------------------------------------------

    def stop(self) -> None:
        """Ask a running gateway to finish."""
        self._stop.set()

    async def run(
        self, max_samples: int | None = None, duration_s: float | None = None
    ) -> GatewayReport:
        """Run until ``max_samples`` readings are scored, ``duration_s`` passes or ``stop()``."""
        await self.sink.prepare()
        intake = asyncio.create_task(self._intake(), name="guard-intake")
        scorer = asyncio.create_task(self._scorer(max_samples), name="guard-scorer")
        writer = asyncio.create_task(self._writer(), name="guard-writer")
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=duration_s)
            self._stop.set()
            await intake
        finally:
            for task in (intake, scorer, writer):
                task.cancel()
            await asyncio.gather(intake, scorer, writer, return_exceptions=True)
        # Score whatever was still queued, then make sure the sink has the final peak.
        while not self._samples.empty():
            self._score(self._samples.get_nowait())
        if self._peak_seen > self._peak_written:
            await self._write_alert(self._peak_seen)
        self.report.incomplete_dropped = self._assembler.incomplete_dropped
        return self.report


def sink_from_env(component_id: str = COMPONENT_ID) -> AlertSink:
    """Neo4j sink if ``NEO4J_URI`` is set, otherwise an in-memory one."""
    if os.getenv("NEO4J_URI"):
        return Neo4jAlertSink.from_env(component_id=component_id)
    log.info("NEO4J_URI not set: alerts are kept in memory and logged only")
    return InMemoryAlertSink()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BioPrint-Guard OPC UA gateway")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="OPC UA server endpoint")
    parser.add_argument("--samples", type=int, default=None, help="stop after N readings")
    parser.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    parser.add_argument(
        "--baseline-samples",
        type=int,
        default=100,
        help="readings used to fix the healthy baseline (default 100 = 10 s at 10 Hz)",
    )
    parser.add_argument(
        "--require-alert",
        action="store_true",
        help="exit 1 unless exactly one OPEN alert exists for the component at the end",
    )
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    sink = sink_from_env()
    gateway = OpcUaGateway(
        args.endpoint, sink, StreamingAnomalyDetector(baseline_samples=args.baseline_samples)
    )
    try:
        report = await gateway.run(max_samples=args.samples, duration_s=args.duration)
        state = await sink.alert_state(gateway.component_id)
    finally:
        await sink.close()
    peak = max((r.scored.score for r in report.records), default=0.0)
    log.info(
        "Scored %d readings over %d connection(s); peak risk %.3f; %d alert write(s); "
        "graph state %s",
        report.samples,
        report.connections,
        peak,
        report.alert_writes,
        state,
    )
    if args.require_alert and (state["open_alerts"] != 1 or state["components"] != 1):
        log.error("FAIL: expected exactly one OPEN alert on one component, got %s", state)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
