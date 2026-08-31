"""
Product Intelligence 2.0 regression tests.

Covers the backend changes made for the Command Center refinement:
1. product_intelligence() route renders successfully (no 500) with
   both populated and zero data, and every new section (Priority
   Builds, Situation, Request Lifecycle, Attention Required, BuildIQ
   Pulse, Recently Resolved, Build Direction incl. the new "evolving"
   lane, BuildIQ Ecosystem, Platform State) is present with real values.
2. The removed System Health gauge (fleet_uptime_pct/resolution_rate_pct)
   is gone -- no template reference, no route computation.
3. roadmap_item_update() accepts the new "evolving" lane.
4. Role simplification: only Administrator/Procurement/Operations are
   offered for NEW assignment (server-side, not just UI), while an
   existing legacy role assignment (Project Manager/Estimator/Employee)
   keeps working exactly as before -- not revoked, not corrupted.
5. Existing Product Intelligence behavior (filters, click-through,
   Preview Employee View, admin-only access) is unaffected.

Real app, real routes, real login flow, valid CSRF, isolated disposable
database (see _test_db_setup.py) -- same pattern as every other script
in this directory.

Usage (from the project root):
    APP_ENV=development python3 scripts/product_intelligence_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import datetime, date, timedelta

import _test_db_setup
_test_db_setup.isolate_test_database()  # before `import app`

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

    print("Setting up fixtures...")
    admin_email = "__pit_admin@test.local"
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", ("__pit_admin", admin_email, pw_hash, now))
    db.commit()
    admin_uid = db.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (admin_uid, admin_role_id))
    db.commit()

    client = appmod.app.test_client()
    csrf = login(client, admin_email, pw)

    print()
    print("=== 1. Zero-data render: route succeeds, no fabricated content, real empty states ===")
    r0 = client.get("/admin/product-intelligence", follow_redirects=False)
    body0 = r0.get_data(as_text=True)
    check("route returns 200 with an empty database (not a 500)", r0.status_code == 200)
    check("no Python traceback leaked into the response", "Traceback" not in body0)
    check("Attention Required shows the real CLEAR empty state", "Clear" in body0 and "No product-development blockers" in body0)
    check("Recently Resolved shows a real empty state", "Nothing resolved yet" in body0)
    check("Latest Movement shows a real empty state", "No recent movement yet" in body0)
    check("All Requests shows the real 'no requests exist' empty state", "No requests exist yet" in body0)
    check("the old System Health gauge is completely gone", "SYSTEM HEALTH" not in body0 and "resolution_rate_pct" not in body0)
    check("Platform State section is present with truthful Configured/Not Configured wording", "Platform State" in body0 and ("Configured" in body0))
    check("Atlas/WhatsApp state never uses Healthy/Unhealthy wording (that's for build modules, not config state)",
          "Atlas <strong>Healthy" not in body0 and "WhatsApp <strong>Healthy" not in body0)
    check("Application Operational indicator has been removed (not meaningful uptime monitoring)", "Operational" not in body0)
    check("no module tile literally renders the word 'Healthy' (no module gets a blanket health certification)", ">Healthy<" not in body0)
    check("SitePulse shows a truthful attention-scoped status, not a hardcoded claim", "No Attention Items" in body0)
    fresh_pcts = [r[0] for r in db.execute("SELECT progress_pct FROM roadmap_items").fetchall()]
    check("freshly-seeded roadmap items have progress_pct=0, not an invented completion percentage", all(p == 0 for p in fresh_pcts))
    check("Priority Builds hero renders with no progress-percentage markup at all", "pi2-build-pct" not in body0)
    check("Build Direction lane items render with no progress-percentage bar markup", "pi2-build-track" not in body0 and "pi2-build-fill" not in body0)
    check("roadmap wording no longer implies live-production deployment", "Canonical Project Identity is live" not in body0)
    check("BuildIQ Ecosystem uses the merged single-list layout (no duplicate Module Activity section)", "Module Activity" not in body0)

    print()
    print("=== 2. Populated-data render: every new section shows real values ===")
    statuses = ["Submitted", "Reviewing", "Approved", "Building", "Testing", "Released"]
    fr_ids = []
    for i, s in enumerate(statuses):
        name = f"__pit_request_{i}"
        db.execute("INSERT INTO feature_requests (original_request,status,requester_name,requester_email,department,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (name, s, "Test User", admin_email, "Operations", now, now))
        db.commit()
        fr_id = db.execute("SELECT id FROM feature_requests WHERE original_request=?", (name,)).fetchone()[0]
        fr_ids.append(fr_id)
        db.execute("INSERT INTO feature_request_status_history (feature_request_id, status, changed_by, changed_at) VALUES (?,?,?,?)",
                   (fr_id, s, admin_email, now))
        db.commit()

    db.execute("INSERT INTO tracker_projects (name,client,status,bid_due_date,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               ("__pit_project", "Client", "In Progress", (date.today() + timedelta(days=1)).isoformat(), now, now))
    db.commit()
    project_id = db.execute("SELECT id FROM tracker_projects WHERE name='__pit_project'").fetchone()[0]

    r1 = client.get("/admin/product-intelligence", follow_redirects=False)
    body1 = r1.get_data(as_text=True)
    check("route still 200 with real data present", r1.status_code == 200)
    check("Priority Builds hero shows Product Core (real roadmap data)", "Product Core" in body1)
    check("Priority Builds hero shows Product Intelligence with its real status label", "POLISHING" in body1)
    check("Priority Builds hero shows Atlas with EVOLVING label", "EVOLVING" in body1)
    check("Request Lifecycle section renders", "Request Lifecycle" in body1)
    check("a real Building-status request title appears in the page", "__pit_request_3" in body1)
    check("BuildIQ Ecosystem section renders with real module tiles", "BuildIQ Ecosystem" in body1 and "Project Hunt" in body1)
    check("Build Direction shows all 4 lanes including the new Evolving lane", all(x in body1 for x in ["Now", "Next", "Evolving", "Later"]))

    print()
    print("=== 3. roadmap_item_update accepts the new 'evolving' lane ===")
    atlas_roadmap_id = db.execute("SELECT id FROM roadmap_items WHERE name='Atlas'").fetchone()[0]
    r2 = client.post(f"/admin/roadmap/{atlas_roadmap_id}/update",
                      data={"csrf_token": csrf, "lane": "evolving", "progress_pct": "45", "note": "test note"},
                      follow_redirects=False)
    row = db.execute("SELECT lane, progress_pct FROM roadmap_items WHERE id=?", (atlas_roadmap_id,)).fetchone()
    check("roadmap_item_update accepted lane='evolving'", row["lane"] == "evolving")
    check("progress_pct was saved correctly", row["progress_pct"] == 45)

    print()
    print("=== 3b. Startup never auto-rewrites an existing roadmap (CTO audit fix) ===")
    legacy_rows = {
        "SitePulse": "Filters and delivery dates shipped. Polishing purchase request flow next.",
        "Project Hunt": "Bid tracker filters and KPI theme done. Vendor follow-up automation left.",
        "Equipment Center": "Matching Product Intelligence's KPI colors and filters.",
        "BidFlow": "Takeoff + bid system. Waiting on final Excel sheets from estimating.",
    }
    db.execute("DELETE FROM roadmap_items WHERE name IN (%s)" % ",".join("?" * len(legacy_rows)), list(legacy_rows.keys()))
    for name, note in legacy_rows.items():
        db.execute("INSERT INTO roadmap_items (name, lane, note, progress_pct, sort_order, updated_at) VALUES (?,?,?,?,?,?)",
                   (name, "now", note, 50, 99, now))
    db.commit()
    before_rewrite = {name: note for name, note in db.execute(
        "SELECT name, note FROM roadmap_items WHERE name IN (%s)" % ",".join("?" * len(legacy_rows)), list(legacy_rows.keys())
    ).fetchall()}
    appmod.init_db()  # exactly what every real application startup calls
    after_rewrite = {name: note for name, note in db.execute(
        "SELECT name, note FROM roadmap_items WHERE name IN (%s)" % ",".join("?" * len(legacy_rows)), list(legacy_rows.keys())
    ).fetchall()}
    check("normal application startup (init_db()) does NOT rewrite an existing, non-empty roadmap_items table",
          before_rewrite == after_rewrite)
    check("_correct_stale_roadmap_seed no longer exists on the app module (moved to an explicit, manually-run script)",
          not hasattr(appmod, "_correct_stale_roadmap_seed"))
    db.execute("DELETE FROM roadmap_items WHERE name IN (%s)" % ",".join("?" * len(legacy_rows)), list(legacy_rows.keys()))
    db.commit()

    print()
    print("=== 4. Filters and click-through still work ===")
    r3 = client.get("/admin/product-intelligence?status=Building", follow_redirects=False)
    check("status filter returns 200 and includes the matching request", r3.status_code == 200 and "__pit_request_3" in r3.get_data(as_text=True))
    r4 = client.get("/admin/product-intelligence?department=Operations", follow_redirects=False)
    check("department filter returns 200 and includes the matching request", r4.status_code == 200 and "__pit_request_0" in r4.get_data(as_text=True))
    r5 = client.get(f"/admin/product-intelligence/{fr_ids[0]}", follow_redirects=False)
    check("request detail click-through still works", r5.status_code == 200)
    r6 = client.get("/admin/product-intelligence/preview", follow_redirects=False)
    check("Preview Employee View still works", r6.status_code == 200)

    print()
    print("=== 5. Role simplification: server-side enforcement + legacy compatibility ===")
    legacy_email = "__pit_legacy_target@test.local"
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", ("__pit_legacy_target", legacy_email, pw_hash, now))
    db.commit()
    legacy_uid = db.execute("SELECT id FROM users WHERE email=?", (legacy_email,)).fetchone()[0]
    estimator_role_id = db.execute("SELECT id FROM roles WHERE name='Estimator'").fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (legacy_uid, estimator_role_id))
    db.commit()

    # legacy role still functionally grants its permissions (not corrupted)
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        legacy_row = db.execute("SELECT * FROM users WHERE id=?", (legacy_uid,)).fetchone()
        legacy_user = appmod.User(legacy_row)
        login_user(legacy_user)
        check("existing Estimator role assignment still grants its real permissions (module:project_hunt:view)",
              appmod.user_has_permission(legacy_user, "module:project_hunt:view") is True)

    # UI: only the 3 simplified roles offered for NEW assignment
    r7 = client.get(f"/admin/users/{legacy_uid}/permissions", follow_redirects=False)
    body7 = r7.get_data(as_text=True)
    # Scope precisely using the actual role_id hidden input value, which
    # only ever appears inside a role-chip <form> -- definitive proof of
    # whether that role is offered at all, unlike matching on the name
    # text (which also appears in an explanatory sentence elsewhere on
    # the page that legitimately names all three legacy roles as prose).
    pm_role_id = db.execute("SELECT id FROM roles WHERE name='Project Manager'").fetchone()[0]
    employee_role_id = db.execute("SELECT id FROM roles WHERE name='Employee'").fetchone()[0]
    roles_box_match = re.search(r'<h3[^>]*>Roles</h3>(.*?)<h3', body7, re.S)
    roles_box = roles_box_match.group(1) if roles_box_match else ""
    check("Estimator (already assigned) is shown, marked legacy", "Estimator" in roles_box and "(legacy)" in roles_box)
    check("Project Manager (not assigned to this user) has no role-chip form at all", f'value="{pm_role_id}"' not in roles_box)
    check("Employee (not assigned to this user) has no role-chip form at all", f'value="{employee_role_id}"' not in roles_box)
    check("Administrator/Procurement/Operations are all offered as role chips", all(x in roles_box for x in ["Administrator", "Procurement", "Operations"]))

    # server-side: direct POST attempting to assign a legacy role is rejected
    before_roles = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (legacy_uid,)).fetchall())
    r8 = client.post(f"/admin/users/{legacy_uid}/permissions", data={"csrf_token": csrf, "action": "assign_role", "role_id": str(employee_role_id)}, follow_redirects=False)
    after_roles = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (legacy_uid,)).fetchall())
    check("server-side rejects assigning a legacy role even via direct POST (not just hidden in UI)", employee_role_id not in after_roles)
    check("existing Estimator assignment is untouched by the rejected attempt", estimator_role_id in after_roles)

    # a simplified role CAN still be assigned normally
    ops_role_id = db.execute("SELECT id FROM roles WHERE name='Operations'").fetchone()[0]
    r9 = client.post(f"/admin/users/{legacy_uid}/permissions", data={"csrf_token": csrf, "action": "assign_role", "role_id": str(ops_role_id)}, follow_redirects=False)
    final_roles = set(r[0] for r in db.execute("SELECT role_id FROM user_roles WHERE user_id=?", (legacy_uid,)).fetchall())
    check("Operations (one of the 3 simplified roles) can still be assigned normally", ops_role_id in final_roles)

    print()
    print("=== 6. Non-admin access is unaffected (regression check) ===")
    emp_email = "__pit_employee@test.local"
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", ("__pit_employee", emp_email, pw_hash, now))
    db.commit()
    emp_uid = db.execute("SELECT id FROM users WHERE email=?", (emp_email,)).fetchone()[0]
    emp_role_id = db.execute("SELECT id FROM roles WHERE name='Employee'").fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (emp_uid, emp_role_id))
    db.commit()
    client2 = appmod.app.test_client()
    login(client2, emp_email, pw)
    r10 = client2.get("/admin/product-intelligence", follow_redirects=False)
    check("an ordinary Employee-role user is still denied Product Intelligence (unchanged)", r10.status_code == 302)
    client2.get("/logout")

    client.get("/logout")

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print()
    print("Cleaning up...")
    db.execute("DELETE FROM feature_request_status_history WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE original_request LIKE '__pit_%')")
    db.execute("DELETE FROM feature_requests WHERE original_request LIKE '__pit_%'")
    db.execute("DELETE FROM tracker_projects WHERE id=?", (project_id,))
    for uid in (admin_uid, legacy_uid, emp_uid):
        db.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()

    orphans = hygiene.assert_no_orphan_privilege_rows(db)
    for o in orphans:
        FAIL.append(f"DB hygiene: {o}")
        print(f"FAIL  DB hygiene: {o}")
    if not orphans:
        check("no orphan user_roles/user_permission_overrides/role_permissions rows remain", True)
    hygiene.emergency_cleanup_orphans(db)

    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
