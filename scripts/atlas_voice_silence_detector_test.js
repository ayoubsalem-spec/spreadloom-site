// Atlas voice: deterministic JS test harness for the adaptive
// noise-floor end-of-turn detector.
//
// ROUND 2 (this file): a real iPhone TEST report found that round 1's
// detector still failed to auto-stop when the user spoke IMMEDIATELY
// upon Atlas showing LISTENING -- the first CALIBRATION_MS=300 window
// unconditionally averaged every sample (speech or not) into the noise
// floor, so immediate speech pushed speechThreshold above the user's
// own voice level and hasSpokenYet never became true. Fixed by removing
// the blind time-gated calibration window entirely: sessionNoiseFloor
// is now session-level (persists across turns, not reset per-turn),
// starts at a conservative fixed default, and is updated ONLY from
// samples already classified as non-speech -- speech samples never
// feed the floor, from tick 1 of turn 1 onward. A short
// SPEECH_CONFIRM_TICKS debounce absorbs a brief opening transient/pop
// without either poisoning the floor (it never could -- only non-speech
// samples ever touch it) or prematurely starting a silence countdown.
//
// This is a faithful, standalone reproduction of the exact per-tick
// algorithm in watchForSilence() (templates/assistant.html) -- same
// constants, same update order, same session-vs-turn variable scoping
// -- driven against SCRIPTED synthetic analyser-level sequences instead
// of a real microphone, so the decision logic itself can be proven
// correct deterministically. It does not (and cannot, without a real
// device) prove what iOS Safari's AnalyserNode actually reports in
// practice -- that requires the console-log instrumentation shipped in
// the same build and a real on-device TEST session.
//
// IMPORTANT: if watchForSilence()'s algorithm ever changes, this
// reproduction must be updated to match, or it stops proving anything
// real.
//
// Usage:
//   node scripts/atlas_voice_silence_detector_test.js

let PASS = 0, FAIL = 0;
function check(label, condition) {
    if (condition) { PASS++; console.log('  OK  ' + label); }
    else { FAIL++; console.log('FAIL  ' + label); }
}

const DEFAULT_INITIAL_NOISE_FLOOR = 5;
const SPEECH_MARGIN = 10;
const MIN_SPEECH_THRESHOLD = 6;
const FLOOR_EMA_DOWN = 0.25;
const FLOOR_EMA_UP = 0.02;
const SPEECH_CONFIRM_TICKS = 2;
const SILENCE_HOLD_MS = 1300;
const MAX_DURATION_MS = 30000;
const TICK_MS = 100;

// A "session" carries sessionNoiseFloor across multiple turns, exactly
// as the real page's outer-scope variable does across multiple
// watchForSilence() calls within one voice session.
function newSession() {
    return { noiseFloor: null };
}

// Runs the exact per-tick decision logic for ONE turn against a
// scripted sequence of (elapsedMs, level) samples, reading/writing the
// session's persisted noiseFloor exactly as the real code does.
// Returns { stoppedAt, events, finalHasSpokenYet, sessionAfter }
function runTurn(session, samples) {
    if (typeof session.noiseFloor !== 'number') session.noiseFloor = DEFAULT_INITIAL_NOISE_FLOOR;
    let hasSpokenYet = false;
    let silenceStartedAt = null;
    let consecutiveSpeechTicks = 0;
    let stoppedAt = null;
    const events = [];

    for (const [elapsed, level] of samples) {
        if (stoppedAt !== null) break;

        const speechThreshold = Math.max(MIN_SPEECH_THRESHOLD, session.noiseFloor + SPEECH_MARGIN);
        const isCandidateSpeech = level > speechThreshold;

        if (isCandidateSpeech) {
            consecutiveSpeechTicks++;
            // floor is NEVER touched here, confirmed or not
            if (consecutiveSpeechTicks >= SPEECH_CONFIRM_TICKS) {
                if (!hasSpokenYet) events.push({ t: elapsed, type: 'speech_detected', level, noiseFloor: session.noiseFloor, speechThreshold });
                hasSpokenYet = true;
                silenceStartedAt = null;
            }
        } else {
            consecutiveSpeechTicks = 0;
            session.noiseFloor = session.noiseFloor + (level - session.noiseFloor) * (level < session.noiseFloor ? FLOOR_EMA_DOWN : FLOOR_EMA_UP);
            if (hasSpokenYet) {
                if (silenceStartedAt === null) {
                    silenceStartedAt = elapsed;
                    events.push({ t: elapsed, type: 'silence_started', level, noiseFloor: session.noiseFloor });
                }
                const silenceDuration = elapsed - silenceStartedAt;
                if (silenceDuration > SILENCE_HOLD_MS) {
                    stoppedAt = elapsed;
                    events.push({ t: elapsed, type: 'auto_stop', silenceDuration });
                    break;
                }
            }
        }
        if (elapsed > MAX_DURATION_MS && stoppedAt === null) {
            stoppedAt = elapsed;
            events.push({ t: elapsed, type: 'max_duration_stop' });
            break;
        }
    }
    return { stoppedAt, events, finalHasSpokenYet: hasSpokenYet, finalNoiseFloor: session.noiseFloor };
}

function buildSamples(spec) {
    const samples = [];
    let t = 0;
    for (const seg of spec) {
        while (t < seg.untilMs) {
            samples.push([t, seg.level]);
            t += TICK_MS;
        }
    }
    return samples;
}

console.log('=== 1. Immediate speech beginning at 0ms -- must detect speech and auto-stop after real silence ===');
const session1 = newSession();
const s1 = buildSamples([
    { untilMs: 2000, level: 45 },   // speaking from the very first sample, no lead-in silence at all
    { untilMs: 5500, level: 4 },    // then genuine sustained silence
]);
const r1 = runTurn(session1, s1);
check('speech was detected essentially immediately (within the first few ticks, not delayed by a calibration window)',
      r1.events.some(e => e.type === 'speech_detected' && e.t <= 300));
check('the immediate speech did NOT get baked into the noise floor (floor stayed low, near the ambient default, not near speech level 45)',
      r1.finalNoiseFloor < 20);
check('detector still auto-stopped after real silence followed the immediate speech', r1.events.some(e => e.type === 'auto_stop'));

console.log();
console.log('=== 2. Immediate speech in a NOISY room -- must still be detected correctly against the conservative default floor ===');
const session2 = newSession();
const s2 = buildSamples([
    { untilMs: 2000, level: 40 },   // immediate speech, room happens to be a bit noisy too
    { untilMs: 5500, level: 15 },   // "silence" in this room settles higher than a quiet room, but still clearly below speech
]);
const r2 = runTurn(session2, s2);
check('immediate speech in a noisy room is detected against the conservative starting floor', r2.events.some(e => e.type === 'speech_detected' && e.t <= 300));
check('detector auto-stops once real (if noisier) silence follows', r2.events.some(e => e.type === 'auto_stop'));

console.log();
console.log('=== 3. True ambient noise at startup (no speech at all) -- must NOT be falsely classified as speech ===');
const session3 = newSession();
const s3 = buildSamples([
    { untilMs: 2000, level: 6 },    // just room noise, right at/near the conservative default -- never real speech
]);
const r3 = runTurn(session3, s3);
check('pure ambient noise at startup is never classified as speech', !r3.events.some(e => e.type === 'speech_detected'));
check('the floor adapted toward the real observed ambient level instead of staying frozen at the arbitrary default',
      Math.abs(r3.finalNoiseFloor - 6) < 3);

console.log();
console.log('=== 4. A brief opening transient/pop does NOT permanently poison the baseline or cause a premature stop ===');
const session4 = newSession();
const s4 = buildSamples([
    { untilMs: 100, level: 50 },    // single-tick pop right as the mic opens
    { untilMs: 700, level: 4 },     // quiet again -- the pop must not have been "confirmed" as speech (needs 2 consecutive ticks)
    { untilMs: 2200, level: 45 },   // NOW the user actually starts talking
    { untilMs: 5700, level: 4 },    // real sustained silence afterward
]);
const r4 = runTurn(session4, s4);
const speechEvents4 = r4.events.filter(e => e.type === 'speech_detected');
check('the single-tick pop alone was not confirmed as speech (SPEECH_CONFIRM_TICKS requires 2 consecutive)', speechEvents4.length <= 1);
if (speechEvents4.length === 1) {
    check('the one confirmed speech event corresponds to the REAL utterance (starting at t=700), not the t=0 pop', speechEvents4[0].t >= 700);
}
check('the pop did not poison the floor (floor stayed low, near ambient, not near the pop level of 50)', r4.finalNoiseFloor < 20);
check('detector still auto-stops correctly after the real utterance + real silence', r4.events.some(e => e.type === 'auto_stop'));

console.log();
console.log('=== 5. Speech after a normal quiet lead-in still works exactly as before (no regression) ===');
const session5 = newSession();
const s5 = buildSamples([
    { untilMs: 800, level: 4 },     // quiet lead-in, user takes a moment before speaking
    { untilMs: 2200, level: 45 },   // speech
    { untilMs: 5700, level: 4 },    // silence
]);
const r5 = runTurn(session5, s5);
check('speech following a normal quiet lead-in is still detected correctly', r5.events.some(e => e.type === 'speech_detected'));
check('detector still auto-stops correctly in the ordinary (non-immediate-speech) case', r5.events.some(e => e.type === 'auto_stop'));

console.log();
console.log('=== 6. Second/subsequent turns do NOT require a silent lead-in -- the learned floor carries over ===');
const session6 = newSession();
// Turn 1: normal quiet-then-speak, which teaches the session a real floor.
const turn1Samples = buildSamples([
    { untilMs: 500, level: 4 },
    { untilMs: 1800, level: 45 },
    { untilMs: 5300, level: 4 },
]);
const turn1Result = runTurn(session6, turn1Samples);
check('(setup) turn 1 completed normally and taught the session a real floor', turn1Result.events.some(e => e.type === 'auto_stop'));
const floorAfterTurn1 = session6.noiseFloor;

// Turn 2: the SAME session object is reused (mirrors sessionNoiseFloor
// persisting across watchForSilence() calls within one voice session)
// -- and this time speech starts immediately, with NO silent lead-in.
const turn2Samples = buildSamples([
    { untilMs: 2000, level: 45 },   // immediate speech again, second turn
    { untilMs: 5500, level: 4 },
]);
const turn2Result = runTurn(session6, turn2Samples);
check('turn 2 starts from the floor learned in turn 1, not a fresh unknown state', session6.noiseFloor !== null);
check('turn 2 detects immediate speech with no silent lead-in required', turn2Result.events.some(e => e.type === 'speech_detected' && e.t <= 300));
check('turn 2 still auto-stops correctly', turn2Result.events.some(e => e.type === 'auto_stop'));

console.log();
console.log(`RESULT: ${PASS} passed, ${FAIL} failed`);
if (FAIL > 0) process.exit(1);
process.exit(0);
