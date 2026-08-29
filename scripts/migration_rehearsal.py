"""
Migration rehearsal -- deliberately SEPARATE from the six automated
test suites (CTO finding: normal suites must never be pointed at an
existing/company database, even for migration testing).

This script does the opposite of isolate_test_database(): instead of
always creating an empty fresh DB, it takes an EXISTING source database,
copies it to a brand-new disposable temp path, and only ever touches
the copy. The source is verified byte-for-byte unchanged afterward.

Usage:
    python3 scripts/migration_rehearsal.py /path/to/source/buildiq.db

If no path is given, rehearses against a freshly-created empty DB
instead (still copies it first, same code path, just nothing
pre-existing to protect).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
import shutil
import sqlite3
import tempfile


def hash_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    source_path = sys.argv[1] if len(sys.argv) > 1 else None

    rehearsal_dir = tempfile.mkdtemp(prefix="buildiq_migration_rehearsal_")
    copy_path = os.path.join(rehearsal_dir, "buildiq.db")

    if source_path:
        if not os.path.exists(source_path):
            sys.exit(f"Source database not found: {source_path}")
        source_hash_before = hash_file(source_path)
        print(f"Source: {source_path}")
        print(f"Source SHA-256 before: {source_hash_before}")
        shutil.copy2(source_path, copy_path)
        print(f"Copied to disposable rehearsal path: {copy_path}")
    else:
        print("No source path given -- rehearsing against a fresh empty DB.")
        source_hash_before = None

    # Point the app at the COPY only, then import it -- this triggers
    # init_db(), which is exactly what a real deploy's startup migration
    # does (creates missing tables, seeds missing permission/role rows,
    # etc., all idempotently).
    os.environ["DATA_DIR"] = rehearsal_dir
    import app as appmod  # noqa: F401  (import triggers init_db() against the copy)

    db = sqlite3.connect(copy_path)
    perm_count = db.execute("SELECT COUNT(*) FROM permissions").fetchone()[0]
    role_count = db.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    print(f"Rehearsal DB after migration: {perm_count} permissions, {role_count} roles")
    db.close()

    if source_path:
        source_hash_after = hash_file(source_path)
        print(f"Source SHA-256 after:  {source_hash_after}")
        if source_hash_before == source_hash_after:
            print("CONFIRMED: source database is byte-for-byte unchanged.")
        else:
            print("MISMATCH: source database changed -- this should never happen.")
            sys.exit(1)

    print(f"Rehearsal artifacts left at: {rehearsal_dir} (not cleaned up automatically -- inspect or delete manually)")


if __name__ == "__main__":
    main()
