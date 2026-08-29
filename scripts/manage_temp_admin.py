"""
Create or remove the TEMPORARY test-admin@darycet.com account.

This only touches the users table. It does not touch app.py or
templates/base.html -- when you're done testing, also remove the
TEMP_TEST_ADMIN_EMAIL block from app.py and the two hardcoded
"test-admin@darycet.com" entries in templates/base.html.

Usage (run from the project root, same folder as app.py):

    python3 scripts/manage_temp_admin.py --create
    python3 scripts/manage_temp_admin.py --remove
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

TEST_EMAIL = "test-admin@darycet.com"
TEST_NAME = "Test Admin"
TEST_PASSWORD = "Darycet-Temp-2026!"  # change if you want, then re-run --create

DB_DIR = os.environ.get("DATA_DIR", ".")
DB_PATH = os.path.join(DB_DIR, "buildiq.db")


def create():
    db = sqlite3.connect(DB_PATH)
    existing = db.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()
    if existing:
        print(f"{TEST_EMAIL} already exists (id {existing[0]}). Nothing to do.")
        return
    db.execute(
        "INSERT INTO users (name, email, password_hash, created_at, department) VALUES (?, ?, ?, ?, ?)",
        (TEST_NAME, TEST_EMAIL, generate_password_hash(TEST_PASSWORD),
         datetime.now(timezone.utc).isoformat(), "Testing"),
    )
    db.commit()
    print(f"Created {TEST_EMAIL}")
    print(f"Password: {TEST_PASSWORD}")


def remove():
    db = sqlite3.connect(DB_PATH)
    cur = db.execute("DELETE FROM users WHERE email = ?", (TEST_EMAIL,))
    db.commit()
    if cur.rowcount:
        print(f"Removed {TEST_EMAIL}.")
    else:
        print(f"{TEST_EMAIL} was not found -- nothing removed.")
    print("Don't forget to also remove the TEMP_TEST_ADMIN_EMAIL block in app.py")
    print("and the two test-admin@darycet.com entries in templates/base.html.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true")
    group.add_argument("--remove", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        sys.exit(f"No database found at {DB_PATH} -- run this from the project root.")

    if args.create:
        create()
    else:
        remove()
