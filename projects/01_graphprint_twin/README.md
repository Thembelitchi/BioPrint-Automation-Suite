# 🏗️ GraphPrint-Twin

**Semantic cyber-physical digital twin.** I stream simulated PLC telemetry (nozzle temperature and pump pressure) into Neo4j. Every reading becomes a node attached to the printer that generated it and the wall segment it was printing, so one query traces a defect back to its material batch.

This project owns the suite's [shared graph schema](../../docs/graph-schema.md).

> **Status:** planned for **v0.2.0**. Code, run instructions and the requirements map land with that release.

| Requirement | Summary |
|---|---|
| FR-1 | Random-walk telemetry around 230 °C / 45 bar, tagged `OPTIMAL` or `ANOMALY_WARNING` (> 235 °C or > 48 bar) |
| FR-2 | Constraints and baseline topology, idempotent `MERGE` |
| FR-3 | Parameterised managed-transaction ingestion with retries |
| FR-4 | Forensic traceback query: anomaly → wall → material batch → vendor |
