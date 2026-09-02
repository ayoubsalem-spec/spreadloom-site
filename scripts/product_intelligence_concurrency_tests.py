"""
Product Intelligence approval gate -- GENUINE concurrency tests.

This is deliberately separate from product_intelligence_flow_tests.py's
existing sequential two-client stale-decision test (kept and re-run
below too). That test proves the correct behavior when one decision is
already committed before the second is even attempted -- it does NOT
prove anything about two decisions racing to update the same row at
close to the same instant.

This file exercises REAL concurrency:
  - two OS threads (threading.Thread), not sequential calls dressed up
    to look concurrent
  - each thread uses its OWN Flask test client (own session/cookies),
    which means each request gets its OWN sqlite3 connection via
    get_db() -- genuinely separate database connections, not one
    connection shared and pretended to be two
  - a threading.Barrier(2) forces both threads to issue their POST at
    essentially the same instant, rather than one reliably starting
    and finishing before the other begins
  - PRAGMA busy_timeout (set in get_db()) means that even where the OS
    thread scheduler doesn't achieve perfect overlap at the exact SQL
    statement, a "loser" connection queues briefly instead of
    immediately raising "database is locked" -- and per the fix, still
    correctly loses the race once it actually runs (the WHERE
    approval_status='Pending' clause on the UPDATE will no longer match)

For every case (Approve/Approve, Approve/Return, Return/Return) against
the same Pending request, this proves:
  - exactly one decision wins
  - final approval_status matches the winner's decision
  - approval_decided_by matches the winner
  - exactly one feature_request_approvals row exists for the request
  - the loser cannot overwrite the winner
  - the loser creates zero additional history rows
  - the loser gets a safe, non-crashing "already handled" outcome
  - no unhandled SQLite exception reaches either request

Usage (from the project root):
    APP_ENV=development python3 scripts/product_intelligence_concurrency_tests.py
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
        (requester_email, requester_name, "Ops", text, "Submitted", "Pending", now, now)
    )
    db.commit()
    return cur.lastrowid


def run_concurrent_decision(client_a, url, token_a, action_a, reason_a,
                             client_b, token_b, action_b, reason_b):
    """Fires two REAL POST requests at the same request_id using two
    ALREADY-LOGGED-IN, independent Flask test clients (own sessions ->
    own sqlite3 connections per get_db()), synchronized with a barrier
    so both threads submit essentially simultaneously rather than one
    reliably completing before the other starts. Each client/token pair
    must come from the SAME login session -- CSRF tokens are session-
    bound, so mixing a token from one client with a different client's
    session would fail CSRF validation for reasons that have nothing to
    do with the actual concurrency behavior under test. Returns
    (resp_a, resp_b)."""
    barrier = threading.Barrier(2)
    results = {}

    def worker(key, client, token, action, reason):
        barrier.wait()  # both threads block here until BOTH are ready, then release together
        resp = client.post(url, data={
            "csrf_token": token, "action": action, "reason": reason or ""
        }, follow_redirects=True)
        results[key] = resp

    t_a = threading.Thread(target=worker, args=("a", client_a, token_a, action_a, reason_a))
    t_b = threading.Thread(target=worker, args=("b", client_b, token_b, action_b, reason_b))
    t_a.start()
    t_b.start()
    t_a.join(timeout=15)
    t_b.join(timeout=15)
    return results.get("a"), results.get("b")


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    requester_email = "__pi_conc_requester@test.local"
    approver_a_email = "__pi_conc_approver_a@test.local"
    approver_b_email = "__pi_conc_approver_b@test.local"

    make_user(db, requester_email, "__pi_conc_requester", now, pw_hash, ["module:product_intelligence:view"])
    make_user(db, approver_a_email, "__pi_conc_approver_a", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:approve_requests"])
    make_user(db, approver_b_email, "__pi_conc_approver_b", now, pw_hash,
              ["module:product_intelligence:view", "action:product_intelligence:approve_requests"])

    def run_case(label, action_a, reason_a, action_b, reason_b):
        print()
        print(f"=== Genuine concurrency: {label} ===")
        req_id = create_request(db, requester_email, "__pi_conc_requester", f"Concurrency test request ({label})", now)
        url = f"/admin/product-intelligence/{req_id}"

        # Pre-fetch valid CSRF tokens for each approver's own session
        # BEFORE the race, exactly as a real browser would already have
        # the page open with a valid token when the race begins.
        client_a_setup = appmod.app.test_client()
        login(client_a_setup, approver_a_email, pw)
        token_a = get_csrf(client_a_setup, url)

        client_b_setup = appmod.app.test_client()
        login(client_b_setup, approver_b_email, pw)
        token_b = get_csrf(client_b_setup, url)

        resp_a, resp_b = run_concurrent_decision(
            client_a_setup, url, token_a, action_a, reason_a,
            client_b_setup, token_b, action_b, reason_b
        )

        check(f"[{label}] both concurrent requests completed without a server error (no unhandled exception)",
              resp_a is not None and resp_b is not None and resp_a.status_code == 200 and resp_b.status_code == 200)

        row = db.execute("SELECT * FROM feature_requests WHERE id = ?", (req_id,)).fetchone()
        check(f"[{label}] the request is no longer Pending -- exactly one decision was applied",
              row["approval_status"] in ("Approved", "Returned"))

        history_rows = db.execute(
            "SELECT * FROM feature_request_approvals WHERE feature_request_id = ? ORDER BY id", (req_id,)
        ).fetchall()
        check(f"[{label}] exactly ONE approval-history row exists for this request (no duplicate/lost-update)",
              len(history_rows) == 1)

        if history_rows:
            winner_decision = history_rows[0]["decision"]
            winner_decided_by = history_rows[0]["decided_by"]
            check(f"[{label}] feature_requests.approval_status matches the SOLE history row's decision",
                  row["approval_status"] == winner_decision)
            check(f"[{label}] feature_requests.approval_decided_by matches the SOLE history row's actor",
                  row["approval_decided_by"] == winner_decided_by)
            # Whichever approver's decision matches the winning history
            # row, the OTHER one's response must show a safe conflict
            # message, never a false success.
            winner_email = approver_a_email if winner_decided_by == approver_a_email else approver_b_email
            loser_resp = resp_b if winner_email == approver_a_email else resp_a
            loser_body = loser_resp.get_data(as_text=True) if loser_resp else ""
            check(f"[{label}] the LOSING request's response shows the safe 'already handled' outcome, not a false success",
                  "already been handled" in loser_body)
            check(f"[{label}] the losing response does not ALSO claim a false success message",
                  "approved and moved to the development queue" not in loser_body
                  and "returned to the requester" not in loser_body)

        return req_id

    # ================================================================
    # CASE A: Approve vs Approve
    # ================================================================
    run_case("Approve vs Approve", "approve_request", "", "approve_request", "")

    # ================================================================
    # CASE B: Approve vs Return
    # ================================================================
    run_case("Approve vs Return", "approve_request", "", "return_request", "Not this quarter")

    # ================================================================
    # CASE C: Return vs Return
    # ================================================================
    run_case("Return vs Return", "return_request", "Budget concern", "return_request", "Scope concern")

    # ================================================================
    # SEQUENTIAL STALE-DECISION REGRESSION (kept, re-run here alongside
    # the genuine concurrency cases -- this is the ORIGINAL, still-valid
    # test proving a decision made after another has already committed
    # is correctly rejected; genuine concurrency is a stronger, separate
    # guarantee, not a replacement for this one).
    # ================================================================
    print()
    print("=== Sequential stale-decision regression (kept) ===")
    req_id_seq = create_request(db, requester_email, "__pi_conc_requester", "Sequential stale test request", now)
    url_seq = f"/admin/product-intelligence/{req_id_seq}"

    client_a = appmod.app.test_client()
    login(client_a, approver_a_email, pw)
    token_a_seq = get_csrf(client_a, url_seq)  # A's page load -- "stale" from here on

    client_b = appmod.app.test_client()
    login(client_b, approver_b_email, pw)
    token_b_seq = get_csrf(client_b, url_seq)
    client_b.post(url_seq, data={"csrf_token": token_b_seq, "action": "approve_request", "reason": ""}, follow_redirects=True)

    row_mid = db.execute("SELECT approval_status FROM feature_requests WHERE id = ?", (req_id_seq,)).fetchone()
    check("(setup) Approver B's sequential approval succeeded", row_mid["approval_status"] == "Approved")

    resp_stale = client_a.post(url_seq, data={"csrf_token": token_a_seq, "action": "approve_request", "reason": ""}, follow_redirects=True)
    row_final = db.execute("SELECT approval_status, approval_decided_by FROM feature_requests WHERE id = ?", (req_id_seq,)).fetchone()
    check("sequential stale attempt does not overwrite the earlier committed decision", row_final["approval_decided_by"] == approver_b_email)
    check("sequential stale attempt shows the clear already-handled message", "already been handled" in resp_stale.get_data(as_text=True))
    history_count_seq = db.execute("SELECT COUNT(*) FROM feature_request_approvals WHERE feature_request_id = ?", (req_id_seq,)).fetchone()[0]
    check("sequential stale attempt creates no duplicate history row", history_count_seq == 1)

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM feature_request_approvals WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__pi_conc_%')")
    db.execute("DELETE FROM feature_request_status_history WHERE feature_request_id IN (SELECT id FROM feature_requests WHERE requester_email LIKE '__pi_conc_%')")
    db.execute("DELETE FROM feature_requests WHERE requester_email LIKE '__pi_conc_%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
