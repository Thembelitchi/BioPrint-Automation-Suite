"""GraphPrint-Twin entry point: simulate PLC telemetry and stream it into Neo4j.

Run from the project folder:

    python src/main.py                     # 10 cycles, 1 s apart (PRD default)
    python src/main.py --cycles 120        # 2-minute soak test (PRD FR-3)
    python src/main.py --interval 0        # as fast as possible

Steps:
    1. Wait for Neo4j, then create constraints and the baseline topology.
    2. Each cycle: read the simulated PLC registers, classify, write to the graph.
    3. Verify that every reading landed and is linked to its printer and wall.
    4. Run the forensic traceback and save it to ``output/anomaly_traceback.csv``.

The exit code is 0 only if every check passes, so CI can rely on it.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd
from database import PRINTER_ID, WALL_ID, DigitalTwinBridge
from plc_simulator import PLCExtruderSimulator, TelemetrySource

LATENCY_BUDGET_MS = 250.0  # PRD NFR 5.2: edge-to-graph write latency per reading
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

log = logging.getLogger("graphprint_twin")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GraphPrint-Twin telemetry streamer")
    parser.add_argument("--cycles", type=int, default=10, help="number of readings (default 10)")
    parser.add_argument(
        "--interval", type=float, default=1.0, help="seconds between readings (default 1.0)"
    )
    parser.add_argument("--seed", type=int, default=42, help="simulator random seed")
    parser.add_argument(
        "--prune-days",
        type=int,
        default=None,
        help="also prune OPTIMAL readings older than N days",
    )
    return parser.parse_args(argv)


def run(
    bridge: DigitalTwinBridge,
    source: TelemetrySource,
    cycles: int,
    interval_s: float,
) -> list[str]:
    """Stream ``cycles`` readings into the graph and return the ids written."""
    log_ids: list[str] = []
    for cycle in range(1, cycles + 1):
        started = time.perf_counter()

        reading = source.read_registers()
        log_id = bridge.ingest_telemetry(PRINTER_ID, WALL_ID, reading)
        log_ids.append(log_id)

        latency_ms = (time.perf_counter() - started) * 1000
        marker = "⚠" if reading.status == "ANOMALY_WARNING" else "✓"
        log.info(
            "%s cycle %2d/%d  temp=%6.2f °C  pressure=%5.2f bar  %-15s  %5.1f ms",
            marker,
            cycle,
            cycles,
            reading.nozzle_temp,
            reading.pump_pressure,
            reading.status,
            latency_ms,
        )
        if latency_ms > LATENCY_BUDGET_MS:
            log.warning(
                "Write latency %.1f ms exceeded %.0f ms budget", latency_ms, LATENCY_BUDGET_MS
            )

        if cycle < cycles and interval_s > 0:
            # Sleep only for the rest of the interval, so cycle timing stays steady.
            time.sleep(max(0.0, interval_s - (time.perf_counter() - started)))
    return log_ids


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    if args.cycles < 1:
        log.error("--cycles must be at least 1")
        return 2

    with DigitalTwinBridge.from_env() as bridge:
        bridge.wait_until_ready()
        bridge.initialize_factory_topology()

        log_ids = run(bridge, PLCExtruderSimulator(seed=args.seed), args.cycles, args.interval)

        # FR-2 / FR-3: every reading exists and is attached to printer and wall.
        integrity = bridge.verify_run(log_ids)
        log.info("Integrity: %s", integrity)
        ok = integrity["found"] == integrity["expected"] == integrity["linked"]

        # FR-4: forensic traceback, exported for the run record.
        traceback = pd.DataFrame(bridge.trace_anomalies())
        OUTPUT_DIR.mkdir(exist_ok=True)
        csv_path = OUTPUT_DIR / "anomaly_traceback.csv"
        traceback.to_csv(csv_path, index=False)
        if traceback.empty:
            log.info("Traceback: no anomalies recorded in the graph")
        else:
            log.info(
                "Traceback: %d anomalous readings -> batches %s (saved to %s)",
                len(traceback),
                sorted(traceback["batch"].unique()),
                csv_path,
            )

        if args.prune_days:
            bridge.prune_optimal_logs(args.prune_days)

    if ok:
        log.info("PASS: %d readings ingested and fully linked", len(log_ids))
        return 0
    log.error("FAIL: graph integrity check failed: %s", integrity)
    return 1


if __name__ == "__main__":
    sys.exit(main())
