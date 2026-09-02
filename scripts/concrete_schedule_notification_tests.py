"""
Concrete request scheduling notification -- requested-vs-confirmed time
regression.

Real employee report: a concrete request was originally requested for
7:00 AM. The requested time was unavailable, so procurement changed the
confirmed/scheduled time to 8:00 AM when placing the order. The
automatic WhatsApp message announcing the scheduled delivery to the
Peninsula group still showed 7:00 AM.

Root cause: inventory_concrete_requests has two distinct time columns --
pour_time (the ORIGINALLY REQUESTED time, set at submission and never
touched by the order-placement route) and concrete_arrival_time (the
CONFIRMED/scheduled time, captured specifically on the "Place Order"
form and correctly persisted before any notification is built). Two
notification-building code paths defaulted to pour_time for their
primary displayed time instead of preferring concrete_arrival_time:

  1. inventory_place_concrete_order's immediate "Concrete Scheduled for
     {date} at {time}" WhatsApp message.
  2. build_concrete_order_notification's header line (used for both the
     on-screen order preview and the next-day WhatsApp reminder sent by
     send_due_concrete_reminders).

The fix is a two-line precedence change at those exact call sites:
`concrete_arrival_time or pour_time`, matching the fallback pattern
already used correctly elsewhere in build_concrete_order_notification
(the concrete-company line) and in the place-order template's own
default. pour_time itself, the initial-submission notification, the
edit route, pump/lab/drilling logic, and the schema are all untouched.

This test proves, against the real Flask app and real routes (no
mocking of the app's own logic -- only send_whatsapp_group_message is
stubbed to capture outgoing message text instead of hitting the real
Ultramsg API):

  1. requested 7:00 AM + confirmed 8:00 AM -> the immediate "Concrete
     Scheduled" WhatsApp message says 8:00 AM, not 7:00 AM.
  2. the same request's build_concrete_order_notification() header
     (preview / next-day reminder) also says 8:00 AM.
  3. when concrete_arrival_time is never set, the scheduled notification
     correctly falls back to pour_time (both call sites).
  4. the initial "New concrete request submitted" notification (sent at
     creation, before any scheduling has happened) still uses the
     originally requested pour_time -- unaffected by this fix, as it
     must be: at that point pour_time IS the only known time.

Usage (from the project root):
    APP_ENV=development python3 scripts/concrete_schedule_notification_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import date, datetime, timedelta

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


def make_procurement_user(db, email, name, now, pw_hash):
    db.execute("DELETE FROM users WHERE email=?", (email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", (name, email, pw_hash, now))
    db.commit()
    uid = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    # Needs module:sitepulse:view (see the request) + action:sitepulse:manage
    # (create/edit) + action:sitepulse:place_order (is_procurement(), the
    # Place Order route).
    for key in ("module:sitepulse:view", "action:sitepulse:manage", "action:sitepulse:place_order"):
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

    email = "__concrete_sched@test.local"
    make_procurement_user(db, email, "__concrete_sched", now, pw_hash)

    # Capture outgoing WhatsApp messages instead of hitting the real
    # Ultramsg API. Every other call in the route/functions under test
    # is the real app code -- only the network-facing send is stubbed.
    sent_messages = []

    def fake_send_whatsapp_group_message(text, chat_id=None):
        sent_messages.append(text)
        return True, "ok"

    original_send = appmod.send_whatsapp_group_message
    appmod.send_whatsapp_group_message = fake_send_whatsapp_group_message

    try:
        pour_date = (date.today() + timedelta(days=3)).isoformat()

        with appmod.app.test_client() as client:
            login(client, email, pw)

            # ================================================================
            # 1 & 4. Create a request at 7:00 AM requested time, then confirm
            # it at 8:00 AM via Place Order. Checks both the initial-
            # submission notification (must still say 7:00 AM -- requirement
            # 4) and the immediate scheduled notification (must say 8:00 AM,
            # not 7:00 AM -- requirement 1).
            # ================================================================
            print("=== 1 & 4. Requested 7:00 AM, confirmed 8:00 AM ===")
            sent_messages.clear()
            new_form = {
                "csrf_token": get_csrf(client, "/inventory/concrete/new"),
                "project": "__Test Peninsula Job",
                "job_site_address": "123 Test Way",
                "area_description": "Test Slab",
                "pour_date": pour_date,
                "pour_time": "07:00",
                "mix_design_psi": "4000",
                "mix_slump": "4",
                "concrete_amount": "10 yd",
                "truck_spacing": "15 min",
                "pump_type": "None",
                "pump_size": "",
                "pump_arrival_time": "",
                "lab_required": "No",
                "lab_time": "",
                "drilling_required": "No",
                "drilling_time": "",
                "requested_date": date.today().isoformat(),
            }
            resp = client.post("/inventory/concrete/new", data=new_form, follow_redirects=True)
            check("request created successfully (HTTP 200 after redirect)", resp.status_code == 200)

            check("(setup) exactly one WhatsApp message sent on creation", len(sent_messages) == 1)
            creation_msg = sent_messages[0] if sent_messages else ""
            check("REQUIREMENT 4: initial 'New concrete request submitted' notification uses the requested time (7:00 AM)",
                  "7:00 AM" in creation_msg)
            check("initial notification does not (yet) reference 8:00 AM -- nothing has been confirmed yet",
                  "8:00 AM" not in creation_msg)

            r = db.execute(
                "SELECT * FROM inventory_concrete_requests WHERE project = ? ORDER BY id DESC LIMIT 1",
                ("__Test Peninsula Job",)
            ).fetchone()
            check("(setup) request row found", r is not None)
            check("(setup) pour_time persisted as requested (07:00)", r["pour_time"] == "07:00")
            request_id = r["id"]

            sent_messages.clear()
            order_form = {
                "csrf_token": get_csrf(client, f"/inventory/concrete/{request_id}/order"),
                "concrete_company": "Test Ready Mix",
                "concrete_company_phone": "555-0100",
                "concrete_arrival_time": "08:00",  # the confirmed/rescheduled time -- differs from the 07:00 requested
                "pump_company": "",
                "pump_company_phone": "",
                "pump_arrival_time": "",
                "lab_company": "",
                "lab_time": "",
                "drilling_company": "",
                "drilling_company_phone": "",
                "drilling_time": "",
            }
            resp = client.post(f"/inventory/concrete/{request_id}/order", data=order_form, follow_redirects=True)
            check("order placed successfully (HTTP 200 after redirect)", resp.status_code == 200)

            check("(setup) exactly one WhatsApp message sent on scheduling", len(sent_messages) == 1)
            scheduled_msg = sent_messages[0] if sent_messages else ""
            check("REQUIREMENT 1: immediate 'Concrete Scheduled' WhatsApp message uses the CONFIRMED time (8:00 AM)",
                  "8:00 AM" in scheduled_msg)
            check("REQUIREMENT 1 (negative): immediate scheduled message does NOT show the stale requested time (7:00 AM)",
                  "7:00 AM" not in scheduled_msg)

            r2 = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (request_id,)).fetchone()
            check("(setup) pour_time is untouched by order placement -- still 07:00 (requested-time record preserved for audit)",
                  r2["pour_time"] == "07:00")
            check("(setup) concrete_arrival_time persisted as confirmed (08:00)", r2["concrete_arrival_time"] == "08:00")
            check("(setup) status is now Scheduled", r2["status"] == "Scheduled")

            # ================================================================
            # 2. Same request: build_concrete_order_notification() (the
            # preview / next-day-reminder builder) header must also show the
            # confirmed 8:00 AM, not the requested 7:00 AM.
            # ================================================================
            print()
            print("=== 2. Preview / next-day-reminder header (build_concrete_order_notification) ===")
            notification_text = appmod.build_concrete_order_notification(r2)
            header_line = notification_text.split("\n")[0]
            check("REQUIREMENT 2: preview/reminder header uses the CONFIRMED time (8:00 AM)", "8:00 AM" in header_line)
            check("REQUIREMENT 2 (negative): preview/reminder header does NOT show the stale requested time (7:00 AM)",
                  "7:00 AM" not in header_line)
            check("preview/reminder message still correctly includes the concrete company", "Test Ready Mix" in notification_text)

            # ================================================================
            # 3. A second request where concrete_arrival_time is never set --
            # both notification paths must fall back to pour_time.
            # ================================================================
            print()
            print("=== 3. concrete_arrival_time not set -- must fall back to pour_time ===")
            sent_messages.clear()
            new_form_2 = dict(new_form)
            new_form_2["csrf_token"] = get_csrf(client, "/inventory/concrete/new")
            new_form_2["project"] = "__Test Peninsula Job 2"
            new_form_2["pour_time"] = "07:00"
            resp = client.post("/inventory/concrete/new", data=new_form_2, follow_redirects=True)
            check("(setup) second request created successfully", resp.status_code == 200)

            r3 = db.execute(
                "SELECT * FROM inventory_concrete_requests WHERE project = ? ORDER BY id DESC LIMIT 1",
                ("__Test Peninsula Job 2",)
            ).fetchone()
            request_id_2 = r3["id"]

            sent_messages.clear()
            order_form_2 = dict(order_form)
            order_form_2["csrf_token"] = get_csrf(client, f"/inventory/concrete/{request_id_2}/order")
            order_form_2["concrete_arrival_time"] = ""  # deliberately left blank -- no confirmed time entered
            resp = client.post(f"/inventory/concrete/{request_id_2}/order", data=order_form_2, follow_redirects=True)
            check("(setup) second order placed successfully", resp.status_code == 200)

            fallback_scheduled_msg = sent_messages[0] if sent_messages else ""
            check("REQUIREMENT 3: immediate scheduled message falls back to pour_time (7:00 AM) when concrete_arrival_time is blank",
                  "7:00 AM" in fallback_scheduled_msg)

            r4 = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (request_id_2,)).fetchone()
            check("(setup) concrete_arrival_time is indeed blank on this row", not r4["concrete_arrival_time"])
            fallback_notification = appmod.build_concrete_order_notification(r4)
            fallback_header = fallback_notification.split("\n")[0]
            check("REQUIREMENT 3: preview/reminder header also falls back to pour_time (7:00 AM) when concrete_arrival_time is blank",
                  "7:00 AM" in fallback_header)

            # ================================================================
            # Scope guardrails: pump/other fields untouched by this fix.
            # ================================================================
            print()
            print("=== Scope guardrails ===")
            check("edit route still exists and is unrelated to this fix (not exercised, not modified)",
                  hasattr(appmod, "inventory_edit_concrete"))
            check("pump fields on the confirmed request are exactly what was submitted (pump logic untouched)",
                  r2["pump_type"] == "None" and not r2["pump_arrival_time"])

    finally:
        appmod.send_whatsapp_group_message = original_send

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__Test Peninsula Job%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
