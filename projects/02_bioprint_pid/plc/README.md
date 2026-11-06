# PLC port: IEC 61131-3 Structured Text

The control and QC logic of BioPrint-PID, ported from Python to Structured Text so it can run on a real or soft PLC (TwinCAT 3, CODESYS, or any IEC 61131-3 runtime with LREAL support).

| File | What it is |
|---|---|
| `src/FB_BioPrintPID.st` | Heater PID: derivative on measurement, filtered D, output clamp, conditional-integration anti-windup. Line-for-line port of `../src/pid_controller.py`. |
| `src/FB_WestgardMonitor.st` | Westgard 1-2s / 1-3s / 2-2s with a baseline-fixed SD and a latched run reject. Port of `../src/westgard_validator.py` (rolling dashboard statistics not ported). |
| `src/DUT_ReferenceVectors.st`, `src/GVL_ReferenceVectors.st` | **Generated.** 300 scans of inputs and expected outputs from the Python reference, as ST constants. |
| `tests/FB_BioPrintPID_Test.st`, `tests/FB_WestgardMonitor_Test.st` | [TcUnit](https://tcunit.org) suites that replay the vectors and check every output. |
| `test_vectors/*.csv` | The same vectors as CSV, for any other test framework. |
| `generate_test_vectors.py` | Regenerates the vectors and GVL from the Python model. |
| `st_subset.py` | A small interpreter for the ST subset used here. It is used **only** to check the ST logic in CI. |

## What has and hasn't been verified

- ✅ **Logic:** CI executes the actual `.st` source with `st_subset.py` and replays all 300 reference scans. PID output and integrator match Python within 1e-9; every Westgard flag and the baseline SD match exactly (`tests/test_plc_port.py`).
- ✅ **Freshness:** CI regenerates the vectors and fails if the committed copies are stale.
- ⚠️ **Not yet done:** compiling on a real toolchain and running on a target. The interpreter checks behaviour, not every rule of the IEC grammar, type system or task timing. **This is the step I'd most like a controls engineer to try.**

## Running it in TwinCAT 3 or CODESYS

1. Create a PLC project. Add a DUT and a GVL and paste in `DUT_ReferenceVectors.st` and `GVL_ReferenceVectors.st`.
2. Add two function blocks (POUs) and paste in `FB_BioPrintPID.st` and `FB_WestgardMonitor.st`.
3. Install TcUnit (TwinCAT) or the CODESYS TcUnit port, then add the two test FBs from `tests/`.
4. In a test PRG, declare instances of both test FBs, call them, then call `TcUnit.RUN();`.
5. Run on a 100 ms task (`lrDt` = 0.1 s) and check the TcUnit output.

The `.st` files are plain text rather than `.TcPOU` / `.project` exports so they diff cleanly and stay vendor-neutral. Paste the body and declarations into the matching editor sections.

## Using the FB in a real loop

```iecst
PROGRAM MAIN
VAR
    fbZone1  : FB_BioPrintPID;
    fbQc1    : FB_WestgardMonitor;
    diScan   : DINT;
    lrTc1    : LREAL;     (* scaled thermocouple input *)
    lrHeater : LREAL;     (* % duty to the SSR / PWM output *)
END_VAR

fbZone1(lrSetpoint := 230.0, lrMeasurement := lrTc1);
lrHeater := fbZone1.lrOutput;

fbQc1(lrValue := lrTc1, diStep := diScan);
IF fbQc1.bReject13s OR fbQc1.bReject22s THEN
    ;  (* raise an alarm / hold the print / log a QC event *)
END_IF;
diScan := diScan + 1;
```

Questions I'd value from a controls engineer are in [`docs/reviewers.md`](../../../docs/reviewers.md#controls-engineer).
