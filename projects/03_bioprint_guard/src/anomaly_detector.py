"""Streaming anomaly detector: multivariate Z-score + EWMA mapped to a 0–1 risk score (FR-3).

The approach is the one a clinical lab uses for its control material, applied
to a motor:

1. **Fix the limits from a stable baseline.** The first ``baseline_samples``
   readings set the mean and SD of each signal. They are then frozen. A baseline
   that keeps adapting would learn a slow fault as the new normal, which is
   exactly the failure mode a wear detector must avoid.
2. **Judge each reading against those limits.** Each signal becomes a Z-score,
   and the three are combined into one distance (root mean square of the Z's;
   about 1 for a healthy motor).
3. **Look at the pattern, not one value.** An exponentially weighted moving
   average (EWMA) of that distance smooths single-sample noise, so one outlier
   cannot raise an alert but a sustained shift does.
4. **Map to 0–1.** A logistic curve turns the smoothed distance into a risk
   score: about 0 when healthy, 0.5 at ``midpoint`` SDs, close to 1 beyond it.

While the baseline is being collected the score is 0 and ``ready`` is False.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

ALERT_THRESHOLD = 0.85
CRITICAL_THRESHOLD = 0.95


def severity_for(score: float) -> str | None:
    """Alert severity for a risk score, or ``None`` below the alert threshold."""
    if score >= CRITICAL_THRESHOLD:
        return "CRITICAL"
    if score >= ALERT_THRESHOLD:
        return "HIGH"
    return None


@dataclass(frozen=True)
class ScoredSample:
    """Detector output for one reading."""

    score: float  # 0..1 risk
    distance: float  # RMS Z-score of this reading
    smoothed_distance: float  # EWMA of the distance
    z_scores: tuple[float, ...]
    ready: bool  # False while the baseline is still being collected


class StreamingAnomalyDetector:
    """Online wear scorer over a fixed number of synchronous signals.

    Args:
        baseline_samples: Readings used to fix each signal's mean and SD.
        ewma_alpha: EWMA weight of the newest reading (0 < alpha <= 1).
        midpoint: Smoothed distance (in SDs) that maps to a score of 0.5.
        steepness: Width of the logistic transition, in SDs.
        min_sd: Floor on each baseline SD, as a fraction of the baseline mean's
            magnitude, so a perfectly flat baseline cannot divide by zero.
    """

    def __init__(
        self,
        baseline_samples: int = 100,
        ewma_alpha: float = 0.15,
        midpoint: float = 4.0,
        steepness: float = 0.6,
        min_sd: float = 1e-6,
    ) -> None:
        if baseline_samples < 10:
            raise ValueError("baseline_samples must be at least 10")
        if not 0.0 < ewma_alpha <= 1.0:
            raise ValueError("ewma_alpha must be in (0, 1]")
        if steepness <= 0:
            raise ValueError("steepness must be positive")
        self.baseline_samples = baseline_samples
        self.ewma_alpha = ewma_alpha
        self.midpoint = midpoint
        self.steepness = steepness
        self.min_sd = min_sd
        self._baseline: list[tuple[float, ...]] = []
        self._mean: np.ndarray | None = None
        self._sd: np.ndarray | None = None
        self._ewma: float | None = None

    @property
    def ready(self) -> bool:
        """True once the baseline limits are fixed."""
        return self._mean is not None

    @property
    def baseline_mean(self) -> tuple[float, ...] | None:
        return None if self._mean is None else tuple(float(v) for v in self._mean)

    @property
    def baseline_sd(self) -> tuple[float, ...] | None:
        return None if self._sd is None else tuple(float(v) for v in self._sd)

    def _fix_baseline(self) -> None:
        data = np.asarray(self._baseline, dtype=float)
        self._mean = data.mean(axis=0)
        floor = np.maximum(np.abs(self._mean) * self.min_sd, 1e-12)
        self._sd = np.maximum(data.std(axis=0, ddof=1), floor)
        self._baseline.clear()

    def update(self, values: Sequence[float]) -> ScoredSample:
        """Score one reading (the same signals, in the same order, every call)."""
        row = tuple(float(v) for v in values)
        if any(not math.isfinite(v) for v in row):
            raise ValueError(f"non-finite reading: {row}")

        if self._mean is None:
            if self._baseline and len(row) != len(self._baseline[0]):
                raise ValueError("number of signals changed between readings")
            self._baseline.append(row)
            if len(self._baseline) >= self.baseline_samples:
                self._fix_baseline()
            return ScoredSample(0.0, 0.0, 0.0, tuple(0.0 for _ in row), False)

        if len(row) != len(self._mean):
            raise ValueError("number of signals changed between readings")
        z = (np.asarray(row) - self._mean) / self._sd
        distance = float(np.sqrt(np.mean(z**2)))
        if self._ewma is None:
            self._ewma = distance
        else:
            self._ewma += self.ewma_alpha * (distance - self._ewma)
        exponent = (self._ewma - self.midpoint) / self.steepness
        score = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, exponent))))
        return ScoredSample(score, distance, self._ewma, tuple(float(v) for v in z), True)
