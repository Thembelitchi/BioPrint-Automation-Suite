"""BioPrint-Guard scenario: OPC UA server -> gateway -> detector -> alert, with acceptance checks.

Run from the project folder::

    python src/app.py                 # 30 s at 10 Hz in real time, wear fault after 15 s
    python src/app.py --speed 10      # same samples, 10x faster (about 3 s)
    NEO4J_URI=bolt://localhost:7687 python src/app.py   # write the alert into the shared twin

This starts the simulated OPC UA server and the gateway in one process and
connects them over a real OPC UA session on localhost. To run them as two
processes instead, see ``opcua_server.py`` and ``gateway.py``.

Outputs (in ``output/``):

* ``guard_scores.csv``: every reading with its Z-scores and risk score
* ``guard_risk_score.png``: the three signals and the risk score
* ``acceptance.json``: the FR-1 to FR-4 checks

The exit code is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from alert_store import COMPONENT_ID, InMemoryAlertSink
from anomaly_detector import ALERT_THRESHOLD, StreamingAnomalyDetector
from gateway import GatewayReport, OpcUaGateway, sink_from_env
from motor_model import SIGNALS, StepperMotorSimulator
from opcua_server import NODE_PATH, MotorOpcUaServer

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
HEALTHY_MAX_SCORE = 0.5  # FR-3: the score must stay below this before wear onset

log = logging.getLogger("bioprint_guard")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BioPrint-Guard end-to-end scenario")
    parser.add_argument(
        "--duration", type=float, default=30.0, help="simulated seconds (default 30)"
    )
    parser.add_argument(
        "--fault-after", type=float, default=15.0, help="wear onset, simulated seconds (default 15)"
    )
    parser.add_argument(
        "--baseline-samples",
        type=int,
        default=100,
        help="readings used to fix the healthy baseline (default 100 = 10 s)",
    )
    parser.add_argument("--speed", type=float, default=1.0, help="wall-clock speed-up (default 1)")
    parser.add_argument("--seed", type=int, default=42, help="simulator random seed")
    parser.add_argument(
        "--port", type=int, default=4840, help="OPC UA port (default 4840; 0 picks a free one)"
    )
    parser.add_argument(
        "--close-alerts",
        action="store_true",
        help="first mark the component's open alerts CLOSED (inspection done), then run",
    )
    return parser.parse_args(argv)


async def run_scenario(
    duration_s: float = 30.0,
    fault_after_s: float | None = 15.0,
    baseline_samples: int = 100,
    speed: float = 1.0,
    seed: int = 42,
    port: int = 0,
    sink: Any = None,
    timeout_s: float | None = None,
) -> tuple[GatewayReport, MotorOpcUaServer, list[float]]:
    """Run server and gateway together; return the report, the server and the true wear levels."""
    port = port or free_port()
    simulator = StepperMotorSimulator(
        seed=seed, fault_after_s=fault_after_s, epoch=datetime.now(timezone.utc)
    )
    n_samples = round(duration_s / simulator.sample_period_s)
    wear = [simulator.wear_at(i * simulator.sample_period_s) for i in range(n_samples)]
    server = MotorOpcUaServer(simulator, f"opc.tcp://0.0.0.0:{port}/bioprint/", speed)
    gateway = OpcUaGateway(
        f"opc.tcp://localhost:{port}/bioprint/",
        sink or InMemoryAlertSink(),
        StreamingAnomalyDetector(baseline_samples=baseline_samples),
    )
    if timeout_s is None:
        timeout_s = duration_s / speed + 30.0

    async with server:
        gateway_task = asyncio.create_task(gateway.run(max_samples=n_samples, duration_s=timeout_s))
        # Publish only once the subscription is up, so the scenario starts at sample 0.
        while gateway.report.subscriptions_open == 0 and not gateway_task.done():
            await asyncio.sleep(0.05)
        await server.publish(n_samples)
        report = await gateway_task
    return report, server, wear


def to_frame(report: GatewayReport, wear: list[float], sample_period_s: float) -> pd.DataFrame:
    rows = []
    for record in report.records:
        row: dict[str, Any] = {
            "index": record.index,
            "t_s": round(record.index * sample_period_s, 3),
            "timestamp": record.timestamp.isoformat(),
        }
        row.update(dict(zip(SIGNALS, record.values, strict=True)))
        row.update({f"z_{s}": z for s, z in zip(SIGNALS, record.scored.z_scores, strict=True)})
        row["distance"] = record.scored.distance
        row["smoothed_distance"] = record.scored.smoothed_distance
        row["risk_score"] = record.scored.score
        row["baseline_ready"] = record.scored.ready
        row["true_wear"] = wear[record.index] if record.index < len(wear) else None
        rows.append(row)
    return pd.DataFrame(rows)


def evaluate(
    frame: pd.DataFrame,
    report: GatewayReport,
    published: int,
    fault_after_s: float | None,
    graph_state: dict[str, int],
    sink_kind: str,
) -> dict[str, dict[str, Any]]:
    """FR-1 to FR-4 acceptance checks."""
    checks: dict[str, dict[str, Any]] = {}
    checks["FR-1"] = {
        "passed": report.connections >= 1 and report.monitored_items == len(SIGNALS),
        "node_path": "/".join(NODE_PATH),
        "variables": list(SIGNALS),
        "samples_published": published,
    }
    checks["FR-2"] = {
        "passed": report.subscriptions_open == 1
        and report.samples == published
        and report.incomplete_dropped == 0,
        "subscriptions": report.subscriptions_open,
        "monitored_items": report.monitored_items,
        "samples_received": report.samples,
        "samples_lost": published - report.samples,
        "connections": report.connections,
        "max_queue_depth": report.max_queue_depth,
    }

    if frame.empty or fault_after_s is None:
        healthy, faulty = frame, frame.iloc[0:0]
    else:
        healthy = frame[frame["t_s"] < fault_after_s]
        faulty = frame[frame["t_s"] >= fault_after_s]
    healthy_max = float(healthy["risk_score"].max()) if len(healthy) else 0.0
    crossed = faulty[faulty["risk_score"] > ALERT_THRESHOLD]
    first_alert_s = float(crossed["t_s"].iloc[0]) if len(crossed) else None
    checks["FR-3"] = {
        "passed": healthy_max < HEALTHY_MAX_SCORE and first_alert_s is not None,
        "healthy_max_score": round(healthy_max, 4),
        "peak_score": round(float(frame["risk_score"].max()), 4) if len(frame) else 0.0,
        "first_alert_s": first_alert_s,
        "detection_delay_s": None
        if first_alert_s is None or fault_after_s is None
        else round(first_alert_s - fault_after_s, 2),
        "true_wear_at_alert": None if not len(crossed) else float(crossed["true_wear"].iloc[0]),
    }
    checks["FR-4"] = {
        "passed": graph_state.get("open_alerts") == 1 and graph_state.get("components") == 1,
        "store": sink_kind,
        "open_alerts": graph_state.get("open_alerts"),
        "components_with_this_id": graph_state.get("components"),
        "alert_writes": report.alert_writes,
        "alert": None
        if report.alert is None
        else {
            "id": report.alert.id,
            "severity": report.alert.severity,
            "peak_risk_score": round(report.alert.peak_risk_score, 4),
        },
    }
    return checks


def plot(frame: pd.DataFrame, fault_after_s: float | None, baseline_samples: int, path: Path):
    """Three signals and the risk score on a shared time axis."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    panels = [
        ("motor_current", "Motor current (A)", "#1f77b4"),
        ("housing_temp", "Housing temp (°C)", "#d62728"),
        ("vibration_frequency", "Vibration (Hz)", "#2ca02c"),
    ]
    fig, axes = plt.subplots(4, 1, figsize=(11, 9.5), sharex=True)
    t = frame["t_s"]
    baseline_end = float(t.iloc[min(baseline_samples, len(t)) - 1]) if len(t) else 0.0
    for ax, (column, label, colour) in zip(axes[:3], panels, strict=True):
        ax.plot(t, frame[column], color=colour, lw=0.9)
        ax.set_ylabel(label)
    risk = axes[3]
    risk.plot(t, frame["risk_score"], color="black", lw=1.4, label="Risk score")
    risk.axhline(ALERT_THRESHOLD, color="#d62728", ls="--", lw=1, label="Alert threshold 0.85")
    risk.axhline(HEALTHY_MAX_SCORE, color="grey", ls=":", lw=1, label="Healthy ceiling 0.5")
    risk.set_ylim(-0.03, 1.05)
    risk.set_ylabel("Risk score (0–1)")
    risk.set_xlabel("Time (s)")
    crossed = frame[frame["risk_score"] > ALERT_THRESHOLD]
    for ax in axes:
        ax.axvspan(0, baseline_end, color="#1f77b4", alpha=0.07)
        if fault_after_s is not None:
            ax.axvline(fault_after_s, color="#ff7f0e", ls="--", lw=1)
        if len(crossed):
            ax.axvline(float(crossed["t_s"].iloc[0]), color="#d62728", lw=1)
        ax.grid(alpha=0.25)
    axes[0].set_title(
        "BioPrint-Guard: stepper-motor wear detection over OPC UA (simulated)\n"
        "blue band = baseline window, orange dashed = wear onset, red = alert raised",
        fontsize=11,
    )
    risk.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


async def _main(args: argparse.Namespace) -> int:
    sink = sink_from_env()
    sink_kind = type(sink).__name__
    try:
        if args.close_alerts:
            await sink.prepare()
            closed = await sink.close_open_alerts(COMPONENT_ID)  # type: ignore[attr-defined]
            log.info("Closed %d open alert(s) on %s", closed, COMPONENT_ID)
        report, server, wear = await run_scenario(
            args.duration,
            args.fault_after,
            args.baseline_samples,
            args.speed,
            args.seed,
            args.port,
            sink,
        )
        graph_state = await sink.alert_state(COMPONENT_ID)
    finally:
        await sink.close()

    frame = to_frame(report, wear, server.simulator.sample_period_s)
    checks = evaluate(
        frame, report, server.samples_published, args.fault_after, graph_state, sink_kind
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    frame.to_csv(OUTPUT_DIR / "guard_scores.csv", index=False)
    plot(frame, args.fault_after, args.baseline_samples, OUTPUT_DIR / "guard_risk_score.png")
    (OUTPUT_DIR / "acceptance.json").write_text(json.dumps(checks, indent=2) + "\n")

    for name, check in checks.items():
        detail = {k: v for k, v in check.items() if k != "passed"}
        log.info("%s %s  %s", "PASS" if check["passed"] else "FAIL", name, json.dumps(detail))
    if sink_kind == "InMemoryAlertSink":
        log.info("FR-4 was checked against the in-memory store; set NEO4J_URI to check the graph")
    return 0 if all(check["passed"] for check in checks.values()) else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)
    if args.duration <= 0 or args.speed <= 0:
        log.error("--duration and --speed must be positive")
        return 2
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
