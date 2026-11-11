"""Clinical Westgard QC applied to an industrial process variable (BioPrint-PID FR-3).

A clinical lab runs control material with every batch of patient samples. Before
the controls are used for decisions, the lab measures them during a stable period
to fix a mean and standard deviation (SD). Each new control result is then
judged against those fixed limits using the Westgard multirule:

* **1-2s** (warning): one result beyond ±2 SD. Look more closely; don't reject yet.
* **1-3s** (reject): one result beyond ±3 SD. Usually random error.
* **2-2s** (reject): two consecutive results beyond 2 SD *on the same side*.
  Usually systematic error, such as drift, a calibration shift or here a
  change in the process environment.

``BiomedicalQAMonitor`` does exactly this with the barrel temperature. It
collects a baseline window once the loop is settled, freezes the SD, then
evaluates every later sample against the setpoint (the "target value" of the
control material). It also reports rolling mean, SD and CV% for the
Levey-Jennings chart.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

Rule = Literal["1-2s", "1-3s", "2-2s"]
Severity = Literal["WARNING", "REJECT"]

RULE_SEVERITY: dict[Rule, Severity] = {"1-2s": "WARNING", "1-3s": "REJECT", "2-2s": "REJECT"}


@dataclass(frozen=True)
class QCViolation:
    step: int
    time_s: float
    rule: Rule
    severity: Severity
    value: float
    z_score: float


@dataclass
class QCStatistics:
    """Rolling statistics over the recent window (for the dashboard)."""

    mean: float
    sd: float
    cv_pct: float


@dataclass
class BiomedicalQAMonitor:
    """Westgard multirule monitor for a single process variable.

    Args:
        target: Target value (the setpoint), like a control material's assigned value.
        baseline_start: First step of the baseline window (after the loop has settled).
        baseline_length: Number of samples used to establish the SD.
        dt: Sample interval in seconds, used to timestamp violations.
        rolling_window: Window length for the rolling mean, SD and CV% statistics.
        min_sd: Floor on the baseline SD, so a near-noiseless baseline can't make
            the limits unrealistically tight.
    """

    target: float
    baseline_start: int
    baseline_length: int = 40
    dt: float = 0.1
    rolling_window: int = 20
    min_sd: float = 0.05

    baseline_sd: float | None = field(default=None, init=False)
    baseline_mean: float | None = field(default=None, init=False)
    violations: list[QCViolation] = field(default_factory=list, init=False)
    _baseline: list[float] = field(default_factory=list, init=False)
    _recent: deque[float] = field(init=False)
    _prev_z: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.baseline_length < 10:
            raise ValueError("baseline_length should be at least 10 samples")
        self._recent = deque(maxlen=self.rolling_window)

    @property
    def baseline_end(self) -> int:
        return self.baseline_start + self.baseline_length

    @property
    def is_armed(self) -> bool:
        """True once the baseline is established and rules are being evaluated."""
        return self.baseline_sd is not None

    def limits(self) -> dict[str, float]:
        """Control limits (target ± 1/2/3 SD) for the Levey-Jennings chart."""
        if self.baseline_sd is None:
            raise RuntimeError("baseline not yet established")
        sd = self.baseline_sd
        return {
            f"{sign}{k}sd": self.target + (k * sd if sign == "+" else -k * sd)
            for k in (1, 2, 3)
            for sign in ("+", "-")
        }

    def rolling(self) -> QCStatistics:
        arr = np.asarray(self._recent, dtype=float)
        if arr.size < 2:
            return QCStatistics(
                mean=float(arr.mean()) if arr.size else float("nan"),
                sd=float("nan"),
                cv_pct=float("nan"),
            )
        mean = float(arr.mean())
        sd = float(arr.std(ddof=1))
        return QCStatistics(mean=mean, sd=sd, cv_pct=100.0 * sd / mean if mean else float("nan"))

    def evaluate(self, step: int, value: float) -> list[QCViolation]:
        """Feed one sample. Returns the violations raised by this sample (possibly none)."""
        self._recent.append(value)

        # Phase 1: collect the baseline window.
        if self.baseline_start <= step < self.baseline_end:
            self._baseline.append(value)
            if len(self._baseline) == self.baseline_length:
                arr = np.asarray(self._baseline)
                self.baseline_mean = float(arr.mean())
                self.baseline_sd = max(float(arr.std(ddof=1)), self.min_sd)
            return []
        if not self.is_armed:
            return []

        # Phase 2: evaluate the rules against the target value and the fixed SD.
        assert self.baseline_sd is not None
        z = (value - self.target) / self.baseline_sd
        raised: list[QCViolation] = []

        def flag(rule: Rule) -> None:
            raised.append(
                QCViolation(
                    step, round(step * self.dt, 3), rule, RULE_SEVERITY[rule], value, round(z, 3)
                )
            )

        if abs(z) > 3.0:
            flag("1-3s")
        elif abs(z) > 2.0:
            flag("1-2s")
        if (
            self._prev_z is not None
            and abs(z) > 2.0
            and abs(self._prev_z) > 2.0
            and np.sign(z) == np.sign(self._prev_z)
        ):
            flag("2-2s")

        self._prev_z = z
        self.violations.extend(raised)
        return raised

    def rejected(self) -> bool:
        """True if any reject rule fired: the run would be rejected in a lab."""
        return any(v.severity == "REJECT" for v in self.violations)
