"""Discrete PID controller with anti-windup (BioPrint-PID FR-1).

This is the kind of PID block a PLC runs on a fixed scan time. Three choices make
it robust on a real heater:

* **Derivative on measurement.** The D term acts on the process variable, not the
  error, so a setpoint step doesn't cause a derivative "kick" in heater output.
* **Filtered derivative.** A first-order low-pass filter (time constant
  ``Td / N``) stops sensor noise from being amplified into heater chatter.
* **Anti-windup by conditional integration.** While the output is saturated
  (0 % or 100 % duty), the integrator stops accumulating any error that would push
  it further into saturation. During a long warm-up from ambient, the integral
  therefore can't build up a huge store of error that later causes a large
  overshoot.

Output is a heater duty cycle in percent (0–100).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PIDGains:
    """Parallel-form PID gains. ``ki`` is per second; ``kd`` is in seconds."""

    kp: float
    ki: float
    kd: float


class DiscretePID:
    """Fixed-step PID controller.

    Args:
        gains: Proportional, integral and derivative gains.
        dt: Controller scan time in seconds (PRD: 0.1 s).
        out_min: Lower output limit (% duty).
        out_max: Upper output limit (% duty).
        derivative_filter_n: Filter coefficient N; the derivative filter time
            constant is ``kd / (kp * N)``. Typical values are 5–20.
        anti_windup: Turn conditional integration on (default) or off. Off exists
            only to demonstrate windup in the tuning comparison.
    """

    def __init__(
        self,
        gains: PIDGains,
        dt: float = 0.1,
        out_min: float = 0.0,
        out_max: float = 100.0,
        derivative_filter_n: float = 10.0,
        anti_windup: bool = True,
    ) -> None:
        if dt <= 0:
            raise ValueError("dt must be positive")
        if out_min >= out_max:
            raise ValueError("out_min must be below out_max")

        self.gains = gains
        self.dt = dt
        self.out_min = out_min
        self.out_max = out_max
        self.anti_windup = anti_windup

        # Filter time constant for the derivative term; 0 disables filtering.
        self._tf = (gains.kd / (gains.kp * derivative_filter_n)) if gains.kp > 0 else 0.0

        self.reset()

    def reset(self) -> None:
        """Clear the integrator and derivative memory."""
        self.integral: float = 0.0
        self._d_state: float = 0.0
        self._prev_measurement: float | None = None
        self.last_terms: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def update(self, setpoint: float, measurement: float) -> float:
        """Run one scan and return the clamped output (% duty)."""
        error = setpoint - measurement
        g = self.gains

        # --- Derivative on measurement, low-pass filtered -------------------
        if self._prev_measurement is None:
            raw_d = 0.0
        else:
            raw_d = -(measurement - self._prev_measurement) / self.dt
        alpha = self.dt / (self._tf + self.dt) if self._tf > 0 else 1.0
        self._d_state += alpha * (raw_d - self._d_state)
        self._prev_measurement = measurement

        p_term = g.kp * error
        d_term = g.kd * self._d_state

        # --- Integral with conditional integration (anti-windup) ------------
        candidate_integral = self.integral + g.ki * error * self.dt
        unclamped = p_term + candidate_integral + d_term
        saturated_high = unclamped > self.out_max and error > 0
        saturated_low = unclamped < self.out_min and error < 0
        if not (self.anti_windup and (saturated_high or saturated_low)):
            self.integral = candidate_integral
        if self.anti_windup:
            # Hard bound as a second line of defence against numerical drift.
            self.integral = float(np.clip(self.integral, self.out_min, self.out_max))

        output = float(np.clip(p_term + self.integral + d_term, self.out_min, self.out_max))
        self.last_terms = (p_term, self.integral, d_term)
        return output
