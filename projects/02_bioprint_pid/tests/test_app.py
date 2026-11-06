"""End-to-end scenario checks (PRD §7), including robustness across noise seeds."""

from pathlib import Path

import app
import numpy as np
import pytest
from config import load_config

SEEDS = range(50)
EXAMPLES = sorted((Path(__file__).resolve().parent.parent / "config" / "examples").glob("*.toml"))


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _run(cfg, seed: int):
    run = app.simulate(cfg, seed=seed)
    return run, app.pre_disturbance_metrics(cfg, run)


def test_default_seed_passes_every_check(cfg) -> None:
    run, metrics = _run(cfg, cfg.scenario.seed)
    checks = app.acceptance_checks(cfg, run, metrics)
    assert all(c["pass"] for c in checks.values()), checks


def test_fr1_holds_for_every_seed(cfg) -> None:
    for seed in SEEDS:
        _, m = _run(cfg, seed)
        assert m.overshoot_pct < 5.0, (seed, m)
        assert m.settling_time_s < 15.0, (seed, m)


def test_fr3_detection_and_false_rejection_rate(cfg) -> None:
    """The disturbance is always caught. False rejects in the 3 s stable window stay rare.

    Evaluating every 10 Hz sample against 1-3s / 2-2s has a per-sample false-reject
    probability of about 0.3 %, so roughly 10 % of runs show one spurious reject in 30
    samples. That is expected, and the test bounds it.
    """
    detected = false_rejects = 0
    dstep = cfg.scenario.disturbance_step
    for seed in SEEDS:
        run, _ = _run(cfg, seed)
        rejects = [v for v in run.qc.violations if v.severity == "REJECT"]
        detected += any(v.step >= dstep for v in rejects)
        false_rejects += any(v.step < dstep for v in rejects)
    assert detected == len(SEEDS)
    assert false_rejects / len(SEEDS) <= 0.15


def test_untuned_loop_winds_up(cfg) -> None:
    run = app.simulate(cfg, cfg.untuned, anti_windup=False, with_qc=False)
    assert app.pre_disturbance_metrics(cfg, run).overshoot_pct > 20.0


def test_disturbance_raises_pressure(cfg) -> None:
    """Melt cools after the disturbance, so straight-run pressure rises above 45 bar."""
    run, _ = _run(cfg, 42)
    df = run.frame
    straight = df["gantry_speed"] == 1.0
    before = df[straight & df["step"].between(100, 149)]["pressure_bar"].mean()
    after = df[straight & df["step"].between(155, 185)]["pressure_bar"].max()
    assert after > before + 1.0


def test_runs_do_not_leak_disturbance(cfg) -> None:
    """A disturbed run must not change the shared config used by the next run."""
    ambient = cfg.thermal.ambient_temp
    app.simulate(cfg)
    assert cfg.thermal.ambient_temp == ambient
    a = app.simulate(cfg, seed=3).frame["temperature_c"].to_numpy()
    b = app.simulate(cfg, seed=3).frame["temperature_c"].to_numpy()
    assert np.array_equal(a, b)


def test_signals_are_finite(cfg) -> None:
    run, _ = _run(cfg, 1)
    numeric = run.frame.select_dtypes("number").to_numpy()
    assert np.isfinite(numeric).all()


def test_material_sensitivity_table(cfg) -> None:
    table = app.material_sensitivity(cfg)
    ref = table[(table.fibre_wt_pct == 15.0) & (table.recycle_generation == 1)]
    assert ref["pressure_bar"].iloc[0] == pytest.approx(45.0)
    for _, grp in table.groupby("recycle_generation"):
        assert grp["pressure_bar"].is_monotonic_increasing  # more fibre, stiffer melt


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.stem)
def test_example_configs_load_and_run(example: Path) -> None:
    cfg = load_config(example)
    run = app.simulate(cfg)
    assert len(run.frame) == cfg.scenario.steps


def test_main_writes_outputs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    assert app.main([]) == 0
    for name in (
        "pid_step_response.png",
        "pid_tuning_comparison.png",
        "material_sensitivity.png",
        "layer_time_window.png",
        "material_sensitivity.csv",
        "pid_run.csv",
        "qc_violations.csv",
        "acceptance.json",
    ):
        assert (tmp_path / name).exists(), name


def test_main_rejects_bad_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    bad = tmp_path / "bad.toml"
    bad.write_text("[thermal]\nheat_capcity = 1.0\n")
    assert app.main(["--config", str(bad)]) == 2
