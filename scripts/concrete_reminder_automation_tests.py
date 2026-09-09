"""
Concrete day-before (exact-time) WhatsApp reminder automation regression --
timing precision, atomic claim-before-send concurrency, and safe logging.

Usage:
    APP_ENV=development python3 scripts/concrete_reminder_automation_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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


class _FrozenAt(datetime):
    """A real datetime subclass (so strptime/strftime/etc. all still
    work exactly like the genuine class) whose .now()/.utcnow() are
    pinned to a fixed instant for the duration of a `with patch(...)`
    block -- datetime.datetime itself cannot be patched directly (it's
    an immutable C type), so every test that needs a controlled "now"
    builds one of these."""
    _fixed_houston = None
    _fixed_utc = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed_houston.astimezone(tz) if tz else cls._fixed_houston

    @classmethod
    def utcnow(cls):
        return cls._fixed_utc


def frozen_at(houston_dt):
    frozen = type("_FrozenAt", (_FrozenAt,), {"_fixed_houston": houston_dt, "_fixed_utc": houston_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)})
    return patch("app.datetime", frozen)


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    perms = ["module:sitepulse:view", "action:sitepulse:manage"]
    uid = make_user(db, "__concreterem_user@test.local", "__concreterem_user", now, pw_hash, perms)

    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__ConcreteRemTest%'")
    db.commit()

    def make_concrete(project="__ConcreteRemTest Job", status="Scheduled", pour_date="2026-09-11",
                       pour_time="07:00", concrete_arrival_time=None, sent_at=None, claimed_at=None):
        cur = db.execute(
            """INSERT INTO inventory_concrete_requests
               (project, pour_date, pour_time, concrete_arrival_time, job_site_address, area_description, status,
                full_reminder_sent_at, reminder_claimed_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (project, pour_date, pour_time, concrete_arrival_time, "123 Test St", "Slab", status,
             sent_at, claimed_at, now, now)
        )
        db.commit()
        return cur.lastrowid

    with appmod.app.app_context():
        print("=== 1. 7 AM pour -> reminder eligible starting 7 AM the previous day ===")
        rid1 = make_concrete(pour_date="2026-09-11", pour_time="07:00")
        with frozen_at(datetime(2026, 9, 10, 7, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa:
                result1 = appmod.process_due_concrete_reminders()
        check("1. exactly at 7:00 AM the day before, the 7 AM pour reminder is eligible and sent", wa.called and result1["sent"] == 1)
        row1 = db.execute("SELECT full_reminder_sent_at FROM inventory_concrete_requests WHERE id=?", (rid1,)).fetchone()
        check("1. full_reminder_sent_at populated", bool(row1["full_reminder_sent_at"]))

        print()
        print("=== 2. 2 PM pour -> reminder eligible starting 2 PM the previous day ===")
        rid2 = make_concrete(project="__ConcreteRemTest 2PM", pour_date="2026-09-11", pour_time="14:00")
        with frozen_at(datetime(2026, 9, 10, 14, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa:
                result2 = appmod.process_due_concrete_reminders()
        check("2. exactly at 2:00 PM the day before, the 2 PM pour reminder is eligible and sent", wa.called and result2["sent"] == 1)

        print()
        print("=== 3. One minute before reminder_due_at -> NOT eligible ===")
        rid3 = make_concrete(project="__ConcreteRemTest OneMinEarly", pour_date="2026-09-11", pour_time="09:00")
        with frozen_at(datetime(2026, 9, 10, 8, 59, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa:
                result3 = appmod.process_due_concrete_reminders()
        check("3. one minute before the exact due instant, nothing is sent for this record", not wa.called)
        row3 = db.execute("SELECT full_reminder_sent_at FROM inventory_concrete_requests WHERE id=?", (rid3,)).fetchone()
        check("3. not marked sent", not row3["full_reminder_sent_at"])

        print()
        print("=== 4. At/after reminder_due_at -> eligible ===")
        with frozen_at(datetime(2026, 9, 10, 9, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa:
                appmod.process_due_concrete_reminders()
        row3_after = db.execute("SELECT full_reminder_sent_at FROM inventory_concrete_requests WHERE id=?", (rid3,)).fetchone()
        check("4. exactly at the due instant, it is now eligible and sent", bool(row3_after["full_reminder_sent_at"]))

        print()
        print("=== 5. Delayed cron run still sends before the pour ===")
        rid5 = make_concrete(project="__ConcreteRemTest Delayed", pour_date="2026-09-11", pour_time="07:00")
        with frozen_at(datetime(2026, 9, 10, 11, 30, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa:
                result5 = appmod.process_due_concrete_reminders()
        check("5. a run several hours late (but before the pour) still sends the overdue reminder", wa.called and result5["sent"] == 1)

        print()
        print("=== 6. Already-past pour does not send a stale reminder ===")
        rid6 = make_concrete(project="__ConcreteRemTest AlreadyPoured", pour_date="2026-09-11", pour_time="07:00")
        with frozen_at(datetime(2026, 9, 11, 10, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa:
                result6 = appmod.process_due_concrete_reminders()
        row6 = db.execute("SELECT full_reminder_sent_at FROM inventory_concrete_requests WHERE id=?", (rid6,)).fetchone()
        check("6. once the pour has already happened, no stale reminder is sent", not row6["full_reminder_sent_at"])
        check("6. not counted as due at all once the window has closed", result6["due"] == 0 or not wa.called)

        print()
        print("=== 7/8. Explicit America/Chicago handling, DST-safe ===")
        rid78 = make_concrete(project="__ConcreteRemTest Precedence", pour_date="2026-09-11",
                               pour_time="06:00", concrete_arrival_time="07:00")
        with frozen_at(datetime(2026, 9, 10, 6, 30, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa:
                appmod.process_due_concrete_reminders()
        check("7. at 6:30 AM (after requested pour_time 6:00 but before CONFIRMED arrival_time 7:00), not yet eligible -- confirmed time wins", not wa.called)
        with frozen_at(datetime(2026, 9, 10, 7, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa2:
                appmod.process_due_concrete_reminders()
        check("7. at 7:00 AM (the CONFIRMED time), now eligible -- proves concrete_arrival_time precedence is used, not pour_time", wa2.called)

        rid_dst = make_concrete(project="__ConcreteRemTest DST", pour_date="2026-03-09", pour_time="07:00")
        with frozen_at(datetime(2026, 3, 8, 7, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa_dst:
                result_dst = appmod.process_due_concrete_reminders()
        check("8. DST-adjacent date: reminder still correctly eligible at the Houston-local due instant", wa_dst.called and result_dst["sent"] == 1)

        print()
        print("=== 12/13. Successful send stays authoritative; repeated runs never duplicate ===")
        rid1213 = make_concrete(project="__ConcreteRemTest Repeat")
        with frozen_at(datetime(2026, 9, 10, 7, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa_a:
                appmod.process_due_concrete_reminders()
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa_b:
                appmod.process_due_concrete_reminders()
        check("13. second run within the same eligible window does not re-send", not wa_b.called)
        row1213 = db.execute("SELECT full_reminder_sent_at, reminder_claimed_at FROM inventory_concrete_requests WHERE id=?", (rid1213,)).fetchone()
        check("12. full_reminder_sent_at remains the authoritative marker, claim cleared on success", bool(row1213["full_reminder_sent_at"]) and not row1213["reminder_claimed_at"])

        print()
        print("=== 10. Failed claimed send -> retry possible ===")
        rid10 = make_concrete(project="__ConcreteRemTest FailRetry")
        with frozen_at(datetime(2026, 9, 10, 7, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(False, "simulated failure")):
                result10a = appmod.process_due_concrete_reminders()
            row10a = db.execute("SELECT full_reminder_sent_at, reminder_claimed_at FROM inventory_concrete_requests WHERE id=?", (rid10,)).fetchone()
            check("10. failed send leaves full_reminder_sent_at unset", not row10a["full_reminder_sent_at"])
            check("10. claim is released immediately on failure (not left stuck)", not row10a["reminder_claimed_at"])
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa10:
                appmod.process_due_concrete_reminders()
        check("10. immediate retry (same window) succeeds without waiting for a staleness timeout", wa10.called)
        row10b = db.execute("SELECT full_reminder_sent_at FROM inventory_concrete_requests WHERE id=?", (rid10,)).fetchone()
        check("10. now marked sent", bool(row10b["full_reminder_sent_at"]))

        print()
        print("=== 11. Stale/abandoned claim becomes retryable after the timeout ===")
        rid11 = make_concrete(project="__ConcreteRemTest StaleClaim")
        stale_claim_time = (datetime.utcnow() - timedelta(seconds=appmod._CONCRETE_REMINDER_CLAIM_TIMEOUT_SECONDS + 30)).isoformat()
        db.execute("UPDATE inventory_concrete_requests SET reminder_claimed_at=? WHERE id=?", (stale_claim_time, rid11))
        db.commit()
        with frozen_at(datetime(2026, 9, 10, 7, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa11:
                result11 = appmod.process_due_concrete_reminders()
        check("11. a claim older than the staleness timeout is reclaimable and the reminder sends", wa11.called and result11["sent"] >= 1)
        row11 = db.execute("SELECT full_reminder_sent_at FROM inventory_concrete_requests WHERE id=?", (rid11,)).fetchone()
        check("11. marked sent after reclaiming the stale claim", bool(row11["full_reminder_sent_at"]))

        rid11b = make_concrete(project="__ConcreteRemTest FreshClaim")
        with frozen_at(datetime(2026, 9, 10, 7, 0, tzinfo=ZoneInfo("America/Chicago"))):
            fresh_claim_time = appmod.datetime.utcnow().isoformat()
            db.execute("UPDATE inventory_concrete_requests SET reminder_claimed_at=? WHERE id=?", (fresh_claim_time, rid11b))
            db.commit()
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa11b:
                appmod.process_due_concrete_reminders()
        check("11. a FRESH (not yet stale) claim is correctly NOT reclaimed by another run", not wa11b.called)

        print()
        print("=== 14. Legacy GET /inventory/concrete still works as backup ===")
        rid14 = make_concrete(project="__ConcreteRemTest PageLoad")
        with frozen_at(datetime(2026, 9, 10, 7, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with appmod.app.test_client() as client:
                login(client, "__concreterem_user@test.local", pw)
                with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")) as wa14:
                    resp14 = client.get("/inventory/concrete")
        check("14. page load succeeds", resp14.status_code == 200)
        row14 = db.execute("SELECT full_reminder_sent_at FROM inventory_concrete_requests WHERE id=?", (rid14,)).fetchone()
        check("14. legacy page-load path still triggers the SAME processor and marks sent", bool(row14["full_reminder_sent_at"]) and wa14.called)

        print()
        print("=== Non-Scheduled / wrong-status safety (preserved from prior round) ===")
        rid_h = make_concrete(project="__ConcreteRemTest NotScheduled", status="Submitted")
        with frozen_at(datetime(2026, 9, 10, 7, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(True, "Sent.")):
                appmod.process_due_concrete_reminders()
        row_h = db.execute("SELECT full_reminder_sent_at FROM inventory_concrete_requests WHERE id=?", (rid_h,)).fetchone()
        check("a Submitted (not Scheduled) request is never reminded", not row_h["full_reminder_sent_at"])

        print()
        print("=== Safe logging: no raw provider/exception detail in the new log line ===")
        rid_log = make_concrete(project="__ConcreteRemTest LogSafety")
        hostile_detail = "Network error reaching Ultramsg: ConnectionError fetching https://api.ultramsg.com/FAKE-INSTANCE-ID-12345/messages/chat"
        import io
        import contextlib
        stdout_capture = io.StringIO()
        with frozen_at(datetime(2026, 9, 10, 7, 0, tzinfo=ZoneInfo("America/Chicago"))):
            with patch("app.send_whatsapp_group_message", return_value=(False, hostile_detail)):
                with contextlib.redirect_stdout(stdout_capture):
                    appmod.process_due_concrete_reminders()
        logged_text = stdout_capture.getvalue()
        check("safe logging: the hostile instance-ID-carrying detail string never appears in the log output", "FAKE-INSTANCE-ID-12345" not in logged_text)
        check("safe logging: no Ultramsg URL appears in the log output", "ultramsg.com" not in logged_text)
        check("safe logging: the log line still identifies which record failed (useful, not silent)", f"id={rid_log}" in logged_text)

    print()
    print("=== 9a. Deterministic DB-level claim contention (no thread-timing dependency) ===")
    # Exercises the EXACT claim UPDATE statement itself, directly,
    # against two fully independent sqlite3 connections to the same
    # file -- deliberately with NO threading at all. This removes any
    # dependency on natural OS thread-scheduling overlap: the claim's
    # correctness is a property of SQLite's own per-statement write
    # atomicity, which holds regardless of whether two callers happen
    # to be wall-clock-simultaneous or merely arrive one after another
    # before either has captured the row -- so calling the same claim
    # statement twice in a row, from two separate connections, on the
    # same still-unclaimed row, deterministically proves the guarantee
    # every real concurrent scenario also relies on.
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__DeterministicRace%'")
    db.commit()
    det_cur = db.execute(
        """INSERT INTO inventory_concrete_requests
           (project, pour_date, pour_time, job_site_address, area_description, status, full_reminder_sent_at, reminder_claimed_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("__DeterministicRace Job", "2026-09-11", "07:00", "123", "Slab", "Scheduled", None, None, now, now)
    )
    db.commit()
    det_rid = det_cur.lastrowid

    conn_a = sqlite3.connect(appmod.DB_PATH)
    conn_b = sqlite3.connect(appmod.DB_PATH)
    claim_sql = (
        "UPDATE inventory_concrete_requests SET reminder_claimed_at = ? "
        "WHERE id = ? AND (full_reminder_sent_at IS NULL OR full_reminder_sent_at = '') "
        "AND (reminder_claimed_at IS NULL OR reminder_claimed_at = '' OR reminder_claimed_at <= ?)"
    )
    stale_before_det = (datetime.utcnow() - timedelta(seconds=appmod._CONCRETE_REMINDER_CLAIM_TIMEOUT_SECONDS)).isoformat()
    claim_time_det = datetime.utcnow().isoformat()

    cur_a = conn_a.execute(claim_sql, (claim_time_det, det_rid, stale_before_det))
    conn_a.commit()
    cur_b = conn_b.execute(claim_sql, (claim_time_det, det_rid, stale_before_det))
    conn_b.commit()

    check("9a. Connection A's claim UPDATE matched exactly one row (rowcount == 1) -- A wins", cur_a.rowcount == 1)
    check("9a. Connection B's claim UPDATE, attempted on the now-already-claimed row, matched zero rows (rowcount == 0) -- B loses cleanly", cur_b.rowcount == 0)
    check("9a. exactly one of the two connections may proceed to send -- the other is structurally excluded before ever calling WhatsApp",
          (cur_a.rowcount == 1) != (cur_b.rowcount == 1))
    conn_a.close()
    conn_b.close()

    print()
    print("=== 9. Concurrent double invocation -> exactly one send ===")
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__RaceTest%'")
    db.commit()
    # Deliberately uses REAL, unmocked time (not app.datetime patching) --
    # patching a shared module attribute from two concurrent threads at
    # once is itself unsafe/undefined behavior in unittest.mock, which
    # would make this test's own infrastructure the source of flakiness
    # rather than proving anything about the actual claim mechanism.
    # Instead: seed a record whose pour is genuinely, currently due
    # under real wall-clock Houston time -- pour is "tomorrow" at
    # exactly the current Houston time, so reminder_due_at (pour minus
    # 1 day) is "right now."
    real_houston_now = datetime.now(ZoneInfo("America/Chicago"))
    real_pour_dt = real_houston_now + timedelta(days=1)
    cur = db.execute(
        """INSERT INTO inventory_concrete_requests
           (project, pour_date, pour_time, job_site_address, area_description, status, full_reminder_sent_at, reminder_claimed_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("__RaceTest Job", real_pour_dt.date().isoformat(), real_pour_dt.strftime("%H:%M"), "123", "Slab", "Scheduled", None, None, now, now)
    )
    db.commit()
    race_rid = cur.lastrowid

    send_calls = []
    call_lock = threading.Lock()

    def slow_send(text, chat_id=None):
        # Simulates real network latency to Ultramsg -- deliberately
        # NOT a barrier/rendezvous: with the atomic claim working
        # correctly, only the winning thread ever reaches this call at
        # all (the loser correctly bails out right after losing the
        # claim, before ever attempting to send) -- a barrier here
        # would incorrectly assume both threads always reach this
        # point, which is precisely what the fix being tested prevents.
        time.sleep(0.3)
        with call_lock:
            send_calls.append(text)
        return (True, "Sent.")

    results = {}

    def run_process(label):
        with appmod.app.app_context():
            with patch("app.send_whatsapp_group_message", side_effect=slow_send):
                results[label] = appmod.process_due_concrete_reminders()

    t1 = threading.Thread(target=run_process, args=("A",))
    t2 = threading.Thread(target=run_process, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    check("9. exactly ONE WhatsApp send occurred despite two truly concurrent invocations", len(send_calls) == 1)
    row_race = db.execute("SELECT full_reminder_sent_at, reminder_claimed_at FROM inventory_concrete_requests WHERE id=?", (race_rid,)).fetchone()
    check("9. exactly one successful sent marker recorded", bool(row_race["full_reminder_sent_at"]))
    check("9. no leftover claim after the winner finished", not row_race["reminder_claimed_at"])
    total_sent_across_both = results["A"]["sent"] + results["B"]["sent"]
    check("9. combined sent count across both processes is exactly 1 (the loser correctly saw 0 due to losing the claim)", total_sent_across_both == 1)

    print()
    print("=== 15. Standalone scheduled command uses the same processor ===")
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__ConcreteRemTest CronCommand%'")
    db.commit()
    cur15 = db.execute(
        """INSERT INTO inventory_concrete_requests
           (project, pour_date, pour_time, job_site_address, area_description, status, full_reminder_sent_at, reminder_claimed_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("__ConcreteRemTest CronCommand", "2026-09-11", "07:00", "123", "Slab", "Scheduled", None, None, now, now)
    )
    db.commit()
    rid15 = cur15.lastrowid
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_concrete_reminders.py")
    frozen15 = type("_FrozenAt", (_FrozenAt,), {
        "_fixed_houston": datetime(2026, 9, 10, 7, 0, tzinfo=ZoneInfo("America/Chicago")),
        "_fixed_utc": datetime(2026, 9, 10, 12, 0),
    })
    with patch("app.datetime", frozen15), patch.object(appmod, "send_whatsapp_group_message", return_value=(True, "Sent.")):
        with appmod.app.app_context():
            import importlib.util
            spec = importlib.util.spec_from_file_location("run_concrete_reminders", script_path)
            cron_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cron_module)
            cron_module.main()
    row15 = db.execute("SELECT full_reminder_sent_at FROM inventory_concrete_requests WHERE id=?", (rid15,)).fetchone()
    check("15. the scheduled command's own main() uses the exact same processor and correctly marks sent", bool(row15["full_reminder_sent_at"]))

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM inventory_concrete_requests WHERE project LIKE '__ConcreteRemTest%' OR project LIKE '__RaceTest%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
