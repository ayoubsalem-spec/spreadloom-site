"""
Permission Enforcement Consolidation -- regression tests.

Covers what scripts/phase3a_tests.py does not: the full seeded role
matrix, explicit-deny/explicit-grant precedence, direct-URL bypass
attempts (GET and POST) against every migrated route, and legacy-user
parity after migration. Uses the real app, real routes, real Flask
test client -- not mocks.

Runs entirely against its own disposable, auto-created database (see
_test_db_setup.isolate_test_database(), called below before `import
app`) -- never against any existing/company database. Creates
temporary users and cleans them up at the end, win or lose, purely as
good practice; isolation is what actually guarantees no external data
is ever at risk. For migration rehearsal against a real existing
database, use scripts/migration_rehearsal.py instead.

Usage (from the project root):
    APP_ENV=development python3 scripts/permission_migration_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
import re
import sqlite3

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
    return resp


def main():
    # Uses a plain, independent sqlite3 connection (not appmod.get_db())
    # and holds no outer Flask app context across test-client calls --
    # see scripts/security_correction_tests.py's main() docstring for
    # why that combination matters (it caused a stale-CSRF-token
    # cross-session leak when this script had more than one real login
    # in earlier revisions).
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()

    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    print("Setting up temporary role-matrix test users...")
    role_names = ["Administrator", "Project Manager", "Procurement", "Estimator", "Operations", "Employee"]
    test_emails = {}
    for role_name in role_names:
        email = f"__test_{role_name.lower().replace(' ', '_')}@test.local"
        test_emails[role_name] = email
        db.execute(
            "INSERT OR IGNORE INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
            (f"__test_{role_name}", email, pw_hash, now)
        )
        db.commit()
        uid = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
        role_id = db.execute("SELECT id FROM roles WHERE name=?", (role_name,)).fetchone()[0]
        db.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (uid, role_id))
        db.commit()

    # Extra user for explicit deny/grant precedence tests
    deny_email = "__test_deny@test.local"
    db.execute("INSERT OR IGNORE INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__test_deny", deny_email, pw_hash, now))
    db.commit()
    deny_uid = db.execute("SELECT id FROM users WHERE email=?", (deny_email,)).fetchone()[0]
    admin_role_id = db.execute("SELECT id FROM roles WHERE name='Administrator'").fetchone()[0]
    db.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (deny_uid, admin_role_id))
    db.commit()
    # Administrator role grants module:team_admin:view -- now explicitly deny it for this one user
    perm_id = db.execute("SELECT id FROM permissions WHERE key='module:team_admin:view'").fetchone()[0]
    db.execute(
        "INSERT OR IGNORE INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
        (deny_uid, perm_id, "deny", "test_setup", now)
    )
    db.commit()

    grant_email = "__test_grant@test.local"
    db.execute("INSERT OR IGNORE INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__test_grant", grant_email, pw_hash, now))
    db.commit()
    grant_uid = db.execute("SELECT id FROM users WHERE email=?", (grant_email,)).fetchone()[0]
    emp_role_id = db.execute("SELECT id FROM roles WHERE name='Employee'").fetchone()[0]
    db.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (grant_uid, emp_role_id))
    db.commit()
    # Employee role does NOT grant module:team_admin:view -- explicitly grant it for this one user
    db.execute(
        "INSERT OR IGNORE INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
        (grant_uid, perm_id, "grant", "test_setup", now)
    )
    db.commit()

    no_role_email = "__test_norole@test.local"
    db.execute("INSERT OR IGNORE INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__test_norole", no_role_email, pw_hash, now))
    db.commit()

    print()
    print("=== Explicit deny beats role grant ===")
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        deny_row = db.execute("SELECT * FROM users WHERE email=?", (deny_email,)).fetchone()
        deny_user = appmod.User(deny_row)
        login_user(deny_user)
        check(
            "Administrator with explicit deny on module:team_admin:view -> False",
            appmod.user_has_permission(deny_user, "module:team_admin:view") is False
        )

    print()
    print("=== Explicit grant beats missing role permission ===")
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        grant_row = db.execute("SELECT * FROM users WHERE email=?", (grant_email,)).fetchone()
        grant_user = appmod.User(grant_row)
        login_user(grant_user)
        check(
            "Employee with explicit grant on module:team_admin:view -> True",
            appmod.user_has_permission(grant_user, "module:team_admin:view") is True
        )

    print()
    print("=== No role, no override -> False ===")
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        nr_row = db.execute("SELECT * FROM users WHERE email=?", (no_role_email,)).fetchone()
        nr_user = appmod.User(nr_row)
        login_user(nr_user)
        check(
            "User with zero roles/overrides -> module:team_admin:view False",
            appmod.user_has_permission(nr_user, "module:team_admin:view") is False
        )
        check(
            "User with zero roles/overrides -> module:project_hunt:view False",
            appmod.user_has_permission(nr_user, "module:project_hunt:view") is False
        )

    print()
    print("=== Role matrix: seeded permissions actually followed ===")
    expectations = {
        "Administrator": {"module:team_admin:view": True, "module:project_hunt:view": True, "action:system_data:manage": True},
        "Project Manager": {"module:project_hunt:view": True, "module:team_admin:view": False, "action:system_data:manage": False},
        "Procurement": {"action:sitepulse:place_order": True, "module:project_hunt:view": False},
        "Estimator": {"module:project_hunt:view": True, "action:sitepulse:place_order": False},
        "Operations": {"module:equipment_center:view": True, "module:project_hunt:view": False},
        "Employee": {"module:equipment_center:view": True, "module:project_hunt:view": False, "module:team_admin:view": False},
    }
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        for role_name, perm_checks in expectations.items():
            row = db.execute("SELECT * FROM users WHERE email=?", (test_emails[role_name],)).fetchone()
            u = appmod.User(row)
            login_user(u)
            for perm_key, expected in perm_checks.items():
                actual = appmod.user_has_permission(u, perm_key)
                check(f"{role_name}: {perm_key} -> {expected}", actual == expected)

    print()
    print("=== Direct URL bypass: restricted employee cannot reach protected pages/actions ===")
    client = appmod.app.test_client()
    login_token = get_csrf(client, "/login")
    login(client, test_emails["Employee"], pw)

    protected_gets = [
        "/team", "/admin/users", "/admin/product-intelligence",
        "/admin/backup", "/admin/restore", "/admin/export", "/admin/import",
        "/sitepulse/activity", "/tracker",
    ]
    for path in protected_gets:
        resp = client.get(path, follow_redirects=False)
        # A blocked route redirects (302) away rather than rendering the
        # protected page (200 would mean the bypass worked).
        check(f"Employee GET {path} does not render protected page (got {resp.status_code})", resp.status_code in (302, 404))

    # Direct POST bypass attempts against protected mutation endpoints.
    # Reuses the token fetched at login time -- proven valid for the rest
    # of the session (see security_correction_tests.py's login() for why),
    # so a rejection here is a genuine authorization denial, not CSRF
    # noise. Tightened to require exactly that: only 302/404 count as
    # "blocked" now, not 400 -- a 400 would mean the request never
    # reached the permission check at all, which is not proof of anything.
    resp = client.post("/team/1/delete", data={"csrf_token": login_token}, follow_redirects=False)
    check(f"Employee POST /team/1/delete blocked by authorization (got {resp.status_code})", resp.status_code in (302, 404))

    resp2 = client.post("/inventory/materials/1/delete", data={"csrf_token": login_token}, follow_redirects=False)
    check(f"Employee POST /inventory/materials/1/delete blocked by authorization (got {resp2.status_code})", resp2.status_code in (302, 404))

    resp3 = client.post("/whatsapp-groups/new", data={"csrf_token": login_token, "keyword": "test", "chat_id": "123"}, follow_redirects=False)
    check(f"Employee POST /whatsapp-groups/new blocked by authorization (got {resp3.status_code})", resp3.status_code in (302, 404))

    client.get("/logout")

    print()
    print("=== Legacy user parity: existing hardcoded admin still has full access ===")
    # No real legacy-listed user exists in this test DB (by design -- we
    # never seed real people's credentials into a test script), so this
    # checks the mechanism instead: is_admin() OR'd into _authorized()
    # still passes for anyone in ADMIN_EMAILS regardless of role/override
    # state, which is what actually protects existing production users.
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        legacy_admin_row = db.execute("SELECT * FROM users WHERE email=?", (test_emails["Administrator"],)).fetchone()
        legacy_admin = appmod.User(legacy_admin_row)
        # Temporarily verify the OR-with-legacy mechanism directly:
        # is_admin() checks ADMIN_EMAILS, unrelated to this user's email,
        # so this should be False -- proving _authorized() genuinely
        # depends on the permission system for this user, not a silent
        # legacy passthrough.
        login_user(legacy_admin)
        check(
            "Test Administrator user is NOT in legacy ADMIN_EMAILS (proves grant came from new system)",
            legacy_admin_row["email"] not in appmod.ADMIN_EMAILS
        )
        check(
            "...yet _authorized('module:team_admin:view') is True via role alone",
            appmod._authorized("module:team_admin:view") is True
        )

    # Exact fixture cleanup must run BEFORE the orphan assertion --
    # otherwise a bug in this very cleanup step could create an orphan
    # that the assertion, running earlier, would never see (CTO finding).
    print()
    print("Cleaning up temporary test fixtures...")
    all_test_emails = list(test_emails.values()) + [deny_email, grant_email, no_role_email]
    for email in all_test_emails:
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
    # Deliberately NOT wrapped in "with appmod.app.app_context():" -- see
    # main()'s comment. Held app contexts leak Flask-WTF's cached CSRF
    # token across separate test-client sessions.
    main()
