# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/releases/tag/v0.1.0
