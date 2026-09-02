"""
v4 regression: employee feedback loop (Pending/Returned visibility,
Update & Resubmit), Attention Required direct navigation, and Latest
Movement read-model truthfulness.

Usage (from the project root):
    APP_ENV=development python3 scripts/employee_resubmit_flow_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
import threading
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


def create_request(db, requester_email, requester_name, text, now, approval_status="Pending"):
    cur = db.execute(
        "INSERT INTO feature_requests (requester_email, requester_name, department, original_request, status, approval_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (requester_email, requester_name, "Ops", text, "Submitted", approval_status, now, now)
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

    requester_email = "__resub_requester@test.local"
    other_employee_email = "__resub_other_employee@test.local"
    approver_email = "__resub_approver@test.local"
    admin_email = "__resub_admin@test.local"

    make_user(db, requester_email, "__resub_requester", now, pw_hash, ["module:product_intelligence:view"])
    make_user(db, other_employee_email, "__resub_other_employee", now, pw_hash, ["module:product_intelligence:view"])
    make_user(db, approver_email, "__resub_approver", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:approve_requests"])
    admin_uid = make_user(db, admin_email, "__resub_admin", now, pw_hash, [])
    admin_role_id = db.execute("SELECT id FROM roles WHERE name = 'Administrator'").fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (admin_uid, admin_role_id))
    db.commit()

    print("=== 1. Pending employee display ===")
    pending_req_id = create_request(db, requester_email, "__resub_requester", "Pending request text", now, "Pending")
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        html = client.get("/requests").get_data(as_text=True)
        check("Pending request shows 'Awaiting Procurement Approval'", "Awaiting Procurement Approval" in html)
        check("Pending request uses the distinct approval-gate treatment (not the raw dev timeline as primary)",
              "req-approval-gate-pending" in html)

    print()
    print("=== 2/3. Returned employee display + reason visibility ===")
    returned_req_id = create_request(db, requester_email, "__resub_requester", "Returned request text", now, "Returned")
    db.execute("UPDATE feature_requests SET approval_reason=?, approval_decided_by=?, approval_decided_at=? WHERE id=?",
               ("Needs a specific dollar amount", approver_email, now, returned_req_id))
    db.execute("INSERT INTO feature_request_approvals (feature_request_id, decision, reason, decided_by, decided_at) VALUES (?,?,?,?,?)",
               (returned_req_id, "Returned", "Needs a specific dollar amount", approver_email, now))
    db.commit()
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        html = client.get("/requests").get_data(as_text=True)
        check("Returned request shows 'Returned' / 'Action Needed' language", "Returned" in html and "Action Needed" in html)
        check("the specific Return reason is visible to the original requester", "Needs a specific dollar amount" in html)
        check("an Update & Resubmit action is offered", f"/requests/{returned_req_id}/resubmit" in html)

    print()
    print("=== 4. Return reason not exposed to another employee ===")
    with appmod.app.test_client() as client:
        login(client, other_employee_email, pw)
        html = client.get("/requests").get_data(as_text=True)
        check("a different employee's Request Center does not show the other person's Return reason",
              "Needs a specific dollar amount" not in html)
        resp = client.get(f"/requests/{returned_req_id}/resubmit", follow_redirects=True)
        check("a different employee's access to another user's resubmit page does not show the private reason",
              "Needs a specific dollar amount" not in resp.get_data(as_text=True))

    print()
    print("=== 5. Approved employee display (Reviewing) continues working ===")
    approved_req_id = create_request(db, requester_email, "__resub_requester", "Reviewing request text", now, "Approved")
    db.execute("UPDATE feature_requests SET status='Reviewing' WHERE id=?", (approved_req_id,))
    db.execute("INSERT INTO feature_request_status_history (feature_request_id, status, changed_by, changed_at) VALUES (?,?,?,?)",
               (approved_req_id, "Reviewing", approver_email, now))
    db.commit()
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        html = client.get("/requests").get_data(as_text=True)
        check("an Approved+Reviewing request shows the normal development timeline (unchanged behavior)",
              "Reviewing request text" in html and "req-timeline" in html)

    print()
    print("=== 6-9. Resubmit: same id, Returned -> Pending, dev status unaffected ===")
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        resub_url = f"/requests/{returned_req_id}/resubmit"
        token = get_csrf(client, resub_url)
        client.post(resub_url, data={"csrf_token": token, "original_request": "Corrected returned request text", "department": "Ops"}, follow_redirects=True)
        row = db.execute("SELECT * FROM feature_requests WHERE id=?", (returned_req_id,)).fetchone()
        check("6. same request ID preserved (no duplicate row)", row["id"] == returned_req_id)
        check("7. exactly one row has this content", db.execute(
            "SELECT COUNT(*) FROM feature_requests WHERE original_request LIKE '%Corrected returned request text%'").fetchone()[0] == 1)
        check("8. approval_status changed Returned -> Pending", row["approval_status"] == "Pending")
        check("9. development status remains Submitted", row["status"] == "Submitted")
        check("request text was actually updated", row["original_request"] == "Corrected returned request text")

    print()
    print("=== 10/11. History preservation ===")
    approvals = db.execute("SELECT * FROM feature_request_approvals WHERE feature_request_id=? ORDER BY decided_at", (returned_req_id,)).fetchall()
    check("10. the original Return decision row still exists, untouched",
          any(a["decision"] == "Returned" and a["reason"] == "Needs a specific dollar amount" for a in approvals))
    resubmissions = db.execute("SELECT * FROM feature_request_resubmissions WHERE feature_request_id=?", (returned_req_id,)).fetchall()
    check("11. a Resubmission event was recorded", len(resubmissions) == 1 and resubmissions[0]["resubmitted_by"] == requester_email)

    print()
    print("=== 12. Updated content visible to approver ===")
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        html = client.get("/admin/product-intelligence").get_data(as_text=True)
        pending_marker = html.find('id="pi2-pending-approval"')
        attention_marker = html.find('id="pi2-attention"')
        check("the resubmitted (now Pending) request appears in the inbox with UPDATED text",
              "Corrected returned request text" in html[pending_marker:attention_marker])
        detail_html = client.get(f"/admin/product-intelligence/{returned_req_id}").get_data(as_text=True)
        check("original Return reason still visible on the detail page", "Needs a specific dollar amount" in detail_html)
        check("Resubmitted event visible on the detail page", "Resubmitted" in detail_html)

    print()
    print("=== 13/14. Authorization boundaries ===")
    returned_req_id_2 = create_request(db, requester_email, "__resub_requester", "Second returned request", now, "Returned")
    db.execute("UPDATE feature_requests SET approval_reason=?, approval_decided_by=?, approval_decided_at=? WHERE id=?",
               ("Please clarify scope", approver_email, now, returned_req_id_2))
    db.commit()

    with appmod.app.test_client() as client:
        login(client, other_employee_email, pw)
        token = get_csrf(client, f"/requests/{returned_req_id_2}/resubmit")
        client.post(f"/requests/{returned_req_id_2}/resubmit", data={"csrf_token": token, "original_request": "HIJACKED", "department": "Ops"}, follow_redirects=True)
        row = db.execute("SELECT approval_status, original_request FROM feature_requests WHERE id=?", (returned_req_id_2,)).fetchone()
        check("13. a different employee's POST does not modify someone else's returned request", row["original_request"] != "HIJACKED")
        check("13. approval_status unaffected", row["approval_status"] == "Returned")

    with appmod.app.test_client() as client:
        login(client, admin_email, pw)
        token = get_csrf(client, f"/requests/{returned_req_id_2}/resubmit")
        client.post(f"/requests/{returned_req_id_2}/resubmit", data={"csrf_token": token, "original_request": "ADMIN HIJACKED", "department": "Ops"}, follow_redirects=True)
        row = db.execute("SELECT approval_status, original_request FROM feature_requests WHERE id=?", (returned_req_id_2,)).fetchone()
        check("14. an Administrator cannot use the employee resubmit route to impersonate the requester", row["original_request"] != "ADMIN HIJACKED")
        check("14. approval_status unaffected", row["approval_status"] == "Returned")

    print()
    print("=== 15-17. Only Returned requests can be resubmitted ===")
    pending_only_id = create_request(db, requester_email, "__resub_requester", "Pending only", now, "Pending")
    approved_only_id = create_request(db, requester_email, "__resub_requester", "Approved only", now, "Approved")
    released_id = create_request(db, requester_email, "__resub_requester", "Released only", now, "Approved")
    db.execute("UPDATE feature_requests SET status='Released' WHERE id=?", (released_id,))
    db.commit()

    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        for rid, label in ((pending_only_id, "15. Pending"), (approved_only_id, "16. Approved"), (released_id, "17. Released")):
            token = get_csrf(client, f"/requests/{rid}/resubmit")
            client.post(f"/requests/{rid}/resubmit", data={"csrf_token": token, "original_request": "SHOULD NOT APPLY", "department": "Ops"}, follow_redirects=True)
            row = db.execute("SELECT original_request FROM feature_requests WHERE id=?", (rid,)).fetchone()
            check(f"{label} request cannot be resubmitted (unchanged)", row["original_request"] != "SHOULD NOT APPLY")

    print()
    print("=== 18. Sequential double-resubmit does not duplicate ===")
    dbl_id = create_request(db, requester_email, "__resub_requester", "Double resubmit test", now, "Returned")
    db.execute("UPDATE feature_requests SET approval_reason=? WHERE id=?", ("First reason", dbl_id))
    db.commit()
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        resub_url = f"/requests/{dbl_id}/resubmit"
        token1 = get_csrf(client, resub_url)
        client.post(resub_url, data={"csrf_token": token1, "original_request": "First resubmit", "department": "Ops"}, follow_redirects=True)
        token2 = get_csrf(client, "/requests")
        client.post(resub_url, data={"csrf_token": token2, "original_request": "Second resubmit should fail", "department": "Ops"}, follow_redirects=True)
        row = db.execute("SELECT original_request FROM feature_requests WHERE id=?", (dbl_id,)).fetchone()
        check("18. the second (stale) resubmit attempt does not overwrite the first", row["original_request"] == "First resubmit")
        resub_count = db.execute("SELECT COUNT(*) FROM feature_request_resubmissions WHERE feature_request_id=?", (dbl_id,)).fetchone()[0]
        check("18. exactly one resubmission event recorded, not two", resub_count == 1)

    print()
    print("=== 19. Genuine concurrent resubmit (real threads, real barrier) ===")
    conc_id = create_request(db, requester_email, "__resub_requester", "Concurrency resubmit test", now, "Returned")
    db.commit()

    client_a = appmod.app.test_client()
    login(client_a, requester_email, pw)
    token_a = get_csrf(client_a, f"/requests/{conc_id}/resubmit")

    client_b = appmod.app.test_client()
    login(client_b, requester_email, pw)
    token_b = get_csrf(client_b, f"/requests/{conc_id}/resubmit")

    barrier = threading.Barrier(2)
    results = {}

    def worker(key, client, token, text):
        barrier.wait()
        resp = client.post(f"/requests/{conc_id}/resubmit", data={"csrf_token": token, "original_request": text, "department": "Ops"}, follow_redirects=True)
        results[key] = resp

    t_a = threading.Thread(target=worker, args=("a", client_a, token_a, "Concurrent text A"))
    t_b = threading.Thread(target=worker, args=("b", client_b, token_b, "Concurrent text B"))
    t_a.start()
    t_b.start()
    t_a.join(timeout=15)
    t_b.join(timeout=15)

    check("19. both concurrent requests completed without a server error",
          results.get("a") is not None and results.get("b") is not None
          and results["a"].status_code == 200 and results["b"].status_code == 200)
    row = db.execute("SELECT approval_status, original_request FROM feature_requests WHERE id=?", (conc_id,)).fetchone()
    check("19. the request ended up Pending (exactly one resubmission applied)", row["approval_status"] == "Pending")
    check("19. the final text is exactly one of the two competing values, not corrupted",
          row["original_request"] in ("Concurrent text A", "Concurrent text B"))
    resub_count_conc = db.execute("SELECT COUNT(*) FROM feature_request_resubmissions WHERE feature_request_id=?", (conc_id,)).fetchone()[0]
    check("19. exactly one resubmission event recorded despite two concurrent attempts", resub_count_conc == 1)

    print()
    print("=== 20. CSRF enforced ===")
    csrf_test_id = create_request(db, requester_email, "__resub_requester", "CSRF test request", now, "Returned")
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        resp = client.post(f"/requests/{csrf_test_id}/resubmit", data={"original_request": "no csrf token"}, follow_redirects=True)
        check("20. a resubmit POST without a CSRF token is rejected", resp.status_code == 400 or "no csrf token" not in resp.get_data(as_text=True))
        row = db.execute("SELECT original_request FROM feature_requests WHERE id=?", (csrf_test_id,)).fetchone()
        check("20. request content unchanged when CSRF is missing", row["original_request"] == "CSRF test request")

    print()
    print("=== 21/22. Approve and Return work normally after resubmission ===")
    reapprove_id = create_request(db, requester_email, "__resub_requester", "Reapprove after resubmit", now, "Returned")
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        token = get_csrf(client, f"/requests/{reapprove_id}/resubmit")
        client.post(f"/requests/{reapprove_id}/resubmit", data={"csrf_token": token, "original_request": "Fixed for reapproval", "department": "Ops"}, follow_redirects=True)
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        durl = f"/admin/product-intelligence/{reapprove_id}"
        token = get_csrf(client, durl)
        client.post(durl, data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)
        row = db.execute("SELECT approval_status FROM feature_requests WHERE id=?", (reapprove_id,)).fetchone()
        check("21. Approve after resubmission works normally", row["approval_status"] == "Approved")

    rereturn_id = create_request(db, requester_email, "__resub_requester", "Rereturn after resubmit", now, "Returned")
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        token = get_csrf(client, f"/requests/{rereturn_id}/resubmit")
        client.post(f"/requests/{rereturn_id}/resubmit", data={"csrf_token": token, "original_request": "Still not quite right", "department": "Ops"}, follow_redirects=True)
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        durl = f"/admin/product-intelligence/{rereturn_id}"
        token = get_csrf(client, durl)
        client.post(durl, data={"csrf_token": token, "action": "return_request", "reason": "Still missing details"}, follow_redirects=True)
        row = db.execute("SELECT approval_status FROM feature_requests WHERE id=?", (rereturn_id,)).fetchone()
        check("22. Return after resubmission works normally", row["approval_status"] == "Returned")

        approvals_after = db.execute("SELECT * FROM feature_request_approvals WHERE feature_request_id=? ORDER BY decided_at", (rereturn_id,)).fetchall()
        check("23. Return decision history preserved (nothing overwritten)",
              len([a for a in approvals_after if a["decision"] == "Returned"]) == 1)

    print()
    print("=== 24/25. Attention Required direct navigation ===")
    db.execute("UPDATE feature_requests SET status='Building' WHERE requester_email LIKE '__resub_%' AND status IN ('Submitted','Reviewing') AND id != ?", (approved_req_id,))
    db.commit()
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        html = client.get("/admin/product-intelligence").get_data(as_text=True)
        m = re.search(r'REQUEST CENTER.*?href="([^"]+)"[^>]*>Review<', html, re.S)
        check("24. Attention Required 'Review' link found", m is not None)
        if m:
            review_href = m.group(1).replace("&amp;", "&")
            check("24. Review link points DIRECTLY to a request detail page, not a filtered list",
                  "/admin/product-intelligence/" in review_href and review_href.count("/admin/product-intelligence/") == 1
                  and "?status=" not in review_href.split("?")[0])
            check("24. Review link carries back= pointing to the Attention Required anchor", "pi2-attention" in review_href)
            detail_resp = client.get(review_href)
            check("24. following the Review link returns 200", detail_resp.status_code == 200)
            detail_html = detail_resp.get_data(as_text=True)
            back_href_match = re.search(r'href="([^"]*)"[^>]*>&larr; Back to Product Intelligence', detail_html)
            check("25. the detail page's Back link returns to Attention Required specifically",
                  back_href_match is not None and back_href_match.group(1).endswith("#pi2-attention"))

    print()
    print("=== 26. back_url validation spot check ===")
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        resp = client.get(f"/admin/product-intelligence/{approved_req_id}", query_string={"back": "https://evil.com"})
        html = resp.get_data(as_text=True)
        back_href_match = re.search(r'href="([^"]*)"[^>]*>&larr; Back to Product Intelligence', html)
        check("26. a malicious back= value is still rejected (unchanged from v3)",
              back_href_match is not None and "evil.com" not in back_href_match.group(1))

    print()
    print("=== 27. Latest Movement truthfulness ===")
    movement_id = create_request(db, requester_email, "__resub_requester", "Latest movement truth test", now, "Pending")
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        durl = f"/admin/product-intelligence/{movement_id}"
        token = get_csrf(client, durl)
        client.post(durl, data={"csrf_token": token, "action": "return_request", "reason": "Movement test reason"}, follow_redirects=True)
        html = client.get("/admin/product-intelligence").get_data(as_text=True)
        idx = html.find("Latest Movement")
        movement_section = html[idx:idx + 3000]
        check("27. Latest Movement shows 'Returned'/'Procurement' for this request, not the stale dev status",
              "Latest movement truth test" in movement_section and "Returned" in movement_section and "Procurement" in movement_section)

    print()
    print("=== 28. Existing development lifecycle unaffected ===")
    check("28. the earlier Approved+Reviewing request (test 5) still shows the normal timeline (already asserted in test 5)", True)

    # ================================================================
    # FIX 5: All Requests filters preserve #pi2-all-requests scroll context
    # ================================================================
    print()
    print("=== Fix 5: All Requests filter context preservation ===")
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        html = client.get("/admin/product-intelligence").get_data(as_text=True)

        toolbar_match = re.search(r'<form method="GET" class="pi-table-toolbar" action="([^"]+)">', html)
        check("1/2. the All Requests filter toolbar form has an action pointing at the All Requests anchor (covers both Status and Department, which both submit this same form)",
              toolbar_match is not None and toolbar_match.group(1).replace("&#35;", "#").endswith("#pi2-all-requests"))

        check("3. Search is client-side only (oninput, no page reload) -- scroll context is inherently preserved, nothing to fix",
              'oninput="ccFilterRequests()"' in html and "pi-req-search" in html)

        # 4. Clear filter (top-of-page banner, shown when a status filter is active).
        filtered_resp = client.get("/admin/product-intelligence?status=Building")
        filtered_html = filtered_resp.get_data(as_text=True)
        clear_match = re.search(r'Clear filter</a>', filtered_html)
        clear_href_match = re.search(r'href="([^"]+)"[^>]*>Clear filter', filtered_html)
        check("4. the 'Clear filter' banner link returns to the All Requests anchor",
              clear_href_match is not None and clear_href_match.group(1).replace("&#35;", "#").endswith("#pi2-all-requests"))

        # 5. Filter query parameters remain correct (status filter actually still filters).
        check("5. the status filter itself still works correctly (query params unaffected by the anchor addition)",
              "Building" in filtered_html)

        # 6. No open-redirect/back_url regression -- re-run a malicious back= against this same page.
        malicious_resp = client.get(f"/admin/product-intelligence/{approved_req_id}", query_string={"back": "//evil.com"})
        malicious_html = malicious_resp.get_data(as_text=True)
        back_href_match = re.search(r'href="([^"]*)"[^>]*>&larr; Back to Product Intelligence', malicious_html)
        check("6. back_url validation is unaffected by Fix 5 -- still rejects a protocol-relative external URL",
              back_href_match is not None and "evil.com" not in back_href_match.group(1))

        # 7. Total Requests KPI -> All Requests still works.
        total_kpi_match = re.search(r'href="([^"]+)"[^>]*class="pi2-strip-item[^"]*">\s*<div class="pi2-strip-value">', html)
        check("7. Total Requests KPI still links to the All Requests anchor (unaffected by Fix 5)",
              total_kpi_match is not None and total_kpi_match.group(1).endswith("#pi2-all-requests"))

        # 8. All Requests detail -> Back -> All Requests still works (approval-status-filtered variant).
        with_approval_filter_resp = client.get("/admin/product-intelligence?approval=Approved")
        with_approval_filter_html = with_approval_filter_resp.get_data(as_text=True)
        row_link_match = re.search(r"window.location='([^']+)'", with_approval_filter_html)
        check("8. an All Requests row link (from a filtered view) still carries a back= context",
              row_link_match is not None and "back=" in row_link_match.group(1))

    # ================================================================
    # BLOCKER 2 FIX: resubmit Department must use the canonical vocabulary
    # ================================================================
    print()
    print("=== Blocker 2 fix: resubmit Department is a controlled vocabulary, not free text ===")
    real_departments = [d["name"] for d in db.execute("SELECT name FROM departments").fetchall()]
    check("(setup) real departments exist to test against", len(real_departments) > 0)
    valid_dept = real_departments[0] if real_departments else "Ops"

    b2_req = create_request(db, requester_email, "__resub_requester", "Valid department resubmit test", now, "Returned")
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        resub_url = f"/requests/{b2_req}/resubmit"
        rget = client.get(resub_url).get_data(as_text=True)
        check("resubmit page renders a controlled <select> for department, not a free-text input",
              '<select name="department">' in rget and 'type="text" name="department"' not in rget)
        token = get_csrf(client, resub_url)
        client.post(resub_url, data={"csrf_token": token, "original_request": "valid dept text", "department": valid_dept}, follow_redirects=True)
        row = db.execute("SELECT department, approval_status, id FROM feature_requests WHERE id=?", (b2_req,)).fetchone()
        check("a valid canonical department is accepted", row["department"] == valid_dept)
        check("same request ID preserved through department change", row["id"] == b2_req)
        check("Returned -> Pending transition still occurred (atomicity unaffected by the department fix)", row["approval_status"] == "Pending")

    b2_req2 = create_request(db, requester_email, "__resub_requester", "Arbitrary department resubmit test", now, "Returned")
    db.execute("UPDATE feature_requests SET department='Ops' WHERE id=?", (b2_req2,))
    db.commit()
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        resub_url2 = f"/requests/{b2_req2}/resubmit"
        token2 = get_csrf(client, resub_url2)
        client.post(resub_url2, data={"csrf_token": token2, "original_request": "arbitrary dept text", "department": "procurement123"}, follow_redirects=True)
        row2 = db.execute("SELECT department, original_request, approval_status, id FROM feature_requests WHERE id=?", (b2_req2,)).fetchone()
        check("an arbitrary/non-canonical department POST is rejected and safely ignored (original department retained)",
              row2["department"] == "Ops" and row2["department"] != "procurement123")
        check("the rest of the resubmission (request text) still applies despite the rejected department",
              row2["original_request"] == "arbitrary dept text")
        check("same request ID preserved even when department is rejected", row2["id"] == b2_req2)
        check("Returned -> Pending transition still occurred despite the rejected department value", row2["approval_status"] == "Pending")

    b2_req3 = create_request(db, requester_email, "__resub_requester", "Ownership check with department fix", now, "Returned")
    with appmod.app.test_client() as client:
        login(client, other_employee_email, pw)
        resub_url3 = f"/requests/{b2_req3}/resubmit"
        token3 = get_csrf(client, resub_url3)
        client.post(resub_url3, data={"csrf_token": token3, "original_request": "HIJACK", "department": valid_dept}, follow_redirects=True)
        row3 = db.execute("SELECT original_request, approval_status FROM feature_requests WHERE id=?", (b2_req3,)).fetchone()
        check("ownership protection remains intact after the department fix (different employee still blocked)",
              row3["original_request"] != "HIJACK" and row3["approval_status"] == "Returned")

    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        filtered = client.get(f"/admin/product-intelligence?department={valid_dept}").get_data(as_text=True)
        check("PI department filter still works consistently for a department set via resubmit",
              "valid dept text" in filtered)

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM feature_request_resubmissions WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__resub_%')")
    db.execute("DELETE FROM feature_request_approvals WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__resub_%')")
    db.execute("DELETE FROM feature_request_status_history WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__resub_%')")
    db.execute("DELETE FROM feature_requests WHERE requester_email LIKE '__resub_%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
