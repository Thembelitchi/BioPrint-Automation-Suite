"""Configuration loading, merging and validation."""

import pytest
from config import ConfigError, load_config


def write(tmp_path, text: str):
    path = tmp_path / "override.toml"
    path.write_text(text)
    return path


def test_defaults_load() -> None:
    cfg = load_config()
    assert cfg.scenario.setpoint == 230.0
    assert cfg.controller.gains.kp == 8.0
    assert cfg.material.composition_ratio() == pytest.approx(1.0)
    assert cfg.source == "default.toml"


def test_override_merges_only_given_keys(tmp_path) -> None:
    cfg = load_config(write(tmp_path, "[thermal]\nambient_temp = 12.0\n"))
    assert cfg.thermal.ambient_temp == 12.0
    assert cfg.thermal.heat_capacity == 400.0  # untouched default
    assert "override.toml" in cfg.source


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[thermal]\nheat_capcity = 1.0\n", "heat_capcity"),
        ("[nonsense]\nx = 1\n", "nonsense"),
        ("[rheology]\nflow_index = 1.5\n", "flow_index"),
        ("[material]\nfibre_wt_pct = 70.0\n", "packing limit"),
        ("[material]\nmw_retention_per_cycle = 0.0\n", "mw_retention"),
        ("[qc]\nbaseline_start = 140\n", "baseline"),
        ("[layer]\nbond_min_temp = 130.0\n", "bond_min_temp"),
        ("[scenario]\ndt = 0.0\n", "dt"),
    ],
)
def test_invalid_overrides_fail_clearly(tmp_path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))
