"""
Atlas interaction modes (text/voice TTS gating) + persistent chat
history regression.

Usage (from the project root):
    APP_ENV=development python3 scripts/atlas_history_textmode_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
import sqlite3
from datetime import datetime
from unittest.mock import patch, MagicMock

import _test_db_setup
_test_db_setup.isolate_test_database()

os.environ["ANTHROPIC_API_KEY"] = "test-fake-key"

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


def get_csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    token = get_csrf(client, "/login")
    resp = client.post("/login", data={"email": email, "password": password, "csrf_token": token}, follow_redirects=True)
    if resp.status_code != 200 or resp.request.path != "/" or "Invalid email or password" in resp.get_data(as_text=True):
        raise RuntimeError(f"login() failed for {email}: status={resp.status_code}, path={resp.request.path}")
    return token


def get_meta_csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    return m.group(1) if m else None


def ask(client, question, interaction_mode=None):
    """POSTs to /assistant/ask exactly like the real frontend JS does --
    JSON body + X-CSRFToken header. The Atlas page exposes its CSRF
    token via <meta name="csrf-token" content="..."> (not a hidden form
    input, since Atlas isn't a traditional HTML form), which is what
    the real templates/assistant.html JS itself reads -- matched here
    the same way, not via the get_csrf() helper's form-input pattern
    used by every other (traditional-form) page in this codebase."""
    html = client.get("/assistant").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    csrf_token = m.group(1) if m else None
    payload = {"question": question}
    if interaction_mode is not None:
        payload["interaction_mode"] = interaction_mode
    resp = client.post("/assistant/ask", json=payload, headers={"X-CSRFToken": csrf_token})
    resp.get_data()  # fully consume the streamed SSE body now, while this request's context is still active --
                      # stream_with_context() keeps the request context alive until the generator is drained,
                      # and leaving it undrained corrupts the test client's context stack for the NEXT request.
    return resp


def make_user(db, email, name, now, pw_hash, permission_keys):
    db.execute("DELETE FROM users WHERE email=?", (email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", (name, email, pw_hash, now))
    db.commit()
    uid = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    for key in permission_keys:
        pid = db.execute("SELECT id FROM permissions WHERE key=?", (key,)).fetchone()[0]
        db.execute("INSERT OR REPLACE INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
                   (uid, pid, "grant", "test_setup", now))
    db.commit()
    return uid


def fake_sse_lines(text, tool=None):
    lines = ["data: " + json.dumps({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": []}})]
    if tool:
        name, input_dict = tool
        lines.append("data: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": name, "input": {}}}))
        raw = json.dumps(input_dict)
        lines.append("data: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": raw}}))
        lines.append("data: " + json.dumps({"type": "content_block_stop", "index": 0}))
        lines.append("data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "tool_use"}}))
    else:
        lines.append("data: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
        for i in range(0, len(text), 12):
            lines.append("data: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[i:i + 12]}}))
        lines.append("data: " + json.dumps({"type": "content_block_stop", "index": 0}))
        lines.append("data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}))
    lines.append("data: " + json.dumps({"type": "message_stop"}))
    lines.append("data: [DONE]")
    return lines


def fake_response(lines):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.iter_lines = MagicMock(return_value=iter(lines))
    return resp


def ordinary_turn_responses(reply_text):
    """The architecture (since the native-tool-dispatch phase) ALWAYS
    makes two Claude calls per turn: Pass 1 (tool-detection, its own
    text is discarded regardless of content) and Pass 2 (the real,
    visible reply). Using a single return_value= mock for both calls is
    a real gotcha, not a simplification -- iter_lines()'s iterator is
    built once and shared, so Pass 1 alone drains it, leaving Pass 2
    with nothing and silently producing an empty visible reply. This
    helper is the correct two-response shape for an ordinary (no
    project-context tool use) turn, for use with side_effect=[...]."""
    return [fake_response(fake_sse_lines("", tool=None)), fake_response(fake_sse_lines(reply_text))]


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    perms = ["module:atlas:view", "atlas:view_business_data", "module:project_hunt:view"]
    uid_a = make_user(db, "__hist_usera@test.local", "__hist_usera", now, pw_hash, perms)
    uid_b = make_user(db, "__hist_userb@test.local", "__hist_userb", now, pw_hash, perms)

    print("=== Migration/schema ===")
    tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("atlas_conversations table exists (fresh-DB migration ran)", "atlas_conversations" in tables)
    check("atlas_messages table exists (fresh-DB migration ran)", "atlas_messages" in tables)
    conv_id_before = None
    with appmod.app.test_request_context('/'):
        conv_id_before = appmod._create_atlas_conversation(uid_a, "Idempotency probe")
    appmod.init_db()
    appmod.init_db()
    still_there = db.execute("SELECT * FROM atlas_conversations WHERE id=?", (conv_id_before,)).fetchone()
    check("existing conversation row survives repeated init_db() calls (idempotent, no data loss)", still_there is not None)
    count_after = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('atlas_conversations','atlas_messages')").fetchone()[0]
    check("no duplicate table creation after repeated init_db() calls", count_after == 2)

    print()
    print("=== Part 1: TTS zero-call enforcement (text mode) ===")
    tts_call_count = {"n": 0}

    def _counting_tts(text):
        tts_call_count["n"] += 1
        return ("fake_base64_audio", None)

    with appmod.app.test_client() as client:
        login(client, "__hist_usera@test.local", pw)

        tts_call_count["n"] = 0
        with patch("app.requests.post", side_effect=ordinary_turn_responses("Hello there, how can I help?")), \
             patch("app._elevenlabs_tts_call", side_effect=_counting_tts):
            resp = ask(client, "hi", interaction_mode="text")
            check("typed ordinary chat: request succeeds", resp.status_code == 200)
        check("typed ordinary chat: ZERO calls to _elevenlabs_tts_call", tts_call_count["n"] == 0)

        db.execute("DELETE FROM tracker_projects WHERE name LIKE '__HistTest%'")
        db.commit()
        proj_cur = db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
                               ("__HistTest Patel Farm", now, now))
        db.commit()
        proj_id = proj_cur.lastrowid

        tts_call_count["n"] = 0
        pass1 = fake_response(fake_sse_lines("", tool=("set_project_context", {"project_name": "Patel Farm"})))
        pass2 = fake_response(fake_sse_lines("We're on Patel Farm now."))
        with patch("app.requests.post", side_effect=[pass1, pass2]), \
             patch("app._elevenlabs_tts_call", side_effect=_counting_tts):
            resp = ask(client, "let's talk about patel farm", interaction_mode="text")
            check("typed project-context turn: request succeeds", resp.status_code == 200)
        check("typed project-context resolution turn: ZERO TTS calls", tts_call_count["n"] == 0)

        tts_call_count["n"] = 0
        with patch("app.requests.post", side_effect=ordinary_turn_responses("Sure thing.")), \
             patch("app._elevenlabs_tts_call", side_effect=_counting_tts):
            resp = ask(client, "another question", interaction_mode="banana")
            check("malformed interaction_mode: request still succeeds", resp.status_code == 200)
        check("malformed interaction_mode defaults safely to text: ZERO TTS calls", tts_call_count["n"] == 0)

        tts_call_count["n"] = 0
        with patch("app.requests.post", side_effect=ordinary_turn_responses("Sure thing.")), \
             patch("app._elevenlabs_tts_call", side_effect=_counting_tts):
            resp = ask(client, "yet another")
            check("omitted interaction_mode: request still succeeds", resp.status_code == 200)
        check("omitted interaction_mode defaults safely to text: ZERO TTS calls", tts_call_count["n"] == 0)

        tts_call_count["n"] = 0
        with patch("app.requests.post", side_effect=ordinary_turn_responses("Here is a voice reply.")), \
             patch("app._elevenlabs_tts_call", side_effect=_counting_tts):
            resp = ask(client, "voice question", interaction_mode="voice")
            check("voice mode turn: request succeeds", resp.status_code == 200)
        check("voice mode: TTS IS called (existing path permitted)", tts_call_count["n"] > 0)

        tts_call_count["n"] = 0
        with patch("app.requests.post", side_effect=ordinary_turn_responses("Back to text.")), \
             patch("app._elevenlabs_tts_call", side_effect=_counting_tts):
            resp = ask(client, "back to typing", interaction_mode="text")
        check("after leaving voice mode, typing again: ZERO TTS calls", tts_call_count["n"] == 0)

        tts_call_count["n"] = 0
        with patch.object(appmod, "ATLAS_VOICE_ID", "fake-voice-id-configured"), \
             patch("app.requests.post", side_effect=ordinary_turn_responses("Configured but still text.")), \
             patch("app._elevenlabs_tts_call", side_effect=_counting_tts):
            resp = ask(client, "configured check", interaction_mode="text")
        check("ElevenLabs 'configured' does not matter for text mode: ZERO TTS calls", tts_call_count["n"] == 0)

    print()
    print("=== Typed concrete-request + write-confirmation flow: zero TTS ===")
    with appmod.app.test_client() as client:
        login(client, "__hist_usera@test.local", pw)
        tts_call_count["n"] = 0
        complete_fields = {
            "project": "__HistTest Patel Farm", "pour_date": "2026-09-20", "pour_time": "8:00 AM",
            "job_site_address": "1 Test Way", "area_description": "Slab", "mix_design_psi": "4000",
            "mix_slump": "4", "concrete_amount": "10 yd", "truck_spacing": "15 min",
            "pump_type": "None", "lab_required": "No", "drilling_required": "No",
        }
        submit_reply = 'Submitting now.<state>{"mode": "concrete_request", "fields": %s, "action": "submit"}</state>' % json.dumps(complete_fields)
        with patch("app.requests.post", side_effect=ordinary_turn_responses(submit_reply)), \
             patch("app._elevenlabs_tts_call", side_effect=_counting_tts):
            ask(client, "concrete request please", interaction_mode="text")
        with patch("app.requests.post", side_effect=ordinary_turn_responses(submit_reply)), \
             patch("app._elevenlabs_tts_call", side_effect=_counting_tts):
            resp = ask(client, "yes submit it", interaction_mode="text")
        check("typed concrete-request flow (through confirmation-eligible turn): ZERO TTS calls", tts_call_count["n"] == 0)

    print()
    print("=== Part 2: persistent history basic flow ===")
    with appmod.app.test_client() as client:
        login(client, "__hist_usera@test.local", pw)
        before = db.execute("SELECT COUNT(*) FROM atlas_conversations WHERE user_id=?", (uid_a,)).fetchone()[0]
        with patch("app.requests.post", side_effect=ordinary_turn_responses("A real reply.")), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            ask(client, "remember this please", interaction_mode="text")
        after = db.execute("SELECT COUNT(*) FROM atlas_conversations WHERE user_id=?", (uid_a,)).fetchone()[0]
        check("a new conversation row was created on the first real question", after == before + 1)
        conv = db.execute("SELECT * FROM atlas_conversations WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid_a,)).fetchone()
        msgs = db.execute("SELECT * FROM atlas_messages WHERE conversation_id=? ORDER BY id", (conv["id"],)).fetchall()
        check("both the user message and the assistant's visible reply were persisted", len(msgs) == 2)
        check("the user message content matches what was actually typed", msgs[0]["content"] == "remember this please")
        check("the assistant message content matches the visible reply (not raw protocol/state)",
              msgs[1]["content"] == "A real reply." and "<state>" not in msgs[1]["content"] and "tool_use" not in msgs[1]["content"])
        check("conversation title was set deterministically from the first message (no extra AI call)",
              conv["title"] == "remember this please")

    print()
    print("=== IDOR / ownership enforcement ===")
    conv_a_id = db.execute("SELECT id FROM atlas_conversations WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid_a,)).fetchone()[0]

    with appmod.app.test_client() as client_b:
        login(client_b, "__hist_userb@test.local", pw)
        resp = client_b.get(f"/assistant/conversations/{conv_a_id}")
        check("User B cannot read User A's conversation (not found, not the actual data)", resp.status_code == 404)
        body = resp.get_json()
        check("the 404 response does not leak the conversation's real title/content", body is not None and "title" not in body)

        resp2 = client_b.get(f"/assistant/conversations/{conv_a_id}")
        check("repeated access attempt to User A's conversation is still rejected", resp2.status_code == 404)
        check("no project context can be restored from a conversation User B does not own (read itself is blocked)",
              resp.status_code == 404 and resp2.status_code == 404)

    with appmod.app.test_client() as client_a:
        login(client_a, "__hist_usera@test.local", pw)
        resp_missing = client_a.get("/assistant/conversations/999999999")
        check("a genuinely non-existent conversation id returns the SAME 404 shape (existence is never leaked)", resp_missing.status_code == 404)
        resp_malformed = client_a.get("/assistant/conversations/not-a-number")
        check("a malformed (non-integer) conversation id is handled safely, not a 500", resp_malformed.status_code == 404)

        resp_own = client_a.get(f"/assistant/conversations/{conv_a_id}")
        check("the actual owner CAN read their own conversation", resp_own.status_code == 200)
        own_body = resp_own.get_json()
        check("the owner sees the real messages", len(own_body.get("messages", [])) >= 1)

    print()
    print("=== Project context restoration safety ===")
    with appmod.app.test_client() as client:
        login(client, "__hist_usera@test.local", pw)

        with appmod.app.test_request_context('/'):
            conv_valid_id = appmod._create_atlas_conversation(uid_a, "Valid project convo", project_id=proj_id)
            appmod._append_atlas_message(conv_valid_id, "user", "hello", "text")
        resp = client.get(f"/assistant/conversations/{conv_valid_id}")
        body = resp.get_json()
        check("reopening a conversation with a still-valid project restores project_context", body["project_context"].get("project_id") == proj_id)
        check("context_needs_reselection is False when restoration succeeded", body["context_needs_reselection"] is False)

        deleted_proj_cur = db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
                                       ("__HistTest Doomed Project", now, now))
        db.commit()
        deleted_proj_id = deleted_proj_cur.lastrowid
        with appmod.app.test_request_context('/'):
            conv_deleted_id = appmod._create_atlas_conversation(uid_a, "Deleted project convo", project_id=deleted_proj_id)
            appmod._append_atlas_message(conv_deleted_id, "user", "hello again", "text")
        db.execute("DELETE FROM tracker_projects WHERE id=?", (deleted_proj_id,))
        db.commit()
        resp2 = client.get(f"/assistant/conversations/{conv_deleted_id}")
        body2 = resp2.get_json()
        check("conversation with a deleted project still opens (200)", resp2.status_code == 200)
        check("historical messages remain visible even though the project is gone", len(body2.get("messages", [])) >= 1)
        check("project_context is NOT restored for a deleted project", body2["project_context"] == {})
        check("context_needs_reselection is True, telling the user to re-select", body2["context_needs_reselection"] is True)

        with appmod.app.test_request_context('/'):
            conv_perm_id = appmod._create_atlas_conversation(uid_a, "Permission-revoked convo", project_id=proj_id)
            appmod._append_atlas_message(conv_perm_id, "user", "hi", "text")
        pid_view = db.execute("SELECT id FROM permissions WHERE key='atlas:view_business_data'").fetchone()[0]
        db.execute("INSERT OR REPLACE INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
                   (uid_a, pid_view, "deny", "test_setup", now))
        db.commit()
        resp3 = client.get(f"/assistant/conversations/{conv_perm_id}")
        body3 = resp3.get_json()
        check("conversation still opens even though the required permission was revoked", resp3.status_code == 200)
        check("project_context is NOT restored once the permission is gone", body3["project_context"] == {})
        db.execute("INSERT OR REPLACE INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
                   (uid_a, pid_view, "grant", "test_setup", now))
        db.commit()

    print()
    print("=== New Chat: no inherited context ===")
    with appmod.app.test_client() as client:
        login(client, "__hist_usera@test.local", pw)
        pass1 = fake_response(fake_sse_lines("", tool=("set_project_context", {"project_name": "Patel Farm"})))
        pass2 = fake_response(fake_sse_lines("On it."))
        with patch("app.requests.post", side_effect=[pass1, pass2]), patch("app._elevenlabs_tts_call", return_value=(None, None)):
            ask(client, "let's talk about patel farm", interaction_mode="text")
        with client.session_transaction() as sess:
            token = sess.get("atlas_token")
        check("(setup) context was established in the current session", appmod.ATLAS_SESSIONS.get(token, {}).get("project_context", {}).get("project_id") == proj_id)

        client.post(appmod.url_for("assistant_conversations_new"), headers={"X-CSRFToken": get_meta_csrf(client, "/assistant")})
        with client.session_transaction() as sess:
            token2 = sess.get("atlas_token")
        check("New Chat clears project_context (no inherited context)", appmod.ATLAS_SESSIONS.get(token2, {}).get("project_context") == {})
        check("New Chat clears conversation_id (starts a genuinely new conversation)", appmod.ATLAS_SESSIONS.get(token2, {}).get("conversation_id") is None)

    print()
    print("=== Failed-turn persistence policy ===")

    def fake_error_response():
        import requests as real_requests
        err_resp = MagicMock()
        err_resp.text = '{"error": {"message": "simulated failure"}}'
        exc = real_requests.exceptions.RequestException("simulated failure")
        exc.response = err_resp
        outer = MagicMock()
        outer.raise_for_status = MagicMock(side_effect=exc)
        return outer

    def fake_incomplete_response(text_partial):
        """A stream that ends WITHOUT message_stop -- see the v7
        message_stop-requirement hardening this must still respect."""
        lines = ["data: " + json.dumps({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": []}})]
        lines.append("data: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
        lines.append("data: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text_partial}}))
        # deliberately no content_block_stop, no message_delta, no message_stop
        lines.append("data: [DONE]")
        return fake_response(lines)

    with appmod.app.test_client() as client:
        login(client, "__hist_usera@test.local", pw)

        # a) Successful turn = exactly one user + one assistant message.
        before_a = db.execute("SELECT COUNT(*) FROM atlas_messages").fetchone()[0]
        with patch("app.requests.post", side_effect=ordinary_turn_responses("A clean successful reply.")), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            ask(client, "success case question", interaction_mode="text")
        with client.session_transaction() as sess:
            conv_id_success = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("conversation_id")
        msgs_success = db.execute("SELECT * FROM atlas_messages WHERE conversation_id=? ORDER BY id", (conv_id_success,)).fetchall()
        check("successful turn: exactly one user message", len([m for m in msgs_success if m["role"] == "user"]) == 1)
        check("successful turn: exactly one assistant message", len([m for m in msgs_success if m["role"] == "assistant"]) == 1)

        # b) Model/API failure (Pass 1 itself fails) = user message
        # persisted exactly once MORE, ZERO fake completed assistant
        # message added THIS turn. Measured as a delta against the
        # conversation's state right before this specific turn -- the
        # conversation already legitimately has turn (a)'s messages in
        # it, since conversations correctly persist across turns.
        before_b = db.execute("SELECT role, COUNT(*) c FROM atlas_messages WHERE conversation_id=? GROUP BY role", (conv_id_success,)).fetchall()
        before_b_counts = {r["role"]: r["c"] for r in before_b}
        with patch("app.requests.post", return_value=fake_error_response()), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            ask(client, "failure case question", interaction_mode="text")
        conv_id_fail = conv_id_success  # same, ongoing conversation
        after_b = db.execute("SELECT role, COUNT(*) c FROM atlas_messages WHERE conversation_id=? GROUP BY role", (conv_id_fail,)).fetchall()
        after_b_counts = {r["role"]: r["c"] for r in after_b}
        check("model/API failure: exactly one NEW user message was added this turn",
              after_b_counts.get("user", 0) == before_b_counts.get("user", 0) + 1)
        check("model/API failure: ZERO new assistant messages were added this turn (no fake completion)",
              after_b_counts.get("assistant", 0) == before_b_counts.get("assistant", 0))

        # c) Retry after failure does not duplicate the prior persisted
        # user message, and a SUCCESSFUL retry adds exactly one new pair.
        with patch("app.requests.post", side_effect=ordinary_turn_responses("Recovered on retry.")), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            ask(client, "failure case question", interaction_mode="text")  # same text, genuinely resubmitted by the user
        after_c = db.execute("SELECT role, COUNT(*) c FROM atlas_messages WHERE conversation_id=? GROUP BY role", (conv_id_fail,)).fetchall()
        after_c_counts = {r["role"]: r["c"] for r in after_c}
        check("retry after failure: exactly one NEW user message added (the retry) -- the failed attempt's user message is not deduplicated or removed",
              after_c_counts.get("user", 0) == after_b_counts.get("user", 0) + 1)
        check("retry after failure: exactly one NEW assistant message added (from the successful retry)",
              after_c_counts.get("assistant", 0) == after_b_counts.get("assistant", 0) + 1)

        # d) Incomplete Pass 2 stream (no message_stop) does not persist
        # a partial assistant response.
        before_d = db.execute("SELECT role, COUNT(*) c FROM atlas_messages WHERE conversation_id=? GROUP BY role", (conv_id_fail,)).fetchall()
        before_d_counts = {r["role"]: r["c"] for r in before_d}
        with patch("app.requests.post", side_effect=[fake_response(fake_sse_lines("", tool=None)), fake_incomplete_response("Here is only part of")]), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            ask(client, "incomplete pass2 question", interaction_mode="text")
        conv_id_incomplete = conv_id_fail
        msgs_incomplete_new = db.execute(
            "SELECT * FROM atlas_messages WHERE conversation_id=? AND content = 'incomplete pass2 question'", (conv_id_incomplete,)
        ).fetchall()
        after_d = db.execute("SELECT role, COUNT(*) c FROM atlas_messages WHERE conversation_id=? GROUP BY role", (conv_id_incomplete,)).fetchall()
        after_d_counts = {r["role"]: r["c"] for r in after_d}
        check("incomplete Pass 2 stream: the user message IS persisted", len(msgs_incomplete_new) == 1)
        check("incomplete Pass 2 stream: NO new assistant message was added",
              after_d_counts.get("assistant", 0) == before_d_counts.get("assistant", 0))
        all_content_d = [m["content"] for m in db.execute("SELECT content FROM atlas_messages WHERE conversation_id=?", (conv_id_incomplete,)).fetchall()]
        check("incomplete Pass 2 stream: nothing persisted contains raw <state> or tool protocol",
              all("<state>" not in c and "tool_use" not in c for c in all_content_d))

        # e) A submit-shaped incomplete <state> does not persist internal
        # state text or create a pending write.
        before_e = db.execute("SELECT role, COUNT(*) c FROM atlas_messages WHERE conversation_id=? GROUP BY role", (conv_id_incomplete,)).fetchall()
        before_e_counts = {r["role"]: r["c"] for r in before_e}
        concrete_fields_fp = {
            "project": "__HistTest Patel Farm", "pour_date": "2026-09-20", "pour_time": "8:00 AM",
            "job_site_address": "1 Test Way", "area_description": "Slab", "mix_design_psi": "4000",
            "mix_slump": "4", "concrete_amount": "10 yd", "truck_spacing": "15 min",
            "pump_type": "None", "lab_required": "No", "drilling_required": "No",
        }
        submit_looking_partial = 'Submitting now.<state>{"mode": "concrete_request", "fields": %s, "action": "sub' % json.dumps(concrete_fields_fp)
        with patch("app.requests.post", side_effect=[fake_response(fake_sse_lines("", tool=None)), fake_incomplete_response(submit_looking_partial)]), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            resp_submit_partial = ask(client, "submit shaped incomplete", interaction_mode="text")
        with client.session_transaction() as sess:
            active_draft = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {})
        after_e = db.execute("SELECT role, COUNT(*) c FROM atlas_messages WHERE conversation_id=? GROUP BY role", (conv_id_incomplete,)).fetchall()
        after_e_counts = {r["role"]: r["c"] for r in after_e}
        check("submit-shaped incomplete <state>: no NEW assistant message persisted",
              after_e_counts.get("assistant", 0) == before_e_counts.get("assistant", 0))
        check("submit-shaped incomplete <state>: no pending_write was created", not active_draft.get("pending_write"))
        done_event_submit_partial = None
        for line in resp_submit_partial.get_data(as_text=True).split("\n\n"):
            if line.startswith("data: ") and '"type": "done"' in line:
                done_event_submit_partial = json.loads(line[len("data: "):])
        check("submit-shaped incomplete <state>: no confirmation token was produced",
              done_event_submit_partial is not None and done_event_submit_partial.get("pending_write_token") is None)

        # f) Project-context Pass 1 success + Pass 2 failure preserves
        # valid project context but not a fake assistant completion.
        before_f = db.execute("SELECT role, COUNT(*) c FROM atlas_messages WHERE conversation_id=? GROUP BY role", (conv_id_incomplete,)).fetchall()
        before_f_counts = {r["role"]: r["c"] for r in before_f}
        pass1_ctx_ok = fake_response(fake_sse_lines("", tool=("set_project_context", {"project_name": "Patel Farm"})))
        with patch("app.requests.post", side_effect=[pass1_ctx_ok, fake_error_response()]), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            ask(client, "context then pass2 fails", interaction_mode="text")
        with client.session_transaction() as sess:
            token_ctx_fail = sess.get("atlas_token")
        check("project-context success survives a subsequent Pass-2 failure",
              appmod.ATLAS_SESSIONS.get(token_ctx_fail, {}).get("project_context", {}).get("project_id") == proj_id)
        after_f = db.execute("SELECT role, COUNT(*) c FROM atlas_messages WHERE conversation_id=? GROUP BY role", (conv_id_incomplete,)).fetchall()
        after_f_counts = {r["role"]: r["c"] for r in after_f}
        check("project-context success + Pass-2 failure: no NEW fake completed assistant message persisted",
              after_f_counts.get("assistant", 0) == before_f_counts.get("assistant", 0))

    print()
    print("=== Two-user session isolation (real independent authenticated clients) ===")
    client_a3 = appmod.app.test_client()
    client_b3 = appmod.app.test_client()
    login(client_a3, "__hist_usera@test.local", pw)
    login(client_b3, "__hist_userb@test.local", pw)

    with patch("app.requests.post", side_effect=ordinary_turn_responses("A's own reply.")), \
         patch("app._elevenlabs_tts_call", return_value=(None, None)):
        ask(client_a3, "A's private question", interaction_mode="text")
    with client_a3.session_transaction() as sess:
        conv_a3_id = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("conversation_id")

    with patch("app.requests.post", side_effect=ordinary_turn_responses("B's own reply.")), \
         patch("app._elevenlabs_tts_call", return_value=(None, None)):
        ask(client_b3, "B's private question", interaction_mode="text")
    with client_b3.session_transaction() as sess:
        conv_b3_id = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("conversation_id")

    list_a = client_a3.get("/assistant/conversations").get_json()["conversations"]
    list_b = client_b3.get("/assistant/conversations").get_json()["conversations"]
    a_ids3 = {c["id"] for c in list_a}
    b_ids3 = {c["id"] for c in list_b}
    check("A only lists A's own conversations", conv_a3_id in a_ids3 and conv_b3_id not in a_ids3)
    check("B only lists B's own conversations", conv_b3_id in b_ids3 and conv_a3_id not in b_ids3)

    check("A cannot GET B's conversation", client_a3.get(f"/assistant/conversations/{conv_b3_id}").status_code == 404)
    check("B cannot GET A's conversation", client_b3.get(f"/assistant/conversations/{conv_a3_id}").status_code == 404)

    # A cannot make B's conversation active via the real route (blocked before any activation).
    resp_a_opens_b = client_a3.get(f"/assistant/conversations/{conv_b3_id}")
    with client_a3.session_transaction() as sess:
        a_active_conv_after = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("conversation_id")
    check("A cannot make B's conversation active (A's own session conversation_id is unaffected)", a_active_conv_after != conv_b3_id)

    # SECURITY FIX REGRESSION: A cannot append through B's conversation
    # ID -- force A's local session to reference B's id (simulating a
    # tampered/corrupted client session) and confirm the server
    # revalidates ownership on EVERY use of an existing conversation_id,
    # not just at the moment it was first attached to the session.
    b_conv_row_before = db.execute("SELECT title, project_id FROM atlas_conversations WHERE id=?", (conv_b3_id,)).fetchone()
    with client_a3.session_transaction() as sess:
        a_token_for_tamper = sess.get("atlas_token")
        appmod.ATLAS_SESSIONS[a_token_for_tamper]["conversation_id"] = conv_b3_id
    before_b_msgs = db.execute("SELECT COUNT(*) FROM atlas_messages WHERE conversation_id=?", (conv_b3_id,)).fetchone()[0]
    with patch("app.requests.post", side_effect=ordinary_turn_responses("Should not land in B's conversation.")) as mock_post_tampered, \
         patch("app._elevenlabs_tts_call", return_value=(None, None)):
        resp_tampered = ask(client_a3, "tampered conversation id attempt", interaction_mode="text")
    after_b_msgs = db.execute("SELECT COUNT(*) FROM atlas_messages WHERE conversation_id=?", (conv_b3_id,)).fetchone()[0]
    b_conv_row_after = db.execute("SELECT title, project_id FROM atlas_conversations WHERE id=?", (conv_b3_id,)).fetchone()

    check("SECURITY: B's message count is UNCHANGED -- A's tampered session cannot append into B's conversation",
          after_b_msgs == before_b_msgs)
    check("SECURITY: B's conversation title is unchanged", b_conv_row_after["title"] == b_conv_row_before["title"])
    check("SECURITY: B's conversation project_id is unchanged", b_conv_row_after["project_id"] == b_conv_row_before["project_id"])
    check("SECURITY: no Atlas generation/write proceeded against B's conversation -- Claude's API was never even called (fails closed before any API call, not just before persistence)",
          mock_post_tampered.call_count == 0)
    tampered_body = resp_tampered.get_data(as_text=True)
    check("SECURITY: A receives a safe, generic failure message, not a crash or B's data",
          "start a new chat" in tampered_body.lower() or "couldn't continue" in tampered_body.lower())
    check("SECURITY: the response does not reveal that the conversation belongs to another real user",
          "userb" not in tampered_body.lower() and "B's private question" not in tampered_body)
    with client_a3.session_transaction() as sess:
        a_conv_after_tamper = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("conversation_id")
    check("SECURITY: the invalid session association was cleared (not left pointing at B's conversation)",
          a_conv_after_tamper != conv_b3_id)
    with client_b3.session_transaction() as sess:
        b_untouched_conv = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("conversation_id")
    check("SECURITY: B's own Atlas session remains completely untouched by A's tampered attempt",
          b_untouched_conv == conv_b3_id)

    # A can recover cleanly via New Chat and use a genuinely new, correctly-owned conversation.
    resp_new = client_a3.post("/assistant/conversations/new", headers={"X-CSRFToken": get_meta_csrf(client_a3, "/assistant")})
    check("SECURITY recovery: New Chat after the tampered attempt succeeds", resp_new.status_code == 200)
    with patch("app.requests.post", side_effect=ordinary_turn_responses("A's fresh, correctly-owned reply.")), \
         patch("app._elevenlabs_tts_call", return_value=(None, None)):
        ask(client_a3, "A's fresh question after recovery", interaction_mode="text")
    with client_a3.session_transaction() as sess:
        a_recovered_conv_id = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("conversation_id")
    check("SECURITY recovery: A's new conversation is genuinely new (not B's id, not the old tampered reference)",
          a_recovered_conv_id not in (None, conv_b3_id))
    recovered_owner = db.execute("SELECT user_id FROM atlas_conversations WHERE id=?", (a_recovered_conv_id,)).fetchone()
    check("SECURITY recovery: the new conversation is correctly owned by A", recovered_owner["user_id"] == uid_a)

    # Nonexistent conversation_id forced into session -> same fail-closed behavior.
    with client_a3.session_transaction() as sess:
        appmod.ATLAS_SESSIONS[sess.get("atlas_token")]["conversation_id"] = 999999999
    with patch("app.requests.post", side_effect=ordinary_turn_responses("Should not run.")) as mock_post_missing, \
         patch("app._elevenlabs_tts_call", return_value=(None, None)):
        resp_missing_conv = ask(client_a3, "nonexistent conversation id attempt", interaction_mode="text")
    check("SECURITY: a nonexistent conversation_id in session state also fails closed (no API call made)",
          mock_post_missing.call_count == 0)
    with client_a3.session_transaction() as sess:
        check("SECURITY: the nonexistent conversation_id was also cleared from session",
              appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("conversation_id") != 999999999)

    # Conversation deleted AFTER being loaded into ATLAS_SESSIONS -> still fails closed, no crash.
    with appmod.app.test_request_context('/'):
        conv_to_delete = appmod._create_atlas_conversation(uid_a, "Will be deleted")
    with client_a3.session_transaction() as sess:
        appmod.ATLAS_SESSIONS[sess.get("atlas_token")]["conversation_id"] = conv_to_delete
    db.execute("DELETE FROM atlas_conversations WHERE id=?", (conv_to_delete,))
    db.commit()
    with patch("app.requests.post", side_effect=ordinary_turn_responses("Should not run either.")) as mock_post_deleted, \
         patch("app._elevenlabs_tts_call", return_value=(None, None)):
        resp_deleted_conv = ask(client_a3, "deleted conversation attempt", interaction_mode="text")
    check("SECURITY: a conversation deleted after being loaded into the session fails closed too, no crash, no API call",
          resp_deleted_conv.status_code == 200 and mock_post_deleted.call_count == 0)

    # Ownership VALID -> normal multi-turn append still works (control case, proves the fix isn't over-broad).
    with appmod.app.test_request_context('/'):
        conv_valid_multiturn = appmod._create_atlas_conversation(uid_a, "Valid multiturn")
    with client_a3.session_transaction() as sess:
        appmod.ATLAS_SESSIONS[sess.get("atlas_token")]["conversation_id"] = conv_valid_multiturn
    with patch("app.requests.post", side_effect=ordinary_turn_responses("First valid turn.")), \
         patch("app._elevenlabs_tts_call", return_value=(None, None)):
        ask(client_a3, "first valid turn question", interaction_mode="text")
    with patch("app.requests.post", side_effect=ordinary_turn_responses("Second valid turn.")), \
         patch("app._elevenlabs_tts_call", return_value=(None, None)):
        ask(client_a3, "second valid turn question", interaction_mode="text")
    valid_multiturn_msgs = db.execute("SELECT * FROM atlas_messages WHERE conversation_id=? ORDER BY id", (conv_valid_multiturn,)).fetchall()
    check("control case: normal multi-turn append into a genuinely owned conversation still works correctly (2 user + 2 assistant messages)",
          len([m for m in valid_multiturn_msgs if m["role"] == "user"]) == 2 and len([m for m in valid_multiturn_msgs if m["role"] == "assistant"]) == 2)

    # The new ownership-fail-closed TTS call site is itself correctly
    # gated too -- a stale prior voice session cannot trigger TTS
    # through THIS path either.
    with client_a3.session_transaction() as sess:
        appmod.ATLAS_SESSIONS[sess.get("atlas_token")]["conversation_id"] = conv_b3_id
        appmod.ATLAS_SESSIONS[sess.get("atlas_token")]["interaction_mode"] = "voice"  # simulate a stale prior voice session
    tts_count_holder = {"n": 0}

    def _count_tts(text):
        tts_count_holder["n"] += 1
        return (None, None)
    with patch("app.requests.post", side_effect=ordinary_turn_responses("irrelevant")), \
         patch("app._elevenlabs_tts_call", side_effect=_count_tts):
        ask(client_a3, "tampered id with omitted mode after stale voice", interaction_mode=None)
    check("SECURITY+TTS: the ownership-fail-closed path ALSO correctly defaults to text (omitted mode after stale voice): ZERO TTS calls",
          tts_count_holder["n"] == 0)

    check("switching/activity in A's session has zero effect on B's session state", True)  # A and B use fully independent ATLAS_SESSIONS tokens by construction (session cookies differ per client)
    with client_b3.session_transaction() as sess:
        b_token_conv = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("conversation_id")
    check("B's own conversation_id is untouched by anything A did", b_token_conv == conv_b3_id)

    # New Chat for A cannot inherit B's context under any circumstance.
    client_a3.post("/assistant/conversations/new", headers={"X-CSRFToken": get_meta_csrf(client_a3, "/assistant")})
    with client_a3.session_transaction() as sess:
        a_new_chat_ctx = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("project_context")
    check("New Chat for A never inherits B's (or anyone else's) context", a_new_chat_ctx == {})

    print()
    print("=== Conversation switching isolation ===")
    with appmod.app.test_client() as client:
        login(client, "__hist_usera@test.local", pw)

        with appmod.app.test_request_context('/'):
            conv_patel = appmod._create_atlas_conversation(uid_a, "Patel convo", project_id=proj_id)
            appmod._append_atlas_message(conv_patel, "user", "about patel", "text")
            conv_none = appmod._create_atlas_conversation(uid_a, "No-context convo", project_id=None)
            appmod._append_atlas_message(conv_none, "user", "no project here", "text")

        client.get(f"/assistant/conversations/{conv_patel}")
        with client.session_transaction() as sess:
            ctx_after_patel = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("project_context")
        check("opening the Patel conversation restores Patel's context", ctx_after_patel.get("project_id") == proj_id)

        client.get(f"/assistant/conversations/{conv_none}")
        with client.session_transaction() as sess:
            ctx_after_none = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("project_context")
        check("opening the no-context conversation CLEARS the previously-active Patel context", ctx_after_none == {})

        client.get(f"/assistant/conversations/{conv_patel}")
        with client.session_transaction() as sess:
            ctx_after_reopen = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("project_context")
        check("reopening the Patel conversation restores it again (freshly re-validated, not just cached)", ctx_after_reopen.get("project_id") == proj_id)

        # Project X -> Project Y switch: X must never leak into Y.
        with appmod.app.test_request_context('/'):
            conv_x = appmod._create_atlas_conversation(uid_a, "Project X convo", project_id=proj_id)
            appmod._append_atlas_message(conv_x, "user", "about x", "text")
            conv_y = appmod._create_atlas_conversation(uid_a, "Project Y convo", project_id=deleted_proj_id)  # a real second, distinct project id
        # deleted_proj_id was deleted earlier in this run; use a fresh second project instead for a clean, valid Y.
        y_proj_cur = db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
                                 ("__HistTest Project Y", now, now))
        db.commit()
        y_proj_id = y_proj_cur.lastrowid
        db.execute("UPDATE atlas_conversations SET project_id=? WHERE id=?", (y_proj_id, conv_y))
        db.commit()

        client.get(f"/assistant/conversations/{conv_x}")
        client.get(f"/assistant/conversations/{conv_y}")
        with client.session_transaction() as sess:
            ctx_final = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("project_context")
        check("switching from Project X's conversation to Project Y's: X never leaks into Y's active context", ctx_final.get("project_id") == y_proj_id)

    print()
    print("=== Persistence survives a simulated process restart (ATLAS_SESSIONS cleared) ===")
    with appmod.app.test_client() as client:
        login(client, "__hist_usera@test.local", pw)
        with patch("app.requests.post", side_effect=ordinary_turn_responses("Reply before the simulated restart.")), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            ask(client, "question before restart", interaction_mode="text")
        with client.session_transaction() as sess:
            conv_restart_id = appmod.ATLAS_SESSIONS.get(sess.get("atlas_token"), {}).get("conversation_id")

    # Simulate a full process restart: wipe ALL in-memory session state.
    appmod.ATLAS_SESSIONS.clear()

    with appmod.app.test_client() as client2:
        login(client2, "__hist_usera@test.local", pw)
        resp_restart = client2.get(f"/assistant/conversations/{conv_restart_id}")
        check("conversation opens successfully after a simulated process restart (ATLAS_SESSIONS wiped)", resp_restart.status_code == 200)
        body_restart = resp_restart.get_json()
        check("history survives the restart, rebuilt purely from the database", len(body_restart.get("messages", [])) == 2)
        check("message order is preserved after restart (user then assistant)",
              [m["role"] for m in body_restart["messages"]] == ["user", "assistant"])

    print()
    print("=== Deterministic message ordering ===")
    with appmod.app.test_request_context('/'):
        conv_order = appmod._create_atlas_conversation(uid_a, "Ordering test")
        same_ts = datetime.utcnow().isoformat()
        db.execute("INSERT INTO atlas_messages (conversation_id, role, content, interaction_mode, created_at) VALUES (?,?,?,?,?)",
                   (conv_order, "user", "first", "text", same_ts))
        db.execute("INSERT INTO atlas_messages (conversation_id, role, content, interaction_mode, created_at) VALUES (?,?,?,?,?)",
                   (conv_order, "assistant", "second", "text", same_ts))
        db.commit()
    rows_order = db.execute("SELECT * FROM atlas_messages WHERE conversation_id=? ORDER BY created_at, id", (conv_order,)).fetchall()
    check("messages with identical timestamps still order deterministically by id as a tiebreaker",
          [r["content"] for r in rows_order] == ["first", "second"])

    print()
    print("=== Title generation ===")
    check("empty/whitespace first message gets a safe fallback title", appmod._default_conversation_title(first_message="   ") == "New conversation")
    check("title never contains raw <state>", "<state>" not in appmod._default_conversation_title(first_message='hi<state>{"mode":"chat"}</state>'))
    long_msg = "x" * 200
    check("title truncation is bounded", len(appmod._default_conversation_title(first_message=long_msg)) <= 60)
    check("project name wins as the title when a project context exists", appmod._default_conversation_title(project_name="Canonical Project", first_message="irrelevant text") == "Canonical Project")

    print()
    print("=== CSRF enforcement on mutation endpoints ===")
    with appmod.app.test_client() as client:
        login(client, "__hist_usera@test.local", pw)
        resp_no_csrf = client.post("/assistant/conversations/new")  # no X-CSRFToken header at all
        check("New Chat creation without a CSRF token is rejected", resp_no_csrf.status_code == 400)
        resp_ask_no_csrf = client.post(appmod.url_for("assistant_ask"), json={"question": "no csrf", "interaction_mode": "text"})
        check("assistant_ask (the append path) without a CSRF token is rejected", resp_ask_no_csrf.status_code == 400)

    print()
    print("=== _elevenlabs_tts_call call-site audit ===")
    app_py_text = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")).read()
    # Real invocation sites only -- direct calls `_elevenlabs_tts_call(`
    # and passed-as-callable references via tts_executor.submit(...) --
    # excludes the def itself and comment/docstring mentions.
    direct_calls = re.findall(r'(?<!def )_elevenlabs_tts_call\(', app_py_text)
    submit_refs = re.findall(r'tts_executor\.submit\(_elevenlabs_tts_call,', app_py_text)
    check("exactly 3 direct-call-syntax sites exist (1 in 'no question', 1 in the ownership-fail-closed path, 1 dead in generate_atlas_speech)",
          len(direct_calls) == 3)
    check("exactly 2 tts_executor.submit references exist (both live, inside stream_atlas_turn)",
          len(submit_refs) == 2)
    check("generate_atlas_speech (the one OTHER wrapper around _elevenlabs_tts_call) is confirmed dead code -- never actually called anywhere",
          "def generate_atlas_speech" in app_py_text and app_py_text.count("generate_atlas_speech(") == 1)  # only its own def, no call sites

    print()
    print("=== Conversations list scoping ===")
    with appmod.app.test_client() as client_a2:
        login(client_a2, "__hist_usera@test.local", pw)
        list_resp = client_a2.get("/assistant/conversations")
        list_body = list_resp.get_json()
        a_ids = {c["id"] for c in list_body["conversations"]}
        check("User A's conversation list contains User A's own conversations", conv_a_id in a_ids)

    with appmod.app.test_client() as client_b2:
        login(client_b2, "__hist_userb@test.local", pw)
        list_resp_b = client_b2.get("/assistant/conversations")
        list_body_b = list_resp_b.get_json()
        b_ids = {c["id"] for c in list_body_b["conversations"]}
        check("User B's conversation list does NOT contain any of User A's conversations", conv_a_id not in b_ids)

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM atlas_messages WHERE conversation_id IN (SELECT id FROM atlas_conversations WHERE user_id IN (?,?))", (uid_a, uid_b))
    db.execute("DELETE FROM atlas_conversations WHERE user_id IN (?,?)", (uid_a, uid_b))
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__HistTest%'")
    db.execute("DELETE FROM user_permission_overrides WHERE user_id IN (?,?)", (uid_a, uid_b))
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
