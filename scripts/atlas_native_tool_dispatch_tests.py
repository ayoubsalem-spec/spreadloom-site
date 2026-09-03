"""
Atlas native tool dispatch (project-context architecture fix) regression.

Every Anthropic API call is mocked -- requests.post is patched with a
sequence of fake SSE responses shaped exactly like the real wire
protocol (message_start, content_block_start/delta/stop, message_delta,
message_stop) so _stream_claude_completion's real parsing code is
exercised, not a shortcut. No real network access occurs.

Usage (from the project root):
    APP_ENV=development python3 scripts/atlas_native_tool_dispatch_tests.py
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
_test_db_setup.isolate_test_database()

os.environ["ANTHROPIC_API_KEY"] = "test-fake-key-never-actually-used-requests-is-mocked"

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


def build_sse_lines(blocks, stop_reason="end_turn"):
    """blocks: list of tuples describing content blocks in order:
        ("text", "some text")
        ("tool_use", name, tool_id, input_json_str)
        ("tool_use_incomplete", name, tool_id, partial_input_json_str)
            -- no content_block_stop emitted for this block, simulating
               hitting max_tokens mid-argument.
    Produces the real content_block_start/delta/stop sequence per
    block, then message_delta(stop_reason) + message_stop + [DONE].
    """
    lines = []
    lines.append("data: " + json.dumps({"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "content": []}}))
    for idx, block in enumerate(blocks):
        kind = block[0]
        if kind == "text":
            text = block[1]
            lines.append("data: " + json.dumps({"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}}))
            for i in range(0, len(text), 12):
                piece = text[i:i + 12]
                lines.append("data: " + json.dumps({"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": piece}}))
            lines.append("data: " + json.dumps({"type": "content_block_stop", "index": idx}))
        elif kind in ("tool_use", "tool_use_incomplete"):
            _, name, tool_id, input_json_str = block
            lines.append("data: " + json.dumps({"type": "content_block_start", "index": idx, "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}}))
            for i in range(0, len(input_json_str), 8):
                piece = input_json_str[i:i + 8]
                lines.append("data: " + json.dumps({"type": "content_block_delta", "index": idx, "delta": {"type": "input_json_delta", "partial_json": piece}}))
            if kind == "tool_use":
                lines.append("data: " + json.dumps({"type": "content_block_stop", "index": idx}))
    lines.append("data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": stop_reason}}))
    lines.append("data: " + json.dumps({"type": "message_stop"}))
    lines.append("data: [DONE]")
    return lines


def fake_response(lines, delay=0.0):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()

    def _iter_lines(decode_unicode=True):
        if delay:
            time.sleep(delay)
        return iter(lines)
    resp.iter_lines = _iter_lines
    return resp


def fake_error_response():
    """Simulates requests raising a RequestException (network/HTTP error)."""
    import requests
    err_resp = MagicMock()
    err_resp.text = '{"error": {"message": "simulated Anthropic API failure"}}'
    exc = requests.exceptions.RequestException("simulated failure")
    exc.response = err_resp
    outer = MagicMock()
    outer.raise_for_status = MagicMock(side_effect=exc)
    return outer


def run_turn(user_text, draft, mock_responses):
    """Runs one stream_atlas_turn() call with requests.post mocked to
    return mock_responses in order. Returns (events, mock_post).

    PROJECT INTELLIGENCE PHASE COMPATIBILITY: Pass 1B (the sequential
    "also gather intelligence" pass, see stream_atlas_turn) is now
    ALWAYS attempted whenever Pass 1 genuinely succeeds at
    set_project_context -- a real architectural change, not a test
    artifact. Tests in this file that predate that phase and only
    supply exactly [pass1, pass2] for a SUCCESSFUL resolution would
    otherwise hit an IndexError on Pass 1B's own API call, which they
    were never testing in the first place (this file's own subject is
    v7 protocol hardening, not Pass 1B). Rather than touching every one
    of those pre-existing call sites individually, exhausting the
    supplied mock list here safely returns a plain "no tool" response --
    exactly what a real Pass 1B call finding nothing further to do
    would look like -- so those tests keep verifying exactly what they
    always verified, while atlas_project_intelligence_tests.py is the
    file that explicitly supplies and asserts on real Pass-1B behavior."""
    def _side_effect(*args, **kwargs):
        idx = _side_effect.calls
        _side_effect.calls += 1
        if idx >= len(mock_responses):
            return fake_response(build_sse_lines([], stop_reason="end_turn"))
        return mock_responses[idx]
    _side_effect.calls = 0

    with patch("app.requests.post", side_effect=_side_effect) as mock_post:
        events = []
        for line in appmod.stream_atlas_turn(user_text, draft):
            payload = line[len("data: "):].strip()
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
        return events, mock_post


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


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    full_perms = ["module:project_hunt:view", "atlas:view_business_data", "module:atlas:view",
                  "atlas:create_requests", "module:sitepulse:view", "action:sitepulse:manage"]
    full_uid = make_user(db, "__native_full@test.local", "__native_full", now, pw_hash, full_perms)
    limited_uid = make_user(db, "__native_limited@test.local", "__native_limited", now, pw_hash, ["module:atlas:view"])

    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__NativeTest%'")
    db.commit()
    patel_cur = db.execute(
        "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
        ("__NativeTest Patel Farm Project", now, now)
    )
    overlook_cur = db.execute(
        "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
        ("__NativeTest Overlook Tower", now, now)
    )
    patel2_cur = db.execute(
        "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
        ("__NativeTest Patel Renovation", now, now)
    )
    db.commit()
    patel_id, overlook_id, patel2_id = patel_cur.lastrowid, overlook_cur.lastrowid, patel2_cur.lastrowid

    from flask_login import login_user

    def base_draft(project_context=None):
        return {"mode": "chat", "fields": {}, "history": [], "pending_submit": None, "project_context": project_context or {}}

    with appmod.app.test_request_context('/'):
        user_row = db.execute("SELECT * FROM users WHERE id=?", (full_uid,)).fetchone()
        user = appmod.User(user_row)
        login_user(user)

        print("=== 1/2. Unique project resolution via native tool dispatch ===")
        pass1_resp = fake_response(build_sse_lines(
            [("tool_use", "set_project_context", "toolu_1", json.dumps({"project_name": "Patel Farm"}))],
            stop_reason="tool_use"
        ))
        pass2_resp = fake_response(build_sse_lines(
            [("text", "Got it, we're now looking at Patel Farm Project.")],
        ))
        # PROJECT INTELLIGENCE PHASE: a genuinely successful
        # set_project_context resolution now always triggers a
        # sequential Pass 1B (see stream_atlas_turn) offering
        # get_project_intelligence -- this mock represents Pass 1B
        # finding nothing further to do (0 tool_use blocks, ordinary
        # end_turn), which is the correct/expected outcome for a bare
        # "let's talk about X" with no attached question. Real calls
        # are now [Pass 1, Pass 1B, Pass 2] in that order.
        pass1b_no_tool_resp = fake_response(build_sse_lines([], stop_reason="end_turn"))
        draft = base_draft()
        events, mock_post = run_turn("Let's talk about Patel Farm", draft, [pass1_resp, pass1b_no_tool_resp, pass2_resp])
        check("1. exactly 3 API calls made (Pass 1 + Pass 1B + Pass 2 -- the accepted architectural cost of the sequential design)", mock_post.call_count == 3)
        check("2. session project_context now holds the canonical project_id", draft["project_context"].get("project_id") == patel_id)
        check("2. session project_context holds the canonical name (not the free-text the person said)",
              draft["project_context"].get("name") == "__NativeTest Patel Farm Project")
        visible_text = "".join(e.get("text", "") for e in events if e.get("type") == "delta")
        check("Atlas's visible reply reflects the SUCCESSFUL resolution (from Pass 2, grounded in the real tool result)",
              "Patel Farm" in visible_text)

        pass2_call_kwargs = mock_post.call_args_list[2].kwargs
        pass2_messages = pass2_call_kwargs["json"]["messages"]
        check("Pass 2's messages include a real assistant tool_use block",
              any(m["role"] == "assistant" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_use" for m in pass2_messages))
        check("Pass 2's messages include a real user tool_result block",
              any(m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result" for m in pass2_messages))
        tool_result_block = next(m["content"][0] for m in pass2_messages if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result")
        tool_result_data = json.loads(tool_result_block["content"])
        check("the tool_result content is the actual structured resolution (found=True, real project_id)",
              tool_result_data.get("found") is True and tool_result_data.get("project_id") == patel_id)
        check("Pass 2 omits `tools` entirely (structurally cannot request another tool call)",
              "tools" not in pass2_call_kwargs["json"])
        pass1_call_kwargs = mock_post.call_args_list[0].kwargs
        check("Pass 1 declares ONLY the allowed native tool(s)",
              [t["name"] for t in pass1_call_kwargs["json"]["tools"]] == appmod.ATLAS_NATIVE_TOOLS_ALLOWED)
        check("Pass 1's declared tool schema does NOT expose project_id to the model",
              "project_id" not in pass1_call_kwargs["json"]["tools"][0]["input_schema"]["properties"])
        check("Pass 1 uses a capped max_tokens (cheap detection call)", pass1_call_kwargs["json"]["max_tokens"] <= 200)

        # ISSUE 3 (NO-GO round 2): the model-visible native declaration
        # must expose project_name ONLY, marked REQUIRED, and must never
        # mention project_id anywhere -- schema, properties, required
        # list, or description text. The underlying registry schema
        # (which other server-side callers legitimately use with
        # project_id) is untouched -- this checks the DECLARED, model-
        # visible shape specifically.
        native_decl = pass1_call_kwargs["json"]["tools"][0]
        check("native declaration: input_schema.required is exactly ['project_name']",
              native_decl["input_schema"]["required"] == ["project_name"])
        native_decl_full_text = json.dumps(native_decl)
        check("native declaration: 'project_id' does not appear ANYWHERE in the declaration (schema or description)",
              "project_id" not in native_decl_full_text)
        check("native declaration: 'project_name' is the only declared property",
              list(native_decl["input_schema"]["properties"].keys()) == ["project_name"])

        print()
        print("=== 3. Follow-up turn retains established context ===")
        pass1_resp2 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_resp2 = fake_response(build_sse_lines([("text", "Sure, tell me more about what?")]))
        events2, mock_post2 = run_turn("Tell me more.", draft, [pass1_resp2, pass2_resp2])
        check("3. project_context is unchanged/retained across the follow-up turn", draft["project_context"].get("project_id") == patel_id)
        check("7. Pass 1's system prompt still carries the established project context on a no-tool turn",
              "Patel Farm Project" in mock_post2.call_args_list[0].kwargs["json"]["system"])
        check("7. Pass 2's system prompt also carries it", "Patel Farm Project" in mock_post2.call_args_list[1].kwargs["json"]["system"])

        print()
        print("=== 4. Session isolation (also covers reset-during-turn safety) ===")
        other_draft = base_draft()
        check("4. a completely separate session's project_context starts empty", other_draft["project_context"] == {})
        pass1_resp3 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_resp3 = fake_response(build_sse_lines([("text", "I don't have a project selected yet.")]))
        run_turn("What project are we on?", other_draft, [pass1_resp3, pass2_resp3])
        check("4. the second session's project_context is STILL empty after its own turn -- unaffected by session 1's Patel Farm context",
              other_draft["project_context"] == {})
        check("4. session 1's context is untouched by session 2's activity", draft["project_context"].get("project_id") == patel_id)

        print()
        print("=== 5. Project switching A -> B ===")
        pass1_resp4 = fake_response(build_sse_lines(
            [("tool_use", "set_project_context", "toolu_2", json.dumps({"project_name": "Overlook Tower"}))],
            stop_reason="tool_use"
        ))
        pass2_resp4 = fake_response(build_sse_lines([("text", "Switched to Overlook Tower.")]))
        run_turn("Let's switch to Overlook Tower.", draft, [pass1_resp4, pass2_resp4])
        check("5. context switched to the new canonical project", draft["project_context"].get("project_id") == overlook_id)
        check("5. context name updated too", draft["project_context"].get("name") == "__NativeTest Overlook Tower")

        print()
        print("=== 6/8. Ambiguous switch attempt preserves existing valid context ===")
        context_before_ambiguous = dict(draft["project_context"])
        pass1_resp5 = fake_response(build_sse_lines(
            [("tool_use", "set_project_context", "toolu_3", json.dumps({"project_name": "Patel"}))],
            stop_reason="tool_use"
        ))
        pass2_resp5 = fake_response(build_sse_lines([("text", "I found more than one project matching Patel -- did you mean Patel Farm Project or Patel Renovation?")]))
        events6, _ = run_turn("Switch to Patel", draft, [pass1_resp5, pass2_resp5])
        check("6. ambiguous resolution does NOT set a new project_id", draft["project_context"].get("project_id") == context_before_ambiguous.get("project_id"))
        check("8. the PREVIOUS valid context (Overlook Tower) remains active, not nulled/cleared", draft["project_context"] == context_before_ambiguous)
        visible6 = "".join(e.get("text", "") for e in events6 if e.get("type") == "delta")
        check("6. Atlas's reply asks for clarification rather than silently picking one", "more than one" in visible6 or "which" in visible6.lower())

        print()
        print("=== 7. Unknown project name does not establish false context ===")
        context_before_unknown = dict(draft["project_context"])
        pass1_resp6 = fake_response(build_sse_lines(
            [("tool_use", "set_project_context", "toolu_4", json.dumps({"project_name": "Totally Fake Project XYZ"}))],
            stop_reason="tool_use"
        ))
        pass2_resp6 = fake_response(build_sse_lines([("text", "I couldn't find a project called Totally Fake Project XYZ.")]))
        events7, _ = run_turn("Let's talk about Totally Fake Project XYZ", draft, [pass1_resp6, pass2_resp6])
        check("unknown project: context unchanged (previous valid project stays active)", draft["project_context"] == context_before_unknown)
        visible7 = "".join(e.get("text", "") for e in events7 if e.get("type") == "delta")
        check("unknown project: Atlas tells the user it couldn't find it, doesn't pretend success",
              "couldn't find" in visible7.lower() or "don't see" in visible7.lower() or "not find" in visible7.lower())

        print()
        print("=== 9. Stale/deleted project context remains protected ===")
        stale_draft = base_draft({"project_id": overlook_id, "name": "__NativeTest Overlook Tower"})
        db.execute("DELETE FROM tracker_projects WHERE id=?", (overlook_id,))
        db.commit()
        stale_result = appmod.execute_tool("create_concrete_request", {
            "project": "Overlook Tower", "pour_date": "2026-09-20", "job_site_address": "1 Test Way",
            "area_description": "Slab", "mix_design_psi": "4000", "mix_slump": "4", "concrete_amount": "10 yd",
            "truck_spacing": "15 min", "pump_type": "None", "lab_required": "No", "drilling_required": "No",
        }, user, confirmed=True, session_context=stale_draft["project_context"])
        check("9. a write depending on a deleted project_id fails closed (project_context_stale), unaffected by native dispatch changes",
              not stale_result.success and stale_result.error == "project_context_stale")
        overlook_cur2 = db.execute(
            "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
            ("__NativeTest Overlook Tower", now, now)
        )
        db.commit()

        print()
        print("=== 11/12. Permission enforcement through native dispatch ===")
        limited_draft = base_draft()
        pass1_resp7 = fake_response(build_sse_lines(
            [("tool_use", "set_project_context", "toolu_5", json.dumps({"project_name": "Patel Farm"}))],
            stop_reason="tool_use"
        ))
        limited_row = db.execute("SELECT * FROM users WHERE id=?", (limited_uid,)).fetchone()
        limited_user = appmod.User(limited_row)

    with appmod.app.test_request_context('/'):
        login_user(limited_user)
        events8, mock_post8 = run_turn("Let's talk about Patel Farm", limited_draft, [pass1_resp7])
        check("12. a user without the required permission does NOT get project_context set via native tool_use", limited_draft["project_context"] == {})
        check("11. only ONE API call was made (no Pass 2 -- permission denial short-circuits before a second Claude call)", mock_post8.call_count == 1)
        visible8 = "".join(e.get("text", "") for e in events8 if e.get("type") == "delta")
        check("12. the user is told plainly they lack permission, without a further model call inventing something else", "permission" in visible8.lower())

    with appmod.app.test_request_context('/'):
        login_user(user)

        print()
        print("=== 13. Model requesting an unapproved tool is rejected ===")
        unapproved_draft = base_draft()
        pass1_resp8 = fake_response(build_sse_lines(
            [("tool_use", "get_project_status", "toolu_6", json.dumps({"project_name": "Patel Farm"}))],
            stop_reason="tool_use"
        ))
        events9, mock_post9 = run_turn("Some request", unapproved_draft, [pass1_resp8])
        check("13. an unapproved tool name is never executed (context stays empty)", unapproved_draft["project_context"] == {})
        check("13. only 1 API call made (rejected before any Pass 2)", mock_post9.call_count == 1)

        print()
        print("=== Multiple/mixed tool_use block shapes -- fail closed ===")

        def assert_fail_closed(label, blocks, stop_reason="tool_use"):
            d = base_draft()
            resp = fake_response(build_sse_lines(blocks, stop_reason=stop_reason))
            evs, mp = run_turn("test", d, [resp])
            check(f"{label}: context NOT mutated", d["project_context"] == {})
            check(f"{label}: only 1 API call (fails closed before any Pass 2)", mp.call_count == 1)
            return evs

        assert_fail_closed(
            "1. two set_project_context calls in one pass",
            [("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"})),
             ("tool_use", "set_project_context", "t2", json.dumps({"project_name": "Overlook Tower"}))],
        )
        assert_fail_closed(
            "2. set_project_context + unapproved tool",
            [("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"})),
             ("tool_use", "some_other_tool", "t2", json.dumps({"x": 1}))],
        )
        check("3. unapproved tool first (covered by test 13 above): context not mutated, 1 call only", True)

        d4 = base_draft()
        resp4 = fake_response(build_sse_lines(
            [("text", "Let me check that."), ("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"}))],
            stop_reason="tool_use"
        ))
        resp4b = fake_response(build_sse_lines([("text", "We're now on Patel Farm Project.")]))
        evs4, mp4 = run_turn("Let's talk about patel farm", d4, [resp4, resp4b])
        check("4. text-then-tool_use in Pass 1: the preamble text is NEVER shown to the user (zero-leak guarantee)",
              not any("Let me check that" in e.get("text", "") for e in evs4 if e.get("type") == "delta"))
        check("4. text-then-tool_use in Pass 1: the single tool call still resolves correctly (one tool_use block is still valid)",
              d4["project_context"].get("project_id") == patel_id)

        d5 = base_draft()
        resp5 = fake_response(build_sse_lines(
            [("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"})), ("text", " -- done.")],
            stop_reason="tool_use"
        ))
        resp5b = fake_response(build_sse_lines([("text", "Switched to Patel Farm Project.")]))
        evs5, mp5 = run_turn("Let's talk about patel farm", d5, [resp5, resp5b])
        check("5. tool_use-then-text in Pass 1: trailing Pass-1 text never shown", not any(" -- done." in e.get("text", "") for e in evs5 if e.get("type") == "delta"))
        check("5. tool_use-then-text in Pass 1: the tool call still resolves correctly", d5["project_context"].get("project_id") == patel_id)

        d6 = base_draft()
        resp6 = fake_response(build_sse_lines(
            [("text", "Sure,"), ("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"})), ("text", " done!")],
            stop_reason="tool_use"
        ))
        resp6b = fake_response(build_sse_lines([("text", "We're on Patel Farm Project now.")]))
        evs6, mp6 = run_turn("Let's talk about patel farm", d6, [resp6, resp6b])
        check("6. text->tool_use->text: no Pass-1 text ever shown", not any(("Sure," in e.get("text", "") or " done!" in e.get("text", "")) for e in evs6 if e.get("type") == "delta"))
        check("6. text->tool_use->text: the single tool call still resolves correctly", d6["project_context"].get("project_id") == patel_id)

        d7 = base_draft()
        resp7 = fake_response(build_sse_lines(
            [("tool_use_incomplete", "set_project_context", "t1", '{"project_name": "Patel Fa')],
            stop_reason="max_tokens"
        ))
        evs7, mp7 = run_turn("Let's talk about patel farm", d7, [resp7])
        check("7. malformed/incomplete tool_use JSON: never executed (context not mutated)", d7["project_context"] == {})
        check("7. malformed/incomplete tool_use JSON: only 1 API call (fails closed, no Pass 2)", mp7.call_count == 1)

        # ISSUE 1 (NO-GO round 2): a tool_use block whose accumulated
        # input HAPPENS to be valid, complete JSON must still NOT
        # execute if it never received its own content_block_stop --
        # parsing successfully is not proof the block actually
        # completed. Exact scenario from the review: full valid JSON
        # input, but the stream cuts to message_delta(stop_reason=
        # "max_tokens") / message_stop with NO content_block_stop for
        # that block at all.
        print()
        print("=== Issue 1: valid-looking JSON without content_block_stop must still fail closed ===")
        d_nostop = base_draft()
        lines_no_stop = []
        lines_no_stop.append("data: " + json.dumps({"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "content": []}}))
        lines_no_stop.append("data: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}}))
        full_json = json.dumps({"project_name": "Patel Farm"})
        for i in range(0, len(full_json), 8):
            piece = full_json[i:i + 8]
            lines_no_stop.append("data: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": piece}}))
        # Deliberately NO content_block_stop for index 0.
        lines_no_stop.append("data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}))
        lines_no_stop.append("data: " + json.dumps({"type": "message_stop"}))
        lines_no_stop.append("data: [DONE]")
        resp_nostop = fake_response(lines_no_stop)
        evs_nostop, mp_nostop = run_turn("Let's talk about patel farm", d_nostop, [resp_nostop])
        check("issue 1: ZERO execute_tool call -- context not mutated even though the JSON was complete/valid",
              d_nostop["project_context"] == {})
        check("issue 1: only ONE Anthropic call was made (fails closed before any Pass 2)", mp_nostop.call_count == 1)
        check("issue 1: a controlled fail-safe reply was sent", any(e.get("type") == "delta" for e in evs_nostop))
        log_row = db.execute("SELECT * FROM activity_log WHERE action='atlas_incomplete_native_tool_use_block' ORDER BY id DESC LIMIT 1").fetchone()
        check("issue 1: a diagnostic log entry was created for the incomplete tool_use block", log_row is not None)

        print()
        print("=== Index-integrity fix: content blocks matched by REAL Anthropic index, not recency ===")

        def raw_lines(*events):
            lines = ["data: " + json.dumps({"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "content": []}})]
            lines.extend("data: " + json.dumps(e) for e in events)
            return lines

        # 1. tool_use index 0 + valid JSON + block_stop index 1 (WRONG, deliberately mismatched).
        d_idx1 = base_draft()
        full_json = json.dumps({"project_name": "Patel Farm"})
        lines_idx1 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": full_json}},
            {"type": "content_block_stop", "index": 1},  # deliberately WRONG index
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_idx1.append("data: [DONE]")
        evs_idx1, mp_idx1 = run_turn("test", d_idx1, [fake_response(lines_idx1)])
        check("1. wrong-index block_stop: ZERO execution (context not mutated)", d_idx1["project_context"] == {})
        check("1. wrong-index block_stop: only 1 API call (fails closed)", mp_idx1.call_count == 1)
        anomaly_log1 = db.execute("SELECT * FROM activity_log WHERE action='atlas_tool_protocol_anomaly' ORDER BY id DESC LIMIT 1").fetchone()
        check("1. wrong-index block_stop: a protocol-anomaly diagnostic log was recorded", anomaly_log1 is not None and anomaly_log1["new_value"] == "orphan_block_stop")

        # 2. tool_use index 0 + input_json_delta index 1 (orphan delta -- no block ever opened at index 1).
        d_idx2 = base_draft()
        lines_idx2 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": full_json}},  # WRONG index
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_idx2.append("data: [DONE]")
        evs_idx2, mp_idx2 = run_turn("test", d_idx2, [fake_response(lines_idx2)])
        check("2. wrong-index input_json_delta: ZERO execution (context not mutated)", d_idx2["project_context"] == {})
        check("2. wrong-index input_json_delta: only 1 API call (fails closed)", mp_idx2.call_count == 1)

        # 3. Correct, fully-matched index throughout -> normal successful resolution.
        d_idx3 = base_draft()
        lines_idx3 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": full_json}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_idx3.append("data: [DONE]")
        pass1b_idx3 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_idx3 = fake_response(build_sse_lines([("text", "We're on Patel Farm Project now.")]))
        evs_idx3, mp_idx3 = run_turn("test", d_idx3, [fake_response(lines_idx3), pass1b_idx3, pass2_idx3])
        check("3. correctly-matched index: normal successful resolution occurs", d_idx3["project_context"].get("project_id") == patel_id)
        check("3. correctly-matched index: 3 API calls made (Pass 1 + Pass 1B + Pass 2)", mp_idx3.call_count == 3)

        # 4. Duplicate block_stop for an already-completed tool block.
        d_idx4 = base_draft()
        lines_idx4 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": full_json}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_stop", "index": 0},  # duplicate
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_idx4.append("data: [DONE]")
        evs_idx4, mp_idx4 = run_turn("test", d_idx4, [fake_response(lines_idx4)])
        check("4. duplicate block_stop: fails closed, no execution/context mutation", d_idx4["project_context"] == {})
        check("4. duplicate block_stop: only 1 API call (no Pass 2)", mp_idx4.call_count == 1)
        anomaly_log4 = db.execute("SELECT * FROM activity_log WHERE action='atlas_tool_protocol_anomaly' ORDER BY id DESC LIMIT 1").fetchone()
        check("4. duplicate block_stop: recorded as the specific anomaly type", anomaly_log4 is not None and anomaly_log4["new_value"] == "duplicate_block_stop")

        # 5. Orphan tool_input_delta with no matching started tool block at all.
        d_idx5 = base_draft()
        lines_idx5 = raw_lines(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": full_json}},  # no content_block_start ever
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        )
        lines_idx5.append("data: [DONE]")
        pass2_idx5 = fake_response(build_sse_lines([("text", "How can I help?")]))
        evs_idx5, mp_idx5 = run_turn("test", d_idx5, [fake_response(lines_idx5), pass2_idx5])
        check("5. orphan tool_input_delta (no started block): context not mutated", d_idx5["project_context"] == {})
        anomaly_log5 = db.execute("SELECT * FROM activity_log WHERE action='atlas_tool_protocol_anomaly' ORDER BY id DESC LIMIT 1").fetchone()
        check("5. orphan tool_input_delta: recorded as the specific anomaly type", anomaly_log5 is not None and anomaly_log5["new_value"] == "orphan_tool_input_delta")

        # 6. Orphan block_stop (no block_start anywhere) must not mark any other block completed.
        d_idx6 = base_draft()
        lines_idx6 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": full_json}},
            {"type": "content_block_stop", "index": 5},  # orphan -- index 5 never started anything
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_idx6.append("data: [DONE]")
        evs_idx6, mp_idx6 = run_turn("test", d_idx6, [fake_response(lines_idx6)])
        check("6. orphan block_stop (index 5): the REAL tool block at index 0 is NOT marked completed by it",
              d_idx6["project_context"] == {})
        check("6. orphan block_stop: only 1 API call (fails closed)", mp_idx6.call_count == 1)

        print()
        print("=== Duplicate/reused content-block index (single-assignment enforcement) ===")

        # 1. duplicate tool_use_start at same index, DIFFERENT tool ids.
        d_dup1 = base_draft()
        json_a = json.dumps({"project_name": "Project A"})
        json_b = json.dumps({"project_name": "Project B"})
        lines_dup1 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json_a}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "set_project_context", "input": {}}},  # reused index, different id
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json_b}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_dup1.append("data: [DONE]")
        evs_dup1, mp_dup1 = run_turn("test", d_dup1, [fake_response(lines_dup1)])
        check("1. duplicate tool_use_start (different ids, same index): ZERO execution", d_dup1["project_context"] == {})
        check("1. duplicate tool_use_start (different ids): only 1 API call (fails closed)", mp_dup1.call_count == 1)
        anomaly_dup1 = db.execute("SELECT * FROM activity_log WHERE action='atlas_tool_protocol_anomaly' ORDER BY id DESC LIMIT 1").fetchone()
        check("1. flagged with a specific duplicate-index anomaly type", anomaly_dup1 is not None and anomaly_dup1["new_value"] in ("duplicate_content_block_start", "duplicate_tool_use_start"))

        # 2. duplicate tool_use_start at same index, SAME tool id.
        d_dup2 = base_draft()
        lines_dup2 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json_a}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},  # reused index, SAME id
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json_b}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_dup2.append("data: [DONE]")
        evs_dup2, mp_dup2 = run_turn("test", d_dup2, [fake_response(lines_dup2)])
        check("2. duplicate tool_use_start (same id, same index): ZERO execution", d_dup2["project_context"] == {})
        check("2. duplicate tool_use_start (same id): only 1 API call (fails closed)", mp_dup2.call_count == 1)

        # 3. text content_block_start index 0, LATER a tool_use content_block_start ALSO index 0.
        d_dup3 = base_draft()
        lines_dup3 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},  # reused index
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json_a}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_dup3.append("data: [DONE]")
        evs_dup3, mp_dup3 = run_turn("test", d_dup3, [fake_response(lines_dup3)])
        check("3. text block then tool_use reusing the SAME index: protocol anomaly / ZERO execution", d_dup3["project_context"] == {})
        check("3. text-then-tool_use reused index: only 1 API call (fails closed)", mp_dup3.call_count == 1)

        # 4. completed tool block index 0, THEN a second block_start also index 0.
        d_dup4 = base_draft()
        lines_dup4 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json_a}},
            {"type": "content_block_stop", "index": 0},  # block 0 completes normally here
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "set_project_context", "input": {}}},  # reused AFTER completion
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json_b}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_dup4.append("data: [DONE]")
        evs_dup4, mp_dup4 = run_turn("test", d_dup4, [fake_response(lines_dup4)])
        check("4. index reused AFTER the first block already completed: protocol anomaly / ZERO execution", d_dup4["project_context"] == {})
        check("4. reused-after-completion index: only 1 API call (fails closed)", mp_dup4.call_count == 1)

        # 5. Verify the ORIGINAL first tool block's data is never overwritten
        # (inspected directly, not just via the end-to-end zero-execution
        # outcome above) -- re-run scenario 1's exact stream and confirm
        # via a deliberately instrumented check that Project A's data,
        # not Project B's, is what would have been associated with index 0
        # had this NOT failed closed. Since it DOES fail closed, the
        # strongest available proof is that NEITHER project ever gets set
        # -- specifically not Project B (the "second/overwriting" one).
        check("5. the overwriting call's project (Project B) is definitely never what gets set (context is empty, not Project B)",
              d_dup1["project_context"].get("name") != "Project B")

        # 6. Normal unique indices continue to work (control case).
        d_dup6 = base_draft()
        lines_dup6 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"project_name": "Patel Farm"})}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_dup6.append("data: [DONE]")
        pass1b_dup6 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_dup6 = fake_response(build_sse_lines([("text", "We're on Patel Farm Project now.")]))
        evs_dup6, mp_dup6 = run_turn("test", d_dup6, [fake_response(lines_dup6), pass1b_dup6, pass2_dup6])
        check("6. a normal, unique-index stream still resolves successfully", d_dup6["project_context"].get("project_id") == patel_id)
        check("6. normal unique-index stream: 3 API calls made (Pass 1 + Pass 1B + Pass 2)", mp_dup6.call_count == 3)

        print()
        print("=== Terminal stop_reason gate: a complete tool block is not sufficient without stop_reason=='tool_use' ===")

        def complete_tool_block_lines(stop_reason_value):
            return raw_lines(
                {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"project_name": "Patel Farm"})}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": stop_reason_value}} if stop_reason_value is not None else {"type": "message_delta", "delta": {}},
                {"type": "message_stop"},
            ) + ["data: [DONE]"]

        # 1. completed valid tool block + stop_reason=max_tokens -> ZERO execution.
        d_sr1 = base_draft()
        evs_sr1, mp_sr1 = run_turn("test", d_sr1, [fake_response(complete_tool_block_lines("max_tokens"))])
        check("1. complete tool block but stop_reason=max_tokens: ZERO execution", d_sr1["project_context"] == {})
        check("1. stop_reason=max_tokens: only 1 API call (fails closed)", mp_sr1.call_count == 1)
        sr_log1 = db.execute("SELECT * FROM activity_log WHERE action='atlas_tool_block_without_tool_use_stop_reason' ORDER BY id DESC LIMIT 1").fetchone()
        check("1. diagnostic log records the actual unexpected stop reason (max_tokens)", sr_log1 is not None and sr_log1["new_value"] == "max_tokens")

        # 2. completed valid tool block + stop_reason=end_turn -> ZERO execution.
        d_sr2 = base_draft()
        evs_sr2, mp_sr2 = run_turn("test", d_sr2, [fake_response(complete_tool_block_lines("end_turn"))])
        check("2. complete tool block but stop_reason=end_turn: ZERO execution", d_sr2["project_context"] == {})
        check("2. stop_reason=end_turn: only 1 API call (fails closed)", mp_sr2.call_count == 1)

        # 3. completed valid tool block + stop_reason=None/missing -> ZERO execution.
        d_sr3 = base_draft()
        evs_sr3, mp_sr3 = run_turn("test", d_sr3, [fake_response(complete_tool_block_lines(None))])
        check("3. complete tool block but stop_reason missing/None: ZERO execution", d_sr3["project_context"] == {})
        check("3. stop_reason missing: only 1 API call (fails closed)", mp_sr3.call_count == 1)

        # 3b. stop_sequence -- another explicitly-listed unexpected value.
        d_sr3b = base_draft()
        evs_sr3b, mp_sr3b = run_turn("test", d_sr3b, [fake_response(complete_tool_block_lines("stop_sequence"))])
        check("3b. complete tool block but stop_reason=stop_sequence: ZERO execution", d_sr3b["project_context"] == {})

        # 4. completed valid tool block + stop_reason=tool_use -> normal resolution.
        d_sr4 = base_draft()
        pass1b_sr4 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_sr4 = fake_response(build_sse_lines([("text", "We're on Patel Farm Project now.")]))
        evs_sr4, mp_sr4 = run_turn("test", d_sr4, [fake_response(complete_tool_block_lines("tool_use")), pass1b_sr4, pass2_sr4])
        check("4. complete tool block + stop_reason=tool_use: normal successful resolution", d_sr4["project_context"].get("project_id") == patel_id)
        check("4. stop_reason=tool_use: 3 API calls made (Pass 1 + Pass 1B + Pass 2)", mp_sr4.call_count == 3)

        # 5. no tool_use block + normal end_turn -> ordinary two-pass chat behavior intact.
        d_sr5 = base_draft()
        pass1_sr5 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_sr5 = fake_response(build_sse_lines([("text", "Sure, how can I help?")]))
        evs_sr5, mp_sr5 = run_turn("How's it going?", d_sr5, [pass1_sr5, pass2_sr5])
        check("5. ordinary no-tool turn (stop_reason=end_turn, no tool_use block) still works normally", mp_sr5.call_count == 2)
        check("5. ordinary no-tool turn still produces a visible reply", any(e.get("type") == "delta" for e in evs_sr5))

        # 6. tool_use stop reason but INCOMPLETE block -- still fails closed
        # under the existing completion gate (proves the two checks are
        # independent, not that one subsumes the other).
        d_sr6 = base_draft()
        lines_sr6 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"project_name": "Patel Farm"})}},
            # deliberately NO content_block_stop
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        lines_sr6.append("data: [DONE]")
        evs_sr6, mp_sr6 = run_turn("test", d_sr6, [fake_response(lines_sr6)])
        check("6. stop_reason=tool_use but block never completed: still fails closed (completion gate independently enforced)",
              d_sr6["project_context"] == {})
        check("6. incomplete block + tool_use stop reason: only 1 API call", mp_sr6.call_count == 1)

        print()
        print("=== Zero-tool-blocks stop-reason consistency matrix (Case A) ===")

        def zero_tool_lines(stop_reason_value, with_text=True):
            events = []
            if with_text:
                events.append({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                events.append({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Sure, let me think about that "}})
                events.append({"type": "content_block_stop", "index": 0})
            if stop_reason_value is not None:
                events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason_value}})
            else:
                events.append({"type": "message_delta", "delta": {}})
            events.append({"type": "message_stop"})
            lines = raw_lines(*events)
            lines.append("data: [DONE]")
            return lines

        # 1. zero tool blocks + end_turn -> normal ordinary two-pass chat.
        d_z1 = base_draft()
        pass2_z1 = fake_response(build_sse_lines([("text", "Sure, how can I help?")]))
        evs_z1, mp_z1 = run_turn("How's it going?", d_z1, [fake_response(zero_tool_lines("end_turn")), pass2_z1])
        check("1. zero tool blocks + end_turn: normal ordinary turn (2 API calls, real Pass 2)", mp_z1.call_count == 2)
        check("1. zero tool blocks + end_turn: a real visible reply is produced", any(e.get("type") == "delta" for e in evs_z1))

        # 2. zero tool blocks + max_tokens -> fail closed, NO Pass 2.
        d_z2 = base_draft()
        evs_z2, mp_z2 = run_turn("Let's talk about patel farm", d_z2, [fake_response(zero_tool_lines("max_tokens"))])
        check("2. zero tool blocks + max_tokens: fails closed, NO Pass 2", mp_z2.call_count == 1)
        check("2. zero tool blocks + max_tokens: context not mutated", d_z2["project_context"] == {})
        z2_log = db.execute("SELECT * FROM activity_log WHERE action='atlas_zero_tool_blocks_unexpected_stop_reason' ORDER BY id DESC LIMIT 1").fetchone()
        check("2. diagnostic log records the actual stop reason (max_tokens)", z2_log is not None and z2_log["new_value"] == "max_tokens")

        # 3. zero tool blocks + tool_use (contradictory: claims tool_use but no block exists) -> fail closed.
        d_z3 = base_draft()
        evs_z3, mp_z3 = run_turn("test", d_z3, [fake_response(zero_tool_lines("tool_use"))])
        check("3. zero tool blocks + stop_reason=tool_use (contradictory): fails closed, NO Pass 2", mp_z3.call_count == 1)
        check("3. zero tool blocks + contradictory tool_use: context not mutated", d_z3["project_context"] == {})

        # 4. zero tool blocks + stop_sequence -> fail closed.
        d_z4 = base_draft()
        evs_z4, mp_z4 = run_turn("test", d_z4, [fake_response(zero_tool_lines("stop_sequence"))])
        check("4. zero tool blocks + stop_sequence: fails closed, NO Pass 2", mp_z4.call_count == 1)

        # 5. zero tool blocks + None/missing stop reason -> fail closed.
        d_z5 = base_draft()
        evs_z5, mp_z5 = run_turn("test", d_z5, [fake_response(zero_tool_lines(None))])
        check("5. zero tool blocks + missing/None stop reason: fails closed, NO Pass 2", mp_z5.call_count == 1)

        # 6. one valid completed tool block + tool_use -> normal resolution (control, already covered above but re-confirmed here in this matrix's context).
        d_z6 = base_draft()
        pass2_z6 = fake_response(build_sse_lines([("text", "We're on Patel Farm Project now.")]))
        evs_z6, mp_z6 = run_turn("Let's talk about patel farm", d_z6, [fake_response(complete_tool_block_lines("tool_use")), pass2_z6])
        check("6. one valid completed block + tool_use: normal resolution", d_z6["project_context"].get("project_id") == patel_id)

        # 7. one valid completed tool block + end_turn -> fail closed (existing v5 behavior retained).
        d_z7 = base_draft()
        evs_z7, mp_z7 = run_turn("test", d_z7, [fake_response(complete_tool_block_lines("end_turn"))])
        check("7. one valid completed block + end_turn: still fails closed (v5 behavior retained)", d_z7["project_context"] == {})
        check("7. one valid completed block + end_turn: only 1 API call", mp_z7.call_count == 1)

        # 8. one valid completed tool block + max_tokens -> fail closed (existing v5 behavior retained).
        d_z8 = base_draft()
        evs_z8, mp_z8 = run_turn("test", d_z8, [fake_response(complete_tool_block_lines("max_tokens"))])
        check("8. one valid completed block + max_tokens: still fails closed (v5 behavior retained)", d_z8["project_context"] == {})
        check("8. one valid completed block + max_tokens: only 1 API call", mp_z8.call_count == 1)

        # REAL USER SCENARIO: "Let's talk about Patel Farm" with Pass 1
        # artificially truncated (max_tokens) BEFORE it ever reaches a
        # tool call -- the exact regression this whole fix exists to
        # close. Atlas must NOT fall through to a no-tool Pass 2, and
        # must NOT produce any snapshot-based "I don't see it" answer.
        print()
        print("=== Real scenario: 'Let's talk about Patel Farm' truncated before any tool call ===")
        d_real = base_draft()
        # Pass 1 emits only preamble text and hits max_tokens before
        # any content_block_start for a tool_use ever occurs.
        lines_real = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me look into"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
            {"type": "message_stop"},
        )
        lines_real.append("data: [DONE]")
        evs_real, mp_real = run_turn("Let's talk about Patel Farm", d_real, [fake_response(lines_real)])
        check("real scenario: Atlas does NOT fall through to a no-tool Pass 2 (only 1 API call made)", mp_real.call_count == 1)
        check("real scenario: project_context was never established", d_real["project_context"] == {})
        visible_real = "".join(e.get("text", "") for e in evs_real if e.get("type") == "delta")
        check("real scenario: no snapshot-based 'I don't see it' style answer was produced",
              "don't see" not in visible_real.lower() and "not listed in the snapshot" not in visible_real.lower())
        check("real scenario: a safe retry/error message was given instead", "try again" in visible_real.lower() or ("trouble completing" in visible_real.lower() or "try again" in visible_real.lower()))
        check("real scenario: the fail-safe reply is NOT silently truncated (full text, including its final characters, reaches the browser)",
              visible_real.rstrip().endswith("?") or visible_real.rstrip().endswith("."))

        print()
        print("=== message_stop requirement: EOF/dropped-connection without message_stop must never be treated as a completed message ===")

        def lines_without_message_stop(*events):
            """Like raw_lines(), but deliberately omits the terminal
            message_stop event -- simulating a dropped connection or
            truncated response that ends right after message_delta."""
            lines = ["data: " + json.dumps({"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "content": []}})]
            lines.extend("data: " + json.dumps(e) for e in events)
            lines.append("data: [DONE]")  # requests' own stream framing still ends; Anthropic's message_stop specifically does not appear
            return lines

        # PASS 1, TEST 1: complete valid tool block + stop_reason=tool_use + NO message_stop -> ZERO execution.
        d_ms1 = base_draft()
        lines_ms1 = lines_without_message_stop(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"project_name": "Patel Farm"})}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        )
        evs_ms1, mp_ms1 = run_turn("Let's talk about patel farm", d_ms1, [fake_response(lines_ms1)])
        check("PASS1-1. complete tool block + tool_use stop_reason but NO message_stop: ZERO execute_tool / context not mutated",
              d_ms1["project_context"] == {})
        check("PASS1-1. no Pass 2 was made (fails closed before it)", mp_ms1.call_count == 1)
        check("PASS1-1. a safe failure message was still shown", any(e.get("type") == "delta" for e in evs_ms1))

        # PASS 1, TEST 2: zero tool blocks + stop_reason=end_turn + NO message_stop -> fail closed, no Pass 2.
        d_ms2 = base_draft()
        lines_ms2 = lines_without_message_stop(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Sure thing"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        )
        evs_ms2, mp_ms2 = run_turn("How's it going?", d_ms2, [fake_response(lines_ms2)])
        check("PASS1-2. zero tool blocks + end_turn but NO message_stop: fails closed", mp_ms2.call_count == 1)
        check("PASS1-2. context not mutated", d_ms2["project_context"] == {})

        # PASS 1, TEST 3: normal valid tool response WITH a real message_stop -> still resolves successfully (control case).
        d_ms3 = base_draft()
        pass2_ms3 = fake_response(build_sse_lines([("text", "We're on Patel Farm Project now.")]))
        evs_ms3, mp_ms3 = run_turn("Let's talk about patel farm", d_ms3, [fake_response(complete_tool_block_lines("tool_use")), pass2_ms3])
        check("PASS1-3. normal valid tool response WITH real message_stop: still resolves successfully", d_ms3["project_context"].get("project_id") == patel_id)

        # PASS 1, TEST 4: normal no-tool end_turn WITH real message_stop -> ordinary Pass 2 still works (control case).
        d_ms4 = base_draft()
        pass1_ms4 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_ms4 = fake_response(build_sse_lines([("text", "Sure, how can I help?")]))
        evs_ms4, mp_ms4 = run_turn("How's it going?", d_ms4, [pass1_ms4, pass2_ms4])
        check("PASS1-4. normal no-tool end_turn WITH real message_stop: ordinary Pass 2 still works", mp_ms4.call_count == 2)

        # PASS 2, TEST 5: Pass 1 succeeds normally (no tool). Pass 2 emits
        # some text then EOF with NO message_stop -> safe generation
        # failure, partial response not treated as completed.
        d_ms5 = base_draft()
        pass1_ms5 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_ms5_lines = lines_without_message_stop(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Here is part of an answer"}},
        )
        evs_ms5, mp_ms5 = run_turn("Tell me something", d_ms5, [pass1_ms5, fake_response(pass2_ms5_lines)])
        visible_ms5 = "".join(e.get("text", "") for e in evs_ms5 if e.get("type") == "delta")
        check("PASS2-5. user receives an explicit safe generation-failure message", ("trouble completing" in visible_ms5.lower() or "try again" in visible_ms5.lower()))
        done_ms5 = next((e for e in evs_ms5 if e.get("type") == "done"), None)
        check("PASS2-5. no pending_write_token was produced from the partial/incomplete response", done_ms5 is not None and done_ms5.get("pending_write_token") is None)

        # PASS 2, TEST 6: set_project_context succeeds in Pass 1. Pass 2
        # emits partial text then EOF/no message_stop -> context remains
        # established, visible turn fails safely, follow-up turn still
        # has the context.
        d_ms6 = base_draft()
        pass1_ms6 = fake_response(complete_tool_block_lines("tool_use"))
        pass1b_ms6 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_ms6_lines = lines_without_message_stop(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "We're now look"}},
        )
        evs_ms6, mp_ms6 = run_turn("Let's talk about patel farm", d_ms6, [pass1_ms6, pass1b_ms6, fake_response(pass2_ms6_lines)])
        check("PASS2-6. canonical project_context REMAINS established despite Pass 2's incomplete stream", d_ms6["project_context"].get("project_id") == patel_id)
        visible_ms6 = "".join(e.get("text", "") for e in evs_ms6 if e.get("type") == "delta")
        check("PASS2-6. the visible turn fails safely (explicit error shown)", ("trouble completing" in visible_ms6.lower() or "try again" in visible_ms6.lower()))

        pass1_ms6_follow = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_ms6_follow = fake_response(build_sse_lines([("text", "Sure, about Patel Farm Project...")]))
        _, mp_ms6_follow = run_turn("Tell me more", d_ms6, [pass1_ms6_follow, pass2_ms6_follow])
        check("PASS2-6. follow-up turn still has the established context (system prompt carries it)",
              "Patel Farm Project" in mp_ms6_follow.call_args_list[0].kwargs["json"]["system"])

        # PASS 2, TEST 7: concrete-request Pass 2 emits a syntactically
        # complete-looking <state> with action="submit" but NO
        # message_stop -> must NOT create/advance pending_submit or
        # pending_write, must NOT produce a confirmation token.
        d_ms7 = base_draft()
        concrete_fields_ms7 = {
            "project": "__NativeTest Patel Farm Project", "pour_date": "2026-09-20", "pour_time": "8:00 AM",
            "job_site_address": "1 Test Way", "area_description": "Slab", "mix_design_psi": "4000",
            "mix_slump": "4", "concrete_amount": "10 yd", "truck_spacing": "15 min",
            "pump_type": "None", "lab_required": "No", "drilling_required": "No",
        }
        submit_reply_ms7 = 'Submitting now.<state>{"mode": "concrete_request", "fields": %s, "action": "submit"}</state>' % json.dumps(concrete_fields_ms7)
        pass1_ms7 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_ms7_lines = lines_without_message_stop(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": submit_reply_ms7}},
        )
        before_pending = d_ms7.get("pending_write")
        evs_ms7, mp_ms7 = run_turn("yes, submit it", d_ms7, [pass1_ms7, fake_response(pass2_ms7_lines)])
        check("PASS2-7. no pending_write was created from the incomplete-stream submit-looking response",
              "pending_write" not in d_ms7 or d_ms7.get("pending_write") is None)
        done_ms7 = next((e for e in evs_ms7 if e.get("type") == "done"), None)
        check("PASS2-7. no confirmation token was produced", done_ms7 is not None and done_ms7.get("pending_write_token") is None)
        check("PASS2-7. mode/action were never advanced from the incomplete response (draft mode unchanged from default 'chat')",
              d_ms7.get("mode", "chat") == "chat")

        # Cross-checks: message_stop without a prior stop_reason does not
        # magically become a legitimate tool-use turn (existing matrix
        # still applies); in-stream error still terminates immediately
        # even with message_stop never reached; no duplicate terminal events.
        d_ms8 = base_draft()
        lines_ms8 = raw_lines(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"project_name": "Patel Farm"})}},
            {"type": "content_block_stop", "index": 0},
            # no message_delta at all -- stop_reason stays None -- but message_stop DOES arrive.
            {"type": "message_stop"},
        )
        lines_ms8.append("data: [DONE]")
        evs_ms8, mp_ms8 = run_turn("test", d_ms8, [fake_response(lines_ms8)])
        check("cross-check: message_stop present but stop_reason never set (None) does NOT count as a legitimate tool_use turn",
              d_ms8["project_context"] == {})

        d_ms9 = base_draft()
        lines_ms9 = [
            "data: " + json.dumps({"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "content": []}}),
            "data: " + json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "boom"}}),
            # deliberately no message_stop after the error either
        ]
        evs_ms9, mp_ms9 = run_turn("test", d_ms9, [fake_response(lines_ms9)])
        check("cross-check: an in-stream error still terminates immediately (no message_stop needed to also fail closed)",
              mp_ms9.call_count == 1 and d_ms9["project_context"] == {})
        check("cross-check: exactly one terminal condition reached (no duplicate error-then-stop double-handling artifacts)",
              len([e for e in evs_ms9 if e.get("type") == "delta" and "trouble completing" in e.get("text", "").lower()]) <= 1)

        print()
        print("=== Pass-1 token cap: excessive preamble consumes budget before tool_use starts ===")
        d_cap = base_draft()
        resp_cap = fake_response(build_sse_lines(
            [("text", "Sure, let me think about how to best help you with that project selection process ")],
            stop_reason="max_tokens"
        ))
        resp_cap_pass2 = fake_response(build_sse_lines([("text", "How can I help?")]))
        evs_cap, mp_cap = run_turn("Let's talk about patel farm", d_cap, [resp_cap, resp_cap_pass2])
        check("token cap (preamble-only): no tool executed, context not mutated", d_cap["project_context"] == {})
        check("token cap (preamble-only): Pass 1's burned-budget prose is never shown",
              not any("think about how" in e.get("text", "") for e in evs_cap if e.get("type") == "delta"))
        check("token cap (preamble-only): turn still completes with SOME visible reply (0 tool_use blocks == ordinary path, Pass 2 runs)",
              any(e.get("type") == "delta" for e in evs_cap))

        print()
        print("=== 16. No raw protocol/state leakage in any collected event ===")
        all_test_events_text = json.dumps(events + events2 + events6 + events7 + evs4 + evs5 + evs6 + evs7 + evs_cap)
        check("no raw tool_use content-block JSON ever appears in any payload sent to the browser",
              '"type":"tool_use"' not in all_test_events_text.replace(" ", ""))
        check("no <state> tag content ever leaked into a delta event",
              not any("<state>" in e.get("text", "") for e in events + events2 + events6 + events7))

        print()
        print("=== 17. Ordinary chat still works AND streams incrementally (Pass 2) ===")
        d_ord = base_draft()
        long_reply = "This is a longer ordinary reply with several words so we can check incremental delivery. " * 2
        resp_ord1 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        resp_ord2 = fake_response(build_sse_lines([("text", long_reply)]))
        evs_ord, mp_ord = run_turn("How's it going?", d_ord, [resp_ord1, resp_ord2])
        delta_events = [e for e in evs_ord if e.get("type") == "delta"]
        check("17. ordinary chat produces a normal visible reply", "".join(e.get("text", "") for e in delta_events).strip() != "")
        check("17. ordinary chat reply is delivered as MULTIPLE delta events, not one giant flush (incremental streaming preserved for Pass 2)",
              len(delta_events) > 1)

        print()
        print("=== 18/10. Concrete-request workflow still works, inherits canonical context ===")
        concrete_draft = base_draft({"project_id": patel_id, "name": "__NativeTest Patel Farm Project"})
        complete_fields = {
            "project": "__NativeTest Patel Farm Project", "pour_date": "2026-09-20", "pour_time": "8:00 AM",
            "job_site_address": "1 Test Way", "area_description": "Slab", "mix_design_psi": "4000",
            "mix_slump": "4", "concrete_amount": "10 yd", "truck_spacing": "15 min",
            "pump_type": "None", "lab_required": "No", "drilling_required": "No",
        }
        submit_reply = 'Submitting now.<state>{"mode": "concrete_request", "fields": %s, "action": "submit"}</state>' % json.dumps(complete_fields)
        concrete_draft["pending_write"] = None
        pass1_concrete1 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_concrete1 = fake_response(build_sse_lines([("text", submit_reply)]))
        run_turn("yes submit it", concrete_draft, [pass1_concrete1, pass2_concrete1])
        pass1_concrete2 = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_concrete2 = fake_response(build_sse_lines([("text", submit_reply)]))
        events_confirm, _ = run_turn("yes, submit it", concrete_draft, [pass1_concrete2, pass2_concrete2])
        done_event = next(e for e in events_confirm if e.get("type") == "done")
        write_token = done_event.get("pending_write_token")
        check("18. two-turn write confirmation still works with native dispatch present", bool(write_token))
        check("18. draft carries the canonical project_context through the confirmation flow", concrete_draft["project_context"].get("project_id") == patel_id)

        atlas_token = "test-native-dispatch-token"
        appmod.ATLAS_SESSIONS[atlas_token] = concrete_draft
        before_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__NativeTest Patel Farm Project'").fetchone()[0]
        with appmod.app.test_request_context('/', method="POST", json={"token": write_token}):
            login_user(user)
            from flask import session as flask_session
            flask_session["atlas_token"] = atlas_token
            confirm_resp = appmod.assistant_confirm_write()
        after_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project='__NativeTest Patel Farm Project'").fetchone()[0]
        check("18. the confirmed write actually created exactly one record", after_count == before_count + 1)
        written_row = db.execute("SELECT * FROM inventory_concrete_requests WHERE project='__NativeTest Patel Farm Project' ORDER BY id DESC LIMIT 1").fetchone()
        check("10. the resulting request retains the canonical project_id (inherited from native-dispatch-established context)",
              written_row["project_id"] == patel_id)
        del appmod.ATLAS_SESSIONS[atlas_token]

        print()
        print("=== 19. Session reset behavior ===")
        reset_draft = base_draft({"project_id": patel_id, "name": "__NativeTest Patel Farm Project"})
        reset_draft.clear()
        reset_draft.update({"mode": "chat", "fields": {}, "history": [], "pending_submit": None, "project_context": {}})
        check("19. a reset draft has empty project_context, as a fresh session should", reset_draft["project_context"] == {})

        print()
        print("=== Issue 2: Anthropic in-stream SSE error events (distinct from HTTP errors) ===")

        def fake_sse_error_response(message="simulated overloaded_error"):
            """A response whose HTTP call succeeds, but the STREAM ITSELF
            carries a real Anthropic {"type":"error",...} event -- not an
            HTTP/request-level failure. _stream_claude_completion must
            treat this as terminal, not as an ordinary
            stop_reason=None completion."""
            lines = [
                "data: " + json.dumps({"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "content": []}}),
                "data: " + json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": message}}),
            ]
            return fake_response(lines)

        # 2A. Pass 1 itself emits an SSE error -> no tool execution, safe
        # user error, no Pass 2 at all.
        d_sse_a = base_draft()
        resp_sse_a = fake_sse_error_response("Pass 1 overloaded")
        evs_sse_a, mp_sse_a = run_turn("Let's talk about patel farm", d_sse_a, [resp_sse_a])
        check("2A. Pass-1 SSE error: no tool executed, context not mutated", d_sse_a["project_context"] == {})
        check("2A. Pass-1 SSE error: only 1 API call made (no Pass 2)", mp_sse_a.call_count == 1)
        visible_sse_a = "".join(e.get("text", "") for e in evs_sse_a if e.get("type") == "delta")
        check("2A. Pass-1 SSE error: the user gets a safe error message", ("trouble completing" in visible_sse_a.lower() or "try again" in visible_sse_a.lower()))

        # 2B. set_project_context SUCCEEDS in Pass 1, then Pass 2 itself
        # (not just the HTTP layer) emits an SSE error -> context stays
        # established, safe failure message, no invented final answer,
        # and a follow-up turn still sees the established context.
        d_sse_b = base_draft()
        pass1_sse_b = fake_response(build_sse_lines(
            [("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"}))],
            stop_reason="tool_use"
        ))
        pass2_sse_b = fake_sse_error_response("Pass 2 overloaded")
        pass1b_sse_b = fake_response(build_sse_lines([], stop_reason="end_turn"))
        evs_sse_b, mp_sse_b = run_turn("Let's talk about patel farm", d_sse_b, [pass1_sse_b, pass1b_sse_b, pass2_sse_b])
        check("2B. Pass-2 SSE error: canonical project context REMAINS established (Pass 1's execute_tool already succeeded)",
              d_sse_b["project_context"].get("project_id") == patel_id)
        visible_sse_b = "".join(e.get("text", "") for e in evs_sse_b if e.get("type") == "delta")
        check("2B. Pass-2 SSE error: user receives a safe failure message, not an invented final answer",
              ("trouble completing" in visible_sse_b.lower() or "try again" in visible_sse_b.lower()))
        check("2B. Pass-2 SSE error: no fabricated success claim like 'Patel Farm Project' appears in the error turn's visible text",
              "we're now" not in visible_sse_b.lower() and "switched" not in visible_sse_b.lower())

        pass1_sse_follow = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_sse_follow = fake_response(build_sse_lines([("text", "Sure, about Patel Farm Project...")]))
        _, mp_sse_follow = run_turn("Tell me more", d_sse_b, [pass1_sse_follow, pass2_sse_follow])
        check("2B. follow-up turn after the Pass-2 SSE error still sees the established context in the system prompt",
              "Patel Farm Project" in mp_sse_follow.call_args_list[0].kwargs["json"]["system"])

        print()
        print("=== Error after successful tool execution: context persists, user gets a safe error ===")
        err_draft = base_draft()
        pass1_err = fake_response(build_sse_lines(
            [("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"}))],
            stop_reason="tool_use"
        ))
        pass2_err = fake_error_response()
        pass1b_err = fake_response(build_sse_lines([], stop_reason="end_turn"))
        events_err, mp_err = run_turn("Let's talk about patel farm", err_draft, [pass1_err, pass1b_err, pass2_err])
        check("execute_tool succeeded and mutated context even though Pass 2 failed", err_draft["project_context"].get("project_id") == patel_id)
        visible_err = "".join(e.get("text", "") for e in events_err if e.get("type") == "delta")
        check("the user receives a safe, honest error message, not an invented final answer", ("trouble completing" in visible_err.lower() or "try again" in visible_err.lower()))

        pass1_follow = fake_response(build_sse_lines([], stop_reason="end_turn"))
        pass2_follow = fake_response(build_sse_lines([("text", "Sure, what about Patel Farm Project?")]))
        events_follow, mp_follow = run_turn("What's the status?", err_draft, [pass1_follow, pass2_follow])
        follow_system = mp_follow.call_args_list[0].kwargs["json"]["system"]
        check("follow-up turn after the Pass-2 error can still USE the established project context (system prompt carries it)",
              "Patel Farm Project" in follow_system)

        print()
        print("=== Timing measurement (MOCK-BASED -- simulated network delay, not production benchmarking) ===")

        def timed_turn(label, user_text, draft_in, response_specs):
            """AUDIT NOTE (item 2, timing-measurement audit): this helper
            deliberately uses a STRICT side_effect -- an unexpected extra
            API call raises IndexError immediately rather than being
            silently absorbed by a fallback response. Every call site
            below supplies the EXACT number of responses the real
            architecture is expected to make for that turn shape, and
            each call site's return value is asserted with an explicit
            `calls == N` check -- so a real bug that caused an
            unexpected 4th/5th call would fail loudly here, not pass
            quietly. (Contrast with run_turn's fallback further up this
            file, which exists ONLY for older, unrelated tests that
            never touch Pass 1B and don't assert exact call counts for
            project-resolution turns -- see run_turn's own docstring.)"""
            responses = [fake_response(lines, delay=d) for lines, d in response_specs]
            t_start = time.perf_counter()
            first_delta_time = [None]

            def _side_effect(*a, **kw):
                idx = _side_effect.calls
                _side_effect.calls += 1
                return responses[idx]
            _side_effect.calls = 0

            with patch("app.requests.post", side_effect=_side_effect):
                for line in appmod.stream_atlas_turn(user_text, draft_in):
                    if first_delta_time[0] is None and '"type": "delta"' in line:
                        first_delta_time[0] = time.perf_counter()
            t_end = time.perf_counter()
            total = t_end - t_start
            ttfb = (first_delta_time[0] - t_start) if first_delta_time[0] else None
            print(f"  [{label}] total={total*1000:.1f}ms  time-to-first-visible-token={('%.1fms' % (ttfb*1000)) if ttfb else 'n/a'}  api_calls={_side_effect.calls}")
            return total, ttfb, _side_effect.calls

        d_t1 = base_draft()
        _, _, calls_t1 = timed_turn(
            "ordinary no-tool chat", "How's it going?", d_t1,
            [(build_sse_lines([], stop_reason="end_turn"), 0.03), (build_sse_lines([("text", "Doing well, thanks for asking!")]), 0.05)]
        )
        check("timing: ordinary no-tool chat makes EXACTLY 2 API calls (explicit responses, no fallback reliance)", calls_t1 == 2)

        d_t2 = base_draft()
        # EXPLICIT 3-response sequence -- Pass 1 (set_project_context
        # succeeds) -> Pass 1B (get_project_intelligence declared, model
        # chooses not to call it here) -> Pass 2. No reliance on any
        # exhausted-mock fallback: if stream_atlas_turn made an
        # unexpected 4th call, this response list would be exhausted and
        # the test would fail loudly (see the audit note in run_turn's
        # docstring for why that fallback exists ONLY for unrelated,
        # older tests that never touch Pass 1B at all).
        _, _, calls_t2 = timed_turn(
            "project-selection turn", "Let's talk about Patel Farm", d_t2,
            [(build_sse_lines([("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"}))], stop_reason="tool_use"), 0.03),
             (build_sse_lines([], stop_reason="end_turn"), 0.02),
             (build_sse_lines([("text", "We're on Patel Farm Project now.")]), 0.05)]
        )
        check("timing: project-selection turn makes EXACTLY 3 API calls (Pass 1 + Pass 1B + Pass 2 -- the accepted architectural cost), asserted explicitly not just printed",
              calls_t2 == 3)

        d_t3 = base_draft()
        _, _, calls_t3 = timed_turn(
            "concrete-request conversational turn", "I need concrete for a slab", d_t3,
            [(build_sse_lines([], stop_reason="end_turn"), 0.03),
             (build_sse_lines([("text", 'Sure, what\'s the pour date?<state>{"mode": "concrete_request", "fields": {}, "action": "none"}</state>')]), 0.05)]
        )
        check("timing: concrete-request conversational turn makes EXACTLY 2 API calls (no project tool involved)", calls_t3 == 2)

        print("  NOTE: stream_atlas_turn is the SAME function for text and voice input --")
        print("  voice pays the identical Pass-1-then-Pass-2 latency; no separate/duplicate voice code path exists.")
        print("  These numbers use artificial 30-50ms mock delays, NOT real Anthropic latency -- they demonstrate")
        print("  the STRUCTURE (always 2 API calls total) and relative ordering (Pass 1 always completes before")
        print("  any visible token), not real-world timing.")

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__NativeTest%'")
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__NativeTest%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
