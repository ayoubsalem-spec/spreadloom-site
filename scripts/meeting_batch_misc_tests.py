"""
Meeting-batch regression: items 1, 2, 4, 5, 7 (items 3 and 6 have their
own dedicated suites -- product_intelligence_approval_gate_tests.py and
atlas_project_context_tests.py).

  1. SitePulse concrete attention indicator -- factual, status-based
  2. Product Intelligence Total Requests KPI click-through anchor
  4. Project Hunt back navigation (deterministic, filter-preserving)
  5. Project Hunt Create/Delete button styling (behavior unchanged)
  7. SitePulse default landing page is the Requests hub, not Inventory

Uses the real Flask app/routes/DB, same pattern as the other suites in
this directory.

Usage (from the project root):
    APP_ENV=development python3 scripts/meeting_batch_misc_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import datetime, date, timedelta

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


def grant(db, user_id, permission_key, now):
    pid = db.execute("SELECT id FROM permissions WHERE key=?", (permission_key,)).fetchone()[0]
    db.execute(
        "INSERT INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id, permission_id) DO UPDATE SET state=excluded.state",
        (user_id, pid, "grant", "test_setup", now)
    )


def make_user(db, email, name, now, pw_hash, permission_keys):
    db.execute("DELETE FROM users WHERE email=?", (email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", (name, email, pw_hash, now))
    db.commit()
    uid = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    for key in permission_keys:
        grant(db, uid, key, now)
    db.commit()
    return uid


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    sitepulse_email = "__batch_sitepulse@test.local"
    ph_email = "__batch_ph@test.local"

    make_user(db, sitepulse_email, "__batch_sitepulse", now, pw_hash,
              ["module:sitepulse:view", "action:sitepulse:manage"])
    make_user(db, ph_email, "__batch_ph", now, pw_hash,
              ["module:project_hunt:view", "action:project_hunt:manage",
               "module:sitepulse:view", "action:sitepulse:manage"])

    # ================================================================
    # ITEM 1 -- SitePulse concrete attention indicator
    # ================================================================
    print("=== Item 1: Concrete attention indicator (factual, status-based) ===")
    pour_date = (date.today() + timedelta(days=5)).isoformat()
    cur = db.execute(
        """INSERT INTO inventory_concrete_requests (project, pour_date, status, requested_by, created_at, updated_at)
           VALUES (?, ?, 'Submitted', ?, ?, ?)""",
        ("__Batch Attention Needed", pour_date, "__batch_sitepulse", now, now)
    )
    db.commit()
    submitted_id = cur.lastrowid
    cur2 = db.execute(
        """INSERT INTO inventory_concrete_requests (project, pour_date, status, requested_by, created_at, updated_at)
           VALUES (?, ?, 'Scheduled', ?, ?, ?)""",
        ("__Batch Already Handled", pour_date, "__batch_sitepulse", now, now)
    )
    db.commit()
    scheduled_id = cur2.lastrowid
    cur3 = db.execute(
        """INSERT INTO inventory_concrete_requests (project, pour_date, status, requested_by, created_at, updated_at)
           VALUES (?, ?, 'Completed', ?, ?, ?)""",
        ("__Batch Done", pour_date, "__batch_sitepulse", now, now)
    )
    db.commit()
    completed_id = cur3.lastrowid

    with appmod.app.test_client() as client:
        login(client, sitepulse_email, pw)
        resp = client.get("/inventory/concrete")
        check("concrete requests list returns 200", resp.status_code == 200)
        html = resp.get_data(as_text=True)

        # Locate each row by its project name and check for the
        # attention glyph specifically within that row's vicinity.
        def row_has_attention_marker(project_name):
            # Locate the row via the <strong>project name</strong>
            # table-cell wrapper specifically -- the project name also
            # appears earlier on the page inside the filter <select>
            # dropdown, which is not the row we care about.
            marker = f"<strong>{project_name}</strong>"
            idx = html.find(marker)
            if idx == -1:
                return None
            window = html[max(0, idx - 300):idx]
            return "Needs ordering/scheduling" in window

        check("Submitted (not yet ordered/scheduled) request shows the attention marker",
              row_has_attention_marker("__Batch Attention Needed") is True)
        check("Scheduled (already handled) request does NOT show the attention marker",
              row_has_attention_marker("__Batch Already Handled") is False)
        check("Completed request does NOT show the attention marker",
              row_has_attention_marker("__Batch Done") is False)

        # Clicking through still works exactly as before.
        resp = client.get(f"/inventory/concrete/{submitted_id}")
        check("opening a Submitted (attention) request still works normally", resp.status_code == 200)
        resp = client.get(f"/inventory/concrete/{scheduled_id}")
        check("opening a Scheduled request still works normally", resp.status_code == 200)

        # Filters still work (unaffected by the marker).
        resp = client.get("/inventory/concrete?status=Submitted")
        check("existing status filter still works", resp.status_code == 200 and "__Batch Attention Needed" in resp.get_data(as_text=True))
        resp = client.get("/inventory/concrete?status=Completed")
        filtered_html = resp.get_data(as_text=True)
        check("existing status filter correctly excludes non-matching requests",
              "<strong>__Batch Attention Needed</strong>" not in filtered_html and "<strong>__Batch Done</strong>" in filtered_html)

    # ================================================================
    # ITEM 2 -- Product Intelligence Total Requests KPI click-through
    # ================================================================
    print()
    print("=== Item 2: Total Requests KPI click-through ===")
    pi_email = "__batch_pi@test.local"
    make_user(db, pi_email, "__batch_pi", now, pw_hash, ["module:product_intelligence:view"])
    with appmod.app.test_client() as client:
        login(client, pi_email, pw)
        resp = client.get("/admin/product-intelligence")
        check("product intelligence page returns 200", resp.status_code == 200)
        html = resp.get_data(as_text=True)
        m = re.search(r'<a href="([^"]*)" class="pi2-strip-item[^"]*">\s*<div class="pi2-strip-value">', html)
        check("Total Requests KPI is a link", m is not None)
        if m:
            href = m.group(1)
            check("Total Requests KPI link targets the All Requests anchor", href.endswith("#pi2-all-requests"))
        check("the All Requests section anchor exists on the page", 'id="pi2-all-requests"' in html)

    # ================================================================
    # ITEM 4 -- Project Hunt back navigation
    # ================================================================
    print()
    print("=== Item 4: Project Hunt back navigation ===")
    proj_cur = db.execute(
        "INSERT INTO tracker_projects (name, status, created_at, updated_at) VALUES (?, 'In Progress', ?, ?)",
        ("__Batch PH Project", now, now)
    )
    db.commit()
    ph_project_id = proj_cur.lastrowid
    with appmod.app.test_client() as client:
        login(client, ph_email, pw)
        # Dashboard's project link should carry the active filter through.
        resp = client.get("/tracker/?filter=active")
        check("dashboard with an explicit filter returns 200", resp.status_code == 200)
        dash_html = resp.get_data(as_text=True)
        check("project row link carries the filter through to the detail page",
              f"/tracker/project/{ph_project_id}?filter=active" in dash_html)

        resp = client.get(f"/tracker/project/{ph_project_id}?filter=active")
        check("project detail page returns 200", resp.status_code == 200)
        detail_html = resp.get_data(as_text=True)
        check("a deterministic Back link is present on the project detail page", "&larr; Project Hunt" in detail_html)
        check("the Back link carries the originating filter back to the dashboard",
              '/tracker/?filter=active' in detail_html or 'href="/tracker/?filter=active"' in detail_html)

        # New/edit project pages also have a deterministic back link now.
        resp = client.get("/tracker/project/new")
        check("new project page has a Back link", "&larr; Project Hunt" in resp.get_data(as_text=True))
        resp = client.get(f"/tracker/project/{ph_project_id}/edit")
        check("edit project page has a deterministic back-to-project link",
              f"/tracker/project/{ph_project_id}" in resp.get_data(as_text=True))

    # ================================================================
    # ITEM 5 -- Project Hunt Create/Delete button visibility (styling
    # only -- behavior, permissions, and confirmation must be unchanged)
    # ================================================================
    print()
    print("=== Item 5: Create/Delete button styling (behavior unchanged) ===")
    with appmod.app.test_client() as client:
        login(client, ph_email, pw)
        resp = client.get("/tracker/project/new")
        html = resp.get_data(as_text=True)
        check("Create Project button uses the standard btn + btn-gold classes (was bare btn-gold, unstyled)",
              'class="btn btn-gold">Create Project' in html)

        resp = client.get(f"/tracker/project/{ph_project_id}/edit")
        html = resp.get_data(as_text=True)
        check("Delete Project Permanently button uses a real class (btn btn-danger), not raw inline style text",
              'class="btn btn-danger">Delete Project Permanently' in html)
        check("the destructive confirm() dialog is still present -- item 5 did not weaken this protection",
              "Delete this project permanently?" in html and "cannot be undone" in html)
        check("the delete form still POSTs to the real delete route with a real CSRF token",
              f'action="/tracker/project/{ph_project_id}/delete"' in html and 'name="csrf_token"' in html)

        # Behavior check: delete still actually requires the real
        # permission server-side (not just hidden by styling).
        no_perm_email = "__batch_ph_noperm@test.local"
        make_user(db, no_perm_email, "__batch_ph_noperm", now, pw_hash, ["module:project_hunt:view"])

    with appmod.app.test_client() as client2:
        login(client2, no_perm_email, pw)
        token = get_csrf(client2, "/tracker/")
        resp = client2.post(f"/tracker/project/{ph_project_id}/delete", data={"csrf_token": token}, follow_redirects=True)
        still_exists = db.execute("SELECT 1 FROM tracker_projects WHERE id = ?", (ph_project_id,)).fetchone()
        check("delete permission is still enforced server-side regardless of button styling", still_exists is not None)

    # ================================================================
    # ITEM 7 -- SitePulse default landing page is Requests, not Inventory
    # ================================================================
    print()
    print("=== Item 7: SitePulse default landing is the Requests hub ===")
    with appmod.app.test_client() as client:
        login(client, sitepulse_email, pw)
        resp = client.get("/")
        nav_html = resp.get_data(as_text=True)
        m = re.search(r'href="([^"]*)"[^>]*>SitePulse<', nav_html)
        check("SitePulse nav link is present", m is not None)
        if m:
            check("SitePulse nav link now targets the Requests hub (/inventory/), not /inventory/materials",
                  m.group(1) == "/inventory/")

        resp = client.get("/inventory/")
        check("the Requests hub (/inventory/) returns 200", resp.status_code == 200)
        hub_html = resp.get_data(as_text=True)
        check("the hub links to Concrete Requests", "Concrete Requests" in hub_html)
        check("the hub links to Purchase Requests", "Purchase Requests" in hub_html)
        check("the hub still links to Inventory (not removed, just no longer the default)", "Inventory" in hub_html)

        # Inventory itself remains fully available and unchanged at its
        # own URL.
        resp = client.get("/inventory/materials")
        check("direct URL to Inventory (/inventory/materials) still works unchanged", resp.status_code == 200)

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__Batch %'")
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__Batch %'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
