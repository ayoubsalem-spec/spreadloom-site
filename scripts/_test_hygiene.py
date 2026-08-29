"""
Shared test hygiene helpers, used by every script in this directory.

ORDERING IS THE WHOLE POINT (CTO finding): assert_no_orphan_privilege_rows()
must run and be recorded BEFORE any broad safety-net cleanup runs, or the
safety net silently erases the exact defect the assertion exists to catch.
The correct sequence every script follows:

    1. the script's own explicit, ID-based fixture cleanup
    2. assert_no_orphan_privilege_rows(db)  -- record FAIL for any hit
    3. only now: emergency_cleanup_orphans(db) as a narrowly-scoped net

Never call emergency_cleanup_orphans() before step 2, and never let step 2
run after it.

This module also requires database isolation: every public function here
calls scripts._test_db_setup.assert_is_disposable_test_path() against the
current DATA_DIR before doing anything destructive. A script that forgot
to isolate its database (see _test_db_setup.py) gets a hard RuntimeError
here instead of quietly deleting rows from whatever DB it's actually
pointed at.
"""
import os

import _test_db_setup


def _guard():
    _test_db_setup.assert_is_disposable_test_path(os.environ.get("DATA_DIR", ""))


def assert_no_orphan_privilege_rows(db):
    """Returns a list of human-readable problem descriptions (empty list
    == clean). Checks user_roles, user_permission_overrides, and
    role_permissions for rows referencing a user_id/role_id/permission_id
    that doesn't exist -- nothing at the DB level enforces this (the app
    never enables SQLite foreign keys; confirmed by grep, there is no
    PRAGMA foreign_keys anywhere in app.py), so this is the only thing
    that catches it. Read-only -- does not delete anything itself."""
    problems = []

    for row in db.execute(
        "SELECT ur.user_id, ur.role_id FROM user_roles ur "
        "LEFT JOIN users u ON u.id = ur.user_id WHERE u.id IS NULL"
    ).fetchall():
        problems.append(f"orphan user_roles row: user_id={row[0]} (no such user) role_id={row[1]}")

    for row in db.execute(
        "SELECT ur.user_id, ur.role_id FROM user_roles ur "
        "LEFT JOIN roles r ON r.id = ur.role_id WHERE r.id IS NULL"
    ).fetchall():
        problems.append(f"orphan user_roles row: role_id={row[1]} (no such role) user_id={row[0]}")

    for row in db.execute(
        "SELECT upo.user_id, upo.permission_id FROM user_permission_overrides upo "
        "LEFT JOIN users u ON u.id = upo.user_id WHERE u.id IS NULL"
    ).fetchall():
        problems.append(f"orphan user_permission_overrides row: user_id={row[0]} (no such user) permission_id={row[1]}")

    for row in db.execute(
        "SELECT upo.user_id, upo.permission_id FROM user_permission_overrides upo "
        "LEFT JOIN permissions p ON p.id = upo.permission_id WHERE p.id IS NULL"
    ).fetchall():
        problems.append(f"orphan user_permission_overrides row: permission_id={row[1]} (no such permission) user_id={row[0]}")

    for row in db.execute(
        "SELECT rp.role_id, rp.permission_id FROM role_permissions rp "
        "LEFT JOIN roles r ON r.id = rp.role_id WHERE r.id IS NULL"
    ).fetchall():
        problems.append(f"orphan role_permissions row: role_id={row[0]} (no such role)")

    for row in db.execute(
        "SELECT rp.role_id, rp.permission_id FROM role_permissions rp "
        "LEFT JOIN permissions p ON p.id = rp.permission_id WHERE p.id IS NULL"
    ).fetchall():
        problems.append(f"orphan role_permissions row: permission_id={row[1]} (no such permission)")

    return problems


def emergency_cleanup_orphans(db):
    """Narrowly scoped: deletes ONLY rows that assert_no_orphan_privilege_rows
    would flag (dangling user_roles/user_permission_overrides/role_permissions
    references). Must be called AFTER that assertion has already run and
    been recorded -- calling this first would hide exactly the defect the
    assertion exists to catch (this was the CTO's core finding: the old
    ordering let cleanup erase orphans before the assertion ever saw them).
    Guarded against running against a non-test database."""
    _guard()
    db.execute("DELETE FROM user_roles WHERE user_id NOT IN (SELECT id FROM users)")
    db.execute("DELETE FROM user_roles WHERE role_id NOT IN (SELECT id FROM roles)")
    db.execute("DELETE FROM user_permission_overrides WHERE user_id NOT IN (SELECT id FROM users)")
    db.execute("DELETE FROM user_permission_overrides WHERE permission_id NOT IN (SELECT id FROM permissions)")
    db.execute("DELETE FROM role_permissions WHERE role_id NOT IN (SELECT id FROM roles)")
    db.execute("DELETE FROM role_permissions WHERE permission_id NOT IN (SELECT id FROM permissions)")
    db.commit()


def cleanup_test_users_by_prefix(db):
    """Deletes users (and their user_roles/user_permission_overrides rows)
    whose email or name starts with the double-underscore test-fixture
    convention every script in this directory uses. This predicate is
    narrow and specific to a naming convention no real account would ever
    use -- unlike the broad business-value predicates (vendor_name IN
    ('Test Vendor', 'Vendor'), LIKE '%regression%', keyword IN ('hacked',
    'test')) that used to live here and were removed after CTO review:
    those could plausibly match legitimate company data and are gone.
    Callers should still prefer deleting fixtures by the exact row id
    they captured at creation time; this exists as a narrow net for the
    user/role/override rows specifically, since a test's own login()
    helper often creates users through paths that don't always thread an
    id back to a single cleanup list.
    Guarded against running against a non-test database."""
    _guard()
    test_user_ids = [r[0] for r in db.execute(
        "SELECT id FROM users WHERE email LIKE '\\_\\_%' ESCAPE '\\' OR name LIKE '\\_\\_%' ESCAPE '\\'"
    ).fetchall()]
    for uid in test_user_ids:
        db.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
        db.execute("DELETE FROM user_permission_overrides WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()


def create_orphan_fixtures_for_detector_test(db):
    """Deliberately creates one orphan row in each of the three tables
    the detector checks, so a test can prove assert_no_orphan_privilege_rows
    actually goes RED before it's ever trusted to report GREEN. Returns
    the exact identifiers used, so the caller can clean up precisely
    afterward (never relies on a broad predicate). A nonexistent user_id/
    role_id/permission_id is chosen far outside any real id range to
    avoid any chance of colliding with a real row.
    Guarded against running against a non-test database."""
    _guard()
    SENTINEL = 9_000_001
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?, ?)", (SENTINEL, SENTINEL))
    db.execute("INSERT INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) "
               "VALUES (?, ?, 'grant', 'orphan_detector_test', '2026-01-01T00:00:00')", (SENTINEL, SENTINEL))
    db.execute("INSERT INTO role_permissions (role_id, permission_id) VALUES (?, ?)", (SENTINEL, SENTINEL))
    db.commit()
    return SENTINEL


def remove_orphan_fixtures_for_detector_test(db, sentinel):
    """Exact-id cleanup for create_orphan_fixtures_for_detector_test --
    no broad predicate, deletes precisely the three rows that function
    created. Guarded against running against a non-test database."""
    _guard()
    db.execute("DELETE FROM user_roles WHERE user_id=? AND role_id=?", (sentinel, sentinel))
    db.execute("DELETE FROM user_permission_overrides WHERE user_id=? AND permission_id=?", (sentinel, sentinel))
    db.execute("DELETE FROM role_permissions WHERE role_id=? AND permission_id=?", (sentinel, sentinel))
    db.commit()
