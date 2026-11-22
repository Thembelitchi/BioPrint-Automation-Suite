"""The Structured Text port must reproduce the Python reference, scan by scan.

The .st files are executed with plc/st_subset.py, a small interpreter for the ST
subset they use. That checks the logic in the test suite. A compile in TwinCAT / CODESYS
(see plc/README.md) is still needed to confirm syntax and target behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

PLC = Path(__file__).resolve().parent.parent / "plc"
sys.path.insert(0, str(PLC))

import generate_test_vectors  # noqa: E402
from st_subset import STFunctionBlock  # noqa: E402


@pytest.fixture(scope="module")
def pid_vectors() -> pd.DataFrame:
    return pd.read_csv(PLC / "test_vectors" / "pid_reference.csv")


@pytest.fixture(scope="module")
def qc_vectors() -> pd.DataFrame:
    return pd.read_csv(PLC / "test_vectors" / "westgard_reference.csv")


def test_committed_vectors_are_up_to_date(tmp_path) -> None:
    """If the model or config changes, regenerate: python plc/generate_test_vectors.py"""
    generated = generate_test_vectors.write_all(tmp_path)
    for path in generated:
        committed = PLC / path.relative_to(tmp_path)
        assert committed.read_text() == path.read_text(), f"{committed.name} is stale"


def test_st_pid_matches_python(pid_vectors) -> None:
    fb = STFunctionBlock(PLC / "src" / "FB_BioPrintPID.st")
    worst = 0.0
    for row in pid_vectors.itertuples():
        fb(lrSetpoint=row.setpoint, lrMeasurement=row.measurement)
        worst = max(worst, abs(fb.lrOutput - row.output), abs(fb.lrITerm - row.i_term))
    assert worst < 1e-9


def test_st_pid_reset_and_anti_windup() -> None:
    fb = STFunctionBlock(PLC / "src" / "FB_BioPrintPID.st")
    for _ in range(100):
        fb(lrSetpoint=230.0, lrMeasurement=25.0)
    assert fb.lrITerm <= 100.0 and fb.bSaturated and fb.lrOutput == 100.0
    fb(bReset=True)
    assert fb.lrITerm == 0.0 and fb.lrOutput == 0.0


def test_st_pid_without_anti_windup_winds_up() -> None:
    fb = STFunctionBlock(PLC / "src" / "FB_BioPrintPID.st")
    for _ in range(100):
        fb(lrSetpoint=230.0, lrMeasurement=25.0, bAntiWindup=False)
    assert fb.lrITerm > 1000.0


def test_st_westgard_matches_python(qc_vectors) -> None:
    fb = STFunctionBlock(PLC / "src" / "FB_WestgardMonitor.st")
    mismatches = []
    for row in qc_vectors.itertuples():
        fb(lrValue=row.value, diStep=row.step)
        got = (fb.bWarn12s, fb.bReject13s, fb.bReject22s)
        want = (row.warn_1_2s, row.reject_1_3s, row.reject_2_2s)
        if got != want:
            mismatches.append((row.step, got, want))
    assert not mismatches, mismatches[:5]
    gvl = (PLC / "src" / "GVL_ReferenceVectors.st").read_text()
    sd = float(gvl.split("LR_BASELINE_SD  : LREAL := ")[1].split(";")[0])
    assert fb.lrBaselineSd == pytest.approx(sd, abs=1e-12)
    assert fb.bRunRejected


def test_st_westgard_flags_bad_configuration() -> None:
    fb = STFunctionBlock(PLC / "src" / "FB_WestgardMonitor.st")
    fb(lrValue=230.0, diStep=0, iBaselineLength=5)
    assert fb.bConfigError and not fb.bArmed


def test_interpreter_refuses_unsupported_syntax(tmp_path) -> None:
    src = tmp_path / "FB_X.st"
    src.write_text(
        "FUNCTION_BLOCK FB_X\nVAR\n  x : INT;\nEND_VAR\n"
        "WHILE x < 3 DO x := x + 1; END_WHILE;\nEND_FUNCTION_BLOCK\n"
    )
    with pytest.raises(ValueError):
        STFunctionBlock(src)
