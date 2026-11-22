"""Step-response performance metrics used to verify FR-1."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class StepMetrics:
    overshoot_pct: float  # peak above setpoint as % of the step size
    rise_time_s: float  # 10 % → 90 % of the step
    settling_time_s: float  # last time outside the ±band before the window ends
    iae: float  # integral of absolute error (°C·s)

    def as_dict(self) -> dict[str, float]:
        return {
            "overshoot_pct": round(self.overshoot_pct, 3),
            "rise_time_s": round(self.rise_time_s, 2),
            "settling_time_s": round(self.settling_time_s, 2),
            "iae": round(self.iae, 2),
        }


def step_metrics(
    pv: np.ndarray, setpoint: float, initial: float, dt: float, band: float = 1.0
) -> StepMetrics:
    """Compute overshoot, rise time, settling time and IAE for a step from ``initial``.

    ``pv`` should cover only the window of interest (e.g. before a disturbance).
    The settling time is the end of the last sample outside ``setpoint ± band``;
    it is ``inf`` if the response is still outside the band at the end.
    """
    pv = np.asarray(pv, dtype=float)
    step = setpoint - initial
    if step == 0:
        raise ValueError("setpoint equals initial value; no step to measure")

    overshoot = (
        max(0.0, (pv.max() - setpoint) / step * 100.0)
        if step > 0
        else max(0.0, (setpoint - pv.min()) / -step * 100.0)
    )

    frac = (pv - initial) / step
    i10 = np.argmax(frac >= 0.1) if np.any(frac >= 0.1) else None
    i90 = np.argmax(frac >= 0.9) if np.any(frac >= 0.9) else None
    rise = (i90 - i10) * dt if i10 is not None and i90 is not None else float("inf")

    outside = np.where(np.abs(pv - setpoint) > band)[0]
    if outside.size == 0:
        settling = 0.0
    elif outside[-1] == pv.size - 1:
        settling = float("inf")
    else:
        settling = (outside[-1] + 1) * dt

    iae = float(np.sum(np.abs(setpoint - pv)) * dt)
    return StepMetrics(float(overshoot), float(rise), float(settling), iae)
