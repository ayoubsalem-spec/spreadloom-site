// Atlas terminal-stream-contract JS test harness (release review:
// "client treats unexpected EOF as success" fix).
//
// This is NOT a DOM/browser test (assistant.html has real DOM/fetch/
// ReadableStream dependencies impractical to run headlessly here).
// Instead it faithfully reproduces the EXACT pump()/handleEvent() logic
// from templates/assistant.html's streamFromServer() -- same variable
// names (terminalDoneReceived, isStillCurrent, turnInFlight, sendBtn
// state, networkTurnCounter) and the same shape of check -- and drives
// it through scripted event sequences to prove the fixed contract:
// an unexpected EOF (result.done === true) WITHOUT a prior real Atlas
// 'done' event now safely clears loading state and shows one error,
// instead of leaving the UI permanently "Thinking...".
//
// IMPORTANT: if pump()/handleEvent()'s logic in templates/assistant.html
// ever changes, this harness's reproduction must be updated to match,
// or it stops proving anything real.
//
// Usage:
//   node scripts/atlas_stream_terminal_contract_test.js

let PASS = 0, FAIL = 0;
function check(label, condition) {
    if (condition) { PASS++; console.log('  OK  ' + label); }
    else { FAIL++; console.log('FAIL  ' + label); }
}

// ---- Faithful reproduction of the real shared state + pump()/handleEvent() ----
let networkTurnCounter = 0;
let turnInFlight = false;
let sendBtnDisabled = false;
let status = '';
let typingRemoved = false;
let errorsShown = [];
let voiceStateSet = null;

function resetSharedState() {
    turnInFlight = false;
    sendBtnDisabled = false;
    status = '';
    typingRemoved = false;
    errorsShown = [];
    voiceStateSet = null;
}

function startTurn(voiceSessionActive) {
    turnInFlight = true;
    const myNetworkTurn = ++networkTurnCounter;
    status = 'Thinking...';
    sendBtnDisabled = true;

    function isStillCurrent() { return myNetworkTurn === networkTurnCounter; }

    let terminalDoneReceived = false; // mirrors the new variable in assistant.html exactly

    return {
        myNetworkTurn,
        isStillCurrent,
        // Mirrors handleEvent's 'done' branch.
        fireRealDone() {
            if (!isStillCurrent()) return;
            terminalDoneReceived = true;
            status = '';
            sendBtnDisabled = false;
            turnInFlight = false;
        },
        // Mirrors pump()'s result.done handling -- THE FIX under test.
        fireUnexpectedEOF() {
            if (!isStillCurrent()) return;
            if (!terminalDoneReceived && isStillCurrent()) {
                typingRemoved = true;
                status = '';
                sendBtnDisabled = false;
                turnInFlight = false;
                errorsShown.push('Something went wrong reaching the assistant. Please try again.');
                if (voiceSessionActive) voiceStateSet = 'ERROR';
            }
        },
        // Mirrors the existing .catch() handler (unchanged by this fix).
        fireReject() {
            if (!isStillCurrent()) return;
            typingRemoved = true;
            status = '';
            sendBtnDisabled = false;
            turnInFlight = false;
            errorsShown.push('Something went wrong reaching the assistant.');
        },
    };
}

console.log('=== A. Normal stream: real events + real done -> no false error, loading clears normally ===');
resetSharedState();
{
    const turn = startTurn(false);
    turn.fireRealDone();
    check('A. no error was shown', errorsShown.length === 0);
    check('A. status cleared', status === '');
    check('A. sendBtn re-enabled', sendBtnDisabled === false);
    check('A. turnInFlight cleared', turnInFlight === false);
}

console.log();
console.log('=== B. Unexpected EOF without a real done event -> loading clears, one safe error shown ===');
resetSharedState();
{
    const turn = startTurn(false);
    turn.fireUnexpectedEOF();
    check('B. typing indicator removed', typingRemoved === true);
    check('B. status cleared', status === '');
    check('B. send button re-enabled', sendBtnDisabled === false);
    check('B. turnInFlight cleared', turnInFlight === false);
    check('B. exactly one safe, retryable error shown', errorsShown.length === 1 && /try again/i.test(errorsShown[0]));
    check('B. the error message contains no raw exception/technical detail', !/Exception|Traceback|RequestException/i.test(errorsShown[0]));
}

console.log();
console.log('=== B2. Legitimate done already received -> unexpected-EOF path does NOT double-fire ===');
resetSharedState();
{
    const turn = startTurn(false);
    turn.fireRealDone();
    turn.fireUnexpectedEOF(); // stream naturally reaching EOF after a real done -- must be a no-op
    check('B2. no duplicate/spurious error shown after a legitimate done', errorsShown.length === 0);
}

console.log();
console.log('=== C. reader.read() rejects -> existing catch() behavior still works unchanged ===');
resetSharedState();
{
    const turn = startTurn(false);
    turn.fireReject();
    check('C. typing indicator removed', typingRemoved === true);
    check('C. status cleared', status === '');
    check('C. send button re-enabled', sendBtnDisabled === false);
    check('C. turnInFlight cleared', turnInFlight === false);
    check('C. an error was shown via the existing catch-path message', errorsShown.length === 1);
}

console.log();
console.log("=== D. Turn superseded before unexpected EOF -> old stream must NOT alter the new turn's UI state ===");
resetSharedState();
{
    const turnA = startTurn(false);
    const turnB = startTurn(false); // supersedes A -- networkTurnCounter bumped, exactly like triggerBargeIn()/a new question
    check('D. (setup) turn A is no longer current', turnA.isStillCurrent() === false);
    check('D. (setup) turn B is current', turnB.isStillCurrent() === true);

    const stateBeforeStaleEOF = { status, sendBtnDisabled, turnInFlight, errorsShownCount: errorsShown.length };
    turnA.fireUnexpectedEOF(); // A's own stream finally reaches EOF late, after B already started
    check("D. superseded turn A's late EOF changed NOTHING about B's active state",
          status === stateBeforeStaleEOF.status &&
          sendBtnDisabled === stateBeforeStaleEOF.sendBtnDisabled &&
          turnInFlight === stateBeforeStaleEOF.turnInFlight &&
          errorsShown.length === stateBeforeStaleEOF.errorsShownCount);

    // B's own legitimate completion must still work normally afterward.
    turnB.fireRealDone();
    check("D. turn B still completes normally despite A's stale late EOF", turnInFlight === false && status === '');
}

console.log();
console.log('=== D2. Voice session active during unexpected EOF -> voice state moves to ERROR for recovery ===');
resetSharedState();
{
    const turn = startTurn(true); // voiceSessionActive = true
    turn.fireUnexpectedEOF();
    check('D2. voice state set to ERROR so the voice loop can recover, not stay silently stuck', voiceStateSet === 'ERROR');
}

console.log(`\nRESULT: ${PASS} passed, ${FAIL} failed`);
if (FAIL > 0) process.exit(1);
