// Atlas voice: deterministic JS test harness for the end-of-turn
// silence/ambient detector.
//
// ROUND 4 (this file): release review reproduced round 3's
// rolling-percentile-window detector against the real iPhone fixture
// and found it technically recovers from round 2's deadlock, but far
// too slowly for conversational voice (~6.7s of post-speech latency,
// bounded by the whole AMBIENT_WINDOW_MS=8000 window needing to evict
// its pre-seeded defaults before the ambient estimate could catch up).
// It also flagged that a noisy startup could let persistent ambient
// ALONE eventually cross SPEECH_CONFIRM_TICKS while that window was
// still catching up, incorrectly setting hasSpokenYet=true from
// ambient alone -- unacceptable even if it would "eventually recover".
//
// This file reproduces THREE algorithms, faithfully, against shared
// deterministic fixtures:
//   - runTurnRound2(): the original gated-EMA floor ("only update from
//     samples already classified as non-speech"). Kept ONLY to prove,
//     deterministically, that it deadlocks on the real bug fixture.
//   - runTurnRound3(): the rolling-percentile-window estimator. Kept
//     ONLY to prove, deterministically, that it recovers but with
//     unacceptable (~6-7s) latency -- this is what this round replaces.
//   - runTurnNew(): the corrected round-4 design, matching
//     watchForSilence() in templates/assistant.html tick-for-tick.
//     Neither prior algorithm is used anywhere in the real app anymore.
//
// runTurnNew()'s design explicitly separates three concerns, per the
// release review's request:
//   1. SPEECH ONSET DETECTION -- a level must clear the CURRENT
//      threshold for SPEECH_CONFIRM_TICKS consecutive polls (unchanged
//      debounce from every prior round).
//   2. AMBIENT ADAPTATION -- sessionNoiseFloor moves toward the raw
//      level by a BOUNDED ABSOLUTE STEP (FLOOR_ADAPT_STEP) each tick,
//      gated on CONFIRMATION state (hasSpokenYet) rather than on each
//      tick's own candidate classification: it keeps adapting, every
//      tick, for as long as speech has NOT yet been confirmed in this
//      turn. A fixed step (not a percentage of the gap, i.e. not a
//      plain EMA) closes a SMALL gap (a wrong default vs. real
//      ambient, ~10-20) within just 2-3 ticks -- fast enough to break
//      round 2's deadlock -- while the SAME fixed step closes a LARGE
//      gap (floor vs. genuine speech, ~35-45) far more slowly, so a
//      loud utterance can't get "caught" by its own onset within a
//      couple of ticks the way a proportional rate would. Once
//      confirmed, adaptation freezes for the rest of that active
//      utterance -- protecting sustained real speech from being
//      absorbed into the floor.
//   3. CONFIRMED-USER-SPEECH STATE -- only entered after
//      SPEECH_CONFIRM_TICKS consecutive candidate ticks against a
//      CONTINUOUSLY-UPDATING threshold -- persistent ambient that
//      never actually rises above the floor's own adapting estimate of
//      itself cannot satisfy that within the available ticks, because
//      the bounded step closes that (small) gap before
//      SPEECH_CONFIRM_TICKS is reached.
//
// If watchForSilence()'s algorithm ever changes again, this
// reproduction must be updated to match, or it stops proving anything
// real.
//
// SECTION 0e (added this pass): release review found the "real iPhone
// fixture" above used an idealized speech level (45) that does not
// match the device's ACTUAL reported speech amplitude (15.3-16.7,
// overlapping with -- and sometimes quieter than -- the device's own
// reported ambient range of 15-26). Section 0e tests against the
// actual reported numbers and runs an exhaustive grid search over
// FLOOR_ADAPT_STEP/SPEECH_MARGIN/SPEECH_CONFIRM_TICKS to answer
// whether any tuning of those constants can resolve it. It cannot:
// when ambient's own peak is louder than the target speech, no
// amplitude-only threshold on this signal can separate them. That
// finding is asserted directly (not just described) and TEST-only RMS
// instrumentation has been added to assistant.html so a real-device
// session can capture comparison data before any detector change.
//
// Usage:
//   node scripts/atlas_voice_silence_detector_test.js

let PASS = 0, FAIL = 0;
function check(label, condition) {
    if (condition) { PASS++; console.log('  OK  ' + label); }
    else { FAIL++; console.log('FAIL  ' + label); }
}

const TICK_MS = 100;
const DEFAULT_INITIAL_NOISE_FLOOR = 5;
const SPEECH_MARGIN = 10;
const MIN_SPEECH_THRESHOLD = 6;
const SPEECH_CONFIRM_TICKS = 2;
const SILENCE_HOLD_MS = 1300;
const MAX_DURATION_MS = 30000;

// Product requirement (this round): once real speech has actually
// ended, Atlas should recognize end-of-turn on roughly the same
// timescale as SILENCE_HOLD_MS itself, not several seconds later. Give
// some headroom above SILENCE_HOLD_MS for confirm-tick overhead and
// polling granularity, but this must stay firmly in "conversational"
// territory -- nowhere close to round 3's ~6.7s.
const MAX_ACCEPTABLE_POST_SPEECH_LATENCY_MS = 2500;

// ---- Round 2 constants (gated EMA -- reproduction only) ----
const FLOOR_EMA_DOWN_R2 = 0.25;
const FLOOR_EMA_UP_R2 = 0.02;

// ---- Round 3 constants (rolling-percentile window -- reproduction only) ----
const AMBIENT_WINDOW_MS_R3 = 8000;
const AMBIENT_PERCENTILE_R3 = 0.15;
const AMBIENT_WINDOW_TICKS_R3 = Math.round(AMBIENT_WINDOW_MS_R3 / TICK_MS);

// ---- Round 4 constants (bounded-step, confirmation-gated) --
// MUST match templates/assistant.html's watchForSilence() exactly.
const FLOOR_ADAPT_STEP = 7;

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

function tileCycle(cycleLevels, fromMs, toMs) {
    const samples = [];
    let i = 0;
    for (let t = fromMs; t < toMs; t += TICK_MS) {
        samples.push([t, cycleLevels[i % cycleLevels.length]]);
        i++;
    }
    return samples;
}

// ============================================================
// ROUND 2 -- gated EMA. Session shape: { noiseFloor }.
// ============================================================
function newRound2Session() { return { noiseFloor: null }; }

function runTurnRound2(session, samples) {
    if (typeof session.noiseFloor !== 'number') session.noiseFloor = DEFAULT_INITIAL_NOISE_FLOOR;
    let hasSpokenYet = false, silenceStartedAt = null, consecutiveSpeechTicks = 0, stoppedAt = null;
    const events = [];
    for (const [elapsed, level] of samples) {
        if (stoppedAt !== null) break;
        const speechThreshold = Math.max(MIN_SPEECH_THRESHOLD, session.noiseFloor + SPEECH_MARGIN);
        const isCandidateSpeech = level > speechThreshold;
        if (isCandidateSpeech) {
            consecutiveSpeechTicks++;
            if (consecutiveSpeechTicks >= SPEECH_CONFIRM_TICKS) {
                if (!hasSpokenYet) events.push({ t: elapsed, type: 'speech_detected' });
                hasSpokenYet = true;
                silenceStartedAt = null;
            }
        } else {
            consecutiveSpeechTicks = 0;
            session.noiseFloor = session.noiseFloor + (level - session.noiseFloor) * (level < session.noiseFloor ? FLOOR_EMA_DOWN_R2 : FLOOR_EMA_UP_R2);
            if (hasSpokenYet) {
                if (silenceStartedAt === null) { silenceStartedAt = elapsed; events.push({ t: elapsed, type: 'silence_started' }); }
                const silenceDuration = elapsed - silenceStartedAt;
                if (silenceDuration > SILENCE_HOLD_MS) { stoppedAt = elapsed; events.push({ t: elapsed, type: 'auto_stop', silenceDuration }); break; }
            }
        }
        if (elapsed > MAX_DURATION_MS && stoppedAt === null) { stoppedAt = elapsed; events.push({ t: elapsed, type: 'max_duration_stop' }); break; }
    }
    return { stoppedAt, events, finalHasSpokenYet: hasSpokenYet, finalNoiseFloor: session.noiseFloor };
}

// ============================================================
// ROUND 3 -- rolling-percentile window. Session shape: { window }.
// ============================================================
function newRound3Session() { return { window: null }; }

function computePercentileEstimate(window) {
    const sorted = window.slice().sort((a, b) => a - b);
    const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor(AMBIENT_PERCENTILE_R3 * (sorted.length - 1))));
    return sorted[idx];
}

function runTurnRound3(session, samples) {
    if (!session.window) {
        session.window = [];
        for (let i = 0; i < AMBIENT_WINDOW_TICKS_R3; i++) session.window.push(DEFAULT_INITIAL_NOISE_FLOOR);
    }
    let hasSpokenYet = false, silenceStartedAt = null, consecutiveSpeechTicks = 0, stoppedAt = null;
    const events = [];
    for (const [elapsed, level] of samples) {
        if (stoppedAt !== null) break;
        session.window.push(level);
        if (session.window.length > AMBIENT_WINDOW_TICKS_R3) session.window.shift();
        const ambientEstimate = computePercentileEstimate(session.window);
        const speechThreshold = Math.max(MIN_SPEECH_THRESHOLD, ambientEstimate + SPEECH_MARGIN);
        const isCandidateSpeech = level > speechThreshold;
        if (isCandidateSpeech) {
            consecutiveSpeechTicks++;
            if (consecutiveSpeechTicks >= SPEECH_CONFIRM_TICKS) {
                if (!hasSpokenYet) events.push({ t: elapsed, type: 'speech_detected' });
                hasSpokenYet = true;
                silenceStartedAt = null;
            }
        } else {
            consecutiveSpeechTicks = 0;
            if (hasSpokenYet) {
                if (silenceStartedAt === null) { silenceStartedAt = elapsed; events.push({ t: elapsed, type: 'silence_started' }); }
                const silenceDuration = elapsed - silenceStartedAt;
                if (silenceDuration > SILENCE_HOLD_MS) { stoppedAt = elapsed; events.push({ t: elapsed, type: 'auto_stop', silenceDuration }); break; }
            }
        }
        if (elapsed > MAX_DURATION_MS && stoppedAt === null) { stoppedAt = elapsed; events.push({ t: elapsed, type: 'max_duration_stop' }); break; }
    }
    return { stoppedAt, events, finalHasSpokenYet: hasSpokenYet };
}

// ============================================================
// ROUND 4 -- bounded-step, confirmation-gated. Must match
// watchForSilence() in templates/assistant.html tick-for-tick. Session
// shape: { noiseFloor }.
// ============================================================
function newSession() { return { noiseFloor: null }; }

function stepFloorToward(session, level) {
    if (level > session.noiseFloor) session.noiseFloor = Math.min(session.noiseFloor + FLOOR_ADAPT_STEP, level);
    else session.noiseFloor = Math.max(session.noiseFloor - FLOOR_ADAPT_STEP, level);
}

function runTurnNew(session, samples) {
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
            if (!hasSpokenYet) stepFloorToward(session, level);
            if (consecutiveSpeechTicks >= SPEECH_CONFIRM_TICKS) {
                if (!hasSpokenYet) events.push({ t: elapsed, type: 'speech_detected', level, noiseFloor: session.noiseFloor, speechThreshold });
                hasSpokenYet = true;
                silenceStartedAt = null;
            }
        } else {
            consecutiveSpeechTicks = 0;
            stepFloorToward(session, level);
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

// Returns the timestamp of the LAST sample at/above `speechLevel` that
// occurs before the given event -- i.e. "when did the person actually
// stop talking", not "when did recording start". Used to measure real
// post-speech response latency rather than total elapsed time.
function lastSpeechSampleBefore(samples, speechLevel, beforeT) {
    let last = null;
    for (const [t, level] of samples) {
        if (t >= beforeT) break;
        if (level >= speechLevel) last = t;
    }
    return last;
}

// ============================================================
// 0. THE ACTUAL REPORTED BUG -- reproduced from real-device numbers
// ============================================================
console.log('=== 0. REAL IPHONE BUG FIXTURE: brief speech, then persistent 15-26 ambient ===');
const bugSpeech = buildSamples([{ untilMs: 1500, level: 45 }]);
const bugAmbientCycle = [26, 16, 22, 17, 24, 18, 20, 16, 19, 17]; // matches the reported 15-26 range, strictly above round 2's frozen ~15 threshold on every tick (genuine permanent deadlock under round 2, not just slow recovery)
const bugAmbient = tileCycle(bugAmbientCycle, 1500, 22000);
const bugSamples = bugSpeech.concat(bugAmbient);

const r2Session = newRound2Session();
const r2Result = runTurnRound2(r2Session, bugSamples);
check('ROUND 2 (gated EMA): speech is still detected initially (fixture is valid)', r2Result.events.some(e => e.type === 'speech_detected'));
check('ROUND 2 DEMONSTRABLY DEADLOCKS -- "silence started" never fires across 20+ seconds of real post-speech ambient',
      !r2Result.events.some(e => e.type === 'silence_started'));
check('ROUND 2 DEMONSTRABLY DEADLOCKS -- auto-stop never fires via the silence path',
      !r2Result.events.some(e => e.type === 'auto_stop'));

const r3Session = newRound3Session();
const r3Result = runTurnRound3(r3Session, bugSamples);
const r3AutoStop = r3Result.events.find(e => e.type === 'auto_stop');
check('ROUND 3 (percentile window): does technically recover (auto-stop eventually fires)', !!r3AutoStop);
const r3LastSpeechT = lastSpeechSampleBefore(bugSamples, 40, r3AutoStop ? r3AutoStop.t : Infinity);
const r3Latency = r3AutoStop ? (r3AutoStop.t - r3LastSpeechT) : null;
check('ROUND 3 DEMONSTRABLY HAS UNACCEPTABLE LATENCY -- post-speech recovery takes multiple seconds (>3000ms), confirming the release review\'s finding',
      r3Latency !== null && r3Latency > 3000);

const newSessionBug = newSession();
const newResultBug = runTurnNew(newSessionBug, bugSamples);
check('ROUND 4: speech is still detected (no regression on the same fixture)', newResultBug.events.some(e => e.type === 'speech_detected'));
check('ROUND 4 RECOVERS -- "silence started" fires', newResultBug.events.some(e => e.type === 'silence_started'));
check('ROUND 4 RECOVERS -- auto-stop fires via the real silence path (not the 30s safety cutoff)', newResultBug.events.some(e => e.type === 'auto_stop'));
const newBugAutoStop = newResultBug.events.find(e => e.type === 'auto_stop');
const newBugLastSpeechT = lastSpeechSampleBefore(bugSamples, 40, newBugAutoStop ? newBugAutoStop.t : Infinity);
const newBugLatency = newBugAutoStop ? (newBugAutoStop.t - newBugLastSpeechT) : null;
check(`ROUND 4 MEETS THE CONVERSATIONAL LATENCY REQUIREMENT -- post-speech latency (${newBugLatency}ms) is within ${MAX_ACCEPTABLE_POST_SPEECH_LATENCY_MS}ms of the user actually stopping talking (measured from the last real speech sample, not from recording start)`,
      newBugLatency !== null && newBugLatency <= MAX_ACCEPTABLE_POST_SPEECH_LATENCY_MS);
check('ROUND 4: ambient estimate moved off the original ~5 default (adaptation is genuinely happening)',
      newResultBug.finalNoiseFloor >= 10);

// ============================================================
// 0b. NO-SPEECH NOISY STARTUP -- the release review's specific new requirement
// ============================================================
console.log();
console.log('=== 0b. No-speech noisy startup: ambient 15-26 the WHOLE turn, user never actually speaks ===');
const noSpeechSession = newSession();
const noSpeechSamples = tileCycle(bugAmbientCycle, 0, 25000); // no speech burst at all -- pure elevated ambient, matching the exact real-device range, for the whole turn
const noSpeechResult = runTurnNew(noSpeechSession, noSpeechSamples);
check('hasSpokenYet NEVER becomes confirmed from persistent ambient alone', noSpeechResult.finalHasSpokenYet === false);
check('no speech_detected event fires at any point', !noSpeechResult.events.some(e => e.type === 'speech_detected'));
check('no silence-path auto_stop occurs (there is no "silence" to detect -- nothing was ever confirmed as speech)',
      !noSpeechResult.events.some(e => e.type === 'auto_stop'));

console.log();
console.log('=== 0c. No-speech noisy startup, extended to MAX_DURATION_MS -- only the safety cutoff may terminate it ===');
const noSpeechLongSession = newSession();
const noSpeechLongSamples = tileCycle(bugAmbientCycle, 0, MAX_DURATION_MS + 2000);
const noSpeechLongResult = runTurnNew(noSpeechLongSession, noSpeechLongSamples);
check('hasSpokenYet still never confirms, even over a full 30+ second noisy-ambient-only stretch', noSpeechLongResult.finalHasSpokenYet === false);
check('the ONLY way this turn ends is the max_duration_stop safety cutoff', noSpeechLongResult.events.some(e => e.type === 'max_duration_stop'));
check('it does NOT end via the normal silence/auto_stop path (there was never a confirmed utterance to end)',
      !noSpeechLongResult.events.some(e => e.type === 'auto_stop'));

// A second noisy-ambient pattern (different exact values, still in the
// reported range) with an occasional near-threshold value -- specifically
// the pattern that exposed round 4's first draft (a plain fast EMA) as
// still capable of a brief false-positive confirmation.
console.log();
console.log('=== 0d. A second noisy-room pattern (different values, same range) must also never confirm ===');
const noisyCycle2 = [18, 22, 16, 25, 15, 20, 24, 17];
const noisySession2 = newSession();
const noisyResult2 = runTurnNew(noisySession2, tileCycle(noisyCycle2, 0, 15000));
check('hasSpokenYet never confirms on this pattern either', noisyResult2.finalHasSpokenYet === false);
check('no speech_detected event fires on this pattern either', !noisyResult2.events.some(e => e.type === 'speech_detected'));

// ============================================================
// 0e. ACTUAL DEVICE ONSET VALUES -- this is the fixture the release
// review specifically asked for. The earlier "bug fixture" above used
// speech=45 (a loud, comfortably-separated level) to demonstrate the
// round-2 deadlock and round-3 latency, which is valid for THAT
// purpose, but it does NOT represent the real device's actual speech
// amplitude. The real event log showed:
//   speech candidate (level=15.3, threshold=14.9)
//   speech confirmed (level=16.7)
// with the SAME device's reported post-speech/background readings in
// the 15-26 range -- i.e. on this device, real speech (15.3-17.2) and
// real ambient (15-26) OVERLAP, and ambient's own peak (26) is LOUDER
// than the observed speech. This section tests against that actual
// data, not an idealized one.
// ============================================================
console.log();
console.log('=== 0e. ACTUAL DEVICE ONSET VALUES: real speech ~15.3-17.2 against real ambient 15-26 (the exact numbers from the device event log) ===');

// The real onset sequence, reconstructed from the reported event log
// (candidate@15.3 -> confirmed@16.7), continued with a few more ticks
// at similarly quiet levels (this device's speech was consistently
// quiet, not just at onset) before the reported post-speech ambient.
const realDeviceSpeech = [[0, 15.3], [100, 16.7], [200, 16.0], [300, 17.2], [400, 15.8], [500, 16.4], [600, 15.9]];
const realDeviceAmbient = tileCycle(bugAmbientCycle, 700, 22000);
const realDeviceFixture = realDeviceSpeech.concat(realDeviceAmbient);

const realDeviceSession = newSession();
const realDeviceResult = runTurnNew(realDeviceSession, realDeviceFixture);
const realDeviceConfirmed = realDeviceResult.events.some(e => e.type === 'speech_detected');
console.log(`  (informational, not a pass/fail check by itself) with the CURRENT constants (FLOOR_ADAPT_STEP=7, SPEECH_MARGIN=10, SPEECH_CONFIRM_TICKS=2), this real-amplitude speech ${realDeviceConfirmed ? 'DOES' : 'DOES NOT'} get confirmed`);

// Generic, parametrized reproduction of the round-4 algorithm (same
// logic as runTurnNew, but with STEP/MARGIN/CONF as arguments) used
// ONLY for the grid search below -- to answer the release review's
// actual question: is there ANY tuning of these three constants that
// resolves this, or is the signal itself inadequate?
function runTurnParametrized(samples, STEP, MARGIN, CONF) {
    let floor = DEFAULT_INITIAL_NOISE_FLOOR, hasSpokenYet = false, consecutiveSpeechTicks = 0, silenceStartedAt = null;
    const events = [];
    for (const [t, level] of samples) {
        const threshold = Math.max(MIN_SPEECH_THRESHOLD, floor + MARGIN);
        const isCandidate = level > threshold;
        if (isCandidate) {
            consecutiveSpeechTicks++;
            if (!hasSpokenYet) floor = level > floor ? Math.min(floor + STEP, level) : Math.max(floor - STEP, level);
            if (consecutiveSpeechTicks >= CONF) { if (!hasSpokenYet) events.push({ t, type: 'speech_detected' }); hasSpokenYet = true; silenceStartedAt = null; }
        } else {
            consecutiveSpeechTicks = 0;
            floor = level > floor ? Math.min(floor + STEP, level) : Math.max(floor - STEP, level);
            if (hasSpokenYet) {
                if (silenceStartedAt === null) { silenceStartedAt = t; events.push({ t, type: 'silence_started' }); }
                if (t - silenceStartedAt > SILENCE_HOLD_MS) { events.push({ t, type: 'auto_stop' }); break; }
            }
        }
    }
    return events;
}

// A realistic, much harder ambient stress-test than the short 8-value
// repeating cycles used elsewhere in this file: 60 real seconds,
// pseudo-randomized (seeded, so this stays deterministic) but
// constrained to the EXACT reported real-device range (15-26). A short
// repeating cycle can accidentally avoid ever landing on the specific
// near-threshold value that trips a given constant combination --
// this fixture is deliberately built to not have that blind spot.
function seededRand(seed) {
    let s = seed;
    return () => { s = (s * 1103515245 + 12345) & 0x7fffffff; return s / 0x7fffffff; };
}
const rand = seededRand(42);
const longRealisticAmbient = [];
for (let i = 0; i < 600; i++) longRealisticAmbient.push([i * TICK_MS, 15 + rand() * 11]); // uniform in the reported 15-26 range

let anyConstantComboWorks = false;
let combosChecked = 0;
for (const STEP of [1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20]) {
    for (const MARGIN of [1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20]) {
        for (const CONF of [1, 2, 3, 4]) {
            combosChecked++;
            const speechEvents = runTurnParametrized(
                realDeviceSpeech.concat(longRealisticAmbient.map(([t, l]) => [t + 700, l])), STEP, MARGIN, CONF
            ).filter(e => e.type === 'speech_detected');
            const ambientEvents = runTurnParametrized(longRealisticAmbient, STEP, MARGIN, CONF);
            const ambientFalsePositive = ambientEvents.some(e => e.type === 'speech_detected');
            if (speechEvents.length > 0 && !ambientFalsePositive) anyConstantComboWorks = true;
        }
    }
}
check(`EXHAUSTIVE GRID SEARCH (${combosChecked} combinations of FLOOR_ADAPT_STEP x SPEECH_MARGIN x SPEECH_CONFIRM_TICKS): NONE simultaneously confirm real device-level speech (~15-17) AND reject real device-level ambient (~15-26) over a realistic 60s stretch -- this is a SIGNAL problem, not a tuning problem, so this pass does not attempt to tune around it further`,
      anyConstantComboWorks === false);
check('confirms the fixture itself is a genuine conflict, not a fluke of one specific 8-value repeating cycle: the real device\'s reported ambient PEAK (26) exceeds its reported SPEECH level (15.3-17.2) -- no positive margin can admit the quieter signal while excluding the louder one',
      Math.max(...bugAmbientCycle) > Math.max(...realDeviceSpeech.map(([, l]) => l)));

console.log();
console.log('  CONCLUSION: per the release review\'s instruction, this pass does NOT keep tuning FLOOR_ADAPT_STEP/SPEECH_MARGIN/SPEECH_CONFIRM_TICKS.');
console.log('  Instead, TEST-only RMS instrumentation (getByteTimeDomainData-based) has been added alongside the existing');
console.log('  frequency-average level in templates/assistant.html and the on-screen diagnostics panel, logging both signals');
console.log('  side by side WITHOUT changing any detection behavior. A real-iPhone session capturing both numbers together is');
console.log('  needed before deciding whether to switch the detector to an RMS-based (or otherwise redesigned) signal.');

// ============================================================
// 1. Immediate speech at t=0 (quiet room)
// ============================================================
console.log();
console.log('=== 1. Immediate speech beginning at 0ms, quiet room -- must detect speech and auto-stop quickly after real silence ===');
const session1 = newSession();
const s1 = buildSamples([
    { untilMs: 2000, level: 45 },
    { untilMs: 5500, level: 4 },
]);
const r1 = runTurnNew(session1, s1);
check('speech was detected essentially immediately (within the first few ticks, not delayed by a calibration window)',
      r1.events.some(e => e.type === 'speech_detected' && e.t <= 300));
const r1AutoStop = r1.events.find(e => e.type === 'auto_stop');
check('detector auto-stopped after real silence followed the immediate speech', !!r1AutoStop);
const r1LastSpeechT = lastSpeechSampleBefore(s1, 40, r1AutoStop ? r1AutoStop.t : Infinity);
check('post-speech latency is conversational (within requirement), not multi-second',
      r1AutoStop && (r1AutoStop.t - r1LastSpeechT) <= MAX_ACCEPTABLE_POST_SPEECH_LATENCY_MS);

// ============================================================
// 2. Quiet room (no speech at all)
// ============================================================
console.log();
console.log('=== 2. Quiet room, no speech at all -- must never falsely classify ambient as speech ===');
const session2 = newSession();
const s2 = buildSamples([{ untilMs: 6000, level: 5 }]);
const r2q = runTurnNew(session2, s2);
check('pure quiet-room ambient is never classified as speech', !r2q.events.some(e => e.type === 'speech_detected'));
check('ambient estimate stays at the true quiet level', r2q.finalNoiseFloor === 5);

// ============================================================
// 3. Noisy room (no speech, elevated ambient like the real bug range) -- must NEVER confirm
// ============================================================
console.log();
console.log('=== 3. Noisy room, no speech -- elevated 15-26 ambient must NEVER be confirmed as speech (strengthened per release review) ===');
const session3 = newSession();
const s3 = tileCycle(bugAmbientCycle, 0, 15000);
const r3n = runTurnNew(session3, s3);
check('hasSpokenYet is never confirmed from noisy ambient alone', r3n.finalHasSpokenYet === false);
check('no speech_detected event fires', !r3n.events.some(e => e.type === 'speech_detected'));
check('ambient estimate still adapts toward the real observed noisy range (adaptation itself still works)',
      r3n.finalNoiseFloor >= 10 && r3n.finalNoiseFloor <= 26);
// Continue the SAME session with real speech, then real silence, to
// prove the detector is not left in some bad state by the noisy
// no-speech stretch.
const s3b = buildSamples([{ untilMs: 1500, level: 45 }]).map(([t, l]) => [t + 15000, l])
    .concat(buildSamples([{ untilMs: 5000, level: 4 }]).map(([t, l]) => [t + 16500, l]));
const r3bn = runTurnNew(session3, s3b);
check('after a noisy-room stretch, real speech is still detected correctly', r3bn.events.some(e => e.type === 'speech_detected'));
check('after a noisy-room stretch, real silence following speech still triggers auto-stop quickly', r3bn.events.some(e => e.type === 'auto_stop'));

// ============================================================
// 4. Changing ambient level mid-turn (room gets noisier, then quieter)
// ============================================================
console.log();
console.log('=== 4. Changing ambient level mid-turn -- estimate must track a shifting room, not stay pinned to the starting level ===');
const session4 = newSession();
const s4 = buildSamples([
    { untilMs: 3000, level: 5 },
    { untilMs: 9000, level: 20 },
    { untilMs: 12000, level: 45 },
    { untilMs: 16000, level: 20 },
]);
const r4 = runTurnNew(session4, s4);
check('speech is still detected correctly even after the ambient level shifted upward mid-turn', r4.events.some(e => e.type === 'speech_detected'));
check('detector eventually recognizes the new (louder) post-speech ambient as silence and auto-stops', r4.events.some(e => e.type === 'auto_stop'));

// ============================================================
// 5. Sustained speech must NOT become the noise floor
// ============================================================
console.log();
console.log('=== 5. Sustained multi-second speech (with realistic natural dynamics) must not be cut off or absorbed into the ambient estimate ===');
const session5 = newSession();
const sentenceCycle = [42, 38, 44, 35, 46, 40, 36, 44]; // natural word/phrase-level variation -- softens toward 35, never drops anywhere near true ambient (~5-25)
const s5 = tileCycle(sentenceCycle, 0, 8000).concat(buildSamples([{ untilMs: 5000, level: 4 }]).map(([t, l]) => [t + 8000, l]));
const r5 = runTurnNew(session5, s5);
check('the entire 8-second sustained utterance is recognized as one continuous speech turn, not chopped up (no premature auto-stop during it)',
      !r5.events.some(e => e.type === 'auto_stop' && e.t < 8000));
check('sustained speech did not get absorbed into the ambient estimate (floor stayed frozen well below the speech level, not near ~40)',
      (() => {
          const probeSession = newSession();
          const probeSpeechOnly = tileCycle(sentenceCycle, 0, 8000);
          const probeResult = runTurnNew(probeSession, probeSpeechOnly);
          return probeResult.finalNoiseFloor < 20;
      })());
check('detector still auto-stops correctly, and quickly, once the sustained speech genuinely ends and real silence follows',
      (() => {
          const stop = r5.events.find(e => e.type === 'auto_stop');
          if (!stop) return false;
          const lastSpeechT = lastSpeechSampleBefore(s5, 30, stop.t);
          return (stop.t - lastSpeechT) <= MAX_ACCEPTABLE_POST_SPEECH_LATENCY_MS;
      })());

// ============================================================
// 6. Brief pause inside a sentence must not prematurely submit
// ============================================================
console.log();
console.log('=== 6. A brief mid-sentence pause (shorter than SILENCE_HOLD_MS) must not trigger a premature auto-stop ===');
const session6 = newSession();
const s6 = buildSamples([
    { untilMs: 800, level: 4 },
    { untilMs: 2000, level: 45 },   // "I need to submit a concrete request..."
    { untilMs: 2700, level: 4 },    // brief breath/pause, well under SILENCE_HOLD_MS (1300ms)
    { untilMs: 4500, level: 45 },   // "...for Patel Farm"
    { untilMs: 7500, level: 4 },    // real end-of-turn silence
]);
const r6 = runTurnNew(session6, s6);
const autoStops6 = r6.events.filter(e => e.type === 'auto_stop');
check('no premature auto-stop occurred during the brief mid-sentence pause (must not fire before the real end-of-turn silence begins at t=4500)',
      autoStops6.every(e => e.t >= 4500));
check('the turn still auto-stops correctly, and quickly, once the real end-of-turn silence arrives',
      (() => {
          const stop = autoStops6[0];
          if (!stop) return false;
          return (stop.t - 4500) <= MAX_ACCEPTABLE_POST_SPEECH_LATENCY_MS;
      })());

// ============================================================
// 7. Sudden background noise after speech
// ============================================================
console.log();
console.log('=== 7. A sudden, brief background noise spike after speech must not permanently block auto-stop ===');
const session7 = newSession();
const s7 = buildSamples([
    { untilMs: 2000, level: 45 },   // speech
    { untilMs: 2600, level: 4 },    // starts to go quiet
    { untilMs: 2700, level: 60 },   // one single loud spike -- a door, a dropped object, etc (1 tick only)
    { untilMs: 6500, level: 4 },    // back to genuine silence for the rest of the turn
]);
const r7 = runTurnNew(session7, s7);
check('a single-tick noise spike does not get confirmed as new speech (needs 2 consecutive ticks)',
      r7.events.filter(e => e.type === 'speech_detected').length === 1);
check('the turn still auto-stops once genuine silence resumes after the noise spike', r7.events.some(e => e.type === 'auto_stop'));

// ============================================================
// 8. Second voice turn using the same session (learned estimate carries over, stays fast)
// ============================================================
console.log();
console.log('=== 8. Second turn in the same voice session reuses the learned ambient estimate and is NOT slowed by any recalibration ===');
const session8 = newSession();
const turn1Samples = buildSamples([
    { untilMs: 500, level: 4 },
    { untilMs: 1800, level: 45 },
    { untilMs: 5300, level: 4 },
]);
const turn1Result = runTurnNew(session8, turn1Samples);
check('(setup) turn 1 completed normally and auto-stopped', turn1Result.events.some(e => e.type === 'auto_stop'));

const turn2Samples = buildSamples([
    { untilMs: 2000, level: 45 },   // immediate speech again, second turn, no silent lead-in
    { untilMs: 5500, level: 4 },
]);
const turn2Result = runTurnNew(session8, turn2Samples);
check('turn 2 detects immediate speech with no silent lead-in required', turn2Result.events.some(e => e.type === 'speech_detected' && e.t <= 300));
const turn2AutoStop = turn2Result.events.find(e => e.type === 'auto_stop');
check('turn 2 still auto-stops correctly', !!turn2AutoStop);
const turn2LastSpeechT = lastSpeechSampleBefore(turn2Samples, 40, turn2AutoStop ? turn2AutoStop.t : Infinity);
check('turn 2 is just as fast as turn 1 -- no long recalibration penalty on later turns',
      turn2AutoStop && (turn2AutoStop.t - turn2LastSpeechT) <= MAX_ACCEPTABLE_POST_SPEECH_LATENCY_MS);

console.log();
console.log(`RESULT: ${PASS} passed, ${FAIL} failed`);
if (FAIL > 0) process.exit(1);
process.exit(0);
