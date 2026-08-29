"""
Test database isolation guard.

MUST be imported and isolate_test_database() called BEFORE `import app`
in every test script -- app.py reads the DATA_DIR environment variable
at MODULE IMPORT TIME to compute DB_PATH, so this has to run first or
it's too late; the app will already be pointed at whatever DATA_DIR
happened to be set (or the real repo directory, by default, if unset).

This exists because the six test suites contain real destructive SQL
(DELETE statements, some previously with predicates broad enough to
plausibly match legitimate business data). Before this module, every
suite connected directly to the application's normal buildiq.db.
Structurally preventing that -- rather than trusting every cleanup
predicate to be narrow enough forever -- is the actual fix.
"""
import os
import tempfile


def isolate_test_database():
    """Points the application at a fresh, disposable SQLite database in
    the system temp directory. Returns the directory path. Refuses to
    proceed (raises RuntimeError) if the resolved path doesn't look like
    a real disposable test location -- this is the hard safety guard:
    a test script cannot accidentally end up pointed at the ordinary
    application database, whether by a missing environment variable, a
    typo, or a copy-pasted DATA_DIR from somewhere else.

    UNCONDITIONAL (CTO finding): every call creates its own brand-new
    temp directory via tempfile.mkdtemp() and overwrites DATA_DIR with
    it -- it does NOT reuse a pre-existing DATA_DIR, even one that looks
    plausible (e.g. already under /tmp with "test" in its name). A
    normal automated suite must be structurally incapable of being
    pointed at a caller-supplied database, including an existing test
    clone someone left lying around at a path like
    /tmp/company_test_clone -- "contains 'test'" is not a strong enough
    signal to trust for that. Migration-rehearsal-against-a-real-copy is
    a deliberately separate, explicit workflow (see
    scripts/migration_rehearsal.py) that never goes through this
    function."""
    data_dir = tempfile.mkdtemp(prefix="buildiq_test_")
    os.environ["DATA_DIR"] = data_dir
    assert_is_disposable_test_path(data_dir)
    return data_dir


def assert_is_disposable_test_path(data_dir):
    """The hard guard itself, split out so other destructive helpers
    (e.g. _test_hygiene's cleanup functions) can call it independently
    right before doing anything destructive -- defense in depth, not
    just a one-time check at startup."""
    real = os.path.realpath(data_dir)
    tmp_root = os.path.realpath(tempfile.gettempdir())
    inside_tmp = real == tmp_root or real.startswith(tmp_root + os.sep)
    has_test_marker = "test" in os.path.basename(real).lower()
    if not (inside_tmp and has_test_marker):
        raise RuntimeError(
            f"Refusing to operate against DATA_DIR={data_dir!r}: this does "
            "not look like a disposable test directory (it must live under "
            f"the system temp directory {tmp_root!r} and its own directory "
            "name must contain 'test'). This check exists specifically so "
            "a test script can never mutate the real application database "
            "-- if you're seeing this, DATA_DIR was set to something other "
            "than a path created by isolate_test_database()."
        )
