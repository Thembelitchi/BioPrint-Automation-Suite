# Model Assumptions Register

Every number in BioPrint-PID's physics, rheology, layer and QC models is listed here with its basis and my confidence in it. The values live in [`projects/02_bioprint_pid/config/default.toml`](../projects/02_bioprint_pid/config/default.toml) under the same `[A-xx]` tags. Reviewers can override any of them without touching code:

```bash
cd projects/02_bioprint_pid
python src/app.py --config your_values.toml --report-only
```

**Confidence levels**
- **Structural:** the form of the relationship is textbook; the coefficient is generic.
- **Plausible:** an order-of-magnitude engineering estimate, not taken from a datasheet.
- **Placeholder:** chosen to make the demo behave sensibly. Real data is needed.
- **Tuned:** fitted to this model, so it is only as good as the model.

**Reviewer key:** LFAM = LFAM process engineer · POLY = polymer / materials scientist · CTRL = controls engineer · OPS = robotic construction operations · DT = digital twin architect

## Thermal model (barrel / nozzle zone)

| ID | Parameter | Default | Unit | Basis | Confidence | Who should review | Replace with |
|---|---|---|---|---|---|---|---|
| A-01 | `thermal.heat_capacity` | 400 | J/K | Sized so that warm-up takes ~5 s; a real LFAM barrel zone is far larger | Placeholder | LFAM, CTRL | Measured zone step response (process gain and time constant) |
| A-02 | `thermal.heater_power_max` | 15 000 | W | Order of magnitude for an LFAM band-heater zone | Plausible | LFAM | Installed heater rating per zone |
| A-03 | `thermal.loss_coeff` | 8 | W/K | Gives ~10–20 % steady duty at 230 °C | Placeholder | LFAM | Fitted from cooling curves with the heater off |
| A-04 | `thermal.ambient_temp` | 25 | °C | Conditioned factory | Plausible | OPS | Site climate data / HVAC setpoint |
| A-05 | `thermal.feed_temp` | 25 | °C | Pellets at ambient (undried) | Plausible | LFAM | Dryer outlet temperature (often 60–70 °C for PETG) |
| A-06 | `thermal.specific_heat` | 1 500 | J/(kg·K) | Typical polymer c_p | Structural | POLY | DSC data for the compound |
| A-07 | `thermal.nominal_mass_flow` | 0.005 | kg/s | 18 kg/h: low-to-mid LFAM throughput | Plausible | LFAM | Machine throughput at the chosen bead size |

## Melt rheology

| ID | Parameter | Default | Unit | Basis | Confidence | Who should review | Replace with |
|---|---|---|---|---|---|---|---|
| A-08 | `rheology.flow_index` (n) | 0.4 | – | Power-law index typical of filled thermoplastics at extrusion shear rates | Structural | POLY | Capillary rheometry fit |
| A-09 | `rheology.activation_energy` | 50 | kJ/mol | Mid-range Arrhenius flow activation energy for polyesters | Plausible | POLY | Fitted from rheometry at 3+ temperatures |
| A-10 | `rheology.reference_pressure` @ `reference_temp` | 45 bar @ 230 °C | bar, °C | Calibration point chosen to match GraphPrint-Twin's 45 bar target | Placeholder | LFAM | Measured nozzle pressure at nominal flow |
| A-11 | `rheology.max_pressure` | 250 | bar | Relief-valve clamp | Plausible | LFAM | Machine pressure limit |

**Model form:** `ΔP ∝ K(T, composition) · Qⁿ`, with `K(T) = K_ref · exp(Eₐ/R · (1/T − 1/T_ref))`. This assumes a fixed die geometry and isothermal flow through the nozzle, and ignores pressure-dependent viscosity, wall slip and fibre orientation.

## Composition (fibre loading, recycling)

| ID | Parameter | Default | Unit | Basis | Confidence | Who should review | Replace with |
|---|---|---|---|---|---|---|---|
| A-12 | `material.fibre_wt_pct` / `reference_fibre_wt_pct` | 15 / 15 | wt % | Common short-glass-fibre loading for printable PETG compounds | Plausible | POLY, LFAM | Compound datasheet; ash test |
| A-13 | `fibre_density` / `matrix_density` | 2 540 / 1 270 | kg/m³ | E-glass / PETG handbook values | Structural | POLY | Supplier values |
| A-14 | `fibre_intrinsic_viscosity` [η] / `max_packing_fraction` φ_m | 5.0 / 0.35 | – | Krieger-Dougherty with values in the range reported for short fibres (aspect-ratio dependent) | Plausible | POLY | Fit η_r(φ) from rheometry at several loadings |
| A-15 | `recycle_generation`, `mw_retention_per_cycle` | 1, 0.95 | –, – | 5 % Mw loss per reprocessing pass (dried material) | Placeholder | POLY | GPC / intrinsic viscosity after each pass; strongly moisture dependent |
| A-16 | `mw_exponent` | 3.4 | – | η₀ ∝ Mw^3.4 for entangled melts | Structural | POLY | — |

**Known simplifications** a polymer scientist will spot:

1. Mw^3.4 strictly applies to the zero-shear viscosity. Applying it to the power-law K assumes the shear-thinning index doesn't change with Mw.
2. Hydrolytic degradation of PETG depends heavily on residual moisture. `moisture_ppm` is recorded in the graph but not yet used by the model.
3. Fibre attrition (aspect-ratio loss) during recycling is ignored, so recycled compounds probably lose fibre reinforcement faster than modelled.
4. Fibres change n and add yield-like behaviour at high loadings, which is not modelled.

## Toolpath, sensor, interlock, disturbance

| ID | Parameter | Default | Unit | Basis | Confidence | Who should review | Replace with |
|---|---|---|---|---|---|---|---|
| A-17 | `gantry.*` | corner every 4 s, 0.8 s at 40 % | s, – | Simple periodic toolpath | Placeholder | LFAM | Speed profile from the slicer / G-code |
| A-18 | `sensor.noise_sd` / `lag_s` | 0.15 °C / 0.5 s | °C, s | Grounded Type J/K thermocouple in a well | Plausible | CTRL | Measured noise; sheath time constant |
| A-19 | `interlock.band` | 2 | °C | Auger allowed once within 2 °C of setpoint | Plausible | CTRL, LFAM | Site cold-extrusion interlock settings (often with a soak timer) |
| A-20 | `scenario.disturbed_*` | 5 °C, 30 W/K | °C, W/K | Roller door opening in winter: stress case | Placeholder | OPS | Logged ambient / draft events |
| A-21 | `qc.baseline_start`, `baseline_length` | step 80, 40 samples | – | After settling, before the disturbance | Tuned | CTRL | Site QC SOP (control frequency, n for limits) |

## Layer-time (inter-layer adhesion) model

| ID | Parameter | Default | Unit | Basis | Confidence | Who should review | Replace with |
|---|---|---|---|---|---|---|---|
| A-22 | `layer.bead_width_mm` / `bead_height_mm` | 20 / 5 | mm | LFAM-scale bead | Plausible | LFAM | Process sheet |
| A-23 | `bead_density` / `bead_specific_heat` / `bead_conductivity` | 1 400 / 1 800 / 0.30 | kg/m³, J/(kg·K), W/(m·K) | 15 wt % GF rPETG estimates | Plausible | POLY | Measured compound properties |
| A-24 | `convective_h` | 10 (still air) | W/(m²·K) | Natural convection; ~25 with fans | Plausible | LFAM, OPS | Thermal-camera cooling curves of a printed bead |
| A-25 | `glass_transition` | 80 | °C | PETG Tg | Structural | POLY | DSC |
| A-26 | `bond_min_temp` / `support_max_temp` | 90 / 120 | °C | Bond needs the substrate above Tg; support needs it stiff enough to carry the next layer | **Placeholder** | **LFAM** | Process window from peel/tensile tests vs layer time |

**Model form:** lumped (Newtonian) cooling of the bead from the melt setpoint. It ignores conduction into the layer below and re-heating by the next layer. The Biot number (~0.11 in still air, ~0.28 with fans) is reported on the chart: above 0.1 the lumped assumption is approximate, and a 1-D or 2-D conduction model would be the upgrade.

## Controller

| ID | Parameter | Default | Basis | Confidence | Who should review |
|---|---|---|---|---|---|
| A-27 | `controller.kp / ki / kd`, `derivative_filter_n` | 8 / 2.5 / 2, N = 10 | Grid sweep against this model for overshoot, settling after the interlock adds feed load, and disturbance recovery | Tuned | CTRL |

Gains tuned to a placeholder plant won't transfer to a real machine. With a measured step response (A-01, A-03), the next step would be an IMC or SIMC tuning rule and a comparison with the swept gains.

---

## How to propose a change

Open an issue with the **Expert review** template. Include the A-ID, your value, its basis (a datasheet, test data or experience), and whether you'd expect it to change any PRD acceptance result. If you attach a TOML override, I'll add it to `config/examples/` and run it in CI.
