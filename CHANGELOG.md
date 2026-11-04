# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/releases/tag/v0.1.0
