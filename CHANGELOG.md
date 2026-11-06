# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `scripts/verify.sh`: runs lint, every project's tests, the scenario runs and (when Docker is available) the live Neo4j stage locally, with a pass/fail/skip summary.

### Changed
- The GitHub Actions workflow runs only when started manually; local verification is the primary path.
- README: removed the workflow-status and release badges.

## [0.4.0] - 2026-11-06 — Expert-review readiness

The aim of this release is that each of five specialisms can evaluate the suite on its own terms: LFAM process engineers, polymer scientists, robotic construction operations leads, industrial digital twin architects and controls engineers.

### Added
- **Reviewer guide** (`docs/reviewers.md`): for each specialism, what is built, a 10-minute exercise, the specific questions I'd like answered, and the limitations I already know about. Includes a cross-disciplinary glossary.
- **Assumptions register** (`docs/model-assumptions.md`): every model parameter (A-01 to A-27) with value, unit, basis, confidence, intended reviewer and what real data would replace it.
- **Standards alignment** (`docs/standards-alignment.md`): ISA-95, ISA-88, AAS, IFC, OPC UA companion specs and IEC 61131-3, with explicit gaps.
- **Expert review issue form** (`.github/ISSUE_TEMPLATE/expert_review.yml`).
- `pid`: **config-driven model.** All parameters are in `config/default.toml` (annotated with units and assumption IDs). `--config` merges a reviewer's override, invalid or misspelt keys are rejected clearly, and `--report-only` lets reviewers explore out-of-spec values. A worked example (`config/examples/winter_shop_high_fibre.toml`) is included.
- `pid`: **composition effects on melt viscosity.** Krieger-Dougherty fibre loading and Mw-based recycling degradation, applied relative to the calibration batch, plus a `material_sensitivity.png` chart and CSV.
- `pid`: **layer-time / inter-layer adhesion window** vs factory ambient (`layer_thermal.py`, `layer_time_window.png`), with a Biot-number validity check.
- `pid`: **IEC 61131-3 Structured Text port** of the PID and Westgard blocks (`plc/`), with TcUnit test suites, 300-scan reference vectors (CSV and ST GVL) and a generator. The test suite executes the ST logic with a small subset interpreter and matches Python to 1e-9.
- `twin`: **layer-bucketed telemetry** (`layer_aggregator.py`). The 10 Hz samples of each printed layer are written as one `LayerTelemetry` node with summary statistics, anomaly offsets and implicit timestamps; raw arrays are kept only for anomalous layers (`--raw-policy`). Layers are chained with `NEXT_LAYER`. This cuts graph growth from ~864,000 nodes per printer per day to ~120. Ingest is one idempotent transaction per layer (`MERGE` on deterministic ids and on every relationship).
- `twin`: **ISA-95 equipment hierarchy** (Site → Area → WorkCenter → printer), ISA-88 control-module level, **AAS ids** on assets, **IFC GlobalId** (`ifc.py`) and `IfcWall` class on wall segments, and materials-science properties on batches (`fibre_wt_pct`, `recycle_generation`, `moisture_ppm`, `material_definition`). The traceback now returns IFC ids and batch composition.

### Changed
- `twin`: **`TelemetryLog` (one node per reading) replaced by `LayerTelemetry`**; `ingest_telemetry` → `ingest_layer`; `prune_optimal_logs` → `prune_raw_arrays` (summaries are kept forever, raw arrays are dropped after N days); CLI `--cycles` → `--layers` / `--samples-per-layer`.
- `twin`: the simulator now samples at 10 Hz on a simulated clock, with noise, reversion and upset rate rescaled for that rate.
- `twin`: `MaterialBatch.fibre_pct` renamed to `fibre_wt_pct`.
- `pid`: `app.simulate()` now takes a `SimulationConfig`; the module-level constants are gone.
- Roadmap: BioPrint-Guard moves to v0.5.0 and cross-project integration to v0.6.0.
- `pid`: the plant keeps a private copy of its thermal parameters, so a shared config object can never carry one run's disturbance into the next (regression test added).


## [0.3.0] - 2026-11-06

### Added
- **BioPrint-PID** (`projects/02_bioprint_pid`):
  - `DiscretePID`: parallel PID at dt = 0.1 s with derivative on measurement, filtered derivative, 0–100 % output clamp and anti-windup by conditional integration.
  - `ExtruderThermalMass`: lumped thermal model (heater, ambient loss, cold-feed load), a power-law shear-thinning melt (n = 0.4) with Arrhenius K(T) calibrated to 45 bar at 230 °C, gantry-coupled auger flow and a cold-extrusion interlock.
  - `BiomedicalQAMonitor`: Westgard 1-2s / 1-3s / 2-2s against SD fixed from a post-settling baseline, plus rolling mean, SD and CV%.
  - `app.py`: 30 s scenario with an ambient disturbance at step 150; writes a four-panel dashboard (tracking, effort, pressure, Levey-Jennings), an untuned-vs-tuned comparison, CSVs and `acceptance.json`; exits non-zero if any PRD check fails; `--realtime` pacing.
  - 36 tests, including a 50-seed robustness sweep (FR-1 holds for every seed, detection 50/50, false-reject rate bounded).
- CI job `bioprint-pid`, which uploads the dashboard and run data as artifacts.
- Dashboard and comparison charts in `assets/`, embedded in the READMEs.


## [0.2.0] - 2026-11-05

### Added
- **GraphPrint-Twin** (`projects/01_graphprint_twin`):
  - `PLCExtruderSimulator`: a seeded, mean-reverting random walk around 230 °C / 45 bar with one-reading process upsets, classified `OPTIMAL` / `ANOMALY_WARNING` against the 235 °C / 48 bar envelope.
  - `DigitalTwinBridge`: uniqueness constraints, idempotent `MERGE` topology (PLC → printer → stepper motor; wall → material batch), parameterised managed-transaction ingestion that refuses to create unattached readings, run-integrity check, forensic traceback query and retention pruning.
  - `main.py` CLI (`--cycles`, `--interval`, `--seed`, `--prune-days`) with per-write latency logging against the 250 ms budget and a CSV traceback export. It exits non-zero if any reading is missing or unlinked.
  - Unit tests (simulator; bridge with a fake driver) and live-Neo4j integration tests.
- CI job `graphprint-twin`: a Neo4j 5 service container runs the unit tests, the 10-cycle pipeline and the integration tests, then uploads the traceback CSV.
- `TelemetrySource` protocol, so a Modbus/OPC UA/Kafka reader can replace the simulator.


## [0.1.0] - 2026-11-05

### Added
- Monorepo layout: `projects/01_graphprint_twin`, `02_bioprint_pid` and `03_bioprint_guard`, each with `src/`, `requirements.txt` and a README.
- Master README with the OT → IT architecture overview, sub-project summaries and roadmap.
- `docs/architecture.md` and `docs/graph-schema.md`, the shared Neo4j schema used by GraphPrint-Twin and BioPrint-Guard.
- `docker-compose.yml` for a local Neo4j 5 Community instance.
- GitHub Actions workflow `integration-tests.yml` with a Ruff lint stage.
- Shared Ruff configuration in `pyproject.toml`.
- Issue templates, a pull request template and `CONTRIBUTING.md`.
- MIT licence.

[Unreleased]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/releases/tag/v0.1.0
