// Atlas TEST-only diagnostics timer JS test harness.
//
// Faithfully reproduces the EXACT diagnostic-timer state/logic added to
// templates/assistant.html's streamFromServer() -- same variable names
// (atlasDiagTraceId, atlasDiagTimerHandle, atlasDiagStartMs,
// stopDiagTimer, diagStatusSuffix) and the same shape of check, driven
// with a fake clock/interval so it's deterministic and doesn't actually
// sleep.
//
// IMPORTANT: if the real diagnostic-timer logic in templates/assistant.html
// ever changes, this harness's reproduction must be updated to match.
//
// Usage:
//   node scripts/atlas_diagnostics_timer_test.js

let PASS = 0, FAIL = 0;
function check(label, condition) {
    if (condition) { PASS++; console.log('  OK  ' + label); }
    else { FAIL++; console.log('FAIL  ' + label); }
}

// ---- Fake clock/interval (deterministic, no real sleeping) ----
let fakeNowMs = 0;
let intervalCallbacks = new Map();
let nextIntervalId = 1;
function fakeSetInterval(fn) { const id = nextIntervalId++; intervalCallbacks.set(id, fn); return id; }
function fakeClearInterval(id) { intervalCallbacks.delete(id); }
function fakeTickSeconds(n) {
    for (let i = 0; i < n; i++) {
        fakeNowMs += 1000;
        for (const fn of intervalCallbacks.values()) fn();
    }
}

// ---- Faithful reproduction of the real shared state ----
let networkTurnCounter = 0;
let status = '';

function startTurn() {
    const myNetworkTurn = ++networkTurnCounter;
    function isStillCurrent() { return myNetworkTurn === networkTurnCounter; }

    let atlasDiagTraceId = null;
    let atlasDiagTimerHandle = null;
    let atlasDiagStartMs = null;
    function stopDiagTimer() {
        if (atlasDiagTimerHandle) { fakeClearInterval(atlasDiagTimerHandle); atlasDiagTimerHandle = null; }
    }
    function diagStatusSuffix() {
        if (!atlasDiagTraceId) return '';
        const elapsedS = (atlasDiagStartMs !== null) ? Math.floor((fakeNowMs - atlasDiagStartMs) / 1000) : 0;
        return ' ' + elapsedS + 's [' + atlasDiagTraceId + ']';
    }

    return {
        isStillCurrent,
        hasRunningTimer() { return atlasDiagTimerHandle !== null; },
        // Mirrors handleEvent's 'diagnostic' branch.
        receiveDiagnosticEvent(traceId) {
            if (!isStillCurrent()) return;
            atlasDiagTraceId = traceId;
            atlasDiagStartMs = fakeNowMs;
            status = 'Thinking...' + diagStatusSuffix();
            stopDiagTimer();
            atlasDiagTimerHandle = fakeSetInterval(function () {
                if (!isStillCurrent()) { stopDiagTimer(); return; }
                status = 'Thinking...' + diagStatusSuffix();
            });
        },
        // Mirrors handleEvent's 'done' branch (the stopDiagTimer() call added there).
        receiveRealDone() {
            if (!isStillCurrent()) return;
            stopDiagTimer();
            status = '';
        },
        // Mirrors pump()'s unexpected-EOF recovery branch.
        receiveUnexpectedEOF() {
            if (!isStillCurrent()) return;
            stopDiagTimer();
            status = '';
        },
        // Mirrors the .catch() rejection handler.
        receiveReject() {
            if (!isStillCurrent()) return;
            stopDiagTimer();
            status = '';
        },
        currentStatus() { return status; },
    };
}

console.log('=== Timer starts on diagnostic event, updates status with elapsed seconds ===');
fakeNowMs = 0; networkTurnCounter = 0; intervalCallbacks.clear(); nextIntervalId = 1; status = '';
{
    const turn = startTurn();
    turn.receiveDiagnosticEvent('abc123');
    check('timer is running after a diagnostic event', turn.hasRunningTimer());
    check('status shows 0s and the trace id immediately', turn.currentStatus() === 'Thinking... 0s [abc123]');
    fakeTickSeconds(4);
    check('status shows 4s after 4 fake ticks', turn.currentStatus() === 'Thinking... 4s [abc123]');
    fakeTickSeconds(8);
    check('status shows 12s after 12 total ticks', turn.currentStatus() === 'Thinking... 12s [abc123]');
}

console.log();
console.log('=== Timer stops on a real Atlas done event ===');
fakeNowMs = 0; networkTurnCounter = 0; intervalCallbacks.clear(); nextIntervalId = 1; status = '';
{
    const turn = startTurn();
    turn.receiveDiagnosticEvent('def456');
    fakeTickSeconds(3);
    turn.receiveRealDone();
    check('timer stopped after a real done event', !turn.hasRunningTimer());
    check('status cleared after done', turn.currentStatus() === '');
    const statusBeforeMoreTicks = turn.currentStatus();
    fakeTickSeconds(5); // should have NO effect -- timer is stopped
    check('further ticks after done do not resurrect the timer/status', turn.currentStatus() === statusBeforeMoreTicks);
}

console.log();
console.log('=== Timer stops on unexpected EOF ===');
fakeNowMs = 0; networkTurnCounter = 0; intervalCallbacks.clear(); nextIntervalId = 1; status = '';
{
    const turn = startTurn();
    turn.receiveDiagnosticEvent('eof789');
    fakeTickSeconds(2);
    turn.receiveUnexpectedEOF();
    check('timer stopped after unexpected EOF', !turn.hasRunningTimer());
    check('status cleared after unexpected EOF', turn.currentStatus() === '');
}

console.log();
console.log('=== Timer stops on reader.read() rejection ===');
fakeNowMs = 0; networkTurnCounter = 0; intervalCallbacks.clear(); nextIntervalId = 1; status = '';
{
    const turn = startTurn();
    turn.receiveDiagnosticEvent('rej000');
    fakeTickSeconds(1);
    turn.receiveReject();
    check('timer stopped after a reject', !turn.hasRunningTimer());
    check('status cleared after reject', turn.currentStatus() === '');
}

console.log();
console.log('=== Superseded turn cannot leave its old timer running or alter the newer turn\'s status ===');
fakeNowMs = 0; networkTurnCounter = 0; intervalCallbacks.clear(); nextIntervalId = 1; status = '';
{
    const turnA = startTurn();
    turnA.receiveDiagnosticEvent('turnA-id');
    fakeTickSeconds(2);
    check('(setup) turn A has a running timer before being superseded', turnA.hasRunningTimer());

    const turnB = startTurn(); // supersedes A -- networkTurnCounter bumped
    check('turn A is no longer current after being superseded', !turnA.isStillCurrent());

    // A's own interval callback fires again (it hasn't been explicitly
    // stopped yet) -- but its isStillCurrent() check inside the interval
    // callback itself must catch this and stop itself, never touching status.
    const statusBeforeStaleTick = status;
    fakeTickSeconds(1);
    check('a stale tick from superseded turn A did not change status (guarded by isStillCurrent inside the interval callback itself)',
          status === statusBeforeStaleTick);
    check('turn A\'s own timer self-stops once it detects it is no longer current', !turnA.hasRunningTimer());

    // Turn B now gets its own diagnostic event and behaves normally.
    turnB.receiveDiagnosticEvent('turnB-id');
    check('turn B can start its own independent timer', turnB.hasRunningTimer());
    check('turn B\'s status reflects ITS OWN trace id, not A\'s', status.indexOf('turnB-id') !== -1 && status.indexOf('turnA-id') === -1);
    turnB.receiveRealDone();
    check('turn B completes normally afterward', !turnB.hasRunningTimer() && status === '');
}

console.log(`\nRESULT: ${PASS} passed, ${FAIL} failed`);
if (FAIL > 0) process.exit(1);
