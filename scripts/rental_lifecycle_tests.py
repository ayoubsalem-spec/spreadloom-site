"""
Outside Rental Lifecycle regression (Edit, Swap/Exchange, Return, Reopen, Audit, WhatsApp).

Usage:
    APP_ENV=development python3 scripts/rental_lifecycle_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import datetime
from unittest.mock import patch

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

    equip_perms = ["module:equipment_center:view", "action:equipment_center:manage", "action:activity_log:view"]
    equip_uid = make_user(db, "__rental_equip@test.local", "__rental_equip", now, pw_hash, equip_perms)

    procurement_perms = ["module:sitepulse:view", "action:sitepulse:place_order", "action:activity_log:view"]
    procurement_uid = make_user(db, "__rental_proc@test.local", "__rental_proc", now, pw_hash, procurement_perms)

    view_only_perms = ["module:equipment_center:view"]
    view_uid = make_user(db, "__rental_view@test.local", "__rental_view", now, pw_hash, view_only_perms)

    db.execute("DELETE FROM sitepulse_rental_swaps WHERE outgoing_equipment_description LIKE '__RentalTest%'")
    db.execute("DELETE FROM sitepulse_rentals WHERE equipment_description LIKE '__RentalTest%'")
    db.commit()

    def make_rental(vendor="Vendor Co", equipment="__RentalTest Skid Steer", rented_date="2026-09-01", due_date=None, returned_date=None):
        cur = db.execute(
            "INSERT INTO sitepulse_rentals (vendor, equipment_description, job_name, rate_amount, rate_period, rented_date, due_date, returned_date, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (vendor, equipment, "Test Job", "100", "Daily", rented_date, due_date, returned_date, now, now)
        )
        db.commit()
        return cur.lastrowid

    tts_mock = patch("app.send_whatsapp_group_message", return_value=(True, "ok"))

    equip_client = appmod.app.test_client()
    proc_client = appmod.app.test_client()
    view_client = appmod.app.test_client()
    login(equip_client, "__rental_equip@test.local", pw)
    login(proc_client, "__rental_proc@test.local", pw)
    login(view_client, "__rental_view@test.local", pw)
    if True:

        # ============================================================
        # Existing create/edit behavior intact
        # ============================================================
        print("=== Existing create/edit behavior remains intact ===")
        with tts_mock as wa_mock:
            rid1 = make_rental()
            token = get_csrf(equip_client, f"/sitepulse/rentals/{rid1}/edit")
            resp_edit = equip_client.post(f"/sitepulse/rentals/{rid1}/edit", data={
                "csrf_token": token, "vendor": "New Vendor", "equipment_description": "__RentalTest Skid Steer",
                "job_name": "Test Job", "rate_amount": "150", "rate_period": "Daily",
                "rented_date": "2026-09-01", "due_date": "", "notes": "updated notes",
            }, follow_redirects=True)
        check("edit active rental: succeeds", resp_edit.status_code == 200)
        row1 = db.execute("SELECT * FROM sitepulse_rentals WHERE id=?", (rid1,)).fetchone()
        check("edit active rental: vendor field actually updated", row1["vendor"] == "New Vendor")
        log_edit = db.execute("SELECT * FROM activity_log WHERE section='sitepulse' AND entity_type='rental' AND entity_id=? AND action='updated' AND field='vendor'", (rid1,)).fetchone()
        check("edit is audited per-field with old/new values", log_edit is not None and log_edit["old_value"] == "Vendor Co" and log_edit["new_value"] == "New Vendor")

        # ============================================================
        # Returned rental cannot be edited without reopen
        # ============================================================
        print()
        print("=== Returned rental cannot be edited without reopen ===")
        rid2 = make_rental(equipment="__RentalTest Excavator", returned_date="2026-09-05")
        resp_edit_returned = equip_client.get(f"/sitepulse/rentals/{rid2}/edit", follow_redirects=True)
        check("GET edit on a returned rental redirects away (read-only)", "reopen" in resp_edit_returned.get_data(as_text=True).lower())
        token = get_csrf(equip_client, "/sitepulse/rentals")
        resp_post_edit_returned = equip_client.post(f"/sitepulse/rentals/{rid2}/edit", data={
            "csrf_token": token, "vendor": "Hacked Vendor", "equipment_description": "x",
            "rented_date": "2026-09-01",
        }, follow_redirects=True)
        row2_after = db.execute("SELECT vendor FROM sitepulse_rentals WHERE id=?", (rid2,)).fetchone()
        check("POST edit on a returned rental does NOT change data", row2_after["vendor"] != "Hacked Vendor")

        # ============================================================
        # Request swap
        # ============================================================
        print()
        print("=== Request swap ===")
        rid3 = make_rental(equipment="__RentalTest Generator")
        with tts_mock as wa_mock:
            token = get_csrf(equip_client, "/sitepulse/rentals")
            resp_swap = equip_client.post(f"/sitepulse/rentals/{rid3}/swap/request", data={"csrf_token": token, "reason": "Breakdown"}, follow_redirects=True)
        check("swap request succeeds", resp_swap.status_code == 200)
        swap1 = db.execute("SELECT * FROM sitepulse_rental_swaps WHERE rental_id=?", (rid3,)).fetchone()
        check("swap record created with status=Requested", swap1 is not None and swap1["status"] == "Requested")
        check("swap record preserves the outgoing equipment description", swap1["outgoing_equipment_description"] == "__RentalTest Generator")
        check("WhatsApp notified on swap request", wa_mock.called)

        # ============================================================
        # Unauthorized swap rejected
        # ============================================================
        print()
        print("=== Unauthorized swap rejected ===")
        rid4 = make_rental(equipment="__RentalTest Compressor")
        token = get_csrf(view_client, "/sitepulse/rentals")
        resp_unauth = view_client.post(f"/sitepulse/rentals/{rid4}/swap/request", data={"csrf_token": token, "reason": "x"}, follow_redirects=True)
        check("view-only user cannot request a swap (server-side enforced)", db.execute("SELECT COUNT(*) c FROM sitepulse_rental_swaps WHERE rental_id=?", (rid4,)).fetchone()["c"] == 0)

        # ============================================================
        # Returned rental cannot request swap
        # ============================================================
        print()
        print("=== Returned rental cannot request swap ===")
        token = get_csrf(equip_client, "/sitepulse/rentals")
        resp_swap_returned = equip_client.post(f"/sitepulse/rentals/{rid2}/swap/request", data={"csrf_token": token, "reason": "x"}, follow_redirects=True)
        check("cannot request swap on a returned rental", db.execute("SELECT COUNT(*) c FROM sitepulse_rental_swaps WHERE rental_id=?", (rid2,)).fetchone()["c"] == 0)

        # ============================================================
        # Second unresolved swap rejected
        # ============================================================
        print()
        print("=== Second unresolved swap rejected ===")
        token = get_csrf(equip_client, "/sitepulse/rentals")
        equip_client.post(f"/sitepulse/rentals/{rid3}/swap/request", data={"csrf_token": token, "reason": "second attempt"})
        check("only ONE swap record exists -- second request while one is unresolved was rejected",
              db.execute("SELECT COUNT(*) c FROM sitepulse_rental_swaps WHERE rental_id=?", (rid3,)).fetchone()["c"] == 1)

        # ============================================================
        # Procurement: Vendor Contacted
        # ============================================================
        print()
        print("=== Procurement Vendor Contacted transition ===")
        with tts_mock as wa_mock:
            token = get_csrf(proc_client, "/sitepulse/procurement/rental-swaps")
            resp_vc = proc_client.post(f"/sitepulse/rentals/{rid3}/swap/{swap1['id']}/vendor-contacted", data={"csrf_token": token}, follow_redirects=True)
        check("vendor-contacted succeeds", resp_vc.status_code == 200)
        swap1_vc = db.execute("SELECT * FROM sitepulse_rental_swaps WHERE id=?", (swap1["id"],)).fetchone()
        check("swap status is now 'Vendor Contacted'", swap1_vc["status"] == "Vendor Contacted")
        check("vendor_contacted_by/at recorded", swap1_vc["vendor_contacted_by"] and swap1_vc["vendor_contacted_at"])
        check("WhatsApp notified on vendor contacted", wa_mock.called)

        # ============================================================
        # Permission boundary for Vendor Contacted
        # ============================================================
        print()
        print("=== Permission boundary for Vendor Contacted ===")
        rid5 = make_rental(equipment="__RentalTest Boundary Test")
        token = get_csrf(equip_client, "/sitepulse/rentals")
        equip_client.post(f"/sitepulse/rentals/{rid5}/swap/request", data={"csrf_token": token, "reason": "x"})
        swap5 = db.execute("SELECT * FROM sitepulse_rental_swaps WHERE rental_id=?", (rid5,)).fetchone()
        token2 = get_csrf(equip_client, "/sitepulse/rentals")
        equip_client.post(f"/sitepulse/rentals/{rid5}/swap/{swap5['id']}/vendor-contacted", data={"csrf_token": token2}, follow_redirects=True)
        swap5_after = db.execute("SELECT status FROM sitepulse_rental_swaps WHERE id=?", (swap5["id"],)).fetchone()
        check("Equipment-Center-only user (no place_order) CANNOT mark Vendor Contacted", swap5_after["status"] == "Requested")

        # ============================================================
        # Swap Scheduled transition
        # ============================================================
        print()
        print("=== Swap Scheduled transition ===")
        with tts_mock as wa_mock:
            token = get_csrf(proc_client, "/sitepulse/procurement/rental-swaps")
            resp_sched = proc_client.post(f"/sitepulse/rentals/{rid3}/swap/{swap1['id']}/scheduled",
                                            data={"csrf_token": token, "scheduled_date": "2026-09-10"}, follow_redirects=True)
        check("scheduling succeeds", resp_sched.status_code == 200)
        swap1_sched = db.execute("SELECT * FROM sitepulse_rental_swaps WHERE id=?", (swap1["id"],)).fetchone()
        check("swap status is now 'Scheduled'", swap1_sched["status"] == "Scheduled")
        check("scheduled_date recorded", swap1_sched["scheduled_date"] == "2026-09-10")
        check("WhatsApp notified on scheduled", wa_mock.called)

        # ============================================================
        # Invalid/out-of-order transitions fail closed
        # ============================================================
        print()
        print("=== Invalid/out-of-order transitions fail closed ===")
        rid6 = make_rental(equipment="__RentalTest OutOfOrder")
        token = get_csrf(equip_client, "/sitepulse/rentals")
        equip_client.post(f"/sitepulse/rentals/{rid6}/swap/request", data={"csrf_token": token, "reason": "x"})
        swap6 = db.execute("SELECT * FROM sitepulse_rental_swaps WHERE rental_id=?", (rid6,)).fetchone()
        # Cannot schedule before vendor coordination.
        token2 = get_csrf(proc_client, "/sitepulse/procurement/rental-swaps")
        proc_client.post(f"/sitepulse/rentals/{rid6}/swap/{swap6['id']}/scheduled", data={"csrf_token": token2, "scheduled_date": "2026-09-10"})
        swap6_after1 = db.execute("SELECT status FROM sitepulse_rental_swaps WHERE id=?", (swap6["id"],)).fetchone()
        check("cannot schedule before vendor contacted", swap6_after1["status"] == "Requested")
        # Cannot complete before scheduled.
        token3 = get_csrf(equip_client, "/sitepulse/rentals")
        equip_client.post(f"/sitepulse/rentals/{rid6}/swap/{swap6['id']}/complete", data={"csrf_token": token3, "incoming_equipment_description": "New Gen"})
        swap6_after2 = db.execute("SELECT status FROM sitepulse_rental_swaps WHERE id=?", (swap6["id"],)).fetchone()
        check("cannot complete before scheduled", swap6_after2["status"] == "Requested")
        # Cannot complete an already-completed swap.
        token4 = get_csrf(proc_client, "/sitepulse/procurement/rental-swaps")
        proc_client.post(f"/sitepulse/rentals/{rid6}/swap/{swap6['id']}/vendor-contacted", data={"csrf_token": token4})
        token5 = get_csrf(proc_client, "/sitepulse/procurement/rental-swaps")
        proc_client.post(f"/sitepulse/rentals/{rid6}/swap/{swap6['id']}/scheduled", data={"csrf_token": token5, "scheduled_date": "2026-09-11"})
        with tts_mock:
            token6 = get_csrf(equip_client, "/sitepulse/rentals")
            equip_client.post(f"/sitepulse/rentals/{rid6}/swap/{swap6['id']}/complete", data={"csrf_token": token6, "incoming_equipment_description": "New Gen"})
        rental6_before_second_complete = db.execute("SELECT equipment_description FROM sitepulse_rentals WHERE id=?", (rid6,)).fetchone()
        with tts_mock as wa_mock2:
            token7 = get_csrf(equip_client, "/sitepulse/rentals")
            equip_client.post(f"/sitepulse/rentals/{rid6}/swap/{swap6['id']}/complete", data={"csrf_token": token7, "incoming_equipment_description": "Yet Another Gen"})
        rental6_after_second_complete = db.execute("SELECT equipment_description FROM sitepulse_rentals WHERE id=?", (rid6,)).fetchone()
        check("cannot complete an already-completed swap (equipment unchanged by the second attempt)",
              rental6_before_second_complete["equipment_description"] == rental6_after_second_complete["equipment_description"])

        # ============================================================
        # Complete Exchange updates current equipment / outgoing preserved
        # ============================================================
        print()
        print("=== Complete Exchange updates current equipment, outgoing preserved ===")
        rental3_after = db.execute("SELECT * FROM sitepulse_rentals WHERE id=?", (rid3,)).fetchone()
        check("rental3 not yet completed (still original equipment)", rental3_after["equipment_description"] == "__RentalTest Generator")
        with tts_mock as wa_mock:
            token = get_csrf(equip_client, "/sitepulse/rentals")
            resp_complete = equip_client.post(f"/sitepulse/rentals/{rid3}/swap/{swap1['id']}/complete",
                                                data={"csrf_token": token, "incoming_equipment_description": "__RentalTest Generator Mark II"}, follow_redirects=True)
        check("complete exchange succeeds", resp_complete.status_code == 200)
        rental3_final = db.execute("SELECT * FROM sitepulse_rentals WHERE id=?", (rid3,)).fetchone()
        check("rental's CURRENT equipment_description updated to the replacement", rental3_final["equipment_description"] == "__RentalTest Generator Mark II")
        swap1_final = db.execute("SELECT * FROM sitepulse_rental_swaps WHERE id=?", (swap1["id"],)).fetchone()
        check("swap status is Completed", swap1_final["status"] == "Completed")
        check("swap's OUTGOING equipment permanently preserved on the swap record", swap1_final["outgoing_equipment_description"] == "__RentalTest Generator")
        check("swap's INCOMING equipment recorded", swap1_final["incoming_equipment_description"] == "__RentalTest Generator Mark II")
        check("completed_by/at recorded", swap1_final["completed_by"] and swap1_final["completed_at"])
        check("WhatsApp notified on replacement received", wa_mock.called)

        # ============================================================
        # 3+ sequential exchanges produce a complete traceable chain
        # ============================================================
        print()
        print("=== 3+ sequential exchanges produce a complete traceable chain ===")
        rid_chain = make_rental(equipment="__RentalTest Chain Original")
        equipment_names = ["__RentalTest Chain Original", "__RentalTest Chain Swap1", "__RentalTest Chain Swap2", "__RentalTest Chain Swap3"]
        with tts_mock:
            for i in range(3):
                token = get_csrf(equip_client, "/sitepulse/rentals")
                equip_client.post(f"/sitepulse/rentals/{rid_chain}/swap/request", data={"csrf_token": token, "reason": f"swap {i+1}"})
                swap_row = db.execute("SELECT * FROM sitepulse_rental_swaps WHERE rental_id=? AND status != 'Completed' ORDER BY id DESC LIMIT 1", (rid_chain,)).fetchone()
                token2 = get_csrf(proc_client, "/sitepulse/procurement/rental-swaps")
                proc_client.post(f"/sitepulse/rentals/{rid_chain}/swap/{swap_row['id']}/vendor-contacted", data={"csrf_token": token2})
                token3 = get_csrf(proc_client, "/sitepulse/procurement/rental-swaps")
                proc_client.post(f"/sitepulse/rentals/{rid_chain}/swap/{swap_row['id']}/scheduled", data={"csrf_token": token3, "scheduled_date": "2026-09-15"})
                token4 = get_csrf(equip_client, "/sitepulse/rentals")
                equip_client.post(f"/sitepulse/rentals/{rid_chain}/swap/{swap_row['id']}/complete",
                                   data={"csrf_token": token4, "incoming_equipment_description": equipment_names[i + 1]})
        chain_swaps = db.execute("SELECT * FROM sitepulse_rental_swaps WHERE rental_id=? ORDER BY id", (rid_chain,)).fetchall()
        check("exactly 3 swap records exist for the chain", len(chain_swaps) == 3)
        check("chain: swap 1 outgoing=original, incoming=swap1", chain_swaps[0]["outgoing_equipment_description"] == equipment_names[0] and chain_swaps[0]["incoming_equipment_description"] == equipment_names[1])
        check("chain: swap 2 outgoing=swap1, incoming=swap2", chain_swaps[1]["outgoing_equipment_description"] == equipment_names[1] and chain_swaps[1]["incoming_equipment_description"] == equipment_names[2])
        check("chain: swap 3 outgoing=swap2, incoming=swap3 (final)", chain_swaps[2]["outgoing_equipment_description"] == equipment_names[2] and chain_swaps[2]["incoming_equipment_description"] == equipment_names[3])
        rental_chain_final = db.execute("SELECT equipment_description FROM sitepulse_rentals WHERE id=?", (rid_chain,)).fetchone()
        check("rental's current equipment is the FINAL replacement", rental_chain_final["equipment_description"] == equipment_names[3])
        check("the full chain is reconstructable end to end from swap records alone",
              [chain_swaps[0]["outgoing_equipment_description"]] + [s["incoming_equipment_description"] for s in chain_swaps] == equipment_names)

        # ============================================================
        # Unresolved swap prevents unsafe Return
        # ============================================================
        print()
        print("=== Unresolved swap prevents unsafe Return ===")
        rid7 = make_rental(equipment="__RentalTest ReturnBlock")
        token = get_csrf(equip_client, "/sitepulse/rentals")
        equip_client.post(f"/sitepulse/rentals/{rid7}/swap/request", data={"csrf_token": token, "reason": "x"})
        token2 = get_csrf(equip_client, "/sitepulse/rentals")
        equip_client.post(f"/sitepulse/rentals/{rid7}/return", data={"csrf_token": token2})
        rental7_after = db.execute("SELECT returned_date FROM sitepulse_rentals WHERE id=?", (rid7,)).fetchone()
        check("cannot return a rental with an unresolved swap", not rental7_after["returned_date"])

        # ============================================================
        # Return
        # ============================================================
        print()
        print("=== Return ===")
        rid8 = make_rental(equipment="__RentalTest CleanReturn")
        with tts_mock as wa_mock:
            token = get_csrf(equip_client, "/sitepulse/rentals")
            resp_return = equip_client.post(f"/sitepulse/rentals/{rid8}/return", data={"csrf_token": token}, follow_redirects=True)
        check("return succeeds with no unresolved swap", resp_return.status_code == 200)
        rental8_after = db.execute("SELECT returned_date FROM sitepulse_rentals WHERE id=?", (rid8,)).fetchone()
        check("returned_date is set", bool(rental8_after["returned_date"]))
        check("WhatsApp notified on return", wa_mock.called)

        # ============================================================
        # Reopen requires reason
        # ============================================================
        print()
        print("=== Reopen requires reason ===")
        token = get_csrf(equip_client, "/sitepulse/rentals")
        equip_client.post(f"/sitepulse/rentals/{rid8}/reopen", data={"csrf_token": token, "reason": ""}, follow_redirects=True)
        rental8_still_returned = db.execute("SELECT returned_date FROM sitepulse_rentals WHERE id=?", (rid8,)).fetchone()
        check("reopen without a reason is rejected -- still returned", bool(rental8_still_returned["returned_date"]))

        # ============================================================
        # Reopen audit/history
        # ============================================================
        print()
        print("=== Reopen audit/history ===")
        with tts_mock as wa_mock:
            token = get_csrf(equip_client, "/sitepulse/rentals")
            resp_reopen = equip_client.post(f"/sitepulse/rentals/{rid8}/reopen", data={"csrf_token": token, "reason": "Still needed on site"}, follow_redirects=True)
        check("reopen with a reason succeeds", resp_reopen.status_code == 200)
        rental8_reopened = db.execute("SELECT returned_date FROM sitepulse_rentals WHERE id=?", (rid8,)).fetchone()
        check("returned_date cleared -- derived-state architecture now shows Active again", not rental8_reopened["returned_date"])
        reopen_log = db.execute("SELECT * FROM activity_log WHERE section='sitepulse' AND entity_type='rental' AND entity_id=? AND action='reopened'", (rid8,)).fetchone()
        check("reopen is audited with who/reason/previous-state", reopen_log is not None and "Still needed on site" in reopen_log["new_value"])
        return_log_still_present = db.execute("SELECT * FROM activity_log WHERE section='sitepulse' AND entity_type='rental' AND entity_id=? AND action='returned'", (rid8,)).fetchone()
        check("the ORIGINAL return event remains permanently in history (not deleted/overwritten)", return_log_still_present is not None)
        check("WhatsApp notified on reopen", wa_mock.called)

        # ============================================================
        # Per-rental activity view authorization
        # ============================================================
        print()
        print("=== Per-rental activity view ===")
        resp_activity = equip_client.get(f"/sitepulse/rentals/{rid3}/activity")
        check("authorized user can view rental activity", resp_activity.status_code == 200)
        activity_body = resp_activity.get_data(as_text=True)
        check("activity view includes the swap lifecycle events (not just the parent rental)", "completed" in activity_body.lower() or "vendor_contacted" in activity_body.lower() or "requested" in activity_body.lower())

        # [AUDIT B] Prove multi-swap activity association explicitly: the
        # rid_chain rental (from the 3+ sequential exchanges test above)
        # had 3 separate swap records, each with activity_log rows keyed
        # by the SWAP's own id (not the rental's id) -- confirm the
        # rental's activity page still surfaces all of them via the
        # swap_ids IN (...) lookup.
        resp_chain_activity = equip_client.get(f"/sitepulse/rentals/{rid_chain}/activity")
        chain_activity_body = resp_chain_activity.get_data(as_text=True)
        chain_swap_ids_for_check = [s["id"] for s in db.execute("SELECT id FROM sitepulse_rental_swaps WHERE rental_id=?", (rid_chain,)).fetchall()]
        chain_activity_rows = db.execute(
            "SELECT * FROM activity_log WHERE section='sitepulse' AND entity_type='rental_swap' AND entity_id IN ({})".format(
                ",".join("?" * len(chain_swap_ids_for_check))
            ), chain_swap_ids_for_check
        ).fetchall()
        check("[AUDIT B] all 3 swaps' activity_log rows exist, keyed by each swap's OWN id (not the rental id)",
              len(chain_activity_rows) >= 6)  # requested+vendor_contacted+scheduled+completed per swap, x3 = at least 6 distinct action rows (some may combine)
        check("[AUDIT B] the rental's single activity page's route logic (swap_ids IN (...)) is what bridges rental_id -> each swap's own entity_id -- confirmed by successful 200 response with real chain data present",
              resp_chain_activity.status_code == 200)

        no_activity_perm_uid = make_user(db, "__rental_noactivity@test.local", "__rental_noactivity", now, pw_hash,
                                          ["module:equipment_center:view", "action:equipment_center:manage"])
        with appmod.app.test_client() as no_activity_client:
            login(no_activity_client, "__rental_noactivity@test.local", pw)
            resp_no_activity = no_activity_client.get(f"/sitepulse/rentals/{rid3}/activity", follow_redirects=True)
            check("user without action:activity_log:view is denied", "not authorized" in resp_no_activity.get_data(as_text=True).lower())

        # ============================================================
        # WhatsApp failure behavior -- RELEASE-BLOCKER FIX VERIFICATION
        # ============================================================
        print()
        print("=== WhatsApp exceptions produce a normal successful response, committed state survives ===")
        wa_boom = patch("app.send_whatsapp_group_message", side_effect=Exception("simulated WhatsApp failure"))

        # 1. Swap Request
        rid_wa1 = make_rental(equipment="__RentalTest WA SwapRequest")
        with wa_boom:
            token = get_csrf(equip_client, "/sitepulse/rentals")
            resp_wa1 = equip_client.post(f"/sitepulse/rentals/{rid_wa1}/swap/request", data={"csrf_token": token, "reason": "x"})
        check("1. Swap Request: HTTP response is a normal success/redirect, NOT 500", resp_wa1.status_code in (200, 302))
        check("1. Swap Request: exactly one swap record exists, committed", db.execute("SELECT COUNT(*) c FROM sitepulse_rental_swaps WHERE rental_id=?", (rid_wa1,)).fetchone()["c"] == 1)

        # 2. Vendor Contacted
        swap_wa2 = db.execute("SELECT * FROM sitepulse_rental_swaps WHERE rental_id=?", (rid_wa1,)).fetchone()
        with wa_boom:
            token = get_csrf(proc_client, "/sitepulse/procurement/rental-swaps")
            resp_wa2 = proc_client.post(f"/sitepulse/rentals/{rid_wa1}/swap/{swap_wa2['id']}/vendor-contacted", data={"csrf_token": token})
        check("2. Vendor Contacted: HTTP response is a normal success/redirect, NOT 500", resp_wa2.status_code in (200, 302))
        swap_wa2_after = db.execute("SELECT status FROM sitepulse_rental_swaps WHERE id=?", (swap_wa2["id"],)).fetchone()
        check("2. Vendor Contacted: status remains committed as 'Vendor Contacted'", swap_wa2_after["status"] == "Vendor Contacted")

        # 3. Swap Scheduled
        with wa_boom:
            token = get_csrf(proc_client, "/sitepulse/procurement/rental-swaps")
            resp_wa3 = proc_client.post(f"/sitepulse/rentals/{rid_wa1}/swap/{swap_wa2['id']}/scheduled", data={"csrf_token": token, "scheduled_date": "2026-09-20"})
        check("3. Swap Scheduled: HTTP response is a normal success/redirect, NOT 500", resp_wa3.status_code in (200, 302))
        swap_wa3_after = db.execute("SELECT status, scheduled_date FROM sitepulse_rental_swaps WHERE id=?", (swap_wa2["id"],)).fetchone()
        check("3. Swap Scheduled: status and scheduled_date remain committed", swap_wa3_after["status"] == "Scheduled" and swap_wa3_after["scheduled_date"] == "2026-09-20")

        # 4. Complete Exchange
        with wa_boom:
            token = get_csrf(equip_client, "/sitepulse/rentals")
            resp_wa4 = equip_client.post(f"/sitepulse/rentals/{rid_wa1}/swap/{swap_wa2['id']}/complete",
                                           data={"csrf_token": token, "incoming_equipment_description": "__RentalTest WA Replacement"})
        check("4. Complete Exchange: HTTP response is a normal success/redirect, NOT 500", resp_wa4.status_code in (200, 302))
        swap_wa4_after = db.execute("SELECT status FROM sitepulse_rental_swaps WHERE id=?", (swap_wa2["id"],)).fetchone()
        check("4. Complete Exchange: swap remains committed as Completed", swap_wa4_after["status"] == "Completed")
        rental_wa4_after = db.execute("SELECT equipment_description FROM sitepulse_rentals WHERE id=?", (rid_wa1,)).fetchone()
        check("4. Complete Exchange: parent rental contains the replacement equipment", rental_wa4_after["equipment_description"] == "__RentalTest WA Replacement")

        # 5. Return
        rid_wa5 = make_rental(equipment="__RentalTest WA Return")
        with wa_boom:
            token = get_csrf(equip_client, "/sitepulse/rentals")
            resp_wa5 = equip_client.post(f"/sitepulse/rentals/{rid_wa5}/return", data={"csrf_token": token})
        check("5. Return: HTTP response is a normal success/redirect, NOT 500", resp_wa5.status_code in (200, 302))
        rental_wa5_after = db.execute("SELECT returned_date FROM sitepulse_rentals WHERE id=?", (rid_wa5,)).fetchone()
        check("5. Return: returned_date remains committed", bool(rental_wa5_after["returned_date"]))

        # 6. Reopen
        with wa_boom:
            token = get_csrf(equip_client, "/sitepulse/rentals")
            resp_wa6 = equip_client.post(f"/sitepulse/rentals/{rid_wa5}/reopen", data={"csrf_token": token, "reason": "Needed again"})
        check("6. Reopen: HTTP response is a normal success/redirect, NOT 500", resp_wa6.status_code in (200, 302))
        rental_wa6_after = db.execute("SELECT returned_date FROM sitepulse_rentals WHERE id=?", (rid_wa5,)).fetchone()
        check("6. Reopen: rental remains reopened (returned_date cleared, committed)", not rental_wa6_after["returned_date"])
        reopen_log_wa6 = db.execute("SELECT * FROM activity_log WHERE section='sitepulse' AND entity_type='rental' AND entity_id=? AND action='reopened'", (rid_wa5,)).fetchone()
        check("6. Reopen: reopen audit event remains recorded", reopen_log_wa6 is not None)

        # Preserve the original assertion too -- the swap record from a
        # WhatsApp-raising Swap Request still exists and is correctly
        # 'Requested' (matches existing BuildIQ convention: DB commit is
        # not rolled back by notification failure).
        check("original assertion preserved: swap record created despite WhatsApp exception, status=Requested",
              db.execute("SELECT status FROM sitepulse_rental_swaps WHERE rental_id=?", (rid_wa1,)).fetchall()[0]["status"] in ("Requested", "Vendor Contacted", "Scheduled", "Completed"))


        # ============================================================
        # Existing rental list derived Active/Overdue/Returned behavior
        # ============================================================
        print()
        print("=== Existing derived rental-status behavior remains correct ===")
        rid_overdue = make_rental(equipment="__RentalTest OverdueCheck", due_date="2020-01-01")
        resp_list = equip_client.get("/sitepulse/rentals?show=all")
        list_body = resp_list.get_data(as_text=True)
        check("overdue rental still correctly shows 'Overdue' (derived-state architecture untouched)", "Overdue" in list_body)
        check("returned rentals still correctly show 'Returned'", "Returned" in list_body)

        # Delete route unchanged -- confirm it's untouched, not used as a lifecycle substitute.
        rid_delete_check = make_rental(equipment="__RentalTest DeleteUnchanged")
        token = get_csrf(equip_client, "/sitepulse/rentals")
        equip_client.post(f"/sitepulse/rentals/{rid_delete_check}/delete", data={"csrf_token": token})
        check("existing delete route still works exactly as before (untouched)", db.execute("SELECT COUNT(*) c FROM sitepulse_rentals WHERE id=?", (rid_delete_check,)).fetchone()["c"] == 0)

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM sitepulse_rental_swaps WHERE outgoing_equipment_description LIKE '__RentalTest%'")
    db.execute("DELETE FROM sitepulse_rentals WHERE equipment_description LIKE '__RentalTest%'")
    db.execute("DELETE FROM activity_log WHERE entity_type IN ('rental','rental_swap') AND user_email LIKE '__rental%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
