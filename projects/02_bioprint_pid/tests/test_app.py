"""End-to-end scenario checks (PRD §7), including robustness across noise seeds."""

import app
import numpy as np
from metrics import step_metrics

SEEDS = range(50)


def _run(seed: int):
    run = app.simulate(app.TUNED, seed=seed)
    pre = run.frame["temperature_c"].to_numpy()[: app.DISTURBANCE_STEP]
    return run, step_metrics(pre, app.SETPOINT, 25.0, app.DT)


def test_default_seed_passes_every_check() -> None:
    run, metrics = _run(42)
    checks = app.acceptance_checks(run, metrics)
    assert all(c["pass"] for c in checks.values()), checks


def test_fr1_holds_for_every_seed() -> None:
    for seed in SEEDS:
        _, m = _run(seed)
        assert m.overshoot_pct < 5.0, (seed, m)
        assert m.settling_time_s < 15.0, (seed, m)


def test_fr3_detection_and_false_rejection_rate() -> None:
    """The disturbance is always caught. False rejects in the 3 s stable window stay rare.

    Evaluating every 10 Hz sample against 1-3s / 2-2s has a per-sample false-reject
    probability of about 0.3 %, so roughly 10 % of runs show one spurious reject in 30
    samples. That is expected, and the test bounds it.
    """
    detected = false_rejects = 0
    for seed in SEEDS:
        run, _ = _run(seed)
        rejects = [v for v in run.qc.violations if v.severity == "REJECT"]
        detected += any(v.step >= app.DISTURBANCE_STEP for v in rejects)
        false_rejects += any(v.step < app.DISTURBANCE_STEP for v in rejects)
    assert detected == len(SEEDS)
    assert false_rejects / len(SEEDS) <= 0.15


def test_untuned_loop_winds_up() -> None:
    run = app.simulate(app.UNTUNED, anti_windup=False, with_qc=False)
    pre = run.frame["temperature_c"].to_numpy()[: app.DISTURBANCE_STEP]
    assert step_metrics(pre, app.SETPOINT, 25.0, app.DT).overshoot_pct > 20.0


def test_disturbance_raises_pressure() -> None:
    """Melt cools after the disturbance, so straight-run pressure rises above 45 bar."""
    run, _ = _run(42)
    df = run.frame
    straight = df["gantry_speed"] == 1.0
    before = df[straight & df["step"].between(100, 149)]["pressure_bar"].mean()
    after = df[straight & df["step"].between(155, 185)]["pressure_bar"].max()
    assert after > before + 1.0


def test_signals_are_finite() -> None:
    run, _ = _run(1)
    numeric = run.frame.select_dtypes("number").to_numpy()
    assert np.isfinite(numeric).all()


def test_main_writes_outputs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    assert app.main([]) == 0
    for name in (
        "pid_step_response.png",
        "pid_tuning_comparison.png",
        "pid_run.csv",
        "qc_violations.csv",
        "acceptance.json",
    ):
        assert (tmp_path / name).exists(), name
