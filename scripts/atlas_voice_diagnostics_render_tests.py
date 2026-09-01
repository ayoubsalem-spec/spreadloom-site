"""
Atlas Voice Diagnostics -- template render regression.

Added after a real Railway TEST deployment failure:
    jinja2.exceptions.TemplateSyntaxError: Encountered unknown tag
    'endblock'. ... The innermost block that needs to be closed is 'if'.

Root cause: two JS comments inside the <script> block of
templates/assistant.html contained the literal text
"{% if atlas_voice_diagnostics_enabled %}" as plain prose (explaining
where the real markup block was). Jinja does not know these are inside
a `//` JS comment -- it parses `{% %}` anywhere in the template source
-- so each one opened an extra, unmatched `if` block, which is exactly
what broke `{% endblock %}` at the bottom of the file.

This test exists so that class of bug can never silently regress again:
it renders the REAL template through Flask's REAL Jinja environment
(not node --check, not a grep for balanced tags -- an actual
app.test_client().get("/assistant")), with the real ATLAS_VOICE_DIAGNOSTICS
env flag set both ways, and asserts:
    - HTTP 200 in both cases (a TemplateSyntaxError would produce a 500)
    - the diagnostics panel markup is present when the flag is enabled
    - the diagnostics panel markup is ABSENT (not just hidden) when the
      flag is disabled -- i.e. atlas_voice_diagnostics_enabled genuinely
      gates the block, not just happens to also fix the syntax error

Real app, real routes, real login flow, valid CSRF -- same pattern as
the other scripts in this directory. No outer app_context held across
test-client calls (see security_correction_tests.py's login() docstring
for why that matters).

Usage (from the project root):
    APP_ENV=development python3 scripts/atlas_voice_diagnostics_render_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
import importlib
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


def get_csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    token = get_csrf(client, "/login")
    resp = client.post("/login", data={"email": email, "password": password, "csrf_token": token}, follow_redirects=True)
    if resp.status_code != 200 or resp.request.path != "/" or "Invalid email or password" in resp.get_data(as_text=True):
        raise RuntimeError(f"login() failed for {email}: status={resp.status_code}, path={resp.request.path}")
    return token


def make_atlas_user(db, email, name, now, pw_hash):
    db.execute("DELETE FROM users WHERE email=?", (email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", (name, email, pw_hash, now))
    db.commit()
    uid = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    pid = db.execute("SELECT id FROM permissions WHERE key=?", ("module:atlas:view",)).fetchone()[0]
    db.execute(
        "INSERT INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id, permission_id) DO UPDATE SET state=excluded.state",
        (uid, pid, "grant", "test_setup", now)
    )
    db.commit()
    return uid


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    email = "__avd_render@test.local"
    make_atlas_user(db, email, "__avd_render", now, pw_hash)

    DIAG_MARKER = 'id="voiceDiagPanel"'

    # ------------------------------------------------------------------
    # 1. Flag DISABLED (also confirms the actual default: env var unset)
    # ------------------------------------------------------------------
    print("=== 1. ATLAS_VOICE_DIAGNOSTICS unset (default) -- /assistant renders, panel absent ===")
    os.environ.pop("ATLAS_VOICE_DIAGNOSTICS", None)
    appmod.ATLAS_VOICE_DIAGNOSTICS = False  # mirror real module-level read, same as a fresh process boot would compute

    with appmod.app.test_client() as client:
        login(client, email, pw)
        resp = client.get("/assistant")
        check("GET /assistant returns HTTP 200 (flag disabled)", resp.status_code == 200)
        html = resp.get_data(as_text=True)
        check("no unrendered '{% if' / '{% endif' Jinja source leaks into the response", "{%" not in html and "{ %" not in html)
        check("diagnostics panel markup is ABSENT from the DOM when disabled", DIAG_MARKER not in html)
        check("real Atlas voice UI still renders normally (Start Voice button present)", 'id="voice-start-btn"' in html)

    # ------------------------------------------------------------------
    # 2. Flag ENABLED
    # ------------------------------------------------------------------
    print("\n=== 2. ATLAS_VOICE_DIAGNOSTICS=true -- /assistant renders, panel present ===")
    os.environ["ATLAS_VOICE_DIAGNOSTICS"] = "true"
    appmod.ATLAS_VOICE_DIAGNOSTICS = True  # mirror real module-level read for an explicitly-enabled process

    with appmod.app.test_client() as client:
        login(client, email, pw)
        resp = client.get("/assistant")
        check("GET /assistant returns HTTP 200 (flag enabled)", resp.status_code == 200)
        html = resp.get_data(as_text=True)
        check("no unrendered '{% if' / '{% endif' Jinja source leaks into the response", "{%" not in html and "{ %" not in html)
        check("diagnostics panel markup IS present in the DOM when enabled", DIAG_MARKER in html)
        check("Copy Diagnostics button is present", "Copy Diagnostics" in html)
        check("real Atlas voice UI still renders normally (Start Voice button present)", 'id="voice-start-btn"' in html)
        # Sensitive-data guardrail: the panel must never leak API keys/
        # tokens/secrets into the rendered page.
        check("no raw API key env var names leaked into the response", "ANTHROPIC_API_KEY=" not in html and "ELEVENLABS_API_KEY=" not in html)

    # Reset to the real default for anything that runs after this script
    # in the same process (defensive; each script here is normally run
    # standalone, but this costs nothing and avoids surprising a future
    # combined test runner).
    os.environ.pop("ATLAS_VOICE_DIAGNOSTICS", None)
    appmod.ATLAS_VOICE_DIAGNOSTICS = False

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
