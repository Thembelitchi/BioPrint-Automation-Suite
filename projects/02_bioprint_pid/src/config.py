"""Configuration loading for BioPrint-PID.

Every physical and tuning parameter lives in ``config/default.toml``. A reviewer
can supply their own TOML file containing only the values they want to change,
and it is merged over the defaults::

    python src/app.py --config my_machine.toml

Unknown keys and out-of-range values fail early with a clear message, so a typo
can't silently fall back to a default.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

from extruder_physics import MaterialParams, RheologyParams, ThermalParams
from layer_thermal import LayerParams
from pid_controller import PIDGains

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "default.toml"


class ConfigError(ValueError):
    """Raised for unknown keys or physically invalid values."""


@dataclass(frozen=True)
class ScenarioConfig:
    dt: float
    steps: int
    setpoint: float
    disturbance_step: int
    disturbed_ambient_temp: float
    disturbed_loss_coeff: float
    seed: int


@dataclass(frozen=True)
class ControllerConfig:
    kp: float
    ki: float
    kd: float
    derivative_filter_n: float
    out_min: float
    out_max: float

    @property
    def gains(self) -> PIDGains:
        return PIDGains(self.kp, self.ki, self.kd)


@dataclass(frozen=True)
class GantryConfig:
    corner_every_s: float
    corner_duration_s: float
    corner_speed: float


@dataclass(frozen=True)
class SensorConfig:
    noise_sd: float
    lag_s: float


@dataclass(frozen=True)
class InterlockConfig:
    band: float


@dataclass(frozen=True)
class QCConfig:
    baseline_start: int
    baseline_length: int
    rolling_window: int
    min_sd: float


@dataclass(frozen=True)
class SimulationConfig:
    """All parameters of one BioPrint-PID run."""

    scenario: ScenarioConfig
    controller: ControllerConfig
    untuned: PIDGains
    thermal: ThermalParams
    rheology: RheologyParams
    material: MaterialParams
    gantry: GantryConfig
    sensor: SensorConfig
    interlock: InterlockConfig
    qc: QCConfig
    layer: LayerParams
    source: str  # which file(s) the values came from


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key not in merged:
            raise ConfigError(f"unknown section or key '{key}'")
        if isinstance(value, dict) and isinstance(merged[key], dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build(cls: type, section: str, values: dict[str, Any]) -> Any:
    allowed = {f.name for f in fields(cls)}
    unknown = set(values) - allowed
    if unknown:
        raise ConfigError(f"[{section}] unknown key(s): {', '.join(sorted(unknown))}")
    missing = allowed - set(values)
    if missing:
        raise ConfigError(f"[{section}] missing key(s): {', '.join(sorted(missing))}")
    return cls(**values)


def _validate(cfg: SimulationConfig) -> None:
    s, c, q, m, th = cfg.scenario, cfg.controller, cfg.qc, cfg.material, cfg.thermal
    checks = [
        (s.dt > 0, "scenario.dt must be > 0"),
        (0 < s.disturbance_step <= s.steps, "scenario.disturbance_step must be within the run"),
        (c.out_min < c.out_max, "controller.out_min must be below out_max"),
        (th.heat_capacity > 0 and th.heater_power_max > 0, "thermal capacities must be > 0"),
        (0 < cfg.rheology.flow_index <= 1.0, "rheology.flow_index must be in (0, 1]"),
        (
            0 <= m.fibre_wt_pct < 100 and 0 <= m.reference_fibre_wt_pct < 100,
            "material fibre loadings must be in [0, 100) wt %",
        ),
        (0 < m.mw_retention_per_cycle <= 1.0, "material.mw_retention_per_cycle must be in (0, 1]"),
        (m.recycle_generation >= 0, "material.recycle_generation must be >= 0"),
        (
            q.baseline_start + q.baseline_length <= s.disturbance_step,
            "qc baseline window must end before the disturbance",
        ),
        (
            cfg.layer.bond_min_temp < cfg.layer.support_max_temp < s.setpoint,
            "layer: need bond_min_temp < support_max_temp < setpoint",
        ),
    ]
    for ok, message in checks:
        if not ok:
            raise ConfigError(message)
    # Fibre loading must stay below the packing limit (raises ConfigError via ValueError).
    try:
        m.volume_fraction(m.fibre_wt_pct)
        m.volume_fraction(m.reference_fibre_wt_pct)
        m.fibre_viscosity_factor(m.fibre_wt_pct)
    except ValueError as exc:
        raise ConfigError(f"material: {exc}") from exc


def load_config(override_path: str | Path | None = None) -> SimulationConfig:
    """Load the defaults, merge an optional override file, validate and return."""
    with DEFAULT_CONFIG_PATH.open("rb") as fh:
        raw = tomllib.load(fh)
    source = DEFAULT_CONFIG_PATH.name
    if override_path is not None:
        path = Path(override_path)
        with path.open("rb") as fh:
            raw = _deep_merge(raw, tomllib.load(fh))
        source = f"{DEFAULT_CONFIG_PATH.name} + {path.name}"

    layer = dict(raw["layer"])
    layer["ambient_sweep"] = tuple(layer["ambient_sweep"])

    cfg = SimulationConfig(
        scenario=_build(ScenarioConfig, "scenario", raw["scenario"]),
        controller=_build(ControllerConfig, "controller", raw["controller"]),
        untuned=_build(PIDGains, "controller_untuned", raw["controller_untuned"]),
        thermal=_build(ThermalParams, "thermal", raw["thermal"]),
        rheology=_build(RheologyParams, "rheology", raw["rheology"]),
        material=_build(MaterialParams, "material", raw["material"]),
        gantry=_build(GantryConfig, "gantry", raw["gantry"]),
        sensor=_build(SensorConfig, "sensor", raw["sensor"]),
        interlock=_build(InterlockConfig, "interlock", raw["interlock"]),
        qc=_build(QCConfig, "qc", raw["qc"]),
        layer=_build(LayerParams, "layer", layer),
        source=source,
    )
    _validate(cfg)
    return cfg
