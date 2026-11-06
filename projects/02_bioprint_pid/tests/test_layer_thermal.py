"""Bead cooling and inter-layer adhesion window."""

import math

import numpy as np
import pytest
from layer_thermal import (
    LayerParams,
    bead_temperature,
    biot_number,
    layer_window,
    time_constant,
    window_sweep,
)

P = LayerParams()


def test_time_constant_formula() -> None:
    w, h = 0.020, 0.005
    expected = 1400.0 * 1800.0 * (w * h) / (10.0 * (w + 2 * h))
    assert time_constant(P) == pytest.approx(expected)


def test_window_is_ordered_and_positive() -> None:
    win = layer_window(P, 230.0, 25.0)
    assert 0 < win.t_min_s < win.t_max_s
    assert win.width_s > 0


def test_bead_hits_thresholds_at_window_edges() -> None:
    win = layer_window(P, 230.0, 25.0)
    t_lo, t_hi = bead_temperature(P, 230.0, 25.0, np.array([win.t_min_s, win.t_max_s]))
    assert t_lo == pytest.approx(P.support_max_temp)
    assert t_hi == pytest.approx(P.bond_min_temp)


def test_colder_factory_shortens_the_window() -> None:
    warm = layer_window(P, 230.0, 25.0)
    cold = layer_window(P, 230.0, 5.0)
    assert cold.t_max_s < warm.t_max_s
    assert cold.width_s < warm.width_s


def test_fans_shorten_layer_time() -> None:
    fans = LayerParams(convective_h=25.0)
    assert layer_window(fans, 230.0, 25.0).t_max_s < layer_window(P, 230.0, 25.0).t_max_s


def test_ambient_above_bond_temp_never_closes() -> None:
    assert math.isinf(layer_window(P, 230.0, 95.0).t_max_s)


def test_biot_number_reported() -> None:
    assert 0.05 < biot_number(P) < 0.5


def test_sweep_covers_range() -> None:
    sweep = window_sweep(P, 230.0, n=5)
    assert [w.ambient_temp for w in sweep] == [0.0, 10.0, 20.0, 30.0, 40.0]
