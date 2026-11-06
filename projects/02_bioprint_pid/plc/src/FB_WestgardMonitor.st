(* =============================================================================
   FB_WestgardMonitor — IEC 61131-3 Structured Text port of src/westgard_validator.py
   -----------------------------------------------------------------------------
   Clinical Westgard multirule on one process variable, called once per sample:
     Phase 1  collect iBaselineLength samples from diBaselineStart, then fix the SD
              (sample SD, n-1) with a floor of lrMinSd
     Phase 2  judge each later sample against lrTarget ± k·SD
                1-2s  warning   |z| > 2
                1-3s  reject    |z| > 3
                2-2s  reject    two consecutive |z| > 2 on the same side
   bRunRejected latches until bReset, like a rejected analytical run in a lab.

   Not ported: rolling mean / SD / CV% (dashboard statistics only).
   Status: NOT compiled in this repository. Verify with tests/FB_WestgardMonitor_Test.st.
   ============================================================================= *)
FUNCTION_BLOCK FB_WestgardMonitor
VAR_INPUT
    lrValue          : LREAL;          (* sample, e.g. barrel temperature °C *)
    lrTarget         : LREAL := 230.0; (* assigned value, i.e. the setpoint *)
    diStep           : DINT;           (* sample index, starting at 0 *)
    diBaselineStart  : DINT := 80;
    iBaselineLength  : INT  := 40;     (* 10 .. MAX_BASELINE *)
    lrMinSd          : LREAL := 0.05;
    bReset           : BOOL;
END_VAR
VAR_OUTPUT
    bArmed        : BOOL;              (* baseline fixed, rules active *)
    lrBaselineSd  : LREAL;
    lrZ           : LREAL;
    bWarn12s      : BOOL;              (* this sample *)
    bReject13s    : BOOL;              (* this sample *)
    bReject22s    : BOOL;              (* this sample *)
    bRunRejected  : BOOL;              (* latched *)
    bConfigError  : BOOL;
END_VAR
VAR CONSTANT
    MAX_BASELINE : INT := 200;
END_VAR
VAR
    alrBaseline : ARRAY[1..200] OF LREAL;
    iCount      : INT;
    i           : INT;
    lrSum       : LREAL;
    lrMean      : LREAL;
    lrSq        : LREAL;
    lrPrevZ     : LREAL;
    bHavePrev   : BOOL;
END_VAR

IF bReset THEN
    iCount := 0; bArmed := FALSE; bHavePrev := FALSE; bRunRejected := FALSE;
    lrBaselineSd := 0.0; lrZ := 0.0;
END_IF;

bWarn12s := FALSE;
bReject13s := FALSE;
bReject22s := FALSE;
bConfigError := (iBaselineLength < 10) OR (iBaselineLength > MAX_BASELINE);
IF bConfigError THEN
    RETURN;
END_IF;

(* --- Phase 1: baseline window ---------------------------------------------- *)
IF diStep >= diBaselineStart AND diStep < diBaselineStart + INT_TO_DINT(iBaselineLength) THEN
    iCount := iCount + 1;
    alrBaseline[iCount] := lrValue;
    IF iCount = iBaselineLength THEN
        lrSum := 0.0;
        FOR i := 1 TO iCount DO
            lrSum := lrSum + alrBaseline[i];
        END_FOR;
        lrMean := lrSum / INT_TO_LREAL(iCount);
        lrSq := 0.0;
        FOR i := 1 TO iCount DO
            lrSq := lrSq + (alrBaseline[i] - lrMean) * (alrBaseline[i] - lrMean);
        END_FOR;
        lrBaselineSd := MAX(SQRT(lrSq / INT_TO_LREAL(iCount - 1)), lrMinSd);
        bArmed := TRUE;
    END_IF;
    RETURN;
END_IF;

IF NOT bArmed THEN
    RETURN;
END_IF;

(* --- Phase 2: Westgard rules against target and fixed SD ------------------- *)
lrZ := (lrValue - lrTarget) / lrBaselineSd;

IF ABS(lrZ) > 3.0 THEN
    bReject13s := TRUE;
ELSIF ABS(lrZ) > 2.0 THEN
    bWarn12s := TRUE;
END_IF;

IF bHavePrev AND ABS(lrZ) > 2.0 AND ABS(lrPrevZ) > 2.0
   AND ((lrZ > 0.0) = (lrPrevZ > 0.0)) THEN
    bReject22s := TRUE;
END_IF;

lrPrevZ := lrZ;
bHavePrev := TRUE;
bRunRejected := bRunRejected OR bReject13s OR bReject22s;
END_FUNCTION_BLOCK
