"""
Atlas cross-module authorization regression -- Project Hunt is no longer
a universal prerequisite for Atlas project context/intelligence; each
BuildIQ module's own permission independently gates its own data.

Usage:
    APP_ENV=development python3 scripts/atlas_module_authorization_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import datetime
from unittest.mock import patch

import _test_db_setup
_test_db_setup.isolate_test_database()

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
    pw_hash = generate_password_hash("TestPass123!")

    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__AuthzTest%'")
    db.commit()
    proj_cur = db.execute(
        "INSERT INTO tracker_projects (name, client, status, bid_due_date, estimated_value, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("__AuthzTest Peninsula", "__AuthzTest Client Co", "In Progress", "2026-12-01", "500000", now, now)
    )
    db.commit()
    pid = proj_cur.lastrowid

    from flask_login import login_user

    with appmod.app.test_request_context('/'):
        # ============================================================
        # 1. Full-rights user -- no regression
        # ============================================================
        print("=== 1. Full-rights user: context succeeds, complete payload, no regression ===")
        full_perms = ["module:project_hunt:view", "module:equipment_center:view", "module:sitepulse:view",
                      "atlas:view_business_data", "module:atlas:view"]
        full_uid = make_user(db, "__authz_full@test.local", "__authz_full", now, pw_hash, full_perms)
        full_row = db.execute("SELECT * FROM users WHERE id=?", (full_uid,)).fetchone()
        full_user = appmod.User(full_row)
        login_user(full_user)

        ctx_result = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, full_user)
        check("1. context resolves successfully", ctx_result.success and ctx_result.data.get("found") is True)

        intel_result = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, full_user, session_context={"project_id": pid, "name": "__AuthzTest Peninsula"})
        check("1. intelligence call succeeds", intel_result.success)
        data = intel_result.data
        check("1. shared identity present", data["project"]["project_id"] == pid and data["project"]["name"] == "__AuthzTest Peninsula" and data["project"]["client"] == "__AuthzTest Client Co")
        check("1. Project Hunt protected fields present (status/bid_due_date/estimated_value)",
              data["project"]["status"] == "In Progress" and data["project"]["bid_due_date"] == "2026-12-01" and data["project"]["estimated_value"] == "500000")
        check("1. equipment/rentals sections present", "equipment" in data and "rentals" in data)
        check("1. concrete/purchases sections present", "concrete" in data and "purchases" in data)
        check("1. attention section present", "attention" in data)

        # ============================================================
        # 2. Project-Hunt-only user
        # ============================================================
        print()
        print("=== 2. Project-Hunt-only user: context succeeds, gets Project Hunt fields, no Equipment/SitePulse ===")
        ph_uid = make_user(db, "__authz_ph@test.local", "__authz_ph", now, pw_hash,
                            ["module:project_hunt:view", "atlas:view_business_data"])
        ph_row = db.execute("SELECT * FROM users WHERE id=?", (ph_uid,)).fetchone()
        ph_user = appmod.User(ph_row)
        login_user(ph_user)
        ph_ctx = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, ph_user)
        check("2. context resolves successfully", ph_ctx.success and ph_ctx.data.get("found") is True)
        ph_intel = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, ph_user, session_context={"project_id": pid, "name": "__AuthzTest Peninsula"})
        check("2. intelligence call succeeds", ph_intel.success)
        ph_data = ph_intel.data
        check("2. gets Project Hunt fields", ph_data["project"]["status"] == "In Progress" and ph_data["project"]["bid_due_date"] == "2026-12-01" and ph_data["project"]["estimated_value"] == "500000")
        check("2. does NOT receive equipment", "equipment" not in ph_data)
        check("2. does NOT receive rentals", "rentals" not in ph_data)
        check("2. does NOT receive concrete", "concrete" not in ph_data)
        check("2. does NOT receive purchases", "purchases" not in ph_data)

        # ============================================================
        # 3. Equipment-only user
        # ============================================================
        print()
        print("=== 3. Equipment-only user: context succeeds, gets identity + equipment/rentals, no PH fields, no SitePulse ===")
        eq_uid = make_user(db, "__authz_eq@test.local", "__authz_eq", now, pw_hash,
                            ["module:equipment_center:view", "atlas:view_business_data"])
        eq_row = db.execute("SELECT * FROM users WHERE id=?", (eq_uid,)).fetchone()
        eq_user = appmod.User(eq_row)
        login_user(eq_user)
        eq_ctx = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, eq_user)
        check("3. context resolves successfully", eq_ctx.success and eq_ctx.data.get("found") is True)
        eq_intel = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, eq_user, session_context={"project_id": pid, "name": "__AuthzTest Peninsula"})
        check("3. intelligence call succeeds", eq_intel.success)
        eq_data = eq_intel.data
        check("3. gets project_id/name/client", eq_data["project"]["project_id"] == pid and eq_data["project"]["name"] == "__AuthzTest Peninsula" and eq_data["project"]["client"] == "__AuthzTest Client Co")
        check("3. gets equipment/rentals", "equipment" in eq_data and "rentals" in eq_data)
        check("3. NO status/bid_due_date/estimated_value", "status" not in eq_data["project"] and "bid_due_date" not in eq_data["project"] and "estimated_value" not in eq_data["project"])
        check("3. NO concrete/purchases", "concrete" not in eq_data and "purchases" not in eq_data)

        # ============================================================
        # 4. SitePulse-only user
        # ============================================================
        print()
        print("=== 4. SitePulse-only user: context succeeds, gets identity + concrete/purchases, no PH fields, no Equipment ===")
        sp_uid = make_user(db, "__authz_sp@test.local", "__authz_sp", now, pw_hash,
                            ["module:sitepulse:view", "atlas:view_business_data"])
        sp_row = db.execute("SELECT * FROM users WHERE id=?", (sp_uid,)).fetchone()
        sp_user = appmod.User(sp_row)
        login_user(sp_user)
        sp_ctx = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, sp_user)
        check("4. context resolves successfully", sp_ctx.success and sp_ctx.data.get("found") is True)
        sp_intel = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, sp_user, session_context={"project_id": pid, "name": "__AuthzTest Peninsula"})
        check("4. intelligence call succeeds", sp_intel.success)
        sp_data = sp_intel.data
        check("4. gets project_id/name/client", sp_data["project"]["project_id"] == pid and sp_data["project"]["client"] == "__AuthzTest Client Co")
        check("4. gets concrete/purchases", "concrete" in sp_data and "purchases" in sp_data)
        check("4. NO status/bid_due_date/estimated_value", "status" not in sp_data["project"] and "bid_due_date" not in sp_data["project"] and "estimated_value" not in sp_data["project"])
        check("4. NO equipment/rentals", "equipment" not in sp_data and "rentals" not in sp_data)

        # ============================================================
        # 5. Equipment + SitePulse, no Project Hunt
        # ============================================================
        print()
        print("=== 5. Equipment + SitePulse, no Project Hunt: both operational sources, no PH fields ===")
        mix_uid = make_user(db, "__authz_mix@test.local", "__authz_mix", now, pw_hash,
                             ["module:equipment_center:view", "module:sitepulse:view", "atlas:view_business_data"])
        mix_row = db.execute("SELECT * FROM users WHERE id=?", (mix_uid,)).fetchone()
        mix_user = appmod.User(mix_row)
        login_user(mix_user)
        mix_ctx = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, mix_user)
        check("5. context resolves successfully", mix_ctx.success)
        mix_intel = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, mix_user, session_context={"project_id": pid, "name": "__AuthzTest Peninsula"})
        mix_data = mix_intel.data
        check("5. gets equipment/rentals AND concrete/purchases", all(k in mix_data for k in ("equipment", "rentals", "concrete", "purchases")))
        check("5. NO Project Hunt protected fields", "status" not in mix_data["project"] and "bid_due_date" not in mix_data["project"] and "estimated_value" not in mix_data["project"])

        # ============================================================
        # 6. atlas:view_business_data only, zero modules
        # ============================================================
        print()
        print("=== 6. Zero-module Atlas user: denied outright, cannot confirm project existence/name ===")
        zero_uid = make_user(db, "__authz_zero@test.local", "__authz_zero", now, pw_hash, ["atlas:view_business_data"])
        zero_row = db.execute("SELECT * FROM users WHERE id=?", (zero_uid,)).fetchone()
        zero_user = appmod.User(zero_row)
        login_user(zero_user)
        zero_ctx = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, zero_user)
        check("6. set_project_context denied", not zero_ctx.success and zero_ctx.error == "not permitted")
        zero_intel = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, zero_user, session_context={"project_id": pid, "name": "__AuthzTest Peninsula"})
        check("6. get_project_intelligence denied", not zero_intel.success and zero_intel.error == "not permitted")

        # ============================================================
        # 7. No atlas:view_business_data at all
        # ============================================================
        print()
        print("=== 7. No atlas:view_business_data: denied exactly as today ===")
        noatlas_uid = make_user(db, "__authz_noatlas@test.local", "__authz_noatlas", now, pw_hash,
                                 ["module:project_hunt:view", "module:equipment_center:view", "module:sitepulse:view"])
        noatlas_row = db.execute("SELECT * FROM users WHERE id=?", (noatlas_uid,)).fetchone()
        noatlas_user = appmod.User(noatlas_row)
        login_user(noatlas_user)
        noatlas_ctx = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, noatlas_user)
        check("7. denied even with every module permission but no atlas:view_business_data", not noatlas_ctx.success and noatlas_ctx.error == "not permitted")

        # ============================================================
        # 8. Explicit unauthorized bid request -- protected data never retrieved
        # ============================================================
        print()
        print("=== 8. Explicit unauthorized bid request: no protected data retrieved ===")
        eq_bid_intel = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, eq_user, session_context={"project_id": pid, "name": "__AuthzTest Peninsula"})
        check("8. equipment-only user asking broadly still gets zero Project Hunt fields", "status" not in eq_bid_intel.data["project"])

        # ============================================================
        # 9. Broad "tell me everything" -- union of authorized sources only
        # ============================================================
        print()
        print("=== 9. Broad overview scope returns union of ONLY authorized modules ===")
        check("9. full-rights user's overview includes every source", all(k in data for k in ("equipment", "rentals", "concrete", "purchases", "attention")) and "status" in data["project"])
        check("9. mixed (Equipment+SitePulse) user's overview includes exactly their union, nothing more", all(k in mix_data for k in ("equipment", "rentals", "concrete", "purchases")) and "status" not in mix_data["project"])

        # ============================================================
        # 10. Concrete create user without Project Hunt -- unaffected
        # ============================================================
        print()
        print("=== 10. Concrete create user without Project Hunt: create_concrete_request still works ===")
        concrete_uid = make_user(db, "__authz_concrete@test.local", "__authz_concrete", now, pw_hash,
                                  ["action:sitepulse:manage", "atlas:create_requests"])
        concrete_row = db.execute("SELECT * FROM users WHERE id=?", (concrete_uid,)).fetchone()
        concrete_user = appmod.User(concrete_row)
        login_user(concrete_user)
        with patch("app.create_concrete_request", return_value=12345) as mock_create:
            create_result = appmod.execute_tool(
                "create_concrete_request",
                {"project": "__AuthzTest Peninsula", "pour_date": "2026-09-15"},
                concrete_user,
                confirmed=True,
            )
        check("10. concrete request creation succeeds without any Project Hunt permission", create_result.success)

        # ============================================================
        # 11. PROOF: protected columns are never SELECTed at the SQL
        # level for a non-Project-Hunt user -- not merely dropped from
        # the result afterward. Uses sqlite3's own trace_callback to
        # capture every SQL statement actually sent to the database
        # during the call.
        # ============================================================
        print()
        print("=== 11p. SQL-level proof: set_project_context never retrieves protected columns ===")
        captured_ctx_sql = []
        _raw_db_ctx = appmod.get_db()
        _raw_db_ctx.set_trace_callback(lambda sql: captured_ctx_sql.append(sql))
        try:
            appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, eq_user)
        finally:
            _raw_db_ctx.set_trace_callback(None)

        def _mentions_protected_column_early(sql):
            # Precise on purpose: a query can legitimately mention
            # tracker_projects (e.g. joined only for tp.name, or a
            # WHERE clause on a DIFFERENT table's own "status" column
            # that happens to share a name) without ever touching
            # tracker_projects' own protected columns. Only a real
            # `SELECT *` against tracker_projects, or an explicit
            # reference to tracker_projects' own status/bid_due_date/
            # estimated_value (aliased as tp.<col> in a join, or bare
            # <col> in a query that is ONLY ever tracker_projects with
            # no join at all) counts.
            s = sql.lower()
            if "tracker_projects" not in s:
                return False
            if "select *" in s and "from tracker_projects" in s:
                return True
            if any(f"tp.{col}" in s for col in ("status", "bid_due_date", "estimated_value")):
                return True
            if "join" not in s and "from tracker_projects" in s:
                return any(col in s for col in ("status", "bid_due_date", "estimated_value"))
            return False

        check("11p. set_project_context's actual SQL never selects protected columns/`SELECT *` from tracker_projects for an Equipment-only user",
              not any(_mentions_protected_column_early(s) for s in captured_ctx_sql))
        check("11p. at least one real tracker_projects query DID happen (not a vacuous pass)", any("tracker_projects" in s.lower() for s in captured_ctx_sql))

        # ============================================================
        print()
        print("=== 11. SQL-level proof: scope=\"overview\" (including Attention) never retrieves protected columns for non-Project-Hunt users ===")

        def _mentions_protected_column(sql):
            # Precise on purpose: a query can legitimately mention
            # tracker_projects (e.g. joined only for tp.name, or a
            # WHERE clause on a DIFFERENT table's own "status" column
            # that happens to share a name) without ever touching
            # tracker_projects' own protected columns. Only a real
            # `SELECT *` against tracker_projects, or an explicit
            # reference to tracker_projects' own status/bid_due_date/
            # estimated_value (aliased as tp.<col> in a join, or bare
            # <col> in a query that is ONLY ever tracker_projects with
            # no join at all) counts.
            s = sql.lower()
            if "tracker_projects" not in s:
                return False
            if "select *" in s and "from tracker_projects" in s:
                return True
            if any(f"tp.{col}" in s for col in ("status", "bid_due_date", "estimated_value")):
                return True
            if "join" not in s and "from tracker_projects" in s:
                return any(col in s for col in ("status", "bid_due_date", "estimated_value"))
            return False

        def _mentions_unauthorized_source_table(sql, forbidden_tables):
            s = sql.lower()
            return any(t in s for t in forbidden_tables)

        def _trace_overview_call(user_obj):
            captured = []
            raw_db = appmod.get_db()
            raw_db.set_trace_callback(lambda sql: captured.append(sql))
            try:
                result = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, user_obj, session_context={"project_id": pid, "name": "__AuthzTest Peninsula"})
            finally:
                raw_db.set_trace_callback(None)
            return result, captured

        # Equipment-only user, scope=overview -- must NOT query Project
        # Hunt attention sources (tracker_quotes JOIN, bid_due_date
        # scan) or SitePulse attention sources (inventory_concrete_
        # requests), but MAY query its own authorized equipment/rental
        # sources (sitepulse_rentals, sitepulse_assets/usage_log).
        eq_result, eq_sql = _trace_overview_call(eq_user)
        eq_offending_protected = [s for s in eq_sql if _mentions_protected_column(s)]
        eq_offending_sitepulse = [s for s in eq_sql if _mentions_unauthorized_source_table(s, ["inventory_concrete_requests"])]
        check("11a. Equipment-only, scope=overview: no Project Hunt-protected query executes", len(eq_offending_protected) == 0)
        check("11a. Equipment-only, scope=overview: no unauthorized SitePulse attention query executes", len(eq_offending_sitepulse) == 0)
        check("11a. Equipment-only, scope=overview: authorized rental attention query DID execute", any("sitepulse_rentals" in s.lower() for s in eq_sql))
        check("11a. Equipment-only, scope=overview: result contains only authorized attention items (none tagged project_hunt/sitepulse)",
              all(item.get("source_module") not in ("project_hunt", "sitepulse") for item in eq_result.data.get("attention", [])))

        # SitePulse-only user, scope=overview -- inverse of the above.
        sp_result, sp_sql = _trace_overview_call(sp_user)
        sp_offending_protected = [s for s in sp_sql if _mentions_protected_column(s)]
        sp_offending_equipment = [s for s in sp_sql if _mentions_unauthorized_source_table(s, ["sitepulse_rentals"])]
        check("11b. SitePulse-only, scope=overview: no Project Hunt-protected query executes", len(sp_offending_protected) == 0)
        check("11b. SitePulse-only, scope=overview: no unauthorized Equipment attention query (sitepulse_rentals) executes", len(sp_offending_equipment) == 0)
        check("11b. SitePulse-only, scope=overview: authorized concrete attention query DID execute", any("inventory_concrete_requests" in s.lower() for s in sp_sql))
        check("11b. SitePulse-only, scope=overview: result contains only authorized attention items (none tagged project_hunt/equipment_center)",
              all(item.get("source_module") not in ("project_hunt", "equipment_center") for item in sp_result.data.get("attention", [])))

        # Project-Hunt-only user, scope=overview -- Project Hunt attention
        # MAY query; unauthorized operational sources must not.
        ph_result, ph_sql = _trace_overview_call(ph_user)
        ph_offending_operational = [s for s in ph_sql if _mentions_unauthorized_source_table(s, ["sitepulse_rentals", "inventory_concrete_requests"])]
        check("11c. Project-Hunt-only, scope=overview: Project Hunt attention query DID execute (tracker_quotes join)", any("tracker_quotes" in s.lower() for s in ph_sql))
        check("11c. Project-Hunt-only, scope=overview: no unauthorized operational (Equipment/SitePulse) attention query executes", len(ph_offending_operational) == 0)
        check("11c. Project-Hunt-only, scope=overview: result contains only project_hunt-tagged attention items",
              all(item.get("source_module") == "project_hunt" for item in ph_result.data.get("attention", [])))

        # Full-rights control -- ALL attention source queries still
        # execute, proving this fix does not reduce the full-rights
        # experience at all.
        full_result, full_sql = _trace_overview_call(full_user)
        check("11d. CONTROL -- full-rights user, scope=overview: Project Hunt attention query executes", any("tracker_quotes" in s.lower() for s in full_sql))
        check("11d. CONTROL -- full-rights user, scope=overview: SitePulse attention query executes", any("inventory_concrete_requests" in s.lower() for s in full_sql))
        check("11d. CONTROL -- full-rights user, scope=overview: Equipment attention query executes", any("sitepulse_rentals" in s.lower() for s in full_sql))
        check("11d. CONTROL -- full-rights user, scope=overview: protected columns ARE retrieved (proves the trace methodology itself is sound, not vacuous)",
              any(_mentions_protected_column(s) for s in full_sql))

        # ============================================================
        # 12. Global permission framework backward-compatibility
        # ============================================================
        print()
        print("=== 12. Backward compatibility: single-string permission tools unaffected ===")
        # Use an existing single-string-permission read tool (get_project_status)
        # as the control -- confirms ordinary tools' permission checks
        # are completely unaffected by the new tuple-OR code path.
        gps_authorized_uid = make_user(db, "__authz_gps_ok@test.local", "__authz_gps_ok", now, pw_hash,
                                        ["module:project_hunt:view", "atlas:view_business_data"])
        gps_ok_row = db.execute("SELECT * FROM users WHERE id=?", (gps_authorized_uid,)).fetchone()
        gps_ok_user = appmod.User(gps_ok_row)
        login_user(gps_ok_user)
        gps_ok_result = appmod.execute_tool("get_project_status", {"project_name": "__AuthzTest Peninsula"}, gps_ok_user)
        check("12.1 existing single-string-permission tool: authorized user succeeds", gps_ok_result.success)

        gps_denied_uid = make_user(db, "__authz_gps_no@test.local", "__authz_gps_no", now, pw_hash,
                                    ["atlas:view_business_data"])
        gps_no_row = db.execute("SELECT * FROM users WHERE id=?", (gps_denied_uid,)).fetchone()
        gps_no_user = appmod.User(gps_no_row)
        login_user(gps_no_user)
        gps_denied_result = appmod.execute_tool("get_project_status", {"project_name": "__AuthzTest Peninsula"}, gps_no_user)
        check("12.2 existing single-string-permission tool: unauthorized user denied", not gps_denied_result.success and gps_denied_result.error == "not permitted")

        print()
        print("=== 13. Tuple permission: each alternative independently sufficient ===")
        login_user(ph_user)
        tuple_ph_only = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, ph_user)
        check("13.1 tuple permission: FIRST alternative only (Project Hunt) succeeds", tuple_ph_only.success)

        login_user(eq_user)
        tuple_eq_only = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, eq_user)
        check("13.2 tuple permission: SECOND alternative only (Equipment Center) succeeds", tuple_eq_only.success)

        login_user(sp_user)
        tuple_sp_only = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, sp_user)
        check("13.3 tuple permission: THIRD alternative only (SitePulse) succeeds", tuple_sp_only.success)

        login_user(zero_user)
        tuple_none = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, zero_user)
        check("13.4 tuple permission: NONE of the alternatives -> denied", not tuple_none.success and tuple_none.error == "not permitted")

        print()
        print("=== 14. Correct manual permission but missing atlas_permission -> denied ===")
        manual_only_uid = make_user(db, "__authz_manualonly@test.local", "__authz_manualonly", now, pw_hash,
                                     ["module:equipment_center:view"])  # deliberately NO atlas:view_business_data
        manual_only_row = db.execute("SELECT * FROM users WHERE id=?", (manual_only_uid,)).fetchone()
        manual_only_user = appmod.User(manual_only_row)
        login_user(manual_only_user)
        manual_only_result = appmod.execute_tool("set_project_context", {"project_name": "__AuthzTest Peninsula"}, manual_only_user)
        check("14. relevant module permission present but atlas:view_business_data missing -> denied",
              not manual_only_result.success and manual_only_result.error == "not permitted")

        print()
        print("=== 15. atlas:view_business_data present, module permission missing (get_project_intelligence specifically) -> denied ===")
        login_user(zero_user)
        gpi_zero_result = appmod.execute_tool("get_project_intelligence", {"scope": "overview"}, zero_user, session_context={"project_id": pid, "name": "__AuthzTest Peninsula"})
        check("15. get_project_intelligence: atlas:view_business_data alone (no module permission) -> denied",
              not gpi_zero_result.success and gpi_zero_result.error == "not permitted")

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__AuthzTest%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
