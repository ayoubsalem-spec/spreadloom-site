"""
Security Correction regression tests -- valid-CSRF POST authorization
bypass attempts, with database state captured before and after each
denied mutation to prove zero side effects. Also proves the specific
mandatory case: a legacy-admin-list member with an explicit DENY is
denied, and that legacy list membership no longer independently grants
runtime authorization anywhere.

Uses the real app, real routes, a real Flask test client with genuine
CSRF tokens fetched from real form pages (not skipped/mocked) -- these
requests reach the actual permission-check code, not just CSRF
middleware.

Usage (from the project root):
    APP_ENV=development python3 scripts/security_correction_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import datetime

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


def get_csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1)


def login(client, email, password):
    """Logs the given client in via the REAL production login route --
    GET /login for a CSRF token, then POST valid credentials with that
    token, exactly like a real browser. Returns the CSRF token, which
    stays valid for subsequent POSTs on this session (Flask-WTF ties it
    to the session secret, not to authentication state).

    ROOT CAUSE NOTE (see report): an earlier version of this suite used
    session_transaction() to inject _user_id directly, skipping the real
    login route. That was unreliable -- in some client instances the
    manually-written session did not propagate to subsequent requests,
    so @login_required treated the client as unauthenticated even though
    the write appeared to succeed. Isolated reproduction confirmed this
    is a Flask/Werkzeug test-client cookie-jar quirk around
    session_transaction() combined with a session cookie already
    established by an earlier real GET -- not a BuildIQ application
    defect. The real login path below was proven reliable across every
    repeated invocation and is what every other test in this file (and
    the app itself) actually uses."""
    token = get_csrf(client, "/login")
    resp = client.post("/login", data={"email": email, "password": password, "csrf_token": token}, follow_redirects=True)
    if resp.status_code != 200 or resp.request.path != "/" or "Invalid email or password" in resp.get_data(as_text=True):
        raise RuntimeError(f"login() failed for {email}: status={resp.status_code}, path={resp.request.path}")
    return token


def snapshot(db, query, params=()):
    """Read-only snapshot of a row/value, used for before/after comparison."""
    return db.execute(query, params).fetchone()


def main():
    # ROOT-CAUSE FIX: this script's own DB setup/teardown uses a plain,
    # independent sqlite3 connection -- NOT appmod.get_db() -- and no
    # outer Flask app context is held while test-client requests run
    # (see the bottom of this file). Holding one caused Flask's test
    # client to reuse that context instead of pushing a fresh one per
    # request, which leaked Flask-WTF's per-request CSRF token cache in
    # `g` across what should have been independent client sessions.
    # See the report for the full root-cause writeup.
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    print("Setting up temporary fixtures...")

    # A user who is in the LEGACY ADMIN_EMAILS list (so is_admin() would
    # say True) but explicitly DENIED module:team_admin:view in the new
    # system -- the exact mandatory case from the review.
    legacy_deny_email = appmod.ADMIN_EMAILS[0]  # a real legacy-listed email
    existing = db.execute("SELECT * FROM users WHERE email=?", (legacy_deny_email,)).fetchone()
    created_temp_legacy_user = False
    if existing:
        legacy_row = existing
    else:
        db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
                   ("__legacy_admin_test", legacy_deny_email, pw_hash, now))
        db.commit()
        legacy_row = db.execute("SELECT * FROM users WHERE email=?", (legacy_deny_email,)).fetchone()
        created_temp_legacy_user = True

    # Backfill this legacy user (idempotent -- won't touch them if they
    # already had roles from a prior run/real usage).
    appmod._backfill_user_roles(db)
    db.commit()

    deny_perm_id = db.execute("SELECT id FROM permissions WHERE key='module:team_admin:view'").fetchone()[0]
    db.execute(
        "INSERT OR REPLACE INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) "
        "VALUES ((SELECT id FROM user_permission_overrides WHERE user_id=? AND permission_id=?), ?, ?, ?, ?)"
        if False else
        "INSERT INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
        (legacy_row["id"], deny_perm_id, "deny", "test_setup", now)
    )
    db.commit()

    # A plain restricted employee for the bypass-attempt tests
    emp_email = "__test_restricted_employee@test.local"
    db.execute("INSERT OR IGNORE INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__test_restricted_employee", emp_email, pw_hash, now))
    db.commit()
    emp_uid = db.execute("SELECT id FROM users WHERE email=?", (emp_email,)).fetchone()[0]
    emp_role_id = db.execute("SELECT id FROM roles WHERE name='Employee'").fetchone()[0]
    db.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (emp_uid, emp_role_id))
    db.commit()

    # A second, throwaway team member for the delete-bypass test, and a
    # roadmap item for the roadmap-mutation-bypass test.
    victim_email = "__test_delete_victim@test.local"
    db.execute("INSERT OR IGNORE INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__test_delete_victim", victim_email, pw_hash, now))
    db.commit()
    victim_id = db.execute("SELECT id FROM users WHERE email=?", (victim_email,)).fetchone()[0]

    db.execute(
        "INSERT INTO roadmap_items (name, lane, note, progress_pct, sort_order, updated_at) VALUES (?,?,?,?,?,?)",
        ("__test_roadmap_item", "later", "original note", 5, 999, now)
    )
    db.commit()
    roadmap_id = db.execute("SELECT id FROM roadmap_items WHERE name='__test_roadmap_item'").fetchone()[0]

    material_id = None
    row = db.execute("SELECT id FROM inventory_materials LIMIT 1").fetchone()
    if not row:
        db.execute("INSERT INTO inventory_materials (item_name, quantity, site, created_at) VALUES (?,?,?,?)",
                   ("__test_material", "1", "TestSite", now))
        db.commit()
        material_id = db.execute("SELECT id FROM inventory_materials WHERE item_name='__test_material'").fetchone()[0]
    else:
        material_id = row[0]

    print()
    print("=== MANDATORY: legacy admin + explicit deny = DENIED ===")
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        legacy_user = appmod.User(legacy_row)
        login_user(legacy_user)
        check(
            "user_has_permission() returns False despite legacy ADMIN_EMAILS membership",
            appmod.user_has_permission(legacy_user, "module:team_admin:view") is False
        )
        check(
            "_authorized() returns False (no is_admin() fallback rescuing it)",
            appmod._authorized("module:team_admin:view") is False
        )
        check(
            "is_admin() itself still returns True (proves the list membership is real -- the DENY is what's blocking, not a missing list entry)",
            appmod.is_admin() is True
        )

    print()
    print("=== Legacy runtime bypass fully removed: spot-check all 4 migrated helper functions ===")
    # These check the FUNCTION behavior directly for a user with no role/
    # override at all -- if any of them still fell back to a legacy list,
    # a fresh non-legacy user would incorrectly pass.
    fresh_email = "__test_fresh_norole@test.local"
    db.execute("INSERT OR IGNORE INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__test_fresh_norole", fresh_email, pw_hash, now))
    db.commit()
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        fresh_row = db.execute("SELECT * FROM users WHERE email=?", (fresh_email,)).fetchone()
        fresh_user = appmod.User(fresh_row)
        login_user(fresh_user)
        check("is_project_hunt_allowed() False for zero-role user", appmod.is_project_hunt_allowed() is False)
        check("is_atlas_allowed() False for zero-role user", appmod.is_atlas_allowed() is False)
        check("is_procurement() False for zero-role user", appmod.is_procurement() is False)
        check("is_whatsapp_admin() False for zero-role user", appmod.is_whatsapp_admin() is False)
        check("_authorized(module:team_admin:view) False for zero-role user", appmod._authorized("module:team_admin:view") is False)

    print()
    print("=== Valid-CSRF POST bypass attempts by a restricted Employee, with DB before/after proof ===")
    client = appmod.app.test_client()
    csrf = login(client, emp_email, pw)

    # --- 1. Team/user mutation: delete another user ---
    before = snapshot(db, "SELECT * FROM users WHERE id=?", (victim_id,))
    resp = client.post(f"/team/{victim_id}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    after = db.execute("SELECT * FROM users WHERE id=?", (victim_id,)).fetchone()
    check(f"Team delete: request rejected (got {resp.status_code}, expected 302 redirect-away, not 200)", resp.status_code == 302)
    check("Team delete: victim user still exists (DB unchanged)", after is not None)
    check("Team delete: victim row identical before/after", tuple(before) == tuple(after) if after else False)

    # --- 2. Product Intelligence roadmap mutation ---
    before_rm = snapshot(db, "SELECT * FROM roadmap_items WHERE id=?", (roadmap_id,))
    resp2 = client.post(f"/admin/roadmap/{roadmap_id}/update",
                         data={"csrf_token": csrf, "lane": "now", "progress_pct": "100", "note": "HACKED"},
                         follow_redirects=False)
    after_rm = db.execute("SELECT * FROM roadmap_items WHERE id=?", (roadmap_id,)).fetchone()
    check(f"Roadmap update: request rejected (got {resp2.status_code})", resp2.status_code == 302)
    check("Roadmap update: row unchanged (still original note/lane/progress)",
          after_rm["note"] == "original note" and after_rm["lane"] == "later" and after_rm["progress_pct"] == 5)

    # --- 3. Destructive SitePulse/inventory action: delete a material ---
    before_mat = snapshot(db, "SELECT * FROM inventory_materials WHERE id=?", (material_id,))
    resp3 = client.post(f"/inventory/materials/{material_id}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    after_mat = db.execute("SELECT * FROM inventory_materials WHERE id=?", (material_id,)).fetchone()
    check(f"Material delete: request rejected (got {resp3.status_code})", resp3.status_code == 302)
    check("Material delete: row still exists, unchanged", after_mat is not None and tuple(before_mat) == tuple(after_mat))

    # --- 4. Admin/system-data mutation: import (POST-based) ---
    resp4 = client.post("/admin/import", data={"csrf_token": csrf}, follow_redirects=False)
    check(f"Admin import: request rejected (got {resp4.status_code})", resp4.status_code == 302)

    # --- 5. WhatsApp group mutation ---
    before_wa_count = db.execute("SELECT COUNT(*) FROM whatsapp_site_groups").fetchone()[0]
    resp5 = client.post("/whatsapp-groups/new", data={"csrf_token": csrf, "keyword": "hacked", "chat_id": "999"}, follow_redirects=False)
    after_wa_count = db.execute("SELECT COUNT(*) FROM whatsapp_site_groups").fetchone()[0]
    check(f"WhatsApp group create: request rejected (got {resp5.status_code})", resp5.status_code == 302)
    check("WhatsApp group create: no new row inserted", before_wa_count == after_wa_count)

    # --- 6. Admin user-permissions mutation ---
    before_override_count = db.execute("SELECT COUNT(*) FROM user_permission_overrides WHERE user_id=?", (victim_id,)).fetchone()[0]
    resp6 = client.post(f"/admin/users/{victim_id}/permissions",
                         data={"csrf_token": csrf, "permission_id": str(deny_perm_id), "state": "grant"},
                         follow_redirects=False)
    after_override_count = db.execute("SELECT COUNT(*) FROM user_permission_overrides WHERE user_id=?", (victim_id,)).fetchone()[0]
    check(f"Permissions edit: request rejected (got {resp6.status_code})", resp6.status_code == 302)
    check("Permissions edit: no override row created for victim", before_override_count == after_override_count)

    client.get("/logout")

    print()
    print("=== ISOLATED: /admin/users VIEW-permission-does-not-grant-WRITE boundary ===")
    print("--- Step 1: create a fresh test user with ONLY module:team_admin:view ---")
    view_only_email = "__test_view_only@test.local"
    db.execute("DELETE FROM users WHERE email=?", (view_only_email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__test_view_only", view_only_email, pw_hash, now))
    db.commit()
    vo_uid = db.execute("SELECT id FROM users WHERE email=?", (view_only_email,)).fetchone()[0]
    view_perm_id = db.execute("SELECT id FROM permissions WHERE key='module:team_admin:view'").fetchone()[0]
    write_perm_id = db.execute("SELECT id FROM permissions WHERE key='action:team_admin:manage_users'").fetchone()[0]
    db.execute("DELETE FROM user_permission_overrides WHERE user_id=?", (vo_uid,))
    db.execute(
        "INSERT INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
        (vo_uid, view_perm_id, "grant", "test_setup", now)
    )
    db.commit()

    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        vo_row = db.execute("SELECT * FROM users WHERE id=?", (vo_uid,)).fetchone()
        vo_user = appmod.User(vo_row)
        login_user(vo_user)
        check("Fixture has module:team_admin:view", appmod.user_has_permission(vo_user, "module:team_admin:view") is True)
        check("Fixture does NOT have action:team_admin:manage_users", appmod.user_has_permission(vo_user, "action:team_admin:manage_users") is False)

    print("--- Step 2: authenticate via the real login flow on a fresh client ---")
    client2 = appmod.app.test_client()
    login_token = login(client2, view_only_email, pw)  # raises if login fails -- see login()'s docstring

    print("--- Step 3: prove authentication BEFORE testing /admin/users ---")
    home_resp = client2.get("/", follow_redirects=False)
    check("Authenticated client gets 200 on GET / (not a login redirect)", home_resp.status_code == 200)
    check("Home page does not redirect to /login", "/login" not in (home_resp.headers.get("Location") or ""))

    print("--- Step 4: GET /admin/users must succeed (VIEW permission present) ---")
    resp_get = client2.get("/admin/users", follow_redirects=False)
    check(f"GET /admin/users returns 200 (got {resp_get.status_code})", resp_get.status_code == 200)
    check("GET /admin/users did not redirect to /login (proves this is a permission pass, not an auth artifact)",
          "/login" not in (resp_get.headers.get("Location") or ""))

    print("--- Step 5: fetch a fresh CSRF token from the now-authenticated session for the POST ---")
    post_csrf = get_csrf(client2, "/admin/users")
    check("Fresh CSRF token obtained from the authenticated /admin/users page", bool(post_csrf))

    print("--- Step 6: capture DB state the mutation would change, BEFORE the POST ---")
    before_dept_count = db.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
    before_depts = [dict(r) for r in db.execute("SELECT * FROM departments ORDER BY id").fetchall()]

    print("--- Step 7: valid-CSRF POST attempting a real mutation (add a department) ---")
    resp_post = client2.post("/admin/users", data={
        "csrf_token": post_csrf, "action": "add_department", "new_department": "HACKED_DEPT"
    }, follow_redirects=False)
    check(f"POST /admin/users rejected by authorization, not CSRF (got {resp_post.status_code}, a 400 would mean CSRF -- 302 means it reached the permission check)", resp_post.status_code == 302)
    check("POST did not redirect to /login (proves this is a permission denial on an authenticated session, not a re-auth prompt)",
          "/login" not in (resp_post.headers.get("Location") or ""))

    print("--- Step 8: verify DB state AFTER the POST is byte-for-byte identical ---")
    after_dept_count = db.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
    after_depts = [dict(r) for r in db.execute("SELECT * FROM departments ORDER BY id").fetchall()]
    check("Department count unchanged (BEFORE == AFTER)", before_dept_count == after_dept_count)
    check("Full department table contents unchanged (BEFORE == AFTER)", before_depts == after_depts)
    check("HACKED_DEPT was not created", db.execute("SELECT 1 FROM departments WHERE name='HACKED_DEPT'").fetchone() is None)

    client2.get("/logout")

    print()
    print("=== Backfill idempotency: repeated runs never duplicate roles ===")
    before_role_count = db.execute("SELECT COUNT(*) FROM user_roles WHERE user_id=?", (legacy_row["id"],)).fetchone()[0]
    for _ in range(3):
        appmod._backfill_user_roles(db)
        db.commit()
    after_role_count = db.execute("SELECT COUNT(*) FROM user_roles WHERE user_id=?", (legacy_row["id"],)).fetchone()[0]
    check("3x repeated backfill does not duplicate the legacy admin's role assignment", before_role_count == after_role_count)

    print()
    print("Cleaning up temporary fixtures...")
    cleanup_emails = [emp_email, victim_email, fresh_email, view_only_email]
    if created_temp_legacy_user:
        cleanup_emails.append(legacy_deny_email)
    else:
        # Don't delete a pre-existing legacy user -- just remove the
        # explicit deny override we added for the test.
        db.execute("DELETE FROM user_permission_overrides WHERE user_id=? AND permission_id=? AND granted_by='test_setup'",
                   (legacy_row["id"], deny_perm_id))
    for email in cleanup_emails:
        row = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            uid = row[0]
            db.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
            db.execute("DELETE FROM user_permission_overrides WHERE user_id=?", (uid,))
            db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.execute("DELETE FROM roadmap_items WHERE id=?", (roadmap_id,))
    # Defensive only -- test #5 (an unauthorized WhatsApp group create
    # attempt) already proves via before/after COUNT(*) that no row was
    # created; this is a narrow just-in-case cleanup, not depended on for
    # correctness, and matches on both fields together rather than a bare
    # keyword to minimize any chance of matching an unrelated real row.
    db.execute("DELETE FROM whatsapp_site_groups WHERE keyword='hacked' AND chat_id='999'")
    # BUG FOUND AND FIXED (CTO audit): this test creates __test_material
    # as a fixture (only when the materials table was otherwise empty)
    # but never deleted it -- confirmed as real leftover residue in the
    # packaged DB. It's deliberately never deleted mid-test (test #3
    # proves the denied delete attempt left it untouched), so it has to
    # be cleaned up here instead, at the point the test fixture's job is
    # done, not left to accumulate across runs.
    db.execute("DELETE FROM inventory_materials WHERE item_name='__test_material'")
    db.commit()
    # ORDERING MATTERS (CTO finding): the orphan assertion runs against
    # the state left by this script's own explicit cleanup above --
    # BEFORE any broad safety-net cleanup can hide a defect. Only after
    # the assertion has been recorded does the narrowly-scoped emergency
    # net run.
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
    # Deliberately NOT wrapped in "with appmod.app.app_context():" --
    # see main()'s comment for why. main() manages its own DB connection
    # and its own short-lived request/app contexts where needed.
    main()
