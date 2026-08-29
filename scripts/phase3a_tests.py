"""
Phase 3A regression tests -- run this after pulling the update, and
again before ever removing the legacy-email OR-fallback from
is_whatsapp_admin() / is_atlas_allowed() / is_procurement().

Uses the real app, the real execute_tool() gateway, and real DB rows --
not mocks. Safe to run against a copy of the database; it creates a
few temporary users/records and does not modify anything that already
existed (it never deletes or updates an existing row).

Usage (from the project root):
    python3 scripts/phase3a_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date, timedelta
from flask_login import login_user

import app as appmod

PASS = []
FAIL = []


def check(label, condition):
    if condition:
        PASS.append(label)
    else:
        FAIL.append(label)
    print(("  OK  " if condition else "FAIL  ") + label)


def main():
    db = appmod.get_db()
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

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")

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

    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    with appmod.app.app_context():
        main()
