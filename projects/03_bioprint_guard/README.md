# 🛡️ BioPrint-Guard

**Async OPC UA predictive maintenance.** A simulated OPC UA server publishes stepper-motor current, housing temperature and vibration at 10 Hz, with a progressive friction-wear fault injected after 15 s. An async gateway subscribes, scores each sample from 0 to 1 (rolling multivariate Z-score + EWMA), and raises a maintenance alert in the shared Neo4j twin when the score passes 0.85.

> **Status:** planned for **v0.4.0**.

| Requirement | Summary |
|---|---|
| FR-1 | OPC UA server on `opc.tcp://0.0.0.0:4840/bioprint/`; node path FactoryFloor → ExtrusionGantry → StepperMotor |
| FR-2 | Single subscription for all three variables, with reconnect and backoff |
| FR-3 | Score < 0.5 during the healthy baseline and > 0.85 after wear onset |
| FR-4 | Exactly one OPEN `MaintenanceAlert` via `[:REQUIRES_INSPECTION]`, with no duplicate components |
