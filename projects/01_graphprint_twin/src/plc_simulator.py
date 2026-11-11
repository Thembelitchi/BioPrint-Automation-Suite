"""Simulated PLC extruder registers for GraphPrint-Twin (FR-1).

The simulator stands in for a PLC exposing two holding registers on a polymer
extruder: nozzle temperature (°C) and pump extrusion pressure (bar). It is
sampled at a fixed rate (default 10 Hz). Each call to
:meth:`PLCExtruderSimulator.read_registers` returns the next sample, stamped
with a *simulated* clock that advances exactly one sample period per call.
Runs are therefore reproducible and can be compressed in time for CI.

Each variable follows a *bounded random walk*: Gaussian noise plus a pull back
towards its setpoint (a discrete Ornstein-Uhlenbeck process). At 10 Hz the
per-sample noise is small and the reversion slow, so the signal wanders like a
regulated loop does. Occasional "process upsets" add a one-sample spike, the
kind of event a QA engineer would want to trace back to a material batch.

Every sample is classified like a laboratory QC result: inside the safety
envelope it is ``OPTIMAL``, outside it is ``ANOMALY_WARNING``.

Everything here is simulated; no hardware is touched.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

Status = Literal["OPTIMAL", "ANOMALY_WARNING"]

# Process targets and safety envelope (from the GraphPrint-Twin PRD, FR-1).
TARGET_TEMP_C: float = 230.0
TARGET_PRESSURE_BAR: float = 45.0
TEMP_LIMIT_C: float = 235.0
PRESSURE_LIMIT_BAR: float = 48.0
SAMPLE_RATE_HZ: float = 10.0


@dataclass(frozen=True)
class TelemetryReading:
    """One snapshot of the extruder's PLC registers."""

    nozzle_temp: float
    pump_pressure: float
    status: Status
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_params(self) -> dict[str, Any]:
        """Return the reading as query parameters (never interpolated into query text)."""
        return {
            "log_id": self.id,
            "timestamp": self.timestamp,
            "nozzle_temp": self.nozzle_temp,
            "pump_pressure": self.pump_pressure,
            "status": self.status,
        }


class TelemetrySource(Protocol):
    """Anything that can produce telemetry readings.

    The ingestion loop depends only on this interface, so the simulator can be
    replaced by a Modbus TCP reader, an OPC UA client or a Kafka consumer
    without touching the graph layer (PRD NFR 5.3).
    """

    sample_period_s: float

    def read_registers(self) -> TelemetryReading:  # pragma: no cover - interface
        ...


def classify(nozzle_temp: float, pump_pressure: float) -> Status:
    """Tag a reading against the safety envelope, like a QC pass/flag decision."""
    if nozzle_temp > TEMP_LIMIT_C or pump_pressure > PRESSURE_LIMIT_BAR:
        return "ANOMALY_WARNING"
    return "OPTIMAL"


class PLCExtruderSimulator:
    """Bounded random-walk simulator for extruder temperature and pressure registers.

    Defaults are tuned for 10 Hz sampling: the walk's stationary SD is about
    1.1 °C and 0.6 bar, so the 235 °C / 48 bar envelope is crossed almost only by
    process upsets.

    Args:
        seed: Random seed. A fixed seed gives a reproducible run, which CI relies on.
        sample_rate_hz: Sampling rate; sets the simulated clock step.
        start_time: Simulated time of the first sample (default: now, UTC).
        temp_noise: Standard deviation of the per-sample temperature noise (°C).
        pressure_noise: Standard deviation of the per-sample pressure noise (bar).
        reversion: Fraction of the gap to setpoint closed each sample (0–1). Models
            the regulating PID loop pulling the process back to target.
        upset_probability: Chance per sample that a process upset occurs.
        upset_magnitude: Size of an upset as (temperature °C, pressure bar).
    """

    def __init__(
        self,
        seed: int | None = None,
        sample_rate_hz: float = SAMPLE_RATE_HZ,
        start_time: datetime | None = None,
        temp_noise: float = 0.35,
        pressure_noise: float = 0.18,
        reversion: float = 0.05,
        upset_probability: float = 0.001,
        upset_magnitude: tuple[float, float] = (8.0, 4.5),
    ) -> None:
        if not 0.0 <= reversion <= 1.0:
            raise ValueError("reversion must be between 0 and 1")
        if not 0.0 <= upset_probability <= 1.0:
            raise ValueError("upset_probability must be between 0 and 1")
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")

        self._rng = random.Random(seed)
        self.sample_period_s = 1.0 / sample_rate_hz
        self._clock = start_time or datetime.now(timezone.utc).replace(microsecond=0)
        self.temp_noise = temp_noise
        self.pressure_noise = pressure_noise
        self.reversion = reversion
        self.upset_probability = upset_probability
        self.upset_magnitude = upset_magnitude

        self._temp = TARGET_TEMP_C
        self._pressure = TARGET_PRESSURE_BAR

    def _step(self, value: float, target: float, noise: float) -> float:
        """Advance one variable by one step of the mean-reverting random walk."""
        drift = self.reversion * (target - value)
        return value + drift + self._rng.gauss(0.0, noise)

    def read_registers(self) -> TelemetryReading:
        """Advance the process one sample and return the current register values."""
        self._temp = self._step(self._temp, TARGET_TEMP_C, self.temp_noise)
        self._pressure = self._step(self._pressure, TARGET_PRESSURE_BAR, self.pressure_noise)

        temp, pressure = self._temp, self._pressure

        # A process upset (e.g. a wet pellet slug or a partial nozzle block) is a
        # transient spike on this sample only. It is not fed back into the walk,
        # so the underlying process stays centred on its setpoints.
        if self._rng.random() < self.upset_probability:
            d_temp, d_pressure = self.upset_magnitude
            temp += d_temp * self._rng.uniform(0.6, 1.0)
            pressure += d_pressure * self._rng.uniform(0.6, 1.0)

        temp = round(temp, 2)
        pressure = round(pressure, 2)
        reading = TelemetryReading(
            nozzle_temp=temp,
            pump_pressure=pressure,
            status=classify(temp, pressure),
            timestamp=self._clock,
        )
        self._clock += timedelta(seconds=self.sample_period_s)
        return reading
