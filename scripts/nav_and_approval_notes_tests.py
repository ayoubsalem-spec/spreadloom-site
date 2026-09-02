"""
v4 (Fix 6 + Fix 7) regression:
  - Fix 6: top nav right-alignment -- CSS/layout only, verify routes/
    active-state/permission conditionals are unaffected.
  - Fix 7: optional approval notes on Approve, required reason on
    Return unchanged, note storage/visibility/ownership/concurrency/XSS/
    length-limit.

Usage (from the project root):
    APP_ENV=development python3 scripts/nav_and_approval_notes_tests.py
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

    requester_email = "__f67_requester@test.local"
    other_employee_email = "__f67_other_employee@test.local"
    approver_a_email = "__f67_approver_a@test.local"
    approver_b_email = "__f67_approver_b@test.local"

    make_user(db, requester_email, "__f67_requester", now, pw_hash, ["module:product_intelligence:view"])
    make_user(db, other_employee_email, "__f67_other_employee", now, pw_hash, ["module:product_intelligence:view"])
    make_user(db, approver_a_email, "__f67_approver_a", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:approve_requests"])
    make_user(db, approver_b_email, "__f67_approver_b", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:approve_requests"])

    print("=== Fix 6: nav layout ===")
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        html = client.get("/").get_data(as_text=True)

        check("nav-wrap uses justify-content:flex-end (the actual layout fix)",
              "justify-content:flex-end" in html)

        nav_section_match = re.search(r'<div class="nav-primary">(.*?)</div>\s*</nav>', html, re.S)
        check("nav-primary block still present", nav_section_match is not None)
        if nav_section_match:
            nav_html = nav_section_match.group(1)
            link_texts_in_order = re.findall(r'>([A-Za-z ]+)</a>', nav_html)
            expected_labels = [l for l in ["Project Hunt", "Equipment Center", "SitePulse", "Atlas", "Requests", "Product Intelligence"] if l in nav_html]
            check("module nav labels appear in the exact existing order (Fix 6 did not reorder)",
                  [t.strip() for t in link_texts_in_order] == expected_labels)
        check("account/user menu trigger still present (far-right item preserved)", 'id="nav-admin-trigger"' in html)
        check("brand/logo link still anchored (unchanged)", 'class="brand"' in html and "BUILDIQ" in html)

        pi_page = client.get("/admin/product-intelligence").get_data(as_text=True)
        check("active-tab class still applied on the current section's nav link",
              re.search(r'href="[^"]*product-intelligence[^"]*"\s+class="active"', pi_page) is not None)

    print()
    print("=== Fix 7.1/7.2: Approve with blank note / with a note ===")
    req_blank = create_request(db, requester_email, "__f67_requester", "Approve blank note test", now, "Pending")
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        durl = f"/admin/product-intelligence/{req_blank}"
        token = get_csrf(client, durl)
        client.post(durl, data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)
        row = db.execute("SELECT approval_status, approval_reason FROM feature_requests WHERE id=?", (req_blank,)).fetchone()
        check("7.1 Approve succeeds with a blank note", row["approval_status"] == "Approved")
        check("7.1 blank note stored as no note", not row["approval_reason"])

    req_noted = create_request(db, requester_email, "__f67_requester", "Approve with note test", now, "Pending")
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        durl = f"/admin/product-intelligence/{req_noted}"
        token = get_csrf(client, durl)
        client.post(durl, data={"csrf_token": token, "action": "approve_request", "reason": "Approved, prioritize for next sprint"}, follow_redirects=True)
        row = db.execute("SELECT approval_status, approval_reason FROM feature_requests WHERE id=?", (req_noted,)).fetchone()
        check("7.2 Approve succeeds with a note", row["approval_status"] == "Approved")
        check("7.2 the note is stored exactly as entered", row["approval_reason"] == "Approved, prioritize for next sprint")

    print()
    print("=== 7.3: note stored with the correct history entry ===")
    approvals = db.execute("SELECT * FROM feature_request_approvals WHERE feature_request_id=?", (req_noted,)).fetchall()
    check("7.3 exactly one approval-history row for this request", len(approvals) == 1)
    check("7.3 that row's decision is Approved and reason matches the note",
          approvals[0]["decision"] == "Approved" and approvals[0]["reason"] == "Approved, prioritize for next sprint")

    print()
    print("=== 7.4: note visible on approver detail ===")
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        detail_html = client.get(f"/admin/product-intelligence/{req_noted}").get_data(as_text=True)
        check("7.4 the note appears in Approval History on the detail page", "Approved, prioritize for next sprint" in detail_html)

    print()
    print("=== 7.5/7.6: note visibility -- requester yes, other employee no ===")
    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        rc_html = client.get("/requests").get_data(as_text=True)
        check("7.5 the original requester sees the approval note on their own card", "Approved, prioritize for next sprint" in rc_html)

    with appmod.app.test_client() as client:
        login(client, other_employee_email, pw)
        other_rc_html = client.get("/requests").get_data(as_text=True)
        check("7.6 a different employee's Request Center never shows someone else's approval note",
              "Approved, prioritize for next sprint" not in other_rc_html)

    print()
    print("=== 7.7/7.8: Return reason still required, Return unchanged ===")
    req_return_test = create_request(db, requester_email, "__f67_requester", "Return still requires reason", now, "Pending")
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        durl = f"/admin/product-intelligence/{req_return_test}"
        token = get_csrf(client, durl)
        resp = client.post(durl, data={"csrf_token": token, "action": "return_request", "reason": ""}, follow_redirects=True)
        row = db.execute("SELECT approval_status FROM feature_requests WHERE id=?", (req_return_test,)).fetchone()
        check("7.7 Return with a blank reason is still rejected", row["approval_status"] == "Pending")
        check("7.7 the required-reason message is shown", "reason is required" in resp.get_data(as_text=True).lower())

        token2 = get_csrf(client, durl)
        resp2 = client.post(durl, data={"csrf_token": token2, "action": "return_request", "reason": "Needs more scope detail"}, follow_redirects=True)
        row2 = db.execute("SELECT approval_status, approval_reason FROM feature_requests WHERE id=?", (req_return_test,)).fetchone()
        check("7.8 Return with a reason still works normally", row2["approval_status"] == "Returned" and row2["approval_reason"] == "Needs more scope detail")

    print()
    print("=== 7.9: self-approval still blocked ===")
    self_id = create_request(db, approver_a_email, "__f67_approver_a", "Self approval attempt", now, "Pending")
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        durl = f"/admin/product-intelligence/{self_id}"
        token = get_csrf(client, durl)
        client.post(durl, data={"csrf_token": token, "action": "approve_request", "reason": "trying to approve my own"}, follow_redirects=True)
        row = db.execute("SELECT approval_status FROM feature_requests WHERE id=?", (self_id,)).fetchone()
        check("7.9 self-approval (even with a note) is still blocked", row["approval_status"] == "Pending")

    print()
    print("=== 7.10: unauthorized user cannot approve/persist a note ===")
    unauth_id = create_request(db, requester_email, "__f67_requester", "Unauthorized approval attempt", now, "Pending")
    with appmod.app.test_client() as client:
        login(client, other_employee_email, pw)
        durl = f"/admin/product-intelligence/{unauth_id}"
        client.get(durl)
        token = get_csrf(client, durl) or "invalid"
        client.post(durl, data={"csrf_token": token, "action": "approve_request", "reason": "sneaky note"}, follow_redirects=True)
        row = db.execute("SELECT approval_status, approval_reason FROM feature_requests WHERE id=?", (unauth_id,)).fetchone()
        check("7.10 an unauthorized user's approve attempt does not change approval_status", row["approval_status"] == "Pending")
        check("7.10 no note was persisted by the unauthorized attempt", row["approval_reason"] != "sneaky note")

    print()
    print("=== 7.11/7.12: genuine concurrent approval with notes ===")
    conc_id = create_request(db, requester_email, "__f67_requester", "Concurrent approval note test", now, "Pending")
    db.commit()

    client_a = appmod.app.test_client()
    login(client_a, approver_a_email, pw)
    token_a = get_csrf(client_a, f"/admin/product-intelligence/{conc_id}")

    client_b = appmod.app.test_client()
    login(client_b, approver_b_email, pw)
    token_b = get_csrf(client_b, f"/admin/product-intelligence/{conc_id}")

    barrier = threading.Barrier(2)
    results = {}

    def worker(key, client, token, note):
        barrier.wait()
        resp = client.post(f"/admin/product-intelligence/{conc_id}", data={"csrf_token": token, "action": "approve_request", "reason": note}, follow_redirects=True)
        results[key] = resp

    t_a = threading.Thread(target=worker, args=("a", client_a, token_a, "Note from Approver A"))
    t_b = threading.Thread(target=worker, args=("b", client_b, token_b, "Note from Approver B"))
    t_a.start()
    t_b.start()
    t_a.join(timeout=15)
    t_b.join(timeout=15)

    check("7.11 both concurrent approval attempts completed without a server error",
          results.get("a") is not None and results.get("b") is not None
          and results["a"].status_code == 200 and results["b"].status_code == 200)
    row = db.execute("SELECT approval_status, approval_reason, approval_decided_by FROM feature_requests WHERE id=?", (conc_id,)).fetchone()
    check("7.11 the request ended up Approved (exactly one decision applied)", row["approval_status"] == "Approved")
    check("7.11 the final note is exactly one of the two competing notes, not corrupted",
          row["approval_reason"] in ("Note from Approver A", "Note from Approver B"))
    approvals_conc = db.execute("SELECT * FROM feature_request_approvals WHERE feature_request_id=?", (conc_id,)).fetchall()
    check("7.11 exactly one approval-history row recorded despite two concurrent attempts", len(approvals_conc) == 1)
    check("7.12 the sole history row's note matches the winner (the loser did not overwrite it)",
          approvals_conc[0]["reason"] == row["approval_reason"] and approvals_conc[0]["decided_by"] == row["approval_decided_by"])

    print()
    print("=== 7.13: XSS in approval note is escaped ===")
    xss_id = create_request(db, requester_email, "__f67_requester", "XSS note test", now, "Pending")
    xss_payload = "<script>alert('note-xss')</script>"
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        durl = f"/admin/product-intelligence/{xss_id}"
        token = get_csrf(client, durl)
        client.post(durl, data={"csrf_token": token, "action": "approve_request", "reason": xss_payload}, follow_redirects=True)
        detail_html = client.get(durl).get_data(as_text=True)
        check("7.13 raw <script> tag from the note is NOT present in the rendered detail page", "<script>alert('note-xss')" not in detail_html)
        check("7.13 the note is present in its escaped form", "&lt;script&gt;" in detail_html)

    with appmod.app.test_client() as client:
        login(client, requester_email, pw)
        rc_html = client.get("/requests").get_data(as_text=True)
        check("7.13 raw <script> tag is NOT present on the requester's own card either", "<script>alert('note-xss')" not in rc_html)

    print()
    print("=== 7.14: existing no-note approval records still render normally ===")
    no_note_id = create_request(db, requester_email, "__f67_requester", "No note historical record", now, "Approved")
    db.execute("INSERT INTO feature_request_approvals (feature_request_id, decision, reason, decided_by, decided_at) VALUES (?,?,?,?,?)",
               (no_note_id, "Approved", None, approver_a_email, now))
    db.commit()
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        detail_html = client.get(f"/admin/product-intelligence/{no_note_id}").get_data(as_text=True)
        check("7.14 a no-note historical approval record renders without error", "No note historical record" in detail_html)
        check("7.14 no stray 'None' text leaks into the rendered history for the missing note", "&middot; None" not in detail_html)

    print()
    print("=== Server-side approval note length limit ===")
    long_id = create_request(db, requester_email, "__f67_requester", "Long note test", now, "Pending")
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        durl = f"/admin/product-intelligence/{long_id}"
        token = get_csrf(client, durl)
        long_note = "x" * (appmod.APPROVAL_NOTE_MAX_LENGTH + 100)
        resp = client.post(durl, data={"csrf_token": token, "action": "approve_request", "reason": long_note}, follow_redirects=True)
        row = db.execute("SELECT approval_status, approval_reason FROM feature_requests WHERE id=?", (long_id,)).fetchone()
        check("an overlong note is rejected server-side (not silently truncated and accepted)", row["approval_status"] == "Pending")
        check("the length-limit message is shown", "too long" in resp.get_data(as_text=True).lower())

        ok_note = "y" * appmod.APPROVAL_NOTE_MAX_LENGTH
        token2 = get_csrf(client, durl)
        resp2 = client.post(durl, data={"csrf_token": token2, "action": "approve_request", "reason": ok_note}, follow_redirects=True)
        row2 = db.execute("SELECT approval_status, approval_reason FROM feature_requests WHERE id=?", (long_id,)).fetchone()
        check("a note exactly at the length limit is accepted", row2["approval_status"] == "Approved" and row2["approval_reason"] == ok_note)

    print()
    print("=== Blocker 1 fix: detail page uses the SAME toggle-panel approval UX as the inbox ===")
    b1_req = create_request(db, requester_email, "__f67_requester", "Detail page toggle UX test", now, "Pending")
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        durl = f"/admin/product-intelligence/{b1_req}"
        detail_html = client.get(durl).get_data(as_text=True)

        check("1. Approve on the detail page is a toggle button, not an immediate direct submit",
              f"onclick=\"piToggleApprove('{b1_req}')\"" in detail_html
              and 'name="action" value="approve_request" class="btn btn-gold">Approve<' not in detail_html)
        check("2. an optional-note approval panel exists on the detail page", f'id="pi2-approve-{b1_req}"' in detail_html)
        check("3. the panel's Confirm Approval button submits approve_request",
              re.search(r'id="pi2-approve-' + str(b1_req) + r'".*?value="approve_request"', detail_html, re.S) is not None)
        check("4. Return opens its own required-reason panel on the detail page",
              f'id="pi2-return-{b1_req}"' in detail_html and f"onclick=\"piToggleReturn('{b1_req}')\"" in detail_html)
        return_panel_match = re.search(r'id="pi2-return-' + str(b1_req) + r'".*?</div>', detail_html, re.S)
        check("Return's reason input is still marked required on the detail page",
              return_panel_match is not None and "required" in return_panel_match.group(0))
        check("5. Approve and Return panels share the mutually-exclusive toggle mechanism (piToggleReason)",
              "function piToggleReason(" in detail_html and detail_html.count("function piToggleReason(") == 1)
        check("8. detail page markup matches the inbox's panel structure/classes (same pi2-return-reason class, same panel ids)",
              'class="pi2-return-reason"' in detail_html)

        # 6. Blank approval note allowed via this exact panel/form.
        token = get_csrf(client, durl)
        client.post(durl, data={"csrf_token": token, "action": "approve_request", "reason": ""}, follow_redirects=True)
        row = db.execute("SELECT approval_status FROM feature_requests WHERE id=?", (b1_req,)).fetchone()
        check("6. blank approval note is allowed through the detail-page panel", row["approval_status"] == "Approved")

    # 7. Blank Return reason remains rejected server-side (separate request).
    b1_req2 = create_request(db, requester_email, "__f67_requester", "Detail page return validation test", now, "Pending")
    with appmod.app.test_client() as client:
        login(client, approver_a_email, pw)
        durl2 = f"/admin/product-intelligence/{b1_req2}"
        token2 = get_csrf(client, durl2)
        resp = client.post(durl2, data={"csrf_token": token2, "action": "return_request", "reason": ""}, follow_redirects=True)
        row2 = db.execute("SELECT approval_status FROM feature_requests WHERE id=?", (b1_req2,)).fetchone()
        check("7. blank Return reason remains rejected server-side on the detail page", row2["approval_status"] == "Pending")

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM feature_request_resubmissions WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__f67_%')")
    db.execute("DELETE FROM feature_request_approvals WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__f67_%')")
    db.execute("DELETE FROM feature_request_status_history WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__f67_%')")
    db.execute("DELETE FROM feature_requests WHERE requester_email LIKE '__f67_%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
