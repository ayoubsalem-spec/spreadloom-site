"""
CTO deep-audit follow-up tests -- two findings:

1. Privilege-escalation hardening on /admin/users/<id>/permissions
2. Stored XSS via markdown|safe on tracker_quotes.rfq_email/follow_up_email

Real app, real routes, real Flask test client, real login flow (same
pattern as scripts/security_correction_tests.py -- no outer app_context
held across test-client calls; see that file for why).

Usage (from the project root):
    APP_ENV=development python3 scripts/security_hardening_tests.py
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
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    admin_role_id = db.execute("SELECT id FROM roles WHERE name='Administrator'").fetchone()[0]
    manage_users_pid = db.execute("SELECT id FROM permissions WHERE key='action:team_admin:manage_users'").fetchone()[0]
    system_data_pid = db.execute("SELECT id FROM permissions WHERE key='action:system_data:manage'").fetchone()[0]

    print("Setting up fixtures...")
    # actor: has action:team_admin:manage_users via an override, but is
    # NOT an Administrator -- this is exactly the scenario the guard
    # exists for.
    actor_email = "__pe_actor@test.local"
    db.execute("DELETE FROM users WHERE email=?", (actor_email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__pe_actor", actor_email, pw_hash, now))
    db.commit()
    actor_id = db.execute("SELECT id FROM users WHERE email=?", (actor_email,)).fetchone()[0]
    emp_role_id = db.execute("SELECT id FROM roles WHERE name='Employee'").fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (actor_id, emp_role_id))
    db.execute(
        "INSERT INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
        (actor_id, manage_users_pid, "grant", "test_setup", now)
    )
    db.commit()

    # victim: a completely ordinary employee the actor is (legitimately)
    # allowed to manage.
    victim_email = "__pe_victim@test.local"
    db.execute("DELETE FROM users WHERE email=?", (victim_email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__pe_victim", victim_email, pw_hash, now))
    db.commit()
    victim_id = db.execute("SELECT id FROM users WHERE email=?", (victim_email,)).fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (victim_id, emp_role_id))
    db.commit()

    # a real full Administrator, for the "authorized admin management
    # still works" half of the proof
    real_admin_email = "__pe_real_admin@test.local"
    db.execute("DELETE FROM users WHERE email=?", (real_admin_email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__pe_real_admin", real_admin_email, pw_hash, now))
    db.commit()
    real_admin_id = db.execute("SELECT id FROM users WHERE email=?", (real_admin_email,)).fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (real_admin_id, admin_role_id))
    db.commit()

    client = appmod.app.test_client()
    csrf = login(client, actor_email, pw)

    print()
    print("=== 1a. Non-Administrator actor CANNOT grant Administrator role to a victim ===")
    before_roles = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (victim_id,)).fetchall())
    resp = client.post(f"/admin/users/{victim_id}/permissions",
                        data={"csrf_token": csrf, "action": "assign_role", "role_id": str(admin_role_id)},
                        follow_redirects=False)
    after_roles = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (victim_id,)).fetchall())
    check("victim did NOT receive the Administrator role", admin_role_id not in after_roles)
    check("victim's role set is completely unchanged", before_roles == after_roles)

    print()
    print("=== 1b. Non-Administrator actor CANNOT grant a high-privilege permission ===")
    before_override = db.execute("SELECT state FROM user_permission_overrides WHERE user_id=? AND permission_id=?", (victim_id, system_data_pid)).fetchone()
    resp2 = client.post(f"/admin/users/{victim_id}/permissions",
                         data={"csrf_token": csrf, "action": "set_override", "permission_id": str(system_data_pid), "state": "grant"},
                         follow_redirects=False)
    after_override = db.execute("SELECT state FROM user_permission_overrides WHERE user_id=? AND permission_id=?", (victim_id, system_data_pid)).fetchone()
    check("victim was NOT granted action:system_data:manage", after_override is None or after_override["state"] != "grant")
    check("override row unchanged before/after", before_override == after_override)

    print()
    print("=== 1c. Non-Administrator actor CANNOT remove Administrator role from someone ===")
    before_admin_roles = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (real_admin_id,)).fetchall())
    resp3 = client.post(f"/admin/users/{real_admin_id}/permissions",
                         data={"csrf_token": csrf, "action": "remove_role", "role_id": str(admin_role_id)},
                         follow_redirects=False)
    after_admin_roles = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (real_admin_id,)).fetchall())
    check("real admin's Administrator role was NOT removed", admin_role_id in after_admin_roles)
    check("real admin's role set unchanged", before_admin_roles == after_admin_roles)

    print()
    print("=== 1d. Actor CANNOT modify their OWN roles/permissions through this page ===")
    before_actor_roles = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (actor_id,)).fetchall())
    resp4 = client.post(f"/admin/users/{actor_id}/permissions",
                         data={"csrf_token": csrf, "action": "assign_role", "role_id": str(admin_role_id)},
                         follow_redirects=False)
    after_actor_roles = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (actor_id,)).fetchall())
    check("actor could NOT self-escalate to Administrator", admin_role_id not in after_actor_roles)
    check("actor's own role set unchanged", before_actor_roles == after_actor_roles)

    print()
    print("=== 1e. Ordinary role assignment (non-privileged) still works for a manage_users holder ===")
    ops_role_id = db.execute("SELECT id FROM roles WHERE name='Operations'").fetchone()[0]
    resp5 = client.post(f"/admin/users/{victim_id}/permissions",
                         data={"csrf_token": csrf, "action": "assign_role", "role_id": str(ops_role_id)},
                         follow_redirects=False)
    after_ops = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (victim_id,)).fetchall())
    check("ordinary (non-privileged) role assignment still succeeds", ops_role_id in after_ops)
    check("POST returned a normal redirect (302), not an error", resp5.status_code == 302)

    client.get("/logout")

    print()
    print("=== 1f. Real Administrator CAN grant Administrator role (authorized path still works) ===")
    client2 = appmod.app.test_client()
    csrf2 = login(client2, real_admin_email, pw)
    resp6 = client2.post(f"/admin/users/{victim_id}/permissions",
                          data={"csrf_token": csrf2, "action": "assign_role", "role_id": str(admin_role_id)},
                          follow_redirects=False)
    victim_roles_now = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (victim_id,)).fetchall())
    check("a real Administrator CAN grant the Administrator role", admin_role_id in victim_roles_now)

    print()
    print("=== 1g. Last-Administrator protection ===")
    # Structural note (this is the "genuinely prove the intended branch"
    # fix): with the self-modification block AND the "only an
    # Administrator can remove Administrator" guard both active, the
    # remaining-count-==-0 branch can never actually be reached through a
    # single legitimate route call. Whoever passes the "actor is
    # Administrator" gate necessarily holds the Administrator role
    # themselves (a distinct user_roles row from the target, since
    # self-modification is blocked), and that row always counts toward
    # "remaining" in the query below -- so removing any OTHER admin while
    # you remain one always leaves >=1 behind. This makes the check real
    # defense-in-depth for a future change (e.g. if the actor-must-be-
    # Administrator guard were ever loosened), not a path reachable today.
    # The original version of this test forced real_admin to lose their
    # own Administrator role first, which made it fail the EARLIER
    # "only an Administrator can remove Administrator access" check
    # instead -- a false pass for the wrong reason. This version proves
    # the actual remaining-count logic directly against the real
    # database state, exactly as the route computes it, instead of
    # routing around it.
    db.execute("DELETE FROM user_roles WHERE user_id != ? AND role_id=?", (victim_id, admin_role_id))
    db.commit()
    total_admins = db.execute("SELECT COUNT(*) FROM user_roles WHERE role_id=?", (admin_role_id,)).fetchone()[0]
    check("test setup: exactly one Administrator (victim) exists system-wide", total_admins == 1)
    remaining_if_victim_removed = db.execute(
        "SELECT COUNT(*) FROM user_roles WHERE role_id = ? AND user_id != ?",
        (admin_role_id, victim_id)
    ).fetchone()[0]
    check(
        "the exact query the route uses correctly computes 0 remaining if the last admin were removed "
        "(proves the guard's condition is correct, even though no single request can reach it today "
        "given the other two guards)",
        remaining_if_victim_removed == 0
    )
    # Restore real_admin's role so later sections have a genuine
    # Administrator to log in as.
    db.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (real_admin_id, admin_role_id))
    db.commit()

    print()
    print("=== 1g-ii. End-to-end: an Administrator removing a DIFFERENT admin (not the last one) still succeeds ===")
    # The genuinely reachable, realistic path: 2 admins exist (real_admin,
    # victim); real_admin removes victim's Administrator role. This
    # should succeed (1 admin -- real_admin -- remains), proving the
    # guard doesn't over-block ordinary admin housekeeping.
    client2b = appmod.app.test_client()
    csrf2b = login(client2b, real_admin_email, pw)
    resp7b = client2b.post(f"/admin/users/{victim_id}/permissions",
                            data={"csrf_token": csrf2b, "action": "remove_role", "role_id": str(admin_role_id)},
                            follow_redirects=False)
    victim_roles_after_b = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (victim_id,)).fetchall())
    check("removing a non-last admin's role succeeds (1 admin remains: real_admin)", admin_role_id not in victim_roles_after_b)
    client2b.get("/logout")

    print()
    print("=== 1h. Explicit-deny behavior still works correctly under the new guard ===")
    client3 = appmod.app.test_client()
    csrf3 = login(client3, real_admin_email, pw)
    # real_admin still holds their Administrator role at this point (1g
    # restored it, 1g-ii only removed victim's) -- no extra setup needed.
    resp8 = client3.post(f"/admin/users/{actor_id}/permissions",
                          data={"csrf_token": csrf3, "action": "set_override", "permission_id": str(manage_users_pid), "state": "deny"},
                          follow_redirects=False)
    override_row = db.execute("SELECT state FROM user_permission_overrides WHERE user_id=? AND permission_id=?", (actor_id, manage_users_pid)).fetchone()
    check("explicit DENY override was successfully recorded", override_row is not None and override_row["state"] == "deny")
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        actor_row = db.execute("SELECT * FROM users WHERE id=?", (actor_id,)).fetchone()
        actor_user = appmod.User(actor_row)
        login_user(actor_user)
        check("actor's manage_users permission is now False (deny takes precedence)",
              appmod.user_has_permission(actor_user, "action:team_admin:manage_users") is False)
    print()
    print("=== 1i. Malformed role_id/permission_id input is rejected safely (no 500, no orphan rows) ===")
    before_role_count = db.execute("SELECT COUNT(*) FROM user_roles WHERE user_id=?", (victim_id,)).fetchall()
    for bad_role_id in ("not-a-number", "-1", "999999", "1; DROP TABLE users;--", ""):
        r = client3.post(f"/admin/users/{victim_id}/permissions",
                          data={"csrf_token": csrf3, "action": "assign_role", "role_id": bad_role_id},
                          follow_redirects=False)
        check(f"malformed role_id={bad_role_id!r} does not 500 (got {r.status_code})", r.status_code in (302, 400))
    after_role_count = db.execute("SELECT COUNT(*) FROM user_roles WHERE user_id=?", (victim_id,)).fetchall()
    check("no orphan/garbage role row was created from malformed input", before_role_count == after_role_count)

    for bad_perm_id in ("not-a-number", "-1", "999999", ""):
        r2 = client3.post(f"/admin/users/{victim_id}/permissions",
                           data={"csrf_token": csrf3, "action": "set_override", "permission_id": bad_perm_id, "state": "grant"},
                           follow_redirects=False)
        check(f"malformed permission_id={bad_perm_id!r} does not 500 (got {r2.status_code})", r2.status_code in (302, 400))
    bogus_override = db.execute("SELECT * FROM user_permission_overrides WHERE user_id=? AND permission_id NOT IN (SELECT id FROM permissions)", (victim_id,)).fetchall()
    check("no orphan permission-override row exists pointing at a nonexistent permission", len(bogus_override) == 0)

    client3.get("/logout")

    print()
    print("=== 2. Stored XSS: malicious content cannot execute/render as trusted markup ===")
    # real_admin_id had its Administrator role removed in step 1g's
    # last-admin test -- restore it here so this section has a genuine
    # Administrator with Project Hunt access, independent of that.
    db.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (real_admin_id, admin_role_id))
    db.commit()
    client4 = appmod.app.test_client()
    csrf4 = login(client4, real_admin_email, pw)

    db.execute("INSERT INTO tracker_projects (name,client,status,created_at,updated_at) VALUES (?,?,?,?,?)",
               ("__xss_regress_project", "Client", "In Progress", now, now))
    db.commit()
    xss_project_id = db.execute("SELECT id FROM tracker_projects WHERE name='__xss_regress_project'").fetchone()[0]

    PAYLOAD_SCRIPT = "<script>alert('xss-script')</script>"
    PAYLOAD_ONERROR = '<img src=x onerror="alert(\'xss-onerror\')">'
    PAYLOAD_SVG = '<svg onload="alert(1)"></svg>'
    LEGIT_MD = "**Bold** and *italic* and a [link](https://example.com)\n\n- one\n- two\n\n> quoted"

    db.execute(
        "INSERT INTO tracker_quotes (project_id, trade, vendor_name, status, rfq_email, follow_up_email, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (xss_project_id, "Electrical", "Vendor", "Not Sent", PAYLOAD_SCRIPT + PAYLOAD_SVG, PAYLOAD_ONERROR, now, now)
    )
    db.commit()

    resp9 = client4.get(f"/tracker/project/{xss_project_id}", follow_redirects=False)
    body = resp9.get_data(as_text=True)
    check("page renders successfully (200)", resp9.status_code == 200)
    check("raw <script> tag does not survive", "<script>alert('xss-script')</script>" not in body)
    check("onerror= attribute does not survive", 'onerror="alert(\'xss-onerror\')"' not in body)
    check("onload= attribute does not survive", "onload=" not in body)
    check("no <svg> tag survives", "<svg" not in body)

    print()
    print("=== 2b. Legitimate Markdown formatting is preserved ===")
    legit_html = appmod.tr_markdown_filter(LEGIT_MD)
    check("bold renders as <strong>", "<strong>Bold</strong>" in legit_html)
    check("italic renders as <em>", "<em>italic</em>" in legit_html)
    check("real link renders with real href", '<a href="https://example.com">link</a>' in legit_html)
    check("list renders as <ul><li>", "<ul>" in legit_html and "<li>one</li>" in legit_html)
    check("blockquote renders", "<blockquote>" in legit_html)

    print()
    print("=== 2c. javascript: protocol links are stripped ===")
    js_html = appmod.tr_markdown_filter("[click](javascript:alert(1))")
    check("javascript: protocol does not appear in output", "javascript:" not in js_html)

    client4.get("/logout")

    # Exact fixture cleanup must run BEFORE the orphan assertion --
    # otherwise a bug in this very cleanup step could create an orphan
    # that the assertion, running earlier, would never see (CTO finding).
    print()
    print("Cleaning up...")
    for email in (actor_email, victim_email, real_admin_email):
        row = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            uid = row[0]
            db.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
            db.execute("DELETE FROM user_permission_overrides WHERE user_id=?", (uid,))
            db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.execute("DELETE FROM tracker_quotes WHERE project_id=?", (xss_project_id,))
    db.execute("DELETE FROM tracker_projects WHERE id=?", (xss_project_id,))
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
