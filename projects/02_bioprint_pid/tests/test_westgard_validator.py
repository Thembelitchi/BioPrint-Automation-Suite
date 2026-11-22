"""FR-3: Westgard multirule logic."""

import pytest
from westgard_validator import BiomedicalQAMonitor


def armed_monitor(sd_pattern: tuple[float, float] = (229.0, 231.0)) -> BiomedicalQAMonitor:
    """Monitor whose baseline alternates 229/231 → mean 230, SD ≈ 1.0."""
    qc = BiomedicalQAMonitor(target=230.0, baseline_start=0, baseline_length=20)
    for k in range(20):
        qc.evaluate(k, sd_pattern[k % 2])
    assert qc.is_armed
    return qc


def test_baseline_fixes_sd() -> None:
    qc = armed_monitor()
    assert qc.baseline_sd == pytest.approx(1.026, abs=0.01)
    assert qc.violations == []


def test_no_rules_before_baseline() -> None:
    qc = BiomedicalQAMonitor(target=230.0, baseline_start=10, baseline_length=10)
    assert qc.evaluate(0, 999.0) == []
    assert not qc.is_armed


def test_in_control_value_passes() -> None:
    qc = armed_monitor()
    assert qc.evaluate(20, 230.5) == []


def test_1_2s_warning() -> None:
    qc = armed_monitor()
    (v,) = qc.evaluate(20, 232.5)
    assert v.rule == "1-2s" and v.severity == "WARNING"
    assert not qc.rejected()


def test_1_3s_reject() -> None:
    qc = armed_monitor()
    rules = {v.rule for v in qc.evaluate(20, 234.0)}
    assert rules == {"1-3s"}
    assert qc.rejected()


def test_2_2s_same_side_rejects() -> None:
    qc = armed_monitor()
    qc.evaluate(20, 232.4)
    rules = {v.rule for v in qc.evaluate(21, 232.4)}
    assert "2-2s" in rules
    assert qc.rejected()


def test_2_2s_needs_same_side() -> None:
    qc = armed_monitor()
    qc.evaluate(20, 232.4)
    rules = {v.rule for v in qc.evaluate(21, 227.6)}
    assert "2-2s" not in rules


def test_limits_and_rolling_stats() -> None:
    qc = armed_monitor()
    lim = qc.limits()
    assert lim["+3sd"] > lim["+2sd"] > lim["+1sd"] > 230.0 > lim["-1sd"]
    stats = qc.rolling()
    assert stats.mean == pytest.approx(230.0, abs=0.2)
    assert stats.cv_pct < 1.0


def test_min_sd_floor() -> None:
    qc = BiomedicalQAMonitor(target=230.0, baseline_start=0, baseline_length=10, min_sd=0.05)
    for k in range(10):
        qc.evaluate(k, 230.0)
    assert qc.baseline_sd == 0.05
