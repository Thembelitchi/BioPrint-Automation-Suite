"""Streaming anomaly detectors that turn motor readings into a 0–1 risk score (FR-3).

Two detectors share one interface (``update(values) -> ScoredSample``):

:class:`StreamingAnomalyDetector` (default, ``--detector zscore``)
    Rolling multivariate Z-score + EWMA.

    1. **Limits from a healthy reference window.** Each signal's mean and SD come
       from a window of ``baseline_samples`` readings.
    2. **Judge each reading against those limits.** Each signal becomes a
       Z-score and the three are combined into one distance (root mean square of
       the Z's; about 1 for a healthy motor).
    3. **Look at the pattern, not one value.** An exponentially weighted moving
       average (EWMA) of that distance smooths single-sample noise, so one
       outlier cannot raise an alert but a sustained shift does.
    4. **Map to 0–1.** A logistic curve turns the smoothed distance into a risk
       score: about 0 when healthy, 0.5 at ``midpoint`` SDs, close to 1 beyond.

    The reference window can be ``"rolling"`` (default) or ``"frozen"``:

    * ``rolling`` follows slow, benign change such as the housing warming with
      the factory through the day. Two guards stop it from learning a fault as
      normal. Readings enter the window only after waiting ``lag_samples``, so
      the window always describes the motor a few seconds ago. And while the
      smoothed distance is above ``hold_distance`` the window is **held**: the
      waiting readings are discarded and nothing new is admitted.
    * ``frozen`` fixes the limits from the first ``baseline_samples`` readings
      and never changes them, the way a clinical lab fixes control limits from a
      stable run of control material. It cannot absorb any drift, so it catches
      arbitrarily slow wear, but benign drift will eventually alert too.

:class:`IsolationForestDetector` (optional, ``--detector iforest``)
    A scikit-learn Isolation Forest trained on the baseline window. It makes no
    assumption about each signal's distribution and scores a reading by how
    easily random splits isolate it. Its limits are fixed once trained.

While the baseline is being collected the score is 0 and ``ready`` is False.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

ALERT_THRESHOLD = 0.85
CRITICAL_THRESHOLD = 0.95

DETECTORS: tuple[str, ...] = ("zscore", "iforest")
BASELINE_MODES: tuple[str, ...] = ("rolling", "frozen")


def severity_for(score: float) -> str | None:
    """Alert severity for a risk score, or ``None`` below the alert threshold."""
    if score >= CRITICAL_THRESHOLD:
        return "CRITICAL"
    if score >= ALERT_THRESHOLD:
        return "HIGH"
    return None


def _logistic(value: float, midpoint: float, steepness: float) -> float:
    exponent = (value - midpoint) / steepness
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, exponent))))


def _validated(values: Sequence[float], expected: int | None) -> tuple[float, ...]:
    row = tuple(float(v) for v in values)
    if any(not math.isfinite(v) for v in row):
        raise ValueError(f"non-finite reading: {row}")
    if expected is not None and len(row) != expected:
        raise ValueError("number of signals changed between readings")
    return row


@dataclass(frozen=True)
class ScoredSample:
    """Detector output for one reading."""

    score: float  # 0..1 risk
    distance: float  # this reading's distance from healthy (detector-specific units)
    smoothed_distance: float  # EWMA of the distance
    z_scores: tuple[float, ...]  # per-signal Z-scores against the reference window
    ready: bool  # False while the baseline is still being collected


class Detector(Protocol):
    """What the gateway needs from a detector."""

    @property
    def ready(self) -> bool: ...

    def update(self, values: Sequence[float]) -> ScoredSample: ...


class StreamingAnomalyDetector:
    """Rolling (or frozen) multivariate Z-score + EWMA wear scorer.

    Args:
        baseline_samples: Size of the reference window, and the number of
            readings collected before scoring starts.
        baseline: ``"rolling"`` or ``"frozen"`` (see the module docstring).
        lag_samples: Rolling only. Readings wait this long before entering the
            reference window.
        hold_distance: Rolling only. The window is held while the smoothed
            distance is above this many SDs (a healthy motor sits near 1).
        ewma_alpha: EWMA weight of the newest reading (0 < alpha <= 1).
        midpoint: Smoothed distance (in SDs) that maps to a score of 0.5.
        steepness: Width of the logistic transition, in SDs.
        min_sd: Floor on each SD, as a fraction of the mean's magnitude, so a
            perfectly flat window cannot divide by zero.
    """

    def __init__(
        self,
        baseline_samples: int = 100,
        baseline: str = "rolling",
        lag_samples: int = 50,
        hold_distance: float = 2.0,
        ewma_alpha: float = 0.15,
        midpoint: float = 4.0,
        steepness: float = 0.6,
        min_sd: float = 1e-6,
    ) -> None:
        if baseline_samples < 10:
            raise ValueError("baseline_samples must be at least 10")
        if baseline not in BASELINE_MODES:
            raise ValueError(f"baseline must be one of {BASELINE_MODES}")
        if lag_samples < 0:
            raise ValueError("lag_samples must not be negative")
        if not 0.0 < ewma_alpha <= 1.0:
            raise ValueError("ewma_alpha must be in (0, 1]")
        if steepness <= 0:
            raise ValueError("steepness must be positive")
        if not 0.0 < hold_distance < midpoint:
            raise ValueError("hold_distance must be positive and below midpoint")
        self.baseline_samples = baseline_samples
        self.baseline = baseline
        self.lag_samples = lag_samples
        self.hold_distance = hold_distance
        self.ewma_alpha = ewma_alpha
        self.midpoint = midpoint
        self.steepness = steepness
        self.min_sd = min_sd
        self._window: deque[tuple[float, ...]] = deque(maxlen=baseline_samples)
        self._waiting: deque[tuple[float, ...]] = deque()
        self._mean: np.ndarray | None = None
        self._sd: np.ndarray | None = None
        self._ewma: float | None = None
        self._holding = False

    @property
    def ready(self) -> bool:
        """True once the reference window is full and readings are being scored."""
        return self._mean is not None

    @property
    def holding(self) -> bool:
        """True while a rolling window is held because the motor looks abnormal."""
        return self._holding

    @property
    def baseline_mean(self) -> tuple[float, ...] | None:
        return None if self._mean is None else tuple(float(v) for v in self._mean)

    @property
    def baseline_sd(self) -> tuple[float, ...] | None:
        return None if self._sd is None else tuple(float(v) for v in self._sd)

    def _refresh_limits(self) -> None:
        data = np.asarray(self._window, dtype=float)
        self._mean = data.mean(axis=0)
        floor = np.maximum(np.abs(self._mean) * self.min_sd, 1e-12)
        self._sd = np.maximum(data.std(axis=0, ddof=1), floor)

    def _roll(self, row: tuple[float, ...]) -> None:
        """Admit a reading to the rolling window, after the lag, unless held."""
        self._holding = self._ewma is not None and self._ewma > self.hold_distance
        if self._holding:
            self._waiting.clear()  # readings taken just before a hold are suspect too
            return
        self._waiting.append(row)
        if len(self._waiting) > self.lag_samples:
            self._window.append(self._waiting.popleft())
            self._refresh_limits()

    def update(self, values: Sequence[float]) -> ScoredSample:
        """Score one reading (the same signals, in the same order, every call)."""
        row = _validated(values, len(self._window[0]) if self._window else None)

        if self._mean is None:
            self._window.append(row)
            if len(self._window) >= self.baseline_samples:
                self._refresh_limits()
            return ScoredSample(0.0, 0.0, 0.0, tuple(0.0 for _ in row), False)

        z = (np.asarray(row) - self._mean) / self._sd
        distance = float(np.sqrt(np.mean(z**2)))
        if self._ewma is None:
            self._ewma = distance
        else:
            self._ewma += self.ewma_alpha * (distance - self._ewma)
        score = _logistic(self._ewma, self.midpoint, self.steepness)
        if self.baseline == "rolling":
            self._roll(row)
        return ScoredSample(score, distance, self._ewma, tuple(float(v) for v in z), True)


class IsolationForestDetector:
    """Isolation Forest + EWMA wear scorer (needs scikit-learn).

    The forest is trained once, on the first ``baseline_samples`` readings
    (standardised per signal). A reading's raw anomaly score is rescaled to
    0..1 between the baseline's median score and the score of a point far
    outside the training data, which is where an Isolation Forest saturates.
    That rescaled value is smoothed with an EWMA and mapped through a logistic
    curve, like the Z-score detector.

    Args:
        baseline_samples: Readings used to train the forest.
        ewma_alpha: EWMA weight of the newest reading (0 < alpha <= 1).
        midpoint: Smoothed, rescaled anomaly (0..1) that maps to a score of 0.5.
        steepness: Width of the logistic transition.
        n_estimators: Trees in the forest.
        random_state: Seed for the forest, so runs are reproducible.
    """

    def __init__(
        self,
        baseline_samples: int = 100,
        ewma_alpha: float = 0.15,
        midpoint: float = 0.6,
        steepness: float = 0.07,
        n_estimators: int = 100,
        random_state: int = 0,
    ) -> None:
        if baseline_samples < 10:
            raise ValueError("baseline_samples must be at least 10")
        if not 0.0 < ewma_alpha <= 1.0:
            raise ValueError("ewma_alpha must be in (0, 1]")
        if steepness <= 0:
            raise ValueError("steepness must be positive")
        try:
            from sklearn.ensemble import IsolationForest
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ImportError(
                "The Isolation Forest detector needs scikit-learn: pip install scikit-learn"
            ) from exc
        self.baseline_samples = baseline_samples
        self.ewma_alpha = ewma_alpha
        self.midpoint = midpoint
        self.steepness = steepness
        self._forest: Any = IsolationForest(n_estimators=n_estimators, random_state=random_state)
        self._baseline: list[tuple[float, ...]] = []
        self._mean: np.ndarray | None = None
        self._sd: np.ndarray | None = None
        self._typical = 0.0  # median anomaly score of the baseline
        self._saturated = 1.0  # anomaly score of a point far outside the baseline
        self._ewma: float | None = None

    @property
    def ready(self) -> bool:
        """True once the forest is trained."""
        return self._mean is not None

    def _anomaly(self, standardised: np.ndarray) -> np.ndarray:
        return -self._forest.score_samples(standardised)  # higher = easier to isolate

    def _train(self) -> None:
        data = np.asarray(self._baseline, dtype=float)
        mean = data.mean(axis=0)
        sd = np.maximum(data.std(axis=0, ddof=1), np.maximum(np.abs(mean) * 1e-6, 1e-12))
        standardised = (data - mean) / sd
        self._forest.fit(standardised)
        self._typical = float(np.median(self._anomaly(standardised)))
        far = np.full((1, data.shape[1]), 50.0)  # 50 SDs out on every signal
        self._saturated = max(float(self._anomaly(far)[0]), self._typical + 1e-9)
        self._mean, self._sd = mean, sd
        self._baseline.clear()

    def update(self, values: Sequence[float]) -> ScoredSample:
        """Score one reading (the same signals, in the same order, every call)."""
        expected = len(self._mean) if self._mean is not None else None
        if expected is None and self._baseline:
            expected = len(self._baseline[0])
        row = _validated(values, expected)

        if self._mean is None:
            self._baseline.append(row)
            if len(self._baseline) >= self.baseline_samples:
                self._train()
            return ScoredSample(0.0, 0.0, 0.0, tuple(0.0 for _ in row), False)

        z = (np.asarray(row) - self._mean) / self._sd
        raw = float(self._anomaly(z[None, :])[0])
        span = self._saturated - self._typical
        distance = min(1.0, max(0.0, (raw - self._typical) / span))
        if self._ewma is None:
            self._ewma = distance
        else:
            self._ewma += self.ewma_alpha * (distance - self._ewma)
        score = _logistic(self._ewma, self.midpoint, self.steepness)
        return ScoredSample(score, distance, self._ewma, tuple(float(v) for v in z), True)


def make_detector(
    kind: str = "zscore", baseline: str = "rolling", baseline_samples: int = 100
) -> Detector:
    """Build a detector from the CLI choices ``--detector`` and ``--baseline``."""
    if kind == "zscore":
        return StreamingAnomalyDetector(baseline_samples=baseline_samples, baseline=baseline)
    if kind == "iforest":
        return IsolationForestDetector(baseline_samples=baseline_samples)
    raise ValueError(f"detector must be one of {DETECTORS}")
