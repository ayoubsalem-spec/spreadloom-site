"""
Purchase Request edit data-truth + mobile UX hotfix regression.

Usage:
    APP_ENV=development python3 scripts/purchase_request_edit_hotfix_tests.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import datetime

import _test_db_setup
_test_db_setup.isolate_test_database()

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
        raise RuntimeError(f"login() failed for {email}")
    return token


def real_qty_values(html):
    """Extracts qty[] input values from ACTUAL rendered <input> tags
    inside the real items list only -- deliberately excludes the JS
    clone-template string literal in the page's own <script> block
    (which also contains the text 'name="qty[]"' as source code, not a
    real DOM element), by scoping the search to the pr-items-list div's
    content and stopping before the <script> tag."""
    list_start = html.find('id="pr-items-list"')
    script_start = html.find("<script>", list_start)
    scoped = html[list_start:script_start] if list_start != -1 and script_start != -1 else html
    return re.findall(r'<input type="text" name="qty\[\]"(?: value="([^"]*)")?[^>]*>', scoped)


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    db.execute("DELETE FROM users WHERE email=?", ("__prhotfix_full@test.local",))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", ("__prhotfix_full", "__prhotfix_full@test.local", pw_hash, now))
    db.commit()
    uid = db.execute("SELECT id FROM users WHERE email=?", ("__prhotfix_full@test.local",)).fetchone()[0]
    for key in ["module:sitepulse:view", "action:sitepulse:manage"]:
        pid = db.execute("SELECT id FROM permissions WHERE key=?", (key,)).fetchone()[0]
        db.execute("INSERT OR REPLACE INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
                   (uid, pid, "grant", "test_setup", now))
    db.commit()

    db.execute("DELETE FROM users WHERE email=?", ("__prhotfix_noperm@test.local",))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", ("__prhotfix_noperm", "__prhotfix_noperm@test.local", pw_hash, now))
    db.commit()

    with appmod.app.test_client() as client:
        login(client, "__prhotfix_full@test.local", pw)

        print("=== Data-truth: existing item values populate correctly on Edit ===")
        cur = db.execute(
            "INSERT INTO inventory_purchase_requests (pr_number, request_date, job_name, status, created_at, updated_at) VALUES ('PR-HOTFIX-1', ?, 'Test Job', 'Submitted', ?, ?)",
            (now, now, now)
        )
        db.commit()
        pr_id = cur.lastrowid
        db.execute(
            "INSERT INTO inventory_purchase_request_items (purchase_request_id, item, description, supplier, qty, unit) VALUES (?, 'C sand', 'C sand', 'Arcosa', '11', 'Load')",
            (pr_id,)
        )
        db.commit()

        edit_html = client.get(f"/inventory/purchase/{pr_id}/edit").get_data(as_text=True)
        qty_values = real_qty_values(edit_html)
        check("exactly one real item row rendered (not a fixed stack of 9 blank rows)", len(qty_values) == 1)
        check("the real Quantity 11 is correctly present in the Edit form's value attribute", qty_values[0] == "11")
        check("Supplier 'Arcosa' correctly populated", 'value="Arcosa"' in edit_html)
        check("Description 'C sand' correctly populated", edit_html.count('value="C sand"') >= 1)
        check("Unit 'Load' correctly populated", 'value="Load"' in edit_html)

        print()
        print("=== Full edit journey: 11 -> 12, save, detail reflects new value ===")
        token = get_csrf(client, f"/inventory/purchase/{pr_id}/edit")
        resp_save = client.post(f"/inventory/purchase/{pr_id}/edit", data={
            "csrf_token": token, "request_date": now[:10], "job_name": "Test Job",
            "item[]": ["C sand"], "description[]": ["C sand"], "supplier[]": ["Arcosa"],
            "qty[]": ["12"], "unit[]": ["Load"],
        }, follow_redirects=True)
        check("save succeeds and redirects to the detail page", resp_save.status_code == 200)
        detail_html = resp_save.get_data(as_text=True)
        check("detail page now shows the NEW quantity (12)", ">12<" in detail_html)
        check("detail page no longer shows the OLD quantity as the item's qty", "<td>11</td>" not in detail_html)
        row = db.execute("SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id=?", (pr_id,)).fetchone()
        check("database itself was actually updated to 12", row["qty"] == "12")
        check("Supplier was NOT accidentally lost/changed by an unrelated qty edit", row["supplier"] == "Arcosa")
        check("Description was NOT accidentally lost", row["description"] == "C sand")

        print()
        print("=== Change one field at a time -- other fields never disappear ===")

        def do_edit(item, desc, sup, qty, unit):
            tok = get_csrf(client, f"/inventory/purchase/{pr_id}/edit")
            return client.post(f"/inventory/purchase/{pr_id}/edit", data={
                "csrf_token": tok, "request_date": now[:10], "job_name": "Test Job",
                "item[]": [item], "description[]": [desc], "supplier[]": [sup],
                "qty[]": [qty], "unit[]": [unit],
            }, follow_redirects=True)

        do_edit("C sand", "C sand", "New Supplier Co", "12", "Load")
        row2 = db.execute("SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id=?", (pr_id,)).fetchone()
        check("changing ONLY supplier: qty (12) survived untouched", row2["qty"] == "12")
        check("changing ONLY supplier: description survived untouched", row2["description"] == "C sand")
        check("supplier itself was actually updated", row2["supplier"] == "New Supplier Co")

        do_edit("Fine Sand", "C sand", "New Supplier Co", "12", "Load")
        row3 = db.execute("SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id=?", (pr_id,)).fetchone()
        check("changing ONLY item name: supplier survived untouched", row3["supplier"] == "New Supplier Co")

        do_edit("Fine Sand", "C sand", "New Supplier Co", "12", "Bag")
        row4 = db.execute("SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id=?", (pr_id,)).fetchone()
        check("changing ONLY unit: qty survived untouched", row4["qty"] == "12")

        print()
        print("=== Leaving an item entirely unchanged preserves it exactly ===")
        do_edit("Fine Sand", "C sand", "New Supplier Co", "12", "Bag")
        row5 = db.execute("SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id=?", (pr_id,)).fetchone()
        check("unchanged submission preserves every field exactly",
              (row5["item"], row5["description"], row5["supplier"], row5["qty"], row5["unit"]) == ("Fine Sand", "C sand", "New Supplier Co", "12", "Bag"))

        print()
        print("=== Add a second item -- both persist correctly ===")
        tok = get_csrf(client, f"/inventory/purchase/{pr_id}/edit")
        client.post(f"/inventory/purchase/{pr_id}/edit", data={
            "csrf_token": tok, "request_date": now[:10], "job_name": "Test Job",
            "item[]": ["Fine Sand", "Rebar"], "description[]": ["C sand", "#4 Rebar"],
            "supplier[]": ["New Supplier Co", "Steel Co"], "qty[]": ["12", "50"], "unit[]": ["Bag", "ea"],
        }, follow_redirects=True)
        rows_multi = db.execute("SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id=? ORDER BY id", (pr_id,)).fetchall()
        check("both items persisted after adding a second one", len(rows_multi) == 2)
        check("second item's values are correct", rows_multi[1]["item"] == "Rebar" and rows_multi[1]["qty"] == "50")

        edit_html_multi = client.get(f"/inventory/purchase/{pr_id}/edit").get_data(as_text=True)
        qty_values_multi = real_qty_values(edit_html_multi)
        check("editing again shows exactly 2 real item rows (not a huge stack of blanks)", len(qty_values_multi) == 2)
        check("both real quantities show correctly", set(qty_values_multi) == {"12", "50"})

        print()
        print("=== Edge-case quantity/field values ===")
        for qty_val, label in [("0", "Qty 0"), ("1", "Qty 1"), ("11", "Qty 11"), ("2.5", "decimal quantity"), ("100000", "large quantity"), ("", "blank quantity")]:
            tok = get_csrf(client, f"/inventory/purchase/{pr_id}/edit")
            client.post(f"/inventory/purchase/{pr_id}/edit", data={
                "csrf_token": tok, "request_date": now[:10], "job_name": "Test Job",
                "item[]": ["Item"], "description[]": ["Desc"], "supplier[]": ["Sup"],
                "qty[]": [qty_val], "unit[]": ["ea"],
            }, follow_redirects=True)
            saved = db.execute("SELECT qty FROM inventory_purchase_request_items WHERE purchase_request_id=?", (pr_id,)).fetchone()
            check(f"{label}: saved and re-editable value matches exactly what was submitted ({qty_val!r})", saved["qty"] == qty_val)
            edit_again = client.get(f"/inventory/purchase/{pr_id}/edit").get_data(as_text=True)
            check(f"{label}: correctly re-populates in the Edit form", real_qty_values(edit_again)[0] == qty_val)

        long_desc = "A" * 300
        long_supplier = "B" * 150
        long_unit = "C" * 60
        tok = get_csrf(client, f"/inventory/purchase/{pr_id}/edit")
        client.post(f"/inventory/purchase/{pr_id}/edit", data={
            "csrf_token": tok, "request_date": now[:10], "job_name": "Test Job",
            "item[]": ["Item"], "description[]": [long_desc], "supplier[]": [long_supplier],
            "qty[]": ["5"], "unit[]": [long_unit],
        }, follow_redirects=True)
        saved_long = db.execute("SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id=?", (pr_id,)).fetchone()
        check("long description is preserved exactly, not truncated", saved_long["description"] == long_desc)
        check("long supplier name is preserved exactly", saved_long["supplier"] == long_supplier)
        check("long unit is preserved exactly", saved_long["unit"] == long_unit)

        print()
        print("=== Desktop column headers (Description/Supplier/Qty/Unit) ===")
        edit_html_hdr = client.get(f"/inventory/purchase/{pr_id}/edit").get_data(as_text=True)
        check("desktop header row is present", "pr-items-header" in edit_html_hdr)
        check("header contains Description/Supplier/Qty/Unit labels",
              all(f">{label}<" in edit_html_hdr for label in ["Description", "Supplier", "Qty", "Unit"]))
        hdr_cols_m = re.search(r'\.pr-items-header \{[^}]*grid-template-columns: ([^;]+);', edit_html_hdr)
        row_cols_m = re.search(r'\.pr-item-row \{[^}]*grid-template-columns: ([^;]+);', edit_html_hdr)
        check("header columns use the EXACT same grid-template-columns as the item row (guaranteed alignment)",
              hdr_cols_m is not None and row_cols_m is not None and hdr_cols_m.group(1) == row_cols_m.group(1))
        check("header is hidden at mobile width (mobile keeps its existing inline field labels instead)",
              ".pr-items-header { display: none; }" in edit_html_hdr)
        check("mobile stacked-card field labels are still present/unchanged", "pr-item-field-label" in edit_html_hdr)

        print()
        print("=== Sibling Create flow: same responsive item-row behavior ===")
        new_html = client.get("/inventory/purchase/new").get_data(as_text=True)
        check("Create page renders exactly ONE blank item row (not 9 fixed blank rows)", len(real_qty_values(new_html)) == 1)
        check("Create page uses the SAME shared responsive item-rows partial as Edit", "pr-items-list" in new_html and "pr-add-item-btn" in new_html)
        check("Create page ALSO shows the desktop column header (consistent with Edit)", "pr-items-header" in new_html)

        tok_new = get_csrf(client, "/inventory/purchase/new")
        resp_create = client.post("/inventory/purchase/new", data={
            "csrf_token": tok_new, "request_date": now[:10], "job_name": "New PR Job",
            "item[]": ["Gravel"], "description[]": ["3/4 inch gravel"], "supplier[]": ["Rock Co"],
            "qty[]": ["30"], "unit[]": ["ton"],
        }, follow_redirects=True)
        check("Create flow still succeeds end to end", resp_create.status_code == 200)
        created_row = db.execute("SELECT * FROM inventory_purchase_requests WHERE job_name='New PR Job'").fetchone()
        check("new PR was actually created", created_row is not None)
        created_item = db.execute("SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id=?", (created_row["id"],)).fetchone()
        check("new PR's item was correctly saved", created_item["qty"] == "30" and created_item["supplier"] == "Rock Co")

        print()
        print("=== Detail view: responsive markup present for both desktop table and mobile cards ===")
        detail_html2 = client.get(f"/inventory/purchase/{pr_id}").get_data(as_text=True)
        check("detail view still has the desktop table (unchanged data)", "pr-detail-items-table" in detail_html2)
        check("detail view NOW also has a mobile card fallback (new)", "pr-detail-items-cards" in detail_html2)

        print()
        print("=== Security: CSRF, authorization, parameterized SQL ===")
        resp_no_csrf = client.post(f"/inventory/purchase/{pr_id}/edit", data={
            "request_date": now[:10], "job_name": "Should not save",
            "item[]": ["x"], "description[]": ["x"], "supplier[]": ["x"], "qty[]": ["999"], "unit[]": ["ea"],
        })
        check("edit without CSRF token is rejected", resp_no_csrf.status_code == 400)
        row_after_csrf_attempt = db.execute("SELECT qty FROM inventory_purchase_request_items WHERE purchase_request_id=?", (pr_id,)).fetchone()
        check("the CSRF-less attempt did not actually change the saved data", row_after_csrf_attempt["qty"] != "999")

        tok_inj = get_csrf(client, f"/inventory/purchase/{pr_id}/edit")
        client.post(f"/inventory/purchase/{pr_id}/edit", data={
            "csrf_token": tok_inj, "request_date": now[:10], "job_name": "Test Job",
            "item[]": ["Item'; DROP TABLE inventory_purchase_request_items; --"], "description[]": ["desc"],
            "supplier[]": ["sup"], "qty[]": ["5"], "unit[]": ["ea"],
        }, follow_redirects=True)
        still_exists = db.execute("SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name='inventory_purchase_request_items'").fetchone()
        check("SQL-injection-shaped input does not affect the database structure (parameterized queries hold)", still_exists["c"] == 1)
        stored_literal = db.execute("SELECT item FROM inventory_purchase_request_items WHERE purchase_request_id=?", (pr_id,)).fetchone()
        check("the injection-shaped text was stored as a literal string, not executed", "DROP TABLE" in stored_literal["item"])

    with appmod.app.test_client() as client_noperm:
        login(client_noperm, "__prhotfix_noperm@test.local", pw)
        resp_denied = client_noperm.get(f"/inventory/purchase/{pr_id}/edit", follow_redirects=True)
        check("a user without action:sitepulse:manage cannot open Edit (authorization unchanged)",
              "don't have permission" in resp_denied.get_data(as_text=True).lower() or resp_denied.request.path != f"/inventory/purchase/{pr_id}/edit")

    with appmod.app.test_client() as client:
        login(client, "__prhotfix_full@test.local", pw)
        resp_missing = client.get("/inventory/purchase/999999999/edit", follow_redirects=True)
        check("editing a nonexistent request id fails safely (no crash, redirected)", resp_missing.status_code == 200)

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM inventory_purchase_request_items WHERE purchase_request_id IN (SELECT id FROM inventory_purchase_requests WHERE pr_number LIKE 'PR-HOTFIX%' OR job_name IN ('Test Job','New PR Job'))")
    db.execute("DELETE FROM inventory_purchase_requests WHERE pr_number LIKE 'PR-HOTFIX%' OR job_name IN ('Test Job','New PR Job')")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
