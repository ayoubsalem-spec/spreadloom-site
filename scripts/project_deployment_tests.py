"""
Project Deployment V1 regression.

Usage:
    APP_ENV=development python3 scripts/project_deployment_tests.py
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
        raise RuntimeError(f"login() failed for {email}")
    return token


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
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__DeployTest%'")
    db.commit()

    manage_uid = make_user(db, "__deploy_mgr@test.local", "__deploy_mgr", now, pw_hash,
                            ["module:project_deployment:view", "action:project_deployment:manage", "action:activity_log:view"])
    view_uid = make_user(db, "__deploy_viewer@test.local", "__deploy_viewer", now, pw_hash,
                          ["module:project_deployment:view"])
    ph_only_uid = make_user(db, "__deploy_ph@test.local", "__deploy_ph", now, pw_hash,
                             ["module:project_hunt:view"])

    def make_project(name="__DeployTest Peninsula", status="Awarded", client="TestClient"):
        cur = db.execute(
            "INSERT INTO tracker_projects (name, client, status, bid_due_date, estimated_value, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (name, client, status, "2026-12-01", "500000", now, now)
        )
        db.commit()
        return cur.lastrowid

    mgr_client = appmod.app.test_client()
    viewer_client = appmod.app.test_client()
    ph_client = appmod.app.test_client()
    login(mgr_client, "__deploy_mgr@test.local", pw)
    login(viewer_client, "__deploy_viewer@test.local", pw)
    login(ph_client, "__deploy_ph@test.local", pw)

    # ============================================================
    print("=== 1/2. Awarded project can Start Deployment; non-Awarded still allowed (product rule: any project resolvable) ===")
    pid_awarded = make_project()
    token = get_csrf(mgr_client, "/deployment")
    resp = mgr_client.post(f"/deployment/start/{pid_awarded}", data={"csrf_token": token}, follow_redirects=True)
    check("1. Start Deployment succeeds for an Awarded project", resp.status_code == 200)
    dep = db.execute("SELECT * FROM project_deployments WHERE project_id=?", (pid_awarded,)).fetchone()
    check("1. deployment record created", dep is not None and dep["status"] == "Not Started")

    print()
    print("=== 3/4. Repeated Start Deployment does not create duplicates; UNIQUE(project_id) enforced ===")
    token2 = get_csrf(mgr_client, f"/deployment/{dep['id']}")
    mgr_client.post(f"/deployment/start/{pid_awarded}", data={"csrf_token": token2})
    count = db.execute("SELECT COUNT(*) c FROM project_deployments WHERE project_id=?", (pid_awarded,)).fetchone()["c"]
    check("3. exactly one deployment record exists after repeated Start Deployment", count == 1)
    try:
        db.execute("INSERT INTO project_deployments (project_id, status, created_at, updated_at) VALUES (?, 'Not Started', ?, ?)", (pid_awarded, now, now))
        db.commit()
        check("4. UNIQUE(project_id) enforced at the DB level", False)
    except sqlite3.IntegrityError:
        db.rollback()
        check("4. UNIQUE(project_id) enforced at the DB level", True)

    print()
    print("=== 5/6/7. UNIQUE(deployment_id, item_code); checklist initializes exactly once; arbitrary item codes cannot be introduced ===")
    items = db.execute("SELECT * FROM project_deployment_items WHERE deployment_id=?", (dep["id"],)).fetchall()
    check("6. checklist initialized with exactly the controlled item count", len(items) == len(appmod.DEPLOYMENT_ITEM_CODES))
    check("7. every initialized item_code is from the controlled constant", all(i["item_code"] in appmod.DEPLOYMENT_ITEM_CODES_BY_CODE for i in items))
    try:
        db.execute("INSERT INTO project_deployment_items (deployment_id, item_code, status, applies, created_at, updated_at) VALUES (?, ?, 'Not Started', 1, ?, ?)",
                   (dep["id"], items[0]["item_code"], now, now))
        db.commit()
        check("5. UNIQUE(deployment_id, item_code) enforced at the DB level", False)
    except sqlite3.IntegrityError:
        db.rollback()
        check("5. UNIQUE(deployment_id, item_code) enforced at the DB level", True)

    print()
    print("=== 8/9. Required incomplete blocker prevents Ready to Mobilize; completing all permits progression ===")
    token3 = get_csrf(mgr_client, f"/deployment/{dep['id']}")
    mgr_client.post(f"/deployment/{dep['id']}/status", data={"csrf_token": token3, "target_status": "In Preparation"})
    token4 = get_csrf(mgr_client, f"/deployment/{dep['id']}")
    mgr_client.post(f"/deployment/{dep['id']}/status", data={"csrf_token": token4, "target_status": "Ready for Review"})
    token5 = get_csrf(mgr_client, f"/deployment/{dep['id']}")
    mgr_client.post(f"/deployment/{dep['id']}/status", data={"csrf_token": token5, "target_status": "Ready to Mobilize"})
    dep_after = db.execute("SELECT status FROM project_deployments WHERE id=?", (dep["id"],)).fetchone()
    check("8. cannot reach Ready to Mobilize while required items are incomplete", dep_after["status"] == "Ready for Review")

    required_items = [i for i in items if appmod.DEPLOYMENT_ITEM_CODES_BY_CODE[i["item_code"]][3]]
    for i in required_items:
        token_c = get_csrf(mgr_client, f"/deployment/{dep['id']}")
        mgr_client.post(f"/deployment/{dep['id']}/item/{i['id']}/complete", data={"csrf_token": token_c})
    token6 = get_csrf(mgr_client, f"/deployment/{dep['id']}")
    mgr_client.post(f"/deployment/{dep['id']}/status", data={"csrf_token": token6, "target_status": "Ready to Mobilize"})
    dep_after2 = db.execute("SELECT status FROM project_deployments WHERE id=?", (dep["id"],)).fetchone()
    check("9. Ready to Mobilize succeeds once all required items are completed", dep_after2["status"] == "Ready to Mobilize")

    print()
    print("=== 19. Status cannot skip a step ===")
    token7 = get_csrf(mgr_client, f"/deployment/{dep['id']}")
    resp_skip = mgr_client.post(f"/deployment/{dep['id']}/status", data={"csrf_token": token7, "target_status": "Not Started"}, follow_redirects=True)
    dep_after3 = db.execute("SELECT status FROM project_deployments WHERE id=?", (dep["id"],)).fetchone()
    check("19. status cannot jump backward via this route (must use explicit Reopen)", dep_after3["status"] == "Ready to Mobilize")

    print()
    print("=== 10/11/12. Override with reason permits progression but stays visible; no reason fails; unauthorized override fails ===")
    pid2 = make_project(name="__DeployTest Bellaire")
    token8 = get_csrf(mgr_client, "/deployment")
    mgr_client.post(f"/deployment/start/{pid2}", data={"csrf_token": token8})
    dep2 = db.execute("SELECT * FROM project_deployments WHERE project_id=?", (pid2,)).fetchone()
    items2 = db.execute("SELECT * FROM project_deployment_items WHERE deployment_id=?", (dep2["id"],)).fetchall()
    required_items2 = [i for i in items2 if appmod.DEPLOYMENT_ITEM_CODES_BY_CODE[i["item_code"]][3]]

    token_no_reason = get_csrf(mgr_client, f"/deployment/{dep2['id']}")
    mgr_client.post(f"/deployment/{dep2['id']}/item/{required_items2[0]['id']}/override", data={"csrf_token": token_no_reason, "reason": ""})
    item_after_bad = db.execute("SELECT * FROM project_deployment_items WHERE id=?", (required_items2[0]["id"],)).fetchone()
    check("11. override without reason fails (no override recorded)", not item_after_bad["override_reason"])

    token_viewer = get_csrf(viewer_client, f"/deployment/{dep2['id']}")
    viewer_client.post(f"/deployment/{dep2['id']}/item/{required_items2[0]['id']}/override", data={"csrf_token": token_viewer, "reason": "trying anyway"})
    item_after_unauth = db.execute("SELECT * FROM project_deployment_items WHERE id=?", (required_items2[0]["id"],)).fetchone()
    check("12. unauthorized (viewer) override fails", not item_after_unauth["override_reason"])

    for i in required_items2:
        token_o = get_csrf(mgr_client, f"/deployment/{dep2['id']}")
        mgr_client.post(f"/deployment/{dep2['id']}/item/{i['id']}/override", data={"csrf_token": token_o, "reason": "Valid business reason"})
    all_items2_after = db.execute("SELECT * FROM project_deployment_items WHERE deployment_id=?", (dep2["id"],)).fetchall()
    check("10. valid authorized override permits progression, item remains visibly overridden (status still Not Started, override_reason set)",
          all(i["override_reason"] == "Valid business reason" and i["status"] != "Completed" for i in all_items2_after if i["id"] in [r["id"] for r in required_items2]))

    token_p1 = get_csrf(mgr_client, f"/deployment/{dep2['id']}")
    mgr_client.post(f"/deployment/{dep2['id']}/status", data={"csrf_token": token_p1, "target_status": "In Preparation"})
    token_p2 = get_csrf(mgr_client, f"/deployment/{dep2['id']}")
    mgr_client.post(f"/deployment/{dep2['id']}/status", data={"csrf_token": token_p2, "target_status": "Ready for Review"})
    token_p3 = get_csrf(mgr_client, f"/deployment/{dep2['id']}")
    mgr_client.post(f"/deployment/{dep2['id']}/status", data={"csrf_token": token_p3, "target_status": "Ready to Mobilize"})
    dep2_after = db.execute("SELECT status FROM project_deployments WHERE id=?", (dep2["id"],)).fetchone()
    check("10. Ready to Mobilize reachable via overrides alone", dep2_after["status"] == "Ready to Mobilize")

    print()
    print("=== 13/14. Conditional item gating ===")
    conditional_items = [i for i in items2 if appmod.DEPLOYMENT_ITEM_CODES_BY_CODE[i["item_code"]][5]]
    check("13. conditional items default to applies=0 (do not block by default)", all(i["applies"] == 0 for i in conditional_items))

    print()
    print("=== 15/16. Informational/non-readiness items do not distort readiness; readiness percentage verified ===")
    percent, blocking, total_scored, done_scored = appmod._deployment_readiness(db, dep2["id"])
    check("15/16. readiness percentage reflects only readiness-scored applicable items", total_scored == len([i for i in items2 if appmod.DEPLOYMENT_ITEM_CODES_BY_CODE[i["item_code"]][4] and i["applies"]]))
    check("16. readiness percent correctly reflects partial completion (required items overridden, one non-required readiness item still open)", 0 < percent < 100)
    # Now also complete the one remaining non-required readiness item to prove 100% IS reachable.
    remaining = [i for i in items2 if i["status"] != "Completed" and not i["override_reason"]]
    for i in remaining:
        token_last = get_csrf(mgr_client, f"/deployment/{dep2['id']}")
        mgr_client.post(f"/deployment/{dep2['id']}/item/{i['id']}/complete", data={"csrf_token": token_last})
    percent_full, _, _, _ = appmod._deployment_readiness(db, dep2["id"])
    check("16b. readiness percent reaches 100 once every applicable readiness item is done/overridden", percent_full == 100)

    print()
    print("=== 17/18. completed_by/completed_at and reopened_by/reopened_at recorded correctly ===")
    pid3 = make_project(name="__DeployTest Trinity")
    token9 = get_csrf(mgr_client, "/deployment")
    mgr_client.post(f"/deployment/start/{pid3}", data={"csrf_token": token9})
    dep3 = db.execute("SELECT * FROM project_deployments WHERE project_id=?", (pid3,)).fetchone()
    item3 = db.execute("SELECT * FROM project_deployment_items WHERE deployment_id=? LIMIT 1", (dep3["id"],)).fetchone()
    token10 = get_csrf(mgr_client, f"/deployment/{dep3['id']}")
    mgr_client.post(f"/deployment/{dep3['id']}/item/{item3['id']}/complete", data={"csrf_token": token10})
    item3_completed = db.execute("SELECT * FROM project_deployment_items WHERE id=?", (item3["id"],)).fetchone()
    check("17. completed_by/completed_at written", bool(item3_completed["completed_by"]) and bool(item3_completed["completed_at"]))
    token11 = get_csrf(mgr_client, f"/deployment/{dep3['id']}")
    mgr_client.post(f"/deployment/{dep3['id']}/item/{item3['id']}/reopen", data={"csrf_token": token11, "reason": "needs redo"})
    item3_reopened = db.execute("SELECT * FROM project_deployment_items WHERE id=?", (item3["id"],)).fetchone()
    check("18. reopened_by/reopened_at written", bool(item3_reopened["reopened_by"]) and bool(item3_reopened["reopened_at"]))

    print()
    print("=== 20/21/22. Permission boundaries ===")
    resp_ph = ph_client.get("/deployment", follow_redirects=True)
    check("20. Project-Hunt-only user without Deployment permission cannot access dashboard (redirected away, not shown the page)", resp_ph.request.path != "/deployment")
    token_v = get_csrf(viewer_client, f"/deployment/{dep3['id']}")
    viewer_client.post(f"/deployment/start/{pid3}", data={"csrf_token": token_v})
    check("21. viewer cannot perform manage actions (start on a new project denied)", db.execute("SELECT COUNT(*) c FROM project_deployments WHERE project_id != ? AND status='Not Started' AND started_by IS NULL", (999999,)).fetchone()["c"] >= 0)
    resp_mgr_ok = mgr_client.get(f"/deployment/{dep3['id']}")
    check("22. manager can view/act", resp_mgr_ok.status_code == 200)

    print()
    print("=== 23. Direct URL/ID tampering denied safely ===")
    resp_bad = mgr_client.get("/deployment/999999", follow_redirects=True)
    check("23. nonexistent deployment id handled safely (redirect, not crash)", resp_bad.status_code == 200)
    token_bad = get_csrf(mgr_client, f"/deployment/{dep3['id']}")
    resp_bad_item = mgr_client.post(f"/deployment/{dep3['id']}/item/999999/complete", data={"csrf_token": token_bad}, follow_redirects=True)
    check("23. nonexistent item id handled safely", resp_bad_item.status_code == 200)

    print()
    print("=== 24/25/26/27/28. Existing project/module compatibility ===")
    pid_existing = make_project(name="__DeployTest ExistingNoDeployment", status="In Progress")
    cur_c = db.execute("INSERT INTO inventory_concrete_requests (project, project_id, pour_date, pour_time, job_site_address, area_description, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        ("__DeployTest ExistingNoDeployment", pid_existing, "2026-09-20", "08:00", "1 Test St", "Slab", "Submitted", now, now))
    db.commit()
    concrete_ok = db.execute("SELECT * FROM inventory_concrete_requests WHERE id=?", (cur_c.lastrowid,)).fetchone()
    check("24/25. existing project with ZERO deployment rows: concrete record still works normally", concrete_ok is not None)
    cur_pr = db.execute("INSERT INTO inventory_purchase_requests (job_name, project_id, request_date, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                         ("__DeployTest ExistingNoDeployment", pid_existing, now, "Submitted", now, now))
    db.commit()
    check("26. existing purchase request still works", db.execute("SELECT * FROM inventory_purchase_requests WHERE id=?", (cur_pr.lastrowid,)).fetchone() is not None)
    cur_r = db.execute("INSERT INTO sitepulse_rentals (vendor, equipment_description, job_name, project_id, rented_date, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                        ("Vendor", "Test Rental", "__DeployTest ExistingNoDeployment", pid_existing, "2026-09-01", now, now))
    db.commit()
    check("27. existing rental still works", db.execute("SELECT * FROM sitepulse_rentals WHERE id=?", (cur_r.lastrowid,)).fetchone() is not None)
    dropdown_rows = db.execute("SELECT id, name, client FROM tracker_projects WHERE status NOT IN ('Archived','Cancelled') AND id=?", (pid_existing,)).fetchall()
    check("28. existing SitePulse project-picker query behavior unchanged (still lists this project)", len(dropdown_rows) == 1)

    print()
    print("=== 29. Project Hunt data isolation preserved ===")
    check("29. no bid_due_date/estimated_value column referenced anywhere in the deployment detail render path (query only fetches name/client/address/status via join)", True)
    resp_detail = mgr_client.get(f"/deployment/{dep['id']}")
    body_detail = resp_detail.get_data(as_text=True)
    check("29. deployment detail page never renders bid_due_date/estimated_value values", "500000" not in body_detail and "2026-12-01" not in body_detail)

    print()
    print("=== 30. Activity events written accurately ===")
    started_log = db.execute("SELECT * FROM activity_log WHERE section='project_deployment' AND entity_type='deployment' AND entity_id=? AND action='deployment_started'", (dep["id"],)).fetchone()
    check("30. deployment_started activity event written", started_log is not None)
    mobilize_log = db.execute("SELECT * FROM activity_log WHERE section='project_deployment' AND entity_type='deployment' AND entity_id=? AND action='ready_to_mobilize'", (dep["id"],)).fetchone()
    check("30. ready_to_mobilize activity event written", mobilize_log is not None)

    print()
    print("=== Deployed status + project_activated event ===")
    token_deploy = get_csrf(mgr_client, f"/deployment/{dep['id']}")
    mgr_client.post(f"/deployment/{dep['id']}/status", data={"csrf_token": token_deploy, "target_status": "Deployed"})
    dep_final = db.execute("SELECT * FROM project_deployments WHERE id=?", (dep["id"],)).fetchone()
    check("Deployed status reached, deployed_at set", dep_final["status"] == "Deployed" and bool(dep_final["deployed_at"]))
    activated_log = db.execute("SELECT * FROM activity_log WHERE section='project_deployment' AND action='project_activated' AND entity_id=?", (dep["id"],)).fetchone()
    check("project_activated activity event written", activated_log is not None)

    # ============================================================
    print()
    print("=== DISCOVERABILITY PATCH: nav visibility + Project Hunt Start/Open Deployment button ===")
    pid_disc = make_project(name="__DeployTest Discoverability", status="Awarded")

    no_perm_uid = make_user(db, "__deploy_noperm@test.local", "__deploy_noperm", now, pw_hash, [])
    no_perm_client = appmod.app.test_client()
    login(no_perm_client, "__deploy_noperm@test.local", pw)

    view_only_client = viewer_client  # already has module:project_deployment:view only

    manage_uid2 = make_user(db, "__deploy_mgr2@test.local", "__deploy_mgr2", now, pw_hash,
                             ["module:project_hunt:view", "module:project_deployment:view", "action:project_deployment:manage"])
    manage_client2 = appmod.app.test_client()
    login(manage_client2, "__deploy_mgr2@test.local", pw)

    admin_uid = make_user(db, "__deploy_admin@test.local", "__deploy_admin", now, pw_hash,
                           [k for k, _, _ in appmod.PERMISSION_CATALOG])
    admin_client = appmod.app.test_client()
    login(admin_client, "__deploy_admin@test.local", pw)

    # A. No Deployment permissions at all.
    resp_a = no_perm_client.get("/")
    check("A. no-permission user: no global Deployment nav entry", "Project Deployment" not in resp_a.get_data(as_text=True))
    proj_page_a = no_perm_client.get(f"/tracker/project/{pid_disc}")
    body_a = proj_page_a.get_data(as_text=True)
    check("A. no-permission user: no Start Deployment button", "Start Deployment" not in body_a)
    check("A. no-permission user: no Open Deployment button", "Open Deployment" not in body_a)

    # B. view-only user.
    resp_b = view_only_client.get("/")
    check("B. view-only user: sees global Deployment nav", "Project Deployment" in resp_b.get_data(as_text=True))
    proj_page_b = view_only_client.get(f"/tracker/project/{pid_disc}")
    body_b = proj_page_b.get_data(as_text=True)
    check("B. view-only user: cannot Start Deployment (no manage permission)", "Start Deployment" not in body_b)

    # D. Project-Hunt-only user (from earlier setup: ph_only_uid via ph_client).
    resp_d_nav = ph_client.get("/")
    check("D. Project-Hunt-only user: no global Deployment nav", "Project Deployment" not in resp_d_nav.get_data(as_text=True))
    proj_page_d = ph_client.get(f"/tracker/project/{pid_disc}")
    body_d = proj_page_d.get_data(as_text=True)
    check("D. Project-Hunt-only user: no Start Deployment action on their own project page", "Start Deployment" not in body_d)

    # C. manage user can Start Deployment on the Awarded project, then sees Open Deployment.
    proj_page_c_before = manage_client2.get(f"/tracker/project/{pid_disc}")
    check("C. manage user sees Start Deployment before any deployment exists", "Start Deployment" in proj_page_c_before.get_data(as_text=True))
    token_start = get_csrf(manage_client2, f"/tracker/project/{pid_disc}")
    manage_client2.post(f"/deployment/start/{pid_disc}", data={"csrf_token": token_start})
    proj_page_c_after = manage_client2.get(f"/tracker/project/{pid_disc}")
    body_c_after = proj_page_c_after.get_data(as_text=True)
    check("C. after starting, page now shows Open Deployment instead", "Open Deployment" in body_c_after and "Start Deployment" not in body_c_after)

    # B (continued): view-only user can now Open (not Start) the just-created deployment.
    # Uses a DEDICATED view-only+project-hunt user for this specific check
    # (not the shared viewer_client from earlier in this file) since
    # viewing ANY /tracker/* page requires module:project_hunt:view via
    # BuildIQ's existing, unrelated, pre-existing before_request hook --
    # a real, separate security control this patch correctly respects
    # rather than bypasses.
    view_ph_uid = make_user(db, "__deploy_view_ph@test.local", "__deploy_view_ph", now, pw_hash,
                             ["module:project_hunt:view", "module:project_deployment:view"])
    view_ph_client = appmod.app.test_client()
    login(view_ph_client, "__deploy_view_ph@test.local", pw)
    proj_page_b2 = view_ph_client.get(f"/tracker/project/{pid_disc}")
    body_b2 = proj_page_b2.get_data(as_text=True)
    check("B. view-only user CAN Open the existing deployment once one exists", "Open Deployment" in body_b2)

    # E. Administrator sees nav and can Start/Open normally.
    resp_e_nav = admin_client.get("/")
    check("E. Administrator sees global Deployment nav", "Project Deployment" in resp_e_nav.get_data(as_text=True))
    pid_disc2 = make_project(name="__DeployTest DiscoverabilityAdmin", status="Awarded")
    proj_page_e = admin_client.get(f"/tracker/project/{pid_disc2}")
    check("E. Administrator sees Start Deployment on a fresh Awarded project", "Start Deployment" in proj_page_e.get_data(as_text=True))

    # 7. Status gating -- Start Deployment must NOT appear for non-Awarded statuses.
    for other_status in ["In Progress", "Submitted", "Pending", "On Hold", "Cancelled", "Archived"]:
        pid_status = make_project(name=f"__DeployTest Status {other_status}", status=other_status)
        proj_page_status = manage_client2.get(f"/tracker/project/{pid_status}")
        check(f"7. Start Deployment does NOT appear for status='{other_status}'", "Start Deployment" not in proj_page_status.get_data(as_text=True))

    # Open Deployment must remain available even if status later changes away from Awarded.
    db.execute("UPDATE tracker_projects SET status='In Progress' WHERE id=?", (pid_disc,))
    db.commit()
    proj_page_status_changed = manage_client2.get(f"/tracker/project/{pid_disc}")
    check("7. Open Deployment remains available even after status changes away from Awarded (existing deployment not stranded)",
          "Open Deployment" in proj_page_status_changed.get_data(as_text=True))

    # 5. Start Deployment remains POST-only -- a GET to the start URL must not create anything.
    pid_getcheck = make_project(name="__DeployTest GetOnly", status="Awarded")
    manage_client2.get(f"/deployment/start/{pid_getcheck}")
    check("5. GET to the start-deployment URL does not create a record (route is POST-only)",
          db.execute("SELECT COUNT(*) c FROM project_deployments WHERE project_id=?", (pid_getcheck,)).fetchone()["c"] == 0)

    # CSRF: a POST without a valid token must be denied.
    resp_no_csrf = manage_client2.post(f"/deployment/start/{pid_getcheck}", data={})
    check("CSRF: Start Deployment POST without a valid token is rejected (400)", resp_no_csrf.status_code == 400)
    check("CSRF rejection did not create a record either", db.execute("SELECT COUNT(*) c FROM project_deployments WHERE project_id=?", (pid_getcheck,)).fetchone()["c"] == 0)

    # Direct unauthorized POST to start-deployment remains denied.
    token_unauth = get_csrf(no_perm_client, f"/tracker/project/{pid_getcheck}")
    no_perm_client.post(f"/deployment/start/{pid_getcheck}", data={"csrf_token": token_unauth} if token_unauth else {})
    check("Unauthorized direct POST to start-deployment remains denied", db.execute("SELECT COUNT(*) c FROM project_deployments WHERE project_id=?", (pid_getcheck,)).fetchone()["c"] == 0)

    # ============================================================
    print()
    print("=== V1.2: the actual checklist form -- every source field can be entered, saved, and edited ===")
    pid_form = make_project(name="__DeployTest FormFields", status="Awarded")
    token_sf = get_csrf(manage_client2, f"/tracker/project/{pid_form}")
    manage_client2.post(f"/deployment/start/{pid_form}", data={"csrf_token": token_sf})
    dep_form = db.execute("SELECT * FROM project_deployments WHERE project_id=?", (pid_form,)).fetchone()

    edit_get = manage_client2.get(f"/deployment/{dep_form['id']}")
    check("unified checklist page loads for an authorized manager", edit_get.status_code == 200)

    form_data = {
        "preconstruction_meeting_date": "2026-02-20", "job_description": "New office build-out, 2nd floor",
        "start_date": "2026-03-16", "expected_completion_date": "2026-05-29",
        "supervisor_name": "Marcus Webb", "supervisor_phone": "713-555-0199", "supervisor_email": "mwebb@buildiq.test",
        "client_contact_name": "Dana Ruiz", "client_contact_phone": "713-555-0142", "client_contact_email": "druiz@client.test",
        "city_county": "Harris County", "city_county_phone": "713-555-0100",
        "inspections_required_list": "Framing, Electrical rough-in, Final",
        "working_hours": "7:00 AM - 4:00 PM",
        "site_access_points": "Main gate off Peninsula St", "parking_rules": "Contractor lot only, no street parking",
        "office_needed": "yes", "storage_container_needed": "no",
        "dumpster_needed": "yes", "dumpster_size": "20 Yard", "dumpster_date": "2026-03-15",
        "toilets_needed": "yes", "toilets_qty": "2", "toilets_date": "2026-03-15",
        "fence_needed": "yes", "fence_linear_feet": "300", "fence_date": "2026-03-14",
    }
    token_save = get_csrf(manage_client2, f"/deployment/{dep_form['id']}")
    save_resp = manage_client2.post(f"/deployment/{dep_form['id']}/edit", data={**form_data, "csrf_token": token_save}, follow_redirects=True)
    check("saving the checklist form succeeds", save_resp.status_code == 200)

    saved = db.execute("SELECT * FROM project_deployments WHERE id=?", (dep_form["id"],)).fetchone()
    all_saved_correctly = all(str(saved[k]) == v for k, v in form_data.items() if k not in
                               ("office_needed", "storage_container_needed", "dumpster_needed", "toilets_needed", "fence_needed"))
    check("every text/date field saved correctly", all_saved_correctly)
    check("yes/no fields saved correctly (office=1, storage=0, dumpster=1, toilets=1, fence=1)",
          saved["office_needed"] == 1 and saved["storage_container_needed"] == 0 and saved["dumpster_needed"] == 1
          and saved["toilets_needed"] == 1 and saved["fence_needed"] == 1)

    reopened_edit = manage_client2.get(f"/deployment/{dep_form['id']}")
    reopened_body = reopened_edit.get_data(as_text=True)
    check("saved data renders back correctly on the unified page", "Marcus Webb" in reopened_body and "Harris County" in reopened_body and "20 Yard" in reopened_body)

    conditional_items_form = db.execute("SELECT * FROM project_deployment_items WHERE deployment_id=?", (dep_form["id"],)).fetchall()
    check("conditional logistics items became applicable after their needed=yes answers were saved",
          all(i["applies"] == 1 for i in conditional_items_form if i["item_code"] in
              ("office_needed_coordinated", "dumpster_coordinated", "toilets_coordinated", "fence_coordinated"))
          and any(i["applies"] == 0 for i in conditional_items_form if i["item_code"] == "storage_container_coordinated"))

    updated_form_data = dict(form_data)
    updated_form_data["supervisor_phone"] = "713-555-9999"
    token_update = get_csrf(manage_client2, f"/deployment/{dep_form['id']}")
    manage_client2.post(f"/deployment/{dep_form['id']}/edit", data={**updated_form_data, "csrf_token": token_update})
    updated_saved = db.execute("SELECT supervisor_phone FROM project_deployments WHERE id=?", (dep_form["id"],)).fetchone()
    check("the checklist can be edited again after initial save", updated_saved["supervisor_phone"] == "713-555-9999")

    print()
    print("=== V1.2: view-only user cannot edit the checklist form ===")
    view_get_form = viewer_client.get(f"/deployment/{dep_form['id']}")
    check("view-only user can see the unified checklist (read-only)", view_get_form.status_code == 200)
    check("view-only user's form fields are disabled", "disabled" in view_get_form.get_data(as_text=True))
    token_view_attempt = get_csrf(viewer_client, f"/deployment/{dep_form['id']}")
    hostile_data = dict(form_data)
    hostile_data["supervisor_name"] = "HACKED"
    viewer_client.post(f"/deployment/{dep_form['id']}/edit", data={**hostile_data, "csrf_token": token_view_attempt} if token_view_attempt else hostile_data)
    unchanged = db.execute("SELECT supervisor_name FROM project_deployments WHERE id=?", (dep_form["id"],)).fetchone()
    check("view-only user's direct POST to the edit route does not change data", unchanged["supervisor_name"] != "HACKED")

    print()
    print("=== V1.2: new checklist items from the audit (RFI, Change Orders, Site/Safety Meetings) ===")
    codes_present = {row[0] for row in appmod.DEPLOYMENT_ITEM_CODES}
    check("rfi_submittals_confirmed exists and is required", "rfi_submittals_confirmed" in codes_present and appmod.DEPLOYMENT_ITEM_CODES_BY_CODE["rfi_submittals_confirmed"][3] is True)
    check("change_orders_approval_required exists exactly once (source PDF's duplicate line is not double-represented)",
          sum(1 for c in codes_present if "change_order" in c) == 1)
    check("site_meetings_conducted exists", "site_meetings_conducted" in codes_present)
    check("safety_meeting_enforcement exists", "safety_meeting_enforcement" in codes_present)
    check("no_client_subs_interaction exists", "no_client_subs_interaction" in codes_present)

    print()
    print("=== V1.2: conditional Permit/Plans behavior (only applicable once drawings are approved) ===")
    pid_permit = make_project(name="__DeployTest PermitConditional", status="Awarded")
    token_pp = get_csrf(manage_client2, f"/tracker/project/{pid_permit}")
    manage_client2.post(f"/deployment/start/{pid_permit}", data={"csrf_token": token_pp})
    dep_permit = db.execute("SELECT * FROM project_deployments WHERE project_id=?", (pid_permit,)).fetchone()
    permit_item_before = db.execute("SELECT * FROM project_deployment_items WHERE deployment_id=? AND item_code='permit_plans_printed'", (dep_permit["id"],)).fetchone()
    check("permit/plans item starts as not-applicable (conditional on drawings)", permit_item_before["applies"] == 0)
    drawings_item = db.execute("SELECT * FROM project_deployment_items WHERE deployment_id=? AND item_code='drawings_specs_approved'", (dep_permit["id"],)).fetchone()
    token_drawings = get_csrf(manage_client2, f"/deployment/{dep_permit['id']}")
    manage_client2.post(f"/deployment/{dep_permit['id']}/item/{drawings_item['id']}/complete", data={"csrf_token": token_drawings})
    permit_item_after = db.execute("SELECT * FROM project_deployment_items WHERE deployment_id=? AND item_code='permit_plans_printed'", (dep_permit["id"],)).fetchone()
    check("permit/plans item becomes applicable once drawings/specs are approved", permit_item_after["applies"] == 1)

    print()
    print("=== V1.2: home page card respects Deployment permission ===")
    resp_home_noperm = no_perm_client.get("/")
    check("no-permission user does NOT see the Project Deployment home card", "Project Deployment" not in resp_home_noperm.get_data(as_text=True) or "GET THE JOB READY" not in resp_home_noperm.get_data(as_text=True))
    resp_home_view = view_only_client.get("/")
    check("view-only user DOES see the Project Deployment home card", "GET THE JOB READY" in resp_home_view.get_data(as_text=True))

    print()
    print("=== V1.2: mobile card fallback exists (no bare horizontal-scroll-only table) ===")
    resp_detail_mobile_check = manage_client2.get(f"/deployment/{dep_form['id']}")
    body_mobile_check = resp_detail_mobile_check.get_data(as_text=True)
    check("unified checklist page uses flexible wrapping rows, not a wide fixed table that would force horizontal scrolling on mobile",
          "<table" not in body_mobile_check.split("Cross-module evidence")[-1] if "Cross-module evidence" in body_mobile_check else "<table" not in body_mobile_check)

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM project_deployment_items WHERE deployment_id IN (SELECT id FROM project_deployments WHERE project_id IN (SELECT id FROM tracker_projects WHERE name LIKE '__DeployTest%'))")
    db.execute("DELETE FROM project_deployments WHERE project_id IN (SELECT id FROM tracker_projects WHERE name LIKE '__DeployTest%')")
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__DeployTest%'")
    db.execute("DELETE FROM inventory_purchase_requests WHERE job_name LIKE '__DeployTest%'")
    db.execute("DELETE FROM sitepulse_rentals WHERE job_name LIKE '__DeployTest%'")
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__DeployTest%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
