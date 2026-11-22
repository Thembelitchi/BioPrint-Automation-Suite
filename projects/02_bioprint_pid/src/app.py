"""BioPrint-PID run: tuned PID on the extruder model, monitored by Westgard QC.

Run from the project folder:

    python src/app.py                         # defaults from config/default.toml
    python src/app.py --config my_values.toml # your own boundary values merged over the defaults
    python src/app.py --realtime              # pace each step to the wall clock
    python src/app.py --report-only           # always exit 0 (exploring parameters)

Scenario (PRD §4.5, defaults):
    * 30 s run, 300 steps at dt = 0.1 s, setpoint 230 °C, starting from ambient.
    * The auger starts once the barrel reaches melt temperature (cold-extrusion interlock).
    * The gantry slows at corners every 4 s, so auger flow and nozzle pressure vary.
    * At step 150 a severe ambient disturbance hits: a roller door opens, ambient
      drops from 25 °C to 5 °C and convective losses rise almost fourfold.
    * Westgard QC fixes its SD from a baseline window (steps 80–119) and evaluates
      every sample after it.

Outputs (in ``output/``):
    pid_step_response.png      tracking, controller effort, nozzle pressure, Levey-Jennings
    pid_tuning_comparison.png  untuned (no anti-windup) vs tuned step response
    material_sensitivity.png   nozzle pressure vs fibre loading, recycle generation, temperature
    layer_time_window.png      inter-layer adhesion window vs factory ambient temperature
    pid_run.csv                time series of every signal
    qc_violations.csv          Westgard events
    acceptance.json            PRD checks, the config source and model diagnostics

The exit code is 0 only if every PRD acceptance check passes (unless --report-only).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless rendering for CI

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from config import ConfigError, SimulationConfig, load_config  # noqa: E402
from extruder_physics import ExtruderThermalMass, gantry_speed_profile  # noqa: E402
from layer_thermal import biot_number, layer_window, time_constant, window_sweep  # noqa: E402
from metrics import StepMetrics, step_metrics  # noqa: E402
from pid_controller import DiscretePID, PIDGains  # noqa: E402
from westgard_validator import BiomedicalQAMonitor  # noqa: E402

log = logging.getLogger("bioprint_pid")

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
DEGRADATION_C = 260.0  # illustrative upper processing limit for the comparison chart


@dataclass
class RunResult:
    frame: pd.DataFrame
    qc: BiomedicalQAMonitor | None


def make_plant(cfg: SimulationConfig) -> ExtruderThermalMass:
    return ExtruderThermalMass(
        thermal=cfg.thermal,
        rheology=cfg.rheology,
        material=cfg.material,
        melt_setpoint=cfg.scenario.setpoint,
        interlock_band=cfg.interlock.band,
    )


def simulate(
    cfg: SimulationConfig,
    gains: PIDGains | None = None,
    anti_windup: bool = True,
    seed: int | None = None,
    with_disturbance: bool = True,
    with_qc: bool = True,
    realtime: bool = False,
) -> RunResult:
    """Run the closed loop for ``cfg.scenario.steps`` scans and return every signal."""
    sc, ctl = cfg.scenario, cfg.controller
    rng = np.random.default_rng(sc.seed if seed is None else seed)
    plant = make_plant(cfg)
    pid = DiscretePID(
        gains or ctl.gains,
        dt=sc.dt,
        out_min=ctl.out_min,
        out_max=ctl.out_max,
        derivative_filter_n=ctl.derivative_filter_n,
        anti_windup=anti_windup,
    )
    qc = (
        BiomedicalQAMonitor(
            target=sc.setpoint,
            baseline_start=cfg.qc.baseline_start,
            baseline_length=cfg.qc.baseline_length,
            dt=sc.dt,
            rolling_window=cfg.qc.rolling_window,
            min_sd=cfg.qc.min_sd,
        )
        if with_qc
        else None
    )
    g = cfg.gantry
    gantry = gantry_speed_profile(
        sc.steps, sc.dt, g.corner_every_s, g.corner_duration_s, g.corner_speed
    )

    measured = plant.state.temperature
    rows: list[dict[str, float | int | str]] = []
    next_tick = time.monotonic()

    for k in range(sc.steps):
        if with_disturbance and k == sc.disturbance_step:
            plant.apply_ambient_disturbance(sc.disturbed_ambient_temp, sc.disturbed_loss_coeff)

        sensor = measured + rng.normal(0.0, cfg.sensor.noise_sd)
        duty = pid.update(sc.setpoint, sensor)
        state = plant.step(duty, gantry[k], sc.dt)
        # First-order thermocouple lag between the true melt and the reading.
        measured += (state.temperature - measured) * sc.dt / cfg.sensor.lag_s

        flags = ""
        if qc is not None:
            flags = ",".join(v.rule for v in qc.evaluate(k, sensor))

        p, i, d = pid.last_terms
        rows.append(
            {
                "step": k,
                "time_s": round(k * sc.dt, 3),
                "setpoint_c": sc.setpoint,
                "temperature_c": state.temperature,
                "measured_c": sensor,
                "duty_pct": duty,
                "p_term": p,
                "i_term": i,
                "d_term": d,
                "gantry_speed": gantry[k],
                "flow_fraction": state.flow_fraction,
                "pressure_bar": state.pressure,
                "auger_on": int(state.auger_enabled),
                "qc_flags": flags,
            }
        )

        if realtime:
            # Deterministic pacing: aim at absolute tick times so error doesn't accumulate.
            next_tick += sc.dt
            time.sleep(max(0.0, next_tick - time.monotonic()))

    return RunResult(pd.DataFrame(rows), qc)


def pre_disturbance_metrics(cfg: SimulationConfig, run: RunResult) -> StepMetrics:
    pre = run.frame["temperature_c"].to_numpy()[: cfg.scenario.disturbance_step]
    return step_metrics(pre, cfg.scenario.setpoint, cfg.thermal.ambient_temp, cfg.scenario.dt)


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------


def plot_dashboard(cfg: SimulationConfig, run: RunResult, metrics: StepMetrics, path: Path) -> None:
    df, qc, sc = run.frame, run.qc, cfg.scenario
    assert qc is not None and qc.baseline_sd is not None
    sp = sc.setpoint
    t = df["time_s"].to_numpy()
    t_dist = sc.disturbance_step * sc.dt
    b0, bl = cfg.qc.baseline_start, cfg.qc.baseline_length

    fig, axes = plt.subplots(
        4, 1, figsize=(11, 12), sharex=True, gridspec_kw={"height_ratios": [3, 1.5, 1.5, 3]}
    )
    fig.suptitle("BioPrint-PID — tuned loop with Westgard QC", fontsize=14, weight="bold")

    ax = axes[0]
    ax.plot(t, df["setpoint_c"], "--", color="#555", lw=1.2, label=f"Setpoint {sp:.0f} °C")
    ax.plot(t, df["temperature_c"], color="#1f6feb", lw=1.8, label="Melt temperature")
    ax.axvline(t_dist, color="#d1242f", ls=":", lw=1.2)
    ax.text(
        t_dist + 0.2,
        cfg.thermal.ambient_temp + 35,
        "ambient\ndisturbance",
        color="#d1242f",
        fontsize=9,
    )
    ax.set_ylabel("°C")
    ax.set_title(
        f"Tracking — overshoot {metrics.overshoot_pct:.2f} %, "
        f"settled (±1 °C) in {metrics.settling_time_s:.1f} s",
        fontsize=10,
        loc="left",
    )
    ax.legend(loc="lower right", fontsize=9)
    inset = ax.inset_axes([0.42, 0.12, 0.33, 0.45])
    window = t >= t_dist - 5
    inset.plot(t[window], df["temperature_c"][window], color="#1f6feb", lw=1.2)
    inset.axhline(sp, color="#555", ls="--", lw=0.8)
    inset.axvline(t_dist, color="#d1242f", ls=":", lw=0.8)
    inset.set_title("zoom around the disturbance", fontsize=8)
    inset.tick_params(labelsize=7)

    ax = axes[1]
    ax.plot(t, df["duty_pct"], color="#8250df", lw=1.4)
    ax.axvline(t_dist, color="#d1242f", ls=":", lw=1.2)
    ax.set_ylabel("Heater %")
    ax.set_ylim(cfg.controller.out_min - 5, cfg.controller.out_max + 5)
    ax.set_title("Controller effort (clamped)", fontsize=10, loc="left")

    ax = axes[2]
    ax.plot(t, df["pressure_bar"], color="#bf8700", lw=1.4)
    ax.axvline(t_dist, color="#d1242f", ls=":", lw=1.2)
    ax.set_ylabel("bar")
    m = cfg.material
    ax.set_title(
        f"Nozzle pressure — power-law melt, {m.fibre_wt_pct:g} wt % GF, "
        f"recycle generation {m.recycle_generation} (dips at corners, rises when melt cools)",
        fontsize=10,
        loc="left",
    )

    ax = axes[3]
    sd = qc.baseline_sd
    for k, color in ((1, "#2da44e"), (2, "#bf8700"), (3, "#d1242f")):
        for sign in (1, -1):
            ax.axhline(sp + sign * k * sd, color=color, lw=0.9, ls="-" if k == 3 else "--")
        ax.text(t[-1] + 0.2, sp + k * sd, f"+{k}SD", color=color, fontsize=8, va="center")
        ax.text(t[-1] + 0.2, sp - k * sd, f"−{k}SD", color=color, fontsize=8, va="center")
    ax.axhline(sp, color="#555", lw=0.9)
    ax.axvspan(b0 * sc.dt, (b0 + bl) * sc.dt, color="#2da44e", alpha=0.08, label="Baseline window")

    lo, hi = sp - 5 * sd, sp + 5 * sd
    edge_lo, edge_hi = lo + 0.15 * sd, hi - 0.15 * sd

    def on_chart(values: np.ndarray) -> np.ndarray:
        """Pin off-scale points to the chart edge so excursions stay visible."""
        return np.clip(values, edge_lo, edge_hi)

    armed = (df["step"] >= b0).to_numpy()
    values = df["measured_c"].to_numpy()[armed]
    ax.plot(t[armed], on_chart(values), ".-", color="#1f6feb", lw=0.6, ms=3)
    off = (values < edge_lo) | (values > edge_hi)
    if off.any():
        ax.scatter(
            t[armed][off],
            on_chart(values[off]),
            marker="v",
            s=14,
            color="#57606a",
            zorder=4,
            label=f"off-scale ({off.sum()})",
        )
    for rule, marker, color in (
        ("1-2s", "o", "#bf8700"),
        ("1-3s", "X", "#d1242f"),
        ("2-2s", "s", "#8250df"),
    ):
        hits = [v for v in qc.violations if v.rule == rule]
        if hits:
            ax.scatter(
                [v.time_s for v in hits],
                on_chart(np.array([v.value for v in hits])),
                marker=marker,
                s=36,
                color=color,
                zorder=5,
                label=f"{rule} ({len(hits)})",
                facecolors="none" if rule == "1-2s" else color,
            )
    ax.axvline(t_dist, color="#d1242f", ls=":", lw=1.2)
    ax.set_ylim(lo, hi)
    ax.set_ylabel("°C (sensor)")
    ax.set_xlabel("Time (s)")
    ax.set_title(
        f"Levey-Jennings chart — target {sp:.0f} °C, SD {sd:.3f} °C fixed from baseline "
        "(▼ = off-scale, pinned to edge)",
        fontsize=10,
        loc="left",
    )
    ax.legend(loc="upper left", fontsize=8, ncol=2)

    for a in axes:
        a.grid(alpha=0.25)
    fig.text(0.01, 0.005, f"config: {cfg.source}", fontsize=7, color="#57606a")
    fig.tight_layout(rect=(0, 0.01, 0.97, 0.97))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_comparison(
    cfg: SimulationConfig, tuned: pd.DataFrame, untuned: pd.DataFrame, path: Path
) -> None:
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 6.5), sharex=True, gridspec_kw={"height_ratios": [3, 1.5]}
    )
    t = tuned["time_s"]
    u, g = cfg.untuned, cfg.controller
    fig.suptitle("Step response — untuned (no anti-windup) vs tuned", fontsize=13, weight="bold")
    ax1.plot(t, tuned["setpoint_c"], "--", color="#555", lw=1, label="Setpoint")
    ax1.plot(
        t,
        untuned["temperature_c"],
        color="#d1242f",
        lw=1.6,
        label=f"Untuned  Kp={u.kp}, Ki={u.ki}, Kd={u.kd}, no anti-windup",
    )
    ax1.plot(
        t,
        tuned["temperature_c"],
        color="#1f6feb",
        lw=1.8,
        label=f"Tuned    Kp={g.kp}, Ki={g.ki}, Kd={g.kd}, anti-windup",
    )
    top = max(360.0, float(untuned["temperature_c"].max()) + 10)
    ax1.axhspan(DEGRADATION_C, top, color="#d1242f", alpha=0.07)
    ax1.text(
        0.3,
        DEGRADATION_C + 6,
        f"> {DEGRADATION_C:.0f} °C: rPETG degradation risk",
        color="#d1242f",
        fontsize=9,
    )
    ax1.set_ylabel("°C")
    ax1.legend(loc="lower right", fontsize=9)
    ax2.plot(t, untuned["i_term"], color="#d1242f", lw=1.4, label="Untuned integral term")
    ax2.plot(t, tuned["i_term"], color="#1f6feb", lw=1.4, label="Tuned integral term")
    ax2.set_ylabel("% duty")
    ax2.set_xlabel("Time (s)")
    ax2.legend(loc="upper right", fontsize=9)
    for a in (ax1, ax2):
        a.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def material_sensitivity(cfg: SimulationConfig) -> pd.DataFrame:
    """Nozzle pressure at the setpoint and nominal flow across fibre loading and recycle passes."""
    rows = []
    max_wt = 30.0
    for gen in range(0, 5):
        for wt in np.arange(0.0, max_wt + 0.1, 2.5):
            material = replace(cfg.material, fibre_wt_pct=float(wt), recycle_generation=gen)
            try:
                plant = ExtruderThermalMass(
                    thermal=cfg.thermal, rheology=cfg.rheology, material=material
                )
            except ValueError:
                continue  # beyond the packing limit
            rows.append(
                {
                    "recycle_generation": gen,
                    "fibre_wt_pct": float(wt),
                    "pressure_bar": plant.nozzle_pressure(cfg.scenario.setpoint, 1.0),
                }
            )
    return pd.DataFrame(rows)


def plot_material_sensitivity(cfg: SimulationConfig, table: pd.DataFrame, path: Path) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.6))
    fig.suptitle(
        "Melt model sensitivity — nozzle pressure at nominal flow", fontsize=13, weight="bold"
    )
    m = cfg.material
    for gen, grp in table.groupby("recycle_generation"):
        ax1.plot(
            grp["fibre_wt_pct"],
            grp["pressure_bar"],
            marker=".",
            lw=1.5,
            label=f"recycle generation {gen}",
        )
    ax1.scatter(
        [m.reference_fibre_wt_pct],
        [cfg.rheology.reference_pressure],
        s=80,
        facecolors="none",
        edgecolors="k",
        zorder=5,
        label="calibration point",
    )
    ax1.set_xlabel("Glass fibre loading (wt %)")
    ax1.set_ylabel("Nozzle pressure (bar)")
    ax1.set_title(
        f"At {cfg.scenario.setpoint:.0f} °C (Krieger-Dougherty × Mw^{m.mw_exponent})",
        fontsize=10,
        loc="left",
    )
    ax1.legend(fontsize=8)

    temps = np.linspace(cfg.scenario.setpoint - 30, cfg.scenario.setpoint + 20, 60)
    for wt in (0.0, m.reference_fibre_wt_pct, 25.0):
        material = replace(m, fibre_wt_pct=wt, recycle_generation=m.reference_recycle_generation)
        plant = ExtruderThermalMass(thermal=cfg.thermal, rheology=cfg.rheology, material=material)
        ax2.plot(
            temps, [plant.nozzle_pressure(x, 1.0) for x in temps], lw=1.6, label=f"{wt:g} wt % GF"
        )
    ax2.axvline(cfg.scenario.setpoint, color="#555", ls="--", lw=0.8)
    ax2.set_xlabel("Melt temperature (°C)")
    ax2.set_title(
        f"Arrhenius term, Eₐ = {cfg.rheology.activation_energy / 1000:.0f} kJ/mol",
        fontsize=10,
        loc="left",
    )
    ax2.legend(fontsize=8)
    for a in (ax1, ax2):
        a.grid(alpha=0.25)
    fig.text(
        0.01,
        0.01,
        "Illustrative parameters — see docs/model-assumptions.md A-08 to A-16",
        fontsize=7,
        color="#57606a",
    )
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_layer_window(cfg: SimulationConfig, path: Path) -> dict[str, float]:
    lp, sp, sc = cfg.layer, cfg.scenario.setpoint, cfg.scenario
    sweep = window_sweep(lp, sp)
    amb = np.array([w.ambient_temp for w in sweep])
    tmin = np.array([w.t_min_s for w in sweep]) / 60
    tmax = np.array([w.t_max_s for w in sweep]) / 60
    nominal = layer_window(lp, sp, cfg.thermal.ambient_temp)
    cold = layer_window(lp, sp, sc.disturbed_ambient_temp)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.8))
    fig.suptitle(
        "Inter-layer adhesion window vs factory ambient temperature", fontsize=13, weight="bold"
    )
    ax1.fill_between(amb, tmin, tmax, color="#2da44e", alpha=0.25, label="acceptable layer time")
    ax1.plot(
        amb, tmin, color="#bf8700", lw=1.5, label=f"min: cool below {lp.support_max_temp:.0f} °C"
    )
    ax1.plot(amb, tmax, color="#1f6feb", lw=1.5, label=f"max: stay above {lp.bond_min_temp:.0f} °C")
    for w, name, color in ((nominal, "normal", "#2da44e"), (cold, "door open", "#d1242f")):
        ax1.vlines(w.ambient_temp, w.t_min_s / 60, w.t_max_s / 60, color=color, lw=4)
        mid = (w.t_min_s + w.t_max_s) / 120
        ax1.text(
            w.ambient_temp + 0.8,
            mid,
            f"{name}\n{w.t_min_s / 60:.1f}–{w.t_max_s / 60:.1f} min",
            fontsize=8,
            color=color,
            va="center",
            weight="bold",
        )
    ax1.set_xlabel("Ambient temperature (°C)")
    ax1.set_ylabel("Layer time (min)")
    ax1.set_title(
        f"Bead {lp.bead_width_mm:g}×{lp.bead_height_mm:g} mm, h = {lp.convective_h:g} "
        f"W/m²K, τ = {time_constant(lp) / 60:.1f} min",
        fontsize=10,
        loc="left",
    )
    ax1.legend(fontsize=8, loc="lower right")

    t = np.linspace(0, max(nominal.t_max_s, cold.t_max_s) * 1.3, 200)
    from layer_thermal import bead_temperature

    for amb_c, color, name in (
        (cfg.thermal.ambient_temp, "#2da44e", "normal"),
        (sc.disturbed_ambient_temp, "#d1242f", "door open"),
    ):
        ax2.plot(
            t / 60,
            bead_temperature(lp, sp, amb_c, t),
            color=color,
            lw=1.6,
            label=f"{name} ({amb_c:g} °C ambient)",
        )
    ax2.axhline(lp.support_max_temp, color="#bf8700", ls="--", lw=1)
    ax2.axhline(lp.bond_min_temp, color="#1f6feb", ls="--", lw=1)
    ax2.axhline(lp.glass_transition, color="#57606a", ls=":", lw=1)
    ax2.text(
        t[-1] / 60,
        lp.glass_transition + 2,
        f"Tg {lp.glass_transition:g} °C",
        fontsize=8,
        ha="right",
        color="#57606a",
    )
    ax2.set_xlabel("Time since deposition (min)")
    ax2.set_ylabel("Bead temperature (°C)")
    ax2.set_title(
        "Lumped bead cooling from deposition at the melt setpoint", fontsize=10, loc="left"
    )
    ax2.legend(fontsize=8)
    for a in (ax1, ax2):
        a.grid(alpha=0.25)
    bi = biot_number(lp)
    fig.text(
        0.01,
        0.01,
        f"Illustrative thresholds (A-22 to A-26). Biot number {bi:.2f}: lumped "
        "cooling is approximate above 0.1; conduction to the layer below is ignored.",
        fontsize=7,
        color="#57606a",
    )
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return {
        "tau_min": round(time_constant(lp) / 60, 2),
        "biot_number": round(bi, 3),
        "window_nominal_min": [round(nominal.t_min_s / 60, 2), round(nominal.t_max_s / 60, 2)],
        "window_disturbed_min": [round(cold.t_min_s / 60, 2), round(cold.t_max_s / 60, 2)],
    }


# ---------------------------------------------------------------------------
# Acceptance checks (PRD §7)
# ---------------------------------------------------------------------------


def acceptance_checks(
    cfg: SimulationConfig, run: RunResult, metrics: StepMetrics
) -> dict[str, dict[str, object]]:
    df, qc, sp = run.frame, run.qc, cfg.scenario.setpoint
    assert qc is not None
    checks: dict[str, dict[str, object]] = {}

    duty = df["duty_pct"]
    checks["FR-1"] = {
        "pass": metrics.settling_time_s < 15.0
        and metrics.overshoot_pct < 5.0
        and duty.min() >= cfg.controller.out_min
        and duty.max() <= cfg.controller.out_max,
        "detail": metrics.as_dict(),
    }

    plant = make_plant(cfg)
    p_hot = plant.nozzle_pressure(sp, 1.0)
    p_cold = plant.nozzle_pressure(sp - 15.0, 1.0)
    checks["FR-2"] = {
        "pass": p_cold > p_hot * 1.2,
        "detail": {
            "pressure_at_setpoint_bar": round(p_hot, 2),
            "pressure_15c_colder_bar": round(p_cold, 2),
            "ratio": round(p_cold / p_hot, 3),
            "composition_ratio": round(cfg.material.composition_ratio(), 3),
        },
    }

    dstep = cfg.scenario.disturbance_step
    stable = [v for v in qc.violations if v.severity == "REJECT" and v.step < dstep]
    disturbed = [v for v in qc.violations if v.severity == "REJECT" and v.step >= dstep]
    first = min((v.time_s for v in disturbed), default=None)
    checks["FR-3"] = {
        "pass": not stable and bool(disturbed),
        "detail": {
            "baseline_sd_c": round(qc.baseline_sd or 0, 4),
            "rejects_before_disturbance": len(stable),
            "rejects_after_disturbance": len(disturbed),
            "first_reject_s": first,
            "rules_fired": sorted({v.rule for v in disturbed}),
        },
    }
    return checks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BioPrint-PID extrusion control run")
    parser.add_argument(
        "--config", type=Path, default=None, help="TOML file merged over config/default.toml"
    )
    parser.add_argument("--realtime", action="store_true", help="pace steps to the wall clock")
    parser.add_argument("--seed", type=int, default=None, help="override the sensor-noise seed")
    parser.add_argument(
        "--report-only", action="store_true", help="always exit 0, even if a PRD check fails"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    try:
        cfg = load_config(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        log.error("Configuration problem: %s", exc)
        return 2
    OUTPUT_DIR.mkdir(exist_ok=True)

    log.info("Config: %s", cfg.source)
    log.info(
        "Tuned run: %s, anti-windup on, disturbance at step %d",
        cfg.controller.gains,
        cfg.scenario.disturbance_step,
    )
    tuned = simulate(cfg, seed=args.seed, realtime=args.realtime)
    untuned = simulate(cfg, cfg.untuned, anti_windup=False, seed=args.seed, with_qc=False)

    metrics = pre_disturbance_metrics(cfg, tuned)
    untuned_metrics = pre_disturbance_metrics(cfg, untuned)

    files = {
        "dashboard": OUTPUT_DIR / "pid_step_response.png",
        "comparison": OUTPUT_DIR / "pid_tuning_comparison.png",
        "material": OUTPUT_DIR / "material_sensitivity.png",
        "layer": OUTPUT_DIR / "layer_time_window.png",
    }
    plot_dashboard(cfg, tuned, metrics, files["dashboard"])
    plot_comparison(cfg, tuned.frame, untuned.frame, files["comparison"])
    sensitivity = material_sensitivity(cfg)
    sensitivity.to_csv(OUTPUT_DIR / "material_sensitivity.csv", index=False)
    plot_material_sensitivity(cfg, sensitivity, files["material"])
    layer = plot_layer_window(cfg, files["layer"])

    tuned.frame.to_csv(OUTPUT_DIR / "pid_run.csv", index=False)
    assert tuned.qc is not None
    pd.DataFrame([v.__dict__ for v in tuned.qc.violations]).to_csv(
        OUTPUT_DIR / "qc_violations.csv", index=False
    )

    checks = acceptance_checks(cfg, tuned, metrics)
    checks["FR-4"] = {
        "pass": all(f.exists() for f in files.values()),
        "detail": {"files": [f.name for f in files.values()]},
    }
    log.info("Untuned comparison: %s", untuned_metrics.as_dict())
    for fr, result in checks.items():
        status = "PASS" if result["pass"] else "FAIL"
        log.info("%s %s  %s", status, fr, json.dumps(result["detail"]))
    log.info("Layer-time window: %s", json.dumps(layer))

    report = {"config": cfg.source, "checks": checks, "layer_window": layer}
    (OUTPUT_DIR / "acceptance.json").write_text(json.dumps(report, indent=2, default=str))

    passed = all(r["pass"] for r in checks.values())
    return 0 if passed or args.report_only else 1


if __name__ == "__main__":
    sys.exit(main())
