"""
Fail-closed resolver + Atlas tool-registration hardening tests.

Covers the two CTO-audit findings:
1. user_has_permission(user, key) must deny (not grant) on a missing/
   empty/unknown key.
2. register_tool() must reject a malformed registration (missing/empty
   permission, atlas_permission, or invalid kind) immediately, rather
   than letting it into ATLAS_TOOLS as a dead-on-arrival or (pre-fix)
   silently-fail-open tool.

Usage (from the project root):
    APP_ENV=development python3 scripts/resolver_hardening_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from datetime import datetime

import _test_db_setup
_test_db_setup.isolate_test_database()  # MUST happen before `import app` -- app.py reads DATA_DIR at import time

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
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash

    print("Setting up fixtures...")
    email = "__resolver_test@test.local"
    db.execute("DELETE FROM users WHERE email=?", (email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
               ("__resolver_test", email, generate_password_hash("x"), now))
    db.commit()
    uid = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    admin_role_id = db.execute("SELECT id FROM roles WHERE name='Administrator'").fetchone()[0]
    db.execute("INSERT INTO user_roles (user_id, role_id) VALUES (?,?)", (uid, admin_role_id))
    db.commit()

    print()
    print("=== 1. Fail-closed resolver: missing/empty/unknown key never authorizes ===")
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        user = appmod.User(row)
        login_user(user)

        # Sanity: this user IS a real, fully-permissioned Administrator --
        # so if any of the checks below return True, it's not because the
        # user genuinely lacks access, it's the bug.
        check("sanity: this Administrator DOES have a real permission (module:team_admin:view)",
              appmod.user_has_permission(user, "module:team_admin:view") is True)

        check("user_has_permission(admin, None) == False", appmod.user_has_permission(user, None) is False)
        check("user_has_permission(admin, '') == False", appmod.user_has_permission(user, "") is False)
        check("user_has_permission(admin, 'not:a:real:key') == False",
              appmod.user_has_permission(user, "not:a:real:key") is False)

    print()
    print("=== Unauthenticated user: every check is False, including edge-case keys ===")
    class FakeAnonymous:
        is_authenticated = False
    anon = FakeAnonymous()
    check("user_has_permission(anon, 'module:team_admin:view') == False", appmod.user_has_permission(anon, "module:team_admin:view") is False)
    check("user_has_permission(anon, None) == False", appmod.user_has_permission(anon, None) is False)
    check("user_has_permission(None, 'module:team_admin:view') == False", appmod.user_has_permission(None, "module:team_admin:view") is False)

    print()
    print("=== 2. register_tool() rejects malformed registrations immediately ===")
    def dummy_handler(user, **kwargs):
        return {"ok": True}

    before_tool_count = len(appmod.ATLAS_TOOLS)

    malformed_cases = [
        ("missing permission", dict(name="__bad_tool_1", description="x", parameters={}, permission="", atlas_permission="atlas:view_business_data", kind="read", handler=dummy_handler)),
        ("missing atlas_permission", dict(name="__bad_tool_2", description="x", parameters={}, permission="module:atlas:view", atlas_permission="", kind="read", handler=dummy_handler)),
        ("permission is None", dict(name="__bad_tool_3", description="x", parameters={}, permission=None, atlas_permission="atlas:view_business_data", kind="read", handler=dummy_handler)),
        ("invalid kind", dict(name="__bad_tool_4", description="x", parameters={}, permission="module:atlas:view", atlas_permission="atlas:view_business_data", kind="delete_everything", handler=dummy_handler)),
        ("non-callable handler", dict(name="__bad_tool_5", description="x", parameters={}, permission="module:atlas:view", atlas_permission="atlas:view_business_data", kind="read", handler="not a function")),
        ("empty name", dict(name="", description="x", parameters={}, permission="module:atlas:view", atlas_permission="atlas:view_business_data", kind="read", handler=dummy_handler)),
    ]
    for label, kwargs in malformed_cases:
        raised = False
        try:
            appmod.register_tool(**kwargs)
        except ValueError:
            raised = True
        check(f"register_tool rejects: {label}", raised)

    after_tool_count = len(appmod.ATLAS_TOOLS)
    check("no malformed tool was actually added to ATLAS_TOOLS", before_tool_count == after_tool_count)
    for label, kwargs in malformed_cases:
        check(f"'{kwargs.get('name')}' is NOT in ATLAS_TOOLS", kwargs.get("name") not in appmod.ATLAS_TOOLS)

    print()
    print("=== 2b. A validly-registered tool remains executable (guard isn't overly strict) ===")
    appmod.register_tool(
        name="__good_test_tool", description="test", parameters={},
        permission="module:atlas:view", atlas_permission="atlas:view_business_data",
        kind="read", handler=lambda user: {"ok": True},
    )
    check("a valid tool registers successfully", "__good_test_tool" in appmod.ATLAS_TOOLS)
    with appmod.app.test_request_context('/'):
        from flask_login import login_user
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        user = appmod.User(row)
        login_user(user)
        r = appmod.execute_tool("__good_test_tool", {}, user)
        check("the valid tool actually executes for an authorized user", r.success is True)
    del appmod.ATLAS_TOOLS["__good_test_tool"]

    # Exact fixture cleanup must run BEFORE the orphan assertion --
    # otherwise a bug in this very cleanup step could create an orphan
    # that the assertion, running earlier, would never see (CTO finding).
    print()
    print("Cleaning up...")
    db.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()

    # ORDERING MATTERS (CTO finding): the orphan assertion runs against
    # the state left by the exact fixture cleanup directly above --
    # BEFORE any broad safety-net cleanup can hide a defect, and AFTER
    # every normal destructive cleanup step this suite performs. Only
    # after the assertion has been recorded does the narrowly-scoped
    # emergency net run.
    orphans = hygiene.assert_no_orphan_privilege_rows(db)
    for o in orphans:
        FAIL.append(f"DB hygiene: {o}")
        print(f"FAIL  DB hygiene: {o}")
    if not orphans:
        check("no orphan user_roles/user_permission_overrides/role_permissions rows remain", True)
    hygiene.emergency_cleanup_orphans(db)

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
