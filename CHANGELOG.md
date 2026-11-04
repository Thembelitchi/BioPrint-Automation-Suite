# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Thembelitchi/BioPrint-Automation-Suite/releases/tag/v0.1.0
