"""
Runs the Concrete day-before WhatsApp reminder processor once, then exits.

This is the command a Railway Cron service should invoke on a schedule --
it does NOT start the Flask web server, does NOT require any browser/HTTP
request, and does NOT introduce any scheduling library (Celery,
APScheduler, Redis, a background thread, etc.) -- Railway provides the
schedule externally; this script only does the work once per invocation.

It calls the EXACT SAME processor (process_due_concrete_reminders) that
the existing GET /inventory/concrete page-load path also calls -- there
is deliberately only one implementation of the reminder logic. The
processor itself determines eligibility using America/Chicago (Houston)
calendar dates via zoneinfo, and only marks a record's
full_reminder_sent_at when send_whatsapp_group_message actually reports
success -- both unchanged by running from this script vs. from the page
load.

Usage (run from the project root, same folder as app.py):

    python3 scripts/run_concrete_reminders.py

Exit code 0 on a clean run (regardless of whether anything was due),
non-zero only if the processor itself raised an exception it could not
safely contain (which should not happen in normal operation -- see
process_due_concrete_reminders' own per-record try/except).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod


def main():
    # Establishes the same application/database context
    # process_due_concrete_reminders() (and everything it calls --
    # get_db(), send_whatsapp_group_message(), etc.) already relies on
    # inside a real request -- WITHOUT starting the web server or
    # binding a port. This is the whole point: a short-lived process
    # that does the work and exits.
    with appmod.app.app_context():
        result = appmod.process_due_concrete_reminders()
        print(f"[concrete-reminder-cron] due={result['due']} sent={result['sent']} failed={result['failed']}")


if __name__ == "__main__":
    main()
