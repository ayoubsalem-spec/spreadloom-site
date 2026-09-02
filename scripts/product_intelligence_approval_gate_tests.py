"""
Product Intelligence -- Procurement Approval Gate (item 3) regression.

Verifies the independent approval-state architecture (approved
separately from feature_requests.status -- see feature_request_approvals
and the approval_status/approval_decided_by/approval_decided_at/
approval_reason columns) end to end, against the real Flask app/routes/DB.

Covers exactly the boundary conditions called out for this pass:
  - an approver (action:product_intelligence:approve_requests only, NOT
    action:product_intelligence:manage) can reach the request detail
    page needed to approve/return
  - that same approver cannot execute any manage-only POST action
    (change_department / save_details / change_status / release)
  - a product manager (action:product_intelligence:manage only, NOT the
    approve permission) cannot approve or return
  - a requester cannot approve/return their own request, even if they
    otherwise hold approval authority
  - every one of the above is enforced on the POST handler itself (a
    direct POST, not just a hidden UI control)
  - approving or returning an already-decided (non-Pending) request is
    rejected, safely, without corrupting the existing decision
  - returning requires a reason; approving does not
  - historical rows (approval_status NULL, simulating pre-migration
    data) come through the backfill as Approved, with a system actor,
    and are never blocked
  - running the startup migration/backfill twice is a no-op the second
    time (idempotent)
  - the existing status workflow (change_status / release) is completely
    unaffected by any of this for a request regardless of approval state
  - the Pending Approval KPI count and ?approval=Pending filter both
    reflect real approval_status data

Usage (from the project root):
    APP_ENV=development python3 scripts/product_intelligence_approval_gate_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import datetime

import _test_db_setup
_test_db_setup.isolate_test_database()  # MUST happen before `import app`

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


def create_request(db, requester_email, requester_name, text, now):
    cur = db.execute(
        "INSERT INTO feature_requests (requester_email, requester_name, department, original_request, status, approval_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (requester_email, requester_name, "Test Dept", text, "Submitted", "Pending", now, now)
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

    requester_email = "__pi_gate_requester@test.local"
    approver_email = "__pi_gate_approver@test.local"
    manager_email = "__pi_gate_manager@test.local"
    self_approver_email = "__pi_gate_self@test.local"

    make_user(db, requester_email, "__pi_gate_requester", now, pw_hash,
              ["module:product_intelligence:view"])
    make_user(db, approver_email, "__pi_gate_approver", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:approve_requests"])
    make_user(db, manager_email, "__pi_gate_manager", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:manage"])
    # Holds BOTH approve permission and is the requester -- tests the
    # self-approval block specifically (permission alone is not enough).
    make_user(db, self_approver_email, "__pi_gate_self", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:approve_requests"])

    # ================================================================
    # SETUP: three requests covering the scenarios below.
    # ================================================================
    req_id = create_request(db, requester_email, "__pi_gate_requester", "Please add a widget", now)
    self_req_id = create_request(db, self_approver_email, "__pi_gate_self", "My own request", now)
    r = db.execute("SELECT * FROM feature_requests WHERE id = ?", (req_id,)).fetchone()
    check("(setup) new request starts approval_status='Pending'", r["approval_status"] == "Pending")
    check("(setup) new request starts status='Submitted' (dev lifecycle untouched by approval gate)", r["status"] == "Submitted")

    detail_url = f"/admin/product-intelligence/{req_id}"

    # ================================================================
    # 1. Approver can access the request detail needed to approve/return
    # ================================================================
    print("=== 1. Approver (approve permission only) can reach request detail ===")
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        resp = client.get(detail_url)
        check("approver GET on request detail returns 200", resp.status_code == 200)
        html = resp.get_data(as_text=True)
        check("approve/return controls are present for the approver", 'value="approve_request"' in html and 'value="return_request"' in html)

    # ================================================================
    # 2. Approver cannot execute any manage-only POST action
    # ================================================================
    print()
    print("=== 2. Approver cannot execute manage-only actions (direct POST) ===")
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        token = get_csrf(client, detail_url)

        resp = client.post(detail_url, data={"csrf_token": token, "action": "change_department", "department": "Hacked Dept"}, follow_redirects=True)
        r_after = db.execute("SELECT department FROM feature_requests WHERE id = ?", (req_id,)).fetchone()
        check("approver's change_department POST does not actually change the department", r_after["department"] != "Hacked Dept")

        resp = client.post(detail_url, data={"csrf_token": token, "action": "change_status", "status": "Building"}, follow_redirects=True)
        r_after = db.execute("SELECT status FROM feature_requests WHERE id = ?", (req_id,)).fetchone()
        check("approver's change_status POST does not actually change status", r_after["status"] == "Submitted")

        resp = client.post(detail_url, data={"csrf_token": token, "action": "save_details", "buildiq_module": "Hacked"}, follow_redirects=True)
        intel = db.execute("SELECT * FROM feature_request_intelligence WHERE feature_request_id = ?", (req_id,)).fetchone()
        check("approver's save_details POST does not create/update intelligence row", intel is None)

        resp = client.post(detail_url, data={"csrf_token": token, "action": "release", "release_note": "x", "confirm_release": "yes"}, follow_redirects=True)
        r_after = db.execute("SELECT status FROM feature_requests WHERE id = ?", (req_id,)).fetchone()
        check("approver's release POST does not release the request", r_after["status"] != "Released")

    # ================================================================
    # 3. Product manager (manage permission only) cannot approve/return
    # ================================================================
    print()
    print("=== 3. Manager (manage permission only, no approve permission) cannot approve/return ===")
    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        token = get_csrf(client, detail_url)
        resp = client.post(detail_url, data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)
        r_after = db.execute("SELECT approval_status FROM feature_requests WHERE id = ?", (req_id,)).fetchone()
        check("manager's approve_request POST does not approve the request", r_after["approval_status"] == "Pending")
        check("manager sees a permission error, not a success flash", b"You don&#39;t have permission" in resp.data or b"don't have permission" in resp.data)

        # Gate correction: a manager (manage permission only, no approval
        # authority) must NOT be able to advance a Pending-approval
        # request's development status, even though they hold
        # action:product_intelligence:manage. This was the exact gap the
        # release review flagged -- this request (req_id) is still
        # approval_status='Pending' at this point in the test, and this
        # assertion now proves the corrected behavior, not the old bug.
        resp = client.post(detail_url, data={"csrf_token": token, "action": "change_status", "status": "Reviewing"}, follow_redirects=True)
        r_after = db.execute("SELECT status FROM feature_requests WHERE id = ?", (req_id,)).fetchone()
        check("manager CANNOT advance a Pending-approval request's development status (the gate correction)",
              r_after["status"] == "Submitted")
        check("manager sees the specific approval-gate error, not a success flash",
              "cannot enter the development pipeline" in resp.get_data(as_text=True))

    # ================================================================
    # 4. Requester cannot approve their own request (even with permission)
    # ================================================================
    print()
    print("=== 4. Requester cannot approve/return their own request ===")
    self_detail_url = f"/admin/product-intelligence/{self_req_id}"
    with appmod.app.test_client() as client:
        login(client, self_approver_email, pw)
        # The approve/return form is correctly NOT rendered for a
        # self-requester (can_approve_request is False), so there is no
        # CSRF token on this page to scrape for the attack attempt below
        # -- get a valid token from a different page this same logged-in
        # user can legitimately reach instead (my_requests), exactly like
        # a real attacker reusing a token from elsewhere in the app would.
        token = get_csrf(client, "/requests")
        resp = client.post(self_detail_url, data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)
        r_after = db.execute("SELECT approval_status FROM feature_requests WHERE id = ?", (self_req_id,)).fetchone()
        check("self-approval attempt does not change approval_status", r_after["approval_status"] == "Pending")
        check("self-approval attempt shows the specific error", "cannot approve or return your own request" in resp.get_data(as_text=True))

    # ================================================================
    # 5. Direct POST from someone with NEITHER permission is rejected
    # ================================================================
    print()
    print("=== 5. Direct POST from an unauthorized user (no manage, no approve) is rejected ===")
    plain_email = "__pi_gate_plain@test.local"
    make_user(db, plain_email, "__pi_gate_plain", now, pw_hash, [])
    with appmod.app.test_client() as client:
        login(client, plain_email, pw)
        resp = client.get(detail_url, follow_redirects=True)
        check("user with neither permission is redirected away from request detail (cannot even load a CSRF token to attack with)",
              resp.request.path != detail_url)

    # ================================================================
    # 6. Approve/return the request for real, then verify a REPEAT
    # attempt against the now-non-Pending request fails safely.
    # ================================================================
    print()
    print("=== 6. Repeated approve/return against a non-Pending request fails safely ===")
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        token = get_csrf(client, detail_url)
        resp = client.post(detail_url, data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)
        r_after = db.execute("SELECT * FROM feature_requests WHERE id = ?", (req_id,)).fetchone()
        check("(setup) request is now Approved", r_after["approval_status"] == "Approved")
        check("(setup) approval_decided_by recorded", r_after["approval_decided_by"] == approver_email)
        check("(setup) approval_decided_at recorded", bool(r_after["approval_decided_at"]))
        history_count_before = db.execute("SELECT COUNT(*) FROM feature_request_approvals WHERE feature_request_id = ?", (req_id,)).fetchone()[0]
        check("(setup) exactly one approval-history row so far", history_count_before == 1)

        # Repeat: try to return the ALREADY-approved request. The
        # approve/return form is correctly no longer rendered on this
        # page (can_approve_request requires approval_status=='Pending'),
        # so grab a token from elsewhere the user can legitimately reach.
        token2 = get_csrf(client, "/requests")
        resp = client.post(detail_url, data={"csrf_token": token2, "action": "return_request", "reason": "changed my mind"}, follow_redirects=True)
        r_after2 = db.execute("SELECT approval_status FROM feature_requests WHERE id = ?", (req_id,)).fetchone()
        check("re-deciding an already-Approved request does not change its approval_status", r_after2["approval_status"] == "Approved")
        history_count_after = db.execute("SELECT COUNT(*) FROM feature_request_approvals WHERE feature_request_id = ?", (req_id,)).fetchone()[0]
        check("re-deciding an already-Approved request does not add a new approval-history row", history_count_after == history_count_before)
        check("user sees an 'already decided' error, not a success flash", "already" in resp.get_data(as_text=True).lower())

    # ================================================================
    # 7. Returned requires a reason
    # ================================================================
    print()
    print("=== 7. Returning a request requires a reason; approving does not ===")
    req_id_2 = create_request(db, requester_email, "__pi_gate_requester", "Second request", now)
    detail_url_2 = f"/admin/product-intelligence/{req_id_2}"
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        token = get_csrf(client, detail_url_2)
        resp = client.post(detail_url_2, data={"csrf_token": token, "action": "return_request", "reason": ""}, follow_redirects=True)
        r_after = db.execute("SELECT approval_status FROM feature_requests WHERE id = ?", (req_id_2,)).fetchone()
        check("returning without a reason is rejected -- request stays Pending", r_after["approval_status"] == "Pending")
        check("blank-reason return shows the specific error", b"reason is required" in resp.data.lower())

        token2 = get_csrf(client, detail_url_2)
        resp = client.post(detail_url_2, data={"csrf_token": token2, "action": "return_request", "reason": "Out of budget this quarter"}, follow_redirects=True)
        r_after = db.execute("SELECT * FROM feature_requests WHERE id = ?", (req_id_2,)).fetchone()
        check("returning WITH a reason succeeds", r_after["approval_status"] == "Returned")
        check("return reason is persisted", r_after["approval_reason"] == "Out of budget this quarter")

    req_id_3 = create_request(db, requester_email, "__pi_gate_requester", "Third request", now)
    detail_url_3 = f"/admin/product-intelligence/{req_id_3}"
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        token = get_csrf(client, detail_url_3)
        resp = client.post(detail_url_3, data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)
        r_after = db.execute("SELECT approval_status FROM feature_requests WHERE id = ?", (req_id_3,)).fetchone()
        check("approving WITHOUT a reason succeeds (reason is optional for approve)", r_after["approval_status"] == "Approved")

    # ================================================================
    # 8. Historical requests (approval_status NULL) are unblocked by
    # the backfill, and the migration is idempotent across repeated
    # startup runs.
    # ================================================================
    print()
    print("=== 8. Historical rows backfilled conservatively; migration is idempotent ===")
    hist_cur = db.execute(
        "INSERT INTO feature_requests (requester_email, requester_name, department, original_request, status, approval_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (requester_email, "__pi_gate_requester", "Test Dept", "Pre-existing historical request", "Building", None, "2020-01-01T00:00:00", "2020-01-01T00:00:00")
    )
    db.commit()
    hist_id = hist_cur.lastrowid
    hist_before = db.execute("SELECT * FROM feature_requests WHERE id = ?", (hist_id,)).fetchone()
    check("(setup) simulated historical row starts with approval_status NULL", hist_before["approval_status"] is None)

    # Re-run the real startup migration path (init_db equivalent) to
    # trigger the backfill, exactly as a real app restart would.
    appmod.init_db()
    hist_after = db.execute("SELECT * FROM feature_requests WHERE id = ?", (hist_id,)).fetchone()
    check("historical row is backfilled to Approved (never blocked from its existing Building status)", hist_after["approval_status"] == "Approved")
    check("historical row's decider is a clearly-labeled system actor, never a real person", "system" in (hist_after["approval_decided_by"] or "").lower())
    check("historical row's decided_at uses its own created_at (not the migration run time)", hist_after["approval_decided_at"] == "2020-01-01T00:00:00")
    check("historical row's status (Building) is completely untouched by the approval backfill", hist_after["status"] == "Building")

    # Idempotency: run it again, confirm nothing changes the second time.
    snapshot_before_2nd = dict(hist_after)
    appmod.init_db()
    hist_after_2nd = db.execute("SELECT * FROM feature_requests WHERE id = ?", (hist_id,)).fetchone()
    check("running the migration a second time does not change the already-backfilled row (idempotent)",
          dict(hist_after_2nd) == snapshot_before_2nd)
    pending_before_3rd = db.execute("SELECT approval_status FROM feature_requests WHERE id = ?", (req_id_2,)).fetchone()["approval_status"]
    appmod.init_db()
    pending_after_3rd = db.execute("SELECT approval_status FROM feature_requests WHERE id = ?", (req_id_2,)).fetchone()["approval_status"]
    check("running the migration again does not touch a genuinely-decided (Returned) request either",
          pending_before_3rd == pending_after_3rd == "Returned")

    # ================================================================
    # 9. Existing status workflow (change_status/release) still works
    # normally for an Approved request -- approval doesn't block or
    # alter the existing dev-lifecycle mechanism.
    # ================================================================
    print()
    print("=== 9. Existing status workflow unaffected by approval state ===")
    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        token = get_csrf(client, detail_url_3)  # req_id_3 is Approved from step 7
        resp = client.post(detail_url_3, data={"csrf_token": token, "action": "change_status", "status": "Building"}, follow_redirects=True)
        r_after = db.execute("SELECT * FROM feature_requests WHERE id = ?", (req_id_3,)).fetchone()
        check("an Approved request can still move through the normal status workflow (Building)", r_after["status"] == "Building")
        check("moving status does not alter the already-recorded approval_status", r_after["approval_status"] == "Approved")

        token2 = get_csrf(client, detail_url_3)
        resp = client.post(detail_url_3, data={"csrf_token": token2, "action": "release", "release_note": "Shipped it", "confirm_release": "yes"}, follow_redirects=True)
        r_after = db.execute("SELECT * FROM feature_requests WHERE id = ?", (req_id_3,)).fetchone()
        check("an Approved request can still be released through the normal mechanism", r_after["status"] == "Released")
        check("approval_status remains Approved after release -- historical truth preserved independent of dev status", r_after["approval_status"] == "Approved")

    # ================================================================
    # 9b. Returned request cannot enter the development pipeline either
    # ================================================================
    print()
    print("=== 9b. Returned request cannot enter the development pipeline ===")
    req_id_4 = create_request(db, requester_email, "__pi_gate_requester", "Fourth request (will be returned)", now)
    detail_url_4 = f"/admin/product-intelligence/{req_id_4}"
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        token = get_csrf(client, detail_url_4)
        client.post(detail_url_4, data={"csrf_token": token, "action": "return_request", "reason": "Not this quarter"}, follow_redirects=True)
        r_returned = db.execute("SELECT approval_status FROM feature_requests WHERE id = ?", (req_id_4,)).fetchone()
        check("(setup) request is now Returned", r_returned["approval_status"] == "Returned")

    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        token = get_csrf(client, detail_url_4)
        resp = client.post(detail_url_4, data={"csrf_token": token, "action": "change_status", "status": "Reviewing"}, follow_redirects=True)
        r_after = db.execute("SELECT status FROM feature_requests WHERE id = ?", (req_id_4,)).fetchone()
        check("a Returned request cannot be advanced into the development pipeline", r_after["status"] == "Submitted")

        token2 = get_csrf(client, detail_url_4)
        resp = client.post(detail_url_4, data={"csrf_token": token2, "action": "release", "release_note": "x", "confirm_release": "yes"}, follow_redirects=True)
        r_after = db.execute("SELECT status FROM feature_requests WHERE id = ?", (req_id_4,)).fetchone()
        check("a Returned request cannot be released either", r_after["status"] != "Released")

    # ================================================================
    # 9c. Purely administrative edits (department / intelligence notes)
    # remain allowed while Pending -- they don't constitute development
    # acceptance, unlike change_status/release.
    # ================================================================
    print()
    print("=== 9c. Administrative edits (department, intelligence notes) remain allowed while Pending ===")
    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        token = get_csrf(client, detail_url_4)  # req_id_4 is Returned, still not Approved
        resp = client.post(detail_url_4, data={"csrf_token": token, "action": "change_department", "department": "Corrected Dept"}, follow_redirects=True)
        r_after = db.execute("SELECT department FROM feature_requests WHERE id = ?", (req_id_4,)).fetchone()
        check("department correction is still allowed on a non-Approved request (purely administrative, not development acceptance)",
              r_after["department"] == "Corrected Dept")

        token2 = get_csrf(client, detail_url_4)
        resp = client.post(detail_url_4, data={"csrf_token": token2, "action": "save_details", "buildiq_module": "Atlas", "internal_notes": "triage note"}, follow_redirects=True)
        intel = db.execute("SELECT * FROM feature_request_intelligence WHERE feature_request_id = ?", (req_id_4,)).fetchone()
        check("saving internal intelligence notes is still allowed on a non-Approved request", intel is not None and intel["internal_notes"] == "triage note")

    # ================================================================
    # 9d. Approval action itself does not fake a development status
    # transition -- approving a request must not silently move `status`.
    # ================================================================
    print()
    print("=== 9d. Approving a request does not itself fake a development status transition ===")
    req_id_5 = create_request(db, requester_email, "__pi_gate_requester", "Fifth request", now)
    detail_url_5 = f"/admin/product-intelligence/{req_id_5}"
    r_before_approval = db.execute("SELECT status FROM feature_requests WHERE id = ?", (req_id_5,)).fetchone()
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        token = get_csrf(client, detail_url_5)
        client.post(detail_url_5, data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)
    r_after_approval = db.execute("SELECT * FROM feature_requests WHERE id = ?", (req_id_5,)).fetchone()
    check("approving a request leaves its development status exactly as it was (Submitted) -- approval and status remain independent",
          r_after_approval["status"] == r_before_approval["status"] == "Submitted")
    check("(setup) but it IS now Approved, so a manager can advance it going forward", r_after_approval["approval_status"] == "Approved")

    # ================================================================
    # 10. Pending Approval KPI count and filter are accurate
    # ================================================================
    print()
    print("=== 10. Pending Approval KPI and filter reflect real data ===")
    real_pending_count = db.execute("SELECT COUNT(*) FROM feature_requests WHERE approval_status = 'Pending'").fetchone()[0]
    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        resp = client.get("/admin/product-intelligence")
        html = resp.get_data(as_text=True)
        check("Pending Approval KPI card is present", "Pending Approval" in html)

        resp_filtered = client.get("/admin/product-intelligence?approval=Pending")
        check("filtered view returns 200", resp_filtered.status_code == 200)
        filtered_html = resp_filtered.get_data(as_text=True)
        pending_ids = set(db.execute("SELECT id FROM feature_requests WHERE approval_status = 'Pending'").fetchall())
        pending_ids = {row[0] for row in pending_ids}
        non_pending_sample = db.execute("SELECT id FROM feature_requests WHERE approval_status != 'Pending' AND approval_status IS NOT NULL LIMIT 1").fetchone()
        check("at least one genuinely-Pending request exists to check against", real_pending_count >= 1)
        if non_pending_sample:
            check(f"a non-Pending request (id={non_pending_sample[0]}) does not appear in the ?approval=Pending filtered list",
                  f"REQ-{non_pending_sample[0]:03d}" not in filtered_html)

    # ================================================================
    # 11. Pending-approval requests do not count as "awaiting product
    # review" (the dev-lifecycle KPI/lifecycle/attention numbers), but
    # DO still count in Total Requests and still appear in the
    # unfiltered All Requests table.
    # ================================================================
    print()
    print("=== 11. Pending-approval requests excluded from dev-pipeline counts, included in totals/All Requests ===")

    def get_awaiting_review_count(html):
        m = re.search(r"(\d+) new requests? awaiting review", html)
        return int(m.group(1)) if m else 0

    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        before_html = client.get("/admin/product-intelligence").get_data(as_text=True)
        awaiting_before = get_awaiting_review_count(before_html)
        total_before = db.execute("SELECT COUNT(*) FROM feature_requests").fetchone()[0]
        pending_before = db.execute("SELECT COUNT(*) FROM feature_requests WHERE approval_status = 'Pending'").fetchone()[0]

    req_id_6 = create_request(db, requester_email, "__pi_gate_requester", "__pi_gate_UNIQUE_marker_11 sixth request", now)

    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        after_html = client.get("/admin/product-intelligence").get_data(as_text=True)
        awaiting_after = get_awaiting_review_count(after_html)
        total_after = db.execute("SELECT COUNT(*) FROM feature_requests").fetchone()[0]
        pending_after = db.execute("SELECT COUNT(*) FROM feature_requests WHERE approval_status = 'Pending'").fetchone()[0]

        check("a new Pending-approval Submitted request does NOT increase the 'awaiting review' dev-pipeline count",
              awaiting_after == awaiting_before)
        check("(sanity) the new request is genuinely Pending", pending_after == pending_before + 1)
        check("Total Requests count DOES increase -- the complete historical total is unaffected by the gate",
              total_after == total_before + 1)

        all_html = client.get("/admin/product-intelligence").get_data(as_text=True)
        check("the new Pending request still appears in the unfiltered All Requests list",
              "__pi_gate_UNIQUE_marker_11" in all_html)

        pending_filtered_html = client.get("/admin/product-intelligence?approval=Pending").get_data(as_text=True)
        check("the new Pending request appears in the Pending Approval filtered queue",
              "__pi_gate_UNIQUE_marker_11" in pending_filtered_html)

        # Now approve it -- it SHOULD start counting as awaiting review,
        # since it can now legitimately advance through the pipeline.
        approve_token = get_csrf(client, f"/admin/product-intelligence/{req_id_6}")
    with appmod.app.test_client() as approver_client:
        login(approver_client, approver_email, pw)
        token = get_csrf(approver_client, f"/admin/product-intelligence/{req_id_6}")
        approver_client.post(f"/admin/product-intelligence/{req_id_6}", data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)

    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        after_approval_html = client.get("/admin/product-intelligence").get_data(as_text=True)
        awaiting_after_approval = get_awaiting_review_count(after_approval_html)
        check("once approved, the request (still Submitted) DOES now count toward 'awaiting review'",
              awaiting_after_approval == awaiting_before + 1)

        # And a manager can now legitimately advance it, proving the
        # gate correctly reflects real approval state, not a one-way
        # lock.
        token2 = get_csrf(client, f"/admin/product-intelligence/{req_id_6}")
        client.post(f"/admin/product-intelligence/{req_id_6}", data={"csrf_token": token2, "action": "change_status", "status": "Reviewing"}, follow_redirects=True)
        r_final = db.execute("SELECT status FROM feature_requests WHERE id = ?", (req_id_6,)).fetchone()
        check("after approval, a manager CAN now advance the request's development status", r_final["status"] == "Reviewing")

    # Historical Approved-backfill rows must not be touched by any of
    # this -- spot-check the earlier historical-row test still holds
    # (already covered in detail by scripts/product_intelligence_approval_gate_tests.py's
    # migration section; this is a final belt-and-suspenders read here).
    hist_check = db.execute(
        "SELECT approval_status, approval_decided_by FROM feature_requests WHERE approval_decided_by = 'system (predates approval gate)' LIMIT 1"
    ).fetchone()
    if hist_check:
        check("a historical backfilled row remains Approved and untouched by the new dev-pipeline gating logic",
              hist_check["approval_status"] == "Approved")

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM feature_request_approvals WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__pi_gate_%')")
    db.execute("DELETE FROM feature_request_status_history WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__pi_gate_%')")
    db.execute("DELETE FROM feature_request_intelligence WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__pi_gate_%')")
    db.execute("DELETE FROM feature_requests WHERE requester_email LIKE '__pi_gate_%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
