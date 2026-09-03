"""
Atlas voice upgrade regression tests.

Covers the two real backend changes made for the voice experience
upgrade:

1. Sentence-buffered progressive TTS: _split_ready_sentences() and the
   audio_chunk events stream_atlas_turn() now emits mid-response,
   before the full Claude reply has finished streaming.
2. The deterministic write-confirmation gate: execute_tool() for
   create_concrete_request is only ever called after the SAME complete
   field set has been proposed via action="submit" on two consecutive
   turns -- BuildIQ's own code, not a single model output, decides when
   a write is actually confirmed.

No real Anthropic/ElevenLabs network calls are made -- requests.post is
mocked to simulate Claude's real SSE wire format, and ATLAS_VOICE_ID is
unset in the test environment (confirmed below), so
_elevenlabs_tts_call() naturally short-circuits to (None, None) without
any network access, exactly as it does in production when voice isn't
configured.

Usage (from the project root):
    APP_ENV=development python3 scripts/atlas_voice_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import sqlite3
import time
from datetime import datetime
from unittest.mock import patch, MagicMock

import _test_db_setup
_test_db_setup.isolate_test_database()  # before `import app`

os.environ["ANTHROPIC_API_KEY"] = "test-fake-key-never-actually-used-requests-is-mocked"  # stream_atlas_turn short-circuits without this; requests.post is mocked below so no real key/network is ever touched

import app as appmod
import _test_hygiene as hygiene

PASS = []
FAIL = []


def check(label, condition):
    if condition:
        PASS.append(label)
    else:
        FAIL.append(label)
    print(("  OK  " if condition else "FAIL  ") + label)


def fake_claude_stream(reply_text, index=0):
    """Builds a MagicMock standing in for ONE requests.post return value,
    whose .iter_lines() yields real-shaped Anthropic streaming lines
    (content_block_start/delta/stop, one text block, index-tagged)
    followed by message_delta/message_stop/[DONE] -- matches what
    _stream_claude_completion actually parses. Splitting into several
    small deltas (not one big one) is deliberate: it's what actually
    exercises the sentence-buffering/audio_chunk logic mid-stream
    instead of only at the very end."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    lines = []
    lines.append("data: " + json.dumps({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}))
    # Emit in small chunks of ~12 chars to simulate real token-by-token streaming.
    for i in range(0, len(reply_text), 12):
        piece = reply_text[i:i + 12]
        lines.append("data: " + json.dumps({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": piece}}))
    lines.append("data: " + json.dumps({"type": "content_block_stop", "index": index}))
    lines.append("data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}))
    lines.append("data: " + json.dumps({"type": "message_stop"}))
    lines.append("data: [DONE]")
    resp.iter_lines = MagicMock(return_value=iter(lines))
    return resp


def fake_no_tool_pass1():
    """Pass 1 (tool-detection) response for a turn that uses no tool --
    every test in this file is a plain chat/concrete-request flow with
    no project-context tool call, so Pass 1 always resolves to
    stop_reason=end_turn with zero tool_use blocks, and Pass 1's own
    (unused) text is irrelevant -- kept empty."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    lines = [
        "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        "data: " + json.dumps({"type": "message_stop"}),
        "data: [DONE]",
    ]
    resp.iter_lines = MagicMock(return_value=iter(lines))
    return resp


def run_turn(user_text, draft, reply_text):
    """Runs one full stream_atlas_turn() call against faked Claude
    responses, returns the list of parsed SSE events. Caller is
    responsible for already being inside a Flask request context (via
    test_request_context, optionally with a logged-in user via
    login_user) -- this function does NOT push its own, since nesting a
    second context here would give current_user a fresh, logged-out
    session, breaking the write-confirmation tests in section 4 that
    rely on an already-authenticated current_user.

    NATIVE TOOL DISPATCH UPDATE: stream_atlas_turn now always makes TWO
    Claude calls per turn (Pass 1: tool-detection, Pass 2: the real
    live-streamed reply) -- see the project-context architecture fix.
    None of this file's tests exercise project-context tool use, so
    Pass 1 is always mocked as a plain no-tool response and Pass 2
    carries the actual reply_text being tested, preserving every
    existing assertion below unchanged."""
    responses = [fake_no_tool_pass1(), fake_claude_stream(reply_text)]

    def _side_effect(*args, **kwargs):
        idx = _side_effect.calls
        _side_effect.calls += 1
        return responses[idx]
    _side_effect.calls = 0

    with patch("app.requests.post", side_effect=_side_effect):
        events = []
        for line in appmod.stream_atlas_turn(user_text, draft):
            payload = line[len("data: "):].strip()
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
        return events


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"

    check("ATLAS_VOICE_ID is unset in the test environment (ElevenLabs calls will short-circuit, no network)",
          appmod.ATLAS_VOICE_ID in (None, ""))

    print()
    print("=== 1. _split_ready_sentences: real sentence-boundary logic ===")
    ready, remainder = appmod._split_ready_sentences("Hello there. How can I help you today? ")
    check("first complete sentence is split off ready for synthesis", ready == ["Hello there."])
    check("the incomplete trailing fragment stays in the remainder", remainder == "How can I help you today? ")

    ready2, remainder2 = appmod._split_ready_sentences("Sure, that's at 3.5 psi mix, ")
    check("a decimal number ('3.5') is NOT mistaken for a sentence boundary", ready2 == [])

    ready3, remainder3 = appmod._split_ready_sentences("You have 3 active bids right now. Want details?")
    check("a sentence followed by more text splits correctly, leaving the trailing unterminated fragment buffered",
          ready3 == ["You have 3 active bids right now."] and remainder3 == "Want details?")

    ready4, remainder4 = appmod._split_ready_sentences("")
    check("empty buffer produces no ready sentences and no error", ready4 == [] and remainder4 == "")

    print()
    print("=== 2. _elevenlabs_tts_call: unconfigured voice short-circuits with zero network calls ===")
    with patch("urllib.request.urlopen") as mock_urlopen:
        audio, err = appmod._elevenlabs_tts_call("hello")
        check("returns (None, None) when ATLAS_VOICE_ID is unset", audio is None and err is None)
        check("no network call was made", mock_urlopen.call_count == 0)

    print()
    print("=== 3. stream_atlas_turn: audio_chunk events arrive progressively, not just once at the end ===")
    draft = {"mode": "chat", "fields": {}, "history": [], "pending_submit": None, "interaction_mode": "voice"}
    reply = 'This is the first sentence. This is the second one. And a third to be sure.<state>{"mode": "chat", "fields": {}, "action": "none"}</state>'
    with appmod.app.test_request_context('/'):
        events = run_turn("hi", draft, reply)
    delta_events = [e for e in events if e.get("type") == "delta"]
    chunk_events = [e for e in events if e.get("type") == "audio_chunk"]
    done_events = [e for e in events if e.get("type") == "done"]
    check("multiple delta events were emitted (real incremental text streaming, unaffected by the voice change)", len(delta_events) > 1)
    check("at least 3 audio_chunk events were emitted (one per sentence)", len(chunk_events) >= 3)
    check("audio_chunk events carry the actual sentence text even with no audio configured", all(c.get("text") for c in chunk_events))
    check("exactly one done event terminates the turn", len(done_events) == 1)
    check("done event's own audio/audio_error are null -- all real audio already went out via audio_chunk events",
          done_events[0].get("audio") is None and done_events[0].get("audio_error") is None)
    # Order proof: audio_chunk events for the first two sentences must
    # appear in the stream BEFORE the delta event containing the third
    # sentence's text -- i.e. genuinely progressive, not batched at the end.
    first_chunk_idx = events.index(chunk_events[0])
    last_delta_idx = max(i for i, e in enumerate(events) if e.get("type") == "delta")
    check("at least one audio_chunk was emitted before the final delta arrived (genuinely progressive, not all-at-the-end)",
          first_chunk_idx < last_delta_idx)

    print()
    print("=== 4. Deterministic write-confirmation gate: writes require a SEPARATE confirm_write request ===")
    admin_email = "__avt_admin@test.local"
    db.execute("DELETE FROM users WHERE email=?", (admin_email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", ("x", admin_email, generate_password_hash(pw), now))
    db.commit()
    admin_uid = db.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()[0]
    admin_role_id = db.execute("SELECT id FROM roles WHERE name='Administrator'").fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (admin_uid, admin_role_id))
    db.commit()

    complete_fields = {
        "project": "__avt_test_project", "pour_date": "2026-09-15", "pour_time": "8:00 AM", "job_site_address": "123 Test St",
        "area_description": "Slab", "mix_design_psi": "4000", "mix_slump": "4", "concrete_amount": "20 yards",
        "truck_spacing": "15 min", "pump_type": "None", "lab_required": "No", "drilling_required": "No",
    }
    submit_reply = 'Great, submitting that now.<state>{"mode": "concrete_request", "fields": %s, "action": "submit"}</state>' % json.dumps(complete_fields)

    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        row = db.execute("SELECT * FROM users WHERE id=?", (admin_uid,)).fetchone()
        user = appmod.User(row)
        login_user(user)

        before_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_test_project'").fetchone()[0]

        draft2 = {"mode": "concrete_request", "fields": complete_fields, "history": [], "pending_submit": None, "pending_write": None}
        events_first = run_turn("yes submit it", draft2, submit_reply)
        after_first_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_test_project'").fetchone()[0]
        check("first 'submit' proposal with a fresh (no prior pending) session does NOT create a record",
              after_first_count == before_count)
        check("the held-for-confirmation reply text was appended to what the model said",
              any("one more time" in e.get("text", "") for e in events_first if e.get("type") == "delta"))
        check("draft.pending_submit is now set, holding the exact fields hash for comparison on the next turn",
              draft2.get("pending_submit") is not None and draft2["pending_submit"].get("fields_hash"))
        first_done = [e for e in events_first if e.get("type") == "done"][0]
        check("first proposal's done event carries no pending_write_token yet", first_done.get("pending_write_token") is None)

        # Second turn: SAME complete fields proposed again. Even now,
        # stream_atlas_turn itself must NOT write anything -- it only
        # becomes ELIGIBLE and hands back a token.
        events_second = run_turn("yes, submit it", draft2, submit_reply)
        after_second_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_test_project'").fetchone()[0]
        check("second consecutive matching 'submit' STILL does not write inline -- stream_atlas_turn never calls execute_tool itself",
              after_second_count == before_count)
        second_done = [e for e in events_second if e.get("type") == "done"][0]
        write_token = second_done.get("pending_write_token")
        check("the done event now carries a real pending_write_token", bool(write_token))
        check("draft.pending_write holds the exact same token and the complete fields", draft2.get("pending_write", {}).get("token") == write_token)
        check("pending_submit is cleared once eligibility is reached (superseded by pending_write)", draft2.get("pending_submit") is None)

        print()
        print("=== 4a. The write ONLY happens once confirm_write is actually called with the matching token ===")
        # stream_atlas_turn is normally driven through the real
        # /assistant/ask route, which is what actually registers the
        # draft into ATLAS_SESSIONS under a session-bound token -- our
        # direct run_turn() calls above bypass that (by design, to keep
        # sections 1-4 fast and focused on stream_atlas_turn's own
        # logic). To test assistant_confirm_write() itself realistically,
        # wire that same registration up explicitly here.
        test_atlas_token = "test-atlas-token-avt"
        appmod.ATLAS_SESSIONS[test_atlas_token] = draft2

        still_before = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_test_project'").fetchone()[0]
        check("(sanity) still zero records immediately before calling confirm_write", still_before == before_count)

        # Wrong/stale token must be rejected and must not write.
        with appmod.app.test_request_context('/', method="POST", json={"token": "not-the-real-token"}):
            login_user(user)
            from flask import session as flask_session
            flask_session["atlas_token"] = test_atlas_token
            resp_bad = appmod.assistant_confirm_write()
            body_bad = resp_bad[0] if isinstance(resp_bad, tuple) else resp_bad
            check("a mismatched token is rejected (success: False)", body_bad.get("success") is False)
        after_bad_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_test_project'").fetchone()[0]
        check("a mismatched confirm_write token creates zero records", after_bad_count == before_count)

        with appmod.app.test_request_context('/', method="POST", json={"token": write_token}):
            login_user(user)
            from flask import session as flask_session
            flask_session["atlas_token"] = test_atlas_token
            resp_ok = appmod.assistant_confirm_write()
            body_ok = resp_ok if not isinstance(resp_ok, tuple) else resp_ok[0]
            check("confirm_write with the correct token succeeds", body_ok.get("success") is True)
            check("confirm_write returns the real submitted_id", body_ok.get("submitted_id") is not None)
        after_confirm_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_test_project'").fetchone()[0]
        check("exactly one record now exists, created ONLY by the explicit confirm_write call", after_confirm_count == before_count + 1)

        # A second confirm_write call with the SAME (now-consumed) token
        # must not create a duplicate record.
        with appmod.app.test_request_context('/', method="POST", json={"token": write_token}):
            login_user(user)
            from flask import session as flask_session
            flask_session["atlas_token"] = test_atlas_token
            resp_replay = appmod.assistant_confirm_write()
            body_replay = resp_replay[0] if isinstance(resp_replay, tuple) else resp_replay
            check("replaying the same already-consumed token is rejected, not re-executed", body_replay.get("success") is False)
        after_replay_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_test_project'").fetchone()[0]
        check("replaying a consumed token does not create a second record", after_replay_count == before_count + 1)

        print()
        print("=== 4d. Two CONCURRENT confirmations with the same token result in exactly ONE write ===")
        # Real threads, real Flask request contexts, both racing to
        # confirm the SAME token. This exercises the actual fix (the
        # atomic claim under ATLAS_WRITE_CONFIRM_LOCK), not just timing
        # luck -- both threads attempt the check-and-clear step, and the
        # lock guarantees only one of them can ever see pending_write
        # still present.
        import threading as test_threading
        concurrent_fields = dict(complete_fields, project="__avt_concurrent_project")
        concurrent_reply = 'Submitting now.<state>{"mode": "concrete_request", "fields": %s, "action": "submit"}</state>' % json.dumps(concurrent_fields)
        draft_concurrent = {"mode": "concrete_request", "fields": concurrent_fields, "history": [], "pending_submit": None, "pending_write": None}
        run_turn("submit it", draft_concurrent, concurrent_reply)
        events_concurrent = run_turn("yes submit it", draft_concurrent, concurrent_reply)
        concurrent_done = [e for e in events_concurrent if e.get("type") == "done"][0]
        concurrent_token = concurrent_done.get("pending_write_token")
        check("concurrency test setup: a real pending_write_token was issued", bool(concurrent_token))

        concurrent_test_atlas_token = "test-atlas-token-avt-concurrent"
        appmod.ATLAS_SESSIONS[concurrent_test_atlas_token] = draft_concurrent

        results = []
        results_lock = test_threading.Lock()
        start_barrier = test_threading.Barrier(2)

        def _confirm_in_thread():
            start_barrier.wait()  # maximize the chance both threads hit the claim step at nearly the same instant
            with appmod.app.test_request_context('/', method="POST", json={"token": concurrent_token}):
                login_user(user)
                from flask import session as flask_session
                flask_session["atlas_token"] = concurrent_test_atlas_token
                resp = appmod.assistant_confirm_write()
                body = resp[0] if isinstance(resp, tuple) else resp
                with results_lock:
                    results.append(body)

        before_concurrent_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_concurrent_project'").fetchone()[0]
        t1 = test_threading.Thread(target=_confirm_in_thread)
        t2 = test_threading.Thread(target=_confirm_in_thread)
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        after_concurrent_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_concurrent_project'").fetchone()[0]
        successes = [r for r in results if r.get("success") is True]
        failures = [r for r in results if r.get("success") is False]
        check("both concurrent requests completed", len(results) == 2)
        check("exactly ONE of the two concurrent confirmations succeeded", len(successes) == 1)
        check("exactly ONE of the two concurrent confirmations was rejected (lost the race for the claim)", len(failures) == 1)
        check("exactly ONE database record was created despite two simultaneous confirmations with the same token",
              after_concurrent_count == before_concurrent_count + 1)
        appmod.ATLAS_SESSIONS.pop(concurrent_test_atlas_token, None)

        print()
        print("=== 4e. An expired pending_write token executes zero writes ===")
        expired_fields = dict(complete_fields, project="__avt_expired_project")
        expired_reply = 'Submitting now.<state>{"mode": "concrete_request", "fields": %s, "action": "submit"}</state>' % json.dumps(expired_fields)
        draft_expired = {"mode": "concrete_request", "fields": expired_fields, "history": [], "pending_submit": None, "pending_write": None}
        run_turn("submit it", draft_expired, expired_reply)
        events_expired = run_turn("yes submit it", draft_expired, expired_reply)
        expired_done = [e for e in events_expired if e.get("type") == "done"][0]
        expired_token = expired_done.get("pending_write_token")
        check("expiration test setup: a real pending_write_token was issued", bool(expired_token))
        # Backdate issued_at well past PENDING_WRITE_TTL_SECONDS.
        draft_expired["pending_write"]["issued_at"] = time.time() - (appmod.PENDING_WRITE_TTL_SECONDS + 30)

        expired_test_atlas_token = "test-atlas-token-avt-expired"
        appmod.ATLAS_SESSIONS[expired_test_atlas_token] = draft_expired
        before_expired_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_expired_project'").fetchone()[0]
        with appmod.app.test_request_context('/', method="POST", json={"token": expired_token}):
            login_user(user)
            from flask import session as flask_session
            flask_session["atlas_token"] = expired_test_atlas_token
            resp_expired = appmod.assistant_confirm_write()
            body_expired = resp_expired[0] if isinstance(resp_expired, tuple) else resp_expired
            check("an expired token is rejected (success: False)", body_expired.get("success") is False)
            check("the rejection reason mentions expiration", "expired" in (body_expired.get("error") or "").lower())
        after_expired_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_expired_project'").fetchone()[0]
        check("an expired token creates zero database writes", after_expired_count == before_expired_count)
        check("the expired token was cleared from the session (can't be retried either)", draft_expired.get("pending_write") is None)
        appmod.ATLAS_SESSIONS.pop(expired_test_atlas_token, None)

        print()
        print("=== 4b. A cancelled/abandoned turn (confirm_write never called) proves zero database writes ===")
        # This is the exact scenario the release review flagged: pending
        # confirmed action -> client/turn cancellation before commit ->
        # zero database writes. We simulate "cancellation" the only way
        # that's actually meaningful here: by simply never calling
        # confirm_write for a second, independent pending write, and
        # proving no record for it ever appears -- there is no other
        # code path that could create one, since execute_tool is called
        # from exactly one place (assistant_confirm_write) in the whole
        # Atlas voice flow.
        abandoned_fields = dict(complete_fields, project="__avt_abandoned_project")
        abandoned_reply = 'Submitting now.<state>{"mode": "concrete_request", "fields": %s, "action": "submit"}</state>' % json.dumps(abandoned_fields)
        draft_abandoned = {"mode": "concrete_request", "fields": abandoned_fields, "history": [], "pending_submit": None, "pending_write": None}
        run_turn("submit it", draft_abandoned, abandoned_reply)  # first proposal -> held
        events_abandoned = run_turn("yes submit it", draft_abandoned, abandoned_reply)  # second matching proposal -> eligible, token issued
        abandoned_done = [e for e in events_abandoned if e.get("type") == "done"][0]
        check("the abandoned turn did reach eligibility and received a real token (proving this isn't just 'never got far enough')",
              bool(abandoned_done.get("pending_write_token")))
        # Deliberately do NOT call confirm_write -- this is the cancellation.
        abandoned_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_abandoned_project'").fetchone()[0]
        check("cancellation before confirm_write is ever called results in ZERO database writes, even though the turn reached full eligibility",
              abandoned_count == 0)

        print()
        print("=== 4c. Changing fields between two submit proposals resets the pending gate (no stale confirmation) ===")
        draft3 = {"mode": "concrete_request", "fields": complete_fields, "history": [], "pending_submit": None, "pending_write": None}
        run_turn("submit it", draft3, submit_reply)
        check("first proposal (fresh draft) is held, not executed", draft3.get("pending_submit") is not None)

        different_fields = dict(complete_fields, project="__avt_test_project_DIFFERENT")
        different_reply = 'Submitting now.<state>{"mode": "concrete_request", "fields": %s, "action": "submit"}</state>' % json.dumps(different_fields)
        before_diff_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_test_project_DIFFERENT'").fetchone()[0]
        events_diff = run_turn("submit it", draft3, different_reply)
        diff_done = [e for e in events_diff if e.get("type") == "done"][0]
        check("a DIFFERENT field set proposed on the follow-up turn is NOT eligible (no pending_write_token issued)",
              diff_done.get("pending_write_token") is None)
        after_diff_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__avt_test_project_DIFFERENT'").fetchone()[0]
        check("...and therefore creates zero records", after_diff_count == before_diff_count)

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print()
    print("Cleaning up...")
    appmod.ATLAS_SESSIONS.pop("test-atlas-token-avt", None)
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__avt_%'")
    db.execute("DELETE FROM activity_log WHERE user_email=?", (admin_email,))
    db.execute("DELETE FROM user_roles WHERE user_id=?", (admin_uid,))
    db.execute("DELETE FROM users WHERE id=?", (admin_uid,))
    db.commit()

    orphans = hygiene.assert_no_orphan_privilege_rows(db)
    for o in orphans:
        FAIL.append(f"DB hygiene: {o}")
        print(f"FAIL  DB hygiene: {o}")
    if not orphans:
        check("no orphan user_roles/user_permission_overrides/role_permissions rows remain", True)
    hygiene.emergency_cleanup_orphans(db)

    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
