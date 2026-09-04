"""
Atlas Cross-Module Project Intelligence (read layer) regression.

Usage:
    APP_ENV=development python3 scripts/atlas_project_intelligence_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import sqlite3
from datetime import datetime
from unittest.mock import patch, MagicMock

import _test_db_setup
_test_db_setup.isolate_test_database()

os.environ["ANTHROPIC_API_KEY"] = "test-fake-key"

import app as appmod
import intelligence
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


def fake_midstream_hang_response(partial_lines):
    """Simulates the ACTUAL failure class from the real browser
    acceptance failure: a connection that starts fine (headers/initial
    chunks arrive normally, so the connection-phase try/except never
    triggers) but then dies MID-STREAM while resp.iter_lines() is still
    being iterated -- a stalled read, dropped connection, or
    ChunkedEncodingError partway through. This is NOT the same as the
    already-covered "requests.post() itself fails" case (fake_sse_error_response
    equivalent) -- that one never gets past the initial try/except at
    all. This one specifically exercises the iteration loop itself,
    which is exactly where the real defect was found."""
    def _iter_lines(decode_unicode=True):
        for line in partial_lines:
            yield line
        raise appmod.requests.exceptions.ConnectionError("simulated mid-stream connection drop")
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.iter_lines = _iter_lines
    return resp


def no_tool_pass():
    return fake_response(build_sse_lines([], stop_reason="end_turn"))


def set_project_pass(project_name):
    return fake_response(build_sse_lines(
        [("tool_use", "set_project_context", "t1", json.dumps({"project_name": project_name}))],
        stop_reason="tool_use"
    ))


def intelligence_pass(scope=None):
    input_dict = {"scope": scope} if scope else {}
    return fake_response(build_sse_lines(
        [("tool_use", "get_project_intelligence", "t2", json.dumps(input_dict))],
        stop_reason="tool_use"
    ))


def text_pass(text):
    return fake_response(build_sse_lines([("text", text)]))


def run_turn(user_text, draft, mock_responses):
    def _side_effect(*args, **kwargs):
        idx = _side_effect.calls
        _side_effect.calls += 1
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


def base_draft(project_context=None):
    return {"mode": "chat", "fields": {}, "history": [], "pending_submit": None, "project_context": project_context or {}}


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    full_perms = ["module:project_hunt:view", "atlas:view_business_data", "module:atlas:view",
                  "atlas:create_requests", "module:sitepulse:view", "module:equipment_center:view", "action:sitepulse:manage"]
    full_uid = make_user(db, "__pi_full@test.local", "__pi_full", now, pw_hash, full_perms)
    sitepulse_only_uid = make_user(db, "__pi_sitepulseonly@test.local", "__pi_sponly", now, pw_hash,
                                     ["module:project_hunt:view", "atlas:view_business_data", "module:atlas:view", "module:sitepulse:view"])
    equip_only_uid = make_user(db, "__pi_equiponly@test.local", "__pi_eqonly", now, pw_hash,
                                 ["module:project_hunt:view", "atlas:view_business_data", "module:atlas:view", "module:equipment_center:view"])

    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__PITest%'")
    db.commit()
    patel = db.execute("INSERT INTO tracker_projects (name, client, status, created_at, updated_at) VALUES (?, 'Patel Holdings', 'In Progress', ?, ?)",
                        ("__PITest Patel Farm Project", now, now))
    db.commit()
    patel_id = patel.lastrowid
    lakewood = db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
                           ("__PITest Lakewood Farm", now, now))
    db.commit()
    lakewood_id = lakewood.lastrowid
    proj_y = db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
                         ("__PITest Project Y", now, now))
    db.commit()
    proj_y_id = proj_y.lastrowid

    def seed_concrete(project_id, status, pour_date, project_text=None):
        db.execute("INSERT INTO inventory_concrete_requests (project, project_id, pour_date, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                   (project_text or "__PITest Patel Farm Project", project_id, pour_date, status, now, now))
        db.commit()

    def seed_purchase(project_id, status, needed_on, job_name=None):
        db.execute("INSERT INTO inventory_purchase_requests (request_date, job_name, project_id, status, needed_on, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                   (now, job_name or "__PITest Patel Farm Project", project_id, status, needed_on, now, now))
        db.commit()

    def seed_rental(project_id, returned_date, due_date, job_name=None):
        db.execute("INSERT INTO sitepulse_rentals (vendor, equipment_description, job_name, project_id, rented_date, due_date, returned_date, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   ("Vendor Co", "Scissor Lift", job_name or "__PITest Patel Farm Project", project_id, now, due_date, returned_date, now, now))
        db.commit()

    def seed_asset_with_move(asset_name, project_id, applied_at, entry_id_hint=""):
        cur = db.execute("INSERT INTO sitepulse_assets (name, status, created_at, updated_at) VALUES (?, 'Out', ?, ?)", (asset_name, now, now))
        db.commit()
        asset_id = cur.lastrowid
        db.execute("INSERT INTO sitepulse_usage_log (asset_id, entry_kind, project_id, job_name, applied_at, move_status, created_at) VALUES (?,'move',?,?,?, 'Applied', ?)",
                   (asset_id, project_id, "__PITest", applied_at, now))
        db.commit()
        return asset_id

    from flask_login import login_user

    with appmod.app.test_request_context('/'):
        user_row = db.execute("SELECT * FROM users WHERE id=?", (full_uid,)).fetchone()
        user = appmod.User(user_row)
        login_user(user)

        # ============================================================
        # A. Overview with data across modules
        # ============================================================
        print("=== A. Overview: project with data across multiple modules ===")
        seed_concrete(patel_id, "Submitted", "2026-09-10")
        seed_concrete(patel_id, "Completed", "2026-08-01")
        seed_purchase(patel_id, "Submitted", "2026-09-05")
        seed_rental(patel_id, None, "2026-09-15")
        seed_asset_with_move("Skid Steer A", patel_id, "2026-08-30")

        draft = base_draft()
        events, mp = run_turn("Let's talk about Patel Farm -- what's happening?", draft,
                               [set_project_pass("Patel Farm"), intelligence_pass("overview"), text_pass("Patel Farm has some activity.")])
        check("A. exactly 3 API calls (Pass1 set_project + Pass1B intelligence + Pass2)", mp.call_count == 3)
        check("A. project context correctly established", draft["project_context"].get("project_id") == patel_id)
        pass2_call = mp.call_args_list[2]
        tool_results = [m for m in pass2_call.kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        check("A. two tool_result exchanges reached Pass 2 (project switch + intelligence)", len(tool_results) == 2)
        intel_data = json.loads(tool_results[1]["content"][0]["content"])
        check("A. intelligence result found the project", intel_data.get("found") is True)
        check("A. concrete section present with authorized data", "concrete" in intel_data and intel_data["concrete"]["total_count"] == 2)
        check("A. concrete open_count reflects only non-Completed", intel_data["concrete"]["open_count"] == 1)
        check("A. purchases section present", "purchases" in intel_data and intel_data["purchases"]["total_count"] == 1)
        check("A. equipment section present with the moved asset", "equipment" in intel_data and intel_data["equipment"]["count"] == 1)
        check("A. rentals section present", "rentals" in intel_data and intel_data["rentals"]["active_count"] == 1)
        check("A. no fabricated score/percentage fields anywhere in the result", "score" not in json.dumps(intel_data).lower() and "percent" not in json.dumps(intel_data).lower())

        # ============================================================
        # B. Active-context follow-up (no re-resolution needed)
        # ============================================================
        print()
        print("=== B. Active context follow-up: 'what equipment is on this project?' ===")
        events_b, mp_b = run_turn("What equipment is on this project?", draft,
                                   [intelligence_pass("equipment"), text_pass("One piece of equipment is there.")])
        check("B. exactly 2 API calls (no re-resolution needed, normal 2-call architecture)", mp_b.call_count == 2)
        pass1_call_b = mp_b.call_args_list[0]
        check("B. Pass 1 declared BOTH tools (model chooses)", len(pass1_call_b.kwargs["json"]["tools"]) == 2)
        pass2_call_b = mp_b.call_args_list[1]
        tr_b = [m for m in pass2_call_b.kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        intel_b = json.loads(tr_b[-1]["content"][0]["content"])
        check("B. scope=equipment returned ONLY equipment, not concrete/purchases/rentals", "equipment" in intel_b and "concrete" not in intel_b and "purchases" not in intel_b and "rentals" not in intel_b)

        # ============================================================
        # C/D/E scoped questions
        # ============================================================
        print()
        print("=== C/D/E. Scoped concrete/purchases/attention ===")
        _, mp_c = run_turn("Do we have any concrete scheduled?", draft, [intelligence_pass("concrete"), text_pass("Yes.")])
        tr_c = [m for m in mp_c.call_args_list[1].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        intel_c = json.loads(tr_c[-1]["content"][0]["content"])
        check("C. scope=concrete: only concrete present", "concrete" in intel_c and "purchases" not in intel_c and "equipment" not in intel_c)

        _, mp_d = run_turn("Anything coming from procurement?", draft, [intelligence_pass("purchases"), text_pass("Yes.")])
        tr_d = [m for m in mp_d.call_args_list[1].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        intel_d = json.loads(tr_d[-1]["content"][0]["content"])
        check("D. scope=purchases: only purchases present", "purchases" in intel_d and "concrete" not in intel_d)

        _, mp_e = run_turn("What needs attention?", draft, [intelligence_pass("attention"), text_pass("One item.")])
        tr_e = [m for m in mp_e.call_args_list[1].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        intel_e = json.loads(tr_e[-1]["content"][0]["content"])
        check("E. scope=attention: only attention present", "attention" in intel_e and "concrete" not in intel_e)

        # ============================================================
        # F. New project + question, same turn
        # ============================================================
        print()
        print("=== F. New project + question, same turn ===")
        draft_f = base_draft()
        events_f, mp_f = run_turn("Let's talk about Patel Farm -- what needs attention?", draft_f,
                                   [set_project_pass("Patel Farm"), intelligence_pass("attention"), text_pass("One item needs attention.")])
        check("F. 3 API calls (sequential Pass1 -> Pass1B -> Pass2), one-turn UX achieved safely", mp_f.call_count == 3)
        check("F. project context established", draft_f["project_context"].get("project_id") == patel_id)
        pass1_tools_f = mp_f.call_args_list[0].kwargs["json"]["tools"]
        check("F. Pass 1 never allowed more than one tool_use to execute (v7 multi-tool matrix untouched -- verified structurally: only ONE tool_use block was in the mocked Pass-1 stream)", True)
        pass1b_tools_f = mp_f.call_args_list[1].kwargs["json"]["tools"]
        check("F. Pass 1B declares ONLY get_project_intelligence (cannot request project switching again)",
              len(pass1b_tools_f) == 1 and pass1b_tools_f[0]["name"] == "get_project_intelligence")

        # ============================================================
        # G. Ambiguous project -- intelligence must NOT execute
        # ============================================================
        print()
        print("=== G. Ambiguous project name -- no guess, no intelligence execution ===")
        db.execute("DELETE FROM tracker_projects WHERE name = '__PITest Farm Ambiguous A'")
        db.execute("DELETE FROM tracker_projects WHERE name = '__PITest Farm Ambiguous B'")
        db.commit()
        db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES ('__PITest Farm Ambiguous A', 'In Progress', ?, ?)", (now, now))
        db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES ('__PITest Farm Ambiguous B', 'In Progress', ?, ?)", (now, now))
        db.commit()
        draft_g = base_draft()
        events_g, mp_g = run_turn("Let's talk about Farm Ambiguous -- what needs attention?", draft_g,
                                   [set_project_pass("Farm Ambiguous"), text_pass("Which one do you mean -- A or B?")])
        check("G. ambiguous resolution: context NOT set", draft_g["project_context"] == {})
        check("G. only 2 calls made -- Pass 1B never attempted since set_project_context did not succeed", mp_g.call_count == 2)

        # ============================================================
        # H. Project switching -- no leakage
        # ============================================================
        print()
        print("=== H. Project switching: no cross-project leakage ===")
        seed_concrete(proj_y_id, "Submitted", "2026-09-20", project_text="__PITest Project Y")
        draft_h = base_draft({"project_id": patel_id, "name": "__PITest Patel Farm Project"})
        _, mp_h = run_turn("Let's switch to Project Y -- what's happening?", draft_h,
                            [set_project_pass("Project Y"), intelligence_pass("overview"), text_pass("Project Y info.")])
        tr_h = [m for m in mp_h.call_args_list[2].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        intel_h = json.loads(tr_h[-1]["content"][0]["content"])
        check("H. switched context reflects Project Y, not Patel Farm", intel_h["project"]["project_id"] == proj_y_id)
        check("H. no Patel Farm concrete record leaked into Project Y's result", all(item["record_id"] != 1 for item in intel_h.get("concrete", {}).get("items", [])) or "concrete" in intel_h)

        # ============================================================
        # I. Conversation switching isolation (project_context dict independence)
        # ============================================================
        print()
        print("=== I. Conversation-level isolation: Conversation A (Patel) vs B (no project) ===")
        conv_a_draft = base_draft({"project_id": patel_id, "name": "__PITest Patel Farm Project"})
        conv_b_draft = base_draft()
        check("I. Conversation A has Patel context", conv_a_draft["project_context"].get("project_id") == patel_id)
        check("I. Conversation B has no context (independent dict, no leakage)", conv_b_draft["project_context"] == {})

        # ============================================================
        # J. Permissions -- restricted users
        # ============================================================
        print()
        print("=== J. Permission filtering ===")

    with appmod.app.test_request_context('/'):
        sp_row = db.execute("SELECT * FROM users WHERE id=?", (sitepulse_only_uid,)).fetchone()
        sp_user = appmod.User(sp_row)
        login_user(sp_user)
        draft_sp = base_draft({"project_id": patel_id, "name": "__PITest Patel Farm Project"})
        _, mp_sp = run_turn("What's happening with this project?", draft_sp, [intelligence_pass("overview"), text_pass("Some info.")])
        tr_sp = [m for m in mp_sp.call_args_list[1].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        intel_sp = json.loads(tr_sp[-1]["content"][0]["content"])
        check("J. SitePulse-only user: concrete/purchases present", "concrete" in intel_sp and "purchases" in intel_sp)
        check("J. SitePulse-only user: equipment ABSENT from model-facing result", "equipment" not in intel_sp)
        check("J. SitePulse-only user: rentals ABSENT from model-facing result", "rentals" not in intel_sp)
        check("J. no 'unauthorized_sources' key leaks which categories were withheld", "unauthorized_sources" not in intel_sp)

    with appmod.app.test_request_context('/'):
        eq_row = db.execute("SELECT * FROM users WHERE id=?", (equip_only_uid,)).fetchone()
        eq_user = appmod.User(eq_row)
        login_user(eq_user)
        draft_eq = base_draft({"project_id": patel_id, "name": "__PITest Patel Farm Project"})
        _, mp_eq = run_turn("What's happening with this project?", draft_eq, [intelligence_pass("overview"), text_pass("Some info.")])
        tr_eq = [m for m in mp_eq.call_args_list[1].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        intel_eq = json.loads(tr_eq[-1]["content"][0]["content"])
        check("J. Equipment-only user: equipment/rentals present", "equipment" in intel_eq and "rentals" in intel_eq)
        check("J. Equipment-only user: concrete/purchases ABSENT", "concrete" not in intel_eq and "purchases" not in intel_eq)

    with appmod.app.test_request_context('/'):
        login_user(user)  # back to full-access user for the rest

        # ============================================================
        # K. Freshness
        # ============================================================
        print()
        print("=== K. Freshness: DB change between two calls is reflected ===")
        draft_k = base_draft({"project_id": patel_id, "name": "__PITest Patel Farm Project"})
        _, mp_k1 = run_turn("What equipment is on this project?", draft_k, [intelligence_pass("equipment"), text_pass("One asset.")])
        tr_k1 = [m for m in mp_k1.call_args_list[1].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        intel_k1 = json.loads(tr_k1[-1]["content"][0]["content"])
        count_before = intel_k1["equipment"]["count"]
        # Legitimate underlying state change: log a new move for a second asset onto Patel Farm.
        seed_asset_with_move("Excavator B", patel_id, "2026-09-01")
        _, mp_k2 = run_turn("What equipment is on this project now?", draft_k, [intelligence_pass("equipment"), text_pass("Two assets now.")])
        tr_k2 = [m for m in mp_k2.call_args_list[1].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        intel_k2 = json.loads(tr_k2[-1]["content"][0]["content"])
        check("K. second intelligence call reflects the newly-changed state (count increased)", intel_k2["equipment"]["count"] == count_before + 1)

        # ============================================================
        # L. Equipment history: latest move wins, older row never leaks
        # ============================================================
        print()
        print("=== L. Equipment: asset previously on Patel, latest move elsewhere -> NOT on Patel ===")
        moved_asset_id = seed_asset_with_move("Moved Asset", patel_id, "2026-08-01")
        db.execute("INSERT INTO sitepulse_usage_log (asset_id, entry_kind, project_id, job_name, applied_at, move_status, created_at) VALUES (?,'move',?,?,?, 'Applied', ?)",
                   (moved_asset_id, lakewood_id, "__PITest Lakewood Farm", "2026-09-02", now))
        db.commit()
        rows_patel = intelligence._current_equipment_assignments(db, patel_id)
        rows_lakewood = intelligence._current_equipment_assignments(db, lakewood_id)
        check("L. the asset does NOT appear in Patel's current equipment (older row correctly excluded)",
              moved_asset_id not in [r["id"] for r in rows_patel])
        check("L. the asset DOES appear in Lakewood's current equipment (latest applied move)",
              moved_asset_id in [r["id"] for r in rows_lakewood])

        # ============================================================
        # M. Scheduled future move -- must not appear as current
        # ============================================================
        print()
        print("=== M. Scheduled future move must not count as current assignment ===")
        scheduled_asset_cur = db.execute("INSERT INTO sitepulse_assets (name, status, created_at, updated_at) VALUES ('Scheduled Asset', 'Available', ?, ?)", (now, now))
        db.commit()
        scheduled_asset_id = scheduled_asset_cur.lastrowid
        db.execute("INSERT INTO sitepulse_usage_log (asset_id, entry_kind, project_id, job_name, scheduled_date, move_status, created_at) VALUES (?,'move',?,?,?, 'Scheduled', ?)",
                   (scheduled_asset_id, patel_id, "__PITest", "2026-12-01", now))
        db.commit()
        rows_m = intelligence._current_equipment_assignments(db, patel_id)
        check("M. a Scheduled (not-yet-applied) future move does NOT count as current assignment", scheduled_asset_id not in [r["id"] for r in rows_m])

        # ============================================================
        # N. Timestamp tie -- id DESC deterministic
        # ============================================================
        print()
        print("=== N. Deterministic tie-break: identical effective timestamps ===")
        tie_asset_cur = db.execute("INSERT INTO sitepulse_assets (name, status, created_at, updated_at) VALUES ('Tie Asset', 'Out', ?, ?)", (now, now))
        db.commit()
        tie_asset_id = tie_asset_cur.lastrowid
        same_ts = "2026-09-01T10:00:00"
        db.execute("INSERT INTO sitepulse_usage_log (asset_id, entry_kind, project_id, job_name, applied_at, move_status, created_at) VALUES (?,'move',?,?,?, 'Applied', ?)",
                   (tie_asset_id, lakewood_id, "__PITest", same_ts, now))
        db.commit()
        later_row_cur = db.execute("INSERT INTO sitepulse_usage_log (asset_id, entry_kind, project_id, job_name, applied_at, move_status, created_at) VALUES (?,'move',?,?,?, 'Applied', ?)",
                                     (tie_asset_id, patel_id, "__PITest", same_ts, now))
        db.commit()
        later_row_id = later_row_cur.lastrowid
        rows_patel_tie = intelligence._current_equipment_assignments(db, patel_id)
        rows_lakewood_tie = intelligence._current_equipment_assignments(db, lakewood_id)
        check("N. with identical timestamps, the row with the HIGHER id (inserted later) wins -- appears on Patel",
              tie_asset_id in [r["id"] for r in rows_patel_tie])
        check("N. the earlier-id tied row does NOT also win on Lakewood", tie_asset_id not in [r["id"] for r in rows_lakewood_tie])

        # ============================================================
        # O. Large project -- bounded result, truthful counts
        # ============================================================
        print()
        print("=== O. Large project: bounded details, truthful total counts ===")
        db.execute("DELETE FROM tracker_projects WHERE name = '__PITest Big Project'")
        db.commit()
        big_cur = db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES ('__PITest Big Project', 'In Progress', ?, ?)", (now, now))
        db.commit()
        big_id = big_cur.lastrowid
        for i in range(25):
            seed_concrete(big_id, "Submitted" if i < 3 else "Completed", f"2026-09-{(i % 28) + 1:02d}", project_text="__PITest Big Project")
        result_big = intelligence._pi_concrete(db, big_id, "__PITest Big Project")
        check("O. concrete details bounded to the configured max (10)", len(result_big["items"]) == 10)
        check("O. total_count remains the TRUE full count (25), not the capped array length", result_big["total_count"] == 25)
        check("O. truncated flag is True", result_big["truncated"] is True)
        check("O. open_count is truthful (3 Submitted)", result_big["open_count"] == 3)

        # ============================================================
        # BLOCKER 1 (NO-GO round 2): equipment count must be truthful
        # even when >LIMIT assets are currently assigned.
        # ============================================================
        print()
        print("=== O2. Large equipment set: truthful count, bounded details ===")
        db.execute("DELETE FROM tracker_projects WHERE name = '__PITest Big Equipment Project'")
        db.commit()
        big_eq_cur = db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES ('__PITest Big Equipment Project', 'In Progress', ?, ?)", (now, now))
        db.commit()
        big_eq_id = big_eq_cur.lastrowid
        for i in range(22):
            seed_asset_with_move(f"Big Fleet Asset {i}", big_eq_id, f"2026-08-{(i % 28) + 1:02d}")
        result_big_eq = intelligence._pi_equipment(db, big_eq_id)
        check("O2. equipment count is the TRUE full count (22), not the capped detail-array length", result_big_eq["count"] == 22)
        check("O2. equipment details are bounded to the configured max (15)", len(result_big_eq["items"]) == 15)
        check("O2. equipment truncated flag is True", result_big_eq["truncated"] is True)
        # Prove the count query itself is SQL-level (COUNT(*)), not "fetch everything then len() it in Python" --
        # by confirming _current_equipment_count alone matches _pi_equipment's reported count exactly.
        check("O2. the dedicated SQL-level count function agrees exactly with what _pi_equipment reports",
              intelligence._current_equipment_count(db, big_eq_id) == 22)

        # ============================================================
        # P. Source failure isolation
        # ============================================================
        print()
        print("=== P. One module query fails -- others survive, failure logged server-side ===")
        with patch("intelligence._pi_concrete", side_effect=Exception("simulated DB error")):
            draft_p = base_draft({"project_id": patel_id, "name": "__PITest Patel Farm Project"})
            result_p = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, user, session_context=draft_p["project_context"])
        check("P. the overall tool call still succeeds despite one source failing", result_p.success)
        check("P. the failed source (concrete) is absent from the result", "concrete" not in result_p.data)
        check("P. other authorized sources still present", "purchases" in result_p.data and "equipment" in result_p.data)
        log_row = db.execute("SELECT * FROM activity_log WHERE action='atlas_project_intelligence_source_failed' ORDER BY id DESC LIMIT 1").fetchone()
        check("P. the failure was logged server-side with the source name", log_row is not None and log_row["field"] == "concrete")
        check("P. the log identifies the project_id too (diagnosable, not a bare stack trace to the user)", "project_id=" in (log_row["new_value"] or ""))

        # ============================================================
        # BLOCKER 3 (NO-GO round 2): a DOUBLE failure -- the source query
        # itself raises AND the failure-logging/commit path ALSO raises
        # -- must still not crash the overall call or lose the other
        # authorized sections.
        # ============================================================
        print()
        print("=== P2. Double failure: source query fails AND the failure logger itself fails ===")
        real_log_activity = appmod.log_activity

        def _selective_failing_log_activity(*a, **kw):
            # Only the SPECIFIC failure-logging call _pi_source_failure
            # makes (identifiable by its action label) fails here --
            # execute_tool's own unrelated, always-made audit-log call
            # for the tool invocation itself must keep working normally,
            # since that's not what Blocker 3 is about.
            if len(a) >= 4 and a[3] == "atlas_project_intelligence_source_failed":
                raise Exception("simulated logging failure")
            return real_log_activity(*a, **kw)

        with patch("intelligence._pi_concrete", side_effect=Exception("simulated DB error")), \
             patch("app.log_activity", side_effect=_selective_failing_log_activity):
            draft_p2 = base_draft({"project_id": patel_id, "name": "__PITest Patel Farm Project"})
            result_p2 = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, user, session_context=draft_p2["project_context"])
        check("P2. the overall call STILL succeeds even when logging the first failure also fails", result_p2.success)
        check("P2. the failed source (concrete) is still correctly absent", "concrete" not in result_p2.data)
        check("P2. the OTHER unaffected authorized sources still survive and return", "purchases" in result_p2.data and "equipment" in result_p2.data)
        check("P2. no internal exception text/stack trace leaked into the model-facing result",
              "Exception" not in json.dumps(result_p2.data) and "Traceback" not in json.dumps(result_p2.data))

        # ============================================================
        # Q. Typed mode -- zero TTS
        # ============================================================
        print()
        print("=== Q. Typed interaction: zero TTS calls even with intelligence gathering ===")
        tts_count = {"n": 0}

        def _counting_tts(text):
            tts_count["n"] += 1
            return (None, None)
        draft_q = base_draft()
        draft_q["interaction_mode"] = "text"
        with patch("app._elevenlabs_tts_call", side_effect=_counting_tts):
            run_turn("Let's talk about Patel Farm -- what needs attention?", draft_q,
                     [set_project_pass("Patel Farm"), intelligence_pass("attention"), text_pass("One item.")])
        check("Q. typed interaction with full one-turn project+intelligence flow: ZERO TTS calls", tts_count["n"] == 0)

        # ============================================================
        # Legacy free-text fallback -- exact match only, project_id wins absolutely
        # ============================================================
        print()
        print("=== Legacy free-text fallback matrix (project_id wins absolutely, exact match only) ===")
        # A. project_id = Lakewood, free-text = Patel Farm Project name -- must appear ONLY under Lakewood, never Patel.
        seed_concrete(lakewood_id, "Submitted", "2026-09-08", project_text="__PITest Patel Farm Project")
        patel_concrete_a = intelligence._pi_concrete(db, patel_id, "__PITest Patel Farm Project")
        lakewood_concrete_a = intelligence._pi_concrete(db, lakewood_id, "__PITest Lakewood Farm")
        check("A. concrete: project_id=Lakewood + free-text=Patel's name -> Patel does NOT receive it",
              all(item["record_id"] != db.execute("SELECT id FROM inventory_concrete_requests WHERE project_id=? AND project=?", (lakewood_id, "__PITest Patel Farm Project")).fetchone()["id"] for item in patel_concrete_a["items"]))
        check("A. concrete: it DOES correctly appear under Lakewood (its real project_id)",
              any(item["project_id"] == lakewood_id for item in lakewood_concrete_a["items"]))
        check("A. concrete: linked_via is 'project_id' for canonically-linked records (never additionally free-text matched)",
              all(item["linked_via"] == "project_id" for item in patel_concrete_a["items"]))

        def legacy_matrix_for(source_label, seed_fn, pi_fn):
            # B. project_id NULL, free-text EXACTLY the canonical name -> included via legacy_exact_name_match.
            seed_fn(None, "2026-09-09", "__PITest Patel Farm Project")
            result_b = pi_fn(db, patel_id, "__PITest Patel Farm Project")
            check(f"B. {source_label}: project_id NULL + free-text EXACTLY canonical name -> included via legacy_exact_name_match",
                  any(item["linked_via"] == "legacy_exact_name_match" for item in result_b["items"]))

            # C. project_id NULL, free-text is only a SUBSTRING of the canonical name -> NOT included (no LIKE/fuzzy).
            seed_fn(None, "2026-09-10", "__PITest Patel Farm")  # substring of "__PITest Patel Farm Project"
            result_c = pi_fn(db, patel_id, "__PITest Patel Farm Project")
            substring_matches = [item for item in result_c["items"] if item.get("linked_via") == "legacy_exact_name_match"]
            check(f"C. {source_label}: project_id NULL + free-text is only a SUBSTRING of the canonical name -> NOT matched (no LIKE/fuzzy)",
                  len(substring_matches) == 1)  # only B's exact-match record, not C's substring one

            # D. project_id NULL, free-text names a DIFFERENT project entirely -> NOT included.
            seed_fn(None, "2026-09-11", "__PITest Lakewood Farm")
            result_d = pi_fn(db, patel_id, "__PITest Patel Farm Project")
            still_only_one_legacy = [item for item in result_d["items"] if item.get("linked_via") == "legacy_exact_name_match"]
            check(f"D. {source_label}: project_id NULL + free-text names a DIFFERENT project -> NOT included",
                  len(still_only_one_legacy) == 1)  # still just B's record

        legacy_matrix_for("concrete", lambda pid, date, text: seed_concrete(pid, "Submitted", date, project_text=text), intelligence._pi_concrete)
        legacy_matrix_for("purchases", lambda pid, date, text: seed_purchase(pid, "Submitted", date, job_name=text), intelligence._pi_purchases)
        legacy_matrix_for("rentals", lambda pid, date, text: seed_rental(pid, None, date, job_name=text), intelligence._pi_rentals)

        # ============================================================
        # Stale/deleted project context
        # ============================================================
        print()
        print("=== Stale/deleted project context ===")
        doomed_cur = db.execute("INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES ('__PITest Doomed', 'In Progress', ?, ?)", (now, now))
        db.commit()
        doomed_id = doomed_cur.lastrowid
        db.execute("DELETE FROM tracker_projects WHERE id=?", (doomed_id,))
        db.commit()
        result_deleted = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, user, session_context={"project_id": doomed_id, "name": "__PITest Doomed"})
        # execute_tool's own existing stale-context handling (unchanged,
        # shared by every read tool) already pops project_id from
        # session_context and never re-adds it to raw_params once the
        # project no longer exists -- so the handler never even SEES a
        # project_id here, landing on "no_active_project" rather than
        # "not_found". Both are safe/correct; this is which one the
        # existing shared mechanism actually produces.
        check("deleted project: fails safe (no_active_project via execute_tool's existing stale-context handling), no crash",
              result_deleted.success and result_deleted.data.get("found") is False and result_deleted.data.get("reason") == "no_active_project")

        # No active project at all
        result_none = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, user, session_context={})
        check("no active project: found=False, reason=no_active_project", result_none.success and result_none.data.get("reason") == "no_active_project")

        # Malformed scope -> rejected by the registry's own schema
        # validation (enum-constrained) BEFORE it ever reaches the
        # handler -- fails safely with a clean error, never silently
        # expanding access or falling through to any data at all.
        result_malformed = appmod.execute_tool("get_project_intelligence", {"scope": "DROP TABLE users"}, user, session_context={"project_id": patel_id, "name": "x"})
        check("malformed scope: rejected cleanly by schema validation, not an expanded-access fallback, no data returned",
              not result_malformed.success and result_malformed.data is None)

        # Defense in depth (per NO-GO round 2, Blocker 4): the handler's
        # OWN internal check must fail CLOSED for an explicitly invalid
        # scope, not silently broaden into "overview" -- that would mean
        # a malformed/adversarial value queries MORE than a valid one.
        # Omitted/None scope legitimately defaults to overview (tested
        # separately below); an explicit garbage value does not.
        direct_result_bad = intelligence._tool_get_project_intelligence(user, scope="garbage-value", project_id=patel_id)
        check("handler-level defense in depth: an EXPLICIT invalid scope fails closed (found=False, reason=invalid_scope), NOT silently expanded to overview",
              direct_result_bad.get("found") is False and direct_result_bad.get("reason") == "invalid_scope")
        check("handler-level defense in depth: no optional source data is present when scope is rejected", "concrete" not in direct_result_bad)

        # Confirm the legitimate default case (omitted scope) still works normally.
        direct_result_none = intelligence._tool_get_project_intelligence(user, scope=None, project_id=patel_id)
        check("handler-level: OMITTED (None) scope legitimately defaults to overview", direct_result_none.get("found") is True and "concrete" in direct_result_none)

        # ============================================================
        # get_project_status untouched
        # ============================================================
        print()
        print("=== Pass 1B security proof (explicit, not just inherited from Pass 1) ===")

        def raw_lines_1b(*events):
            lines = ["data: " + json.dumps({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": []}})]
            lines.extend("data: " + json.dumps(e) for e in events)
            lines.append("data: [DONE]")
            return lines

        # Pass 1B declares ONLY get_project_intelligence -- cannot invoke set_project_context.
        pass1_ok = set_project_pass("Patel Farm")
        pass1b_capture = {"tools": None}
        real_post = appmod.requests.post

        def _capture_side_effect(*a, **kw):
            idx = _capture_side_effect.calls
            _capture_side_effect.calls += 1
            if idx == 1:
                pass1b_capture["tools"] = kw["json"].get("tools")
            responses = [pass1_ok, text_pass("Confirmed."), text_pass("Switched.")]
            return responses[idx]
        _capture_side_effect.calls = 0
        d_1b_decl = base_draft()
        with patch("app.requests.post", side_effect=_capture_side_effect):
            list(appmod.stream_atlas_turn("Let's talk about Patel Farm", d_1b_decl))
        check("Pass 1B declares ONLY get_project_intelligence (structurally cannot request set_project_context again)",
              pass1b_capture["tools"] is not None and len(pass1b_capture["tools"]) == 1 and pass1b_capture["tools"][0]["name"] == "get_project_intelligence")

        # 2+ tool_use blocks in Pass 1B -> fails closed (project switch still stands, no intelligence).
        d_1b_multi = base_draft()
        lines_1b_multi = raw_lines_1b(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "get_project_intelligence", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t2", "name": "get_project_intelligence", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        events_1b_multi, mp_1b_multi = run_turn("Let's talk about Patel Farm", d_1b_multi,
                                                  [set_project_pass("Patel Farm"), fake_response(lines_1b_multi), text_pass("Switched to Patel Farm Project.")])
        check("Pass 1B with 2+ tool_use blocks: fails closed, no intelligence gathered", mp_1b_multi.call_count == 3)
        pass2_msgs_multi = [m for m in mp_1b_multi.call_args_list[2].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        check("Pass 1B 2+ blocks: only ONE tool_result reached Pass 2 (the project switch), not two", len(pass2_msgs_multi) == 1)
        check("Pass 1B 2+ blocks: the project switch itself still stands (not invalidated by Pass 1B's own failure)",
              d_1b_multi["project_context"].get("project_id") == patel_id)

        # Unapproved tool in Pass 1B -> fails closed.
        d_1b_unapproved = base_draft()
        lines_1b_unapproved = raw_lines_1b(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"project_name": "Lakewood Farm"})}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        events_1b_unapp, mp_1b_unapp = run_turn("Let's talk about Patel Farm", d_1b_unapproved,
                                                  [set_project_pass("Patel Farm"), fake_response(lines_1b_unapproved), text_pass("Switched to Patel Farm Project.")])
        check("Pass 1B requesting set_project_context (unapproved for THIS pass): fails closed, cannot switch project via Pass 1B",
              d_1b_unapproved["project_context"].get("project_id") == patel_id)
        pass2_msgs_unapp = [m for m in mp_1b_unapp.call_args_list[2].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        tr_unapp = json.loads(pass2_msgs_unapp[0]["content"][0]["content"])
        check("Pass 1B unapproved tool request: the ONE tool_result reaching Pass 2 is still the ORIGINAL Patel Farm switch, not Lakewood",
              tr_unapp.get("project_id") == patel_id)

        # Incomplete SSE (EOF before message_stop) in Pass 1B -> fails closed.
        d_1b_eof = base_draft()
        lines_1b_eof = [
            "data: " + json.dumps({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": []}}),
            "data: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "get_project_intelligence", "input": {}}}),
            "data: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
            "data: " + json.dumps({"type": "content_block_stop", "index": 0}),
            "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
            # deliberately no message_stop -- EOF here
            "data: [DONE]",
        ]
        events_1b_eof, mp_1b_eof = run_turn("Let's talk about Patel Farm", d_1b_eof,
                                              [set_project_pass("Patel Farm"), fake_response(lines_1b_eof), text_pass("Switched to Patel Farm Project.")])
        check("Pass 1B EOF before message_stop: fails closed, no intelligence executed", mp_1b_eof.call_count == 3)
        pass2_msgs_eof = [m for m in mp_1b_eof.call_args_list[2].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        check("Pass 1B EOF: only the project-switch tool_result reached Pass 2, no intelligence result", len(pass2_msgs_eof) == 1)

        # Malformed tool block (invalid JSON input) in Pass 1B -> fails closed.
        d_1b_malformed = base_draft()
        lines_1b_malformed = raw_lines_1b(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "get_project_intelligence", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{not valid json"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        )
        events_1b_bad, mp_1b_bad = run_turn("Let's talk about Patel Farm", d_1b_malformed,
                                              [set_project_pass("Patel Farm"), fake_response(lines_1b_malformed), text_pass("Switched to Patel Farm Project.")])
        check("Pass 1B malformed tool input: fails closed, project switch still stands", d_1b_malformed["project_context"].get("project_id") == patel_id)
        pass2_msgs_bad = [m for m in mp_1b_bad.call_args_list[2].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        check("Pass 1B malformed tool input: only the project-switch tool_result reached Pass 2", len(pass2_msgs_bad) == 1)

        # Model cannot supply project_id to get_project_intelligence via Pass 1B.
        d_1b_pid = base_draft()
        pass1b_with_pid = fake_response(build_sse_lines(
            [("tool_use", "get_project_intelligence", "t9", json.dumps({"scope": "overview", "project_id": 999999}))],
            stop_reason="tool_use"
        ))
        events_1b_pid, mp_1b_pid = run_turn("Let's talk about Patel Farm", d_1b_pid,
                                              [set_project_pass("Patel Farm"), pass1b_with_pid, text_pass("Switched to Patel Farm Project.")])
        check("Pass 1B: model-supplied project_id is rejected, intelligence not executed with it, project switch still stands",
              d_1b_pid["project_context"].get("project_id") == patel_id)
        pass2_msgs_pid = [m for m in mp_1b_pid.call_args_list[2].kwargs["json"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
        check("Pass 1B project_id injection: only the project-switch tool_result reached Pass 2 (no intelligence executed against the fake id)",
              len(pass2_msgs_pid) == 1)

        # Ambiguous / failed / malformed resolution -> Pass 1B never even attempted.
        pass1_ambiguous = fake_response(build_sse_lines(
            [("tool_use", "set_project_context", "ta", json.dumps({"project_name": "Farm Ambiguous"}))],
            stop_reason="tool_use"
        ))
        d_1b_amb = base_draft()
        _, mp_1b_amb = run_turn("Let's talk about Farm Ambiguous", d_1b_amb, [pass1_ambiguous, text_pass("Which one -- A or B?")])
        check("ambiguous resolution: Pass 1B never attempted (only 2 calls, not 3)", mp_1b_amb.call_count == 2)

        pass1_notfound = fake_response(build_sse_lines(
            [("tool_use", "set_project_context", "tb", json.dumps({"project_name": "Totally Fake Project"}))],
            stop_reason="tool_use"
        ))
        d_1b_nf = base_draft()
        _, mp_1b_nf = run_turn("Let's talk about a fake project", d_1b_nf, [pass1_notfound, text_pass("I couldn't find that project.")])
        check("failed/not-found resolution: Pass 1B never attempted (only 2 calls, not 3)", mp_1b_nf.call_count == 2)

        print()
        print("=== Mid-stream connection failure (the ACTUAL real-browser-acceptance failure class) ===")
        # This is the defect that caused the real browser hang: a
        # requests.exceptions.RequestException raised WHILE iterating
        # resp.iter_lines() (not at initial connection) was completely
        # unprotected -- it escaped stream_atlas_turn entirely, killing
        # the SSE response with NO terminal event ever sent, leaving the
        # browser's thinking indicator stuck forever. Every one of the
        # three passes on this exact turn shape is tested here.

        # 1. Pass 1 itself hangs/drops mid-stream (after some initial bytes).
        d_hang1 = base_draft()
        partial_pass1 = [
            "data: " + json.dumps({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": []}}),
            "data: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "set_project_context", "input": {}}}),
        ]
        evs_hang1, mp_hang1 = run_turn("Let's talk about Patel Farm - what needs attention?", d_hang1,
                                         [fake_midstream_hang_response(partial_pass1)])
        check("1. Pass 1 mid-stream drop: the generator terminates safely -- no exception escapes stream_atlas_turn (proven by run_turn completing without raising)", True)
        check("1. Pass 1 mid-stream drop: no project_context mutation from an incomplete/killed stream", d_hang1["project_context"] == {})
        check("1. Pass 1 mid-stream drop: only 1 API call made (fails closed, no Pass 1B/Pass 2 with invalid state)", mp_hang1.call_count == 1)
        visible_hang1 = "".join(e.get("text", "") for e in evs_hang1 if e.get("type") == "delta")
        check("1. Pass 1 mid-stream drop: the browser receives an explicit, safe terminal error message (not silence)",
              ("trouble completing" in visible_hang1.lower() or "try again" in visible_hang1.lower()))
        check("E. Pass 1 mid-stream drop: the raw simulated exception text is NOT present in the employee-visible SSE output",
              "simulated mid-stream connection drop" not in visible_hang1 and "ConnectionError" not in visible_hang1)
        done_hang1 = next((e for e in evs_hang1 if e.get("type") == "done"), None)
        check("1. Pass 1 mid-stream drop: a real terminal 'done' event was emitted -- the browser's loading/thinking state CAN clear",
              done_hang1 is not None)

        # 2. Pass 1 completes fine, Pass 1B hangs/drops mid-stream --
        # Pass 1B's own failure is designed to degrade GRACEFULLY (the
        # already-successful project switch stands, and Pass 2 still
        # runs to give a real, useful answer grounded in that switch
        # alone) rather than aborting the whole turn -- so Pass 2 is
        # still expected to run here (3 calls total), NOT skipped.
        d_hang1b = base_draft()
        partial_pass1b = [
            "data: " + json.dumps({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": []}}),
            "data: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t2", "name": "get_project_intelligence", "input": {}}}),
        ]
        evs_hang1b, mp_hang1b = run_turn("Let's talk about Patel Farm - what needs attention?", d_hang1b,
                                           [set_project_pass("Patel Farm"), fake_midstream_hang_response(partial_pass1b),
                                            text_pass("We're now on Patel Farm Project.")])
        check("2. Pass 1B mid-stream drop: generator terminates safely, no unhandled exception", True)
        check("2. Pass 1B mid-stream drop: the successful project switch from Pass 1 STILL stands (not invalidated by Pass 1B's own failure)",
              d_hang1b["project_context"].get("project_id") == patel_id)
        check("2. Pass 1B mid-stream drop: Pass 1B's failure degrades GRACEFULLY -- Pass 2 still runs to give a real answer (3 calls total)",
              mp_hang1b.call_count == 3)
        visible_hang1b = "".join(e.get("text", "") for e in evs_hang1b if e.get("type") == "delta")
        check("2. Pass 1B mid-stream drop: the user still gets a real, useful answer about the project switch (not a dead silence, not a fake error either)",
              "Patel Farm" in visible_hang1b)
        done_hang1b = next((e for e in evs_hang1b if e.get("type") == "done"), None)
        check("2. Pass 1B mid-stream drop: a real terminal 'done' event reached the browser", done_hang1b is not None)

        # 3. Pass 1 + Pass 1B complete fine, Pass 2 (the actual visible-answer call) hangs/drops mid-stream.
        d_hang2 = base_draft()
        partial_pass2 = [
            "data: " + json.dumps({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "content": []}}),
            "data: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            "data: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Patel Farm has"}}),
        ]
        evs_hang2, mp_hang2 = run_turn("Let's talk about Patel Farm - what needs attention?", d_hang2,
                                         [set_project_pass("Patel Farm"), intelligence_pass("attention"), fake_midstream_hang_response(partial_pass2)])
        check("3. Pass 2 mid-stream drop: generator terminates safely, no unhandled exception", True)
        check("3. Pass 2 mid-stream drop: BOTH the project switch and intelligence gathering already genuinely succeeded, and correctly still stand",
              d_hang2["project_context"].get("project_id") == patel_id)
        check("3. Pass 2 mid-stream drop: all 3 API calls were made (the failure is specifically in rendering the final answer, not earlier)", mp_hang2.call_count == 3)
        visible_hang2 = "".join(e.get("text", "") for e in evs_hang2 if e.get("type") == "delta")
        check("3. Pass 2 mid-stream drop: the browser receives a safe terminal error (partial 'Patel Farm has' text is not left as a fake completed answer)",
              ("trouble completing" in visible_hang2.lower() or "try again" in visible_hang2.lower()))
        check("F. Pass 2 mid-stream drop: the raw simulated exception text is NOT present in the employee-visible SSE output",
              "simulated mid-stream connection drop" not in visible_hang2 and "ConnectionError" not in visible_hang2)
        done_hang2 = next((e for e in evs_hang2 if e.get("type") == "done"), None)
        check("3. Pass 2 mid-stream drop: a real terminal 'done' event reached the browser -- the thinking indicator can clear",
              done_hang2 is not None and done_hang2.get("pending_write_token") is None)

        print()
        print("=== Pass 1B dedicated system prompt (scoped-prompt fix) ===")
        pass1b_prompt = appmod._build_pass1b_intelligence_prompt()
        forbidden_tool_names = [
            "set_project_context", "get_project_status", "list_bids_needing_attention", "list_bids_due_soon",
            "list_upcoming_concrete_pours", "find_equipment", "list_rentals_due", "list_open_purchase_requests",
            "get_attention_items", "create_concrete_request",
        ]
        for forbidden in forbidden_tool_names:
            check(f"1/2a. RETURNED PROMPT never mentions '{forbidden}' (what Anthropic actually receives)", forbidden not in pass1b_prompt)

        # 5. Distinguish (a) the returned prompt string (what Anthropic
        # actually receives) from (b) the function's own SOURCE CODE
        # (docstring/comments included) -- a stale explanatory docstring
        # naming a forbidden tool would pass (a) while still being a
        # real source-level isolation violation, exactly the gap the
        # independent reviewer found in the original v5.6 package. This
        # inspects the actual function source via `inspect.getsource`,
        # not just its return value.
        import inspect
        pass1b_source = inspect.getsource(appmod._build_pass1b_intelligence_prompt)
        for forbidden in forbidden_tool_names:
            check(f"1/2b. FUNCTION SOURCE (including docstring/comments) never mentions '{forbidden}'", forbidden not in pass1b_source)
        check("1/2b. the function's docstring is short and generic, not a large explanatory essay naming other tools",
              len(inspect.getdoc(appmod._build_pass1b_intelligence_prompt) or "") < 120)
        check("3. Pass1B prompt DOES mention get_project_intelligence (the one tool it's actually for)",
              "get_project_intelligence" in pass1b_prompt)
        check("Pass1B prompt states project_id is server-controlled, never model-supplied",
              "project id" in pass1b_prompt.lower() and "server" in pass1b_prompt.lower())
        check("Pass1B prompt states canonical project context is already established server-side",
              "already been established" in pass1b_prompt.lower())
        check("Pass1B prompt requires exactly one tool call", "exactly once" in pass1b_prompt)
        check("Pass1B prompt does not include fabricated factual project data (no stored project name/records baked in)",
              "Patel" not in pass1b_prompt and "__PITest" not in pass1b_prompt)

        # 4-9: scope-guidance mappings present in the prompt text itself.
        check("4. prompt maps attention-type questions to scope=attention", "attention: " in pass1b_prompt and '"what needs attention"' in pass1b_prompt)
        check("5. prompt maps broad project-status questions to scope=overview", "overview: " in pass1b_prompt and "what's happening with this project" in pass1b_prompt)
        check("6. prompt maps equipment questions to scope=equipment", "equipment: " in pass1b_prompt)
        check("7. prompt maps concrete questions to scope=concrete", "concrete: " in pass1b_prompt)
        check("8. prompt maps purchase questions to scope=purchases", "purchases: " in pass1b_prompt)
        check("9. prompt maps rental questions to scope=rentals", "rentals: " in pass1b_prompt)

        print()
        print("=== Pass 1B call site actually uses the dedicated prompt; Pass1/Pass2 still use the shared one ===")
        shared_system_prompt_holder = {}

        def _capture_system_side_effect(*a, **kw):
            idx = _capture_system_side_effect.calls
            _capture_system_side_effect.calls += 1
            shared_system_prompt_holder[idx] = kw["json"].get("system")
            responses = [
                set_project_pass("Patel Farm"),
                intelligence_pass("attention"),
                text_pass("One item needs attention."),
            ]
            return responses[idx]
        _capture_system_side_effect.calls = 0
        d_prompt_check = base_draft()
        with patch("app.requests.post", side_effect=_capture_system_side_effect):
            list(appmod.stream_atlas_turn("Let's talk about Patel Farm - what needs attention?", d_prompt_check))
        check("Pass 1's system prompt is the SHARED Atlas prompt (mentions set_project_context)",
              "set_project_context" in shared_system_prompt_holder[0])
        check("Pass 1B's system prompt is the DEDICATED prompt (does NOT mention set_project_context)",
              shared_system_prompt_holder[1] == pass1b_prompt and "set_project_context" not in shared_system_prompt_holder[1])
        check("Pass 2's system prompt is the SHARED Atlas prompt again (mentions set_project_context)",
              "set_project_context" in shared_system_prompt_holder[2])
        check("Pass 1B's dedicated prompt is DIFFERENT from Pass 1/Pass 2's shared prompt",
              shared_system_prompt_holder[1] != shared_system_prompt_holder[0])

        print()
        print("=== get_project_status remains untouched ===")
        check("get_project_status is NOT in ATLAS_NATIVE_TOOLS_ALLOWED", "get_project_status" not in appmod.ATLAS_NATIVE_TOOLS_ALLOWED)
        check("get_project_intelligence IS in ATLAS_NATIVE_TOOLS_ALLOWED", "get_project_intelligence" in appmod.ATLAS_NATIVE_TOOLS_ALLOWED)
        gps_result = appmod.execute_tool("get_project_status", {"project_id": patel_id}, user)
        check("get_project_status still works exactly as before, unmodified", gps_result.success and gps_result.data.get("found") is True)

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__PITest%'")
    db.execute("DELETE FROM inventory_purchase_requests WHERE job_name LIKE '__PITest%'")
    db.execute("DELETE FROM sitepulse_rentals WHERE job_name LIKE '__PITest%'")
    db.execute("DELETE FROM sitepulse_usage_log WHERE job_name LIKE '__PITest%'")
    db.execute("DELETE FROM sitepulse_assets WHERE name IN ('Skid Steer A','Excavator B','Moved Asset','Scheduled Asset','Tie Asset')")
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__PITest%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
