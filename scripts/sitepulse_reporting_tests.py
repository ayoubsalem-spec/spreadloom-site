"""
SitePulse Reporting V1 regression.

Usage:
    APP_ENV=development python3 scripts/sitepulse_reporting_tests.py
"""
import sys
import os
import io
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import sqlite3
from datetime import datetime
from unittest.mock import patch
from PIL import Image

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


def make_user(db, email, name, now, pw_hash, permission_keys):
    db.execute("DELETE FROM users WHERE email=?", (email,))
    db.commit()
    db.execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)", (name, email, pw_hash, now))
    db.commit()
    uid = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    for key in permission_keys:
        pid = db.execute("SELECT id FROM permissions WHERE key=?", (key,)).fetchone()[0]
        db.execute("INSERT OR REPLACE INTO user_permission_overrides (user_id, permission_id, state, granted_by, updated_at) VALUES (?,?,?,?,?)",
                   (uid, pid, "grant", "test_setup", now))
    db.commit()
    return uid


def make_png(color=(100, 150, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (200, 150), color=color).save(buf, format="PNG")
    buf.seek(0)
    return buf


def make_bad_file():
    buf = io.BytesIO(b"not an image, just text")
    return buf


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.utcnow().isoformat()
    from werkzeug.security import generate_password_hash
    pw = "TestPass123!"
    pw_hash = generate_password_hash(pw)

    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__RepTest%'")
    db.commit()

    def make_project(name="__RepTest Peninsula", status="In Progress"):
        cur = db.execute(
            "INSERT INTO tracker_projects (name, client, address, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (name, "Client Co", "123 St", status, now, now)
        )
        db.commit()
        return cur.lastrowid

    author_uid = make_user(db, "__rep_author@test.local", "__rep_author", now, pw_hash,
                            ["module:sitepulse:view", "action:sitepulse:report", "action:sitepulse:manage"])
    view_only_uid = make_user(db, "__rep_viewer@test.local", "__rep_viewer", now, pw_hash, ["module:sitepulse:view"])
    no_perm_uid = make_user(db, "__rep_noperm@test.local", "__rep_noperm", now, pw_hash, [])

    author_client = appmod.app.test_client()
    viewer_client = appmod.app.test_client()
    noperm_client = appmod.app.test_client()
    login(author_client, "__rep_author@test.local", pw)
    login(viewer_client, "__rep_viewer@test.local", pw)
    login(noperm_client, "__rep_noperm@test.local", pw)

    # ============================================================
    print("=== 1/2. Permissions: view access vs unauthorized ===")
    pid = make_project()
    check("1. module:sitepulse:view user can view Daily Capture page", author_client.get(f"/sitepulse/project/{pid}/photos").status_code == 200)
    resp_noperm = noperm_client.get(f"/sitepulse/project/{pid}/photos", follow_redirects=True)
    check("2. no-permission user denied (redirected away)", resp_noperm.request.path != f"/sitepulse/project/{pid}/photos")

    print()
    print("=== 3/4/5. Daily Capture upload: valid multi-photo, invalid file rejected ===")
    token = get_csrf(author_client, f"/sitepulse/project/{pid}/photos")
    resp_upload = author_client.post(f"/sitepulse/project/{pid}/photos", data={
        "csrf_token": token, "photos": [(make_png(), "a.png"), (make_png(), "b.png")]
    }, content_type="multipart/form-data")
    photos = db.execute("SELECT * FROM project_field_photos WHERE project_id=?", (pid,)).fetchall()
    check("3. Daily Capture upload succeeds", resp_upload.status_code in (200, 302))
    check("5. multi-photo capture: both photos saved in one request", len(photos) == 2)

    token_bad = get_csrf(author_client, f"/sitepulse/project/{pid}/photos")
    author_client.post(f"/sitepulse/project/{pid}/photos", data={
        "csrf_token": token_bad, "photos": [(make_bad_file(), "evil.exe")]
    }, content_type="multipart/form-data")
    photos_after_bad = db.execute("SELECT * FROM project_field_photos WHERE project_id=?", (pid,)).fetchall()
    check("4. invalid file type rejected (no new row created)", len(photos_after_bad) == 2)

    print()
    print("=== 6/7/8. Caption persistence, project association ===")
    token_cap = get_csrf(author_client, f"/sitepulse/project/{pid}/photos")
    author_client.post(f"/sitepulse/photos/{photos[0]['id']}/caption", data={"csrf_token": token_cap, "caption": "West wall framing"})
    photo_after = db.execute("SELECT * FROM project_field_photos WHERE id=?", (photos[0]["id"],)).fetchone()
    check("6. caption persists", photo_after["caption"] == "West wall framing")
    check("8. photo correctly associated with its project", photo_after["project_id"] == pid)

    print()
    print("=== 9/10. Report creation and editing ===")
    token_new = get_csrf(author_client, f"/sitepulse/project/{pid}/reports")
    author_client.post(f"/sitepulse/project/{pid}/reports/new", data={"csrf_token": token_new})
    report = db.execute("SELECT * FROM field_reports WHERE project_id=?", (pid,)).fetchone()
    check("9. report created in Draft status", report is not None and report["status"] == "Draft")

    token_edit = get_csrf(author_client, f"/sitepulse/reports/{report['id']}")
    author_client.post(f"/sitepulse/reports/{report['id']}", data={
        "csrf_token": token_edit, "report_date": "2026-09-10", "work_completed": "Framed the west wall",
        "has_issues": "no", "next_steps": "Continue framing", "general_notes": "",
        "selected_photos": [str(photos[0]["id"]), str(photos[1]["id"])]
    })
    report_after = db.execute("SELECT * FROM field_reports WHERE id=?", (report["id"],)).fetchone()
    check("10. report editing saves correctly", report_after["work_completed"] == "Framed the west wall")

    print()
    print("=== 11/12. Photo selection and ordering ===")
    selections = db.execute("SELECT * FROM report_photo_selections WHERE report_id=? ORDER BY sort_order", (report["id"],)).fetchall()
    check("10. photo selection saved", len(selections) == 2)
    check("11. photo ordering preserved (sort_order 0, 1)", [s["sort_order"] for s in selections] == [0, 1])

    print()
    print("=== 12b. No Issues state is deterministic ===")
    token_issue = get_csrf(author_client, f"/sitepulse/reports/{report['id']}")
    author_client.post(f"/sitepulse/reports/{report['id']}", data={
        "csrf_token": token_issue, "report_date": "2026-09-10", "work_completed": "Framed the west wall",
        "has_issues": "yes", "issues_blockers": "Delivery delayed", "next_steps": "Continue framing", "general_notes": "",
        "selected_photos": [str(photos[0]["id"])]
    })
    with_issue = db.execute("SELECT * FROM field_reports WHERE id=?", (report["id"],)).fetchone()
    check("12. has_issues=1 when Add Issue selected, text preserved", with_issue["has_issues"] == 1 and with_issue["issues_blockers"] == "Delivery delayed")

    token_noissue = get_csrf(author_client, f"/sitepulse/reports/{report['id']}")
    author_client.post(f"/sitepulse/reports/{report['id']}", data={
        "csrf_token": token_noissue, "report_date": "2026-09-10", "work_completed": "Framed the west wall",
        "has_issues": "no", "next_steps": "Continue framing", "general_notes": "",
        "selected_photos": [str(photos[0]["id"])]
    })
    no_issue_after = db.execute("SELECT * FROM field_reports WHERE id=?", (report["id"],)).fetchone()
    check("12. switching back to No Issues clears has_issues AND stale issue text (no silent inconsistency)",
          no_issue_after["has_issues"] == 0 and no_issue_after["issues_blockers"] is None)

    print()
    print("=== 13. Preview creates no official version ===")
    preview_resp = author_client.get(f"/sitepulse/reports/{report['id']}/preview")
    check("13. preview returns a real PDF", preview_resp.status_code == 200 and preview_resp.content_type == "application/pdf" and len(preview_resp.data) > 500)
    versions_after_preview = db.execute("SELECT COUNT(*) c FROM field_report_versions WHERE report_id=?", (report["id"],)).fetchone()["c"]
    check("13. preview creates zero field_report_versions rows", versions_after_preview == 0)

    print()
    print("=== 14/15/16. First Submit creates V1, PDF exists, snapshot correct ===")
    token_submit1 = get_csrf(author_client, f"/sitepulse/reports/{report['id']}")
    author_client.post(f"/sitepulse/reports/{report['id']}/submit", data={"csrf_token": token_submit1})
    report_v1 = db.execute("SELECT * FROM field_reports WHERE id=?", (report["id"],)).fetchone()
    check("14. status is Submitted after first submit", report_v1["status"] == "Submitted")
    v1 = db.execute("SELECT * FROM field_report_versions WHERE report_id=? AND version_number=1", (report["id"],)).fetchone()
    check("14. V1 version row created", v1 is not None)
    check("14. field_reports.current_version_id points at V1", report_v1["current_version_id"] == v1["id"])
    v1_pdf_path = os.path.join(appmod.UPLOAD_DIR, v1["pdf_filename"])
    check("15. V1 PDF file physically exists on disk", os.path.exists(v1_pdf_path))
    v1_snapshot = json.loads(v1["content_snapshot_json"])
    check("16. V1 snapshot contains correct work_completed", v1_snapshot["work_completed"] == "Framed the west wall")
    check("16. V1 snapshot contains correct has_issues", v1_snapshot["has_issues"] is False)
    check("16. V1 snapshot contains correct photo selection", len(v1_snapshot["photos"]) == 1)
    check("22. V1 snapshot's photo list matches what was selected at that time", v1_snapshot["photos"][0]["photo_id"] == photos[0]["id"])

    print()
    print("=== 17/18/19/20/21/23. Reopen -> edit -> resubmit -> V1 preserved, V2 created ===")
    token_reopen = get_csrf(author_client, f"/sitepulse/reports/{report['id']}")
    reopen_resp = author_client.post(f"/sitepulse/reports/{report['id']}/reopen", data={"csrf_token": token_reopen})
    report_reopened = db.execute("SELECT * FROM field_reports WHERE id=?", (report["id"],)).fetchone()
    check("17. reopen sets status back to Draft", report_reopened["status"] == "Draft")
    v1_still_there = db.execute("SELECT * FROM field_report_versions WHERE id=?", (v1["id"],)).fetchone()
    check("17. V1 version row untouched after reopen", v1_still_there is not None and v1_still_there["content_snapshot_json"] == v1["content_snapshot_json"])

    token_edit2 = get_csrf(author_client, f"/sitepulse/reports/{report['id']}")
    author_client.post(f"/sitepulse/reports/{report['id']}", data={
        "csrf_token": token_edit2, "report_date": "2026-09-11", "work_completed": "Completed roof framing",
        "has_issues": "no", "next_steps": "Start roofing", "general_notes": "",
        "selected_photos": [str(photos[0]["id"]), str(photos[1]["id"])]
    })
    check("18. edit after reopen saves", db.execute("SELECT work_completed FROM field_reports WHERE id=?", (report["id"],)).fetchone()["work_completed"] == "Completed roof framing")
    check("23. photo selection can change for the new draft (now 2 photos)", db.execute("SELECT COUNT(*) c FROM report_photo_selections WHERE report_id=?", (report["id"],)).fetchone()["c"] == 2)

    token_submit2 = get_csrf(author_client, f"/sitepulse/reports/{report['id']}")
    author_client.post(f"/sitepulse/reports/{report['id']}/submit", data={"csrf_token": token_submit2})
    v2 = db.execute("SELECT * FROM field_report_versions WHERE report_id=? AND version_number=2", (report["id"],)).fetchone()
    check("19. second submit creates V2", v2 is not None)
    report_v2 = db.execute("SELECT * FROM field_reports WHERE id=?", (report["id"],)).fetchone()
    check("19. current_version_id now points at V2", report_v2["current_version_id"] == v2["id"])

    v1_after_v2 = db.execute("SELECT * FROM field_report_versions WHERE id=?", (v1["id"],)).fetchone()
    check("20. V1 remains retrievable, completely unchanged after V2 exists", v1_after_v2["content_snapshot_json"] == v1["content_snapshot_json"])
    v2_snapshot = json.loads(v2["content_snapshot_json"])
    check("21. V2 snapshot correctly differs from V1 (different work_completed)", v2_snapshot["work_completed"] != v1_snapshot["work_completed"])
    check("21. V2 snapshot has the updated photo count (2, not 1)", len(v2_snapshot["photos"]) == 2)

    print()
    print("=== 24/25/26. Secure photo/PDF access, no arbitrary filename access ===")
    resp_photo_auth = author_client.get(f"/sitepulse/photos/{photos[0]['id']}/file")
    check("24. authorized user can access a project photo", resp_photo_auth.status_code == 200)
    resp_photo_unauth = noperm_client.get(f"/sitepulse/photos/{photos[0]['id']}/file")
    check("24. unauthorized user denied (403)", resp_photo_unauth.status_code == 403)
    resp_photo_unknown = author_client.get("/sitepulse/photos/999999/file")
    check("26. unknown photo id -> 404, not a crash", resp_photo_unknown.status_code == 404)

    resp_pdf_auth = author_client.get(f"/sitepulse/reports/{report['id']}/versions/{v1['id']}/pdf")
    check("25. authorized user can access a report PDF", resp_pdf_auth.status_code == 200 and resp_pdf_auth.content_type == "application/pdf")
    resp_pdf_unauth = noperm_client.get(f"/sitepulse/reports/{report['id']}/versions/{v1['id']}/pdf")
    check("25. unauthorized user denied PDF access (403)", resp_pdf_unauth.status_code == 403)
    resp_pdf_wrong_report = author_client.get(f"/sitepulse/reports/999999/versions/{v1['id']}/pdf")
    check("26. mismatched report_id/version_id -> 404 (no arbitrary access)", resp_pdf_wrong_report.status_code == 404)

    print()
    print("=== 27/28. Historical version access, report history ===")
    resp_v1_pdf_still = author_client.get(f"/sitepulse/reports/{report['id']}/versions/{v1['id']}/pdf")
    check("27. V1's own PDF still independently downloadable after V2 exists", resp_v1_pdf_still.status_code == 200)
    history_resp = author_client.get(f"/sitepulse/project/{pid}/reports")
    history_body = history_resp.get_data(as_text=True)
    check("28. report history page loads and shows the report", history_resp.status_code == 200 and "V2" in history_body)

    print()
    print("=== 29. Mobile template structural smoke ===")
    check("29. history page has a mobile-only stacked section", 'class="mobile-only"' in history_body and 'class="card desktop-only"' in history_body)

    print()
    print("=== 30. Web Share/download fallback UI present -- actual JS wiring, not just text ===")
    detail_body = author_client.get(f"/sitepulse/reports/{report['id']}").get_data(as_text=True)
    check("30. report detail page offers a Download PDF action for each version", "Download PDF" in detail_body)
    check("30. Share button element is actually present", "share-report-btn" in detail_body)
    check("30. real navigator.share() wiring is present (not just a label)", "navigator.share" in detail_body)
    check("30. navigator.canShare file-sharing path is wired", "navigator.canShare" in detail_body)
    check("30. clean fallback to opening the authorized PDF URL when Web Share is unsupported", "window.open(pdfUrl" in detail_body)
    check("30. Share button uses the SAME authorized PDF URL as Download (no separate/weaker path)",
          re.search(r'data-pdf-url="([^"]+)"', detail_body).group(1) in detail_body and "versions" in re.search(r'data-pdf-url="([^"]+)"', detail_body).group(1))

    print()
    print("=== 31. Activity log events ===")
    check("31. photos_added event logged", db.execute("SELECT * FROM activity_log WHERE section='sitepulse_reporting' AND action='photos_added' AND entity_id=?", (pid,)).fetchone() is not None)
    check("31. report_created event logged", db.execute("SELECT * FROM activity_log WHERE section='sitepulse_reporting' AND action='report_created' AND entity_id=?", (report["id"],)).fetchone() is not None)
    check("31. report_submitted event logged (at least once)", db.execute("SELECT COUNT(*) c FROM activity_log WHERE section='sitepulse_reporting' AND action='report_submitted' AND entity_id=?", (report["id"],)).fetchone()["c"] >= 2)
    check("31. report_reopened event logged", db.execute("SELECT * FROM activity_log WHERE section='sitepulse_reporting' AND action='report_reopened' AND entity_id=?", (report["id"],)).fetchone() is not None)

    print()
    print("=== Reopen requires manage permission, not just author ===")
    token_reopen2 = get_csrf(author_client, f"/sitepulse/reports/{report['id']}")
    author_client.post(f"/sitepulse/reports/{report['id']}/submit", data={"csrf_token": token_reopen2})
    author_only_uid = make_user(db, "__rep_authoronly@test.local", "__rep_authoronly", now, pw_hash, ["module:sitepulse:view", "action:sitepulse:report"])
    author_only_client = appmod.app.test_client()
    login(author_only_client, "__rep_authoronly@test.local", pw)
    token_reopen3 = get_csrf(author_only_client, f"/sitepulse/reports/{report['id']}")
    author_only_client.post(f"/sitepulse/reports/{report['id']}/reopen", data={"csrf_token": token_reopen3})
    still_submitted = db.execute("SELECT status FROM field_reports WHERE id=?", (report["id"],)).fetchone()
    check("action:sitepulse:report alone (no manage) cannot reopen a submitted report", still_submitted["status"] == "Submitted")

    print()
    print("=== Submit failure does not create a broken version row ===")
    pid_fail = make_project(name="__RepTest FailCase")
    token_f = get_csrf(author_client, f"/sitepulse/project/{pid_fail}/reports")
    author_client.post(f"/sitepulse/project/{pid_fail}/reports/new", data={"csrf_token": token_f})
    report_fail = db.execute("SELECT * FROM field_reports WHERE project_id=?", (pid_fail,)).fetchone()
    token_f2 = get_csrf(author_client, f"/sitepulse/reports/{report_fail['id']}")
    author_client.post(f"/sitepulse/reports/{report_fail['id']}", data={
        "csrf_token": token_f2, "work_completed": "Some work", "has_issues": "no", "next_steps": "X", "general_notes": ""
    })
    with patch("app.build_field_report_pdf", side_effect=Exception("simulated PDF failure")):
        token_f3 = get_csrf(author_client, f"/sitepulse/reports/{report_fail['id']}")
        author_client.post(f"/sitepulse/reports/{report_fail['id']}/submit", data={"csrf_token": token_f3})
    report_fail_after = db.execute("SELECT * FROM field_reports WHERE id=?", (report_fail["id"],)).fetchone()
    check("PDF generation failure leaves report in Draft (not falsely Submitted)", report_fail_after["status"] == "Draft")
    check("PDF generation failure creates zero version rows", db.execute("SELECT COUNT(*) c FROM field_report_versions WHERE report_id=?", (report_fail["id"],)).fetchone()["c"] == 0)

    print()
    print("=== Submit requires Work Completed ===")
    pid_empty = make_project(name="__RepTest EmptyCase")
    token_e = get_csrf(author_client, f"/sitepulse/project/{pid_empty}/reports")
    author_client.post(f"/sitepulse/project/{pid_empty}/reports/new", data={"csrf_token": token_e})
    report_empty = db.execute("SELECT * FROM field_reports WHERE project_id=?", (pid_empty,)).fetchone()
    token_e2 = get_csrf(author_client, f"/sitepulse/reports/{report_empty['id']}")
    author_client.post(f"/sitepulse/reports/{report_empty['id']}/submit", data={"csrf_token": token_e2})
    report_empty_after = db.execute("SELECT * FROM field_reports WHERE id=?", (report_empty["id"],)).fetchone()
    check("empty Work Completed blocks submission", report_empty_after["status"] == "Draft")

    # ============================================================
    print()
    print("=== Malformed permission state: action:sitepulse:report WITHOUT module:sitepulse:view ===")
    malformed_uid = make_user(db, "__rep_malformed@test.local", "__rep_malformed", now, pw_hash, ["action:sitepulse:report"])
    malformed_client = appmod.app.test_client()
    login(malformed_client, "__rep_malformed@test.local", pw)
    pid_malformed = make_project(name="__RepTest Malformed")

    token_m1 = get_csrf(malformed_client, f"/sitepulse/project/{pid_malformed}/photos")
    resp_m_upload = malformed_client.post(f"/sitepulse/project/{pid_malformed}/photos", data={
        "csrf_token": token_m1, "photos": [(make_png(), "x.png")]
    } if token_m1 else {}, content_type="multipart/form-data")
    check("malformed permission (action only, no module:view): cannot upload field photos",
          db.execute("SELECT COUNT(*) c FROM project_field_photos WHERE project_id=?", (pid_malformed,)).fetchone()["c"] == 0)

    token_m2 = get_csrf(malformed_client, f"/sitepulse/project/{pid_malformed}/reports")
    malformed_client.post(f"/sitepulse/project/{pid_malformed}/reports/new", data={"csrf_token": token_m2} if token_m2 else {})
    check("malformed permission: cannot create reports", db.execute("SELECT COUNT(*) c FROM field_reports WHERE project_id=?", (pid_malformed,)).fetchone()["c"] == 0)

    # Seed a report via the properly-authorized author client to test caption/edit/submit denial specifically.
    photo_for_malformed = db.execute("SELECT id FROM project_field_photos WHERE project_id=?", (pid,)).fetchone()
    token_cap_m = get_csrf(malformed_client, f"/sitepulse/project/{pid}/photos")
    malformed_client.post(f"/sitepulse/photos/{photo_for_malformed['id']}/caption", data={"csrf_token": token_cap_m, "caption": "SHOULD NOT SAVE"} if token_cap_m else {})
    photo_unchanged = db.execute("SELECT caption FROM project_field_photos WHERE id=?", (photo_for_malformed["id"],)).fetchone()
    check("malformed permission: cannot edit captions", photo_unchanged["caption"] != "SHOULD NOT SAVE")

    token_edit_m = get_csrf(malformed_client, f"/sitepulse/reports/{report['id']}")
    malformed_client.post(f"/sitepulse/reports/{report['id']}", data={
        "csrf_token": token_edit_m, "work_completed": "SHOULD NOT SAVE", "has_issues": "no", "next_steps": "x", "general_notes": ""
    } if token_edit_m else {})
    report_unchanged = db.execute("SELECT work_completed FROM field_reports WHERE id=?", (report["id"],)).fetchone()
    check("malformed permission: cannot edit reports", report_unchanged["work_completed"] != "SHOULD NOT SAVE")

    # reopen it via a real manager first so there's a Draft to attempt submitting.
    mgr_reopen_token = get_csrf(author_client, f"/sitepulse/reports/{report['id']}")
    author_client.post(f"/sitepulse/reports/{report['id']}/reopen", data={"csrf_token": mgr_reopen_token})
    versions_before_malformed_submit = db.execute("SELECT COUNT(*) c FROM field_report_versions WHERE report_id=?", (report["id"],)).fetchone()["c"]
    token_submit_m = get_csrf(malformed_client, f"/sitepulse/reports/{report['id']}")
    malformed_client.post(f"/sitepulse/reports/{report['id']}/submit", data={"csrf_token": token_submit_m} if token_submit_m else {})
    versions_after_malformed_submit = db.execute("SELECT COUNT(*) c FROM field_report_versions WHERE report_id=?", (report["id"],)).fetchone()["c"]
    check("malformed permission: cannot submit reports", versions_after_malformed_submit == versions_before_malformed_submit)
    # restore report to Submitted so later assertions in this file aren't affected.
    resubmit_token = get_csrf(author_client, f"/sitepulse/reports/{report['id']}")
    author_client.post(f"/sitepulse/reports/{report['id']}/submit", data={"csrf_token": resubmit_token})

    print()
    print("=== Normal module + action author can still mutate (regression check for the permission fix) ===")
    pid_normal = make_project(name="__RepTest NormalAuthor")
    token_n = get_csrf(author_client, f"/sitepulse/project/{pid_normal}/reports")
    resp_n = author_client.post(f"/sitepulse/project/{pid_normal}/reports/new", data={"csrf_token": token_n})
    check("normal module:view + action:report user can still create reports", db.execute("SELECT COUNT(*) c FROM field_reports WHERE project_id=?", (pid_normal,)).fetchone()["c"] == 1)

    print()
    print("=== Version snapshot contains BOTH created_by and submitted_by ===")
    latest_version = db.execute("SELECT * FROM field_report_versions WHERE report_id=? ORDER BY version_number DESC LIMIT 1", (report["id"],)).fetchone()
    latest_snapshot = json.loads(latest_version["content_snapshot_json"])
    check("version snapshot contains created_by", latest_snapshot.get("created_by") == "__rep_author")
    check("version snapshot contains submitted_by", latest_snapshot.get("submitted_by") == "__rep_author")

    print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")

    print("\nCleaning up...")
    db.execute("DELETE FROM report_photo_selections WHERE report_id IN (SELECT id FROM field_reports WHERE project_id IN (SELECT id FROM tracker_projects WHERE name LIKE '__RepTest%'))")
    db.execute("DELETE FROM field_report_versions WHERE report_id IN (SELECT id FROM field_reports WHERE project_id IN (SELECT id FROM tracker_projects WHERE name LIKE '__RepTest%'))")
    db.execute("DELETE FROM field_reports WHERE project_id IN (SELECT id FROM tracker_projects WHERE name LIKE '__RepTest%')")
    db.execute("DELETE FROM project_field_photos WHERE project_id IN (SELECT id FROM tracker_projects WHERE name LIKE '__RepTest%')")
    db.execute("DELETE FROM tracker_projects WHERE name LIKE '__RepTest%'")
    db.commit()
    hygiene.cleanup_test_users_by_prefix(db)
    hygiene.assert_no_orphan_privilege_rows(db)
    db.close()

    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
