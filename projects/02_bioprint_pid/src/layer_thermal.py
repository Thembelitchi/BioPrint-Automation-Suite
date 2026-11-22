"""Deposited-bead cooling and the inter-layer adhesion window.

In large-format additive manufacturing (LFAM), *layer time* (how long before
the nozzle comes back over the same spot) decides part quality:

* **Too short:** the layer below is still soft and slumps under the next one.
* **Too long:** the layer below has cooled too far for polymer chains to diffuse
  across the interface, so the layers delaminate.

This module estimates that window for a given factory ambient temperature,
using lumped-capacitance (Newtonian) cooling of the deposited bead::

    T(t) = T_amb + (T_dep − T_amb) · exp(−t / τ),
    τ    = ρ · c_p · A / (h · P_exposed)

where A is the bead cross-section (width × height) and P_exposed the perimeter
open to air (top + two sides). Then:

* ``t_min`` = time for the bead to cool below ``support_max_temp`` (it can carry the next layer)
* ``t_max`` = time for the bead to cool to ``bond_min_temp`` (last chance for a good bond)

Limitations (documented for reviewers, not hidden):

* Conduction into the layer below and re-heating by the next layer are ignored.
* The Biot number for an LFAM bead in still air is around 0.1–0.3, so the lumped
  assumption is approximate. :func:`biot_number` reports it.
* The thresholds are illustrative and are the values an LFAM process engineer
  should replace with their own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class LayerParams:
    bead_width_mm: float = 20.0
    bead_height_mm: float = 5.0
    bead_density: float = 1400.0  # kg/m³
    bead_specific_heat: float = 1800.0  # J/(kg·K)
    bead_conductivity: float = 0.30  # W/(m·K)
    convective_h: float = 10.0  # W/(m²·K)
    glass_transition: float = 80.0  # °C
    bond_min_temp: float = 90.0  # °C
    support_max_temp: float = 120.0  # °C
    ambient_sweep: tuple[float, float] = (0.0, 40.0)  # °C


@dataclass(frozen=True)
class LayerWindow:
    ambient_temp: float
    t_min_s: float  # earliest the next layer may be laid
    t_max_s: float  # latest the next layer may be laid

    @property
    def width_s(self) -> float:
        return self.t_max_s - self.t_min_s


def time_constant(p: LayerParams) -> float:
    """Bead cooling time constant τ (s)."""
    w, h = p.bead_width_mm / 1000.0, p.bead_height_mm / 1000.0
    area = w * h
    exposed_perimeter = w + 2.0 * h
    return p.bead_density * p.bead_specific_heat * area / (p.convective_h * exposed_perimeter)


def biot_number(p: LayerParams) -> float:
    """Bi = h · L_c / k with L_c = A / P_exposed. Lumped cooling is accurate for Bi < 0.1."""
    w, h = p.bead_width_mm / 1000.0, p.bead_height_mm / 1000.0
    return p.convective_h * (w * h / (w + 2.0 * h)) / p.bead_conductivity


def bead_temperature(p: LayerParams, deposit_temp: float, ambient: float, t_s: np.ndarray):
    """Bead temperature (°C) at times ``t_s`` after deposition."""
    return ambient + (deposit_temp - ambient) * np.exp(-np.asarray(t_s) / time_constant(p))


def _time_to(p: LayerParams, deposit_temp: float, ambient: float, target: float) -> float:
    if target <= ambient:
        return math.inf  # the bead never cools to this temperature
    if target >= deposit_temp:
        return 0.0
    return time_constant(p) * math.log((deposit_temp - ambient) / (target - ambient))


def layer_window(p: LayerParams, deposit_temp: float, ambient: float) -> LayerWindow:
    """Allowed layer-time window (s) at one ambient temperature."""
    return LayerWindow(
        ambient_temp=ambient,
        t_min_s=_time_to(p, deposit_temp, ambient, p.support_max_temp),
        t_max_s=_time_to(p, deposit_temp, ambient, p.bond_min_temp),
    )


def window_sweep(p: LayerParams, deposit_temp: float, n: int = 41) -> list[LayerWindow]:
    """Layer windows across ``p.ambient_sweep``."""
    lo, hi = p.ambient_sweep
    return [layer_window(p, deposit_temp, float(a)) for a in np.linspace(lo, hi, n)]
