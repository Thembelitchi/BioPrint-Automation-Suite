(* =============================================================================
   FB_WestgardMonitor_Test — TcUnit test suite
   Replays the Python reference samples and checks every rule flag and the SD.
   ============================================================================= *)
FUNCTION_BLOCK FB_WestgardMonitor_Test EXTENDS TcUnit.FB_TestSuite
VAR
    fbQc      : FB_WestgardMonitor;
    i         : INT;
    nMismatch : INT;
    iFirst    : INT;
END_VAR

TEST('Westgard_ReplaysPythonReferenceVectors');

fbQc(bReset := TRUE, diStep := -1);
fbQc.bReset := FALSE;

nMismatch := 0; iFirst := 0;
FOR i := 1 TO GVL_ReferenceVectors.N_VECTORS DO
    fbQc(lrValue := GVL_ReferenceVectors.aWestgard[i].lrValue,
         diStep  := GVL_ReferenceVectors.aWestgard[i].diStep);
    IF fbQc.bWarn12s   <> GVL_ReferenceVectors.aWestgard[i].bWarn12s
    OR fbQc.bReject13s <> GVL_ReferenceVectors.aWestgard[i].bReject13s
    OR fbQc.bReject22s <> GVL_ReferenceVectors.aWestgard[i].bReject22s THEN
        nMismatch := nMismatch + 1;
        IF iFirst = 0 THEN iFirst := i; END_IF;
    END_IF;
END_FOR;

AssertEquals_LREAL(Expected := GVL_ReferenceVectors.LR_BASELINE_SD, Actual := fbQc.lrBaselineSd,
    Delta := 1.0E-9, Message := 'baseline SD differs from Python');
AssertEquals_INT(Expected := 0, Actual := nMismatch,
    Message := CONCAT('rule flags differ from Python; first sample ', INT_TO_STRING(iFirst)));
AssertTrue(Condition := fbQc.bRunRejected, Message := 'disturbance should reject the run');
TEST_FINISHED();
END_FUNCTION_BLOCK
