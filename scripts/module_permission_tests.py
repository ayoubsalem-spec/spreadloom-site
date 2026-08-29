"""
Module permission enforcement tests -- Project Hunt / Equipment Center /
SitePulse VIEW-vs-MANAGE authorization boundaries, added after the CTO's
deep audit found these mutation routes relied only on the global
module:*:view gate (or, for Equipment Center/SitePulse, nothing at all).

Real app, real routes, real login flow, valid CSRF, before/after DB
proof throughout -- same pattern as the other scripts in this directory.
No outer app_context held across test-client calls (see
security_correction_tests.py's login() docstring for why that matters).

Usage (from the project root):
    APP_ENV=development python3 scripts/module_permission_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import datetime, date, timedelta

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
    return m.group(1) if m else None


def login(client, email, password):
    token = get_csrf(client, "/login")
    resp = client.post("/login", data={"email": email, "password": password, "csrf_token": token}, follow_redirects=True)
    if resp.status_code != 200 or resp.request.path != "/" or "Invalid email or password" in resp.get_data(as_text=True):
        raise RuntimeError(f"login() failed for {email}: status={resp.status_code}, path={resp.request.path}")
    return token


def make_user(db, email, name, now, pw_hash):
    db.execute("DELETE FROM users WHERE email=?", (email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", (name, email, pw_hash, now))
    db.commit()
    return db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]


def grant(db, uid, perm_key, state, now):
    pid = db.execute("SELECT id FROM permissions WHERE key=?", (perm_key,)).fetchone()[0]
    db.execute(
        "INSERT INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id, permission_id) DO UPDATE SET state=excluded.state",
        (uid, pid, state, "test_setup", now)
    )
    db.commit()


def not_login_redirect(resp):
    return "/login" not in (resp.headers.get("Location") or "")


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    # ------------------------------------------------------------------
    # 1 & 2: PROJECT HUNT -- view-only user, and explicit-deny-on-manage
    # ------------------------------------------------------------------
    print("=== 1. Project Hunt: VIEW without MANAGE ===")
    ph_view_email = "__mpt_ph_view@test.local"
    ph_view_id = make_user(db, ph_view_email, "__mpt_ph_view", now, pw_hash)
    grant(db, ph_view_id, "module:project_hunt:view", "grant", now)
    # explicitly NOT granting action:project_hunt:manage

    db.execute("INSERT INTO tracker_projects (name,client,status,created_at,updated_at) VALUES (?,?,?,?,?)",
               ("__mpt_project", "Client", "In Progress", now, now))
    db.commit()
    proj_id = db.execute("SELECT id FROM tracker_projects WHERE name='__mpt_project'").fetchone()[0]

    client = appmod.app.test_client()
    csrf = login(client, ph_view_email, pw)

    r1 = client.get("/tracker/", follow_redirects=False)
    check("GET /tracker/ (dashboard) succeeds for VIEW-only user", r1.status_code == 200)
    r2 = client.get(f"/tracker/project/{proj_id}", follow_redirects=False)
    check("GET project detail succeeds for VIEW-only user", r2.status_code == 200)

    before_proj_count = db.execute("SELECT COUNT(*) FROM tracker_projects").fetchone()[0]
    r3 = client.post("/tracker/project/new", data={"csrf_token": csrf, "name": "__mpt_hacked_project", "status": "In Progress"}, follow_redirects=False)
    after_proj_count = db.execute("SELECT COUNT(*) FROM tracker_projects").fetchone()[0]
    check(f"project-create POST denied (got {r3.status_code})", r3.status_code == 302)
    check("project-create denial is not a login redirect", not_login_redirect(r3))
    check("no project was created", before_proj_count == after_proj_count)

    before_proj_row = dict(db.execute("SELECT * FROM tracker_projects WHERE id=?", (proj_id,)).fetchone())
    r4 = client.post(f"/tracker/project/{proj_id}/update", data={"csrf_token": csrf, "name": "__mpt_hacked_name"}, follow_redirects=False)
    after_proj_row = dict(db.execute("SELECT * FROM tracker_projects WHERE id=?", (proj_id,)).fetchone())
    check(f"project-edit POST denied (got {r4.status_code})", r4.status_code == 302)
    check("project row unchanged after denied edit", before_proj_row == after_proj_row)

    r5 = client.post(f"/tracker/project/{proj_id}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    still_exists = db.execute("SELECT 1 FROM tracker_projects WHERE id=?", (proj_id,)).fetchone()
    check(f"project-delete POST denied (got {r5.status_code})", r5.status_code == 302)
    check("project was NOT deleted", still_exists is not None)

    client.get("/logout")

    print()
    print("=== 2. Project Hunt: explicit DENY on action:project_hunt:manage ===")
    ph_deny_email = "__mpt_ph_deny@test.local"
    ph_deny_id = make_user(db, ph_deny_email, "__mpt_ph_deny", now, pw_hash)
    # Project Manager role grants both view and manage by default --
    # explicit deny on manage must still win.
    pm_role_id = db.execute("SELECT id FROM roles WHERE name='Project Manager'").fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (ph_deny_id, pm_role_id))
    db.commit()
    grant(db, ph_deny_id, "action:project_hunt:manage", "deny", now)

    client2 = appmod.app.test_client()
    csrf2 = login(client2, ph_deny_email, pw)
    r6 = client2.get("/tracker/", follow_redirects=False)
    check("PM role's inherited VIEW still works despite manage being denied", r6.status_code == 200)

    before_count2 = db.execute("SELECT COUNT(*) FROM tracker_projects").fetchone()[0]
    r7 = client2.post("/tracker/project/new", data={"csrf_token": csrf2, "name": "__mpt_hacked2", "status": "In Progress"}, follow_redirects=False)
    after_count2 = db.execute("SELECT COUNT(*) FROM tracker_projects").fetchone()[0]
    check(f"explicit deny blocks project-create even with PM role (got {r7.status_code})", r7.status_code == 302)
    check("no project created despite PM role (deny wins)", before_count2 == after_count2)
    client2.get("/logout")

    # ------------------------------------------------------------------
    # 3 & 4: EQUIPMENT CENTER
    # ------------------------------------------------------------------
    print()
    print("=== 3. Equipment Center: VIEW without MANAGE ===")
    eq_view_email = "__mpt_eq_view@test.local"
    eq_view_id = make_user(db, eq_view_email, "__mpt_eq_view", now, pw_hash)
    grant(db, eq_view_id, "module:equipment_center:view", "grant", now)
    # explicitly NOT granting action:equipment_center:manage

    db.execute("INSERT INTO sitepulse_assets (name,description,status,created_at,updated_at) VALUES (?,?,?,?,?)",
               ("__mpt_asset", "Test asset", "Available", now, now))
    db.commit()
    asset_id = db.execute("SELECT id FROM sitepulse_assets WHERE name='__mpt_asset'").fetchone()[0]

    client3 = appmod.app.test_client()
    csrf3 = login(client3, eq_view_email, pw)
    r8 = client3.get("/sitepulse/", follow_redirects=False)
    check("Equipment Center dashboard GET succeeds for VIEW-only user", r8.status_code == 200)
    r9 = client3.get(f"/sitepulse/asset/{asset_id}", follow_redirects=False)
    check("asset detail GET succeeds for VIEW-only user", r9.status_code == 200)

    before_asset = dict(db.execute("SELECT * FROM sitepulse_assets WHERE id=?", (asset_id,)).fetchone())
    r10 = client3.post(f"/sitepulse/asset/{asset_id}/status", data={"csrf_token": csrf3, "status": "In Maintenance"}, follow_redirects=False)
    after_asset = dict(db.execute("SELECT * FROM sitepulse_assets WHERE id=?", (asset_id,)).fetchone())
    check(f"asset status-change POST denied (got {r10.status_code})", r10.status_code == 302)
    check("asset row unchanged after denied status change", before_asset == after_asset)

    before_asset_count = db.execute("SELECT COUNT(*) FROM sitepulse_assets").fetchone()[0]
    r11 = client3.post("/sitepulse/asset/new", data={"csrf_token": csrf3, "name": "__mpt_hacked_asset", "status": "Available"}, follow_redirects=False)
    after_asset_count = db.execute("SELECT COUNT(*) FROM sitepulse_assets").fetchone()[0]
    check(f"asset-create POST denied (got {r11.status_code})", r11.status_code == 302)
    check("no asset was created", before_asset_count == after_asset_count)
    client3.get("/logout")

    print()
    print("=== 4. Equipment Center: explicit DENY on module:equipment_center:view ===")
    eq_deny_email = "__mpt_eq_deny@test.local"
    eq_deny_id = make_user(db, eq_deny_email, "__mpt_eq_deny", now, pw_hash)
    ops_role_id = db.execute("SELECT id FROM roles WHERE name='Operations'").fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (eq_deny_id, ops_role_id))
    db.commit()
    grant(db, eq_deny_id, "module:equipment_center:view", "deny", now)

    client4 = appmod.app.test_client()
    login(client4, eq_deny_email, pw)
    r12 = client4.get("/sitepulse/", follow_redirects=False)
    check(f"direct Equipment Center URL denied despite Operations role (got {r12.status_code})", r12.status_code == 302)
    check("denial is not a login redirect", not_login_redirect(r12))
    r13 = client4.get(f"/sitepulse/asset/{asset_id}", follow_redirects=False)
    check("direct asset-detail URL also denied", r13.status_code == 302)
    body13 = r13.get_data(as_text=True)
    check("no protected asset content leaked in the denial response", "__mpt_asset" not in body13 and len(body13) < 500)
    client4.get("/logout")

    # ------------------------------------------------------------------
    # 5 & 6: SITEPULSE (inventory)
    # ------------------------------------------------------------------
    print()
    print("=== 5. SitePulse: VIEW without MANAGE ===")
    sp_view_email = "__mpt_sp_view@test.local"
    sp_view_id = make_user(db, sp_view_email, "__mpt_sp_view", now, pw_hash)
    grant(db, sp_view_id, "module:sitepulse:view", "grant", now)
    # explicitly NOT granting action:sitepulse:manage

    db.execute("INSERT INTO tracker_projects (name,client,status,created_at,updated_at) VALUES (?,?,?,?,?)",
               ("__mpt_sp_project", "Client", "In Progress", now, now))
    db.commit()
    sp_proj_id = db.execute("SELECT id FROM tracker_projects WHERE name='__mpt_sp_project'").fetchone()[0]
    db.execute("INSERT INTO inventory_concrete_requests (project,project_id,pour_date,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               ("__mpt_sp_project", sp_proj_id, (date.today()+timedelta(days=2)).isoformat(), "Submitted", now, now))
    db.commit()
    concrete_id = db.execute("SELECT id FROM inventory_concrete_requests WHERE project='__mpt_sp_project'").fetchone()[0]

    client5 = appmod.app.test_client()
    csrf5 = login(client5, sp_view_email, pw)
    r14 = client5.get("/inventory/concrete", follow_redirects=False)
    check("SitePulse concrete list GET succeeds for VIEW-only user", r14.status_code == 200)
    r15 = client5.get(f"/inventory/concrete/{concrete_id}", follow_redirects=False)
    check("concrete request detail GET succeeds for VIEW-only user", r15.status_code == 200)

    before_concrete = dict(db.execute("SELECT * FROM inventory_concrete_requests WHERE id=?", (concrete_id,)).fetchone())
    r16 = client5.post(f"/inventory/concrete/{concrete_id}/status", data={"csrf_token": csrf5, "status": "Completed"}, follow_redirects=False)
    after_concrete = dict(db.execute("SELECT * FROM inventory_concrete_requests WHERE id=?", (concrete_id,)).fetchone())
    check(f"concrete status-change POST denied (got {r16.status_code})", r16.status_code == 302)
    check("concrete request row unchanged after denied status change", before_concrete == after_concrete)

    before_concrete_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
    r17 = client5.post("/inventory/concrete/new", data={"csrf_token": csrf5, "project": "__mpt_hacked_concrete", "pour_date": date.today().isoformat()}, follow_redirects=False)
    after_concrete_count = db.execute("SELECT COUNT(*) FROM inventory_concrete_requests").fetchone()[0]
    check(f"concrete-create POST denied (got {r17.status_code})", r17.status_code == 302)
    check("no concrete request was created", before_concrete_count == after_concrete_count)
    client5.get("/logout")

    print()
    print("=== 6. SitePulse: explicit DENY on module:sitepulse:view ===")
    sp_deny_email = "__mpt_sp_deny@test.local"
    sp_deny_id = make_user(db, sp_deny_email, "__mpt_sp_deny", now, pw_hash)
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (sp_deny_id, ops_role_id))
    db.commit()
    grant(db, sp_deny_id, "module:sitepulse:view", "deny", now)

    client6 = appmod.app.test_client()
    login(client6, sp_deny_email, pw)
    r18 = client6.get("/inventory/concrete", follow_redirects=False)
    check(f"direct SitePulse URL denied despite Operations role (got {r18.status_code})", r18.status_code == 302)
    check("denial is not a login redirect", not_login_redirect(r18))
    body18 = r18.get_data(as_text=True)
    check("no protected concrete-request content leaked in the denial response", "__mpt_sp_project" not in body18 and len(body18) < 500)
    client6.get("/logout")

    # ------------------------------------------------------------------
    # 7: PROCUREMENT ISOLATION -- manage != place_order
    # ------------------------------------------------------------------
    print()
    print("=== 7. SitePulse manage does NOT imply place_order ===")
    manage_only_email = "__mpt_manage_only@test.local"
    manage_only_id = make_user(db, manage_only_email, "__mpt_manage_only", now, pw_hash)
    grant(db, manage_only_id, "module:sitepulse:view", "grant", now)
    grant(db, manage_only_id, "action:sitepulse:manage", "grant", now)
    # explicitly NOT granting action:sitepulse:place_order
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        mo_row = db.execute("SELECT * FROM users WHERE id=?", (manage_only_id,)).fetchone()
        mo_user = appmod.User(mo_row)
        login_user(mo_user)
        check("action:sitepulse:manage grant does NOT also grant action:sitepulse:place_order",
              appmod.user_has_permission(mo_user, "action:sitepulse:place_order") is False)

    client7 = appmod.app.test_client()
    csrf7 = login(client7, manage_only_email, pw)
    # ordinary manage-level mutation should work
    before_status = db.execute("SELECT status FROM inventory_concrete_requests WHERE id=?", (concrete_id,)).fetchone()["status"]
    r19 = client7.post(f"/inventory/concrete/{concrete_id}/status", data={"csrf_token": csrf7, "status": "Completed"}, follow_redirects=False)
    after_status = db.execute("SELECT status FROM inventory_concrete_requests WHERE id=?", (concrete_id,)).fetchone()["status"]
    check(f"manage-level status change (to a non-Scheduled status) succeeds (got {r19.status_code})", r19.status_code == 302)
    check("status actually changed to Completed", after_status == "Completed")

    # but placing an order (procurement-only action) must still be denied
    before_order_status = db.execute("SELECT status FROM inventory_concrete_requests WHERE id=?", (concrete_id,)).fetchone()["status"]
    r20 = client7.post(f"/inventory/concrete/{concrete_id}/order", data={"csrf_token": csrf7, "vendor": "Acme"}, follow_redirects=False)
    after_order_status = db.execute("SELECT status FROM inventory_concrete_requests WHERE id=?", (concrete_id,)).fetchone()["status"]
    check(f"place-order POST denied for manage-only user (got {r20.status_code})", r20.status_code == 302)
    check("status unchanged by the denied order-placement attempt", before_order_status == after_order_status)
    client7.get("/logout")

    # Exact fixture cleanup must run BEFORE the orphan assertion --
    # otherwise a bug in this very cleanup step could create an orphan
    # that the assertion, running earlier, would never see (CTO finding).
    print()
    print("Cleaning up...")
    for email in (ph_view_email, ph_deny_email, eq_view_email, eq_deny_email, sp_view_email, sp_deny_email, manage_only_email):
        row = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            uid = row[0]
            db.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
            db.execute("DELETE FROM user_permission_overrides WHERE user_id=?", (uid,))
            db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__mpt_%'")
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__mpt_%'")
    db.execute("DELETE FROM sitepulse_assets WHERE name LIKE '__mpt_%'")
    db.commit()

    # ORDERING MATTERS (CTO finding): the orphan assertion runs against
    # the state left by the exact fixture cleanup directly above --
    # BEFORE any broad safety-net cleanup can hide a defect, and AFTER
    # every normal destructive cleanup step this suite performs. Only
    # after the assertion has been recorded does the narrowly-scoped
    # emergency net run.
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
    main()
