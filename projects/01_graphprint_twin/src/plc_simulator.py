"""Simulated PLC extruder registers for GraphPrint-Twin (FR-1).

The simulator stands in for a PLC exposing two holding registers on a polymer
extruder: nozzle temperature (°C) and pump extrusion pressure (bar). Each call
to :meth:`PLCExtruderSimulator.read_registers` returns one reading.

Each variable follows a *bounded random walk*: Gaussian noise plus a pull back
towards its setpoint (a discrete Ornstein-Uhlenbeck process). That keeps the
signal realistic for a regulated loop, which wanders but doesn't diverge.
Occasional "process upsets" add a one-reading spike, the kind of event a QA
engineer would want to trace back to a material batch.

Every reading is classified like a laboratory QC result: inside the safety
envelope it is ``OPTIMAL``, outside it is ``ANOMALY_WARNING``.

Everything here is simulated; no hardware is touched.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

Status = Literal["OPTIMAL", "ANOMALY_WARNING"]

# Process targets and safety envelope (from the GraphPrint-Twin PRD, FR-1).
TARGET_TEMP_C: float = 230.0
TARGET_PRESSURE_BAR: float = 45.0
TEMP_LIMIT_C: float = 235.0
PRESSURE_LIMIT_BAR: float = 48.0


@dataclass(frozen=True)
class TelemetryReading:
    """One snapshot of the extruder's PLC registers."""

    nozzle_temp: float
    pump_pressure: float
    status: Status
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_params(self) -> dict[str, Any]:
        """Return the reading as Cypher query parameters (never interpolated into query text)."""
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

    def read_registers(self) -> TelemetryReading:  # pragma: no cover - interface
        ...


def classify(nozzle_temp: float, pump_pressure: float) -> Status:
    """Tag a reading against the safety envelope, like a QC pass/flag decision."""
    if nozzle_temp > TEMP_LIMIT_C or pump_pressure > PRESSURE_LIMIT_BAR:
        return "ANOMALY_WARNING"
    return "OPTIMAL"


class PLCExtruderSimulator:
    """Bounded random-walk simulator for extruder temperature and pressure registers.

    Args:
        seed: Random seed. A fixed seed gives a reproducible run, which CI relies on.
        temp_noise: Standard deviation of the per-step temperature noise (°C).
        pressure_noise: Standard deviation of the per-step pressure noise (bar).
        reversion: Fraction of the gap to setpoint closed each step (0–1). Models
            the regulating PID loop pulling the process back to target.
        upset_probability: Chance per step that a process upset starts.
        upset_magnitude: Size of an upset as (temperature °C, pressure bar).
    """

    def __init__(
        self,
        seed: int | None = None,
        temp_noise: float = 1.2,
        pressure_noise: float = 0.6,
        reversion: float = 0.25,
        upset_probability: float = 0.15,
        upset_magnitude: tuple[float, float] = (7.0, 4.0),
    ) -> None:
        if not 0.0 <= reversion <= 1.0:
            raise ValueError("reversion must be between 0 and 1")
        if not 0.0 <= upset_probability <= 1.0:
            raise ValueError("upset_probability must be between 0 and 1")

        self._rng = random.Random(seed)
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
        """Advance the process one cycle and return the current register values."""
        self._temp = self._step(self._temp, TARGET_TEMP_C, self.temp_noise)
        self._pressure = self._step(self._pressure, TARGET_PRESSURE_BAR, self.pressure_noise)

        temp, pressure = self._temp, self._pressure

        # A process upset (e.g. a wet pellet slug or a partial nozzle block) is a
        # transient spike on this reading only. It is not fed back into the walk,
        # so the underlying process stays centred on its setpoints.
        if self._rng.random() < self.upset_probability:
            d_temp, d_pressure = self.upset_magnitude
            temp += d_temp * self._rng.uniform(0.6, 1.0)
            pressure += d_pressure * self._rng.uniform(0.6, 1.0)

        temp = round(temp, 2)
        pressure = round(pressure, 2)
        return TelemetryReading(
            nozzle_temp=temp,
            pump_pressure=pressure,
            status=classify(temp, pressure),
        )
