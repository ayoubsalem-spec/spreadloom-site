// Atlas voice: deterministic JS test harness for the stale-network-turn
// race identified in release review.
//
// This is NOT a DOM/browser test (assistant.html has real DOM/MediaRecorder/
// AudioContext dependencies that aren't practical to run headlessly here).
// Instead, it faithfully reproduces the EXACT ownership-token pattern used
// in templates/assistant.html's streamFromServer()/triggerBargeIn()/
// endVoiceSession() -- same variable names (networkTurnCounter,
// myNetworkTurn, isStillCurrent(), turnInFlight, currentAbortController)
// and the same shape of check -- and drives it through a scripted race:
// Turn A starts, Turn B supersedes it (as a real barge-in would), then
// Turn A's late/stale callbacks fire and must be proven to do nothing.
//
// IMPORTANT: if streamFromServer()'s ownership-check logic in
// templates/assistant.html ever changes, this harness's reproduction of
// that logic must be updated to match, or it stops proving anything real.
//
// Usage:
//   node scripts/atlas_voice_race_test.js

let PASS = 0, FAIL = 0;
function check(label, condition) {
    if (condition) { PASS++; console.log('  OK  ' + label); }
    else { FAIL++; console.log('FAIL  ' + label); }
}

// ---- Faithful reproduction of the real shared state + ownership pattern ----
let networkTurnCounter = 0;
let turnInFlight = false;
let currentAbortController = null;
let vState = 'IDLE';
let errorsShown = [];
let autoResumeScheduled = 0;
let submissionsConfirmed = 0;

function setVoiceState(next) { vState = next; }

function startTurn(label) {
    turnInFlight = true;
    const myNetworkTurn = ++networkTurnCounter;
    const myAbortController = { aborted: false, abort() { this.aborted = true; } };
    currentAbortController = myAbortController;
    setVoiceState('PROCESSING');

    function isStillCurrent() { return myNetworkTurn === networkTurnCounter; }

    return {
        label,
        myNetworkTurn,
        myAbortController,
        isStillCurrent,
        // Mirrors handleEvent's 'done' branch in streamFromServer.
        fireDoneLate(pendingWriteToken) {
            if (!isStillCurrent()) return; // <-- the actual fix under test
            turnInFlight = false;
            setVoiceState('SPEAKING');
            if (pendingWriteToken) submissionsConfirmed++;
            autoResumeScheduled++;
        },
        // Mirrors the .catch() handler in streamFromServer.
        fireCatchLate(err) {
            if (!isStillCurrent()) return; // <-- the actual fix under test
            turnInFlight = false;
            if (err && err.name === 'AbortError') return;
            errorsShown.push(label + ': ' + err.message);
            setVoiceState('ERROR');
        },
    };
}

function triggerBargeIn(currentTurn) {
    // Mirrors the real triggerBargeIn(): bump the counter BEFORE
    // aborting, so any callback that fires after this point -- even one
    // already scheduled/in-flight -- is provably stale.
    networkTurnCounter++;
    if (currentAbortController) currentAbortController.abort();
    turnInFlight = false;
}

// ============================================================
// SCENARIO: Turn A starts, gets interrupted (barge-in) which starts
// Turn B, then Turn A's late network callbacks (done AND catch) fire
// after Turn B is already active. None of them may touch shared state.
// ============================================================
console.log('=== Race scenario: Turn A superseded by barge-in (Turn B), then Turn A calls back late ===');

const turnA = startTurn('TurnA');
check('Turn A is in flight after starting', turnInFlight === true);
check('vState is PROCESSING for Turn A', vState === 'PROCESSING');

triggerBargeIn(turnA);
check('barge-in aborted Turn A\'s controller', turnA.myAbortController.aborted === true);
check('barge-in cleared turnInFlight immediately (this is Turn A/B transition boundary, not the race itself)', turnInFlight === false);

// Turn B starts (the real code does this via startListening's callback -> startVoiceTurn/askServerWithAudioVoice -> streamFromServer)
const turnB = startTurn('TurnB');
check('Turn B is now in flight', turnInFlight === true);
check('Turn B has a DIFFERENT network turn number than Turn A', turnB.myNetworkTurn !== turnA.myNetworkTurn);
check('Turn A no longer considers itself current', turnA.isStillCurrent() === false);
check('Turn B does consider itself current', turnB.isStillCurrent() === true);

const beforeConfirmed = submissionsConfirmed;
const beforeResume = autoResumeScheduled;
const beforeErrors = errorsShown.length;
const stateBeforeStaleCallback = vState;
const turnInFlightBeforeStaleCallback = turnInFlight;

// Turn A's SSE 'done' event, sent by the server before it noticed the
// abort, finally arrives and its .then() callback runs -- LATE, after
// Turn B has already started.
turnA.fireDoneLate('some-write-token-that-should-never-be-honored');

check('stale Turn A done callback did NOT flip turnInFlight (Turn B still owns it)', turnInFlight === turnInFlightBeforeStaleCallback);
check('stale Turn A done callback did NOT change vState away from what Turn B set', vState === stateBeforeStaleCallback);
check('stale Turn A done callback did NOT confirm a submission', submissionsConfirmed === beforeConfirmed);
check('stale Turn A done callback did NOT schedule an auto-resume', autoResumeScheduled === beforeResume);

// Turn A's fetch .catch() (a real network error, arriving late) also fires.
turnA.fireCatchLate(new Error('stale network failure'));
check('stale Turn A catch callback did NOT flip turnInFlight', turnInFlight === turnInFlightBeforeStaleCallback);
check('stale Turn A catch callback did NOT display a stale error message', errorsShown.length === beforeErrors);
check('stale Turn A catch callback did NOT change vState to ERROR', vState !== 'ERROR');

// Now prove Turn B's OWN (non-stale) done callback DOES correctly work --
// i.e. the ownership check isn't accidentally blocking everything.
turnB.fireDoneLate(null);
check('Turn B\'s own done callback DOES flip turnInFlight back to false (proves the check isn\'t over-broad)', turnInFlight === false);
check('Turn B\'s own done callback DOES update vState', vState === 'SPEAKING');

console.log();
console.log('=== Duplicate submission guard: a stale callback cannot re-enable a second submit ===');
// Reset and prove specifically that a stale done event carrying a real
// pending_write_token does not get treated as confirmable by a NEWER
// turn's context -- confirmSubmission only increments when the ACTIVE
// turn's own done fires with a token, never a stale one's.
submissionsConfirmed = 0;
networkTurnCounter = 0;
const turnC = startTurn('TurnC');
triggerBargeIn(turnC);
const turnD = startTurn('TurnD');
turnC.fireDoneLate('stale-token-from-turn-C'); // must be ignored
check('a stale turn carrying a real write token is still ignored (no confirmation fired)', submissionsConfirmed === 0);
turnD.fireDoneLate('real-token-from-turn-D'); // the actually-current turn
check('the genuinely current turn\'s token IS honored', submissionsConfirmed === 1);

console.log();
console.log(`RESULT: ${PASS} passed, ${FAIL} failed`);
if (FAIL > 0) process.exit(1);
process.exit(0);
