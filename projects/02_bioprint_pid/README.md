# 🧪 BioPrint-PID

**Precision polymer extrusion control with clinical QC.** A discrete PID controller (dt = 0.1 s, anti-windup) holds an extruder barrel at 230 °C, against a model of thermal mass, ambient loss and cold-feed cooling. The melt is modelled as a shear-thinning power-law fluid with Arrhenius temperature dependence. A Westgard monitor (1-2s, 1-3s, 2-2s) watches the process the way a clinical lab watches its control material.

> **Status:** planned for **v0.3.0**.

| Requirement | Summary |
|---|---|
| FR-1 | Settles within ±1 °C in < 15 s with < 5 % overshoot; output clamped to 0–100 % |
| FR-2 | Nozzle pressure responds to temperature (Arrhenius) and flow (power law, n ≈ 0.4) |
| FR-3 | Westgard violations flagged during the step-150 disturbance and none during the stable baseline |
| FR-4 | `pid_step_response.png` (tracking, effort, Levey-Jennings) and a results CSV |
