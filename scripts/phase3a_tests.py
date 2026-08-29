"""
Phase 3A regression tests -- run this after pulling any update to the
permission system.

Uses the real app, the real execute_tool() gateway, and real DB rows --
not mocks. Runs entirely against its own disposable, auto-created
database (see _test_db_setup.isolate_test_database(), called below
before `import app`) -- it is NOT pointed at, and cannot be pointed at,
any existing/company database, including a copy you might otherwise
think to hand it. It creates and cleans up its own temporary users/
records every run and never touches anything outside its own isolated
database file. For migration rehearsal against a real existing
database, use scripts/migration_rehearsal.py instead -- that is a
deliberately separate workflow.

Usage (from the project root):
    APP_ENV=development python3 scripts/phase3a_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from datetime import datetime, date, timedelta
from flask_login import login_user

import _test_db_setup
_test_db_setup.isolate_test_database()  # MUST happen before `import app` -- app.py reads DATA_DIR at import time

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


def main():
    # Plain, independent sqlite3 connection -- not appmod.get_db() -- and
    # no outer Flask app context held across the script (see
    # scripts/security_correction_tests.py's main() docstring for why
    # that combination is unsafe when a test client is involved). This
    # script doesn't use a test client, only test_request_context(), but
    # the same reuse-instead-of-fresh-push behavior applies there too, so
    # it uses the same safe pattern for consistency.
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    today = date.today()

    print("Setting up temporary test fixtures...")
    db.execute("INSERT OR IGNORE INTO users (name,email,password_hash,created_at) VALUES ('__test_admin','__test_admin@test.local','x',?)", (now,))
    db.execute("INSERT OR IGNORE INTO users (name,email,password_hash,created_at) VALUES ('__test_employee','__test_employee@test.local','x',?)", (now,))
    db.commit()

    admin_row = db.execute("SELECT * FROM users WHERE email='__test_admin@test.local'").fetchone()
    emp_row = db.execute("SELECT * FROM users WHERE email='__test_employee@test.local'").fetchone()
    admin_role = db.execute("SELECT id FROM roles WHERE name='Administrator'").fetchone()[0]
    emp_role = db.execute("SELECT id FROM roles WHERE name='Employee'").fetchone()[0]
    db.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (admin_row["id"], admin_role))
    db.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (emp_row["id"], emp_role))
    db.commit()

    admin_user = appmod.User(admin_row)
    emp_user = appmod.User(emp_row)

    print()
    print("=== Permission catalog ===")
    for key in ["action:system_data:manage", "action:activity_log:view", "action:sitepulse:manage_inventory"]:
        row = db.execute("SELECT id FROM permissions WHERE key=?", (key,)).fetchone()
        check(f"permission exists: {key}", row is not None)

    admin_role_id = db.execute("SELECT id FROM roles WHERE name='Administrator'").fetchone()[0]
    for key in ["action:system_data:manage", "action:activity_log:view", "action:sitepulse:manage_inventory"]:
        perm_id = db.execute("SELECT id FROM permissions WHERE key=?", (key,)).fetchone()[0]
        granted = db.execute(
            "SELECT 1 FROM role_permissions WHERE role_id=? AND permission_id=?", (admin_role_id, perm_id)
        ).fetchone()
        check(f"Administrator role has: {key}", granted is not None)

    print()
    print("=== Atlas tool registry ===")
    expected_tools = {
        "create_concrete_request", "get_project_status", "list_bids_needing_attention",
        "list_bids_due_soon", "list_upcoming_concrete_pours", "find_equipment",
        "list_rentals_due", "list_open_purchase_requests", "get_attention_items",
    }
    check("all 9 tools registered", expected_tools.issubset(set(appmod.ATLAS_TOOLS.keys())))

    with appmod.app.test_request_context('/'):
        login_user(admin_user)

        print()
        print("=== get_attention_items: admin sees full set ===")
        r = appmod.execute_tool("get_attention_items", {}, admin_user)
        check("admin get_attention_items succeeds", r.success)

        print()
        print("=== get_attention_items: employee sees only permitted modules ===")
        r2 = appmod.execute_tool("get_attention_items", {}, emp_user)
        # Employee role has no atlas access by default -- expect denial
        check("employee without Atlas override is denied", not r2.success)

        print()
        print("=== find_equipment: enum validation rejects bad status ===")
        r3 = appmod.execute_tool("find_equipment", {"status": "NotARealStatus"}, admin_user)
        check("invalid enum value rejected", not r3.success and "invalid value" in (r3.error or ""))

        print()
        print("=== get_project_status: handles missing project safely ===")
        r4 = appmod.execute_tool("get_project_status", {"project_name": "__no_such_project__"}, admin_user)
        check("missing project returns found=False, not an error", r4.success and r4.data.get("found") is False)

        print()
        print("=== unknown tool name is rejected, not silently ignored ===")
        r5 = appmod.execute_tool("delete_everything", {}, admin_user)
        check("unknown tool rejected", not r5.success and "unknown tool" in (r5.error or ""))

    print()
    print("=== is_admin() deliberately NOT migrated in Phase 3A ===")
    with appmod.app.test_request_context('/'):
        login_user(admin_user)
        # admin_user has the Administrator ROLE but its email is not in the
        # legacy ADMIN_EMAILS list -- is_admin() must still be pure legacy
        # email-list logic (untouched), so this is correctly False. This
        # proves is_admin() truly wasn't touched, not a bug.
        check(
            "is_admin() unaffected by new role (still pure legacy list, as intended)",
            appmod.is_admin() is False
        )

    print()
    print("=== Legacy migration: new-permission-only user gets access via new system alone ===")
    perm_only_row = db.execute("SELECT * FROM users WHERE email='__test_employee@test.local'").fetchone()
    perm_only_user = appmod.User(perm_only_row)
    pid_atlas_view = db.execute("SELECT id FROM permissions WHERE key='module:atlas:view'").fetchone()[0]
    pid_atlas_biz = db.execute("SELECT id FROM permissions WHERE key='atlas:view_business_data'").fetchone()[0]
    for pid in (pid_atlas_view, pid_atlas_biz):
        db.execute(
            "INSERT OR IGNORE INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
            (perm_only_row["id"], pid, "grant", "phase3a_tests", now)
        )
    db.commit()
    with appmod.app.test_request_context('/'):
        login_user(perm_only_user)
        check(
            "is_atlas_allowed() True via new system alone (not in legacy ATLAS_ACCESS_EMAILS)",
            appmod.is_atlas_allowed() and perm_only_row["email"] not in appmod.ATLAS_ACCESS_EMAILS
        )

    db.commit()

    # Exact fixture cleanup must run BEFORE the orphan assertion --
    # otherwise a bug in this very cleanup step could create an orphan
    # that the assertion, running earlier, would never see (CTO finding).
    print()
    print("Cleaning up temporary test fixtures...")
    for email in ("__test_admin@test.local", "__test_employee@test.local"):
        row = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            uid = row[0]
            db.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
            db.execute("DELETE FROM user_permission_overrides WHERE user_id=?", (uid,))
            db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()

    # ORDERING MATTERS (CTO finding): the orphan assertion runs against
    # the state left by the exact fixture cleanup directly above --
    # BEFORE any broad safety-net cleanup can hide a defect, and AFTER
    # every normal destructive cleanup step this suite performs (so a
    # bug in that cleanup itself would also be caught, not just orphans
    # created earlier in the run). Only after the assertion has been
    # recorded does the narrowly-scoped emergency net run.
    orphans = hygiene.assert_no_orphan_privilege_rows(db)
    for o in orphans:
        FAIL.append(f"DB hygiene: {o}")
        print(f"FAIL  DB hygiene: {o}")
    if not orphans:
        check("no orphan user_roles/user_permission_overrides/role_permissions rows remain", True)
    hygiene.emergency_cleanup_orphans(db)

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")

    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    # Deliberately NOT wrapped in "with appmod.app.app_context():" -- see
    # main()'s comment.
    main()
