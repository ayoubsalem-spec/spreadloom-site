"""Short-lived Railway Cron caller for the LIVE BuildIQ Concrete reminder endpoint.

This process intentionally has no database access.  It calls the existing
BuildIQ web service, where process_due_concrete_reminders() runs against the
web service's mounted /data SQLite database, then exits.

Required environment variables:
  BUILDIQ_CRON_URL       e.g. https://<live-domain>/internal/cron/concrete-reminders
  CONCRETE_CRON_SECRET  same secret configured on the LIVE web service
"""
import os
import sys
import requests


def main():
    url = os.environ.get("BUILDIQ_CRON_URL", "").strip()
    secret = os.environ.get("CONCRETE_CRON_SECRET", "").strip()
    if not url or not secret:
        print("[concrete-reminder-cron] configuration missing")
        return 2

    try:
        response = requests.post(
            url,
            headers={"X-BuildIQ-Cron-Secret": secret},
            timeout=30,
        )
    except requests.RequestException:
        print("[concrete-reminder-cron] request failed")
        return 1

    if response.status_code != 200:
        print(f"[concrete-reminder-cron] HTTP {response.status_code}")
        return 1

    try:
        data = response.json()
        print(
            f"[concrete-reminder-cron] due={int(data.get('due', 0))} "
            f"sent={int(data.get('sent', 0))} failed={int(data.get('failed', 0))}"
        )
    except (ValueError, TypeError):
        print("[concrete-reminder-cron] invalid response")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
