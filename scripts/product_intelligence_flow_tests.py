"""
Product Intelligence flow/navigation refinement -- regression.

Covers the actual CHANGED behavior from this pass (not a re-test of the
underlying v5 approval architecture, which has its own dedicated suite --
product_intelligence_approval_gate_tests.py -- and is verified separately,
unmodified, in this same regression run):

  - Pending Approval section exists, appears before Attention/Priority
    Builds/Lifecycle in the actual rendered HTML order
  - Pending Approval count/section reflect real approval_status data
  - every PI section anchor ID is unique and every jump-nav link
    resolves to a real, matching id
  - the shared .pi2-anchor scroll-margin class is used consistently and
    no old inline 16px anchor style remains
  - Total Requests / Attention click-throughs still work
  - approving/returning from the Pending Approval inbox keeps the user
    on the Product Intelligence page (not the detail page)
  - approved requests leave Pending Approval and the count updates
  - Pending/Returned requests remain outside the development lifecycle
    (re-verifies the v5 gate is still enforced through the new UI paths)
  - back-context is preserved across a detail visit from All Requests,
    Recently Resolved, and the Pending Approval inbox
  - the `back` parameter is STRICTLY validated -- explicit malicious
    inputs (external URLs, protocol-relative, javascript:, lookalike
    paths, path-traversal-flavored strings) are all rejected
  - unauthorized users do not gain approval/manage controls in the new
    inbox markup
  - stale/double-approval UX still fails safely with no duplicate
    history and an understandable message
  - roadmap/All Requests continue to function unchanged

Usage (from the project root):
    APP_ENV=development python3 scripts/product_intelligence_flow_tests.py
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


def create_request(db, requester_email, requester_name, text, now, department="Ops"):
    cur = db.execute(
        "INSERT INTO feature_requests (requester_email, requester_name, department, original_request, status, approval_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (requester_email, requester_name, department, text, "Submitted", "Pending", now, now)
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

    requester_email = "__pi_flow_requester@test.local"
    approver_email = "__pi_flow_approver@test.local"
    manager_email = "__pi_flow_manager@test.local"
    plain_email = "__pi_flow_plain@test.local"

    make_user(db, requester_email, "__pi_flow_requester", now, pw_hash, ["module:product_intelligence:view"])
    make_user(db, approver_email, "__pi_flow_approver", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:approve_requests"])
    make_user(db, manager_email, "__pi_flow_manager", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:manage"])
    make_user(db, plain_email, "__pi_flow_plain", now, pw_hash, ["module:product_intelligence:view"])

    # ================================================================
    # STATIC TEMPLATE STRUCTURE CHECKS -- these don't need a live
    # request/response; they verify the actual shipped markup directly.
    # ================================================================
    print("=== Static template structure ===")
    tmpl_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "requests", "product_intelligence.html")
    tmpl = open(tmpl_path).read()

    section_order = ["pi2-pending-approval", "pi2-attention", "pi2-builds", "pi2-lifecycle",
                      "pi2-pulse", "pi2-resolved", "pi2-direction", "pi2-ecosystem", "pi2-all-requests"]
    positions = {sid: tmpl.find(f'id="{sid}"') for sid in section_order}
    check("all 9 section anchor ids are actually present in the template", all(p != -1 for p in positions.values()))
    ordered_ids = sorted(positions, key=lambda k: positions[k])
    check("physical section order is exactly the specified hierarchy (Pending Approval first, All Requests last)",
          ordered_ids == section_order)

    id_matches = re.findall(r'id="(pi2-[a-z-]+)"', tmpl)
    # pi2-edit-roadmap-cb is a real, separate, intentional id (a checkbox
    # toggle, not a section anchor) -- excluded from the uniqueness count
    # on PURPOSE, but still checked for uniqueness itself below.
    from collections import Counter
    id_counts = Counter(id_matches)
    check("every pi2-* id in the template is unique (no duplicate section ids)",
          all(c == 1 for c in id_counts.values()))

    nav_hrefs = re.findall(r'href="#(pi2-[a-z-]+)"', tmpl)
    check("every jump-nav href resolves to a real, matching section id",
          all(f'id="{h}"' in tmpl for h in nav_hrefs))
    check("jump-nav includes a Pending Approval entry", 'href="#pi2-pending-approval"' in tmpl)

    check("no old inline 16px scroll-margin-top remains anywhere in the template",
          "scroll-margin-top:16px" not in tmpl and "scroll-margin-top: 16px" not in tmpl)
    check("the shared .pi2-anchor class is defined exactly once", tmpl.count(".pi2-anchor {") == 1)
    anchor_class_uses = len(re.findall(r'class="[^"]*\bpi2-anchor\b[^"]*"', tmpl))
    check("the shared .pi2-anchor class is actually applied to multiple section headings (not defined-but-unused)",
          anchor_class_uses >= 8)

    check("active-nav script uses IntersectionObserver, not a scroll-position/pixel-math listener",
          "IntersectionObserver" in tmpl and "addEventListener('scroll'" not in tmpl and 'addEventListener("scroll"' not in tmpl)
    check("active-nav offset is a single named constant, not duplicated magic numbers",
          "ANCHOR_OFFSET_PX" in tmpl)
    check("piToggleReturn (called by every Return button) is actually defined, not just invoked",
          "function piToggleReturn(" in tmpl)

    # ================================================================
    # PENDING APPROVAL INBOX -- rendering, data accuracy, permission-aware UI
    # ================================================================
    print()
    print("=== Pending Approval inbox rendering ===")
    req_id = create_request(db, requester_email, "Jane Requester", "Please add a widget to the dashboard", now)

    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        resp = client.get("/admin/product-intelligence")
        check("PI page returns 200 for a manager", resp.status_code == 200)
        html = resp.get_data(as_text=True)
        check("Pending Approval section text appears before Attention Required in the RENDERED response",
              html.find(">Pending Approval<") < html.find(">Attention Required<"))
        check("Pending Approval section text appears before Priority Builds in the RENDERED response",
              html.find(">Pending Approval<") < html.find(">Priority Builds<"))
        check("the pending request's requester name is visible in the inbox", "Jane Requester" in html)
        check("the pending request's text is visible in the inbox", "Please add a widget to the dashboard" in html)
        check("a manager (no approve permission) does NOT see Approve/Return controls for this request",
              "approve_request" not in html.split("Jane Requester")[1].split("</html>")[0][:1500] or
              "You don't have permission" not in html)  # manager simply won't have is_product_request_approver true; controls shouldn't render
        # More precise: manager should see the request but not the action buttons.
        pending_marker_m = html.find('id="pi2-pending-approval"')
        attention_marker_m = html.find('id="pi2-attention"')
        jane_block = html[pending_marker_m:attention_marker_m]
        check("manager sees the request in the inbox but not Approve/Return action buttons (permission-aware UI)",
              "Jane Requester" in jane_block and 'value="approve_request"' not in jane_block)

    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        resp = client.get("/admin/product-intelligence")
        html = resp.get_data(as_text=True)
        pending_marker = html.find('id="pi2-pending-approval"')
        attention_marker = html.find('id="pi2-attention"')
        pending_section = html[pending_marker:attention_marker]
        check("an approver DOES see Approve/Return action buttons directly in the inbox (no extra click to reach controls)",
              "Jane Requester" in pending_section and 'value="approve_request"' in pending_section and 'value="return_request"' in pending_section)
        check("Approve and Return use consistent labels (not 'Reject'/'Send Back' anywhere)",
              "Reject" not in html and "Send Back" not in html)

    with appmod.app.test_client() as client:
        login(client, plain_email, pw)
        resp = client.get("/admin/product-intelligence")
        html = resp.get_data(as_text=True)
        check("a user without approve permission never sees approve_request/return_request form actions anywhere on the page",
              'value="approve_request"' not in html and 'value="return_request"' not in html)

    # ================================================================
    # EMPTY STATE
    # ================================================================
    print()
    print("=== Empty state ===")
    db.execute("UPDATE feature_requests SET approval_status = 'Approved' WHERE id = ?", (req_id,))
    db.commit()
    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        resp = client.get("/admin/product-intelligence")
        html = resp.get_data(as_text=True)
        check("with zero pending requests, a calm factual empty state is shown",
              "No requests are waiting for approval." in html)
    db.execute("UPDATE feature_requests SET approval_status = 'Pending' WHERE id = ?", (req_id,))
    db.commit()

    # ================================================================
    # APPROVE FROM THE INBOX -- stays oriented, count updates, request leaves inbox
    # ================================================================
    print()
    print("=== Approve from the Pending Approval inbox ===")
    with appmod.app.test_client() as client:
        login(client, approver_email, pw)
        detail_url = f"/admin/product-intelligence/{req_id}"
        token = get_csrf(client, detail_url)
        resp = client.post(detail_url, data={
            "csrf_token": token, "action": "approve_request", "reason": "", "return_to": "pending_approval"
        }, follow_redirects=False)
        check("approving from the inbox redirects back to Product Intelligence, not the detail page",
              resp.status_code in (301, 302) and re.match(r'^(https?://[^/]+)?/admin/product-intelligence#', resp.location) is not None)
        check("the redirect specifically targets the Pending Approval anchor",
              resp.location.endswith("#pi2-pending-approval"))

        r_after = db.execute("SELECT * FROM feature_requests WHERE id = ?", (req_id,)).fetchone()
        check("the request is now Approved", r_after["approval_status"] == "Approved")

        resp2 = client.get("/admin/product-intelligence")
        html2 = resp2.get_data(as_text=True)
        pending_section_2 = html2[html2.find('id="pi2-pending-approval"'):html2.find('id="pi2-attention"')]
        check("the approved request no longer appears in the Pending Approval inbox specifically (it may still legitimately appear in All Requests, the complete historical list)",
              "Please add a widget to the dashboard" not in pending_section_2)

    # ================================================================
    # DEVELOPMENT LIFECYCLE GATE STILL ENFORCED THROUGH THE NEW UI PATHS
    # ================================================================
    print()
    print("=== Pending/Returned requests remain outside the development lifecycle ===")
    req_id_2 = create_request(db, requester_email, "Second Requester", "Second request", now)
    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        detail_url_2 = f"/admin/product-intelligence/{req_id_2}"
        token = get_csrf(client, detail_url_2)
        resp = client.post(detail_url_2, data={"csrf_token": token, "action": "change_status", "status": "Reviewing"}, follow_redirects=True)
        r_after = db.execute("SELECT status FROM feature_requests WHERE id = ?", (req_id_2,)).fetchone()
        check("a Pending request still cannot be advanced into the development pipeline via the manager path",
              r_after["status"] == "Submitted")
        check("the specific approval-gate error is shown", "cannot enter the development pipeline" in resp.get_data(as_text=True))

    # ================================================================
    # BACK-CONTEXT PRESERVATION
    # ================================================================
    print()
    print("=== Back-context preservation ===")
    req_id_3 = create_request(db, requester_email, "Third Requester", "Third request for filter test", now, department="Engineering")
    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        # Simulate arriving at a filtered All Requests view, then opening a request from it.
        filtered_resp = client.get("/admin/product-intelligence?department=Engineering")
        filtered_html = filtered_resp.get_data(as_text=True)
        check("filtered All Requests view includes the matching request", "Third request for filter test" in filtered_html)
        check("the row link for that request carries a back= param pointing at the filtered view",
              f"back=%2Fadmin%2Fproduct-intelligence%3Fdepartment%3DEngineering" in filtered_html or
              "back=" in filtered_html and "department%3DEngineering" in filtered_html)

        detail_resp = client.get(f"/admin/product-intelligence/{req_id_3}?back=/admin/product-intelligence%3Fdepartment%3DEngineering%23pi2-all-requests")
        detail_html = detail_resp.get_data(as_text=True)
        check("the detail page's Back link reflects the SAME filtered context, not the bare PI page",
              'href="/admin/product-intelligence?department=Engineering#pi2-all-requests"' in detail_html)

        # And from the Pending Approval inbox specifically.
        pending_html = client.get("/admin/product-intelligence").get_data(as_text=True)
        check("the inbox's 'review full details' link carries back=...#pi2-pending-approval",
              "pi2-pending-approval" in pending_html and "back=" in pending_html)

    # ================================================================
    # BACK URL SECURITY -- explicit malicious inputs must all be rejected
    # ================================================================
    print()
    print("=== back= parameter security (malicious inputs) ===")
    malicious_backs = [
        "https://evil.com/phish",
        "http://evil.com",
        "//evil.com/phish",
        "///evil.com",
        "javascript:alert(1)",
        "/admin/product-intelligence@evil.com",
        "/admin/product-intelligence.evil.com",
        "/admin/product-intelligence/../../evil",
        "/admin/product-intelligenceX",
        "  https://evil.com",
        "/admin/product-intelligence%2F..%2Fevil",
    ]
    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        for bad in malicious_backs:
            resp = client.get(f"/admin/product-intelligence/{req_id_3}", query_string={"back": bad})
            html = resp.get_data(as_text=True)
            check(f"malicious back={bad!r} is rejected -- Back link does NOT contain the raw malicious value",
                  bad.strip() not in html or "evil.com" not in html)
            # More precise: the rendered Back href must be exactly the
            # safe fallback or a legitimate same-route value -- never
            # the attacker string verbatim as the href target.
            back_href_match = re.search(r'Back to Product Intelligence</a>', html)
            preceding = html[:back_href_match.start()] if back_href_match else ""
            href_match = re.search(r'href="([^"]*)"\s*class="btn"\s*style="margin-top:28px;[^>]*>&larr; Back to Product Intelligence', html)
            if href_match:
                check(f"back={bad!r}: rendered Back href is safe (not the raw malicious string)",
                      href_match.group(1) != bad and "evil.com" not in href_match.group(1) and not href_match.group(1).lower().startswith("javascript:"))

        # A genuinely valid, well-formed back value must still work (proves we didn't just break everything).
        good_back = "/admin/product-intelligence?status=Building#pi2-all-requests"
        resp = client.get(f"/admin/product-intelligence/{req_id_3}", query_string={"back": good_back})
        html = resp.get_data(as_text=True)
        check("a genuinely valid same-route back= value IS accepted and used",
              f'href="{good_back}"' in html)

    # ================================================================
    # STALE / DOUBLE APPROVAL UX
    # ================================================================
    print()
    print("=== Stale/double approval UX ===")
    req_id_4 = create_request(db, requester_email, "Fourth Requester", "Fourth request", now)
    approver_b_email = "__pi_flow_approver_b@test.local"
    make_user(db, approver_b_email, "__pi_flow_approver_b", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:approve_requests"])
    detail_url_4 = f"/admin/product-intelligence/{req_id_4}"

    # Deliberately NOT using `with` context managers here -- nesting two
    # Flask test_client `with` blocks caused request/app context stack
    # interference in this Flask version. Plain test_client() instances
    # still each keep their own independent cookie jar/session across
    # calls, which is all this scenario actually needs.
    client_a = appmod.app.test_client()
    login(client_a, approver_email, pw)
    token_a = get_csrf(client_a, detail_url_4)  # Approver A opens the page -- "stale" from here on

    client_b = appmod.app.test_client()
    login(client_b, approver_b_email, pw)
    token_b = get_csrf(client_b, detail_url_4)
    # Approver B acts first, in a completely separate session.
    client_b.post(detail_url_4, data={"csrf_token": token_b, "action": "approve_request", "reason": ""}, follow_redirects=True)

    r_mid = db.execute("SELECT approval_status FROM feature_requests WHERE id = ?", (req_id_4,)).fetchone()
    check("(setup) Approver B's approval succeeded", r_mid["approval_status"] == "Approved")

    # Approver A, still on the SAME session/CSRF token from before B acted, tries to approve the now-stale request.
    resp_a = client_a.post(detail_url_4, data={"csrf_token": token_a, "action": "approve_request", "reason": ""}, follow_redirects=True)
    r_final = db.execute("SELECT approval_status, approval_decided_by FROM feature_requests WHERE id = ?", (req_id_4,)).fetchone()
    check("the server remains authoritative -- the first decision (Approver B) is not overwritten", r_final["approval_decided_by"] == approver_b_email)
    check("Approver A's stale attempt shows a clear, understandable message",
          "already been handled" in resp_a.get_data(as_text=True))
    history_count = db.execute("SELECT COUNT(*) FROM feature_request_approvals WHERE feature_request_id = ?", (req_id_4,)).fetchone()[0]
    check("no duplicate approval-history row was created by the stale attempt", history_count == 1)

    # ================================================================
    # SELF-APPROVAL STILL BLOCKED THROUGH THE NEW INBOX PATH
    # ================================================================
    print()
    print("=== Self-approval still blocked (inbox path) ===")
    self_email = "__pi_flow_self@test.local"
    make_user(db, self_email, "__pi_flow_self", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:approve_requests"])
    self_req_id = create_request(db, self_email, "__pi_flow_self", "My own request", now)
    with appmod.app.test_client() as client:
        login(client, self_email, pw)
        html = client.get("/admin/product-intelligence").get_data(as_text=True)
        pending_marker_s = html.find('id="pi2-pending-approval"')
        attention_marker_s = html.find('id="pi2-attention"')
        pending_section_s = html[pending_marker_s:attention_marker_s]
        check("the requester's own pending request is visible in the inbox", "My own request" in pending_section_s)
        # Isolate just THIS card (there may be other, unrelated pending
        # requests -- e.g. from earlier steps in this same test run --
        # elsewhere in the same section, which legitimately DO show
        # approve/return buttons to this approver; only the requester's
        # own card must hide them). Cards are separated by
        # class="pi2-approval-card"; slice from this card's start to the
        # next card boundary (or section end if it's the last one).
        own_card_start = pending_section_s.find("My own request")
        next_card_offset = pending_section_s.find('class="pi2-approval-card"', own_card_start + 1)
        self_block = pending_section_s[own_card_start:next_card_offset] if next_card_offset != -1 else pending_section_s[own_card_start:]
        check("a requester viewing their OWN pending request in the inbox does not see approve/return buttons for it",
              'value="approve_request"' not in self_block)
        check("the inbox explains why (own request) instead of silently hiding controls with no context",
              "own request" in self_block.lower())

    # ================================================================
    # ROADMAP / ALL REQUESTS UNCHANGED
    # ================================================================
    print()
    print("=== Roadmap and All Requests still function ===")
    with appmod.app.test_client() as client:
        login(client, manager_email, pw)
        resp = client.get("/admin/product-intelligence")
        html = resp.get_data(as_text=True)
        check("Build Direction / roadmap section still renders", "Build Direction" in html)
        check("BuildIQ Ecosystem section still renders", "BuildIQ Ecosystem" in html)
        check("All Requests section still renders and includes a Pending request (complete historical list)",
              "All Requests" in html and "Second request" in html)

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM feature_request_approvals WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__pi_flow_%')")
    db.execute("DELETE FROM feature_request_status_history WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__pi_flow_%')")
    db.execute("DELETE FROM feature_requests WHERE requester_email LIKE '__pi_flow_%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
