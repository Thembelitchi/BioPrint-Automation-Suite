(* =============================================================================
   FB_BioPrintPID — IEC 61131-3 Structured Text port of src/pid_controller.py
   -----------------------------------------------------------------------------
   Parallel PID for a heater zone, called once per task cycle:
     * derivative on measurement (no derivative kick on setpoint steps)
     * first-order filtered derivative, Tf = Kd / (Kp * N)
     * output clamped to [lrOutMin, lrOutMax] (% duty)
     * anti-windup by conditional integration, plus a hard bound on the integrator

   The task cycle time MUST equal lrDt (default T#100MS).
   LREAL is used throughout so that results match the Python reference (IEEE
   double) to ~1e-9 when replaying plc/test_vectors. With REAL, expect
   differences around 1e-3.

   Status: written to match the Python implementation line for line. It has NOT
   been compiled in this repository (no PLC toolchain available here). Verify it with
   tests/FB_BioPrintPID_Test.st (TcUnit) in TwinCAT 3 or CODESYS.
   ============================================================================= *)
FUNCTION_BLOCK FB_BioPrintPID
VAR_INPUT
    lrSetpoint     : LREAL;            (* °C *)
    lrMeasurement  : LREAL;            (* °C, thermocouple *)
    lrKp           : LREAL := 8.0;     (* %/°C *)
    lrKi           : LREAL := 2.5;     (* %/(°C·s) *)
    lrKd           : LREAL := 2.0;     (* %·s/°C *)
    lrDt           : LREAL := 0.1;     (* s, = task cycle time *)
    lrOutMin       : LREAL := 0.0;     (* % *)
    lrOutMax       : LREAL := 100.0;   (* % *)
    lrDerivFilterN : LREAL := 10.0;
    bAntiWindup    : BOOL  := TRUE;
    bReset         : BOOL;             (* clears integrator and derivative memory *)
END_VAR
VAR_OUTPUT
    lrOutput   : LREAL;                (* % duty, clamped *)
    lrPTerm    : LREAL;
    lrITerm    : LREAL;
    lrDTerm    : LREAL;
    bSaturated : BOOL;                 (* TRUE while pushing against a limit *)
END_VAR
VAR
    lrIntegral       : LREAL;
    lrDState         : LREAL;
    lrPrevMeasurement: LREAL;
    bFirstScan       : BOOL := TRUE;
    lrError          : LREAL;
    lrRawD           : LREAL;
    lrTf             : LREAL;
    lrAlpha          : LREAL;
    lrCandidate      : LREAL;
    lrUnclamped      : LREAL;
END_VAR

IF bReset THEN
    lrIntegral := 0.0;
    lrDState   := 0.0;
    bFirstScan := TRUE;
    lrOutput   := 0.0;
    lrPTerm    := 0.0;
    lrITerm    := 0.0;
    lrDTerm    := 0.0;
    bSaturated := FALSE;
    RETURN;
END_IF;

lrError := lrSetpoint - lrMeasurement;

(* --- Derivative on measurement, low-pass filtered ------------------------- *)
IF bFirstScan THEN
    lrRawD := 0.0;
    bFirstScan := FALSE;
ELSE
    lrRawD := -(lrMeasurement - lrPrevMeasurement) / lrDt;
END_IF;
lrPrevMeasurement := lrMeasurement;

IF lrKp > 0.0 AND lrDerivFilterN > 0.0 THEN
    lrTf := lrKd / (lrKp * lrDerivFilterN);
ELSE
    lrTf := 0.0;
END_IF;
IF lrTf > 0.0 THEN
    lrAlpha := lrDt / (lrTf + lrDt);
ELSE
    lrAlpha := 1.0;
END_IF;
lrDState := lrDState + lrAlpha * (lrRawD - lrDState);

lrPTerm := lrKp * lrError;
lrDTerm := lrKd * lrDState;

(* --- Integral with conditional integration (anti-windup) ------------------ *)
lrCandidate := lrIntegral + lrKi * lrError * lrDt;
lrUnclamped := lrPTerm + lrCandidate + lrDTerm;
bSaturated  := (lrUnclamped > lrOutMax AND lrError > 0.0)
            OR (lrUnclamped < lrOutMin AND lrError < 0.0);

IF NOT (bAntiWindup AND bSaturated) THEN
    lrIntegral := lrCandidate;
END_IF;
IF bAntiWindup THEN
    (* Hard bound as a second line of defence against numerical drift *)
    lrIntegral := LIMIT(lrOutMin, lrIntegral, lrOutMax);
END_IF;

lrITerm  := lrIntegral;
lrOutput := LIMIT(lrOutMin, lrPTerm + lrIntegral + lrDTerm, lrOutMax);
END_FUNCTION_BLOCK
