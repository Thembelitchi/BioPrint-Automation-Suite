(* =============================================================================
   FB_BioPrintPID_Test — TcUnit test suite (TwinCAT 3; CODESYS via the TcUnit port)
   Replays the Python reference inputs scan by scan and checks every output.
   Instantiate in a PRG and call it cyclically, then call TcUnit.RUN().
   ============================================================================= *)
FUNCTION_BLOCK FB_BioPrintPID_Test EXTENDS TcUnit.FB_TestSuite
VAR
    fbPid       : FB_BioPrintPID;
    i           : INT;
    nMismatch   : INT;
    iFirst      : INT;
    lrWorst     : LREAL;
    lrDiff      : LREAL;
END_VAR

TEST('PID_ReplaysPythonReferenceVectors');

(* Start from a clean controller, defaults match config/default.toml *)
fbPid(bReset := TRUE);
fbPid.bReset := FALSE;

nMismatch := 0; iFirst := 0; lrWorst := 0.0;
FOR i := 1 TO GVL_ReferenceVectors.N_VECTORS DO
    fbPid(lrSetpoint    := GVL_ReferenceVectors.aPid[i].lrSetpoint,
          lrMeasurement := GVL_ReferenceVectors.aPid[i].lrMeasurement);
    lrDiff := ABS(fbPid.lrOutput - GVL_ReferenceVectors.aPid[i].lrOutput);
    lrWorst := MAX(lrWorst, lrDiff);
    IF lrDiff > 1.0E-6 THEN
        nMismatch := nMismatch + 1;
        IF iFirst = 0 THEN iFirst := i; END_IF;
    END_IF;
END_FOR;

AssertEquals_INT(Expected := 0, Actual := nMismatch,
    Message := CONCAT('output differs from Python by >1e-6 % duty; first scan ', INT_TO_STRING(iFirst)));
TEST_FINISHED();

TEST('PID_AntiWindupBoundsIntegrator');
fbPid(bReset := TRUE);
fbPid.bReset := FALSE;
FOR i := 1 TO 100 DO   (* 10 s pinned at 100 % with a 205 °C error *)
    fbPid(lrSetpoint := 230.0, lrMeasurement := 25.0);
END_FOR;
AssertTrue(Condition := fbPid.lrITerm <= 100.0, Message := 'integrator wound up past the output limit');
AssertTrue(Condition := fbPid.bSaturated, Message := 'saturation flag not set');
TEST_FINISHED();
END_FUNCTION_BLOCK
