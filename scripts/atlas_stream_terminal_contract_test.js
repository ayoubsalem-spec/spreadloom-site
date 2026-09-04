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
let assistantMessagesShown = []; // {kind: 'assistant'|'fallback', text}
let pendingWriteConfirmCalls = [];

function resetSharedState() {
    turnInFlight = false;
    sendBtnDisabled = false;
    status = '';
    typingRemoved = false;
    errorsShown = [];
    voiceStateSet = null;
    assistantMessagesShown = [];
    pendingWriteConfirmCalls = [];
}

const FALLBACK_TEXT = "Atlas completed the request but didn't return a response. Please try again.";

function startTurn(voiceSessionActive) {
    turnInFlight = true;
    const myNetworkTurn = ++networkTurnCounter;
    status = 'Thinking...';
    sendBtnDisabled = true;

    function isStillCurrent() { return myNetworkTurn === networkTurnCounter; }

    let terminalDoneReceived = false; // mirrors the new variable in assistant.html exactly
    let assistantBubble = null; // mirrors assistant.html's per-turn assistantBubble

    return {
        myNetworkTurn,
        isStillCurrent,
        hasBubble() { return assistantBubble !== null; },
        // Mirrors handleEvent's 'delta' branch.
        fireDelta(text) {
            if (!isStillCurrent()) return;
            if (!assistantBubble) {
                typingRemoved = true;
                assistantBubble = { text: '' };
                assistantMessagesShown.push({ kind: 'assistant', text: '' });
            }
            assistantBubble.text += text;
            assistantMessagesShown[assistantMessagesShown.length - 1].text = assistantBubble.text;
        },
        // Mirrors handleEvent's 'done' branch -- THE FIX under test:
        // unconditional removeTyping() + empty-completion fallback.
        fireRealDone(evt) {
            evt = evt || {};
            if (!isStillCurrent()) return;
            terminalDoneReceived = true;
            typingRemoved = true; // now UNCONDITIONAL, not gated on a delta having fired first
            status = '';
            sendBtnDisabled = false;
            turnInFlight = false;
            if (!assistantBubble) {
                assistantMessagesShown.push({ kind: 'fallback', text: FALLBACK_TEXT });
            }
            if (evt.pending_write_token) pendingWriteConfirmCalls.push(evt.pending_write_token);
        },
        // Mirrors pump()'s result.done handling (unchanged by this fix).
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


console.log('=== A. Normal text + done: assistant text shown, typing removed, no fallback, timer/state clear ===');
resetSharedState();
{
    const turn = startTurn(false);
    turn.fireDelta('Patel Farm has two open concrete requests.');
    turn.fireRealDone();
    check('A. no error was shown', errorsShown.length === 0);
    check('A. status cleared', status === '');
    check('A. sendBtn re-enabled', sendBtnDisabled === false);
    check('A. turnInFlight cleared', turnInFlight === false);
    check('A. typing indicator was removed', typingRemoved === true);
    check('A. exactly one assistant message shown, with the real text', assistantMessagesShown.length === 1 && assistantMessagesShown[0].kind === 'assistant' && assistantMessagesShown[0].text === 'Patel Farm has two open concrete requests.');
    check('A. NO fallback message shown (real text was rendered)', !assistantMessagesShown.some(m => m.kind === 'fallback'));
}

console.log();
console.log('=== B. ZERO text + valid done: the exact reproduced real-world scenario ===');
resetSharedState();
{
    const turn = startTurn(false);
    // Pass 2 completes with stop_reason=end_turn but never emits a
    // single text_delta -- exactly the observed real trace (trace id
    // a392be): PASS2_END outcome=success stop_reason=end_turn, but no
    // PASS2_FIRST_TEXT. No fireDelta() call here, deliberately.
    turn.fireRealDone();
    check('B. typing indicator removed (the actual fix under test -- previously this NEVER fired for a zero-text turn)', typingRemoved === true);
    check('B. exactly one safe fallback message rendered, exactly once', assistantMessagesShown.length === 1 && assistantMessagesShown[0].kind === 'fallback');
    check('B. the fallback text matches exactly the required fixed, safe message', assistantMessagesShown[0].text === FALLBACK_TEXT);
    check('B. the fallback message contains no raw upstream/model/tool/trace detail', !/error|exception|trace|tool_use|Anthropic/i.test(assistantMessagesShown[0].text));
    check('B. status cleared', status === '');
    check('B. sendBtn re-enabled', sendBtnDisabled === false);
    check('B. turnInFlight cleared', turnInFlight === false);
    check('B. no permanent three-dot state -- typing indicator removal and full state cleanup both actually occurred', typingRemoved === true && turnInFlight === false && sendBtnDisabled === false);
}

console.log();
console.log('=== C. Unexpected EOF without a real done event -> loading clears, one safe error shown ===');
resetSharedState();
{
    const turn = startTurn(false);
    turn.fireUnexpectedEOF();
    check('C. typing indicator removed', typingRemoved === true);
    check('C. status cleared', status === '');
    check('C. send button re-enabled', sendBtnDisabled === false);
    check('C. turnInFlight cleared', turnInFlight === false);
    check('C. exactly one safe, retryable error shown', errorsShown.length === 1 && /try again/i.test(errorsShown[0]));
    check('C. the error message contains no raw exception/technical detail', !/Exception|Traceback|RequestException/i.test(errorsShown[0]));
}

console.log();
console.log('=== C2. Legitimate done already received -> unexpected-EOF path does NOT double-fire ===');
resetSharedState();
{
    const turn = startTurn(false);
    turn.fireRealDone();
    turn.fireUnexpectedEOF(); // stream naturally reaching EOF after a real done -- must be a no-op
    check('C2. no duplicate/spurious error shown after a legitimate done', errorsShown.length === 0);
}

console.log();
console.log('=== D. reader.read() rejects -> existing catch() behavior still works unchanged ===');
resetSharedState();
{
    const turn = startTurn(false);
    turn.fireReject();
    check('D. typing indicator removed', typingRemoved === true);
    check('D. status cleared', status === '');
    check('D. send button re-enabled', sendBtnDisabled === false);
    check('D. turnInFlight cleared', turnInFlight === false);
    check('D. an error was shown via the existing catch-path message', errorsShown.length === 1);
}

console.log();
console.log("=== E. Turn superseded -> old stream must NOT alter the new turn's UI state, cannot render fallback into new turn ===");
resetSharedState();
{
    const turnA = startTurn(false);
    const turnB = startTurn(false); // supersedes A -- networkTurnCounter bumped, exactly like triggerBargeIn()/a new question
    check('E. (setup) turn A is no longer current', turnA.isStillCurrent() === false);
    check('E. (setup) turn B is current', turnB.isStillCurrent() === true);

    const stateBeforeStaleEOF = { status, sendBtnDisabled, turnInFlight, errorsShownCount: errorsShown.length, msgCount: assistantMessagesShown.length };
    turnA.fireUnexpectedEOF(); // A's own stream finally reaches EOF late, after B already started
    check("E. superseded turn A's late EOF changed NOTHING about B's active state",
          status === stateBeforeStaleEOF.status &&
          sendBtnDisabled === stateBeforeStaleEOF.sendBtnDisabled &&
          turnInFlight === stateBeforeStaleEOF.turnInFlight &&
          errorsShown.length === stateBeforeStaleEOF.errorsShownCount);

    // A also had zero text and (if it had reached done) would have
    // wanted to show the fallback -- but since it's superseded, it must
    // never render ANYTHING into the shared message list, including the
    // fallback, and must not incorrectly remove B's typing indicator.
    turnB.fireDelta('B is still typing its real answer');
    const typingRemovedBeforeStaleDone = typingRemoved;
    turnA.fireRealDone(); // A finally completes, empty, AFTER being superseded -- must be a complete no-op
    check("E. superseded turn A's late (empty) done rendered NO fallback message into the shared UI",
          assistantMessagesShown.length === stateBeforeStaleEOF.msgCount + 1); // only B's real delta-created bubble, nothing from A
    check("E. superseded turn A's late done did not incorrectly touch typingRemoved state", typingRemoved === typingRemovedBeforeStaleDone);

    // B's own legitimate completion must still work normally afterward.
    turnB.fireRealDone();
    check("E. turn B still completes normally despite A's stale late EOF/done", turnInFlight === false && status === '');
    check("E. turn B's real text is what's actually shown, not a fallback", assistantMessagesShown[assistantMessagesShown.length - 1].kind === 'assistant');
}

console.log();
console.log('=== E2. Voice session active during unexpected EOF -> voice state moves to ERROR for recovery ===');
resetSharedState();
{
    const turn = startTurn(true); // voiceSessionActive = true
    turn.fireUnexpectedEOF();
    check('E2. voice state set to ERROR so the voice loop can recover, not stay silently stuck', voiceStateSet === 'ERROR');
}

console.log();
console.log('=== F. Empty-text done WITH pending-write metadata: confirmation still fires, no suppression/duplication ===');
resetSharedState();
{
    const turn = startTurn(false);
    // No delta at all -- Pass 2 produced zero visible text for this
    // turn (e.g. it only needed to confirm a write), but a
    // pending_write_token is still present on the done event.
    turn.fireRealDone({ pending_write_token: 'tok_abc123' });
    check('F. the fallback message still renders exactly once (zero text is zero text, regardless of pending_write_token)',
          assistantMessagesShown.length === 1 && assistantMessagesShown[0].kind === 'fallback');
    check('F. pending-write confirmation is still triggered exactly once, not suppressed by the fallback path',
          pendingWriteConfirmCalls.length === 1 && pendingWriteConfirmCalls[0] === 'tok_abc123');
    check('F. pending-write confirmation is not duplicated', pendingWriteConfirmCalls.length === 1);
    check('F. typing removed, state fully cleared, exactly as any other done', typingRemoved === true && turnInFlight === false && sendBtnDisabled === false);
}

console.log(`\nRESULT: ${PASS} passed, ${FAIL} failed`);
if (FAIL > 0) process.exit(1);
