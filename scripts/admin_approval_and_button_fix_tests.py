"""
Surgical fix regression -- v3:

FIX 1: Administrator must inherit action:product_intelligence:approve_requests
  via the existing backfill architecture (works on pre-existing databases,
  not just fresh ones), while preserving self-approval protection,
  self-role-modification protection, explicit DENY override, and every
  other v2 approval/concurrency guarantee.

FIX 2: Project Hunt's "Save Changes" button (edit_project.html) uses the
  real BuildIQ primary-button classes instead of a bare, unstyled
  btn-gold-only element.

Uses the real Flask app/routes/DB, same pattern as the other suites in
this directory.

Usage (from the project root):
    APP_ENV=development python3 scripts/admin_approval_and_button_fix_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import datetime

import _test_db_setup
_test_db_setup.isolate_test_database()

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


def make_plain_user(db, email, name, now, pw_hash):
    """A user with NO roles and NO permission overrides -- the baseline
    for proving normal employees don't receive the approval permission
    as a side effect of this fix."""
    db.execute("DELETE FROM users WHERE email=?", (email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", (name, email, pw_hash, now))
    db.commit()
    return db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]


def create_request(db, requester_email, requester_name, text, now):
    cur = db.execute(
        "INSERT INTO feature_requests (requester_email, requester_name, department, original_request, status, approval_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (requester_email, requester_name, "Ops", text, "Submitted", "Pending", now, now)
    )
    db.commit()
    return cur.lastrowid


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    # ================================================================
    # FIX 1: Administrator inherits approve_requests
    # ================================================================
    print("=== Fix 1: Administrator role permission set ===")

    admin_role_id = db.execute("SELECT id FROM roles WHERE name = 'Administrator'").fetchone()[0]
    approve_perm_id = db.execute("SELECT id FROM permissions WHERE key = 'action:product_intelligence:approve_requests'").fetchone()[0]
    view_perm_id = db.execute("SELECT id FROM permissions WHERE key = 'module:product_intelligence:view'").fetchone()[0]

    check("Administrator role has action:product_intelligence:approve_requests in role_permissions",
          db.execute("SELECT 1 FROM role_permissions WHERE role_id=? AND permission_id=?", (admin_role_id, approve_perm_id)).fetchone() is not None)

    procurement_role_id = db.execute("SELECT id FROM roles WHERE name = 'Procurement'").fetchone()[0]
    check("Procurement role STILL has action:product_intelligence:approve_requests (unaffected by the Administrator fix)",
          db.execute("SELECT 1 FROM role_permissions WHERE role_id=? AND permission_id=?", (procurement_role_id, approve_perm_id)).fetchone() is not None)
    check("Procurement role still has module:product_intelligence:view",
          db.execute("SELECT 1 FROM role_permissions WHERE role_id=? AND permission_id=?", (procurement_role_id, view_perm_id)).fetchone() is not None)

    # ================================================================
    # Simulate a PRE-EXISTING database: manually strip the approve
    # permission from the Administrator role (as if this key never
    # existed when the role was first seeded), then re-run the real
    # startup/migration path and confirm the backfill actually restores
    # it -- proving requirement 4 ("must work for EXISTING databases",
    # not just a fresh one).
    # ================================================================
    print()
    print("=== Fix 1: existing (pre-fix) database is correctly backfilled on normal startup ===")
    db.execute("DELETE FROM role_permissions WHERE role_id=? AND permission_id=?", (admin_role_id, approve_perm_id))
    db.commit()
    check("(setup) simulated pre-existing DB: Administrator role no longer has the permission",
          db.execute("SELECT 1 FROM role_permissions WHERE role_id=? AND permission_id=?", (admin_role_id, approve_perm_id)).fetchone() is None)

    appmod.init_db()  # the real startup/migration path -- not a test-only shortcut

    check("after a normal init_db() run, Administrator role has the permission restored (idempotent backfill, not a fresh-DB-only fix)",
          db.execute("SELECT 1 FROM role_permissions WHERE role_id=? AND permission_id=?", (admin_role_id, approve_perm_id)).fetchone() is not None)

    # Running it again must not duplicate or error.
    appmod.init_db()
    count_after_second_run = db.execute(
        "SELECT COUNT(*) FROM role_permissions WHERE role_id=? AND permission_id=?", (admin_role_id, approve_perm_id)
    ).fetchone()[0]
    check("running the backfill a second time does not create a duplicate grant (idempotent)", count_after_second_run == 1)

    # ================================================================
    # Normal employees do NOT receive this permission as a side effect.
    # ================================================================
    print()
    print("=== Fix 1: normal employees are unaffected ===")
    plain_email = "__admin_fix_plain@test.local"
    plain_uid = make_plain_user(db, plain_email, "__admin_fix_plain", now, pw_hash)
    plain_user_obj = appmod.User(db.execute("SELECT * FROM users WHERE id=?", (plain_uid,)).fetchone())
    with appmod.app.test_request_context('/'):
        check("a user with no role/no overrides does NOT have the approval permission",
              not appmod.user_has_permission(plain_user_obj, "action:product_intelligence:approve_requests"))

    # ================================================================
    # Administrator user (real role assignment, real login) can see and
    # use approve/return controls for ANOTHER user's request, but not
    # their own, and self-role/self-permission editing remains blocked.
    # ================================================================
    print()
    print("=== Fix 1: Administrator can approve another user's request, not their own ===")
    admin_email = "__admin_fix_admin@test.local"
    db.execute("DELETE FROM users WHERE email=?", (admin_email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", ("__admin_fix_admin", admin_email, pw_hash, now))
    db.commit()
    admin_uid = db.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (admin_uid, admin_role_id))
    db.commit()

    other_req_id = create_request(db, "__admin_fix_other@test.local", "Other Person", "Please review this", now)
    admin_own_req_id = create_request(db, admin_email, "__admin_fix_admin", "Admin's own request", now)

    with appmod.app.test_client() as client:
        login(client, admin_email, pw)

        other_detail_url = f"/admin/product-intelligence/{other_req_id}"
        html = client.get(other_detail_url).get_data(as_text=True)
        check("Administrator sees Approve/Return controls for ANOTHER user's Pending request",
              'value="approve_request"' in html and 'value="return_request"' in html)

        token = get_csrf(client, other_detail_url)
        resp = client.post(other_detail_url, data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)
        r_other = db.execute("SELECT approval_status, approval_decided_by FROM feature_requests WHERE id=?", (other_req_id,)).fetchone()
        check("Administrator successfully approves another user's request", r_other["approval_status"] == "Approved")
        check("approval_decided_by correctly records the Administrator", r_other["approval_decided_by"] == admin_email)

        own_detail_url = f"/admin/product-intelligence/{admin_own_req_id}"
        own_html = client.get(own_detail_url).get_data(as_text=True)
        check("Administrator does NOT see Approve/Return controls on their OWN request",
              'value="approve_request"' not in own_html)

        token2 = get_csrf(client, own_detail_url)
        resp2 = client.post(own_detail_url, data={"csrf_token": token2, "action": "approve_request", "reason": ""}, follow_redirects=True)
        r_own = db.execute("SELECT approval_status FROM feature_requests WHERE id=?", (admin_own_req_id,)).fetchone()
        check("a direct POST attempt to self-approve is still rejected server-side even for an Administrator",
              r_own["approval_status"] == "Pending")
        check("the self-approval rejection message is shown", "cannot approve or return your own request" in resp2.get_data(as_text=True))

        # Self-role/self-permission modification protection unaffected.
        # (Real behavior, confirmed in the route: GET is viewable --
        # there's no reason to hide the page -- but any POST attempting
        # to change the acting user's OWN roles/overrides is rejected.
        # This is pre-existing behavior, unrelated to and unweakened by
        # this fix; asserting it here purely as a regression guard.)
        perm_url = f"/admin/users/{admin_uid}/permissions"
        get_resp = client.get(perm_url)
        check("Administrator CAN view their own permissions page (read-only)", get_resp.status_code == 200)
        perm_token = get_csrf(client, perm_url)
        post_resp = client.post(perm_url, data={"csrf_token": perm_token, "action": "assign_role", "role_id": str(admin_role_id)}, follow_redirects=True)
        check("Administrator still cannot MODIFY their own roles/permissions (existing self-modification protection intact)",
              "roles or permissions from this page" in post_resp.get_data(as_text=True))

    # ================================================================
    # Explicit DENY override still beats inherited Administrator permission.
    # ================================================================
    print()
    print("=== Fix 1: explicit DENY overrides inherited Administrator approval permission ===")
    denied_admin_email = "__admin_fix_denied@test.local"
    db.execute("DELETE FROM users WHERE email=?", (denied_admin_email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", ("__admin_fix_denied", denied_admin_email, pw_hash, now))
    db.commit()
    denied_uid = db.execute("SELECT id FROM users WHERE email=?", (denied_admin_email,)).fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (denied_uid, admin_role_id))
    db.commit()
    db.execute(
        "INSERT INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
        (denied_uid, approve_perm_id, "deny", "test_setup", now)
    )
    db.commit()

    denied_user_obj = appmod.User(db.execute("SELECT * FROM users WHERE id=?", (denied_uid,)).fetchone())
    with appmod.app.test_request_context('/'):
        check("an Administrator with an explicit DENY override does NOT have the approval permission, despite the role granting it",
              not appmod.user_has_permission(denied_user_obj, "action:product_intelligence:approve_requests"))

    deny_test_req_id = create_request(db, "__admin_fix_other2@test.local", "Other Person 2", "Another request", now)
    with appmod.app.test_client() as client:
        login(client, denied_admin_email, pw)
        detail_url = f"/admin/product-intelligence/{deny_test_req_id}"
        token = get_csrf(client, detail_url)
        resp = client.post(detail_url, data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)
        r_denied = db.execute("SELECT approval_status FROM feature_requests WHERE id=?", (deny_test_req_id,)).fetchone()
        check("a direct POST from a DENY-overridden Administrator is rejected server-side", r_denied["approval_status"] == "Pending")

    # ================================================================
    # FIX 2: Save Changes button styling
    # ================================================================
    print()
    print("=== Fix 2: Project Hunt Save Changes button styling ===")
    tracker_email = "__admin_fix_tracker@test.local"
    db.execute("DELETE FROM users WHERE email=?", (tracker_email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", ("__admin_fix_tracker", tracker_email, pw_hash, now))
    db.commit()
    tracker_uid = db.execute("SELECT id FROM users WHERE email=?", (tracker_email,)).fetchone()[0]
    ph_manage_pid = db.execute("SELECT id FROM permissions WHERE key='action:project_hunt:manage'").fetchone()[0]
    ph_view_pid = db.execute("SELECT id FROM permissions WHERE key='module:project_hunt:view'").fetchone()[0]
    now2 = datetime.utcnow().isoformat()
    for pid in (ph_manage_pid, ph_view_pid):
        db.execute("INSERT INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
                   (tracker_uid, pid, "grant", "test_setup", now2))
    db.commit()

    proj_cur = db.execute(
        "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
        ("__Admin Fix Test Project", now, now)
    )
    db.commit()
    proj_id = proj_cur.lastrowid

    with appmod.app.test_client() as client:
        login(client, tracker_email, pw)
        html = client.get(f"/tracker/project/{proj_id}/edit").get_data(as_text=True)
        check("Save Changes button uses the real btn + btn-gold classes (was bare, unstyled btn-gold only)",
              'class="btn btn-gold">Save Changes' in html)
        check("no stray unstyled btn-gold-only Save Changes button remains", 'class="btn-gold">Save Changes' not in html)
        # Confirm the fix is styling-only: route/behavior unchanged.
        check("Delete Project Permanently styling from the prior fix is still intact (not touched by this change)",
              'class="btn btn-danger">Delete Project Permanently' in html)
        token = get_csrf(client, f"/tracker/project/{proj_id}/edit")
        resp = client.post(f"/tracker/project/{proj_id}/edit", data={
            "csrf_token": token, "name": "__Admin Fix Test Project Renamed", "status": "In Progress",
        }, follow_redirects=True)
        renamed = db.execute("SELECT name FROM tracker_projects WHERE id=?", (proj_id,)).fetchone()
        check("Save Changes form submission/behavior is unaffected by the styling fix (still actually saves)",
              renamed["name"] == "__Admin Fix Test Project Renamed")

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM feature_request_approvals WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__admin_fix_%')")
    db.execute("DELETE FROM feature_request_status_history WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__admin_fix_%')")
    db.execute("DELETE FROM feature_requests WHERE requester_email LIKE '__admin_fix_%'")
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__Admin Fix%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
