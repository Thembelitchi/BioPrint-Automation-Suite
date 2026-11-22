"""BioPrint-PID run: tuned PID on the extruder model, monitored by Westgard QC.

Run from the project folder:

    python src/app.py               # simulated time, finishes in about a second
    python src/app.py --realtime    # paces each 0.1 s step to the wall clock (30 s)

Scenario (PRD §4.5):
    * 30 s run, 300 steps at dt = 0.1 s, setpoint 230 °C, starting from ambient.
    * The auger starts once the barrel reaches melt temperature (cold-extrusion interlock).
    * The gantry slows at corners every 4 s, so auger flow and nozzle pressure vary.
    * At step 150 a severe ambient disturbance hits: a roller door opens, ambient
      drops from 25 °C to 5 °C and convective losses rise almost fourfold.
    * Westgard QC fixes its SD from a baseline window (steps 80–119) and evaluates
      every sample after it.

Outputs (in ``output/``):
    pid_step_response.png    tracking, controller effort, nozzle pressure, Levey-Jennings
    pid_tuning_comparison.png  untuned (no anti-windup) vs tuned step response
    pid_run.csv              time series of every signal
    qc_violations.csv        Westgard events

The exit code is 0 only if every PRD acceptance check passes.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless rendering for CI

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from extruder_physics import ExtruderThermalMass, gantry_speed_profile  # noqa: E402
from metrics import StepMetrics, step_metrics  # noqa: E402
from pid_controller import DiscretePID, PIDGains  # noqa: E402
from westgard_validator import BiomedicalQAMonitor  # noqa: E402

log = logging.getLogger("bioprint_pid")

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

DT = 0.1
STEPS = 300
SETPOINT = 230.0
DISTURBANCE_STEP = 150
DISTURBED_AMBIENT_C = 5.0
DISTURBED_LOSS_COEFF = 30.0  # W/K, up from 8 W/K
SENSOR_NOISE_SD = 0.15  # °C, thermocouple noise
SENSOR_LAG_S = 0.5  # thermocouple time constant
BASELINE_START = 80
BASELINE_LENGTH = 40

DEGRADATION_C = 260.0  # illustrative upper processing limit for the comparison chart

TUNED = PIDGains(kp=8.0, ki=2.5, kd=2.0)
UNTUNED = PIDGains(kp=2.0, ki=0.6, kd=0.0)


@dataclass
class RunResult:
    frame: pd.DataFrame
    qc: BiomedicalQAMonitor | None


def simulate(
    gains: PIDGains,
    anti_windup: bool = True,
    seed: int = 42,
    with_disturbance: bool = True,
    with_qc: bool = True,
    realtime: bool = False,
) -> RunResult:
    """Run the closed loop for STEPS scans and return every signal."""
    rng = np.random.default_rng(seed)
    plant = ExtruderThermalMass(melt_setpoint=SETPOINT)
    pid = DiscretePID(gains, dt=DT, anti_windup=anti_windup)
    qc = (
        BiomedicalQAMonitor(
            target=SETPOINT, baseline_start=BASELINE_START, baseline_length=BASELINE_LENGTH, dt=DT
        )
        if with_qc
        else None
    )
    gantry = gantry_speed_profile(STEPS, DT)

    measured = plant.state.temperature
    rows: list[dict[str, float | int | str]] = []
    next_tick = time.monotonic()

    for k in range(STEPS):
        if with_disturbance and k == DISTURBANCE_STEP:
            plant.apply_ambient_disturbance(DISTURBED_AMBIENT_C, DISTURBED_LOSS_COEFF)

        sensor = measured + rng.normal(0.0, SENSOR_NOISE_SD)
        duty = pid.update(SETPOINT, sensor)
        state = plant.step(duty, gantry[k], DT)
        # First-order thermocouple lag between the true melt and the reading.
        measured += (state.temperature - measured) * DT / SENSOR_LAG_S

        flags = ""
        if qc is not None:
            flags = ",".join(v.rule for v in qc.evaluate(k, sensor))

        p, i, d = pid.last_terms
        rows.append(
            {
                "step": k,
                "time_s": round(k * DT, 3),
                "setpoint_c": SETPOINT,
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
            next_tick += DT
            time.sleep(max(0.0, next_tick - time.monotonic()))

    return RunResult(pd.DataFrame(rows), qc)


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------


def plot_dashboard(run: RunResult, metrics: StepMetrics, path: Path) -> None:
    df, qc = run.frame, run.qc
    assert qc is not None and qc.baseline_sd is not None
    t = df["time_s"].to_numpy()
    t_dist = DISTURBANCE_STEP * DT

    fig, axes = plt.subplots(
        4, 1, figsize=(11, 12), sharex=True, gridspec_kw={"height_ratios": [3, 1.5, 1.5, 3]}
    )
    fig.suptitle("BioPrint-PID — tuned loop with Westgard QC", fontsize=14, weight="bold")

    ax = axes[0]
    ax.plot(t, df["setpoint_c"], "--", color="#555", lw=1.2, label="Setpoint 230 °C")
    ax.plot(t, df["temperature_c"], color="#1f6feb", lw=1.8, label="Melt temperature")
    ax.axvline(t_dist, color="#d1242f", ls=":", lw=1.2)
    ax.text(t_dist + 0.2, 60, "ambient\ndisturbance", color="#d1242f", fontsize=9)
    ax.set_ylabel("°C")
    ax.set_title(
        f"Tracking — overshoot {metrics.overshoot_pct:.2f} %, "
        f"settled (±1 °C) in {metrics.settling_time_s:.1f} s",
        fontsize=10,
        loc="left",
    )
    ax.legend(loc="lower right", fontsize=9)
    inset = ax.inset_axes([0.42, 0.12, 0.33, 0.45])
    window = (t >= 10) & (t <= 30)
    inset.plot(t[window], df["temperature_c"][window], color="#1f6feb", lw=1.2)
    inset.axhline(SETPOINT, color="#555", ls="--", lw=0.8)
    inset.axvline(t_dist, color="#d1242f", ls=":", lw=0.8)
    inset.set_title("10–30 s zoom", fontsize=8)
    inset.tick_params(labelsize=7)

    ax = axes[1]
    ax.plot(t, df["duty_pct"], color="#8250df", lw=1.4)
    ax.axvline(t_dist, color="#d1242f", ls=":", lw=1.2)
    ax.set_ylabel("Heater %")
    ax.set_ylim(-5, 105)
    ax.set_title("Controller effort (clamped 0–100 %)", fontsize=10, loc="left")

    ax = axes[2]
    ax.plot(t, df["pressure_bar"], color="#bf8700", lw=1.4)
    ax.axvline(t_dist, color="#d1242f", ls=":", lw=1.2)
    ax.set_ylabel("bar")
    ax.set_title(
        "Nozzle pressure (power-law melt; dips at corners, rises when melt cools)",
        fontsize=10,
        loc="left",
    )

    ax = axes[3]
    sd = qc.baseline_sd
    for k, color in ((1, "#2da44e"), (2, "#bf8700"), (3, "#d1242f")):
        for sign in (1, -1):
            ax.axhline(SETPOINT + sign * k * sd, color=color, lw=0.9, ls="-" if k == 3 else "--")
        ax.text(t[-1] + 0.2, SETPOINT + k * sd, f"+{k}SD", color=color, fontsize=8, va="center")
        ax.text(t[-1] + 0.2, SETPOINT - k * sd, f"−{k}SD", color=color, fontsize=8, va="center")
    ax.axhline(SETPOINT, color="#555", lw=0.9)
    ax.axvspan(
        BASELINE_START * DT,
        (BASELINE_START + BASELINE_LENGTH) * DT,
        color="#2da44e",
        alpha=0.08,
        label="Baseline window",
    )
    lo, hi = SETPOINT - 5 * sd, SETPOINT + 5 * sd
    edge_lo, edge_hi = lo + 0.15 * sd, hi - 0.15 * sd

    def on_chart(values: np.ndarray) -> np.ndarray:
        """Pin off-scale points to the chart edge so excursions stay visible."""
        return np.clip(values, edge_lo, edge_hi)

    armed = (df["step"] >= BASELINE_START).to_numpy()
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
        f"Levey-Jennings chart — target 230 °C, SD {sd:.3f} °C fixed from baseline "
        "(▼ = off-scale, pinned to edge)",
        fontsize=10,
        loc="left",
    )
    ax.legend(loc="upper left", fontsize=8, ncol=2)

    for a in axes:
        a.grid(alpha=0.25)
    fig.tight_layout(rect=(0, 0, 0.97, 0.97))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_comparison(tuned: pd.DataFrame, untuned: pd.DataFrame, path: Path) -> None:
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 6.5), sharex=True, gridspec_kw={"height_ratios": [3, 1.5]}
    )
    t = tuned["time_s"]
    fig.suptitle("Step response — untuned (no anti-windup) vs tuned", fontsize=13, weight="bold")
    ax1.plot(t, tuned["setpoint_c"], "--", color="#555", lw=1, label="Setpoint")
    ax1.plot(
        t,
        untuned["temperature_c"],
        color="#d1242f",
        lw=1.6,
        label=f"Untuned  Kp={UNTUNED.kp}, Ki={UNTUNED.ki}, Kd={UNTUNED.kd}, no anti-windup",
    )
    ax1.plot(
        t,
        tuned["temperature_c"],
        color="#1f6feb",
        lw=1.8,
        label=f"Tuned    Kp={TUNED.kp}, Ki={TUNED.ki}, Kd={TUNED.kd}, anti-windup",
    )
    ax1.axhspan(DEGRADATION_C, 360, color="#d1242f", alpha=0.07)
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


# ---------------------------------------------------------------------------
# Acceptance checks (PRD §7)
# ---------------------------------------------------------------------------


def acceptance_checks(run: RunResult, metrics: StepMetrics) -> dict[str, dict[str, object]]:
    df, qc = run.frame, run.qc
    assert qc is not None
    checks: dict[str, dict[str, object]] = {}

    duty = df["duty_pct"]
    checks["FR-1"] = {
        "pass": metrics.settling_time_s < 15.0
        and metrics.overshoot_pct < 5.0
        and duty.min() >= 0.0
        and duty.max() <= 100.0,
        "detail": metrics.as_dict(),
    }

    plant = ExtruderThermalMass()
    p_hot = plant.nozzle_pressure(SETPOINT, 1.0)
    p_cold = plant.nozzle_pressure(SETPOINT - 15.0, 1.0)
    checks["FR-2"] = {
        "pass": p_cold > p_hot * 1.2,
        "detail": {
            "pressure_230c_bar": round(p_hot, 2),
            "pressure_215c_bar": round(p_cold, 2),
            "ratio": round(p_cold / p_hot, 3),
        },
    }

    stable = [v for v in qc.violations if v.severity == "REJECT" and v.step < DISTURBANCE_STEP]
    disturbed = [v for v in qc.violations if v.severity == "REJECT" and v.step >= DISTURBANCE_STEP]
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
    parser.add_argument("--realtime", action="store_true", help="pace steps to the wall clock")
    parser.add_argument("--seed", type=int, default=42, help="sensor noise seed")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    OUTPUT_DIR.mkdir(exist_ok=True)

    log.info("Tuned run: %s, anti-windup on, disturbance at step %d", TUNED, DISTURBANCE_STEP)
    tuned = simulate(TUNED, seed=args.seed, realtime=args.realtime)
    untuned = simulate(UNTUNED, anti_windup=False, seed=args.seed, with_qc=False)

    pre = tuned.frame["temperature_c"].to_numpy()[:DISTURBANCE_STEP]
    metrics = step_metrics(pre, SETPOINT, initial=25.0, dt=DT)
    untuned_metrics = step_metrics(
        untuned.frame["temperature_c"].to_numpy()[:DISTURBANCE_STEP], SETPOINT, 25.0, DT
    )

    dash = OUTPUT_DIR / "pid_step_response.png"
    comp = OUTPUT_DIR / "pid_tuning_comparison.png"
    plot_dashboard(tuned, metrics, dash)
    plot_comparison(tuned.frame, untuned.frame, comp)

    tuned.frame.to_csv(OUTPUT_DIR / "pid_run.csv", index=False)
    assert tuned.qc is not None
    pd.DataFrame([v.__dict__ for v in tuned.qc.violations]).to_csv(
        OUTPUT_DIR / "qc_violations.csv", index=False
    )

    checks = acceptance_checks(tuned, metrics)
    checks["FR-4"] = {
        "pass": dash.exists() and comp.exists(),
        "detail": {"files": [dash.name, comp.name]},
    }
    log.info("Untuned comparison: %s", untuned_metrics.as_dict())
    for fr, result in checks.items():
        log.info(
            "%s %s  %s", "PASS" if result["pass"] else "FAIL", fr, json.dumps(result["detail"])
        )
    (OUTPUT_DIR / "acceptance.json").write_text(json.dumps(checks, indent=2, default=str))

    return 0 if all(r["pass"] for r in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
