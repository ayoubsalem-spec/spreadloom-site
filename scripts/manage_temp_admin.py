"""
Create or remove the TEMPORARY test-admin@darycet.com account.

This only touches the users table. It does not touch app.py or
templates/base.html -- when you're done testing, also remove the
TEMP_TEST_ADMIN_EMAIL block from app.py and the two hardcoded
"test-admin@darycet.com" entries in templates/base.html.

No password is hardcoded here. Provide one via the TEMP_ADMIN_PASSWORD
environment variable, or you'll be prompted for it securely (input is
not echoed to the terminal, and the value is never printed or logged).

Usage (run from the project root, same folder as app.py):

    python3 scripts/manage_temp_admin.py --create
    python3 scripts/manage_temp_admin.py --remove

    # non-interactive (e.g. CI):
    TEMP_ADMIN_PASSWORD='...' python3 scripts/manage_temp_admin.py --create
"""
import argparse
import getpass
import os
import sqlite3
import sys
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

TEST_EMAIL = "test-admin@darycet.com"
TEST_NAME = "Test Admin"

DB_DIR = os.environ.get("DATA_DIR", ".")
DB_PATH = os.path.join(DB_DIR, "buildiq.db")


def _get_password():
    """Never hardcoded, never printed, never logged. Env var for
    non-interactive/CI use; otherwise a hidden interactive prompt."""
    pw = os.environ.get("TEMP_ADMIN_PASSWORD", "").strip()
    if pw:
        return pw
    pw = getpass.getpass(f"Set a password for {TEST_EMAIL}: ").strip()
    if not pw:
        sys.exit("No password provided -- aborting.")
    confirm = getpass.getpass("Confirm password: ").strip()
    if pw != confirm:
        sys.exit("Passwords did not match -- aborting.")
    return pw


def create():
    db = sqlite3.connect(DB_PATH)
    existing = db.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()
    if existing:
        print(f"{TEST_EMAIL} already exists (id {existing[0]}). Nothing to do.")
        return
    password = _get_password()
    db.execute(
        "INSERT INTO users (name, email, password_hash, created_at, department) VALUES (?, ?, ?, ?, ?)",
        (TEST_NAME, TEST_EMAIL, generate_password_hash(password),
         datetime.now(timezone.utc).isoformat(), "Testing"),
    )
    db.commit()
    print(f"Created {TEST_EMAIL}. Password was not printed -- it's whatever you just entered/provided.")


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
