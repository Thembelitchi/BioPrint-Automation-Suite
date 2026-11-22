"""GraphPrint-Twin entry point: sample PLC telemetry at 10 Hz and stream it into Neo4j per layer.

Run from the project folder:

    python src/main.py                              # 10 layers × 600 samples, 1 s between layers
    python src/main.py --layers 160 --interval 0    # a whole 160-layer wall, as fast as possible
    python src/main.py --raw-policy all             # keep raw arrays on every layer

Steps:
    1. Wait for Neo4j, then create constraints and the baseline topology.
    2. For each layer: read ``--samples-per-layer`` samples from the simulated PLC
       at 10 Hz (simulated clock), aggregate them, and write ONE LayerTelemetry
       bucket in one idempotent transaction.
    3. Verify every bucket landed, is linked to its printer and wall, and is chained
       to the previous layer.
    4. Run the forensic traceback and save it to ``output/anomaly_traceback.csv``.

The default 600 samples per layer is 60 s of 10 Hz data: compressed for a quick
demo. A real LFAM layer is ~10–16 min (~6,000–9,600 samples), which makes the
saving from bucketing larger still.

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
from layer_aggregator import summarise_layer
from plc_simulator import PLCExtruderSimulator, TelemetrySource

LATENCY_BUDGET_MS = 250.0  # PRD NFR 5.2: edge-to-graph write latency per batch
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

log = logging.getLogger("graphprint_twin")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GraphPrint-Twin layer-bucketed telemetry")
    parser.add_argument("--layers", type=int, default=10, help="layers to print (default 10)")
    parser.add_argument(
        "--samples-per-layer",
        type=int,
        default=600,
        help="10 Hz samples per layer (default 600 = 60 s, compressed demo)",
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, help="seconds between layer writes (default 1.0)"
    )
    parser.add_argument(
        "--raw-policy",
        choices=("anomaly", "all", "none"),
        default="anomaly",
        help="when to keep raw sample arrays on a layer node (default: anomalous layers only)",
    )
    parser.add_argument("--seed", type=int, default=42, help="simulator random seed")
    parser.add_argument(
        "--prune-days",
        type=int,
        default=None,
        help="also drop raw arrays older than N days (summaries are kept)",
    )
    return parser.parse_args(argv)


def run(
    bridge: DigitalTwinBridge,
    source: TelemetrySource,
    layers: int,
    samples_per_layer: int,
    interval_s: float,
    raw_policy: str = "anomaly",
) -> list[str]:
    """Print ``layers`` layers and return the ids of the buckets written."""
    layer_ids: list[str] = []
    for index in range(layers):
        started = time.perf_counter()
        readings = [source.read_registers() for _ in range(samples_per_layer)]
        layer = summarise_layer(
            readings,
            PRINTER_ID,
            WALL_ID,
            index,
            source.sample_period_s,
            raw_policy,  # type: ignore[arg-type]
        )
        write_started = time.perf_counter()
        layer_ids.append(bridge.ingest_layer(layer))
        latency_ms = (time.perf_counter() - write_started) * 1000

        marker = "⚠" if layer.status == "ANOMALY_WARNING" else "✓"
        log.info(
            "%s layer %3d  n=%d  temp %.2f±%.2f °C (max %.2f)  pressure max %.2f bar  "
            "anomalies=%d  raw=%s  %5.1f ms",
            marker,
            index,
            layer.n_samples,
            layer.temp_mean,
            layer.temp_sd,
            layer.temp_max,
            layer.pressure_max,
            layer.anomaly_count,
            "kept" if layer.raw_stored else "-",
            latency_ms,
        )
        if latency_ms > LATENCY_BUDGET_MS:
            log.warning(
                "Write latency %.1f ms exceeded %.0f ms budget", latency_ms, LATENCY_BUDGET_MS
            )

        if index < layers - 1 and interval_s > 0:
            # Sleep only for the rest of the interval, so cycle timing stays steady.
            time.sleep(max(0.0, interval_s - (time.perf_counter() - started)))
    return layer_ids


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    if args.layers < 1 or args.samples_per_layer < 1:
        log.error("--layers and --samples-per-layer must be at least 1")
        return 2

    with DigitalTwinBridge.from_env() as bridge:
        bridge.wait_until_ready()
        bridge.initialize_factory_topology()

        source = PLCExtruderSimulator(seed=args.seed)
        layer_ids = run(
            bridge, source, args.layers, args.samples_per_layer, args.interval, args.raw_policy
        )

        # FR-2 / FR-3: every bucket exists, is attached, and the layers are chained.
        integrity = bridge.verify_run(layer_ids)
        log.info("Integrity: %s", integrity)
        expected_samples = args.layers * args.samples_per_layer
        ok = (
            integrity["found"] == integrity["expected"] == integrity["linked"]
            and integrity["chained"] >= args.layers - 1  # a longer earlier run may extend the chain
            and integrity["samples"] == expected_samples
        )
        log.info(
            "Bucketing: %d samples -> %d layer nodes (%d:1); per-sample storage would need "
            "%d nodes and %d relationships",
            expected_samples,
            len(layer_ids),
            args.samples_per_layer,
            expected_samples,
            2 * expected_samples,
        )

        # FR-4: forensic traceback, exported for the run record.
        traceback = pd.DataFrame(bridge.trace_anomalies())
        OUTPUT_DIR.mkdir(exist_ok=True)
        csv_path = OUTPUT_DIR / "anomaly_traceback.csv"
        traceback.to_csv(csv_path, index=False)
        if traceback.empty:
            log.info("Traceback: no anomalous layers in the graph")
        else:
            log.info(
                "Traceback: %d anomalous layers -> batches %s (saved to %s)",
                len(traceback),
                sorted(traceback["batch"].unique()),
                csv_path,
            )

        if args.prune_days:
            bridge.prune_raw_arrays(args.prune_days)

    if ok:
        log.info("PASS: %d layers ingested, linked and chained", len(layer_ids))
        return 0
    log.error("FAIL: graph integrity check failed: %s", integrity)
    return 1


if __name__ == "__main__":
    sys.exit(main())
