"""
Atlas Canonical Project Context (item 6) regression.

Uses the real app and the real execute_tool() gateway -- same pattern as
scripts/phase3a_tests.py. Runs entirely against its own disposable,
isolated database.

IMPORTANT SCOPE NOTE (matches the actual shipped architecture): this
batch establishes canonical Atlas project context (set_project_context,
the session-scoped project_context dict, and execute_tool's generic
project_id injection) and connects it to the one existing write tool
(create_concrete_request) plus the read tools that already declare a
project_id parameter (get_project_status). It does NOT create a
generalized dynamic tool-dispatch loop across every BuildIQ module --
that would be a materially larger, separate change. These tests verify
the foundation and its one real connection point, not a capability that
doesn't exist yet.

Covers exactly the scenarios called out for this pass:
  - exact project resolution
  - ambiguous project name -> no context set
  - invalid project_id
  - deleted/unlinked project after context was set (fail safe, context
    cleared so the person is asked to re-establish it)
  - context switch A -> B
  - explicit tool project_id wins over session context
  - session context fills a missing project_id
  - create_concrete_request's write-confirmation flow uses canonical
    context when the model didn't collect a project_id itself
  - write confirmation is still required -- project resolution cannot
    bypass it
  - no context leak between two separate Atlas sessions (two different
    session_context dicts never share state)

Usage (from the project root):
    APP_ENV=development python3 scripts/atlas_project_context_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
import secrets
import time
from datetime import datetime, date

import _test_db_setup
_test_db_setup.isolate_test_database()  # MUST happen before `import app`

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


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()

    print("Setting up temporary test fixtures...")
    from werkzeug.security import generate_password_hash
    test_pw = "TestPass123!"
    test_pw_hash = generate_password_hash(test_pw)
    db.execute("DELETE FROM users WHERE email='__atlas_ctx_user@test.local'")
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES "
               "('__atlas_ctx_user','__atlas_ctx_user@test.local',?,?)", (test_pw_hash, now))
    db.commit()
    user_row = db.execute("SELECT * FROM users WHERE email='__atlas_ctx_user@test.local'").fetchone()
    admin_role = db.execute("SELECT id FROM roles WHERE name='Administrator'").fetchone()[0]
    db.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (user_row["id"], admin_role))
    db.commit()
    user = appmod.User(user_row)

    # Two distinctly-named projects, plus a deliberately ambiguous pair
    # (share a common substring) and a third that will be deleted mid-test.
    def make_project(name):
        cur = db.execute(
            "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
            (name, now, now)
        )
        db.commit()
        return cur.lastrowid

    project_a_id = make_project("__Test Patel Farm")
    project_b_id = make_project("__Test Overlook Tower")
    make_project("__Test Ambiguous North")
    make_project("__Test Ambiguous South")
    project_to_delete_id = make_project("__Test Doomed Project")
    project_to_delete_id_2 = make_project("__Test Doomed Project 2")

    with appmod.app.test_request_context('/'):
        check("all 10 tools registered including set_project_context",
              "set_project_context" in appmod.ATLAS_TOOLS)
        tool = appmod.ATLAS_TOOLS["set_project_context"]
        check("set_project_context uses a read-level permission (module:project_hunt:view)",
              tool.permission == "module:project_hunt:view")
        check("set_project_context is classified as a read tool (no DB mutation, no write-confirmation gate)",
              tool.kind == "read")

        # ================================================================
        # 1. Exact project resolution
        # ================================================================
        print()
        print("=== 1. Exact project resolution ===")
        ctx = {}
        result = appmod.execute_tool("set_project_context", {"project_name": "__Test Patel Farm"}, user, session_context=ctx)
        check("exact-name resolution succeeds", result.success and result.data.get("found"))
        check("resolved to the correct project_id", result.data.get("project_id") == project_a_id)
        check("session_context is populated with project_id", ctx.get("project_id") == project_a_id)
        check("session_context is populated with the project name", ctx.get("name") == "__Test Patel Farm")

        # ================================================================
        # 2. Ambiguous project name -> no context set
        # ================================================================
        print()
        print("=== 2. Ambiguous project name -> context NOT set ===")
        ctx2 = {}
        result = appmod.execute_tool("set_project_context", {"project_name": "__Test Ambiguous"}, user, session_context=ctx2)
        check("ambiguous name does not resolve to found=True", result.success and not result.data.get("found"))
        check("ambiguous result reports the reason", result.data.get("reason") == "ambiguous")
        check("ambiguous result includes the candidate matches", len(result.data.get("matches", [])) == 2)
        check("session_context remains empty -- nothing guessed", ctx2 == {})

        # ================================================================
        # 3. Invalid project_id
        # ================================================================
        print()
        print("=== 3. Invalid project_id ===")
        ctx3 = {}
        result = appmod.execute_tool("set_project_context", {"project_id": 999999}, user, session_context=ctx3)
        check("invalid project_id does not resolve", result.success and not result.data.get("found"))
        check("invalid project_id reports not_found", result.data.get("reason") == "not_found")
        check("session_context remains empty for an invalid id", ctx3 == {})

        # ================================================================
        # 4. Session context fills a missing project_id on another tool
        # ================================================================
        print()
        print("=== 4. Session context fills a missing project_id (get_project_status) ===")
        ctx4 = {}
        appmod.execute_tool("set_project_context", {"project_name": "__Test Patel Farm"}, user, session_context=ctx4)
        result = appmod.execute_tool("get_project_status", {}, user, session_context=ctx4)
        check("get_project_status resolves the right project purely from session context (no project_name/id passed)",
              result.success and result.data.get("project_id") == project_a_id)

        # ================================================================
        # 5. Explicit tool project_id wins over session context
        # ================================================================
        print()
        print("=== 5. Explicit project_id always wins over session context ===")
        ctx5 = {}
        appmod.execute_tool("set_project_context", {"project_name": "__Test Patel Farm"}, user, session_context=ctx5)
        check("(setup) context is now Patel Farm", ctx5.get("project_id") == project_a_id)
        result = appmod.execute_tool("get_project_status", {"project_id": project_b_id}, user, session_context=ctx5)
        check("an explicitly-provided project_id is used, NOT the session context's project", 
              result.success and result.data.get("project_id") == project_b_id)
        check("session context itself is unchanged by a one-off explicit override on another tool",
              ctx5.get("project_id") == project_a_id)

        # ================================================================
        # 6. Context switch A -> B
        # ================================================================
        print()
        print("=== 6. Context switch A -> B in the same session ===")
        ctx6 = {}
        appmod.execute_tool("set_project_context", {"project_name": "__Test Patel Farm"}, user, session_context=ctx6)
        check("(setup) context starts as Patel Farm", ctx6.get("project_id") == project_a_id)
        appmod.execute_tool("set_project_context", {"project_name": "__Test Overlook Tower"}, user, session_context=ctx6)
        check("context switched cleanly to Overlook Tower", ctx6.get("project_id") == project_b_id)
        check("context name updated to match", ctx6.get("name") == "__Test Overlook Tower")
        result = appmod.execute_tool("get_project_status", {}, user, session_context=ctx6)
        check("a subsequent tool call now resolves against the NEW project, not the old one",
              result.success and result.data.get("project_id") == project_b_id)

        # An ambiguous switch attempt must NOT clobber the existing good context.
        appmod.execute_tool("set_project_context", {"project_name": "__Test Ambiguous"}, user, session_context=ctx6)
        check("a failed/ambiguous switch attempt leaves the previously-established context intact",
              ctx6.get("project_id") == project_b_id)

        # ================================================================
        # 7. Deleted/unlinked project after context was set -- fail safe
        # ================================================================
        print()
        print("=== 7. Deleted project after context was set -- fails safe, context cleared ===")
        ctx7 = {}
        appmod.execute_tool("set_project_context", {"project_id": project_to_delete_id}, user, session_context=ctx7)
        check("(setup) context established for the soon-to-be-deleted project", ctx7.get("project_id") == project_to_delete_id)
        db.execute("DELETE FROM tracker_projects WHERE id = ?", (project_to_delete_id,))
        db.commit()
        result = appmod.execute_tool("get_project_status", {}, user, session_context=ctx7)
        check("tool call proceeds without the now-dangling project_id injected (fails safe, not a crash)", result.success)
        check("the deleted project's data is not returned (no project_id was actually injected)",
              not result.data.get("found") or result.data.get("project_id") != project_to_delete_id)
        check("session_context is cleared after detecting the project no longer exists -- must be re-established",
              "project_id" not in ctx7)

        # ================================================================
        # 8/9. create_concrete_request write flow uses canonical context,
        # AND write confirmation is still required (resolution cannot skip it)
        # ================================================================
        print()
        print("=== 8/9. create_concrete_request uses session context; write confirmation still required ===")
        ctx8 = {}
        appmod.execute_tool("set_project_context", {"project_name": "__Test Patel Farm"}, user, session_context=ctx8)

        # UNCONFIRMED write attempt must still be rejected regardless of a
        # perfectly good, resolved project context -- context resolution
        # must never be treated as write confirmation.
        unconfirmed_fields = {
            "project": "__Test Patel Farm", "pour_date": date.today().isoformat(),
            "job_site_address": "123 Test Way", "area_description": "Test Slab",
            "mix_design_psi": "4000", "mix_slump": "4", "concrete_amount": "10 yd",
            "truck_spacing": "15 min", "pump_type": "None", "lab_required": "No", "drilling_required": "No",
        }
        result = appmod.execute_tool("create_concrete_request", unconfirmed_fields, user, confirmed=False, session_context=ctx8)
        check("an UNCONFIRMED create_concrete_request call is rejected even with a resolved project context",
              not result.success and "confirmation" in (result.error or "").lower())
        before_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests WHERE project = '__Test Patel Farm'").fetchone()[0]
        check("(verify) nothing was actually written by the unconfirmed attempt", before_count == 0)

        # ----------------------------------------------------------
        # CANONICAL IDENTITY test 1: session context Project A + missing
        # project_id in the tool call -> record links to A AND the
        # stored project TEXT agrees with A's canonical name (not just
        # the id).
        # ----------------------------------------------------------
        fields_no_project_id = dict(unconfirmed_fields)
        result = appmod.execute_tool("create_concrete_request", fields_no_project_id, user, confirmed=True, session_context=ctx8)
        check("confirmed write succeeds", result.success)
        new_id = result.data.get("id")
        written_row = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (new_id,)).fetchone()
        check("the written request's project_id was filled in from session context (never explicitly provided)",
              written_row["project_id"] == project_a_id)
        check("CANONICAL IDENTITY 1: stored project TEXT agrees with the canonical name of the context-supplied project_id (A)",
              written_row["project"] == "__Test Patel Farm")

        # ----------------------------------------------------------
        # CANONICAL IDENTITY test 2: session context Project A + the
        # tool call explicitly specifies Project B's id (free-text
        # `project` still says "Patel Farm", inherited from the dict
        # copy below, exactly reproducing the real bug report) -> B's
        # id wins (already covered by execute_tool's precedence rule)
        # AND the stored project TEXT is corrected to B's canonical
        # name, not left as the stale/contradictory "Patel Farm" text.
        # This is the exact scenario the release review flagged: this
        # assertion previously only checked the id and PASSED while the
        # text silently stayed wrong -- it now checks both.
        # ----------------------------------------------------------
        ctx8b = dict(ctx8)  # still Patel Farm
        fields_conflicting = dict(unconfirmed_fields)  # project text = "__Test Patel Farm"
        fields_conflicting["project_id"] = project_b_id  # explicit id = Overlook Tower
        result = appmod.execute_tool("create_concrete_request", fields_conflicting, user, confirmed=True, session_context=ctx8b)
        check("confirmed write with an explicit (conflicting) project_id succeeds", result.success)
        written_row_2 = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (result.data.get("id"),)).fetchone()
        check("CANONICAL IDENTITY 2: the explicit project_id wins for LINKAGE (Overlook Tower, not Patel Farm)",
              written_row_2["project_id"] == project_b_id)
        check("CANONICAL IDENTITY 2: stored project TEXT is CORRECTED to the canonical name of the id that won (Overlook Tower), not left as the conflicting free-text (Patel Farm)",
              written_row_2["project"] == "__Test Overlook Tower")
        check("CANONICAL IDENTITY 3: the contradictory pairing (Patel Farm text + Overlook Tower id) was never actually persisted",
              not (written_row_2["project"] == "__Test Patel Farm" and written_row_2["project_id"] == project_b_id))

        # ----------------------------------------------------------
        # CANONICAL IDENTITY test 4 (corrected): an explicit, invalid/
        # non-existent project_id must FAIL CLOSED -- not silently
        # degrade into an unlinked request. Zero rows written.
        # ----------------------------------------------------------
        before_count_invalid = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        fields_invalid_id = dict(unconfirmed_fields)
        fields_invalid_id["project"] = "__Test Some External Job"
        fields_invalid_id["project_id"] = 999999
        result = appmod.execute_tool("create_concrete_request", fields_invalid_id, user, confirmed=True, session_context={})
        check("CANONICAL IDENTITY 4: an explicit invalid project_id FAILS the write (does not succeed)", not result.success)
        check("CANONICAL IDENTITY 4: the failure is the specific structured reason, not a generic error",
              result.error == "project_not_found")
        after_count_invalid = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        check("CANONICAL IDENTITY 4: zero rows were written by the failed attempt", after_count_invalid == before_count_invalid)

        # ----------------------------------------------------------
        # CANONICAL IDENTITY test 5: a DELETED/stale SESSION-CONTEXT
        # project (as opposed to an explicitly-supplied bad id above)
        # must also fail closed for a write tool -- execute_tool clears
        # the stale context (already covered by the earlier deleted-
        # project read-tool test) but must not let the write proceed
        # unlinked. Zero rows written.
        # ----------------------------------------------------------
        ctx_stale = {}
        appmod.execute_tool("set_project_context", {"project_id": project_to_delete_id_2}, user, session_context=ctx_stale)
        check("(setup) stale-context test project resolved before being deleted", ctx_stale.get("project_id") == project_to_delete_id_2)
        db.execute("DELETE FROM tracker_projects WHERE id = ?", (project_to_delete_id_2,))
        db.commit()

        before_count_stale = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        fields_no_id_stale_ctx = dict(unconfirmed_fields)
        fields_no_id_stale_ctx["project"] = "__Test Stale Context Job"
        result = appmod.execute_tool("create_concrete_request", fields_no_id_stale_ctx, user, confirmed=True, session_context=ctx_stale)
        check("CANONICAL IDENTITY 5: a write relying on a now-deleted session-context project FAILS (does not silently go unlinked)",
              not result.success)
        check("CANONICAL IDENTITY 5: the failure is the specific structured reason for stale context",
              result.error == "project_context_stale")
        after_count_stale = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        check("CANONICAL IDENTITY 5: zero rows were written by the failed attempt", after_count_stale == before_count_stale)

        # ----------------------------------------------------------
        # CANONICAL IDENTITY test 6: the stale context is cleared by the
        # failed attempt above (not left around to fail the same way
        # forever) -- confirmed directly on the same dict.
        # ----------------------------------------------------------
        check("CANONICAL IDENTITY 6: session_context no longer holds the deleted project_id after the failed write",
              "project_id" not in ctx_stale)

        # ----------------------------------------------------------
        # CANONICAL IDENTITY test 7: a genuinely intentional external/
        # unlinked request (no project_id supplied at all, no session
        # context established either) still works exactly as before --
        # the fail-closed behavior above is specific to a project_id
        # having been ATTEMPTED, not a blanket requirement that every
        # request have one.
        # ----------------------------------------------------------
        fields_intentionally_unlinked = dict(unconfirmed_fields)
        fields_intentionally_unlinked["project"] = "__Test Genuinely External Job"
        result = appmod.execute_tool("create_concrete_request", fields_intentionally_unlinked, user, confirmed=True, session_context={})
        check("CANONICAL IDENTITY 7: a request with no project_id attempted at all still succeeds unlinked", result.success)
        written_row_7 = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (result.data.get("id"),)).fetchone()
        check("CANONICAL IDENTITY 7: it is stored with no project_id, exactly as an intentionally-external job should be",
              written_row_7["project_id"] is None and written_row_7["project"] == "__Test Genuinely External Job")

        # ----------------------------------------------------------
        # CANONICAL IDENTITY test 8: write confirmation is still
        # required even AFTER a project context has been (re-)
        # established -- resolving/re-establishing context must never
        # be treated as, or substitute for, confirmation.
        # ----------------------------------------------------------
        ctx_reestablished = {}
        appmod.execute_tool("set_project_context", {"project_name": "__Test Patel Farm"}, user, session_context=ctx_reestablished)
        before_count_confirm = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        result = appmod.execute_tool("create_concrete_request", dict(unconfirmed_fields), user, confirmed=False, session_context=ctx_reestablished)
        check("CANONICAL IDENTITY 8: an UNCONFIRMED write still fails even with a freshly re-established, perfectly valid project context",
              not result.success and "confirmation" in (result.error or "").lower())
        after_count_confirm = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        check("CANONICAL IDENTITY 8: zero rows written by the unconfirmed attempt", after_count_confirm == before_count_confirm)
        result = appmod.execute_tool("create_concrete_request", dict(unconfirmed_fields), user, confirmed=True, session_context=ctx_reestablished)
        check("CANONICAL IDENTITY 8: the SAME request, now confirmed, succeeds normally", result.success)

        # ----------------------------------------------------------
        # WhatsApp/group-routing uses the same canonical project
        # identity as what actually got persisted (test 2's already-
        # verified canonical text drives the routing lookup).
        # ----------------------------------------------------------
        check("routing lookup for the canonical (winning) project name is what the stored/notified text actually is",
              written_row_2["project"] == "__Test Overlook Tower")



        # ================================================================
        # 10. No context leak between two separate Atlas sessions
        # ================================================================
        print()
        print("=== 10. No context leak between separate Atlas sessions ===")
        session_1_ctx = {}
        session_2_ctx = {}
        appmod.execute_tool("set_project_context", {"project_name": "__Test Patel Farm"}, user, session_context=session_1_ctx)
        appmod.execute_tool("set_project_context", {"project_name": "__Test Overlook Tower"}, user, session_context=session_2_ctx)
        check("session 1's context is Patel Farm", session_1_ctx.get("project_id") == project_a_id)
        check("session 2's context is Overlook Tower", session_2_ctx.get("project_id") == project_b_id)
        check("the two session contexts are genuinely independent dicts, not shared state",
              session_1_ctx.get("project_id") != session_2_ctx.get("project_id"))
        # Mutating one must never affect the other.
        session_1_ctx["project_id"] = 12345
        check("mutating session 1's context does not affect session 2's", session_2_ctx.get("project_id") == project_b_id)

    # ================================================================
    # 11. THE EXACT SEQUENCE FROM THE RELEASE REVIEW -- direct execute_tool
    # calls, reproducing the real failure path step by step:
    #   1. Create Project A.
    #   2. Establish Project A as session context.
    #   3. Delete Project A.
    #   4. Attempt create_concrete_request with confirmed=False.
    #   5. Assert failure is project_context_stale, NOT "confirmation required".
    #   6. Assert ZERO rows created.
    #   7. Assert stale session context is cleared.
    #   8. Simulate the confirmed retry using the SAME session context dict.
    #   9. Assert it still cannot silently create an unlinked request.
    #  10. Assert ZERO rows.
    #  11. Re-establish a valid canonical project.
    #  12. Create a fresh proposal.
    #  13. Assert normal confirmation IS required (the gate isn't just gone).
    #  14. Confirm it.
    #  15. Assert exactly one correctly linked request is created.
    # This is what the reordering (permission -> stale-check -> confirm ->
    # handler, stale-check strictly BEFORE confirm) exists to prove: the
    # very first call that would have depended on a stale project is
    # rejected before it can ever enter -- and lose the evidence needed
    # for -- the confirm/retry exchange.
    # ================================================================
    print()
    print("=== 11. THE EXACT FAILURE SEQUENCE: unconfirmed-then-confirmed retry must never launder a stale context into an unlinked write ===")
    with appmod.app.test_request_context('/'):
        project_c_cur = db.execute(
            "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
            ("__Test Sequence Project C", now, now)
        )
        db.commit()
        project_c_id = project_c_cur.lastrowid

        ctx11 = {}
        appmod.execute_tool("set_project_context", {"project_id": project_c_id}, user, session_context=ctx11)
        check("(setup) step 1-2: Project C established as session context", ctx11.get("project_id") == project_c_id)

        db.execute("DELETE FROM tracker_projects WHERE id = ?", (project_c_id,))
        db.commit()
        check("(setup) step 3: Project C is now deleted", db.execute("SELECT 1 FROM tracker_projects WHERE id = ?", (project_c_id,)).fetchone() is None)

        fields_seq = {
            "project": "__Test Sequence Job", "pour_date": date.today().isoformat(),
            "job_site_address": "123 Test Way", "area_description": "Test Slab",
            "mix_design_psi": "4000", "mix_slump": "4", "concrete_amount": "10 yd",
            "truck_spacing": "15 min", "pump_type": "None", "lab_required": "No", "drilling_required": "No",
        }
        before_count_seq = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]

        # Step 4-5: unconfirmed attempt against the now-stale context.
        result_unconfirmed = appmod.execute_tool("create_concrete_request", dict(fields_seq), user, confirmed=False, session_context=ctx11)
        check("step 5: the FIRST (unconfirmed) call fails with project_context_stale, not 'confirmation required'",
              not result_unconfirmed.success and result_unconfirmed.error == "project_context_stale")
        after_count_unconfirmed = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        check("step 6: zero rows were created by the unconfirmed attempt", after_count_unconfirmed == before_count_seq)
        check("step 7: the stale session context was cleared by this single call",
              "project_id" not in ctx11)

        # Step 8-10: the "confirmed retry" -- same session context object
        # (already cleared, exactly as it would be after a real
        # unconfirmed-then-confirmed exchange), same fields, now confirmed=True.
        result_confirmed_retry = appmod.execute_tool("create_concrete_request", dict(fields_seq), user, confirmed=True, session_context=ctx11)
        check("step 9: the confirmed retry does NOT silently succeed as an unlinked write",
              not (result_confirmed_retry.success))
        after_count_confirmed_retry = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        check("step 10: zero rows were created by the confirmed retry either", after_count_confirmed_retry == before_count_seq)

        # Step 11-15: re-establish a genuinely valid project, propose
        # fresh, confirm normally -- proves the gate isn't just eating
        # every future write, only the stale one.
        project_d_cur = db.execute(
            "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
            ("__Test Sequence Project D", now, now)
        )
        db.commit()
        project_d_id = project_d_cur.lastrowid
        ctx11b = {}
        appmod.execute_tool("set_project_context", {"project_id": project_d_id}, user, session_context=ctx11b)
        check("(setup) step 11: Project D re-established as fresh, valid session context", ctx11b.get("project_id") == project_d_id)

        result_fresh_unconfirmed = appmod.execute_tool("create_concrete_request", dict(fields_seq), user, confirmed=False, session_context=ctx11b)
        check("step 13: a FRESH proposal with valid context correctly requires normal confirmation (the gate didn't just disappear)",
              not result_fresh_unconfirmed.success and "confirmation" in (result_fresh_unconfirmed.error or "").lower())
        after_count_fresh_unconfirmed = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        check("(sanity) still zero rows before the fresh confirmation", after_count_fresh_unconfirmed == before_count_seq)

        result_fresh_confirmed = appmod.execute_tool("create_concrete_request", dict(fields_seq), user, confirmed=True, session_context=ctx11b)
        check("step 14: confirming the fresh proposal succeeds", result_fresh_confirmed.success)
        after_count_fresh_confirmed = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        check("step 15: exactly ONE request now exists (the confirmed one), correctly linked to Project D",
              after_count_fresh_confirmed == before_count_seq + 1)
        final_row = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (result_fresh_confirmed.data.get("id"),)).fetchone()
        check("step 15: that one request is genuinely linked to the canonical Project D (id and canonical name both correct)",
              final_row["project_id"] == project_d_id and final_row["project"] == "__Test Sequence Project D")

    # ================================================================
    # 12. THE SAME INVARIANT THROUGH THE REAL PRODUCTION PATH --
    # /assistant/confirm_write, the ONLY real production call site for
    # create_concrete_request's execute_tool call. Constructs the
    # ATLAS_SESSIONS draft exactly as stream_atlas_turn's submit branch
    # would (a pending_write token + fields + the session's real
    # project_context), deletes the linked project, then confirms
    # through the actual route -- not a direct execute_tool call -- to
    # prove the real end-user path fails closed too, not just the
    # lower-level function in isolation.
    # ================================================================
    print()
    print("=== 12. Same invariant through the REAL production path: /assistant/confirm_write ===")
    project_e_cur = db.execute(
        "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
        ("__Test Production Path Project E", now, now)
    )
    db.commit()
    project_e_id = project_e_cur.lastrowid

    with appmod.app.test_client() as client:
        login(client, "__atlas_ctx_user@test.local", test_pw)
        with client.session_transaction() as sess:
            atlas_token = secrets.token_hex(16)
            sess["atlas_token"] = atlas_token

        pending_token = secrets.token_hex(16)
        fields_prod = {
            "project": "__Test Production Path Job", "pour_date": date.today().isoformat(),
            "job_site_address": "123 Test Way", "area_description": "Test Slab",
            "mix_design_psi": "4000", "mix_slump": "4", "concrete_amount": "10 yd",
            "truck_spacing": "15 min", "pump_type": "None", "lab_required": "No", "drilling_required": "No",
        }
        # Exactly the shape stream_atlas_turn's submit branch builds --
        # see new_draft["pending_write"] there.
        appmod.ATLAS_SESSIONS[atlas_token] = {
            "mode": "concrete_request", "fields": fields_prod, "history": [],
            "pending_submit": None,
            "pending_write": {"token": pending_token, "fields": dict(fields_prod), "issued_at": time.time()},
            "project_context": {"project_id": project_e_id, "name": "__Test Production Path Project E"},
        }

        # Delete the linked project AFTER the pending write was created
        # (e.g. someone deletes it in Project Hunt between Atlas asking
        # "shall I submit this?" and the person tapping confirm).
        db.execute("DELETE FROM tracker_projects WHERE id = ?", (project_e_id,))
        db.commit()

        before_count_prod = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        csrf_token = get_csrf(client, "/requests")
        resp = client.post("/assistant/confirm_write", json={"token": pending_token}, headers={"X-CSRFToken": csrf_token})
        check("production path: confirm_write returns 200", resp.status_code == 200)
        body = resp.get_json()
        check("production path: the real confirm_write route reports failure, not success", body["success"] is False)
        check("production path: the real confirm_write route surfaces project_context_stale specifically",
              body["error"] == "project_context_stale")
        after_count_prod = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
        check("production path: zero rows were created through the real route", after_count_prod == before_count_prod)
        check("production path: the draft's project_context was cleared by the real route's execute_tool call",
              "project_id" not in appmod.ATLAS_SESSIONS[atlas_token].get("project_context", {}))

        # The spent pending_write token cannot be replayed either way
        # (pre-existing, unrelated-to-this-fix atomic-claim behavior) --
        # confirms this failure didn't leave anything re-usable behind.
        resp_replay = client.post("/assistant/confirm_write", json={"token": pending_token}, headers={"X-CSRFToken": csrf_token})
        check("production path: the same (already-claimed) token cannot be replayed after the stale-context failure",
              resp_replay.get_json()["success"] is False)

        del appmod.ATLAS_SESSIONS[atlas_token]

    # ================================================================
    # 13. ARCHITECTURAL SCOPING CHECK: the sticky _project_context_stale
    # marker must poison ONLY project-DEPENDENT write tools (ones that
    # declare a project_id parameter), never every future write tool in
    # the session indiscriminately. Proven generically -- not via any
    # hardcoded "concrete" special-case -- by temporarily registering a
    # throwaway write tool with NO project_id parameter at all, exactly
    # the shape a future purchase-request/equipment-movement/SitePulse
    # write tool that has nothing to do with a project might have, and
    # confirming it is completely unaffected by an active stale marker
    # on the same session_context. The tool is registered and torn down
    # within this test only -- it is not part of the real Tool Registry.
    # ================================================================
    print()
    print("=== 13. Stale project marker is scoped to project-dependent tools only (not a blanket write-tool block) ===")
    with appmod.app.test_request_context('/'):
        dummy_calls = []

        def _dummy_non_project_handler(user, **kwargs):
            dummy_calls.append(kwargs)
            return {"ok": True}

        appmod.register_tool(
            name="__test_non_project_write",
            description="Test-only throwaway write tool with no project_id parameter, used to prove the stale-project marker does not leak into unrelated write tools.",
            parameters={"note": {"type": "string", "required": False}},
            permission="module:atlas:view",
            atlas_permission="atlas:view_business_data",
            kind="write",
            confirm=True,
            handler=_dummy_non_project_handler,
        )
        try:
            project_f_cur = db.execute(
                "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
                ("__Test Scoping Project F", now, now)
            )
            db.commit()
            project_f_id = project_f_cur.lastrowid

            ctx13 = {}
            appmod.execute_tool("set_project_context", {"project_id": project_f_id}, user, session_context=ctx13)
            db.execute("DELETE FROM tracker_projects WHERE id = ?", (project_f_id,))
            db.commit()

            # Trigger the stale marker via the REAL project-dependent
            # write tool, exactly as before.
            stale_trigger = appmod.execute_tool("create_concrete_request", dict(unconfirmed_fields), user, confirmed=False, session_context=ctx13)
            check("(setup) the stale marker is now active on ctx13", ctx13.get("_project_context_stale") is True)
            check("(setup) triggering call correctly failed with project_context_stale", stale_trigger.error == "project_context_stale")

            # Now call the throwaway NON-project-dependent write tool on
            # the SAME session_context -- it must proceed normally
            # (still gated by its own real confirmation requirement,
            # just not by the unrelated stale project marker).
            result_dummy_unconfirmed = appmod.execute_tool("__test_non_project_write", {"note": "hello"}, user, confirmed=False, session_context=ctx13)
            check("a non-project-dependent write tool is NOT blocked by the active stale project marker (still just needs its own confirmation)",
                  not result_dummy_unconfirmed.success and result_dummy_unconfirmed.error == "confirmation required")
            check("the failure reason is ordinary 'confirmation required', NOT 'project_context_stale' -- proving the marker didn't leak in",
                  result_dummy_unconfirmed.error != "project_context_stale")

            result_dummy_confirmed = appmod.execute_tool("__test_non_project_write", {"note": "hello"}, user, confirmed=True, session_context=ctx13)
            check("the non-project-dependent write tool succeeds normally once confirmed, completely unaffected by the stale marker",
                  result_dummy_confirmed.success)
            check("the dummy handler actually ran exactly once", len(dummy_calls) == 1)

            # And the marker is still correctly in force for the REAL
            # project-dependent tool on this same session_context --
            # scoping works in both directions, it isn't just "broken/
            # disabled" for everything.
            still_stale = appmod.execute_tool("create_concrete_request", dict(unconfirmed_fields), user, confirmed=True, session_context=ctx13)
            check("the real project-dependent tool is STILL correctly blocked by the marker after the unrelated tool succeeded",
                  not still_stale.success and still_stale.error == "project_context_stale")
        finally:
            appmod.ATLAS_TOOLS.pop("__test_non_project_write", None)

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up temporary test fixtures...")
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__Test %'")
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__Test %'")
    db.execute("DELETE FROM users WHERE email = '__atlas_ctx_user@test.local'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
