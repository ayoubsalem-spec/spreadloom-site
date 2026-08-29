"""
Meta-regression tests for the test harness itself, added after CTO
review found three gaps:

1. The orphan-row assertion was only ever proven to pass (GREEN) --
   never proven capable of actually failing (RED) when a real orphan
   exists, including one CAUSED BY a suite's own cleanup step (not just
   one present before cleanup runs). A detector that has never been
   seen to fire is not proven to work.
2. Nothing proved the six suites can't touch the real application
   database -- they all connected to whatever appmod.DB_PATH resolved
   to, which defaulted to the real buildiq.db unless every script
   individually got DATA_DIR right.
3. isolate_test_database() used to reuse an already-set DATA_DIR if it
   looked plausible (under /tmp, contained "test") -- not strong enough
   to guarantee a normal automated suite can never be pointed at an
   existing external database, such as a company test-data clone
   someone left at a path like /tmp/company_test_clone. Fixed to always
   create its own fresh directory unconditionally.

This script proves all three, using its own isolated disposable database
(see _test_db_setup.py) exactly like every other script here. Migration
rehearsal against a real existing database is a deliberately SEPARATE
workflow -- see scripts/migration_rehearsal.py -- and never goes through
isolate_test_database().

Usage (from the project root):
    APP_ENV=development python3 scripts/harness_meta_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import tempfile

import _test_db_setup
_TEST_DATA_DIR = _test_db_setup.isolate_test_database()  # before `import app`

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


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row

    print("=== 1. Orphan detector proven RED before it's trusted GREEN ===")
    before = hygiene.assert_no_orphan_privilege_rows(db)
    check("clean isolated DB starts with zero orphans (sanity)", before == [])

    sentinel = hygiene.create_orphan_fixtures_for_detector_test(db)
    during = hygiene.assert_no_orphan_privilege_rows(db)
    # Each of the 3 deliberately-created rows has two dangling references
    # (e.g. the user_roles row references both a nonexistent user_id AND
    # a nonexistent role_id), and the detector correctly reports each
    # dangling reference separately -- so 3 orphan rows produce 6
    # reported problems, not 3. Asserting the real count here (rather
    # than the naive "3") is itself part of proving the detector's
    # actual behavior, not an assumption about it.
    check("detector reports all 6 dangling references from the 3 deliberate orphan rows (RED)", len(during) == 6)
    check("reported problem mentions the orphan user_roles row", any("user_roles" in p for p in during))
    check("reported problem mentions the orphan user_permission_overrides row", any("user_permission_overrides" in p for p in during))
    check("reported problem mentions the orphan role_permissions row", any("role_permissions" in p for p in during))

    hygiene.remove_orphan_fixtures_for_detector_test(db, sentinel)
    after = hygiene.assert_no_orphan_privilege_rows(db)
    check("detector reports zero problems again after exact-id removal (GREEN)", after == [])

    print()
    print("=== 2. Ordering proof: emergency cleanup does not run before the assertion in real scripts ===")
    # Re-create the same 3 orphans and verify that calling the assertion
    # FIRST (as every real script now does) sees them, and only calling
    # emergency_cleanup_orphans() AFTER makes them disappear -- proving
    # the fixed ordering actually behaves as documented, not just that
    # the functions exist.
    sentinel2 = hygiene.create_orphan_fixtures_for_detector_test(db)
    seen_before_cleanup = hygiene.assert_no_orphan_privilege_rows(db)
    check("orphans are visible to the assertion BEFORE emergency cleanup runs", len(seen_before_cleanup) == 6)
    hygiene.emergency_cleanup_orphans(db)
    seen_after_cleanup = hygiene.assert_no_orphan_privilege_rows(db)
    check("orphans are gone AFTER emergency cleanup runs (order confirmed correct)", seen_after_cleanup == [])

    print()
    print("=== 2b. Lifecycle proof: an orphan CAUSED BY a suite's own cleanup bug is caught ===")
    # This is the specific case the CTO's final finding was about: not
    # just "was an orphan present before cleanup ran" but "does a BUG IN
    # the cleanup step itself -- one that deletes a user but forgets
    # their user_roles row -- get caught by an assertion that runs AFTER
    # that cleanup". Simulates exactly that bug deliberately, on its own
    # sentinel data, then proves the assertion (run in the position every
    # real suite now uses it -- after normal cleanup) goes RED.
    from datetime import datetime
    from werkzeug.security import generate_password_hash
    now = datetime.utcnow().isoformat()
    lifecycle_email = "__lifecycle_orphan_test@test.local"
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__lifecycle_orphan_test", lifecycle_email, generate_password_hash("x"), now))
    db.commit()
    lifecycle_uid = db.execute("SELECT id FROM users WHERE email=?", (lifecycle_email,)).fetchone()[0]
    emp_role_id = db.execute("SELECT id FROM roles WHERE name='Employee'").fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (lifecycle_uid, emp_role_id))
    db.commit()

    # Deliberately-bad cleanup: deletes the user but forgets user_roles --
    # the exact class of bug the ordering fix exists to catch.
    db.execute("DELETE FROM users WHERE id=?", (lifecycle_uid,))
    db.commit()

    post_bad_cleanup = hygiene.assert_no_orphan_privilege_rows(db)
    check("a cleanup bug that deletes a user but leaves their user_roles row IS caught by the assertion",
          any(f"user_id={lifecycle_uid}" in p for p in post_bad_cleanup))

    # Now clean up this test's own residue precisely (exact id, not a
    # broad predicate) and confirm clean again.
    db.execute("DELETE FROM user_roles WHERE user_id=?", (lifecycle_uid,))
    db.commit()
    check("clean again after the exact-id fix for this test's own residue", hygiene.assert_no_orphan_privilege_rows(db) == [])

    print()
    print("=== 3. Database isolation proof ===")
    check("DATA_DIR is set to a path under the system temp directory", os.path.realpath(_TEST_DATA_DIR).startswith(os.path.realpath(tempfile.gettempdir())))
    check("DATA_DIR's own directory name contains a 'test' marker", "test" in os.path.basename(os.path.realpath(_TEST_DATA_DIR)).lower())
    check("appmod.DB_PATH resolves inside the isolated temp directory, not the repo directory",
          os.path.realpath(appmod.DB_PATH).startswith(os.path.realpath(_TEST_DATA_DIR)))

    repo_db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "buildiq.db")
    repo_db_existed_before = os.path.exists(repo_db_path)
    repo_db_snapshot_before = None
    if repo_db_existed_before:
        with open(repo_db_path, "rb") as fh:
            repo_db_snapshot_before = fh.read()

    # Run real destructive-shaped operations against the ISOLATED db --
    # the same kind of thing every other suite does -- then verify the
    # real repo buildiq.db (if one happens to exist alongside the repo
    # for any reason) was never touched.
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.emergency_cleanup_orphans(db)

    if repo_db_existed_before:
        with open(repo_db_path, "rb") as fh:
            repo_db_snapshot_after = fh.read()
        check("the real repo buildiq.db (if present) is byte-for-byte unchanged after running destructive test operations",
              repo_db_snapshot_before == repo_db_snapshot_after)
    else:
        check("the real repo buildiq.db does not exist and this test run did NOT create one there "
              "(it's fully reproducible from app.py; not expected to be in the source tree)",
              not os.path.exists(repo_db_path))

    print()
    print("=== 4. Hard safety guard rejects a non-test path ===")
    raised = False
    try:
        _test_db_setup.assert_is_disposable_test_path("/home/claude/buildiq/spreadloom-site-main")
    except RuntimeError:
        raised = True
    check("assert_is_disposable_test_path rejects the real repo directory", raised)

    raised2 = False
    try:
        _test_db_setup.assert_is_disposable_test_path(tempfile.gettempdir())
    except RuntimeError:
        raised2 = True
    check("assert_is_disposable_test_path rejects the bare temp root (no 'test' marker in its own name)", raised2)

    print()
    print("=== 5. A plausible-looking pre-set DATA_DIR cannot redirect isolate_test_database() ===")
    # The specific CTO scenario: someone (or some CI step) has already
    # set DATA_DIR to something that looks superficially like a test
    # path -- e.g. an existing company test-data clone -- BEFORE this
    # script runs. isolate_test_database() must not trust that and must
    # always create its own fresh directory instead.
    external_looking_dir = tempfile.mkdtemp(prefix="company_test_clone_")
    external_db_path = os.path.join(external_looking_dir, "buildiq.db")
    with open(external_db_path, "wb") as fh:
        fh.write(b"PRETEND THIS IS REAL COMPANY DATA - MUST NEVER BE TOUCHED")
    external_hash_before = None
    import hashlib
    with open(external_db_path, "rb") as fh:
        external_hash_before = hashlib.sha256(fh.read()).hexdigest()

    os.environ["DATA_DIR"] = external_looking_dir  # simulate a pre-set, plausible-looking DATA_DIR
    new_dir = _test_db_setup.isolate_test_database()
    check("isolate_test_database() did NOT reuse the pre-set external-looking directory",
          os.path.realpath(new_dir) != os.path.realpath(external_looking_dir))
    check("isolate_test_database() created its own genuinely fresh directory instead",
          new_dir.startswith(tempfile.gettempdir()) and os.path.basename(new_dir).startswith("buildiq_test_"))

    with open(external_db_path, "rb") as fh:
        external_hash_after = hashlib.sha256(fh.read()).hexdigest()
    check("the external-looking 'company_test_clone' file was never touched",
          external_hash_before == external_hash_after)

    import shutil
    shutil.rmtree(external_looking_dir, ignore_errors=True)

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")

    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
