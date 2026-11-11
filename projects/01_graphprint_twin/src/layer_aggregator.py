"""Layer-bucketed telemetry: turning 10 Hz samples into one graph node per printed layer.

Writing every sample as its own node doesn't scale. One printer at 10 Hz
produces 864,000 samples a day, and each would cost a node plus two
relationships. A graph is good at *context* (what was printed, by which
machine, from which batch), not at storing long numeric series.

So the edge gateway buffers the samples for one printed layer and writes a
single ``LayerTelemetry`` node:

* **always:** summary statistics (n, mean, min, max, SD, CV%) per variable, the
  count and offsets of out-of-envelope samples, the layer status, and the
  layer's start time and sample period. Sample timestamps are implicit
  (``start_time + offset × sample_period_s``), so no timestamp array is stored.
* **raw arrays only when they matter** (``raw_policy``):
  ``"anomaly"`` (default) keeps raw values only for layers with an anomaly, for
  forensic review; ``"all"`` keeps them for every layer; ``"none"`` never does.

At a typical LFAM layer time of ~12 minutes, that is ~120 nodes per printer per
day instead of ~864,000. For production-scale history, raw samples belong in a
time-series store (TimescaleDB, InfluxDB, a plant historian), with the graph
holding these summaries and events plus a pointer to the raw range. The docs
describe that path; this module implements the graph side.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np
from plc_simulator import TelemetryReading

RawPolicy = Literal["anomaly", "all", "none"]


def layer_id(printer_id: str, wall_id: str, layer_index: int) -> str:
    """Deterministic id: a wall's layer is printed once, so re-ingesting it updates it."""
    return f"{printer_id}/{wall_id}/L{layer_index:04d}"


@dataclass(frozen=True)
class LayerTelemetry:
    """Aggregated telemetry for one printed layer of one wall."""

    id: str
    printer_id: str
    wall_id: str
    layer_index: int
    start_time: datetime
    end_time: datetime
    sample_period_s: float
    n_samples: int
    temp_mean: float
    temp_min: float
    temp_max: float
    temp_sd: float
    temp_cv_pct: float
    pressure_mean: float
    pressure_min: float
    pressure_max: float
    pressure_sd: float
    anomaly_count: int
    anomaly_offsets: tuple[int, ...]
    status: str
    raw_temps: tuple[float, ...] | None
    raw_pressures: tuple[float, ...] | None

    @property
    def raw_stored(self) -> bool:
        return self.raw_temps is not None

    @property
    def previous_id(self) -> str | None:
        if self.layer_index == 0:
            return None
        return layer_id(self.printer_id, self.wall_id, self.layer_index - 1)

    def sample_time(self, offset: int) -> datetime:
        """Timestamp of the sample at ``offset`` (implicit timestamps)."""
        return self.start_time + timedelta(seconds=offset * self.sample_period_s)

    def to_properties(self) -> dict[str, Any]:
        """Node properties. Raw keys are present as None when not kept, so an update
        that drops raw data also removes it from the node (``SET b += $props``)."""
        return {
            "layer_index": self.layer_index,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "sample_period_s": self.sample_period_s,
            "n_samples": self.n_samples,
            "temp_mean": self.temp_mean,
            "temp_min": self.temp_min,
            "temp_max": self.temp_max,
            "temp_sd": self.temp_sd,
            "temp_cv_pct": self.temp_cv_pct,
            "pressure_mean": self.pressure_mean,
            "pressure_min": self.pressure_min,
            "pressure_max": self.pressure_max,
            "pressure_sd": self.pressure_sd,
            "anomaly_count": self.anomaly_count,
            "anomaly_offsets": list(self.anomaly_offsets),
            "status": self.status,
            "raw_stored": self.raw_stored,
            "raw_temps": list(self.raw_temps) if self.raw_temps is not None else None,
            "raw_pressures": list(self.raw_pressures) if self.raw_pressures is not None else None,
        }


def summarise_layer(
    readings: Sequence[TelemetryReading],
    printer_id: str,
    wall_id: str,
    layer_index: int,
    sample_period_s: float,
    raw_policy: RawPolicy = "anomaly",
) -> LayerTelemetry:
    """Aggregate one layer's samples into a :class:`LayerTelemetry` bucket."""
    if not readings:
        raise ValueError("a layer needs at least one sample")
    if raw_policy not in ("anomaly", "all", "none"):
        raise ValueError(f"unknown raw_policy {raw_policy!r}")

    temps = np.array([r.nozzle_temp for r in readings], dtype=float)
    pressures = np.array([r.pump_pressure for r in readings], dtype=float)
    offsets = tuple(i for i, r in enumerate(readings) if r.status == "ANOMALY_WARNING")
    status = "ANOMALY_WARNING" if offsets else "OPTIMAL"
    keep_raw = raw_policy == "all" or (raw_policy == "anomaly" and offsets)

    def sd(x: np.ndarray) -> float:
        return float(x.std(ddof=1)) if x.size > 1 else 0.0

    temp_mean = float(temps.mean())
    return LayerTelemetry(
        id=layer_id(printer_id, wall_id, layer_index),
        printer_id=printer_id,
        wall_id=wall_id,
        layer_index=layer_index,
        start_time=readings[0].timestamp,
        end_time=readings[-1].timestamp,
        sample_period_s=sample_period_s,
        n_samples=len(readings),
        temp_mean=round(temp_mean, 3),
        temp_min=float(temps.min()),
        temp_max=float(temps.max()),
        temp_sd=round(sd(temps), 4),
        temp_cv_pct=round(100.0 * sd(temps) / temp_mean, 4) if temp_mean else 0.0,
        pressure_mean=round(float(pressures.mean()), 3),
        pressure_min=float(pressures.min()),
        pressure_max=float(pressures.max()),
        pressure_sd=round(sd(pressures), 4),
        anomaly_count=len(offsets),
        anomaly_offsets=offsets,
        status=status,
        raw_temps=tuple(temps.tolist()) if keep_raw else None,
        raw_pressures=tuple(pressures.tolist()) if keep_raw else None,
    )
