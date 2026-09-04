"""
Atlas TEST-only turn diagnostics (ATLAS_TURN_DIAGNOSTICS) regression.

Usage:
    APP_ENV=development python3 scripts/atlas_turn_diagnostics_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import io
import json
import re
import sqlite3
import contextlib
import hashlib
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


def build_sse_lines(blocks, stop_reason="end_turn"):
    lines = ["data: " + json.dumps({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": []}})]
    for idx, block in enumerate(blocks):
        kind = block[0]
        if kind == "text":
            text = block[1]
            lines.append("data: " + json.dumps({"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}}))
            for i in range(0, len(text), 12):
                lines.append("data: " + json.dumps({"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text[i:i + 12]}}))
            lines.append("data: " + json.dumps({"type": "content_block_stop", "index": idx}))
        elif kind == "tool_use":
            _, name, tool_id, input_json_str = block
            lines.append("data: " + json.dumps({"type": "content_block_start", "index": idx, "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}}))
            for i in range(0, len(input_json_str), 8):
                lines.append("data: " + json.dumps({"type": "content_block_delta", "index": idx, "delta": {"type": "input_json_delta", "partial_json": input_json_str[i:i + 8]}}))
            lines.append("data: " + json.dumps({"type": "content_block_stop", "index": idx}))
    lines.append("data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": stop_reason}}))
    lines.append("data: " + json.dumps({"type": "message_stop"}))
    lines.append("data: [DONE]")
    return lines


def fake_response(lines):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.iter_lines = MagicMock(return_value=iter(lines))
    return resp


def fake_error_response(exc_message="simulated failure"):
    import requests as real_requests
    err_resp = MagicMock()
    err_resp.text = '{"error": {"message": "simulated internal detail should never leak"}}'
    exc = real_requests.exceptions.RequestException(exc_message)
    exc.response = err_resp
    outer = MagicMock()
    outer.raise_for_status = MagicMock(side_effect=exc)
    return outer


def get_csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    token = get_csrf(client, "/login")
    resp = client.post("/login", data={"email": email, "password": password, "csrf_token": token}, follow_redirects=True)
    if resp.status_code != 200 or resp.request.path != "/" or "Invalid email or password" in resp.get_data(as_text=True):
        raise RuntimeError(f"login() failed for {email}")
    return token


def get_meta_csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    return m.group(1) if m else None


def ask(client, question, interaction_mode="text"):
    csrf_token = get_meta_csrf(client, "/assistant")
    resp = client.post("/assistant/ask", json={"question": question, "interaction_mode": interaction_mode}, headers={"X-CSRFToken": csrf_token})
    resp.get_data()  # fully consume the streamed SSE body while this request's context is still active
    body = resp.get_data(as_text=True)
    return resp, body


def parse_trace_lines(stderr_text):
    return [line for line in stderr_text.splitlines() if line.startswith("[ATLAS TRACE ")]


def trace_ids_used(trace_lines):
    ids = set()
    for line in trace_lines:
        m = re.match(r"\[ATLAS TRACE (\S+)\]", line)
        if m:
            ids.add(m.group(1))
    return ids


def sse_events(body):
    events = []
    for chunk in body.split("\n\n"):
        if chunk.startswith("data:"):
            try:
                events.append(json.loads(chunk[len("data:"):].strip()))
            except json.JSONDecodeError:
                pass
    return events


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

    perms = ["module:project_hunt:view", "atlas:view_business_data", "module:atlas:view",
             "atlas:create_requests", "module:sitepulse:view", "module:equipment_center:view"]
    uid = make_user(db, "__diag_user@test.local", "__diag_user", now, pw_hash, perms)

    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__DiagTest%'")
    db.commit()
    proj_cur = db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
                           ("__DiagTest Patel Farm Project", now, now))
    db.commit()

    def ordinary_responses(reply_text):
        return [fake_response(build_sse_lines([], stop_reason="end_turn")), fake_response(build_sse_lines([("text", reply_text)]))]

    def project_turn_responses(project_name, scope, reply_text):
        return [
            fake_response(build_sse_lines([("tool_use", "set_project_context", "t1", json.dumps({"project_name": project_name}))], stop_reason="tool_use")),
            fake_response(build_sse_lines([("tool_use", "get_project_intelligence", "t2", json.dumps({"scope": scope}))], stop_reason="tool_use")),
            fake_response(build_sse_lines([("text", reply_text)])),
        ]

    print("=== Diagnostics OFF (default): zero trace output, zero diagnostic SSE event ===")
    check("ATLAS_TURN_DIAGNOSTICS defaults to False", appmod.ATLAS_TURN_DIAGNOSTICS is False)
    with appmod.app.test_client() as client:
        login(client, "__diag_user@test.local", pw)
        with patch("app.requests.post", side_effect=ordinary_responses("Hello there.")), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                resp, body = ask(client, "hi", interaction_mode="text")
        check("request succeeds normally with diagnostics off", resp.status_code == 200)
        trace_lines_off = parse_trace_lines(stderr_buf.getvalue())
        check("OFF: zero [ATLAS TRACE ...] lines written to stderr", len(trace_lines_off) == 0)
        events_off = sse_events(body)
        check("OFF: zero 'diagnostic' SSE events reach the browser", not any(e.get("type") == "diagnostic" for e in events_off))

    print()
    print("=== Diagnostics ON: trace id generated, diagnostic SSE event, consistent trace id throughout ===")
    with patch.object(appmod, "ATLAS_TURN_DIAGNOSTICS", True):
        with appmod.app.test_client() as client:
            login(client, "__diag_user@test.local", pw)
            with patch("app.requests.post", side_effect=ordinary_responses("Hello there, diagnostics on.")), \
                 patch("app._elevenlabs_tts_call", return_value=(None, None)):
                stderr_buf = io.StringIO()
                with contextlib.redirect_stderr(stderr_buf):
                    resp, body = ask(client, "hi again", interaction_mode="text")
            check("request succeeds normally with diagnostics on", resp.status_code == 200)
            events_on = sse_events(body)
            diag_events = [e for e in events_on if e.get("type") == "diagnostic"]
            check("ON: exactly one diagnostic SSE event emitted", len(diag_events) == 1)
            check("ON: the diagnostic event contains ONLY type + trace_id (no other keys)",
                  set(diag_events[0].keys()) == {"type", "trace_id"})
            trace_id_from_sse = diag_events[0]["trace_id"]
            check("ON: trace_id is a short random-looking hex string, not empty/predictable", bool(trace_id_from_sse) and len(trace_id_from_sse) >= 4)

            trace_lines_on = parse_trace_lines(stderr_buf.getvalue())
            check("ON: at least one [ATLAS TRACE ...] line was written", len(trace_lines_on) > 0)
            ids_used = trace_ids_used(trace_lines_on)
            check("ON: exactly ONE distinct trace id used across the whole request's server-side trace lines", len(ids_used) == 1)
            check("ON: that server-side trace id matches EXACTLY the one sent to the browser in the diagnostic event",
                  ids_used == {trace_id_from_sse})

    print()
    print("=== Normal completed turn: REQUEST_START -> stage markers -> SSE_DONE_SENT -> REQUEST_END ===")
    with patch.object(appmod, "ATLAS_TURN_DIAGNOSTICS", True):
        with appmod.app.test_client() as client:
            login(client, "__diag_user@test.local", pw)
            with patch("app.requests.post", side_effect=ordinary_responses("A normal completed reply.")), \
                 patch("app._elevenlabs_tts_call", return_value=(None, None)):
                stderr_buf = io.StringIO()
                with contextlib.redirect_stderr(stderr_buf):
                    resp, body = ask(client, "ordinary question", interaction_mode="text")
            trace_lines = parse_trace_lines(stderr_buf.getvalue())
            joined = "\n".join(trace_lines)
            check("REQUEST_START present", "REQUEST_START" in joined)
            check("PASS1_START present", "PASS1_START" in joined)
            check("PASS1_END present with a duration_ms", re.search(r"PASS1_END duration_ms=\d+", joined) is not None)
            check("PASS2_START present", "PASS2_START" in joined)
            check("PASS2_END present with a duration_ms", re.search(r"PASS2_END duration_ms=\d+", joined) is not None)
            check("SSE_DONE_SENT present", "SSE_DONE_SENT" in joined)
            check("REQUEST_END present with a total_ms", re.search(r"REQUEST_END total_ms=\d+", joined) is not None)
            check("REQUEST_START is the first trace line", trace_lines[0].split("]")[1].strip().startswith("REQUEST_START"))
            check("REQUEST_END is the last trace line", "REQUEST_END" in trace_lines[-1])

    print()
    print("=== Hostile/malformed scope values NEVER appear raw in diagnostic trace output ===")
    hostile_scopes = {
        "1. newline/log-injection content": "attention\n[ATLAS TRACE FAKE999] REQUEST_START forged_line",
        "2. employee/message-looking text": "please review the Patel Farm contract details urgently",
        "3. SQL-looking text": "'; DROP TABLE inventory_purchase_requests; --",
        "4. API-key-looking marker": "sk-ant-api03-FAKE_LOOKING_SECRET_MARKER_ABCDEF123456",
        "5. very long arbitrary string": "x" * 5000,
    }
    with patch.object(appmod, "ATLAS_TURN_DIAGNOSTICS", True):
        for label, hostile_value in hostile_scopes.items():
            with appmod.app.test_client() as client:
                login(client, "__diag_user@test.local", pw)
                with patch("app.requests.post", side_effect=project_turn_responses("Patel Farm", hostile_value, "Some reply.")), \
                     patch("app._elevenlabs_tts_call", return_value=(None, None)):
                    stderr_buf = io.StringIO()
                    with contextlib.redirect_stderr(stderr_buf):
                        ask(client, "Let's talk about Patel Farm - what's happening?", interaction_mode="text")
                trace_output = stderr_buf.getvalue()
                check(f"{label}: diagnostic trace shows scope=invalid, not the raw hostile value",
                      "scope=invalid" in trace_output)
                # Only check the FIRST 200 chars of a very-long marker for the "not present" assertion
                # (a truncated/partial fragment of an oversized string isn't itself meaningfully hostile,
                # the point is the FULL raw value never gets written verbatim).
                marker_to_check = hostile_value[:200]
                check(f"{label}: none of the raw hostile marker content appears anywhere in trace output",
                      marker_to_check not in trace_output)
                check(f"{label}: no forged/injected trace line structure appears (log injection did not succeed)",
                      "FAKE999" not in trace_output and "forged_line" not in trace_output)

    print()
    print("=== Valid allowed scope values still log their EXACT closed-enum value (sanitizer doesn't over-block) ===")
    with patch.object(appmod, "ATLAS_TURN_DIAGNOSTICS", True):
        for valid_scope in ["overview", "equipment", "concrete", "purchases", "rentals", "attention"]:
            with appmod.app.test_client() as client:
                login(client, "__diag_user@test.local", pw)
                with patch("app.requests.post", side_effect=project_turn_responses("Patel Farm", valid_scope, "Some reply.")), \
                     patch("app._elevenlabs_tts_call", return_value=(None, None)):
                    stderr_buf = io.StringIO()
                    with contextlib.redirect_stderr(stderr_buf):
                        ask(client, "Let's talk about Patel Farm - what's happening?", interaction_mode="text")
                trace_output = stderr_buf.getvalue()
                check(f"valid scope '{valid_scope}': logs its exact value, not 'invalid'", f"scope={valid_scope}" in trace_output)

    print()
    print("=== Diagnostics OFF remains zero-output even with a hostile scope in play ===")
    with appmod.app.test_client() as client:
        login(client, "__diag_user@test.local", pw)
        with patch("app.requests.post", side_effect=project_turn_responses("Patel Farm", "'; DROP TABLE x; --", "Some reply.")), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                ask(client, "Let's talk about Patel Farm - what's happening?", interaction_mode="text")
        check("diagnostics OFF: zero trace lines even for a hostile-scope-carrying turn", len(parse_trace_lines(stderr_buf.getvalue())) == 0)

    print()
    print("=== Existing diagnostic marker sequence remains unchanged after the sanitizer fix ===")
    with patch.object(appmod, "ATLAS_TURN_DIAGNOSTICS", True):
        with appmod.app.test_client() as client:
            login(client, "__diag_user@test.local", pw)
            with patch("app.requests.post", side_effect=project_turn_responses("Patel Farm", "attention", "One item needs attention.")), \
                 patch("app._elevenlabs_tts_call", return_value=(None, None)):
                stderr_buf = io.StringIO()
                with contextlib.redirect_stderr(stderr_buf):
                    resp, body = ask(client, "Let's talk about Patel Farm - what needs attention?", interaction_mode="text")
            trace_lines = parse_trace_lines(stderr_buf.getvalue())
            joined = "\n".join(trace_lines)
            for marker in ["PASS1_START", "PASS1_END", "CONTEXT_TOOL_START", "CONTEXT_TOOL_END",
                           "PASS1B_START", "PASS1B_END", "INTELLIGENCE_START", "INTELLIGENCE_END",
                           "INTELLIGENCE_PROJECT_CORE_END", "INTELLIGENCE_ATTENTION_END",
                           "PASS2_START", "PASS2_END", "SSE_DONE_SENT", "REQUEST_END"]:
                check(f"marker present: {marker}", marker in joined)
            check("scope=attention: concrete/purchases/equipment/rentals sub-source markers NOT present (scope correctly narrowed the DB work)",
                  "INTELLIGENCE_CONCRETE_END" not in joined and "INTELLIGENCE_PURCHASES_END" not in joined
                  and "INTELLIGENCE_EQUIPMENT_END" not in joined and "INTELLIGENCE_RENTALS_END" not in joined)
            check("INTELLIGENCE_START carries the requested scope as a safe enum value", "scope=attention" in joined)

            def idx_of(marker):
                for i, l in enumerate(trace_lines):
                    if marker in l:
                        return i
                return None
            check("PASS1_START occurs before PASS1_END", idx_of("PASS1_START") < idx_of("PASS1_END"))
            check("CONTEXT_TOOL_START occurs before CONTEXT_TOOL_END", idx_of("CONTEXT_TOOL_START") < idx_of("CONTEXT_TOOL_END"))
            check("PASS1B_START occurs before PASS1B_END", idx_of("PASS1B_START") < idx_of("PASS1B_END"))
            check("INTELLIGENCE_START occurs before INTELLIGENCE_END", idx_of("INTELLIGENCE_START") < idx_of("INTELLIGENCE_END"))
            check("INTELLIGENCE_START occurs before INTELLIGENCE_PROJECT_CORE_END (DB work start precedes its own completion marker)",
                  idx_of("INTELLIGENCE_START") < idx_of("INTELLIGENCE_PROJECT_CORE_END"))
            check("PASS2_START occurs before PASS2_END", idx_of("PASS2_START") < idx_of("PASS2_END"))
            check("REQUEST_START occurs before every other marker", idx_of("REQUEST_START") == 0)

    print()
    print("=== Failure path: END/error outcome emitted, REQUEST_END still emitted ===")
    with patch.object(appmod, "ATLAS_TURN_DIAGNOSTICS", True):
        with appmod.app.test_client() as client:
            login(client, "__diag_user@test.local", pw)
            with patch("app.requests.post", return_value=fake_error_response("simulated connection reset detail")), \
                 patch("app._elevenlabs_tts_call", return_value=(None, None)):
                stderr_buf = io.StringIO()
                with contextlib.redirect_stderr(stderr_buf):
                    resp, body = ask(client, "this will fail", interaction_mode="text")
            trace_lines = parse_trace_lines(stderr_buf.getvalue())
            joined = "\n".join(trace_lines)
            check("PASS1_START present even on a failing turn", "PASS1_START" in joined)
            check("PASS1_END present with outcome=error and a safe exception CLASS name (not raw text)",
                  re.search(r"PASS1_END duration_ms=\d+ outcome=error error_type=\w+", joined) is not None)
            check("REQUEST_END still emitted even though the turn failed (structurally always reachable via the generate() wrapper's finally)",
                  "REQUEST_END" in joined)
            check("the raw simulated failure detail text is NOT present in the trace output",
                  "simulated connection reset detail" not in joined and "simulated internal detail should never leak" not in joined)

    print()
    print("=== Content safety: trace lines never contain prompt/message/model/tool/DB/secret content ===")
    with patch.object(appmod, "ATLAS_TURN_DIAGNOSTICS", True):
        with appmod.app.test_client() as client:
            login(client, "__diag_user@test.local", pw)
            secret_employee_message = "MySecretEmployeeMessageAbout_ProprietaryDetails_12345"
            secret_model_reply = "ThisIsASecretModelResponseThatShouldNeverAppearInLogs_67890"
            with patch("app.requests.post", side_effect=ordinary_responses(secret_model_reply)), \
                 patch("app._elevenlabs_tts_call", return_value=(None, None)):
                stderr_buf = io.StringIO()
                with contextlib.redirect_stderr(stderr_buf):
                    resp, body = ask(client, secret_employee_message, interaction_mode="text")
            trace_output = stderr_buf.getvalue()
            check("employee message text never appears in trace output", secret_employee_message not in trace_output)
            check("model reply text never appears in trace output", secret_model_reply not in trace_output)
            api_key_val = os.environ.get("ANTHROPIC_API_KEY", "")
            check("no API key material appears in trace output", (not api_key_val) or api_key_val not in trace_output)
            check("no 'Authorization' / 'x-api-key' header text appears in trace output", "x-api-key" not in trace_output and "Authorization" not in trace_output)
            check("no raw SQL text appears in trace output", "SELECT" not in trace_output and "INSERT INTO" not in trace_output)

        with appmod.app.test_client() as client2:
            login(client2, "__diag_user@test.local", pw)
            with patch("app.requests.post", side_effect=project_turn_responses("Patel Farm", "overview", "Some overview reply.")), \
                 patch("app._elevenlabs_tts_call", return_value=(None, None)):
                stderr_buf2 = io.StringIO()
                with contextlib.redirect_stderr(stderr_buf2):
                    ask(client2, "Let's talk about Patel Farm - what's happening?", interaction_mode="text")
            trace_output2 = stderr_buf2.getvalue()
            check("the canonical project's real NAME never appears in trace output", "Patel Farm Project" not in trace_output2 and "__DiagTest" not in trace_output2)
            intel_start_lines = [l for l in trace_output2.splitlines() if "INTELLIGENCE_START" in l]
            check("only safe enum/duration fields appear alongside INTELLIGENCE_START (scope=<enum>, nothing else)",
                  len(intel_start_lines) == 1 and re.search(r"INTELLIGENCE_START scope=\w+\s*$", intel_start_lines[0].strip()) is not None)

    # ================================================================
    # PASS1B_REQUEST / PASS1B_GATE / PASS1B_DISPATCH_SKIPPED / ATLAS_BUILD_INFO
    # ================================================================
    print()
    print("=== PASS1B request-construction, gate, and build-identity diagnostics ===")

    def raw_lines_1b(*events):
        lines = ["data: " + json.dumps({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": []}})]
        lines.extend("data: " + json.dumps(e) for e in events)
        lines.append("data: [DONE]")
        return lines

    def find_line(trace_output, marker):
        found = [l for l in trace_output.splitlines() if marker in l]
        return found[0] if found else None

    def run_gated_turn(pass1b_lines_or_response):
        with patch.object(appmod, "ATLAS_TURN_DIAGNOSTICS", True):
            with appmod.app.test_client() as client:
                login(client, "__diag_user@test.local", pw)
                pass1b_resp = pass1b_lines_or_response if not isinstance(pass1b_lines_or_response, list) else fake_response(pass1b_lines_or_response)
                with patch("app.requests.post", side_effect=[
                        fake_response(build_sse_lines([("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"}))], stop_reason="tool_use")),
                        pass1b_resp,
                        fake_response(build_sse_lines([("text", "Some reply.")])),
                    ]), \
                     patch("app._elevenlabs_tts_call", return_value=(None, None)):
                    stderr_buf = io.StringIO()
                    with contextlib.redirect_stderr(stderr_buf):
                        ask(client, "Let's talk about Patel Farm - what needs attention?", interaction_mode="text")
                return stderr_buf.getvalue()

    print("A. PASS1B_REQUEST: exactly one expected declaration")
    trace_a = run_gated_turn(build_sse_lines(
        [("tool_use", "get_project_intelligence", "t2", json.dumps({"scope": "attention"}))], stop_reason="tool_use"
    ))
    req_a = find_line(trace_a, "PASS1B_REQUEST")
    check("A. PASS1B_REQUEST line present", req_a is not None)
    check("A. declared_tool_count=1", "declared_tool_count=1" in req_a)
    check("A. expected_tool_declared=true", "expected_tool_declared=true" in req_a)
    check("A. unexpected_tool_declared=false", "unexpected_tool_declared=false" in req_a)
    check("A. tool_choice_mode=auto (no tool_choice is set anywhere in the codebase)", "tool_choice_mode=auto" in req_a)

    print("B. Unexpected declaration case -> unexpected_tool_declared=true, raw name never leaked")
    with patch.object(appmod, "ATLAS_TURN_DIAGNOSTICS", True):
        with patch("app._atlas_native_tool_declarations", return_value=[
                {"name": "get_project_intelligence", "description": "x", "input_schema": {"type": "object", "properties": {}}},
                {"name": "some_unexpected_tool_marker_XYZ", "description": "y", "input_schema": {"type": "object", "properties": {}}},
            ]):
            with appmod.app.test_client() as client:
                login(client, "__diag_user@test.local", pw)
                with patch("app.requests.post", side_effect=[
                        fake_response(build_sse_lines([("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"}))], stop_reason="tool_use")),
                        fake_response(build_sse_lines([("tool_use", "get_project_intelligence", "t2", json.dumps({"scope": "attention"}))], stop_reason="tool_use")),
                        fake_response(build_sse_lines([("text", "Some reply.")])),
                    ]), \
                     patch("app._elevenlabs_tts_call", return_value=(None, None)):
                    stderr_buf_b = io.StringIO()
                    with contextlib.redirect_stderr(stderr_buf_b):
                        ask(client, "Let's talk about Patel Farm - what needs attention?", interaction_mode="text")
            trace_b = stderr_buf_b.getvalue()
    req_b = find_line(trace_b, "PASS1B_REQUEST")
    check("B. declared_tool_count=2", req_b is not None and "declared_tool_count=2" in req_b)
    check("B. unexpected_tool_declared=true", req_b is not None and "unexpected_tool_declared=true" in req_b)
    check("B. the raw unexpected tool name never appears anywhere in trace output", "some_unexpected_tool_marker_XYZ" not in trace_b)

    print("C. Successful Pass1B gate -> all fields correct, dispatch=true")
    trace_c = run_gated_turn(build_sse_lines(
        [("tool_use", "get_project_intelligence", "t2", json.dumps({"scope": "attention"}))], stop_reason="tool_use"
    ))
    g_c = find_line(trace_c, "PASS1B_GATE")
    check("C. PASS1B_GATE present", g_c is not None)
    check("C. tool_use_count=1", "tool_use_count=1" in g_c)
    check("C. completed_tool_count=1", "completed_tool_count=1" in g_c)
    check("C. stop_reason=tool_use", "stop_reason=tool_use" in g_c)
    check("C. protocol_anomaly=false", "protocol_anomaly=false" in g_c)
    check("C. pass1b_error=false", "pass1b_error=false" in g_c)
    check("C. name_allowed=true", "name_allowed=true" in g_c)
    check("C. json_valid=true", "json_valid=true" in g_c)
    check("C. project_id_supplied=false", "project_id_supplied=false" in g_c)
    check("C. dispatch=true", "dispatch=true" in g_c)
    check("C. no PASS1B_DISPATCH_SKIPPED emitted on a successful dispatch", "PASS1B_DISPATCH_SKIPPED" not in trace_c)
    check("C. real dispatch actually happened too (INTELLIGENCE_START present) -- diagnostic agrees with reality", "INTELLIGENCE_START" in trace_c)

    print("C2. Returned-name diagnostics -- exact match case")
    trace_c2 = run_gated_turn(build_sse_lines(
        [("tool_use", "get_project_intelligence", "t2", json.dumps({"scope": "attention"}))], stop_reason="tool_use"
    ))
    g_c2 = find_line(trace_c2, "PASS1B_GATE")
    expected_hash = hashlib.sha256(b"get_project_intelligence").hexdigest()
    check("C2. returned_name_length matches the real literal's length (24)", "returned_name_length=24" in g_c2)
    check("C2. returned_name_matches_expected_exact=true", "returned_name_matches_expected_exact=true" in g_c2)
    check("C2. returned_name_matches_expected_stripped=true", "returned_name_matches_expected_stripped=true" in g_c2)
    check("C2. returned_name_matches_expected_casefold=true", "returned_name_matches_expected_casefold=true" in g_c2)
    check("C2. returned_name_ascii=true", "returned_name_ascii=true" in g_c2)
    check("C2. returned_name_sha256 equals expected_name_sha256 (exact match case)", f"returned_name_sha256={expected_hash}" in g_c2)
    check("C2. expected_name_sha256 is the fixed, correct hash of the real literal", f"expected_name_sha256={expected_hash}" in g_c2)

    def gate_for_name(raw_name):
        lines = raw_lines_1b(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": raw_name, "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        trace = run_gated_turn(lines)
        return find_line(trace, "PASS1B_GATE"), trace

    print("Returned-name diagnostics -- leading/trailing whitespace case")
    g_ws, trace_ws = gate_for_name("  get_project_intelligence  ")
    check("whitespace case: exact=false", "returned_name_matches_expected_exact=false" in g_ws)
    check("whitespace case: stripped=true", "returned_name_matches_expected_stripped=true" in g_ws)
    check("whitespace case: casefold=true", "returned_name_matches_expected_casefold=true" in g_ws)
    check("whitespace case: name_allowed still false (real gate does no stripping, unaffected by this diagnostic)", "name_allowed=false" in g_ws)
    check("whitespace case: the raw padded name never appears in trace output", "  get_project_intelligence  " not in trace_ws)

    print("Returned-name diagnostics -- case-only mismatch")
    g_case, trace_case = gate_for_name("Get_Project_Intelligence")
    check("case mismatch: exact=false", "returned_name_matches_expected_exact=false" in g_case)
    check("case mismatch: stripped=false", "returned_name_matches_expected_stripped=false" in g_case)
    check("case mismatch: casefold=true", "returned_name_matches_expected_casefold=true" in g_case)
    check("case mismatch: name_allowed still false", "name_allowed=false" in g_case)
    check("case mismatch: raw mixed-case name never appears in trace output", "Get_Project_Intelligence" not in trace_case)

    print("Returned-name diagnostics -- non-ASCII/invisible-character case")
    g_unicode, trace_unicode = gate_for_name("get_project_intelligence\u200b")  # trailing zero-width space
    check("non-ASCII case: exact=false", "returned_name_matches_expected_exact=false" in g_unicode)
    check("non-ASCII case: stripped=false (zero-width space is not whitespace .strip() removes)", "returned_name_matches_expected_stripped=false" in g_unicode)
    check("non-ASCII case: ascii=false", "returned_name_ascii=false" in g_unicode)
    check("non-ASCII case: name_allowed still false", "name_allowed=false" in g_unicode)
    check("non-ASCII case: hash differs from the expected hash (proves the invisible character is captured, without revealing it)",
          f"returned_name_sha256={expected_hash}" not in g_unicode)

    print("Returned-name diagnostics -- completely different ASCII string")
    g_diff, trace_diff = gate_for_name("some_completely_different_tool")
    check("different string: exact=false", "returned_name_matches_expected_exact=false" in g_diff)
    check("different string: stripped=false", "returned_name_matches_expected_stripped=false" in g_diff)
    check("different string: casefold=false", "returned_name_matches_expected_casefold=false" in g_diff)
    check("different string: ascii=true", "returned_name_ascii=true" in g_diff)
    check("different string: length matches that string's real length (30)", "returned_name_length=30" in g_diff)
    check("different string: raw name never appears in trace output", "some_completely_different_tool" not in trace_diff)

    print("Returned-name diagnostics -- missing/non-string name (defensive path)")
    lines_missing = raw_lines_1b(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "input": {}}},  # no "name" key at all
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )
    trace_missing = run_gated_turn(lines_missing)
    g_missing = find_line(trace_missing, "PASS1B_GATE")
    check("missing name: diagnostics do not crash -- PASS1B_GATE line still present", g_missing is not None)
    check("missing name: returned_name_length=0", "returned_name_length=0" in g_missing)
    check("missing name: returned_name_matches_expected_exact=false", "returned_name_matches_expected_exact=false" in g_missing)
    check("missing name: returned_name_matches_expected_stripped=false", "returned_name_matches_expected_stripped=false" in g_missing)
    check("missing name: returned_name_matches_expected_casefold=false", "returned_name_matches_expected_casefold=false" in g_missing)
    check("missing name: returned_name_ascii=false", "returned_name_ascii=false" in g_missing)
    check("missing name: returned_name_sha256=not_a_string (fixed safe sentinel, not a crash, not a fabricated hash)", "returned_name_sha256=not_a_string" in g_missing)
    check("missing name: name_allowed=false, dispatch=false, no crash anywhere downstream", "name_allowed=false" in g_missing and "dispatch=false" in g_missing)
    check("missing name: REQUEST_END still reached (whole request didn't crash)", "REQUEST_END" in trace_missing)

    print("Returned-name diagnostics -- diagnostics OFF emits none of these fields")
    with appmod.app.test_client() as client:
        login(client, "__diag_user@test.local", pw)
        with patch("app.requests.post", side_effect=[
                fake_response(build_sse_lines([("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"}))], stop_reason="tool_use")),
                fake_response(build_sse_lines([("tool_use", "get_project_intelligence", "t2", json.dumps({"scope": "attention"}))], stop_reason="tool_use")),
                fake_response(build_sse_lines([("text", "Some reply.")])),
            ]), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            stderr_buf_off = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf_off):
                ask(client, "Let's talk about Patel Farm - what needs attention?", interaction_mode="text")
        trace_off = stderr_buf_off.getvalue()
        check("diagnostics off: zero returned_name_* fields anywhere", "returned_name_" not in trace_off)
        check("diagnostics off: zero expected_name_sha256 anywhere", "expected_name_sha256" not in trace_off)
        check("diagnostics off: zero trace output overall", len(parse_trace_lines(trace_off)) == 0)

    print("D. Each fail-closed condition independently yields dispatch=false + correct skip reason")

    scenarios = {}
    scenarios["pass1b_error"] = fake_error_response("simulated pass1b failure")
    scenarios["protocol_anomaly"] = raw_lines_1b(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "get_project_intelligence", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_stop", "index": 0},  # duplicate -> protocol anomaly
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )
    scenarios["wrong_tool_count"] = raw_lines_1b(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "get_project_intelligence", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t3", "name": "get_project_intelligence", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )
    scenarios["incomplete_tool"] = [
        "data: " + json.dumps({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": []}}),
        "data: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "get_project_intelligence", "input": {}}}),
        "data: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
        "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
        "data: " + json.dumps({"type": "message_stop"}),
        "data: [DONE]",
    ]
    scenarios["wrong_stop_reason"] = raw_lines_1b(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "get_project_intelligence", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    )
    scenarios["name_not_allowed"] = raw_lines_1b(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "some_other_tool_marker", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )
    scenarios["invalid_json"] = raw_lines_1b(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "get_project_intelligence", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{not valid json"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )
    scenarios["project_id_supplied"] = raw_lines_1b(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "get_project_intelligence", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"scope": "overview", "project_id": 999})}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )

    for expected_reason, payload in scenarios.items():
        trace_d = run_gated_turn(payload)
        g_d = find_line(trace_d, "PASS1B_GATE")
        skip_d = find_line(trace_d, "PASS1B_DISPATCH_SKIPPED")
        check(f"D. [{expected_reason}] PASS1B_GATE shows dispatch=false", g_d is not None and "dispatch=false" in g_d)
        check(f"D. [{expected_reason}] PASS1B_DISPATCH_SKIPPED reason matches exactly", skip_d is not None and f"reason={expected_reason}" in skip_d)
        check(f"D. [{expected_reason}] no real dispatch occurred (no INTELLIGENCE_START)", "INTELLIGENCE_START" not in trace_d)

    print("E. No prompt/user/model/tool/project/SQL/API-key content can appear in any new diagnostic")
    hostile_tool_name = "employee_secret_tool_name_marker_ABC123"
    hostile_scope_marker = "SQL-looking '; DROP TABLE x; --"
    hostile_key_marker = "sk-ant-api03-FAKE_KEY_MARKER_should_never_leak"
    lines_e = raw_lines_1b(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": hostile_tool_name, "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"scope": hostile_scope_marker, "note": hostile_key_marker})}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )
    trace_e = run_gated_turn(lines_e)
    check("E. hostile tool name never appears in trace output", hostile_tool_name not in trace_e)
    check("E. SQL-looking marker never appears in trace output", hostile_scope_marker not in trace_e)
    check("E. API-key-looking marker never appears in trace output", hostile_key_marker not in trace_e)
    g_e = find_line(trace_e, "PASS1B_GATE")
    check("E. gate still correctly fails closed (name_allowed=false) even with hostile input", g_e is not None and "name_allowed=false" in g_e)
    skip_e = find_line(trace_e, "PASS1B_DISPATCH_SKIPPED")
    check("E. correct skip reason (name_not_allowed) despite hostile input", skip_e is not None and "reason=name_not_allowed" in skip_e)

    print("F. Diagnostics OFF -> zero new trace output (PASS1B_REQUEST/GATE/SKIPPED/BUILD_INFO all absent)")
    with appmod.app.test_client() as client:
        login(client, "__diag_user@test.local", pw)
        with patch("app.requests.post", side_effect=[
                fake_response(build_sse_lines([("tool_use", "set_project_context", "t1", json.dumps({"project_name": "Patel Farm"}))], stop_reason="tool_use")),
                fake_response(build_sse_lines([("tool_use", "get_project_intelligence", "t2", json.dumps({"scope": "attention"}))], stop_reason="tool_use")),
                fake_response(build_sse_lines([("text", "Some reply.")])),
            ]), \
             patch("app._elevenlabs_tts_call", return_value=(None, None)):
            stderr_buf_f = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf_f):
                ask(client, "Let's talk about Patel Farm - what needs attention?", interaction_mode="text")
        trace_f = stderr_buf_f.getvalue()
        check("F. zero PASS1B_REQUEST lines when diagnostics are off", "PASS1B_REQUEST" not in trace_f)
        check("F. zero PASS1B_GATE lines when diagnostics are off", "PASS1B_GATE" not in trace_f)
        check("F. zero PASS1B_DISPATCH_SKIPPED lines when diagnostics are off", "PASS1B_DISPATCH_SKIPPED" not in trace_f)
        check("F. zero ATLAS_BUILD_INFO lines when diagnostics are off", "ATLAS_BUILD_INFO" not in trace_f)
        check("F. zero trace output overall when diagnostics are off", len(parse_trace_lines(trace_f)) == 0)

    print("G. Build/deployment identity")
    trace_g = run_gated_turn(build_sse_lines(
        [("tool_use", "get_project_intelligence", "t2", json.dumps({"scope": "attention"}))], stop_reason="tool_use"
    ))
    build_line = find_line(trace_g, "ATLAS_BUILD_INFO")
    check("G. ATLAS_BUILD_INFO line present when diagnostics are on", build_line is not None)
    check("G. carries the fixed build label", "build=TEST-v5.5-pass1b-returned-name-evidence" in build_line)
    check("G. carries an app.py SHA-256 hash (64 hex chars)", re.search(r"app\.py=[0-9a-f]{64}", build_line) is not None)
    check("G. carries an intelligence.py SHA-256 hash", re.search(r"intelligence\.py=[0-9a-f]{64}", build_line) is not None)
    check("G. carries an assistant.html SHA-256 hash", re.search(r"assistant\.html=[0-9a-f]{64}", build_line) is not None)
    check("G. no filesystem path beyond the plain filename appears", "/home/" not in build_line and "/mnt/" not in build_line and os.sep + "app.py" not in build_line.replace("app.py", ""))
    actual_app_hash = hashlib.sha256(open(os.path.join(os.path.dirname(os.path.abspath(appmod.__file__)), "app.py"), "rb").read()).hexdigest()
    check("G. the reported app.py hash matches the ACTUAL file on disk (not a fabricated/stale value)", actual_app_hash in build_line)


    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__DiagTest%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
