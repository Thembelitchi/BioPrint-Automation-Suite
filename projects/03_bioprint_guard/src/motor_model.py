"""Simulated gantry stepper motor with a progressive friction-wear fault (FR-1 data source).

Three signals are produced at a fixed sample period (10 Hz by default):

* ``motor_current`` (A): phase current. Friction makes the drive work harder.
* ``housing_temp`` (°C): housing temperature. It follows the extra losses with a
  first-order lag, so it is the slowest signal to respond.
* ``vibration_frequency`` (Hz): dominant vibration frequency of the axis. A worn
  bearing shifts it upward and makes it noisier.

Healthy behaviour is a stationary signal with Gaussian noise. After
``fault_after_s`` a wear level ``w`` ramps from 0 to 1 over ``wear_ramp_s`` and
pushes all three signals away from their healthy values. Every number here is a
placeholder chosen to make the scenario readable; none comes from a real motor.

The model runs on a simulated clock and a seeded generator, so the same seed
always gives the same samples.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

SIGNALS: tuple[str, ...] = ("motor_current", "housing_temp", "vibration_frequency")

#: Fixed epoch for the simulated clock, so runs are reproducible.
SIM_EPOCH = datetime(2026, 1, 5, 6, 0, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class MotorSample:
    """One synchronous reading of the three motor signals."""

    index: int
    t: float  # simulated seconds since start
    timestamp: datetime
    motor_current: float
    housing_temp: float
    vibration_frequency: float
    wear: float  # ground truth, 0..1 (not published over OPC UA)

    def values(self) -> tuple[float, float, float]:
        return (self.motor_current, self.housing_temp, self.vibration_frequency)


@dataclass(frozen=True)
class MotorParameters:
    """Healthy operating point, noise and fault response (all placeholders)."""

    current_nominal_a: float = 1.80
    current_noise_a: float = 0.03
    current_wear_gain_a: float = 0.45

    temp_nominal_c: float = 45.0
    temp_noise_c: float = 0.15
    temp_wear_gain_c: float = 9.0
    temp_time_constant_s: float = 12.0

    vibration_nominal_hz: float = 120.0
    vibration_noise_hz: float = 1.2
    vibration_wear_gain_hz: float = 22.0
    vibration_wear_noise_gain: float = 1.5  # noise grows to (1 + gain) x at full wear


class StepperMotorSimulator:
    """Seeded stepper-motor signal source with an optional progressive wear fault.

    Args:
        seed: Random seed.
        sample_period_s: Simulated time between samples (0.1 s = 10 Hz).
        fault_after_s: When wear starts. ``None`` keeps the motor healthy.
        wear_ramp_s: Time for wear to go from 0 to 1.
        params: Operating point and fault response.
        temp_drift_c_per_min: Benign housing-temperature drift, e.g. the factory
            warming through the day. It is not a fault and is not counted as wear.
        epoch: Timestamp of the first sample. Defaults to a fixed date so runs
            are reproducible; the OPC UA server passes its start time instead.
    """

    def __init__(
        self,
        seed: int = 42,
        sample_period_s: float = 0.1,
        fault_after_s: float | None = 15.0,
        wear_ramp_s: float = 10.0,
        params: MotorParameters | None = None,
        temp_drift_c_per_min: float = 0.0,
        epoch: datetime = SIM_EPOCH,
    ) -> None:
        if sample_period_s <= 0:
            raise ValueError("sample_period_s must be positive")
        if wear_ramp_s <= 0:
            raise ValueError("wear_ramp_s must be positive")
        self.sample_period_s = sample_period_s
        self.fault_after_s = fault_after_s
        self.wear_ramp_s = wear_ramp_s
        self.params = params or MotorParameters()
        self.temp_drift_c_per_min = temp_drift_c_per_min
        self.epoch = epoch
        self._rng = np.random.default_rng(seed)
        self._index = 0
        self._temp_rise = 0.0  # lagged temperature rise caused by wear

    def wear_at(self, t: float) -> float:
        """Ground-truth wear level (0..1) at simulated time ``t``."""
        if self.fault_after_s is None or t < self.fault_after_s:
            return 0.0
        return min(1.0, (t - self.fault_after_s) / self.wear_ramp_s)

    def read(self) -> MotorSample:
        """Advance the simulated clock by one sample period and return the reading."""
        p = self.params
        index = self._index
        t = index * self.sample_period_s
        wear = self.wear_at(t)

        # First-order lag of the housing temperature towards its wear-dependent target.
        alpha = 1.0 - math.exp(-self.sample_period_s / p.temp_time_constant_s)
        self._temp_rise += alpha * (p.temp_wear_gain_c * wear - self._temp_rise)

        noise = self._rng.standard_normal(3)
        current = p.current_nominal_a + p.current_wear_gain_a * wear + p.current_noise_a * noise[0]
        drift = self.temp_drift_c_per_min * t / 60.0
        temp = p.temp_nominal_c + drift + self._temp_rise + p.temp_noise_c * noise[1]
        vibration = (
            p.vibration_nominal_hz
            + p.vibration_wear_gain_hz * wear
            + p.vibration_noise_hz * (1.0 + p.vibration_wear_noise_gain * wear) * noise[2]
        )

        self._index += 1
        return MotorSample(
            index=index,
            t=round(t, 6),
            timestamp=self.epoch + timedelta(seconds=t),
            motor_current=float(current),
            housing_temp=float(temp),
            vibration_frequency=float(vibration),
            wear=wear,
        )
