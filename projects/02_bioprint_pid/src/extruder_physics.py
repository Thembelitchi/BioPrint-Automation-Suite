"""Extruder barrel thermal model and polymer melt rheology (BioPrint-PID FR-2).

Thermal model (lumped capacitance)::

    C · dT/dt = P_max · u − h · (T − T_amb) − ṁ · c_p · (T − T_feed)

where u is the heater duty (0–1), h is the heat loss coefficient to ambient, and
the last term is the heat taken up by cold pellets entering the barrel. It is
integrated with explicit Euler at the controller scan time.

Melt rheology (non-Newtonian, power-law, shear-thinning)::

    η(T, γ̇) = K(T) · γ̇^(n − 1),          n ≈ 0.4
    K(T)    = K_ref · exp(E_a / R · (1/T − 1/T_ref))     (Arrhenius, T in kelvin)

For a fixed die geometry, the pressure needed to push flow Q through the nozzle
scales as ``ΔP ∝ K(T) · Q^n``. The model is calibrated so that 230 °C at the
nominal flow gives 45 bar. Colder melt is stiffer, so pressure rises. Because
n < 1, doubling the flow less than doubles the pressure: that is shear thinning.

Composition effects (reviewer-adjustable, see ``config/default.toml``) scale K:

* **Fibre loading** uses the Krieger-Dougherty relation for suspensions,
  ``η_r = (1 − φ/φ_m)^(−[η]·φ_m)``, where the weight fraction is converted to a
  volume fraction φ through the fibre and matrix densities.
* **Recycling** uses chain scission: each reprocessing pass keeps a fraction r of
  the weight-average molecular weight, and entangled melts follow ``η₀ ∝ Mw^3.4``,
  so K scales as ``r^(3.4·Δgeneration)``.

Both factors are applied *relative to the calibration batch*. With the default
composition the model is unchanged: 45 bar at 230 °C and nominal flow.

A **cold-extrusion interlock** keeps the auger off until the barrel first
reaches its melt setpoint, the same protection a real PLC applies so that solid
polymer is never forced through the screw.

All parameters are illustrative values for a simulated nozzle zone, not data
for a specific machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

R_GAS = 8.314  # J/(mol·K)
KELVIN = 273.15


@dataclass
class ThermalParams:
    """Thermal parameters of the simulated nozzle/barrel zone."""

    heat_capacity: float = 400.0  # C, J/K
    heater_power_max: float = 15_000.0  # P_max, W
    loss_coeff: float = 8.0  # h, W/K
    ambient_temp: float = 25.0  # T_amb, °C
    feed_temp: float = 25.0  # T_feed, °C
    specific_heat: float = 1_500.0  # c_p of the polymer, J/(kg·K)
    nominal_mass_flow: float = 0.005  # ṁ at full gantry speed, kg/s


@dataclass
class RheologyParams:
    """Power-law melt parameters, calibrated to 45 bar at 230 °C and nominal flow."""

    flow_index: float = 0.4  # n (< 1 means shear-thinning)
    activation_energy: float = 50_000.0  # E_a, J/mol
    reference_temp: float = 230.0  # T_ref, °C
    reference_pressure: float = 45.0  # bar at T_ref and nominal flow
    max_pressure: float = 250.0  # physical clamp (relief valve), bar


@dataclass
class MaterialParams:
    """Composition of the batch being printed, relative to the calibration batch."""

    fibre_wt_pct: float = 15.0
    reference_fibre_wt_pct: float = 15.0
    fibre_density: float = 2540.0  # kg/m³, E-glass
    matrix_density: float = 1270.0  # kg/m³, PETG
    fibre_intrinsic_viscosity: float = 5.0  # Krieger-Dougherty [η]
    max_packing_fraction: float = 0.35  # Krieger-Dougherty φ_m
    recycle_generation: int = 1
    reference_recycle_generation: int = 1
    mw_retention_per_cycle: float = 0.95
    mw_exponent: float = 3.4

    def volume_fraction(self, wt_pct: float) -> float:
        """Convert a fibre weight percentage to a volume fraction φ."""
        if not 0.0 <= wt_pct < 100.0:
            raise ValueError(f"fibre loading {wt_pct} wt % outside [0, 100)")
        w = wt_pct / 100.0
        v_fibre = w / self.fibre_density
        v_matrix = (1.0 - w) / self.matrix_density
        return v_fibre / (v_fibre + v_matrix)

    def fibre_viscosity_factor(self, wt_pct: float) -> float:
        """Krieger-Dougherty relative viscosity η_r for a fibre loading."""
        phi = self.volume_fraction(wt_pct)
        if phi >= self.max_packing_fraction:
            raise ValueError(
                f"fibre volume fraction {phi:.3f} at {wt_pct} wt % reaches the packing "
                f"limit φ_m = {self.max_packing_fraction}; the melt would not flow"
            )
        exponent = -self.fibre_intrinsic_viscosity * self.max_packing_fraction
        return float((1.0 - phi / self.max_packing_fraction) ** exponent)

    def recycle_factor(self, generation: int) -> float:
        """K scaling from molecular-weight loss after ``generation`` reprocessing passes."""
        return float(self.mw_retention_per_cycle ** (self.mw_exponent * generation))

    def composition_ratio(self) -> float:
        """K(batch) / K(calibration batch) at the same temperature."""
        fibre = self.fibre_viscosity_factor(self.fibre_wt_pct) / self.fibre_viscosity_factor(
            self.reference_fibre_wt_pct
        )
        recycle = self.recycle_factor(self.recycle_generation) / self.recycle_factor(
            self.reference_recycle_generation
        )
        return fibre * recycle


@dataclass
class ExtruderState:
    temperature: float
    pressure: float = 0.0
    flow_fraction: float = 0.0  # Q / Q_nominal
    auger_enabled: bool = False


@dataclass
class ExtruderThermalMass:
    """Simulated extruder: barrel temperature, auger flow and nozzle pressure.

    Call :meth:`step` once per controller scan with the heater duty (%) and the
    gantry speed (as a fraction of nominal). The auger flow follows gantry speed
    so that bead width stays constant at corners.
    """

    thermal: ThermalParams = field(default_factory=ThermalParams)
    rheology: RheologyParams = field(default_factory=RheologyParams)
    material: MaterialParams = field(default_factory=MaterialParams)
    melt_setpoint: float = 230.0
    interlock_band: float = 2.0  # auger enabled once T ≥ setpoint − band
    state: ExtruderState = field(init=False)

    def __post_init__(self) -> None:
        # Private copy: disturbances mutate thermal parameters, and a shared
        # config object must never leak one run's disturbance into the next.
        self.thermal = replace(self.thermal)
        self._composition = self.material.composition_ratio()
        self.state = ExtruderState(temperature=self.thermal.ambient_temp)

    # -- rheology --------------------------------------------------------------

    def consistency_ratio(self, temp_c: float) -> float:
        """K(T, batch) / K(T_ref, calibration batch).

        Combines the Arrhenius temperature term with the fibre and recycling factors.
        """
        t = np.float64(temp_c) + KELVIN
        t_ref = self.rheology.reference_temp + KELVIN
        exponent = self.rheology.activation_energy / R_GAS * (1.0 / t - 1.0 / t_ref)
        return float(np.exp(np.clip(exponent, -50.0, 50.0))) * self._composition

    def nozzle_pressure(self, temp_c: float, flow_fraction: float) -> float:
        """Nozzle pressure (bar) for a melt temperature and flow fraction Q/Q_nom."""
        if flow_fraction <= 0.0:
            return 0.0  # no flow, no pressure drop (and no 0**n edge cases)
        r = self.rheology
        p = r.reference_pressure * self.consistency_ratio(temp_c) * flow_fraction**r.flow_index
        return float(np.clip(p, 0.0, r.max_pressure))

    # -- dynamics ----------------------------------------------------------------

    def step(self, heater_duty_pct: float, gantry_speed: float, dt: float) -> ExtruderState:
        """Advance the model by ``dt`` seconds and return the new state."""
        th = self.thermal
        s = self.state
        u = float(np.clip(heater_duty_pct, 0.0, 100.0)) / 100.0

        # Cold-extrusion interlock: latch the auger on once melt temperature is reached.
        if not s.auger_enabled and s.temperature >= self.melt_setpoint - self.interlock_band:
            s.auger_enabled = True

        flow = float(np.clip(gantry_speed, 0.0, 1.5)) if s.auger_enabled else 0.0
        mass_flow = th.nominal_mass_flow * flow

        heat_in = th.heater_power_max * u
        ambient_loss = th.loss_coeff * (s.temperature - th.ambient_temp)
        feed_load = mass_flow * th.specific_heat * (s.temperature - th.feed_temp)

        d_temp = (heat_in - ambient_loss - feed_load) / th.heat_capacity
        s.temperature = float(np.clip(s.temperature + d_temp * dt, -40.0, 400.0))
        s.flow_fraction = flow
        s.pressure = self.nozzle_pressure(s.temperature, flow)
        return s

    def apply_ambient_disturbance(self, ambient_temp: float, loss_coeff: float) -> None:
        """Change the surroundings, e.g. a roller door opening onto a cold, windy yard."""
        self.thermal.ambient_temp = ambient_temp
        self.thermal.loss_coeff = loss_coeff


def gantry_speed_profile(
    n_steps: int,
    dt: float,
    corner_every_s: float = 4.0,
    corner_duration_s: float = 0.8,
    corner_speed: float = 0.4,
) -> np.ndarray:
    """Gantry speed (fraction of nominal) over the run: straight runs with periodic corners."""
    t = np.arange(n_steps) * dt
    in_corner = (t % corner_every_s) >= (corner_every_s - corner_duration_s)
    return np.where(in_corner, corner_speed, 1.0)
