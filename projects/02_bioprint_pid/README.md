# 🧪 BioPrint-PID

**Precision polymer extrusion control, checked the way a clinical lab checks its controls.**

A discrete PID controller holds an extruder barrel at 230 °C while recycled, glass-fibre-reinforced PETG feeds through it. Around the loop I put the quality-control discipline I used for years in clinical pathology: a Westgard monitor fixes its limits from a stable baseline, then judges every reading against 1-2s, 1-3s and 2-2s rules. When the environment changes, the QC layer flags it within 0.2 s, while the controller is still recovering.

> Simulated plant. All parameters are illustrative values for a nozzle zone, not data for a specific machine.

![BioPrint-PID dashboard](../../assets/pid_step_response.png)

## What's in the model

| Piece | Model |
|---|---|
| **Controller** (`pid_controller.py`) | Parallel PID at dt = 0.1 s. Derivative on measurement (no setpoint kick), filtered derivative (N = 10), output clamped to 0–100 % duty, anti-windup by conditional integration. |
| **Barrel thermal mass** (`extruder_physics.py`) | `C·dT/dt = P_max·u − h·(T − T_amb) − ṁ·c_p·(T − T_feed)`: heater input against ambient loss and the cooling load of cold pellets. |
| **Melt rheology** | Power-law, shear-thinning (n = 0.4) with Arrhenius temperature dependence: `ΔP ∝ K(T)·Qⁿ`, calibrated to 45 bar at 230 °C. |
| **Gantry coupling** | The gantry slows to 40 % at corners every 4 s; auger flow follows it, so pressure and thermal load both change. |
| **Cold-extrusion interlock** | The auger stays off until the barrel first reaches melt temperature, as a real PLC would enforce. |
| **Sensor** | Thermocouple with a 0.5 s lag and 0.15 °C noise. Both the controller and QC see only this signal. |
| **QC** (`westgard_validator.py`) | Baseline of 40 samples (8–12 s), after which the SD is frozen. Rules: 1-2s (warn), 1-3s (reject), 2-2s (reject). Rolling mean, SD and CV% are also reported. |

**Scenario:** a 30 s run (300 steps) from a 25 °C cold start. At step 150 a roller door opens, ambient drops to 5 °C and convective loss rises from 8 to 30 W/K.

## Results (seed 42)

| Check | Target (PRD) | Result |
|---|---|---|
| FR-1 Overshoot | < 5 % | **0.99 %** |
| FR-1 Settling (±1 °C) | < 15 s | **6.8 s** |
| FR-1 Output bounds | 0–100 % | ✅ |
| FR-2 Pressure after a 15 °C drop | rises | **45.0 → 65.0 bar (×1.44)** |
| FR-3 Disturbance detected | yes | **First reject at 15.2 s (0.2 s after onset), rules 1-3s and 2-2s** |
| FR-3 Rejects during stable running | none | **0** |
| FR-4 Dashboard + CSV | written | ✅ |

### Robustness across 50 noise seeds
- FR-1 holds for every seed: worst overshoot 1.12 %, worst settling 11.9 s.
- The disturbance is detected in **50 of 50** runs.
- **5 of 50** runs (10 %) show one false reject in the 3 s stable window before the disturbance. That rate is what the maths predicts: checking every 10 Hz sample against 1-3s and 2-2s carries about a 0.3 % false-reject chance per sample, and over 30 samples that adds up to roughly 10 %. A lab avoids this by running controls once per batch rather than continuously, so a next step here would be to evaluate QC on a slower "control sample" schedule. The test suite bounds the false-reject rate at 15 % or below.

### Why anti-windup matters

![Untuned vs tuned](../../assets/pid_tuning_comparison.png)

Without anti-windup, the integrator keeps growing for the whole 5.5 s warm-up while the heater is pinned at 100 %. That stored error drives the barrel to about 347 °C (57 % overshoot), well into the range where the polymer would degrade. With conditional integration, the integral stays near zero until the loop leaves saturation.

## Run it

```bash
cd projects/02_bioprint_pid
pip install -r requirements-dev.txt
python src/app.py              # simulated time, about 1 s
python src/app.py --realtime   # paced to the wall clock, 30 s
python -m pytest               # 36 tests
```

Outputs land in `output/`: `pid_step_response.png`, `pid_tuning_comparison.png`, `pid_run.csv` (every signal, every step), `qc_violations.csv` and `acceptance.json`. The exit code is 0 only if FR-1 to FR-4 all pass.

## Requirements map

| PRD requirement | Where | Checked by |
|---|---|---|
| **FR-1** Discrete PID, dt = 0.1 s, anti-windup | `pid_controller.py` | `test_pid_controller.py`, `test_fr1_holds_for_every_seed` |
| **FR-2** Temperature- and flow-dependent melt, gantry coupling | `extruder_physics.py` | `test_extruder_physics.py`, `test_disturbance_raises_pressure` |
| **FR-3** Westgard 1-2s / 1-3s / 2-2s with rolling mean, SD and CV% | `westgard_validator.py` | `test_westgard_validator.py`, `test_fr3_detection_and_false_rejection_rate` |
| **FR-4** Dashboard and time-series export | `app.py` | `test_main_writes_outputs`, CI artifact |
| NFR 5.1 Deterministic timing | Simulated time by default; `--realtime` uses absolute monotonic ticks | — |
| NFR 5.2 Numerical stability | Clamps on duty, integral, temperature, pressure and the Arrhenius exponent | `test_output_always_finite`, `test_signals_are_finite` |

## Tuning notes

I chose Kp = 8, Ki = 2.5 s⁻¹, Kd = 2 s from a grid sweep against the model, balancing overshoot, settling after the auger interlock adds feed load, and recovery from the disturbance. The untuned comparison (Kp = 2, Ki = 0.6, no D, no anti-windup) is a deliberately naive configuration.
