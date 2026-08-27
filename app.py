"""
BuildIQ -- Darycet's unified technology platform.

One Flask app, one login, one database. Sections are prefixed by URL path
rather than split into separate Flask Blueprint objects, to keep this first
version simple to read top-to-bottom. Sections:
  /            -- home (app picker)
  /tracker/... -- Bid Tracker (formerly Command Center: projects + quotes)
  /sitepulse/... -- SitePulse (equipment + outside rentals)
  /inventory/... -- Site Inventory (concrete requests + material inventory)

This is the first migration pass. Ported fully: auth, home, SitePulse
(equipment + rentals), Site Inventory (concrete requests + materials), and
Bid Tracker's core (projects, quotes, dashboard). NOT yet ported from the
original Command Center: AI-generated RFQ/follow-up emails, quote file
attachments, and Excel export -- flagged here rather than silently dropped,
to be added in a follow-up pass.
"""
import os
import sqlite3
import uuid
import json
import secrets
import base64
import io
import requests
import markdown as md_lib
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfgen import canvas as pdf_canvas
from flask import Flask, render_template, request, redirect, url_for, flash, g, send_file, send_from_directory, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
csrf = CSRFProtect(app)


HOUSTON_TZ = ZoneInfo("America/Chicago")

# --- WhatsApp group notifications (Green API) ------------------------------
# Set these three in your environment (Railway variables, etc.) once you have
# a Green API instance linked to a WhatsApp number that's in the procurement
# group. Left blank, notifications are silently skipped (logged to console)
# so nothing breaks if they're not configured yet.
# --- WhatsApp group notifications (Ultramsg) --------------------------------
# Set these three (four, counting the second group) in your environment
# (Railway variables, etc.) once you have an Ultramsg instance linked to a
# WhatsApp number that's in both the procurement and SitePulse groups. Left
# blank, notifications are silently skipped (logged to console) so nothing
# breaks if they're not configured yet.
ULTRAMSG_INSTANCE_ID = os.environ.get("ULTRAMSG_INSTANCE_ID", "")
ULTRAMSG_TOKEN = os.environ.get("ULTRAMSG_TOKEN", "")
ULTRAMSG_GROUP_CHAT_ID = os.environ.get("ULTRAMSG_GROUP_CHAT_ID", "")  # e.g. "123456789-987654321@g.us" -- procurement
ULTRAMSG_SITEPULSE_GROUP_CHAT_ID = os.environ.get("ULTRAMSG_SITEPULSE_GROUP_CHAT_ID", "")  # equipment/SitePulse group


def send_whatsapp_group_message(text, chat_id=None):
    """Post a message into a WhatsApp group via Ultramsg. Defaults to the
    procurement group if one's configured, otherwise falls back to the
    SitePulse group -- so if only one group is set up (current setup: just
    SitePulse), everything lands there. Never raises -- a WhatsApp hiccup
    should never block someone submitting a request or moving equipment.
    Failures are printed to the server log instead. Returns (ok, detail) so
    callers that want to report success/failure (e.g. the test-message
    button) can, without every other call site needing to check it.
    """
    chat_id = chat_id or ULTRAMSG_GROUP_CHAT_ID or ULTRAMSG_SITEPULSE_GROUP_CHAT_ID
    if not (ULTRAMSG_INSTANCE_ID and ULTRAMSG_TOKEN and chat_id):
        msg = "Ultramsg not configured -- skipping notification:\n" + text
        print("[whatsapp] " + msg)
        return False, "WhatsApp isn't configured yet (missing instance ID, token, or chat ID)."
    url = f"https://api.ultramsg.com/{ULTRAMSG_INSTANCE_ID}/messages/chat"
    try:
        resp = requests.post(url, data={"token": ULTRAMSG_TOKEN, "to": chat_id, "body": text}, timeout=10)
        if resp.status_code >= 300:
            print(f"[whatsapp] Ultramsg returned {resp.status_code}: {resp.text}")
            return False, f"Ultramsg returned an error ({resp.status_code})."
        return True, "Sent."
    except requests.RequestException as e:
        print(f"[whatsapp] failed to send notification: {e}")
        return False, f"Network error reaching Ultramsg: {e}"


def whatsapp_chat_id_for_site(*texts):
    """Match project/job/location text against the configured per-site
    WhatsApp groups (keyword is a case-insensitive substring match against
    any of the given texts, e.g. project="Peninsula Job #4" matches
    keyword="peninsula"). Falls back to the default SitePulse group if
    nothing matches or no site groups are configured yet.
    """
    db = get_db()
    rows = db.execute("SELECT keyword, chat_id FROM whatsapp_site_groups").fetchall()
    haystack = " ".join(t for t in texts if t).lower()
    for row in rows:
        if row["keyword"].lower() in haystack:
            return row["chat_id"]
    return None  # let send_whatsapp_group_message fall back to the default


def send_whatsapp_document(pdf_bytes, filename, chat_id=None, caption=None):
    """Post a PDF (or any small file) into a WhatsApp group via Ultramsg,
    sent as base64 directly in the request -- no public URL/hosting
    needed. Same never-raises, (ok, detail) contract as
    send_whatsapp_group_message. Ultramsg's base64 limit is ~6.5MB of
    encoded text, plenty for a one-page order summary.
    """
    chat_id = chat_id or ULTRAMSG_GROUP_CHAT_ID or ULTRAMSG_SITEPULSE_GROUP_CHAT_ID
    if not (ULTRAMSG_INSTANCE_ID and ULTRAMSG_TOKEN and chat_id):
        print(f"[whatsapp] Ultramsg not configured -- skipping document send: {filename}")
        return False, "WhatsApp isn't configured yet (missing instance ID, token, or chat ID)."
    url = f"https://api.ultramsg.com/{ULTRAMSG_INSTANCE_ID}/messages/document"
    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    data = {"token": ULTRAMSG_TOKEN, "to": chat_id, "document": b64, "filename": filename}
    if caption:
        data["caption"] = caption
    try:
        resp = requests.post(url, data=data, timeout=20)
        if resp.status_code >= 300:
            print(f"[whatsapp] Ultramsg document send returned {resp.status_code}: {resp.text}")
            return False, f"Ultramsg returned an error ({resp.status_code})."
        return True, "Sent."
    except requests.RequestException as e:
        print(f"[whatsapp] failed to send document: {e}")
        return False, f"Network error reaching Ultramsg: {e}"


def _pdf_write_wrapped(c, text, x, y, max_width, font="Helvetica", size=10, leading=14):
    """Write text to a reportlab canvas, wrapping at max_width. Returns the
    y position after the last line, so callers can keep stacking sections."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    c.setFont(font, size)
    words = text.split(" ")
    line = ""
    for word in words:
        trial = (line + " " + word).strip()
        if stringWidth(trial, font, size) > max_width and line:
            c.drawString(x, y, line)
            y -= leading
            line = word
        else:
            line = trial
    if line:
        c.drawString(x, y, line)
        y -= leading
    return y


def build_concrete_order_pdf(r):
    """One-page PDF summary of a placed concrete order -- project, pour
    details, and every vendor/contact, for attaching to the WhatsApp
    notification and for anyone who wants a printable copy.
    """
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    navy = colors.HexColor("#0B1220")
    gold = colors.HexColor("#D4A537")
    x = 0.75 * inch
    y = height - 0.9 * inch

    c.setFillColor(navy)
    c.rect(0, height - 1.1 * inch, width, 1.1 * inch, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(x, height - 0.65 * inch, "Concrete Order Confirmation")
    c.setFont("Helvetica", 10)
    c.drawString(x, height - 0.9 * inch, f"Darycet International  |  Order placed {date.today().isoformat()}")

    y = height - 1.5 * inch
    c.setFillColor(navy)

    def section(title):
        nonlocal y
        y -= 6
        c.setFillColor(gold)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x, y, title)
        c.setFillColor(navy)
        y -= 18

    def line(label, value):
        nonlocal y
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x, y, f"{label}:")
        c.setFont("Helvetica", 10)
        c.drawString(x + 1.6 * inch, y, str(value) if value else "\u2014")
        y -= 16

    section("Project")
    line("Project", r["project"])
    line("Job Site Address", r["job_site_address"])
    line("Pour Date", r["pour_date"] + (f" at {r['pour_time']}" if r["pour_time"] else "") if r["pour_date"] else "")
    line("Amount / Mix", " ".join(v for v in [r["concrete_amount"], f"{r['mix_design_psi']} PSI" if r["mix_design_psi"] else ""] if v))

    section("Concrete")
    line("Company", r["concrete_company"])
    line("Phone", r["concrete_company_phone"])
    line("Arrival Time", r["concrete_arrival_time"] or r["pour_time"])

    if r["pump_company"] or r["pump_size"]:
        section(r["pump_type"] if r["pump_type"] else "Pump")
        line("Type", r["pump_type"])
        line("Size", r["pump_size"])
        line("Contact", r["pump_company"])
        line("Phone", r["pump_company_phone"])
        line("Arrival Time", r["pump_arrival_time"])

    if r["lab_required"] == "Yes":
        section("Lab")
        line("Company", r["lab_company"])
        line("Time", r["lab_time"])

    if r["drilling_required"] == "Yes":
        section("Drilling")
        line("Company", r["drilling_company"])
        line("Phone", r["drilling_company_phone"])
        line("Time", r["drilling_time"])

    section("Ordered By")
    line("Name", r["ordered_by"])
    line("Date", r["ordered_date"])

    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(colors.HexColor("#888888"))
    c.drawString(x, 0.6 * inch, "Generated automatically by BuildIQ / SitePulse")
    c.save()
    buf.seek(0)
    return buf.read()


def build_purchase_order_pdf(r, items):
    """One-page PDF summary of a placed purchase order."""
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    navy = colors.HexColor("#0B1220")
    gold = colors.HexColor("#D4A537")
    x = 0.75 * inch

    c.setFillColor(navy)
    c.rect(0, height - 1.1 * inch, width, 1.1 * inch, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(x, height - 0.65 * inch, "Purchase Order Confirmation")
    c.setFont("Helvetica", 10)
    c.drawString(x, height - 0.9 * inch, f"Darycet International  |  Order placed {friendly_date(date.today().isoformat())}")

    y = height - 1.5 * inch
    c.setFillColor(navy)

    def section(title):
        nonlocal y
        y -= 6
        c.setFillColor(gold)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x, y, title)
        c.setFillColor(navy)
        y -= 18

    def line(label, value):
        nonlocal y
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x, y, f"{label}:")
        c.setFont("Helvetica", 10)
        c.drawString(x + 1.6 * inch, y, str(value) if value else "\u2014")
        y -= 16

    section("Job")
    line("Job Name", r["job_name"])
    line("Location", r["location_description"])
    line("PR Number", r["pr_number"])
    line("Needed By", friendly_date(r["needed_on"]))

    section("Vendor")
    line("Company", r["vendor_company"])
    line("Phone", r["vendor_company_phone"])

    if items:
        section("Items")
        for it in items:
            desc = " \u2014 ".join(v for v in [it["item"], it["description"]] if v)
            qty = f" ({it['qty']}{' ' + it['unit'] if it['unit'] else ''})" if it["qty"] else ""
            y = _pdf_write_wrapped(c, f"\u2022 {desc}{qty}", x, y, width - 1.5 * inch, size=10)

    section("Ordered By")
    line("Name", r["ordered_by"])
    line("Date", friendly_date(r["ordered_date"]))

    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(colors.HexColor("#888888"))
    c.drawString(x, 0.6 * inch, "Generated automatically by BuildIQ / SitePulse")
    c.save()
    buf.seek(0)
    return buf.read()


@app.template_filter("friendly_dt")
def friendly_dt(iso_str):
    """'2026-08-18T19:36:00' (stored UTC) -> 'August 18, 2026 at 2:36 PM'
    (converted to Houston/Central time, DST-aware)."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str).replace(tzinfo=ZoneInfo("UTC")).astimezone(HOUSTON_TZ)
        return dt.strftime("%B %-d, %Y at %-I:%M %p")
    except ValueError:
        return iso_str


@app.template_filter("friendly_date")
def friendly_date(date_str):
    """'2026-08-28' -> '08/28/2026'. For plain date fields (no time
    component) -- pour dates, requested dates, ordered dates, etc.
    Leaves anything that doesn't parse as a bare YYYY-MM-DD unchanged,
    rather than erroring, since some callers may pass already-formatted
    or blank values."""
    if not date_str:
        return ""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return date_str


@app.template_filter("friendly_short_dt")
def friendly_short_dt(iso_str):
    """'2026-08-19T17:46:00' (stored UTC) -> '08/19/2026 5:46 PM' (Houston
    time). Same MM/DD/YYYY convention as friendly_date, just for the
    "Submitted ..." timestamp headers, which also carry a time -- kept
    separate from friendly_dt (which spells out the month name) so
    existing month-name displays elsewhere aren't affected."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str).replace(tzinfo=ZoneInfo("UTC")).astimezone(HOUSTON_TZ)
        return dt.strftime("%m/%d/%Y %-I:%M %p")
    except ValueError:
        return iso_str


def friendly_time(hhmm):
    """'07:00' -> '7:00 AM'. Used for WhatsApp notifications (not a
    Jinja filter -- those render in a template, this builds plain text
    strings), so every notification's time is formatted consistently
    with what's shown in the app itself."""
    if not hhmm:
        return ""
    try:
        return datetime.strptime(hhmm, "%H:%M").strftime("%-I:%M %p")
    except ValueError:
        return hhmm


DB_DIR = os.environ.get("DATA_DIR", ".")
DB_PATH = os.path.join(DB_DIR, "buildiq.db")
UPLOAD_DIR = os.path.join(DB_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "heic", "webp"}
MAX_PHOTO_SIZE_MB = 10

ADMIN_EMAILS = ["ayoub@darycet.com", "rebecca@darycet.com"]
# Domains allowed to sign up. Being on this list only grants basic access
# (Equipment Center / SitePulse) -- Project Hunt and admin tooling stay
# gated per-email below, never per-domain.
ALLOWED_SIGNUP_DOMAINS = ["@darycet.com", "@nomaengineering.com"]
# Extra individual emails allowed to sign up even though they're outside
# every allowed domain above.
EXTRA_ALLOWED_SIGNUP_EMAILS = set()
# Full access to every section, including Project Hunt -- named
# individuals only, never a whole domain.
FULL_ACCESS_EMAILS = {"ayoub@darycet.com", "rebecca@darycet.com", "marilu@darycet.com", "hghuneim@nomaengineering.com"}
# Atlas (voice assistant) access -- separate from Project Hunt so someone
# can get Atlas without also getting Project Hunt. Everyone in
# FULL_ACCESS_EMAILS gets it too, plus anyone listed here individually.
ATLAS_ACCESS_EMAILS = FULL_ACCESS_EMAILS | {"rebecca@nomaengineering.com"}
# Only these can actually place a concrete/material order -- everyone else
# can submit a request, but "Scheduled/Ordered" plus the vendor/contact
# details is procurement's call.
PROCUREMENT_EMAILS = {"ayoub@darycet.com", "rebecca@darycet.com", "marilu@darycet.com"}
# Who can manage the WhatsApp site-group routing -- narrower than full
# admin, but wider than just Ayoub.
WHATSAPP_ADMIN_EMAILS = {"ayoub@darycet.com", "rebecca@darycet.com"}


def is_project_hunt_allowed():
    return current_user.is_authenticated and current_user.email in FULL_ACCESS_EMAILS


def is_atlas_allowed():
    return current_user.is_authenticated and current_user.email in ATLAS_ACCESS_EMAILS


def is_procurement():
    return current_user.is_authenticated and current_user.email in PROCUREMENT_EMAILS


def is_whatsapp_admin():
    return current_user.is_authenticated and current_user.email in WHATSAPP_ADMIN_EMAILS

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def is_admin():
    return current_user.is_authenticated and current_user.email in ADMIN_EMAILS


@app.context_processor
def inject_permissions():
    return {
        "has_project_hunt_access": current_user.is_authenticated and current_user.email in FULL_ACCESS_EMAILS,
        "has_atlas_access": is_atlas_allowed(),
        "is_procurement": current_user.is_authenticated and current_user.email in PROCUREMENT_EMAILS,
        "is_admin_user": is_admin(),
    }


@app.before_request
def restrict_project_hunt():
    """Project Hunt (Bid Tracker) is limited to a specific list of people --
    everyone else gets Equipment Center and SitePulse only. Checked once
    here for every /tracker/* request rather than per-route, so a new route
    added later can't accidentally skip this."""
    if request.path.startswith("/tracker") and current_user.is_authenticated:
        if current_user.email not in FULL_ACCESS_EMAILS:
            flash("You don't have access to Project Hunt.", "error")
            return redirect(url_for("home"))


def save_photo(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_PHOTO_EXTENSIONS:
        flash(f"Photo not saved -- unsupported file type ({ext or 'unknown'}). Use JPG, PNG, HEIC, or WEBP.", "error")
        return None
    file_storage.seek(0, os.SEEK_END)
    size_mb = file_storage.tell() / (1024 * 1024)
    file_storage.seek(0)
    if size_mb > MAX_PHOTO_SIZE_MB:
        flash(f"Photo not saved -- file too large ({size_mb:.1f}MB, max {MAX_PHOTO_SIZE_MB}MB).", "error")
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, secure_filename(filename)))
    return filename


@app.route("/uploads/<filename>")
@login_required
def uploaded_photo(filename):
    return send_from_directory(UPLOAD_DIR, secure_filename(filename))


def log_activity(section, entity_type, entity_id, action, asset_id=None, field=None, old_value=None, new_value=None):
    db = get_db()
    user_email = current_user.email if current_user.is_authenticated else "system"
    db.execute(
        """INSERT INTO activity_log (section, asset_id, entity_type, entity_id, action, field,
           old_value, new_value, user_email, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (section, asset_id, entity_type, entity_id, action, field,
         str(old_value) if old_value is not None else None,
         str(new_value) if new_value is not None else None,
         user_email, datetime.utcnow().isoformat())
    )


def _apply_move(db, move, moved_by, is_auto=False):
    """Shared logic for completing a movement -- whether it's an immediate
    move being recorded right now, or a previously Scheduled one becoming
    active. Snapshots the asset's CURRENT status/hours onto the history
    row (never the value from whenever it was scheduled), moves the asset,
    and stamps who/when it actually happened.
    """
    asset = db.execute("SELECT name, status, hours_mileage FROM sitepulse_assets WHERE id = ?", (move["asset_id"],)).fetchone()
    now = datetime.utcnow().isoformat()
    mover_label = f"{moved_by} (auto, scheduled by {move['created_by']})" if is_auto and move["created_by"] else moved_by
    db.execute("UPDATE sitepulse_assets SET location=?, updated_at=? WHERE id=?",
               (move["to_location"], now, move["asset_id"]))
    db.execute(
        """UPDATE sitepulse_usage_log SET move_status='Applied', status_at_move=?, mileage_hours=?,
           moved_by=?, applied_at=?, out_date=? WHERE id=?""",
        (asset["status"], asset["hours_mileage"], mover_label, now, date.today().isoformat(), move["id"])
    )
    log_activity("sitepulse", "move", move["id"], "applied", asset_id=move["asset_id"],
                 field="location", old_value=move["from_location"], new_value=move["to_location"])
    send_whatsapp_group_message(
        f"📍 {asset['name']} moved{' (scheduled)' if move['scheduled_date'] else ''}\n"
        f"{move['from_location'] or '—'} → {move['to_location']}\n"
        f"Hours/Mileage: {asset['hours_mileage'] or '—'}\n"
        f"{'Auto-applied, scheduled by ' + move['created_by'] if is_auto and move['created_by'] else 'By: ' + moved_by}",
        chat_id=whatsapp_chat_id_for_site(move["to_location"], move["from_location"]) or ULTRAMSG_SITEPULSE_GROUP_CHAT_ID
    )


def apply_due_scheduled_moves(asset_id=None):
    """Scheduled location moves apply themselves once their date arrives --
    there's no cron here, so we check for anything due whenever an asset
    page (or the dashboard) loads and apply it right then. asset_id=None
    checks every asset (used on the dashboard); a specific id scopes it to
    one asset's page load.
    """
    db = get_db()
    today = date.today().isoformat()
    mover = current_user.name or current_user.email if current_user.is_authenticated else "System"
    if asset_id is not None:
        due = db.execute(
            "SELECT * FROM sitepulse_usage_log WHERE asset_id = ? AND entry_kind='move' AND move_status='Scheduled' AND scheduled_date <= ?",
            (asset_id, today)
        ).fetchall()
    else:
        due = db.execute(
            "SELECT * FROM sitepulse_usage_log WHERE entry_kind='move' AND move_status='Scheduled' AND scheduled_date <= ?",
            (today,)
        ).fetchall()
    for move in due:
        _apply_move(db, move, mover, is_auto=True)
    if due:
        db.commit()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section TEXT NOT NULL,
            asset_id INTEGER,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            action TEXT NOT NULL,
            field TEXT,
            old_value TEXT,
            new_value TEXT,
            user_email TEXT,
            created_at TEXT NOT NULL
        );

        -- Per-site WhatsApp group routing (item 16) -- e.g. anything
        -- mentioning "Peninsula" posts to the Peninsula group instead of
        -- the default SitePulse group. Managed from an admin page so new
        -- sites/groups can be added without a code change.
        CREATE TABLE IF NOT EXISTS whatsapp_site_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE,
            chat_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        -- SitePulse: equipment + rentals only. Concrete/materials moved to
        -- Site Inventory below -- this is the "trim SitePulse down" ask.
        CREATE TABLE IF NOT EXISTS sitepulse_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, description TEXT, year TEXT, serial_number TEXT,
            value TEXT, daily_rate TEXT, weekly_rate TEXT, monthly_rate TEXT,
            status TEXT DEFAULT 'Available', location TEXT, hours_mileage TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sitepulse_usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL,
            usage_type TEXT DEFAULT 'Internal Job', job_name TEXT, job_address TEXT,
            client TEXT, out_date TEXT, duration_unit TEXT, return_date TEXT, notes TEXT,
            photo_filename TEXT, created_at TEXT,
            entry_kind TEXT DEFAULT 'usage', from_location TEXT, to_location TEXT,
            mileage_hours TEXT, move_status TEXT DEFAULT 'Applied', scheduled_date TEXT,
            scheduled_time TEXT, move_reason TEXT, status_at_move TEXT, moved_by TEXT,
            applied_at TEXT, created_by TEXT,
            FOREIGN KEY (asset_id) REFERENCES sitepulse_assets (id)
        );
        CREATE TABLE IF NOT EXISTS sitepulse_maintenance_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL,
            entry_date TEXT, work_done TEXT, parts TEXT, hours_at_service TEXT,
            reported_by TEXT, resolved INTEGER DEFAULT 0, photo_filename TEXT, created_at TEXT,
            FOREIGN KEY (asset_id) REFERENCES sitepulse_assets (id)
        );
        CREATE TABLE IF NOT EXISTS sitepulse_mileage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL,
            reading_date TEXT NOT NULL, mileage TEXT NOT NULL, notes TEXT, created_at TEXT,
            FOREIGN KEY (asset_id) REFERENCES sitepulse_assets (id)
        );
        CREATE TABLE IF NOT EXISTS sitepulse_rentals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vendor TEXT NOT NULL,
            equipment_description TEXT NOT NULL, job_name TEXT, rate_amount TEXT,
            rate_period TEXT DEFAULT 'Daily', rented_date TEXT NOT NULL, due_date TEXT,
            returned_date TEXT, notes TEXT, created_at TEXT, updated_at TEXT
        );

        -- Site Inventory: concrete requests + material inventory, split out
        -- of SitePulse into their own section per today's direction.
        CREATE TABLE IF NOT EXISTS inventory_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_name TEXT NOT NULL,
            site TEXT NOT NULL, quantity TEXT, unit TEXT, shelf_location TEXT,
            notes TEXT, created_at TEXT, updated_at TEXT
        );

        -- Request Center + Product Intelligence. Employees submit a
        -- request (feature_requests); every status change is logged to
        -- feature_request_status_history, which is also what the employee
        -- sees as their timeline. feature_request_intelligence holds the
        -- admin-only fields (notes, solution, testing, feedback) in a
        -- genuinely separate table -- not hidden columns on the same
        -- table -- so no employee-facing query can ever touch it.
        CREATE TABLE IF NOT EXISTS feature_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_email TEXT NOT NULL,
            requester_name TEXT,
            department TEXT,
            original_request TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Submitted',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        -- Departments is its own table (not a hardcoded list in code) so
        -- new departments can be added from the admin UI as the company
        -- grows, with zero code changes or redesign required.
        CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feature_request_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature_request_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            release_note TEXT,
            changed_by TEXT,
            changed_at TEXT NOT NULL,
            FOREIGN KEY (feature_request_id) REFERENCES feature_requests(id)
        );
        CREATE TABLE IF NOT EXISTS feature_request_intelligence (
            feature_request_id INTEGER PRIMARY KEY,
            buildiq_module TEXT,
            internal_notes TEXT,
            solution_built TEXT,
            testing_notes TEXT,
            user_feedback TEXT,
            release_date TEXT,
            updated_at TEXT,
            FOREIGN KEY (feature_request_id) REFERENCES feature_requests(id)
        );
        CREATE TABLE IF NOT EXISTS feature_request_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature_request_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            uploaded_by TEXT,
            created_at TEXT,
            FOREIGN KEY (feature_request_id) REFERENCES feature_requests(id)
        );

        CREATE TABLE IF NOT EXISTS inventory_concrete_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
            job_site_address TEXT, area_description TEXT, pour_date TEXT NOT NULL,
            pour_time TEXT, mix_design_psi TEXT, mix_slump TEXT, concrete_amount TEXT,
            truck_spacing TEXT, pump_type TEXT, pump_size TEXT, pump_arrival_time TEXT,
            lab_required TEXT, lab_time TEXT, drilling_required TEXT, drilling_time TEXT,
            requested_by TEXT, requested_signature TEXT, requested_date TEXT,
            ordered_by TEXT, ordered_signature TEXT, ordered_date TEXT,
            concrete_company TEXT, concrete_company_phone TEXT,
            pump_company TEXT, pump_company_phone TEXT,
            lab_company TEXT, drilling_company TEXT, drilling_company_phone TEXT,
            status TEXT DEFAULT 'Submitted',
            created_at TEXT, updated_at TEXT
        );

        -- Purchase Request Form (DIL-24-CON-F-PR-1) -- same paper-replica
        -- approach as Concrete Requests, plus the same Submit/Scheduled/
        -- Completed/Delete workflow.
        CREATE TABLE IF NOT EXISTS inventory_purchase_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pr_number TEXT, request_date TEXT NOT NULL,
            job_name TEXT, location_description TEXT,
            requested_by TEXT, needed_on TEXT, source_of_supply TEXT,
            requestor_signature TEXT, requestor_date TEXT,
            ordered_by TEXT, ordered_date TEXT, vendor_company TEXT, vendor_company_phone TEXT,
            status TEXT DEFAULT 'Submitted',
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS inventory_purchase_request_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, purchase_request_id INTEGER NOT NULL,
            item TEXT, description TEXT, supplier TEXT, qty TEXT, unit TEXT,
            FOREIGN KEY (purchase_request_id) REFERENCES inventory_purchase_requests (id)
        );

        -- Bid Tracker: full port of Command Center's schema.
        CREATE TABLE IF NOT EXISTS tracker_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, client TEXT,
            address TEXT, bid_due_date TEXT, estimated_value TEXT,
            status TEXT DEFAULT 'In Progress', assigned_to TEXT, notes TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tracker_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
            trade TEXT NOT NULL, vendor_name TEXT, vendor_contact TEXT, vendor_email TEXT,
            vendor_phone TEXT, rfq_sent_date TEXT, status TEXT DEFAULT 'Not Sent',
            is_submit_blocking INTEGER DEFAULT 0, amount TEXT, notes TEXT,
            follow_up_email TEXT, rfq_email TEXT, attachment_filename TEXT,
            attachment_original_name TEXT, created_at TEXT, updated_at TEXT,
            FOREIGN KEY (project_id) REFERENCES tracker_projects (id)
        );
        CREATE TABLE IF NOT EXISTS tracker_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
            doc_name TEXT, doc_type TEXT, status TEXT DEFAULT 'Needed',
            notes TEXT, link TEXT, created_at TEXT,
            FOREIGN KEY (project_id) REFERENCES tracker_projects (id)
        );
        CREATE TABLE IF NOT EXISTS tracker_unit_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, item TEXT NOT NULL,
            unit TEXT, price TEXT, notes TEXT, updated_at TEXT
        );
    """)

    # Safe migration: adds the duration_unit column if this database
    # already existed before item 8 was built (e.g. real signups already
    # on Railway). CREATE TABLE IF NOT EXISTS above only handles brand-new
    # databases -- an existing table needs this explicit ALTER instead,
    # or every INSERT into sitepulse_usage_log fails with "no column
    # named duration_unit" once the code expects that column to exist.
    try:
        db.execute("ALTER TABLE sitepulse_usage_log ADD COLUMN duration_unit TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists -- nothing to do

    # Same situation for the "status" column on concrete/purchase requests --
    # added when Submit/Scheduled/Completed was built, after some databases
    # may already have existed without it.
    try:
        db.execute("ALTER TABLE inventory_concrete_requests ADD COLUMN status TEXT DEFAULT 'Submitted'")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE inventory_purchase_requests ADD COLUMN status TEXT DEFAULT 'Submitted'")
    except sqlite3.OperationalError:
        pass

    # Order-placement fields added for item 14 -- procurement records who
    # ordered, when, and the vendor/contact for each piece (concrete truck,
    # pump, lab, drilling; vendor for purchase requests). Same safe-migration
    # pattern as above for databases created before this existed.
    for column_sql in [
        "ALTER TABLE inventory_concrete_requests ADD COLUMN concrete_company TEXT",
        "ALTER TABLE inventory_concrete_requests ADD COLUMN concrete_company_phone TEXT",
        "ALTER TABLE inventory_concrete_requests ADD COLUMN pump_company TEXT",
        "ALTER TABLE inventory_concrete_requests ADD COLUMN pump_company_phone TEXT",
        "ALTER TABLE inventory_concrete_requests ADD COLUMN lab_company TEXT",
        "ALTER TABLE inventory_concrete_requests ADD COLUMN drilling_company TEXT",
        "ALTER TABLE inventory_concrete_requests ADD COLUMN drilling_company_phone TEXT",
        "ALTER TABLE inventory_purchase_requests ADD COLUMN ordered_by TEXT",
        "ALTER TABLE inventory_purchase_requests ADD COLUMN ordered_date TEXT",
        "ALTER TABLE inventory_purchase_requests ADD COLUMN vendor_company TEXT",
        "ALTER TABLE inventory_purchase_requests ADD COLUMN vendor_company_phone TEXT",
        "ALTER TABLE inventory_purchase_requests ADD COLUMN expected_delivery_date TEXT",
        # Location-move auto-tracking (item 15) -- Status & Location now
        # writes movement entries straight into the usage log, including
        # ones scheduled for a future date.
        "ALTER TABLE sitepulse_usage_log ADD COLUMN entry_kind TEXT DEFAULT 'usage'",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN from_location TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN to_location TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN mileage_hours TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN move_status TEXT DEFAULT 'Applied'",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN scheduled_date TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN scheduled_time TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN move_reason TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN status_at_move TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN moved_by TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN applied_at TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN created_by TEXT",
        "ALTER TABLE inventory_concrete_requests ADD COLUMN pump_type TEXT",
        "ALTER TABLE inventory_concrete_requests ADD COLUMN concrete_arrival_time TEXT",
        "ALTER TABLE inventory_concrete_requests ADD COLUMN full_reminder_sent_at TEXT",
        "ALTER TABLE users ADD COLUMN department TEXT",
        "ALTER TABLE inventory_purchase_request_items ADD COLUMN unit TEXT",
    ]:
        try:
            db.execute(column_sql)
        except sqlite3.OperationalError:
            pass

    # Belt-and-suspenders: create departments here too, as its own
    # standalone statement, in case it didn't take earlier (e.g. an
    # existing production DB that predates this table and whose
    # executescript run stopped short of it for any reason).
    try:
        db.execute(
            """CREATE TABLE IF NOT EXISTS departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )"""
        )
    except sqlite3.OperationalError:
        pass

    # Seed the initial department list once. After this, departments are
    # managed entirely from the admin UI (Users & Departments page) --
    # adding a new one is a row insert, not a code change.
    existing_dept_count = db.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
    if existing_dept_count == 0:
        now = datetime.utcnow().isoformat()
        for dept_name in ["Estimating", "Procurement", "Operations"]:
            db.execute("INSERT OR IGNORE INTO departments (name, created_at) VALUES (?, ?)", (dept_name, now))

    db.commit()
    db.close()


init_db()


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.name = row["name"]
        self.email = row["email"]


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User(row) if row else None


# ---------------------------------------------------------------------------
# Auth + Home
# ---------------------------------------------------------------------------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        if not any(email.endswith(d) for d in ALLOWED_SIGNUP_DOMAINS) and email not in EXTRA_ALLOWED_SIGNUP_EMAILS:
            flash(f"Sign up with your {' or '.join(ALLOWED_SIGNUP_DOMAINS)} email.", "error")
            return redirect(url_for("signup"))
        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            flash("An account with that email already exists.", "error")
            return redirect(url_for("signup"))
        db.execute(
            "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (request.form.get("name", ""), email, generate_password_hash(request.form["password"]),
             datetime.utcnow().isoformat())
        )
        db.commit()
        flash("Account created. Log in below.")
        return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row and check_password_hash(row["password_hash"], request.form["password"]):
            login_user(User(row))
            return redirect(url_for("home"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))


@app.route("/team")
@login_required
def team_list():
    if not is_admin():
        return redirect(url_for("home"))
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    return render_template("team.html", users=users)


@app.route("/team/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_team_member(user_id):
    if not is_admin():
        return redirect(url_for("home"))
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        flash("User not found.", "error")
        return redirect(url_for("team_list"))
    if target["email"] in ADMIN_EMAILS:
        flash("Can't remove an admin account this way.", "error")
        return redirect(url_for("team_list"))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Removed {target['email']} -- they can sign up again with the same email.")
    return redirect(url_for("team_list"))


@app.route("/whatsapp-groups")
@login_required
def whatsapp_site_groups_list():
    if not is_whatsapp_admin():
        return redirect(url_for("home"))
    db = get_db()
    groups = db.execute("SELECT * FROM whatsapp_site_groups ORDER BY keyword ASC").fetchall()
    return render_template("whatsapp_site_groups.html", groups=groups,
                            default_chat_id=ULTRAMSG_SITEPULSE_GROUP_CHAT_ID or ULTRAMSG_GROUP_CHAT_ID)


@app.route("/whatsapp-groups/new", methods=["POST"])
@login_required
def whatsapp_site_groups_new():
    if not is_whatsapp_admin():
        return redirect(url_for("home"))
    db = get_db()
    keyword = request.form.get("keyword", "").strip()
    chat_id = request.form.get("chat_id", "").strip()
    if not keyword or not chat_id:
        flash("Both a site keyword and a chat ID are required.", "error")
        return redirect(url_for("whatsapp_site_groups_list"))
    try:
        db.execute("INSERT INTO whatsapp_site_groups (keyword, chat_id, created_at) VALUES (?, ?, ?)",
                   (keyword, chat_id, datetime.utcnow().isoformat()))
        db.commit()
        flash(f'Anything mentioning "{keyword}" now routes to that group.')
    except sqlite3.IntegrityError:
        flash(f'"{keyword}" is already mapped to a group -- delete it first if you want to change it.', "error")
    return redirect(url_for("whatsapp_site_groups_list"))


@app.route("/whatsapp-groups/<int:group_id>/delete", methods=["POST"])
@login_required
def whatsapp_site_groups_delete(group_id):
    if not is_whatsapp_admin():
        return redirect(url_for("home"))
    db = get_db()
    db.execute("DELETE FROM whatsapp_site_groups WHERE id = ?", (group_id,))
    db.commit()
    flash("Removed -- that site's notifications will fall back to the default group.")
    return redirect(url_for("whatsapp_site_groups_list"))


@app.route("/whatsapp-groups/test", methods=["POST"])
@login_required
def whatsapp_site_groups_test():
    if not is_whatsapp_admin():
        return redirect(url_for("home"))
    chat_id = request.form.get("chat_id", "").strip()
    label = request.form.get("label", "").strip() or "this group"
    if not chat_id:
        flash("No chat ID given to test.", "error")
        return redirect(url_for("whatsapp_site_groups_list"))
    ok, detail = send_whatsapp_group_message(
        f"\U0001F9EA Test notification\n"
        f"This confirms the group is receiving BuildIQ alerts correctly.\n"
        f"Triggered by: {current_user.name or current_user.email}",
        chat_id=chat_id
    )
    if ok:
        flash(f"Test message sent to {label} -- check WhatsApp to confirm it landed.")
    else:
        flash(f"Couldn't send to {label}: {detail}", "error")
    return redirect(url_for("whatsapp_site_groups_list"))


@app.route("/")
def home():
    return render_template("home.html")


# ---------------------------------------------------------------------------
# SitePulse -- Equipment
# ---------------------------------------------------------------------------

SP_STATUS_OPTIONS = ["Available", "Out on Job", "In Maintenance", "Sold", "Stolen"]
SP_STATUS_BADGE = {
    "Available": "status-awarded", "Out on Job": "status-inprogress",
    "In Maintenance": "status-pending", "Sold": "status-submitted", "Stolen": "status-submitted",
}


@app.template_filter("sp_statusclass")
def sp_statusclass(status):
    return SP_STATUS_BADGE.get(status, "status-pending")


@app.route("/sitepulse/")
@login_required
def sitepulse_dashboard():
    apply_due_scheduled_moves()
    db = get_db()
    status_filter = request.args.get("status", "")
    location_filter = request.args.get("location", "")

    conditions = []
    params = []
    if status_filter:
        conditions.append("status = ?")
        params.append(status_filter)
    else:
        conditions.append("status NOT IN ('Sold', 'Stolen')")
    if location_filter:
        conditions.append("location = ?")
        params.append(location_filter)
    query = "SELECT * FROM sitepulse_assets WHERE " + " AND ".join(conditions) + " ORDER BY name ASC"
    asset_rows = db.execute(query, params).fetchall()

    available_count = db.execute("SELECT COUNT(*) as c FROM sitepulse_assets WHERE status = 'Available'").fetchone()["c"]
    out_count = db.execute("SELECT COUNT(*) as c FROM sitepulse_assets WHERE status = 'Out on Job'").fetchone()["c"]
    maint_count = db.execute("SELECT COUNT(*) as c FROM sitepulse_assets WHERE status = 'In Maintenance'").fetchone()["c"]

    today_str = date.today().isoformat()
    active_rentals_count = db.execute(
        "SELECT COUNT(*) as c FROM sitepulse_rentals WHERE returned_date IS NULL OR returned_date = ''"
    ).fetchone()["c"]
    overdue_rentals_count = db.execute(
        "SELECT COUNT(*) as c FROM sitepulse_rentals WHERE (returned_date IS NULL OR returned_date = '') "
        "AND due_date IS NOT NULL AND due_date != '' AND due_date < ?", (today_str,)
    ).fetchone()["c"]

    locations = [r["location"] for r in db.execute(
        "SELECT DISTINCT location FROM sitepulse_assets WHERE location IS NOT NULL AND location != '' ORDER BY location ASC"
    ).fetchall()]

    # Latest activity per asset -- same "fold onto the asset row" approach as
    # real SitePulse: most recent usage vs. most recent maintenance, whichever
    # is newer wins, shown right in the dashboard row instead of a separate feed.
    latest_usage = {}
    for r in db.execute("SELECT * FROM sitepulse_usage_log ORDER BY created_at DESC").fetchall():
        if r["asset_id"] not in latest_usage:
            latest_usage[r["asset_id"]] = r
    latest_maint = {}
    for r in db.execute("SELECT * FROM sitepulse_maintenance_log ORDER BY created_at DESC").fetchall():
        if r["asset_id"] not in latest_maint:
            latest_maint[r["asset_id"]] = r

    assets = []
    for a in asset_rows:
        a_dict = dict(a)
        u = latest_usage.get(a["id"])
        m = latest_maint.get(a["id"])
        chosen, chosen_type = None, None
        if u and m:
            chosen, chosen_type = (u, "Usage") if u["created_at"] >= m["created_at"] else (m, "Maintenance")
        elif u:
            chosen, chosen_type = u, "Usage"
        elif m:
            chosen, chosen_type = m, "Maintenance"
        if chosen_type == "Usage":
            a_dict["latest_activity"] = chosen["job_name"] or chosen["client"] or chosen["usage_type"]
            a_dict["latest_activity_date"] = chosen["out_date"] or chosen["created_at"][:10]
        elif chosen_type == "Maintenance":
            status_tag = "Resolved" if chosen["resolved"] else "Open"
            a_dict["latest_activity"] = f"{chosen['work_done']} ({status_tag})" if chosen["work_done"] else f"Maintenance ({status_tag})"
            a_dict["latest_activity_date"] = chosen["entry_date"] or chosen["created_at"][:10]
        else:
            a_dict["latest_activity"] = None
            a_dict["latest_activity_date"] = None
        a_dict["latest_activity_type"] = chosen_type
        assets.append(a_dict)

    return render_template("sitepulse/dashboard.html", assets=assets, status_options=SP_STATUS_OPTIONS,
                            available_count=available_count, out_count=out_count, maint_count=maint_count,
                            active_rentals_count=active_rentals_count, overdue_rentals_count=overdue_rentals_count,
                            current_filter=status_filter, current_location=location_filter, locations=locations)


@app.route("/sitepulse/asset/new", methods=["GET", "POST"])
@login_required
def sitepulse_new_asset():
    if request.method == "POST":
        db = get_db()
        now = datetime.utcnow().isoformat()
        cur = db.execute(
            """INSERT INTO sitepulse_assets (name, description, year, serial_number, value, daily_rate,
               weekly_rate, monthly_rate, status, location, hours_mileage, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request.form["name"], request.form.get("description", ""), request.form.get("year", ""),
             request.form.get("serial_number", ""), request.form.get("value", ""),
             request.form.get("daily_rate", ""), request.form.get("weekly_rate", ""),
             request.form.get("monthly_rate", ""), "Available", request.form.get("location", ""),
             request.form.get("hours_mileage", ""), now, now)
        )
        log_activity("sitepulse", "asset", cur.lastrowid, "created", new_value=request.form["name"])
        db.commit()
        flash("Equipment added.")
        return redirect(url_for("sitepulse_dashboard"))
    return render_template("sitepulse/new_asset.html")


@app.route("/sitepulse/asset/<int:asset_id>")
@login_required
def sitepulse_view_asset(asset_id):
    apply_due_scheduled_moves(asset_id)
    db = get_db()
    a = db.execute("SELECT * FROM sitepulse_assets WHERE id = ?", (asset_id,)).fetchone()
    if not a:
        flash("Asset not found.", "error")
        return redirect(url_for("sitepulse_dashboard"))
    usage = db.execute("SELECT * FROM sitepulse_usage_log WHERE asset_id = ? AND move_status != 'Scheduled' ORDER BY COALESCE(applied_at, out_date, created_at) DESC", (asset_id,)).fetchall()
    scheduled_moves = db.execute(
        "SELECT * FROM sitepulse_usage_log WHERE asset_id = ? AND entry_kind='move' AND move_status='Scheduled' ORDER BY scheduled_date ASC",
        (asset_id,)
    ).fetchall()
    maintenance = db.execute("SELECT * FROM sitepulse_maintenance_log WHERE asset_id = ? ORDER BY entry_date DESC", (asset_id,)).fetchall()

    mileage_entries = db.execute(
        "SELECT * FROM sitepulse_mileage_log WHERE asset_id = ? ORDER BY reading_date DESC", (asset_id,)
    ).fetchall()
    # Group readings by month (YYYY-MM), keeping the highest reading seen
    # in each month -- that's the odometer's value at that point, which is
    # what "mileage this month" means in practice (a running total, not
    # separate trip counts).
    monthly_totals = {}
    for m in mileage_entries:
        try:
            month_key = m["reading_date"][:7]
            reading_num = float(m["mileage"])
            if month_key not in monthly_totals or reading_num > monthly_totals[month_key]:
                monthly_totals[month_key] = reading_num
        except (ValueError, TypeError):
            pass
    monthly_totals_sorted = sorted(monthly_totals.items(), reverse=True)

    return render_template("sitepulse/asset.html", a=a, usage=usage, maintenance=maintenance,
                            scheduled_moves=scheduled_moves,
                            mileage_entries=mileage_entries, monthly_totals=monthly_totals_sorted,
                            status_options=SP_STATUS_OPTIONS, usage_type_options=["Internal Job", "External Rental"],
                            today=date.today().isoformat())


@app.route("/sitepulse/asset/<int:asset_id>/edit-details", methods=["POST"])
@login_required
def sitepulse_edit_asset_details(asset_id):
    db = get_db()
    db.execute(
        """UPDATE sitepulse_assets SET name=?, description=?, year=?, serial_number=?, value=?,
           daily_rate=?, weekly_rate=?, monthly_rate=?, updated_at=? WHERE id=?""",
        (request.form["name"], request.form.get("description", ""), request.form.get("year", ""),
         request.form.get("serial_number", ""), request.form.get("value", ""),
         request.form.get("daily_rate", ""), request.form.get("weekly_rate", ""),
         request.form.get("monthly_rate", ""), datetime.utcnow().isoformat(), asset_id)
    )
    log_activity("sitepulse", "asset", asset_id, "updated", asset_id=asset_id, field="details", new_value=request.form["name"])
    db.commit()
    flash("Asset details updated.")
    return redirect(url_for("sitepulse_view_asset", asset_id=asset_id))


@app.route("/sitepulse/asset/<int:asset_id>/update", methods=["POST"])
@login_required
def sitepulse_update_asset(asset_id):
    db = get_db()
    old = db.execute("SELECT name, status, location FROM sitepulse_assets WHERE id = ?", (asset_id,)).fetchone()
    asset_name = old["name"]
    new_status = request.form["status"]
    new_location = request.form.get("location", "").strip()
    hours_mileage = request.form.get("hours_mileage", "")
    schedule_date = request.form.get("schedule_date", "").strip()
    schedule_time = request.form.get("schedule_time", "").strip()
    move_reason = request.form.get("move_reason", "").strip()
    now = datetime.utcnow().isoformat()
    today = date.today().isoformat()
    old_location = old["location"] or ""
    mover = current_user.name or current_user.email

    location_changed = new_location != old_location and new_location != ""
    # A date in the future schedules the move for later. A date that's
    # today or in the past backdates an already-happened move to that
    # actual date, instead of always stamping it with today. Blank means
    # "happening right now" -- unchanged from before.
    effective_date = schedule_date or today

    if location_changed and schedule_date and schedule_date > today:
        # Future move: don't touch the asset's location yet -- park it as a
        # Scheduled entry in the usage log. apply_due_scheduled_moves()
        # (or manually completing it) fills in the actual status/hours/mover
        # snapshot once it really happens, not at scheduling time.
        db.execute("UPDATE sitepulse_assets SET status=?, hours_mileage=?, updated_at=? WHERE id=?",
                   (new_status, hours_mileage, now, asset_id))
        cur = db.execute(
            """INSERT INTO sitepulse_usage_log (asset_id, entry_kind, from_location, to_location,
               move_status, scheduled_date, scheduled_time, move_reason, created_by, created_at)
               VALUES (?, 'move', ?, ?, 'Scheduled', ?, ?, ?, ?, ?)""",
            (asset_id, old_location, new_location, schedule_date, schedule_time, move_reason, mover, now)
        )
        log_activity("sitepulse", "move", cur.lastrowid, "scheduled", asset_id=asset_id,
                     field="location", old_value=old_location, new_value=new_location)
        db.commit()
        send_whatsapp_group_message(
            f"📅 Move scheduled: {asset_name}\n"
            f"{old_location or '—'} → {new_location}\n"
            f"Date: {schedule_date}{' at ' + schedule_time if schedule_time else ''}\n"
            + (f"Reason: {move_reason}\n" if move_reason else "")
            + f"Scheduled by: {mover}",
            chat_id=whatsapp_chat_id_for_site(new_location, old_location) or ULTRAMSG_SITEPULSE_GROUP_CHAT_ID
        )
        flash(f"Move to {new_location} scheduled for {schedule_date}.")
        return redirect(url_for("sitepulse_view_asset", asset_id=asset_id))

    db.execute("UPDATE sitepulse_assets SET status=?, location=?, hours_mileage=?, updated_at=? WHERE id=?",
               (new_status, new_location, hours_mileage, now, asset_id))

    if location_changed:
        cur = db.execute(
            """INSERT INTO sitepulse_usage_log (asset_id, entry_kind, from_location, to_location,
               mileage_hours, move_status, status_at_move, moved_by, scheduled_date, applied_at,
               out_date, created_by, created_at)
               VALUES (?, 'move', ?, ?, ?, 'Applied', ?, ?, ?, ?, ?, ?, ?)""",
            (asset_id, old_location, new_location, hours_mileage, new_status, mover, effective_date, now, effective_date, mover, now)
        )
        log_activity("sitepulse", "move", cur.lastrowid, "created", asset_id=asset_id,
                     field="location", old_value=old_location, new_value=new_location)
        send_whatsapp_group_message(
            f"📍 {asset_name} moved\n"
            f"{old_location or '—'} → {new_location}\n"
            f"Hours/Mileage: {hours_mileage or '—'}\n"
            f"By: {mover}",
            chat_id=whatsapp_chat_id_for_site(new_location, old_location) or ULTRAMSG_SITEPULSE_GROUP_CHAT_ID
        )

    if old["status"] != new_status:
        log_activity("sitepulse", "asset", asset_id, "updated", field="status", old_value=old["status"], new_value=new_status)
        send_whatsapp_group_message(
            f"🔧 {asset_name} status changed\n"
            f"{old['status']} → {new_status}\n"
            f"By: {mover}",
            chat_id=whatsapp_chat_id_for_site(new_location, old_location) or ULTRAMSG_SITEPULSE_GROUP_CHAT_ID
        )
    db.commit()
    flash("Asset updated.")
    return redirect(url_for("sitepulse_view_asset", asset_id=asset_id))


@app.route("/sitepulse/move/<int:move_id>/complete", methods=["POST"])
@login_required
def sitepulse_complete_scheduled_move(move_id):
    db = get_db()
    move = db.execute("SELECT * FROM sitepulse_usage_log WHERE id = ? AND entry_kind='move'", (move_id,)).fetchone()
    if not move:
        flash("Scheduled move not found.", "error")
        return redirect(url_for("sitepulse_dashboard"))
    if move["move_status"] != "Scheduled":
        flash("That move has already been applied.", "error")
        return redirect(url_for("sitepulse_view_asset", asset_id=move["asset_id"]))
    _apply_move(db, move, current_user.name or current_user.email, is_auto=False)
    db.commit()
    flash(f"Marked moved to {move['to_location']}.")
    return redirect(url_for("sitepulse_view_asset", asset_id=move["asset_id"]))


@app.route("/sitepulse/move/<int:move_id>/cancel", methods=["POST"])
@login_required
def sitepulse_cancel_scheduled_move(move_id):
    db = get_db()
    move = db.execute("SELECT * FROM sitepulse_usage_log WHERE id = ? AND entry_kind='move'", (move_id,)).fetchone()
    if not move:
        flash("Scheduled move not found.", "error")
        return redirect(url_for("sitepulse_dashboard"))
    if move["move_status"] != "Scheduled":
        flash("That move has already been applied.", "error")
        return redirect(url_for("sitepulse_view_asset", asset_id=move["asset_id"]))
    db.execute("DELETE FROM sitepulse_usage_log WHERE id = ?", (move_id,))
    log_activity("sitepulse", "move", move_id, "cancelled", asset_id=move["asset_id"],
                 field="location", old_value=move["from_location"], new_value=move["to_location"])
    db.commit()
    flash("Scheduled move cancelled.")
    return redirect(url_for("sitepulse_view_asset", asset_id=move["asset_id"]))


@app.route("/sitepulse/asset/<int:asset_id>/status", methods=["POST"])
@login_required
def sitepulse_quick_status(asset_id):
    db = get_db()
    new_status = request.form["status"]
    old = db.execute("SELECT status FROM sitepulse_assets WHERE id = ?", (asset_id,)).fetchone()["status"]
    db.execute("UPDATE sitepulse_assets SET status=?, updated_at=? WHERE id=?", (new_status, datetime.utcnow().isoformat(), asset_id))
    log_activity("sitepulse", "asset", asset_id, "updated", field="status", old_value=old, new_value=new_status)
    db.commit()
    return redirect(request.referrer or url_for("sitepulse_dashboard"))


@app.route("/sitepulse/asset/<int:asset_id>/usage/new", methods=["POST"])
@login_required
def sitepulse_new_usage(asset_id):
    db = get_db()
    now = datetime.utcnow().isoformat()
    photo_filename = save_photo(request.files.get("photo"))
    cur = db.execute(
        """INSERT INTO sitepulse_usage_log (asset_id, usage_type, job_name, job_address, client,
           out_date, duration_unit, return_date, notes, photo_filename, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (asset_id, request.form.get("usage_type", "Internal Job"), request.form.get("job_name", ""),
         request.form.get("job_address", ""), request.form.get("client", ""), request.form.get("out_date", ""),
         request.form.get("duration_unit", ""), request.form.get("return_date", ""), request.form.get("notes", ""),
         photo_filename, now)
    )
    db.execute("UPDATE sitepulse_assets SET status='Out on Job', updated_at=? WHERE id=?", (now, asset_id))
    log_activity("sitepulse", "usage", cur.lastrowid, "created", asset_id=asset_id, new_value=request.form.get("job_name", ""))
    db.commit()
    flash("Usage logged, asset marked Out on Job.")
    return redirect(url_for("sitepulse_view_asset", asset_id=asset_id))


@app.route("/sitepulse/usage/<int:usage_id>/update", methods=["POST"])
@login_required
def sitepulse_update_usage(usage_id):
    db = get_db()
    entry = db.execute("SELECT * FROM sitepulse_usage_log WHERE id = ?", (usage_id,)).fetchone()
    if not entry:
        flash("Usage entry not found.", "error")
        return redirect(url_for("sitepulse_dashboard"))
    return_date = request.form.get("return_date", "")
    new_photo = save_photo(request.files.get("photo"))
    photo_filename = new_photo if new_photo else entry["photo_filename"]
    db.execute(
        """UPDATE sitepulse_usage_log SET usage_type=?, job_name=?, job_address=?, client=?, out_date=?,
           duration_unit=?, return_date=?, notes=?, photo_filename=? WHERE id=?""",
        (request.form.get("usage_type", "Internal Job"), request.form.get("job_name", ""),
         request.form.get("job_address", ""), request.form.get("client", ""), request.form.get("out_date", ""),
         request.form.get("duration_unit", ""), return_date, request.form.get("notes", ""), photo_filename, usage_id)
    )
    if return_date:
        db.execute("UPDATE sitepulse_assets SET status='Available', updated_at=? WHERE id=?",
                   (datetime.utcnow().isoformat(), entry["asset_id"]))
    log_activity("sitepulse", "usage", usage_id, "updated", asset_id=entry["asset_id"], new_value=request.form.get("job_name", ""))
    db.commit()
    flash("Usage entry updated.")
    return redirect(url_for("sitepulse_view_asset", asset_id=entry["asset_id"]))


@app.route("/sitepulse/usage/<int:usage_id>/delete", methods=["POST"])
@login_required
def sitepulse_delete_usage(usage_id):
    db = get_db()
    entry = db.execute("SELECT * FROM sitepulse_usage_log WHERE id = ?", (usage_id,)).fetchone()
    if not entry:
        flash("Usage entry not found.", "error")
        return redirect(url_for("sitepulse_dashboard"))
    asset_id = entry["asset_id"]
    db.execute("DELETE FROM sitepulse_usage_log WHERE id = ?", (usage_id,))
    log_activity("sitepulse", "usage", usage_id, "deleted", asset_id=asset_id, old_value=entry["job_name"])
    db.commit()
    flash("Usage entry deleted.")
    return redirect(url_for("sitepulse_view_asset", asset_id=asset_id))


@app.route("/sitepulse/asset/<int:asset_id>/maintenance/new", methods=["POST"])
@login_required
def sitepulse_new_maintenance(asset_id):
    db = get_db()
    now = datetime.utcnow().isoformat()
    photo_filename = save_photo(request.files.get("photo"))
    cur = db.execute(
        """INSERT INTO sitepulse_maintenance_log (asset_id, entry_date, work_done, parts, hours_at_service,
           reported_by, resolved, photo_filename, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (asset_id, request.form.get("entry_date", date.today().isoformat()), request.form.get("work_done", ""),
         request.form.get("parts", ""), request.form.get("hours_at_service", ""),
         current_user.name or current_user.email, 1 if request.form.get("resolved") else 0, photo_filename, now)
    )
    log_activity("sitepulse", "maintenance", cur.lastrowid, "created", asset_id=asset_id, new_value=request.form.get("work_done", ""))
    db.commit()
    asset = db.execute("SELECT name, location FROM sitepulse_assets WHERE id = ?", (asset_id,)).fetchone()
    send_whatsapp_group_message(
        f"🛠️ Maintenance logged: {asset['name']}\n"
        f"Issue: {request.form.get('work_done', '') or '—'}\n"
        + (f"Parts: {request.form.get('parts')}\n" if request.form.get('parts') else "")
        + f"Reported by: {current_user.name or current_user.email}",
        chat_id=whatsapp_chat_id_for_site(asset["location"]) or ULTRAMSG_SITEPULSE_GROUP_CHAT_ID
    )
    flash("Maintenance entry logged.")
    return redirect(url_for("sitepulse_view_asset", asset_id=asset_id))


@app.route("/sitepulse/asset/<int:asset_id>/mileage/new", methods=["POST"])
@login_required
def sitepulse_new_mileage(asset_id):
    db = get_db()
    now = datetime.utcnow().isoformat()
    db.execute(
        """INSERT INTO sitepulse_mileage_log (asset_id, reading_date, mileage, notes, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (asset_id, request.form.get("reading_date", date.today().isoformat()),
         request.form.get("mileage", ""), request.form.get("notes", ""), now)
    )
    log_activity("sitepulse", "mileage", asset_id, "created", asset_id=asset_id,
                 new_value=request.form.get("mileage", ""))
    db.commit()
    flash("Mileage reading logged.")
    return redirect(url_for("sitepulse_view_asset", asset_id=asset_id))


@app.route("/sitepulse/maintenance/<int:entry_id>/update", methods=["POST"])
@login_required
def sitepulse_update_maintenance(entry_id):
    db = get_db()
    entry = db.execute("SELECT * FROM sitepulse_maintenance_log WHERE id = ?", (entry_id,)).fetchone()
    if not entry:
        flash("Maintenance entry not found.", "error")
        return redirect(url_for("sitepulse_dashboard"))
    new_photo = save_photo(request.files.get("photo"))
    photo_filename = new_photo if new_photo else entry["photo_filename"]
    db.execute(
        """UPDATE sitepulse_maintenance_log SET entry_date=?, work_done=?, parts=?, hours_at_service=?,
           resolved=?, photo_filename=? WHERE id=?""",
        (request.form.get("entry_date", ""), request.form.get("work_done", ""), request.form.get("parts", ""),
         request.form.get("hours_at_service", ""), 1 if request.form.get("resolved") else 0, photo_filename, entry_id)
    )
    log_activity("sitepulse", "maintenance", entry_id, "updated", asset_id=entry["asset_id"], new_value=request.form.get("work_done", ""))
    db.commit()
    flash("Maintenance entry updated.")
    return redirect(url_for("sitepulse_view_asset", asset_id=entry["asset_id"]))


@app.route("/sitepulse/maintenance/<int:entry_id>/delete", methods=["POST"])
@login_required
def sitepulse_delete_maintenance(entry_id):
    db = get_db()
    entry = db.execute("SELECT * FROM sitepulse_maintenance_log WHERE id = ?", (entry_id,)).fetchone()
    if not entry:
        flash("Maintenance entry not found.", "error")
        return redirect(url_for("sitepulse_dashboard"))
    asset_id = entry["asset_id"]
    db.execute("DELETE FROM sitepulse_maintenance_log WHERE id = ?", (entry_id,))
    log_activity("sitepulse", "maintenance", entry_id, "deleted", asset_id=asset_id, old_value=entry["work_done"])
    db.commit()
    flash("Maintenance entry deleted.")
    return redirect(url_for("sitepulse_view_asset", asset_id=asset_id))


@app.route("/sitepulse/activity")
@login_required
def sitepulse_activity_log():
    if not is_admin():
        flash("Not authorized.", "error")
        return redirect(url_for("sitepulse_dashboard"))
    db = get_db()
    entries = db.execute(
        """SELECT a.*, ast.name AS asset_name FROM activity_log a
           LEFT JOIN sitepulse_assets ast ON a.asset_id = ast.id
           WHERE a.section = 'sitepulse' ORDER BY a.created_at DESC LIMIT 300"""
    ).fetchall()
    return render_template("sitepulse/activity_log.html", entries=entries)


@app.route("/sitepulse/asset/<int:asset_id>/activity")
@login_required
def sitepulse_asset_activity_log(asset_id):
    if not is_admin():
        flash("Not authorized.", "error")
        return redirect(url_for("sitepulse_dashboard"))
    db = get_db()
    asset = db.execute("SELECT * FROM sitepulse_assets WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        flash("Asset not found.", "error")
        return redirect(url_for("sitepulse_dashboard"))
    entries = db.execute(
        "SELECT * FROM activity_log WHERE section='sitepulse' AND asset_id = ? ORDER BY created_at DESC", (asset_id,)
    ).fetchall()
    return render_template("sitepulse/activity_log.html", entries=entries, asset=asset)


@app.route("/sitepulse/geocode")
@login_required
def sitepulse_geocode():
    address = request.args.get("address", "").strip()
    if not address:
        return {"lat": None, "lon": None}
    try:
        resp = requests.get(
            "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
            params={"address": address, "benchmark": "2020", "format": "json"}, timeout=8)
        matches = resp.json().get("result", {}).get("addressMatches", [])
        if not matches:
            return {"lat": None, "lon": None}
        coords = matches[0]["coordinates"]
        return {"lat": coords["y"], "lon": coords["x"]}
    except Exception:
        return {"lat": None, "lon": None}


# ---- Rentals ----

@app.route("/sitepulse/rentals")
@login_required
def sitepulse_rentals_list():
    db = get_db()
    today_str = date.today().isoformat()
    show = request.args.get("show", "active")
    condition = {"returned": "returned_date IS NOT NULL AND returned_date != ''",
                 "all": "1=1"}.get(show, "returned_date IS NULL OR returned_date = ''")
    rows = db.execute(f"SELECT * FROM sitepulse_rentals WHERE {condition} ORDER BY due_date ASC, rented_date DESC").fetchall()

    rentals = []
    for r in rows:
        rd = dict(r)
        if rd["returned_date"]:
            rd["rental_status"] = "Returned"
        elif rd["due_date"] and rd["due_date"] < today_str:
            rd["rental_status"] = "Overdue"
        else:
            rd["rental_status"] = "Active"
        end = rd["returned_date"] or today_str
        try:
            days = max((date.fromisoformat(end) - date.fromisoformat(rd["rented_date"])).days + 1, 1)
            rate = float(rd["rate_amount"] or 0)
            daily_equiv = {"Weekly": rate / 7, "Monthly": rate / 30}.get(rd["rate_period"], rate)
            rd["running_cost"] = round(days * daily_equiv, 2)
            rd["days_out"] = days
        except (ValueError, TypeError):
            rd["running_cost"] = None
            rd["days_out"] = None
        rentals.append(rd)

    total_cost = round(sum(r["running_cost"] for r in rentals if r["running_cost"]), 2)
    return render_template("sitepulse/rentals.html", rentals=rentals, show=show, total_running_cost=total_cost)


@app.route("/sitepulse/rentals/new", methods=["GET", "POST"])
@login_required
def sitepulse_new_rental():
    if request.method == "POST":
        db = get_db()
        now = datetime.utcnow().isoformat()
        cur = db.execute(
            """INSERT INTO sitepulse_rentals (vendor, equipment_description, job_name, rate_amount,
               rate_period, rented_date, due_date, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request.form["vendor"], request.form["equipment_description"], request.form.get("job_name", ""),
             request.form.get("rate_amount", ""), request.form.get("rate_period", "Daily"),
             request.form["rented_date"], request.form.get("due_date", ""), request.form.get("notes", ""), now, now)
        )
        log_activity("sitepulse", "rental", cur.lastrowid, "created", new_value=request.form["equipment_description"])
        db.commit()
        flash("Rental logged.")
        return redirect(url_for("sitepulse_rentals_list"))
    return render_template("sitepulse/new_rental.html", today=date.today().isoformat())


@app.route("/sitepulse/rentals/<int:rental_id>/update", methods=["POST"])
@login_required
def sitepulse_update_rental(rental_id):
    db = get_db()
    db.execute(
        """UPDATE sitepulse_rentals SET vendor=?, equipment_description=?, job_name=?, rate_amount=?,
           rate_period=?, rented_date=?, due_date=?, notes=?, updated_at=? WHERE id=?""",
        (request.form["vendor"], request.form["equipment_description"], request.form.get("job_name", ""),
         request.form.get("rate_amount", ""), request.form.get("rate_period", "Daily"),
         request.form["rented_date"], request.form.get("due_date", ""), request.form.get("notes", ""),
         datetime.utcnow().isoformat(), rental_id)
    )
    log_activity("sitepulse", "rental", rental_id, "updated", new_value=request.form["equipment_description"])
    db.commit()
    flash("Rental updated.")
    return redirect(url_for("sitepulse_rentals_list"))


@app.route("/sitepulse/rentals/<int:rental_id>/return", methods=["POST"])
@login_required
def sitepulse_return_rental(rental_id):
    db = get_db()
    returned_date = request.form.get("returned_date") or date.today().isoformat()
    db.execute("UPDATE sitepulse_rentals SET returned_date=?, updated_at=? WHERE id=?",
               (returned_date, datetime.utcnow().isoformat(), rental_id))
    log_activity("sitepulse", "rental", rental_id, "returned", field="returned_date", new_value=returned_date)
    db.commit()
    flash("Rental marked returned.")
    return redirect(url_for("sitepulse_rentals_list"))


@app.route("/sitepulse/rentals/<int:rental_id>/delete", methods=["POST"])
@login_required
def sitepulse_delete_rental(rental_id):
    db = get_db()
    r = db.execute("SELECT * FROM sitepulse_rentals WHERE id = ?", (rental_id,)).fetchone()
    db.execute("DELETE FROM sitepulse_rentals WHERE id = ?", (rental_id,))
    log_activity("sitepulse", "rental", rental_id, "deleted", old_value=r["equipment_description"] if r else None)
    db.commit()
    flash("Rental deleted.")
    return redirect(url_for("sitepulse_rentals_list"))


# ---------------------------------------------------------------------------
# Site Inventory -- Concrete Requests + Materials
# ---------------------------------------------------------------------------

@app.route("/inventory/")
@login_required
def inventory_home():
    return render_template("inventory/home.html")


@app.route("/inventory/materials")
@login_required
def inventory_materials_list():
    db = get_db()
    search = request.args.get("q", "").strip()
    if search:
        like = f"%{search}%"
        rows = db.execute(
            "SELECT * FROM inventory_materials WHERE item_name LIKE ? OR site LIKE ? OR shelf_location LIKE ? "
            "ORDER BY site, item_name",
            (like, like, like)).fetchall()
    else:
        rows = db.execute("SELECT * FROM inventory_materials ORDER BY site, item_name").fetchall()
    return render_template("inventory/materials.html", materials=rows, search=search)


@app.route("/inventory/materials/new", methods=["GET", "POST"])
@login_required
def inventory_new_material():
    if request.method == "POST":
        db = get_db()
        now = datetime.utcnow().isoformat()
        cur = db.execute(
            """INSERT INTO inventory_materials (item_name, site, quantity, unit, shelf_location, notes,
               created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (request.form["item_name"], request.form["site"], request.form.get("quantity", ""),
             request.form.get("unit", ""), request.form.get("shelf_location", ""),
             request.form.get("notes", ""), now, now)
        )
        log_activity("inventory", "material", cur.lastrowid, "created", new_value=request.form["item_name"])
        db.commit()
        flash("Material added.")
        return redirect(url_for("inventory_materials_list"))
    db = get_db()
    sites = db.execute("SELECT DISTINCT site FROM inventory_materials ORDER BY site").fetchall()
    return render_template("inventory/new_material.html", sites=[s["site"] for s in sites])


@app.route("/inventory/materials/<int:material_id>/delete", methods=["POST"])
@login_required
def inventory_delete_material(material_id):
    if not is_admin():
        flash("Not authorized.", "error")
        return redirect(url_for("inventory_materials_list"))
    db = get_db()
    m = db.execute("SELECT * FROM inventory_materials WHERE id = ?", (material_id,)).fetchone()
    db.execute("DELETE FROM inventory_materials WHERE id = ?", (material_id,))
    log_activity("inventory", "material", material_id, "deleted", old_value=m["item_name"] if m else None)
    db.commit()
    flash("Material deleted.")
    return redirect(url_for("inventory_materials_list"))


def send_due_concrete_reminders():
    """The day before a scheduled pour, send the full order details (the
    same notification the old immediate-on-schedule message used to be).
    No cron here -- same pattern as apply_due_scheduled_moves: check for
    anything due whenever the concrete list page loads, and send it once.
    """
    db = get_db()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    due = db.execute(
        "SELECT * FROM inventory_concrete_requests WHERE status = 'Scheduled' AND pour_date = ? "
        "AND (full_reminder_sent_at IS NULL OR full_reminder_sent_at = '')",
        (tomorrow,)
    ).fetchall()
    for r in due:
        order_chat_id = whatsapp_chat_id_for_site(r["project"], r["job_site_address"])
        send_whatsapp_group_message(
            "📋 Tomorrow's concrete order:\n\n" + build_concrete_order_notification(r),
            chat_id=order_chat_id
        )
        db.execute(
            "UPDATE inventory_concrete_requests SET full_reminder_sent_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), r["id"])
        )
    if due:
        db.commit()


@app.route("/inventory/concrete")
@login_required
def inventory_concrete_list():
    send_due_concrete_reminders()
    db = get_db()

    project_filter = request.args.get("project", "")
    status_filter = request.args.get("status", "")
    pour_date_filter = request.args.get("pour_date", "")

    conditions, params = [], []
    if project_filter:
        conditions.append("project = ?")
        params.append(project_filter)
    if status_filter:
        conditions.append("status = ?")
        params.append(status_filter)
    if pour_date_filter:
        conditions.append("pour_date = ?")
        params.append(pour_date_filter)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    rows = db.execute(f"SELECT * FROM inventory_concrete_requests {where} ORDER BY pour_date DESC", params).fetchall()
    projects = [p["project"] for p in db.execute(
        "SELECT DISTINCT project FROM inventory_concrete_requests WHERE project IS NOT NULL AND project != '' ORDER BY project"
    ).fetchall()]

    return render_template(
        "inventory/concrete_requests.html", requests=rows, projects=projects,
        status_options=CONCRETE_STATUS_OPTIONS, project_filter=project_filter,
        status_filter=status_filter, pour_date_filter=pour_date_filter
    )


def create_concrete_request(fields, requested_by):
    """Shared insert+notify logic for a new concrete request -- used by
    both the web form and the voice assistant, so both paths behave
    identically (same notification, same defaults, same activity log).
    `fields` is a dict of form-field-name -> value; missing keys default
    the same way request.form.get(..., "") would.
    """
    db = get_db()
    now = datetime.utcnow().isoformat()

    def f(key, default=""):
        return fields.get(key) or default

    # Defense in depth: even if the client-side toggle didn't clear it
    # (JS disabled, browser autofill restoring a stale value after the
    # page loaded), never actually store a pump size/time for a request
    # that doesn't have a real pump selected.
    pump_size = f("pump_size") if f("pump_type") in ("Ground Pump", "Overhead Pump") else ""
    pump_arrival_time = f("pump_arrival_time") if f("pump_type") in ("Ground Pump", "Overhead Pump") else ""
    drilling_time = f("drilling_time") if f("drilling_required") == "Yes" else ""

    cur = db.execute(
        """INSERT INTO inventory_concrete_requests (project, job_site_address, area_description,
           pour_date, pour_time, mix_design_psi, mix_slump, concrete_amount, truck_spacing,
           pump_type, pump_size, pump_arrival_time, lab_required, lab_time, drilling_required, drilling_time,
           requested_by, requested_signature, requested_date, ordered_by, ordered_signature,
           ordered_date, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f("project"), f("job_site_address"), f("area_description"), f("pour_date"), f("pour_time"),
         f("mix_design_psi"), f("mix_slump"), f("concrete_amount"), f("truck_spacing"),
         f("pump_type"), pump_size, pump_arrival_time, f("lab_required", "No"), f("lab_time"),
         f("drilling_required", "No"), drilling_time, requested_by, f("requested_signature"),
         f("requested_date"), f("ordered_by"), f("ordered_signature"), f("ordered_date"),
         "Submitted", now, now)
    )
    log_activity("inventory", "concrete_request", cur.lastrowid, "created", new_value=f("project"))
    db.commit()
    send_whatsapp_group_message(
        f"🧱 New concrete request submitted\n"
        f"Project: {f('project') or '—'}\n"
        f"Pour date: {friendly_date(f('pour_date'))}{' at ' + friendly_time(f('pour_time')) if f('pour_time') else ''}\n"
        f"Amount: {f('concrete_amount') or '—'}\n"
        f"Requested by: {requested_by}",
        chat_id=whatsapp_chat_id_for_site(f("project"), f("job_site_address"))
    )
    return cur.lastrowid


CONCRETE_REQUEST_REQUIRED_FIELDS = [
    "project", "job_site_address", "area_description", "pour_date", "pour_time",
    "mix_design_psi", "mix_slump", "concrete_amount", "truck_spacing",
    "pump_type", "pump_size", "pump_arrival_time", "lab_required", "lab_time",
    "drilling_required", "drilling_time", "requested_date",
]


@app.route("/inventory/concrete/new", methods=["GET", "POST"])
@login_required
def inventory_new_concrete():
    if request.method == "POST":
        needs_pump = request.form.get("pump_type") in ("Ground Pump", "Overhead Pump")
        needs_drilling = request.form.get("drilling_required") == "Yes"
        required_fields = [
            f for f in CONCRETE_REQUEST_REQUIRED_FIELDS
            if (needs_pump or f not in ("pump_size", "pump_arrival_time"))
            and (needs_drilling or f != "drilling_time")
        ]
        missing = [f for f in required_fields if not request.form.get(f, "").strip()]
        if missing:
            flash("Please fill in every field on the form before submitting.", "error")
            return render_template("inventory/new_concrete_request.html", today=date.today().isoformat(), form=request.form)
        create_concrete_request(request.form.to_dict(), current_user.name or current_user.email)
        flash("Concrete request submitted.")
        return redirect(url_for("inventory_concrete_list"))
    return render_template("inventory/new_concrete_request.html", today=date.today().isoformat())


def _time_minus_hours(hhmm, hours):
    """'08:00' minus 1 hour -> '07:00'. Returns '' if hhmm is blank/unparseable."""
    if not hhmm:
        return ""
    try:
        t = datetime.strptime(hhmm, "%H:%M")
        t -= timedelta(hours=hours)
        return t.strftime("%H:%M")
    except ValueError:
        return ""


def build_concrete_order_notification(r):
    """Build the plain-text order notification in the format procurement
    texts/emails out once an order is placed -- e.g.:
    'Concrete scheduled tomorrow (2nd road pour) 07/21/2026 at 8:00 AM'
    """
    if not r["pour_date"]:
        return ""
    try:
        pour_dt = datetime.strptime(r["pour_date"], "%Y-%m-%d").date()
        date_display = pour_dt.strftime("%m/%d/%Y")
        delta = (pour_dt - date.today()).days
        when = "today" if delta == 0 else ("tomorrow" if delta == 1 else pour_dt.strftime("%A"))
    except ValueError:
        date_display = r["pour_date"]
        when = ""

    def fmt_time(t):
        if not t:
            return None
        try:
            return datetime.strptime(t, "%H:%M").strftime("%-I:%M %p")
        except ValueError:
            return t

    pour_time = fmt_time(r["pour_time"])
    lines = []
    header = f"Concrete scheduled {when}".strip()
    if r["area_description"]:
        header += f" ({r['area_description']})"
    header += f" {date_display}"
    if pour_time:
        header += f" at {pour_time}"
    lines.append(header)

    amount_line = " ".join(x for x in [r["concrete_amount"], f"plus {r['mix_design_psi']} PSI" if r["mix_design_psi"] else ""] if x)
    if amount_line:
        lines.append(amount_line)

    if r["pump_company"] or r["pump_size"]:
        pump_time = fmt_time(r["pump_arrival_time"])
        pump_line = r["pump_type"] if r["pump_type"] else "Pump"
        if r["pump_company"]:
            pump_line += f"-{r['pump_company']}"
        if r["pump_company_phone"]:
            pump_line += f" #{r['pump_company_phone']}"
        if pump_time:
            pump_line += f" @{pump_time}"
        lines.append(pump_line)

    if r["concrete_company"]:
        concrete_time = fmt_time(r["concrete_arrival_time"]) or pour_time
        concrete_line = r["concrete_company"]
        if concrete_time:
            concrete_line += f" @{concrete_time}"
        if r["concrete_company_phone"]:
            concrete_line += f" #{r['concrete_company_phone']}"
        lines.append(concrete_line)

    if r["lab_required"] == "Yes" and (r["lab_company"] or r["lab_time"]):
        lab_time = fmt_time(r["lab_time"])
        lab_line = r["lab_company"] or "Lab"
        if lab_time:
            lab_line += f" at {lab_time}"
        lines.append(lab_line)

    if r["drilling_required"] == "Yes" and (r["drilling_company"] or r["drilling_time"]):
        drill_time = fmt_time(r["drilling_time"])
        drill_line = r["drilling_company"] or "Drilling company"
        if r["drilling_company_phone"]:
            drill_line += f" #{r['drilling_company_phone']}"
        if drill_time:
            drill_line += f" at {drill_time}"
        lines.append(drill_line)

    return "\n\n".join(lines)


@app.route("/inventory/concrete/<int:request_id>")
@login_required
def inventory_view_concrete(request_id):
    db = get_db()
    r = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_concrete_list"))
    notification = build_concrete_order_notification(r) if r["ordered_by"] else None
    return render_template("inventory/concrete_request_detail.html", r=r, notification=notification)


@app.route("/inventory/concrete/<int:request_id>/edit", methods=["GET", "POST"])
@login_required
def inventory_edit_concrete(request_id):
    db = get_db()
    r = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_concrete_list"))
    if request.method == "POST":
        # Same defense-in-depth as create: never store a pump size/time
        # unless a real pump is actually selected.
        pump_type_val = request.form.get("pump_type", "")
        needs_pump = pump_type_val in ("Ground Pump", "Overhead Pump")
        pump_size_val = request.form.get("pump_size", "") if needs_pump else ""
        pump_arrival_val = request.form.get("pump_arrival_time", "") if needs_pump else ""
        drilling_required_val = request.form.get("drilling_required", "No")
        drilling_time_val = request.form.get("drilling_time", "") if drilling_required_val == "Yes" else ""
        db.execute(
            """UPDATE inventory_concrete_requests SET project=?, job_site_address=?, area_description=?,
               pour_date=?, pour_time=?, mix_design_psi=?, mix_slump=?, concrete_amount=?, truck_spacing=?,
               pump_type=?, pump_size=?, pump_arrival_time=?, lab_required=?, lab_time=?, drilling_required=?,
               drilling_time=?, updated_at=?
               WHERE id=?""",
            (request.form.get("project", ""), request.form.get("job_site_address", ""),
             request.form.get("area_description", ""), request.form["pour_date"], request.form.get("pour_time", ""),
             request.form.get("mix_design_psi", ""), request.form.get("mix_slump", ""),
             request.form.get("concrete_amount", ""), request.form.get("truck_spacing", ""),
             pump_type_val, pump_size_val, pump_arrival_val,
             request.form.get("lab_required", "No"), request.form.get("lab_time", ""),
             drilling_required_val, drilling_time_val,
             datetime.utcnow().isoformat(), request_id)
        )
        log_activity("inventory", "concrete_request", request_id, "updated", new_value=request.form.get("project", ""))
        db.commit()
        flash("Concrete request updated.")
        return redirect(url_for("inventory_view_concrete", request_id=request_id))
    return render_template("inventory/edit_concrete_request.html", r=r, today=date.today().isoformat())


@app.route("/inventory/concrete/<int:request_id>/order", methods=["GET", "POST"])
@login_required
def inventory_place_concrete_order(request_id):
    if not is_procurement():
        flash("Only procurement (Ayoub, Rebecca, or Marilu) can place a concrete order.", "error")
        return redirect(url_for("inventory_view_concrete", request_id=request_id))
    db = get_db()
    r = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_concrete_list"))
    if request.method == "POST":
        now = datetime.utcnow().isoformat()
        db.execute(
            """UPDATE inventory_concrete_requests SET
               concrete_company=?, concrete_company_phone=?, concrete_arrival_time=?, pump_company=?, pump_company_phone=?,
               pump_arrival_time=?, lab_company=?, lab_time=?, drilling_company=?, drilling_company_phone=?,
               drilling_time=?, ordered_by=?, ordered_date=?, status='Scheduled', updated_at=?
               WHERE id=?""",
            (request.form.get("concrete_company", ""), request.form.get("concrete_company_phone", ""),
             request.form.get("concrete_arrival_time", ""), request.form.get("pump_company", ""), request.form.get("pump_company_phone", ""),
             request.form.get("pump_arrival_time", ""), request.form.get("lab_company", ""),
             request.form.get("lab_time", ""), request.form.get("drilling_company", ""),
             request.form.get("drilling_company_phone", ""), request.form.get("drilling_time", ""),
             current_user.name or current_user.email, date.today().isoformat(), now, request_id)
        )
        log_activity("inventory", "concrete_request", request_id, "updated", field="status",
                     old_value=r["status"], new_value="Scheduled")
        db.commit()
        updated_r = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (request_id,)).fetchone()
        order_chat_id = whatsapp_chat_id_for_site(updated_r["project"], updated_r["job_site_address"])
        pour_date_display = friendly_date(updated_r["pour_date"])
        pour_time_display = " at " + friendly_time(updated_r["pour_time"]) if updated_r["pour_time"] else ""
        send_whatsapp_group_message(
            f"Concrete Scheduled for {pour_date_display}{pour_time_display}",
            chat_id=order_chat_id
        )
        flash("Order placed and marked Scheduled.")
        return redirect(url_for("inventory_view_concrete", request_id=request_id))
    return render_template(
        "inventory/place_concrete_order.html", r=r, today=date.today().isoformat(),
        default_pump_arrival=_time_minus_hours(r["pour_time"], 1) if not r["pump_arrival_time"] else "",
        default_drilling_time=_time_minus_hours(r["pour_time"], 1) if not r["drilling_time"] else "",
        default_lab_time=r["pour_time"] if not r["lab_time"] else "",
    )


@app.route("/inventory/concrete/<int:request_id>/status", methods=["POST"])
@login_required
def inventory_update_concrete_status(request_id):
    db = get_db()
    r = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_concrete_list"))
    new_status = request.form["status"]
    if new_status == "Scheduled" and not is_procurement():
        flash("Only procurement can mark a concrete request Scheduled -- use Place Order.", "error")
        return redirect(url_for("inventory_view_concrete", request_id=request_id))
    db.execute("UPDATE inventory_concrete_requests SET status = ?, updated_at = ? WHERE id = ?",
               (new_status, datetime.utcnow().isoformat(), request_id))
    log_activity("inventory", "concrete_request", request_id, "updated", field="status",
                 old_value=r["status"], new_value=new_status)
    db.commit()
    flash(f"Marked as {new_status}.")
    return redirect(url_for("inventory_view_concrete", request_id=request_id))


@app.route("/inventory/concrete/<int:request_id>/delete", methods=["POST"])
@login_required
def inventory_delete_concrete(request_id):
    db = get_db()
    r = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_concrete_list"))
    db.execute("DELETE FROM inventory_concrete_requests WHERE id = ?", (request_id,))
    log_activity("inventory", "concrete_request", request_id, "deleted", old_value=r["project"])
    db.commit()
    flash("Concrete request deleted.")
    return redirect(url_for("inventory_concrete_list"))


@app.route("/inventory/purchase")
@login_required
def inventory_purchase_list():
    db = get_db()
    rows = db.execute("SELECT * FROM inventory_purchase_requests ORDER BY request_date DESC").fetchall()
    return render_template("inventory/purchase_requests.html", requests=rows)


def _generate_pr_number(db):
    """Auto-generate a purchase request number in the same MMDDYYYY
    format already used historically, with a -2/-3 suffix if more than
    one request happens to land on the same day."""
    base = date.today().strftime("%m%d%Y")
    existing = {
        row["pr_number"] for row in db.execute(
            "SELECT pr_number FROM inventory_purchase_requests WHERE pr_number LIKE ?", (f"{base}%",)
        ).fetchall()
    }
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


@app.route("/inventory/purchase/new", methods=["GET", "POST"])
@login_required
def inventory_new_purchase():
    if request.method == "POST":
        db = get_db()
        now = datetime.utcnow().isoformat()
        requestor_display = current_user.name or current_user.email

        cur = db.execute(
            """INSERT INTO inventory_purchase_requests (pr_number, request_date, job_name,
               location_description, requested_by, needed_on, source_of_supply,
               requestor_signature, requestor_date, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_generate_pr_number(db), request.form["request_date"], request.form.get("job_name", ""),
             request.form.get("location_description", ""), requestor_display,
             request.form.get("needed_on", ""), request.form.get("source_of_supply", ""),
             request.form.get("requestor_signature", ""), request.form.get("requestor_date", ""),
             "Submitted", now, now)
        )
        pr_id = cur.lastrowid
        items = request.form.getlist("item[]")
        descriptions = request.form.getlist("description[]")
        suppliers = request.form.getlist("supplier[]")
        qtys = request.form.getlist("qty[]")
        units = request.form.getlist("unit[]")
        for item, desc, sup, qty, unit in zip(items, descriptions, suppliers, qtys, units):
            if item.strip() or desc.strip():
                db.execute(
                    "INSERT INTO inventory_purchase_request_items (purchase_request_id, item, description, supplier, qty, unit) VALUES (?,?,?,?,?,?)",
                    (pr_id, item, desc, sup, qty, unit)
                )
        log_activity("inventory", "purchase_request", pr_id, "created", new_value=request.form.get("job_name", ""))
        db.commit()

        # If a procurement person is the one actually logged in and
        # submitting, skip the submission notification -- the group only
        # needs to hear about it once the order is actually placed, which
        # already sends its own notification further down the flow.
        if current_user.email not in PROCUREMENT_EMAILS:
            item_summary = "; ".join(i.strip() for i in items if i.strip())[:200]
            send_whatsapp_group_message(
                f"🛒 New purchase request submitted\n"
                f"Job: {request.form.get('job_name', '') or '—'}\n"
                f"Needed by: {friendly_date(request.form.get('needed_on', '')) or '—'}\n"
                f"Items: {item_summary or '—'}\n"
                f"Requested by: {requestor_display}",
                chat_id=whatsapp_chat_id_for_site(request.form.get("job_name", ""), request.form.get("location_description", ""))
            )
        flash("Purchase request submitted.")
        return redirect(url_for("inventory_purchase_list"))

    return render_template("inventory/new_purchase_request.html", today=date.today().isoformat())


@app.route("/inventory/purchase/<int:request_id>")
@login_required
def inventory_view_purchase(request_id):
    db = get_db()
    r = db.execute("SELECT * FROM inventory_purchase_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_purchase_list"))
    items = db.execute("SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id = ?", (request_id,)).fetchall()
    return render_template("inventory/purchase_request_detail.html", r=r, items=items)


@app.route("/inventory/activity")
@login_required
def inventory_activity_log():
    if not is_admin():
        flash("Not authorized.", "error")
        return redirect(url_for("inventory_materials_list"))
    db = get_db()
    entries = db.execute(
        "SELECT * FROM activity_log WHERE section = 'inventory' ORDER BY created_at DESC LIMIT 300"
    ).fetchall()
    return render_template("inventory/activity_log.html", entries=entries)


@app.route("/inventory/concrete/<int:request_id>/activity")
@login_required
def inventory_concrete_activity_log(request_id):
    if not is_admin():
        flash("Not authorized.", "error")
        return redirect(url_for("inventory_concrete_list"))
    db = get_db()
    r = db.execute("SELECT * FROM inventory_concrete_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_concrete_list"))
    entries = db.execute(
        "SELECT * FROM activity_log WHERE section='inventory' AND entity_type='concrete_request' AND entity_id = ? ORDER BY created_at DESC",
        (request_id,)
    ).fetchall()
    return render_template("inventory/activity_log.html", entries=entries, record_name=r["project"])


@app.route("/inventory/purchase/<int:request_id>/activity")
@login_required
def inventory_purchase_activity_log(request_id):
    if not is_admin():
        flash("Not authorized.", "error")
        return redirect(url_for("inventory_purchase_list"))
    db = get_db()
    r = db.execute("SELECT * FROM inventory_purchase_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_purchase_list"))
    entries = db.execute(
        "SELECT * FROM activity_log WHERE section='inventory' AND entity_type='purchase_request' AND entity_id = ? ORDER BY created_at DESC",
        (request_id,)
    ).fetchall()
    return render_template("inventory/activity_log.html", entries=entries, record_name=r["job_name"])


@app.route("/inventory/purchase/<int:request_id>/edit", methods=["GET", "POST"])
@login_required
def inventory_edit_purchase(request_id):
    db = get_db()
    r = db.execute("SELECT * FROM inventory_purchase_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_purchase_list"))
    if request.method == "POST":
        db.execute(
            """UPDATE inventory_purchase_requests SET pr_number=?, request_date=?, job_name=?,
               location_description=?, needed_on=?, source_of_supply=?, updated_at=? WHERE id=?""",
            (request.form.get("pr_number", ""), request.form["request_date"], request.form.get("job_name", ""),
             request.form.get("location_description", ""), request.form.get("needed_on", ""),
             request.form.get("source_of_supply", ""), datetime.utcnow().isoformat(), request_id)
        )
        # Items: simplest reliable approach is replace-all -- delete the old
        # lines and insert whatever's on the form now, same as the create route.
        db.execute("DELETE FROM inventory_purchase_request_items WHERE purchase_request_id = ?", (request_id,))
        items = request.form.getlist("item[]")
        descriptions = request.form.getlist("description[]")
        suppliers = request.form.getlist("supplier[]")
        qtys = request.form.getlist("qty[]")
        units = request.form.getlist("unit[]")
        for item, desc, sup, qty, unit in zip(items, descriptions, suppliers, qtys, units):
            if item.strip() or desc.strip():
                db.execute(
                    "INSERT INTO inventory_purchase_request_items (purchase_request_id, item, description, supplier, qty, unit) VALUES (?,?,?,?,?,?)",
                    (request_id, item, desc, sup, qty, unit)
                )
        log_activity("inventory", "purchase_request", request_id, "updated", new_value=request.form.get("job_name", ""))
        db.commit()
        flash("Purchase request updated.")
        return redirect(url_for("inventory_view_purchase", request_id=request_id))
    existing_items = db.execute("SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id = ?", (request_id,)).fetchall()
    return render_template("inventory/edit_purchase_request.html", r=r, items=existing_items)


@app.route("/inventory/purchase/<int:request_id>/status", methods=["POST"])
@login_required
def inventory_update_purchase_status(request_id):
    db = get_db()
    r = db.execute("SELECT * FROM inventory_purchase_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_purchase_list"))
    new_status = request.form["status"]
    if new_status == "Scheduled" and not is_procurement():
        flash("Only procurement can mark a purchase request Scheduled -- use Place Order.", "error")
        return redirect(url_for("inventory_view_purchase", request_id=request_id))
    db.execute("UPDATE inventory_purchase_requests SET status = ?, updated_at = ? WHERE id = ?",
               (new_status, datetime.utcnow().isoformat(), request_id))
    log_activity("inventory", "purchase_request", request_id, "updated", field="status",
                 old_value=r["status"], new_value=new_status)
    db.commit()
    flash(f"Marked as {new_status}.")
    return redirect(url_for("inventory_view_purchase", request_id=request_id))


@app.route("/inventory/purchase/<int:request_id>/order", methods=["GET", "POST"])
@login_required
def inventory_place_purchase_order(request_id):
    if not is_procurement():
        flash("Only procurement (Ayoub, Rebecca, or Marilu) can place this order.", "error")
        return redirect(url_for("inventory_view_purchase", request_id=request_id))
    db = get_db()
    r = db.execute("SELECT * FROM inventory_purchase_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_purchase_list"))
    if request.method == "POST":
        db.execute(
            """UPDATE inventory_purchase_requests SET vendor_company=?, vendor_company_phone=?,
               ordered_by=?, ordered_date=?, expected_delivery_date=?, status='Scheduled', updated_at=? WHERE id=?""",
            (request.form.get("vendor_company", ""), request.form.get("vendor_company_phone", ""),
             current_user.name or current_user.email, date.today().isoformat(),
             request.form.get("expected_delivery_date", ""),
             datetime.utcnow().isoformat(), request_id)
        )
        log_activity("inventory", "purchase_request", request_id, "updated", field="status",
                     old_value=r["status"], new_value="Scheduled")
        db.commit()
        updated_r = db.execute("SELECT * FROM inventory_purchase_requests WHERE id = ?", (request_id,)).fetchone()
        order_chat_id = whatsapp_chat_id_for_site(updated_r["job_name"], updated_r["location_description"])
        send_whatsapp_group_message(
            f"✅ Purchase order placed by {current_user.name or current_user.email}\n"
            f"Job: {updated_r['job_name'] or '—'}\n"
            f"Vendor: {updated_r['vendor_company'] or '—'}"
            + (f" #{updated_r['vendor_company_phone']}" if updated_r['vendor_company_phone'] else ""),
            chat_id=order_chat_id
        )
        try:
            items = db.execute(
                "SELECT * FROM inventory_purchase_request_items WHERE purchase_request_id = ?", (request_id,)
            ).fetchall()
            pdf_bytes = build_purchase_order_pdf(updated_r, items)
            send_whatsapp_document(
                pdf_bytes, f"Purchase_Order_{request_id}.pdf",
                chat_id=order_chat_id, caption="Purchase order details"
            )
        except Exception as e:
            print(f"[whatsapp] failed to build/send purchase order PDF: {e}")
        flash("Order placed and marked Scheduled.")
        return redirect(url_for("inventory_view_purchase", request_id=request_id))
    return render_template("inventory/place_purchase_order.html", r=r, today=date.today().isoformat())


@app.route("/inventory/purchase/<int:request_id>/delete", methods=["POST"])
@login_required
def inventory_delete_purchase(request_id):
    db = get_db()
    r = db.execute("SELECT * FROM inventory_purchase_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("inventory_purchase_list"))
    db.execute("DELETE FROM inventory_purchase_request_items WHERE purchase_request_id = ?", (request_id,))
    db.execute("DELETE FROM inventory_purchase_requests WHERE id = ?", (request_id,))
    log_activity("inventory", "purchase_request", request_id, "deleted", old_value=r["job_name"])
    db.commit()
    flash("Purchase request deleted.")
    return redirect(url_for("inventory_purchase_list"))


# ---------------------------------------------------------------------------
# Bid Tracker -- Projects + Quotes (core of Command Center)
# ---------------------------------------------------------------------------

TR_STATUS_OPTIONS = ["In Progress", "Submitted", "Awarded", "Unmeant", "On Hold", "Cancelled", "Pending", "Archived"]
TR_STATUS_BADGE_CLASS = {
    "In Progress": "status-inprogress", "Submitted": "status-submitted",
    "Awarded": "status-awarded", "Unmeant": "status-lost",
    "On Hold": "status-onhold", "Cancelled": "status-cancelled", "Pending": "status-pending",
    "Archived": "status-cancelled",
}


@app.template_filter("markdown")
def tr_markdown_filter(text):
    if not text:
        return ""
    return md_lib.markdown(text, extensions=["extra"])


@app.template_filter("statusclass")
def tr_statusclass_filter(status):
    return TR_STATUS_BADGE_CLASS.get(status, "status-pending")


@app.template_filter("daysleft")
def tr_daysleft_filter(due_date_str):
    if not due_date_str:
        return None
    try:
        due = datetime.strptime(due_date_str, "%Y-%m-%d").date()
        return (due - date.today()).days
    except ValueError:
        return None


def tr_format_currency(value):
    if not value:
        return value
    value = value.strip()
    if not value:
        return value
    cleaned = value.replace("$", "").replace(",", "").strip()
    try:
        num = float(cleaned)
        if "." in cleaned:
            return "${:,.2f}".format(num)
        return "${:,.0f}".format(num)
    except ValueError:
        return value


def tr_format_phone(value):
    if not value:
        return value
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:11]}"
    return value


def gather_business_snapshot():
    """Pull a compact, current snapshot of the business for the voice
    assistant to answer questions against -- equipment status, open
    requests, and active bids. Kept short on purpose: this gets stuffed
    into every assistant prompt, so it's a summary, not a full export.
    """
    db = get_db()
    lines = []

    assets = db.execute("SELECT name, status, location FROM sitepulse_assets ORDER BY name").fetchall()
    status_counts = {}
    for a in assets:
        status_counts[a["status"]] = status_counts.get(a["status"], 0) + 1
    lines.append("EQUIPMENT (" + ", ".join(f"{v} {k}" for k, v in status_counts.items()) + f", {len(assets)} total):")
    for a in assets:
        if a["status"] != "Available":
            lines.append(f"  - {a['name']}: {a['status']}" + (f" @ {a['location']}" if a["location"] else ""))

    open_concrete = db.execute(
        "SELECT project, pour_date, status FROM inventory_concrete_requests WHERE status != 'Completed' ORDER BY pour_date"
    ).fetchall()
    lines.append(f"\nCONCRETE REQUESTS (open, {len(open_concrete)}):")
    for r in open_concrete[:15]:
        lines.append(f"  - {r['project'] or 'Untitled'}: {r['status']}, pour {r['pour_date'] or 'TBD'}")

    open_purchase = db.execute(
        "SELECT job_name, needed_on, status FROM inventory_purchase_requests WHERE status != 'Completed' ORDER BY needed_on"
    ).fetchall()
    lines.append(f"\nPURCHASE REQUESTS (open, {len(open_purchase)}):")
    for r in open_purchase[:15]:
        lines.append(f"  - {r['job_name'] or 'Untitled'}: {r['status']}, needed {r['needed_on'] or 'TBD'}")

    try:
        projects = db.execute(
            "SELECT name, client, status, bid_due_date FROM tracker_projects WHERE status != 'Lost' AND status != 'Archived' ORDER BY bid_due_date"
        ).fetchall()
        lines.append(f"\nBID TRACKER (active, {len(projects)}):")
        for p in projects[:15]:
            lines.append(f"  - {p['name']}" + (f" ({p['client']})" if p["client"] else "") + f": {p['status']}, due {p['bid_due_date'] or 'TBD'}")
    except sqlite3.OperationalError:
        pass

    return "\n".join(lines)


CONCRETE_REQUEST_FIELDS = """
- project (text, REQUIRED) -- the job/project name
- job_site_address (text, REQUIRED) -- delivery address
- area_description (text, REQUIRED) -- what area/scope is being poured
- pour_date (date, REQUIRED) -- format YYYY-MM-DD
- pour_time (time, REQUIRED) -- format HH:MM 24-hour
- mix_design_psi (text, REQUIRED) -- e.g. "4000"
- mix_slump (text, REQUIRED) -- e.g. "4 inch"
- concrete_amount (text, REQUIRED) -- e.g. "130 yds"
- truck_spacing (text, REQUIRED) -- spacing between trucks, e.g. "15 min"
- pump_type (REQUIRED) -- one of: None, Ground Pump, Overhead Pump
- pump_size (text, REQUIRED only if pump_type is Ground Pump or Overhead Pump -- skip asking if pump_type is None)
- pump_arrival_time (time, REQUIRED only if pump_type is Ground Pump or Overhead Pump -- skip asking if pump_type is None) -- format HH:MM 24-hour
- lab_required (Yes/No, REQUIRED)
- lab_time (time, REQUIRED) -- format HH:MM 24-hour, even if lab_required is No
- drilling_required (Yes/No, REQUIRED)
- drilling_time (time, REQUIRED) -- format HH:MM 24-hour, even if drilling_required is No
""".strip()

# The one field on the web form NOT asked about by voice -- "requested_date"
# is the date the request itself was made, which is filled in automatically
# with today's date at submission time. Asking someone "what's today's
# date" out loud would be a strange thing for Atlas to ask.
VOICE_REQUIRED_FIELDS = [f for f in CONCRETE_REQUEST_REQUIRED_FIELDS if f != "requested_date"]


def _parse_assistant_reply(raw_text):
    """Split Claude's raw response into the spoken part and the trailing
    <state>...</state> JSON block. Falls back gracefully if the model
    didn't include a state block (treats it as a plain chat answer)."""
    import re
    match = re.search(r"<state>(.*?)</state>", raw_text, re.DOTALL)
    if not match:
        return raw_text.strip(), {"mode": "chat", "fields": {}, "action": "none"}
    spoken = raw_text[:match.start()].strip()
    try:
        state = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        state = {"mode": "chat", "fields": {}, "action": "none"}
    return spoken, state


ATLAS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")  # No hardcoded fallback -- unset means "use the free browser voice," not "use some baked-in voice."


def generate_atlas_speech(text):
    """Generate speech audio for the assistant's reply via ElevenLabs,
    using the "Atlas" voice. Returns (base64_audio_or_None, error_or_None).
    Callers fall back to the browser's free built-in voice when audio is
    None, but the error string is still surfaced to the page so failures
    are visible instead of silently swallowed.

    If no ELEVENLABS_VOICE_ID is configured, this skips calling ElevenLabs
    entirely (not just falls back after a failed call) -- so leaving the
    voice ID unset costs zero ElevenLabs credits for the talking side,
    not just zero dollars.
    """
    if not ATLAS_VOICE_ID:
        return None, None
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return None, "ELEVENLABS_VOICE_ID is set but ELEVENLABS_API_KEY is not."
    if not text:
        return None, None
    import urllib.request
    import urllib.error
    import base64
    body = json.dumps({
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ATLAS_VOICE_ID}",
        data=body,
        headers={"Content-Type": "application/json", "xi-api-key": api_key, "Accept": "audio/mpeg"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            audio_bytes = resp.read()
            return base64.b64encode(audio_bytes).decode("ascii"), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        return None, f"ElevenLabs error {e.code}: {detail}"
    except (urllib.error.URLError, TimeoutError) as e:
        return None, f"ElevenLabs connection error: {str(e)}"


def call_claude_assistant_turn(user_text, draft):
    """One turn of the voice assistant. `draft` is the session's running
    state: {"mode": "chat"|"concrete_request", "fields": {...}, "history": [...]}.
    Returns (spoken_reply, updated_draft, submitted_id_or_None).

    The model is given the field spec for a concrete request and asked to
    hold a normal conversation, asking only for whatever's still missing,
    then to emit a small JSON "state" block alongside its spoken reply so
    the server knows what it decided -- without that, there'd be no
    reliable way to tell "still collecting info" from "ready to submit"
    apart from re-parsing free text every turn.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "This assistant isn't set up yet -- ask Ayoub to add an Anthropic API key.", draft, None

    snapshot = gather_business_snapshot()
    history = draft.get("history", [])
    history_text = "\n".join(f"{h['role']}: {h['text']}" for h in history[-10:])

    system = (
        "You are Atlas, the voice assistant inside BuildIQ. If asked your "
        "name, say Atlas. "
        "People talk to you hands-free, often on a job site, instead of "
        "filling out a form. You'll be read out loud by text-to-speech --  "
        "keep replies short and conversational, no markdown, no lists "
        "unless truly needed.\n\n"
        "You can do two things:\n"
        "1. Answer questions about the current state of the business using "
        "the snapshot below.\n"
        "2. Help someone submit a new concrete request by asking for "
        "whatever's still missing, one or two things at a time -- never "
        "more than that in one turn. A concrete request has these fields:\n"
        f"{CONCRETE_REQUEST_FIELDS}\n\n"
        "Rules for filling out a request:\n"
        "- Only ask about fields that are still blank in the current draft.\n"
        "- Never invent or assume a value the person didn't actually say.\n"
        "- Once every REQUIRED field is filled, read back a short spoken "
        "summary of the whole request and ask them to confirm before "
        "submitting anything.\n"
        "- Only submit (action=submit) on the turn where they clearly "
        "confirm (yes / go ahead / submit it / that's right) AND every "
        "required field is already filled in the draft.\n"
        "- If they ask an unrelated question mid-request, just answer it "
        "normally -- don't force them back to the form.\n"
        "- If they say cancel/never mind/start over, clear the fields and "
        "set mode back to chat.\n\n"
        "CURRENT BUSINESS SNAPSHOT:\n" + snapshot + "\n\n"
        "CURRENT DRAFT (fields collected so far, empty if none in progress):\n"
        + json.dumps(draft.get("fields", {})) + "\n\n"
        "CONVERSATION SO FAR:\n" + (history_text or "(nothing yet)") + "\n\n"
        "Respond in exactly two parts:\n"
        "1. Your natural spoken reply.\n"
        "2. On its own line, a state block in EXACTLY this format:\n"
        '<state>{"mode": "concrete_request" or "chat", "fields": {<all fields known so far>}, "action": "none" or "submit"}</state>'
    )

    import urllib.request
    import urllib.error
    body = json.dumps({
        "model": "claude-sonnet-4-6", "max_tokens": 600,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_text = data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        return f"I hit an error talking to Claude: {e.code} {e.reason}.", draft, None
    except Exception as e:
        return f"I hit an error: {str(e)}.", draft, None

    spoken, state = _parse_assistant_reply(raw_text)
    mode = state.get("mode", "chat")
    fields = state.get("fields", {}) if mode == "concrete_request" else {}
    action = state.get("action", "none")

    new_draft = {"mode": mode, "fields": fields, "history": history + [
        {"role": "user", "text": user_text}, {"role": "assistant", "text": spoken}
    ]}

    submitted_id = None
    if action == "submit":
        needs_pump = fields.get("pump_type") in ("Ground Pump", "Overhead Pump")
        needs_drilling = fields.get("drilling_required") == "Yes"
        required_now = [
            f for f in VOICE_REQUIRED_FIELDS
            if (needs_pump or f not in ("pump_size", "pump_arrival_time"))
            and (needs_drilling or f != "drilling_time")
        ]
        missing = [f for f in required_now if not str(fields.get(f, "")).strip()]
        required_ok = not missing
        if required_ok:
            fields["requested_date"] = date.today().isoformat()
            submitted_id = create_concrete_request(fields, current_user.name or current_user.email)
            new_draft = {"mode": "chat", "fields": {}, "history": []}
        else:
            spoken += " Actually, I'm still missing something required -- let's finish that first."

    return spoken, new_draft, submitted_id


@app.route("/assistant")
@login_required
def assistant_page():
    if not is_atlas_allowed():
        flash("Ask Ayoub for access to the office assistant.", "error")
        return redirect(url_for("home"))
    return render_template("assistant.html")


def transcribe_via_whisper(audio_bytes, mime_type):
    """Transcribe recorded audio via OpenAI's Whisper API. Preferred over
    ElevenLabs for listening -- Whisper costs roughly $0.006/minute versus
    ElevenLabs' ~330 credits/minute, which burns through the free 10,000
    credit/month allowance fast on its own. Keeping listening off
    ElevenLabs leaves the full credit allowance for the voice (talking
    back), which is the part actually worth paying for.
    Returns (text_or_None, error_or_None).
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None, None  # Not configured -- caller falls back to ElevenLabs, not an error.
    if not audio_bytes:
        return None, "No audio received."
    import urllib.request
    import urllib.error
    import uuid

    boundary = uuid.uuid4().hex
    ext = "webm" if "webm" in (mime_type or "") else "mp4" if "mp4" in (mime_type or "") else "wav"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="atlas.{ext}"\r\n'
        f"Content-Type: {mime_type or 'audio/webm'}\r\n\r\n"
    ).encode("utf-8") + audio_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("text", "").strip(), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        return None, f"Whisper transcription error {e.code}: {detail}"
    except (urllib.error.URLError, TimeoutError) as e:
        return None, f"Whisper connection error: {str(e)}"


def transcribe_via_elevenlabs(audio_bytes, mime_type):
    """Transcribe recorded audio via ElevenLabs Speech-to-Text. Fallback
    used only when OPENAI_API_KEY isn't set -- see transcribe_via_whisper
    for why Whisper is preferred when available. Returns
    (text_or_None, error_or_None).
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return None, "Neither OPENAI_API_KEY nor ELEVENLABS_API_KEY is set."
    if not audio_bytes:
        return None, "No audio received."
    import urllib.request
    import urllib.error
    import uuid

    boundary = uuid.uuid4().hex
    ext = "webm" if "webm" in (mime_type or "") else "mp4" if "mp4" in (mime_type or "") else "wav"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model_id"\r\n\r\nscribe_v1\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="atlas.{ext}"\r\n'
        f"Content-Type: {mime_type or 'audio/webm'}\r\n\r\n"
    ).encode("utf-8") + audio_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/speech-to-text",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "xi-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("text", "").strip(), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        return None, f"ElevenLabs transcription error {e.code}: {detail}"
    except (urllib.error.URLError, TimeoutError) as e:
        return None, f"Transcription connection error: {str(e)}"


@app.route("/assistant/ask", methods=["POST"])
@login_required
def assistant_ask():
    if not is_atlas_allowed():
        return {"error": "not authorized"}, 403

    if request.content_type and "multipart/form-data" in request.content_type:
        audio_file = request.files.get("audio")
        if not audio_file:
            return {"answer": "I didn't receive any audio."}
        audio_bytes, mime_type = audio_file.read(), audio_file.mimetype
        question, transcribe_error = transcribe_via_whisper(audio_bytes, mime_type)
        if question is None and transcribe_error is None:
            # Whisper not configured -- fall back to ElevenLabs.
            question, transcribe_error = transcribe_via_elevenlabs(audio_bytes, mime_type)
        if transcribe_error:
            return {"answer": "I couldn't hear that clearly -- try again.", "transcribe_error": transcribe_error}
        question = (question or "").strip()
    else:
        question = (request.get_json(silent=True) or {}).get("question", "").strip()

    if not question:
        return {"answer": "I didn't catch a question."}

    draft = session.get("voice_draft") or {"mode": "chat", "fields": {}, "history": []}
    answer, new_draft, submitted_id = call_claude_assistant_turn(question, draft)
    session["voice_draft"] = new_draft

    audio_b64, audio_error = generate_atlas_speech(answer)

    return {"question": question, "answer": answer, "submitted_id": submitted_id, "mode": new_draft.get("mode"), "audio": audio_b64, "audio_error": audio_error}


@app.route("/assistant/reset", methods=["POST"])
@login_required
def assistant_reset():
    session.pop("voice_draft", None)
    return {"ok": True}



REQUEST_STATUSES = ["Submitted", "Reviewing", "Approved", "Building", "Testing", "Released", "On Hold", "Not Planned"]
CONCRETE_STATUS_OPTIONS = ["Submitted", "Scheduled", "Completed"]


def _log_request_status(db, request_id, status, changed_by, release_note=None):
    """Write one row to the employee-visible status timeline. This is the
    single source of truth both the employee's "My Requests" timeline and
    the admin's status history read from -- there's no separate "current
    status" tracking logic to keep in sync, the latest row here always
    reflects the truth (and feature_requests.status is kept in step
    alongside it for fast filtering/display).
    """
    now = datetime.utcnow().isoformat()
    db.execute(
        "INSERT INTO feature_request_status_history (feature_request_id, status, release_note, changed_by, changed_at) VALUES (?,?,?,?,?)",
        (request_id, status, release_note, changed_by, now)
    )
    db.execute("UPDATE feature_requests SET status = ?, updated_at = ? WHERE id = ?", (status, now, request_id))


@app.route("/requests", methods=["GET", "POST"])
@login_required
def request_center():
    db = get_db()
    department_options = [d["name"] for d in db.execute("SELECT name FROM departments ORDER BY name").fetchall()]
    if request.method == "POST":
        text = request.form.get("original_request", "").strip()
        if not text:
            flash("Please describe what you need before submitting.", "error")
        else:
            submitted_department = request.form.get("department", "").strip()
            if not submitted_department:
                user_row = db.execute("SELECT department FROM users WHERE email = ?", (current_user.email,)).fetchone()
                submitted_department = user_row["department"] if user_row else None
            department = submitted_department
            now = datetime.utcnow().isoformat()
            cur = db.execute(
                "INSERT INTO feature_requests (requester_email, requester_name, department, original_request, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (current_user.email, current_user.name or current_user.email, department, text, "Submitted", now, now)
            )
            request_id = cur.lastrowid
            db.commit()
            _log_request_status(db, request_id, "Submitted", current_user.email)
            for screenshot in request.files.getlist("screenshots"):
                saved_name = save_photo(screenshot)
                if saved_name:
                    db.execute(
                        "INSERT INTO feature_request_attachments (feature_request_id, filename, uploaded_by, created_at) VALUES (?,?,?,?)",
                        (request_id, saved_name, current_user.email, now)
                    )
            db.commit()
            flash("Request submitted.")
        return redirect(url_for("request_center"))

    my_requests = db.execute(
        "SELECT * FROM feature_requests WHERE requester_email = ? ORDER BY created_at DESC",
        (current_user.email,)
    ).fetchall()
    requests_with_history = []
    for r in my_requests:
        history = db.execute(
            "SELECT * FROM feature_request_status_history WHERE feature_request_id = ? ORDER BY changed_at",
            (r["id"],)
        ).fetchall()
        attachments = db.execute(
            "SELECT * FROM feature_request_attachments WHERE feature_request_id = ? ORDER BY id",
            (r["id"],)
        ).fetchall()
        requests_with_history.append({"r": r, "history": history, "attachments": attachments})
    return render_template("requests/my_requests.html", requests_with_history=requests_with_history, department_options=department_options)


def _render_my_requests_for(db, email):
    """Shared by the real employee view and the admin's PREVIEW mode --
    same query, same filtering, so preview is a genuine test of the real
    access control rather than a separately-maintained mockup.
    """
    my_requests = db.execute(
        "SELECT * FROM feature_requests WHERE requester_email = ? ORDER BY created_at DESC", (email,)
    ).fetchall()
    requests_with_history = []
    for r in my_requests:
        history = db.execute(
            "SELECT * FROM feature_request_status_history WHERE feature_request_id = ? ORDER BY changed_at",
            (r["id"],)
        ).fetchall()
        attachments = db.execute(
            "SELECT * FROM feature_request_attachments WHERE feature_request_id = ? ORDER BY id",
            (r["id"],)
        ).fetchall()
        requests_with_history.append({"r": r, "history": history, "attachments": attachments})
    return requests_with_history


def _spark_points(counts, width=220, height=30, pad=3):
    """Turn a list of daily counts into an SVG polyline 'points' string,
    scaled to fit the given viewbox. Used for the small module-tile
    sparklines on the Command Center."""
    if not counts:
        return ""
    mx = max(counts) or 1
    n = len(counts)
    step = width / (n - 1) if n > 1 else 0
    pts = []
    for i, c in enumerate(counts):
        x = round(i * step, 1)
        y = round(height - pad - (c / mx) * (height - 2 * pad), 1) if mx else height / 2
        pts.append(f"{x},{y}")
    return " ".join(pts)


def _trend_paths(counts, width=900, height=140, pad_top=8, pad_bottom=8):
    """Turn a list of daily counts into an SVG line path plus a matching
    filled-area path (closed down to the baseline), scaled to the given
    viewbox. Used for the big Requests Trend chart on Command Center."""
    if not counts:
        return "", ""
    mx = max(counts) if max(counts) > 0 else 1
    n = len(counts)
    step = width / (n - 1) if n > 1 else 0
    pts = []
    for i, c in enumerate(counts):
        x = round(i * step, 1)
        y = round(height - pad_bottom - (c / mx) * (height - pad_top - pad_bottom), 1)
        pts.append((x, y))
    line = " ".join(f"L{x},{y}" if i > 0 else f"M{x},{y}" for i, (x, y) in enumerate(pts))
    fill = line + f" L{pts[-1][0]},{height} L{pts[0][0]},{height} Z"
    return line, fill


@app.route("/admin/product-intelligence")
@login_required
def product_intelligence():
    if not is_admin():
        flash("Product Intelligence is restricted to admins.", "error")
        return redirect(url_for("home"))
    db = get_db()
    status_filter = request.args.get("status", "")
    department_filter = request.args.get("department", "")
    conditions, params = [], []
    if status_filter:
        # Supports a single status (the existing dropdown) or a
        # comma-separated list (the new clickable KPI cards, e.g.
        # "Submitted,Reviewing" for the combined New/Reviewing card).
        status_list = [s.strip() for s in status_filter.split(",") if s.strip()]
        placeholders = ",".join("?" * len(status_list))
        conditions.append(f"status IN ({placeholders})")
        params.extend(status_list)
    if department_filter:
        conditions.append("department = ?")
        params.append(department_filter)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = db.execute(f"SELECT * FROM feature_requests {where} ORDER BY created_at DESC", params).fetchall()
    departments = [d["department"] for d in db.execute(
        "SELECT DISTINCT department FROM feature_requests WHERE department IS NOT NULL AND department != ''"
    ).fetchall()]

    # Dashboard data -- all real aggregate queries against feature_requests
    # and its related tables, computed fresh every load. Nothing here is
    # fabricated or estimated; every number traces back to an actual row.
    all_requests = db.execute("SELECT * FROM feature_requests").fetchall()
    status_counts = {}
    for r in all_requests:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    total_requests = len(all_requests)
    kpi = {
        "total": total_requests,
        "new_reviewing": status_counts.get("Submitted", 0) + status_counts.get("Reviewing", 0),
        "building": status_counts.get("Building", 0),
        "testing": status_counts.get("Testing", 0),
        "released": status_counts.get("Released", 0),
        "stalled": status_counts.get("On Hold", 0) + status_counts.get("Not Planned", 0),
    }

    pipeline_order = ["Submitted", "Reviewing", "Approved", "Building", "Testing", "Released", "On Hold", "Not Planned"]
    pipeline_colors = {
        "Submitted": "#5AC8E0", "Reviewing": "#8A7238", "Approved": "#C9A24B", "Building": "#C9A24B",
        "Testing": "#C9A24B", "Released": "#5EEAD4", "On Hold": "#4A5D70", "Not Planned": "#4A5D70",
    }
    pipeline = [
        {"status": s, "count": status_counts.get(s, 0), "color": pipeline_colors[s],
         "pct": round(100 * status_counts.get(s, 0) / total_requests, 1) if total_requests else 0}
        for s in pipeline_order if status_counts.get(s, 0) > 0
    ]

    dept_counts = {}
    for r in all_requests:
        d = r["department"] or "Unassigned"
        dept_counts[d] = dept_counts.get(d, 0) + 1
    dept_breakdown = sorted(
        [{"label": k, "count": v, "pct": round(100 * v / total_requests) if total_requests else 0} for k, v in dept_counts.items()],
        key=lambda x: -x["count"]
    )

    module_rows = db.execute(
        "SELECT buildiq_module, COUNT(*) as c FROM feature_request_intelligence "
        "WHERE buildiq_module IS NOT NULL AND buildiq_module != '' GROUP BY buildiq_module"
    ).fetchall()
    module_total = sum(m["c"] for m in module_rows) or 1
    module_breakdown = sorted(
        [{"label": m["buildiq_module"], "count": m["c"], "pct": round(100 * m["c"] / module_total)} for m in module_rows],
        key=lambda x: -x["count"]
    )

    recent_activity = db.execute(
        """SELECT h.status, h.changed_by, h.changed_at, f.original_request, f.id as request_id
           FROM feature_request_status_history h JOIN feature_requests f ON f.id = h.feature_request_id
           ORDER BY h.changed_at DESC LIMIT 8"""
    ).fetchall()

    recently_released = db.execute(
        """SELECT f.id, f.original_request, f.requester_name, f.requester_email, f.department,
           h.release_note, h.changed_at, i.buildiq_module
           FROM feature_requests f
           JOIN feature_request_status_history h ON h.feature_request_id = f.id
           LEFT JOIN feature_request_intelligence i ON i.feature_request_id = f.id
           WHERE f.status = 'Released' AND h.status = 'Released'
           ORDER BY h.changed_at DESC LIMIT 5"""
    ).fetchall()

    # ---- Command Center: control-room layer ----
    # Everything below is a real query against existing tables -- nothing
    # here is fabricated. Priority queue, system gauges, the requests
    # trend chart, and per-module activity sparklines.
    today = date.today()

    attention_items = []
    upcoming_bids = db.execute(
        "SELECT id, name, bid_due_date FROM tracker_projects "
        "WHERE status = 'In Progress' AND bid_due_date IS NOT NULL AND bid_due_date != '' "
        "ORDER BY bid_due_date ASC"
    ).fetchall()
    for p in upcoming_bids:
        try:
            due = datetime.strptime(p["bid_due_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        days_left = (due - today).days
        if 0 <= days_left <= 3:
            attention_items.append({
                "priority": "high", "title": f"Bid due in {days_left} day{'s' if days_left != 1 else ''} \u2014 {p['name']}",
                "meta": "PROJECT HUNT", "action_label": "Open",
                "url": url_for("tracker_view_project", project_id=p["id"])
            })

    overdue_rentals = db.execute(
        "SELECT id, equipment_description, due_date FROM sitepulse_rentals "
        "WHERE returned_date IS NULL AND due_date IS NOT NULL AND due_date != '' AND due_date < ?",
        (today.isoformat(),)
    ).fetchall()
    for r in overdue_rentals:
        try:
            due = datetime.strptime(r["due_date"], "%Y-%m-%d").date()
            days_late = (today - due).days
        except ValueError:
            days_late = None
        attention_items.append({
            "priority": "high", "title": f"Rental overdue \u2014 {r['equipment_description']}",
            "meta": f"EQUIPMENT CENTER \u00b7 {days_late} day{'s' if days_late != 1 else ''} late" if days_late is not None else "EQUIPMENT CENTER",
            "action_label": "Open", "url": url_for("sitepulse_rentals_list")
        })

    pending_requests_count = status_counts.get("Submitted", 0) + status_counts.get("Reviewing", 0)
    if pending_requests_count > 0:
        attention_items.append({
            "priority": "med", "title": f"{pending_requests_count} new request{'s' if pending_requests_count != 1 else ''} awaiting review",
            "meta": "REQUEST CENTER", "action_label": "Review",
            "url": url_for("product_intelligence", status="Submitted,Reviewing")
        })

    tomorrow = (today + timedelta(days=1)).isoformat()
    unordered_pours = db.execute(
        "SELECT id, project FROM inventory_concrete_requests WHERE pour_date = ? AND status = 'Submitted'",
        (tomorrow,)
    ).fetchall()
    for c in unordered_pours:
        attention_items.append({
            "priority": "med", "title": "Concrete pour tomorrow \u2014 no order placed",
            "meta": f"SITEPULSE \u00b7 {c['project']}", "action_label": "Open",
            "url": url_for("inventory_concrete_list")
        })

    priority_rank = {"high": 0, "med": 1, "low": 2}
    attention_items.sort(key=lambda x: priority_rank.get(x["priority"], 3))
    attention_items = attention_items[:6]

    total_assets = db.execute("SELECT COUNT(*) FROM sitepulse_assets").fetchone()[0]
    available_assets = db.execute("SELECT COUNT(*) FROM sitepulse_assets WHERE status = 'Available'").fetchone()[0]
    fleet_uptime_pct = round(100 * available_assets / total_assets) if total_assets else None
    resolution_rate_pct = round(100 * kpi["released"] / total_requests) if total_requests else 0

    day_labels = [(today - timedelta(days=i)) for i in range(13, -1, -1)]
    submitted_counts, resolved_counts = [], []
    for d in day_labels:
        d_str = d.isoformat()
        submitted_counts.append(db.execute(
            "SELECT COUNT(*) FROM feature_requests WHERE date(created_at) = ?", (d_str,)
        ).fetchone()[0])
        resolved_counts.append(db.execute(
            "SELECT COUNT(*) FROM feature_request_status_history WHERE status = 'Released' AND date(changed_at) = ?", (d_str,)
        ).fetchone()[0])
    submitted_line, _ = _trend_paths(submitted_counts)
    resolved_line, resolved_fill = _trend_paths(resolved_counts)

    week_days = [(today - timedelta(days=i)) for i in range(6, -1, -1)]

    def _section_spark(section):
        counts = []
        for d in week_days:
            counts.append(db.execute(
                "SELECT COUNT(*) FROM activity_log WHERE section = ? AND date(created_at) = ?",
                (section, d.isoformat())
            ).fetchone()[0])
        return _spark_points(counts)

    active_bids_count = db.execute("SELECT COUNT(*) FROM tracker_projects WHERE status = 'In Progress'").fetchone()[0]
    in_maintenance_count = db.execute("SELECT COUNT(*) FROM sitepulse_assets WHERE status = 'In Maintenance'").fetchone()[0]
    open_po_count = db.execute("SELECT COUNT(*) FROM inventory_purchase_requests WHERE status != 'Completed'").fetchone()[0]

    module_tiles = [
        {"name": "Project Hunt", "value": active_bids_count, "label": "Active bids", "color": "var(--teal)",
         "healthy": not any(a["meta"] == "PROJECT HUNT" for a in attention_items),
         "spark": _section_spark("tracker"), "spark_color": "#5EEAD4", "url": url_for("tracker_dashboard")},
        {"name": "Equipment Center", "value": in_maintenance_count, "label": "In maintenance", "color": "var(--brass)",
         "healthy": len(overdue_rentals) == 0,
         "spark": _section_spark("sitepulse"), "spark_color": "#C9A24B", "url": url_for("sitepulse_dashboard")},
        {"name": "SitePulse", "value": open_po_count, "label": "Open POs", "color": "var(--cyan)",
         "healthy": True,
         "spark": _section_spark("inventory"), "spark_color": "#5AC8E0", "url": url_for("inventory_home")},
    ]

    return render_template(
        "requests/product_intelligence.html", requests=rows, statuses=REQUEST_STATUSES,
        departments=departments, status_filter=status_filter, department_filter=department_filter,
        kpi=kpi, pipeline=pipeline, dept_breakdown=dept_breakdown, module_breakdown=module_breakdown,
        recent_activity=recent_activity, recently_released=recently_released,
        attention_items=attention_items, fleet_uptime_pct=fleet_uptime_pct, resolution_rate_pct=resolution_rate_pct,
        submitted_line=submitted_line, resolved_line=resolved_line, resolved_fill=resolved_fill,
        module_tiles=module_tiles
    )


@app.route("/admin/product-intelligence/<int:request_id>", methods=["GET", "POST"])
@login_required
def product_intelligence_detail(request_id):
    if not is_admin():
        flash("Product Intelligence is restricted to admins.", "error")
        return redirect(url_for("home"))
    db = get_db()
    r = db.execute("SELECT * FROM feature_requests WHERE id = ?", (request_id,)).fetchone()
    if not r:
        flash("Request not found.", "error")
        return redirect(url_for("product_intelligence"))

    if request.method == "POST":
        action = request.form.get("action")
        now = datetime.utcnow().isoformat()

        if action == "change_department":
            new_department = request.form.get("department", "").strip()
            db.execute("UPDATE feature_requests SET department = ?, updated_at = ? WHERE id = ?",
                       (new_department, now, request_id))
            db.commit()
            flash("Department corrected.")

        elif action == "save_details":
            # Saves the admin-only intelligence fields WITHOUT touching
            # status at all -- exactly the separation asked for.
            db.execute(
                """INSERT INTO feature_request_intelligence
                   (feature_request_id, buildiq_module, internal_notes, solution_built, testing_notes, user_feedback, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(feature_request_id) DO UPDATE SET
                   buildiq_module=excluded.buildiq_module, internal_notes=excluded.internal_notes,
                   solution_built=excluded.solution_built, testing_notes=excluded.testing_notes,
                   user_feedback=excluded.user_feedback, updated_at=excluded.updated_at""",
                (request_id, request.form.get("buildiq_module", ""), request.form.get("internal_notes", ""),
                 request.form.get("solution_built", ""), request.form.get("testing_notes", ""),
                 request.form.get("user_feedback", ""), now)
            )
            db.commit()
            flash("Details saved.")

        elif action == "change_status":
            new_status = request.form.get("status", "")
            if new_status in REQUEST_STATUSES and new_status != "Released":
                _log_request_status(db, request_id, new_status, current_user.email)
                db.commit()
                flash(f"Status moved to {new_status}.")
            else:
                flash("Invalid status change.", "error")

        elif action == "release":
            # Deliberate, separate action -- requires a note, confirmed
            # via the checkbox on the form. This is the only path that
            # can ever set status to Released.
            release_note = request.form.get("release_note", "").strip()
            confirmed = request.form.get("confirm_release") == "yes"
            if not release_note or not confirmed:
                flash("A release note and confirmation are both required to release a request.", "error")
            else:
                _log_request_status(db, request_id, "Released", current_user.email, release_note=release_note)
                db.execute(
                    """INSERT INTO feature_request_intelligence (feature_request_id, release_date, updated_at)
                       VALUES (?,?,?)
                       ON CONFLICT(feature_request_id) DO UPDATE SET release_date=excluded.release_date, updated_at=excluded.updated_at""",
                    (request_id, now, now)
                )
                db.commit()
                flash("Request released.")

        return redirect(url_for("product_intelligence_detail", request_id=request_id))

    history = db.execute(
        "SELECT * FROM feature_request_status_history WHERE feature_request_id = ? ORDER BY changed_at",
        (request_id,)
    ).fetchall()
    intel = db.execute("SELECT * FROM feature_request_intelligence WHERE feature_request_id = ?", (request_id,)).fetchone()
    attachments = db.execute(
        "SELECT * FROM feature_request_attachments WHERE feature_request_id = ? ORDER BY id", (request_id,)
    ).fetchall()
    department_options = [d["name"] for d in db.execute("SELECT name FROM departments ORDER BY name").fetchall()]
    return render_template("requests/product_intelligence_detail.html", r=r, history=history, intel=intel, statuses=REQUEST_STATUSES, attachments=attachments, department_options=department_options)


@app.route("/admin/product-intelligence/preview")
@login_required
def product_intelligence_preview():
    if not is_admin():
        flash("Product Intelligence is restricted to admins.", "error")
        return redirect(url_for("home"))
    db = get_db()
    preview_email = request.args.get("email", "")
    all_users = db.execute("SELECT email, name FROM users ORDER BY name").fetchall()
    requests_with_history = _render_my_requests_for(db, preview_email) if preview_email else []
    return render_template(
        "requests/preview_employee_view.html", requests_with_history=requests_with_history,
        all_users=all_users, preview_email=preview_email
    )


@app.route("/admin/users", methods=["GET", "POST"])
@login_required
def admin_users():
    if not is_admin():
        flash("This page is restricted to admins.", "error")
        return redirect(url_for("home"))
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "assign_department")

        if action == "add_department":
            new_dept = request.form.get("new_department", "").strip()
            if not new_dept:
                flash("Enter a department name.", "error")
            else:
                try:
                    db.execute(
                        "INSERT INTO departments (name, created_at) VALUES (?, ?)",
                        (new_dept, datetime.utcnow().isoformat())
                    )
                    db.commit()
                    flash(f"'{new_dept}' added to Departments.")
                except sqlite3.IntegrityError:
                    flash(f"'{new_dept}' already exists.", "error")
            return redirect(url_for("admin_users"))

        user_id = request.form.get("user_id")
        department = request.form.get("department", "").strip()
        db.execute("UPDATE users SET department = ? WHERE id = ?", (department, user_id))
        db.commit()
        flash("Department updated.")
        return redirect(url_for("admin_users"))

    users = db.execute("SELECT id, name, email, department FROM users ORDER BY name").fetchall()
    department_options = [d["name"] for d in db.execute("SELECT name FROM departments ORDER BY name").fetchall()]
    return render_template("requests/admin_users.html", users=users, department_options=department_options)


def tr_call_claude(prompt, max_tokens=800):
    api_key = os.environ.get("ANTHROPIC_API_KEY")


def tr_build_rfq_prompt(quote, project):
    today = datetime.now().strftime("%B %d, %Y")
    return f"""Write a short, professional Request for Quote (RFQ) email to a vendor,
asking them to provide pricing for a specific trade/scope on a construction project.
This is an initial outreach, not a follow-up — the vendor has not been contacted yet.

Today's date: {today}
Project: {project['name']}
Trade/Scope needed: {quote['trade']}
Vendor contact: {quote['vendor_contact'] or 'there'}
Vendor company: {quote['vendor_name'] or ''}

Keep it brief (under 120 words), professional, and clear about what's being requested
(a quote for the specified trade/scope). Mention we'd appreciate pricing at their
earliest convenience. No subject line, just the email body. Sign off generically as
"the estimating team," not a specific person's name."""


def tr_build_followup_prompt(quote, project):
    today = datetime.now().strftime("%B %d, %Y")
    return f"""Write a short, professional follow-up email to a vendor who has not yet
responded to a request for quote (RFQ).

Today's date: {today}
Project: {project['name']}
Trade: {quote['trade']}
Vendor contact: {quote['vendor_contact'] or 'there'}
Vendor company: {quote['vendor_name'] or ''}
RFQ sent date: {quote['rfq_sent_date'] or 'recently'}

Keep it brief (under 100 words), polite but direct about needing a response soon since
the bid deadline is approaching. No subject line, just the email body. Sign off generically
as "the estimating team," not a specific person's name."""


TR_UPLOAD_DIR = UPLOAD_DIR
TR_ALLOWED_UPLOAD_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg"}
TR_FILE_SIGNATURES = {
    "pdf": [b"%PDF"], "png": [b"\x89PNG"], "jpg": [b"\xff\xd8\xff"], "jpeg": [b"\xff\xd8\xff"],
    "docx": [b"PK\x03\x04"], "xlsx": [b"PK\x03\x04"],
    "doc": [b"\xd0\xcf\x11\xe0", b"PK\x03\x04"], "xls": [b"\xd0\xcf\x11\xe0", b"PK\x03\x04"],
}


def tr_allowed_upload_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in TR_ALLOWED_UPLOAD_EXTENSIONS


def tr_file_content_matches_extension(file_storage, extension):
    file_storage.seek(0)
    header = file_storage.read(8)
    file_storage.seek(0)
    signatures = TR_FILE_SIGNATURES.get(extension, [])
    if not signatures:
        return True
    return any(header.startswith(sig) for sig in signatures)


@app.route("/tracker/")
@login_required
def tracker_dashboard():
    db = get_db()
    filter_status = request.args.get("filter", "")
    if "filter" not in request.args:
        # No filter specified at all (fresh visit to Project Hunt) --
        # default to showing only active (In Progress) bids. "all" is
        # the explicit escape hatch to see everything else.
        filter_status = "active"

    all_projects = db.execute("SELECT * FROM tracker_projects ORDER BY bid_due_date ASC").fetchall()
    active_projects = [p for p in all_projects if p["status"] != "Archived"]

    def parse_val(v):
        if not v:
            return 0.0
        cleaned = v.replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    kpis = {
        "active": len([p for p in active_projects if p["status"] == "In Progress"]),
        "submitted": len([p for p in active_projects if p["status"] == "Submitted"]),
        "awarded": len([p for p in active_projects if p["status"] == "Awarded"]),
        "unmeant": len([p for p in active_projects if p["status"] == "Unmeant"]),
    }

    total_blocking = 0
    upcoming = []
    call_today = []
    for p in active_projects:
        quotes = db.execute("SELECT * FROM tracker_quotes WHERE project_id = ?", (p["id"],)).fetchall()
        blocking = [q for q in quotes if q["is_submit_blocking"] and q["status"] != "Received"]
        total_blocking += len(blocking)
        dl = tr_daysleft_filter(p["bid_due_date"])
        if dl is not None and p["status"] in ("In Progress", "Pending") and dl <= 14:
            upcoming.append((p, dl))

        if p["status"] not in ("In Progress", "Pending"):
            continue
        for q in quotes:
            if q["status"] == "Sent" and q["rfq_sent_date"]:
                try:
                    sent = datetime.strptime(q["rfq_sent_date"], "%Y-%m-%d").date()
                    days_waiting = (date.today() - sent).days
                    if days_waiting >= 3:
                        call_today.append((p, q, days_waiting))
                except ValueError:
                    pass
    upcoming.sort(key=lambda x: x[1])
    call_today.sort(key=lambda x: -x[2])

    if filter_status == "active":
        display_projects = [p for p in active_projects if p["status"] == "In Progress"]
    elif filter_status == "submitted":
        display_projects = [p for p in active_projects if p["status"] == "Submitted"]
    elif filter_status == "awarded":
        display_projects = [p for p in active_projects if p["status"] == "Awarded"]
    elif filter_status == "unmeant":
        display_projects = [p for p in active_projects if p["status"] == "Unmeant"]
    else:
        # "all" (explicit) or any other/blank value -- show everything
        # that isn't archived.
        display_projects = active_projects

    client_filter = request.args.get("client", "").strip().lower()
    if client_filter:
        display_projects = [p for p in display_projects if p["client"] and client_filter in p["client"].lower()]

    status_filter = request.args.get("status", "")
    if status_filter:
        display_projects = [p for p in display_projects if p["status"] == status_filter]

    sort_by = request.args.get("sort", "due")
    sort_dir = request.args.get("dir", "asc")
    sort_key_map = {
        "name": lambda p: (p["name"] or "").lower(),
        "client": lambda p: (p["client"] or "").lower(),
        "status": lambda p: (p["status"] or ""),
        "value": lambda p: parse_val(p["estimated_value"]),
        "due": lambda p: p["bid_due_date"] or "9999-99-99",
    }
    if sort_by in sort_key_map:
        display_projects = sorted(display_projects, key=sort_key_map[sort_by], reverse=(sort_dir == "desc"))

    return render_template("tracker/dashboard.html", projects=display_projects, kpis=kpis,
                            total_blocking=total_blocking, upcoming=upcoming[:5],
                            call_today=call_today[:8], filter_status=filter_status,
                            client_filter=request.args.get("client", ""), status_filter=status_filter,
                            sort_by=sort_by, sort_dir=sort_dir, status_options=TR_STATUS_OPTIONS)


@app.route("/tracker/archive")
@login_required
def tracker_archive():
    db = get_db()
    projects = db.execute("SELECT * FROM tracker_projects WHERE status = 'Archived' ORDER BY updated_at DESC").fetchall()
    return render_template("tracker/archive.html", projects=projects)


@app.route("/tracker/project/<int:project_id>/delete", methods=["POST"])
@login_required
def tracker_delete_project(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
    log_activity("tracker", "project", project_id, "deleted", asset_id=project_id,
                 old_value=project["name"] if project else None)
    db.execute("DELETE FROM tracker_quotes WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM tracker_docs WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM tracker_projects WHERE id = ?", (project_id,))
    db.commit()
    flash("Project deleted.")
    return redirect(url_for("tracker_dashboard"))


@app.route("/tracker/quote/<int:quote_id>/upload", methods=["POST"])
@login_required
def tracker_upload_quote_file(quote_id):
    from flask import send_file
    db = get_db()
    quote = db.execute("SELECT * FROM tracker_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        flash("Quote not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    uploaded_file = request.files.get("quote_file")
    if not uploaded_file or uploaded_file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))
    if not tr_allowed_upload_file(uploaded_file.filename):
        flash("File type not allowed. Use PDF, Word, Excel, or image files.", "error")
        return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))
    original_name = uploaded_file.filename
    ext = original_name.rsplit(".", 1)[1].lower()
    if not tr_file_content_matches_extension(uploaded_file, ext):
        flash("That file's content doesn't match its extension — upload rejected for safety.", "error")
        return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))
    stored_name = f"quote_{quote_id}_{secrets.token_hex(6)}.{ext}"
    uploaded_file.save(os.path.join(TR_UPLOAD_DIR, stored_name))
    db.execute(
        "UPDATE tracker_quotes SET attachment_filename = ?, attachment_original_name = ?, updated_at = ? WHERE id = ?",
        (stored_name, original_name, datetime.utcnow().isoformat(), quote_id)
    )
    db.commit()
    flash("File uploaded.")
    return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))


@app.route("/tracker/quote/<int:quote_id>/download")
@login_required
def tracker_download_quote_file(quote_id):
    from flask import send_file
    db = get_db()
    quote = db.execute("SELECT * FROM tracker_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote or not quote["attachment_filename"]:
        flash("No file attached to this quote.", "error")
        return redirect(url_for("tracker_dashboard"))
    file_path = os.path.join(TR_UPLOAD_DIR, quote["attachment_filename"])
    if not os.path.exists(file_path):
        flash("File not found on server.", "error")
        return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))
    return send_file(file_path, as_attachment=True, download_name=quote["attachment_original_name"])


@app.route("/tracker/quote/<int:quote_id>/delete_file", methods=["POST"])
@login_required
def tracker_delete_quote_file(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM tracker_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        flash("Quote not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    if quote["attachment_filename"]:
        file_path = os.path.join(TR_UPLOAD_DIR, quote["attachment_filename"])
        if os.path.exists(file_path):
            os.remove(file_path)
    db.execute("UPDATE tracker_quotes SET attachment_filename = NULL, attachment_original_name = NULL WHERE id = ?", (quote_id,))
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))


@app.route("/tracker/quote/<int:quote_id>/edit", methods=["GET", "POST"])
@login_required
def tracker_edit_quote(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM tracker_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        flash("Quote not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (quote["project_id"],)).fetchone()
    if request.method == "POST":
        new_fields = {
            "trade": request.form["trade"], "vendor_name": request.form.get("vendor_name", ""),
            "vendor_contact": request.form.get("vendor_contact", ""), "vendor_email": request.form.get("vendor_email", ""),
            "vendor_phone": tr_format_phone(request.form.get("vendor_phone", "")),
            "rfq_sent_date": request.form.get("rfq_sent_date", ""), "status": request.form.get("status", quote["status"]),
            "is_submit_blocking": 1 if request.form.get("is_submit_blocking") else 0,
            "amount": tr_format_currency(request.form.get("amount", "")), "notes": request.form.get("notes", ""),
        }
        db.execute(
            """UPDATE tracker_quotes SET trade = ?, vendor_name = ?, vendor_contact = ?, vendor_email = ?,
               vendor_phone = ?, rfq_sent_date = ?, status = ?, is_submit_blocking = ?,
               amount = ?, notes = ?, updated_at = ? WHERE id = ?""",
            (new_fields["trade"], new_fields["vendor_name"], new_fields["vendor_contact"],
             new_fields["vendor_email"], new_fields["vendor_phone"], new_fields["rfq_sent_date"],
             new_fields["status"], new_fields["is_submit_blocking"], new_fields["amount"],
             new_fields["notes"], datetime.utcnow().isoformat(), quote_id)
        )
        for field_name, new_val in new_fields.items():
            old_val = quote[field_name]
            if old_val != new_val:
                log_activity("tracker", "quote", quote_id, "updated", asset_id=quote["project_id"],
                             field=field_name, old_value=old_val, new_value=new_val)
        db.commit()
        return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))
    return render_template("tracker/edit_quote.html", q=quote, project=project)


@app.route("/tracker/quote/<int:quote_id>/delete", methods=["POST"])
@login_required
def tracker_delete_quote(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM tracker_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        flash("Quote not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    project_id = quote["project_id"]
    log_activity("tracker", "quote", quote_id, "deleted", asset_id=project_id,
                 old_value=f"{quote['trade']} - {quote['vendor_name']}")
    db.execute("DELETE FROM tracker_quotes WHERE id = ?", (quote_id,))
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=project_id))


@app.route("/tracker/project/new", methods=["GET", "POST"])
@login_required
def tracker_new_project():
    if request.method == "POST":
        db = get_db()
        now = datetime.utcnow().isoformat()
        cur = db.execute(
            """INSERT INTO tracker_projects (name, client, address, bid_due_date, estimated_value, status,
               assigned_to, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request.form["name"], request.form.get("client", ""), request.form.get("address", ""),
             request.form.get("bid_due_date", ""),
             tr_format_currency(request.form.get("estimated_value", "")), request.form.get("status", "In Progress"),
             request.form.get("assigned_to", ""), request.form.get("notes", ""), now, now)
        )
        new_project_id = cur.lastrowid
        log_activity("tracker", "project", new_project_id, "created", asset_id=new_project_id, new_value=request.form["name"])
        db.commit()
        return redirect(url_for("tracker_view_project", project_id=new_project_id))
    return render_template("tracker/new_project.html", status_options=TR_STATUS_OPTIONS)


@app.route("/tracker/project/<int:project_id>")
@login_required
def tracker_view_project(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        flash("Project not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    quotes = db.execute(
        "SELECT * FROM tracker_quotes WHERE project_id = ? ORDER BY is_submit_blocking DESC, trade ASC, vendor_name ASC",
        (project_id,)
    ).fetchall()
    docs = db.execute("SELECT * FROM tracker_docs WHERE project_id = ? ORDER BY created_at ASC", (project_id,)).fetchall()

    trade_groups = []
    seen_trades = {}
    for q in quotes:
        key = q["trade"]
        if key not in seen_trades:
            seen_trades[key] = {"trade": key, "quotes": [], "any_blocking": False}
            trade_groups.append(seen_trades[key])
        seen_trades[key]["quotes"].append(q)
        if q["is_submit_blocking"] and q["status"] != "Received":
            seen_trades[key]["any_blocking"] = True

    return render_template("tracker/project.html", p=project, trade_groups=trade_groups, docs=docs, status_options=TR_STATUS_OPTIONS)


@app.route("/tracker/project/<int:project_id>/update", methods=["POST"])
@login_required
def tracker_update_project(project_id):
    db = get_db()
    old_project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
    new_status = request.form["status"]
    new_value = tr_format_currency(request.form.get("estimated_value", ""))
    db.execute(
        "UPDATE tracker_projects SET status = ?, estimated_value = ?, updated_at = ? WHERE id = ?",
        (new_status, new_value, datetime.utcnow().isoformat(), project_id)
    )
    if old_project and old_project["status"] != new_status:
        log_activity("tracker", "project", project_id, "updated", asset_id=project_id,
                     field="status", old_value=old_project["status"], new_value=new_status)
    if old_project and old_project["estimated_value"] != new_value:
        log_activity("tracker", "project", project_id, "updated", asset_id=project_id,
                     field="estimated_value", old_value=old_project["estimated_value"], new_value=new_value)
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=project_id))


@app.route("/tracker/project/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def tracker_edit_project(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        flash("Project not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    if request.method == "POST":
        new_fields = {
            "name": request.form["name"], "client": request.form.get("client", ""),
            "address": request.form.get("address", ""), "bid_due_date": request.form.get("bid_due_date", ""),
            "estimated_value": tr_format_currency(request.form.get("estimated_value", "")),
            "status": request.form.get("status", project["status"]),
            "assigned_to": request.form.get("assigned_to", ""), "notes": request.form.get("notes", ""),
        }
        db.execute(
            """UPDATE tracker_projects SET name = ?, client = ?, address = ?, bid_due_date = ?, estimated_value = ?,
               status = ?, assigned_to = ?, notes = ?, updated_at = ? WHERE id = ?""",
            (new_fields["name"], new_fields["client"], new_fields["address"], new_fields["bid_due_date"],
             new_fields["estimated_value"], new_fields["status"], new_fields["assigned_to"], new_fields["notes"],
             datetime.utcnow().isoformat(), project_id)
        )
        for field_name, new_val in new_fields.items():
            old_val = project[field_name]
            if old_val != new_val:
                log_activity("tracker", "project", project_id, "updated", asset_id=project_id,
                             field=field_name, old_value=old_val, new_value=new_val)
        db.commit()
        return redirect(url_for("tracker_view_project", project_id=project_id))
    return render_template("tracker/edit_project.html", p=project, status_options=TR_STATUS_OPTIONS)


@app.route("/tracker/project/<int:project_id>/quote/new", methods=["GET", "POST"])
@login_required
def tracker_new_quote(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        flash("Project not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    if request.method == "POST":
        now = datetime.utcnow().isoformat()
        cur = db.execute(
            """INSERT INTO tracker_quotes (project_id, trade, vendor_name, vendor_contact, vendor_email,
               vendor_phone, rfq_sent_date, status, is_submit_blocking, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, request.form["trade"], request.form.get("vendor_name", ""),
             request.form.get("vendor_contact", ""), request.form.get("vendor_email", ""),
             tr_format_phone(request.form.get("vendor_phone", "")), request.form.get("rfq_sent_date", ""),
             request.form.get("status", "Not Sent"), 1 if request.form.get("is_submit_blocking") else 0,
             request.form.get("notes", ""), now, now)
        )
        log_activity("tracker", "quote", cur.lastrowid, "created", asset_id=project_id,
                     new_value=f"{request.form['trade']} - {request.form.get('vendor_name', '')}")
        db.commit()
        return redirect(url_for("tracker_view_project", project_id=project_id))
    return render_template("tracker/new_quote.html", project=project)


@app.route("/tracker/quote/<int:quote_id>/update_status", methods=["POST"])
@login_required
def tracker_update_quote_status(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM tracker_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        flash("Quote not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    new_status = request.form["status"]
    new_amount = tr_format_currency(request.form.get("amount", quote["amount"]))
    db.execute("UPDATE tracker_quotes SET status = ?, amount = ?, updated_at = ? WHERE id = ?",
               (new_status, new_amount, datetime.utcnow().isoformat(), quote_id))
    if quote["status"] != new_status:
        log_activity("tracker", "quote", quote_id, "updated", asset_id=quote["project_id"],
                     field="status", old_value=quote["status"], new_value=new_status)
    if quote["amount"] != new_amount:
        log_activity("tracker", "quote", quote_id, "updated", asset_id=quote["project_id"],
                     field="amount", old_value=quote["amount"], new_value=new_amount)
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))


@app.route("/tracker/quote/<int:quote_id>/generate_rfq", methods=["POST"])
@login_required
def tracker_generate_rfq(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM tracker_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        flash("Quote not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (quote["project_id"],)).fetchone()
    result = tr_call_claude(tr_build_rfq_prompt(quote, project))
    db.execute("UPDATE tracker_quotes SET rfq_email = ?, updated_at = ? WHERE id = ?",
               (result, datetime.utcnow().isoformat(), quote_id))
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))


@app.route("/tracker/quote/<int:quote_id>/generate_followup", methods=["POST"])
@login_required
def tracker_generate_followup(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM tracker_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        flash("Quote not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (quote["project_id"],)).fetchone()
    result = tr_call_claude(tr_build_followup_prompt(quote, project))
    db.execute("UPDATE tracker_quotes SET follow_up_email = ?, updated_at = ? WHERE id = ?",
               (result, datetime.utcnow().isoformat(), quote_id))
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))


@app.route("/tracker/quote/<int:quote_id>/clear_rfq", methods=["POST"])
@login_required
def tracker_clear_rfq(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM tracker_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        flash("Quote not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    db.execute("UPDATE tracker_quotes SET rfq_email = NULL WHERE id = ?", (quote_id,))
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))


@app.route("/tracker/quote/<int:quote_id>/clear_followup", methods=["POST"])
@login_required
def tracker_clear_followup(quote_id):
    db = get_db()
    quote = db.execute("SELECT * FROM tracker_quotes WHERE id = ?", (quote_id,)).fetchone()
    if not quote:
        flash("Quote not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    db.execute("UPDATE tracker_quotes SET follow_up_email = NULL WHERE id = ?", (quote_id,))
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=quote["project_id"]))


@app.route("/tracker/project/<int:project_id>/doc/new", methods=["POST"])
@login_required
def tracker_new_doc(project_id):
    db = get_db()
    cur = db.execute(
        "INSERT INTO tracker_docs (project_id, doc_name, doc_type, status, notes, link, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, request.form["doc_name"], request.form.get("doc_type", ""),
         request.form.get("status", "Needed"), request.form.get("notes", ""),
         request.form.get("link", ""), datetime.utcnow().isoformat())
    )
    log_activity("tracker", "doc", cur.lastrowid, "created", asset_id=project_id, new_value=request.form["doc_name"])
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=project_id))


@app.route("/tracker/doc/<int:doc_id>/edit", methods=["GET", "POST"])
@login_required
def tracker_edit_doc(doc_id):
    db = get_db()
    doc = db.execute("SELECT * FROM tracker_docs WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        flash("Document not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (doc["project_id"],)).fetchone()
    if request.method == "POST":
        new_fields = {
            "doc_name": request.form["doc_name"], "doc_type": request.form.get("doc_type", ""),
            "status": request.form.get("status", doc["status"]), "link": request.form.get("link", ""),
            "notes": request.form.get("notes", ""),
        }
        db.execute(
            "UPDATE tracker_docs SET doc_name = ?, doc_type = ?, status = ?, link = ?, notes = ? WHERE id = ?",
            (new_fields["doc_name"], new_fields["doc_type"], new_fields["status"],
             new_fields["link"], new_fields["notes"], doc_id)
        )
        for field_name, new_val in new_fields.items():
            old_val = doc[field_name]
            if old_val != new_val:
                log_activity("tracker", "doc", doc_id, "updated", asset_id=doc["project_id"],
                             field=field_name, old_value=old_val, new_value=new_val)
        db.commit()
        return redirect(url_for("tracker_view_project", project_id=doc["project_id"]))
    return render_template("tracker/edit_doc.html", d=doc, project=project)


@app.route("/tracker/doc/<int:doc_id>/delete", methods=["POST"])
@login_required
def tracker_delete_doc(doc_id):
    db = get_db()
    doc = db.execute("SELECT * FROM tracker_docs WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        flash("Document not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    project_id = doc["project_id"]
    log_activity("tracker", "doc", doc_id, "deleted", asset_id=project_id, old_value=doc["doc_name"])
    db.execute("DELETE FROM tracker_docs WHERE id = ?", (doc_id,))
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=project_id))


@app.route("/tracker/doc/<int:doc_id>/update", methods=["POST"])
@login_required
def tracker_update_doc(doc_id):
    db = get_db()
    doc = db.execute("SELECT * FROM tracker_docs WHERE id = ?", (doc_id,)).fetchone()
    new_status = request.form["status"]
    db.execute("UPDATE tracker_docs SET status = ? WHERE id = ?", (new_status, doc_id))
    if doc and doc["status"] != new_status:
        log_activity("tracker", "doc", doc_id, "updated", asset_id=doc["project_id"],
                     field="status", old_value=doc["status"], new_value=new_status)
    db.commit()
    return redirect(url_for("tracker_view_project", project_id=doc["project_id"]))


@app.route("/tracker/unit-prices")
@login_required
def tracker_unit_prices():
    db = get_db()
    prices = db.execute("SELECT * FROM tracker_unit_prices ORDER BY category ASC, item ASC").fetchall()
    return render_template("tracker/unit_prices.html", prices=prices)


@app.route("/tracker/unit-prices/new", methods=["POST"])
@login_required
def tracker_new_unit_price():
    db = get_db()
    db.execute(
        "INSERT INTO tracker_unit_prices (category, item, unit, price, notes, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (request.form.get("category", ""), request.form["item"], request.form.get("unit", ""),
         tr_format_currency(request.form.get("price", "")), request.form.get("notes", ""), datetime.utcnow().isoformat())
    )
    db.commit()
    return redirect(url_for("tracker_unit_prices"))


@app.route("/tracker/activity-log")
@login_required
def tracker_activity_log():
    db = get_db()
    entries = db.execute(
        """SELECT a.*, p.name AS project_name FROM activity_log a
           LEFT JOIN tracker_projects p ON a.asset_id = p.id
           WHERE a.section = 'tracker' ORDER BY a.created_at DESC LIMIT 300"""
    ).fetchall()
    return render_template("tracker/activity_log.html", entries=entries)


@app.route("/tracker/project/<int:project_id>/activity")
@login_required
def tracker_project_activity_log(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        flash("Project not found.", "error")
        return redirect(url_for("tracker_dashboard"))
    entries = db.execute(
        "SELECT * FROM activity_log WHERE section = 'tracker' AND asset_id = ? ORDER BY created_at DESC",
        (project_id,)
    ).fetchall()
    return render_template("tracker/activity_log.html", entries=entries, project=project)


@app.route("/admin/backup")
@login_required
def admin_backup():
    if not is_admin():
        flash("Only admins can download the full database backup.", "error")
        return redirect(url_for("home"))
    if not os.path.exists(DB_PATH):
        flash("No database file found.", "error")
        return redirect(url_for("home"))
    backup_name = f"buildiq_backup_{datetime.now().strftime('%Y-%m-%d_%H%M')}.db"
    return send_file(DB_PATH, as_attachment=True, download_name=backup_name)


@app.route("/admin/restore", methods=["GET", "POST"])
@login_required
def admin_restore():
    if not is_admin():
        flash("Only admins can restore the database from a backup.", "error")
        return redirect(url_for("home"))
    if request.method == "POST":
        uploaded_file = request.files.get("backup_file")
        if not uploaded_file or uploaded_file.filename == "":
            flash("No file selected.", "error")
            return redirect(url_for("admin_restore"))
        temp_path = DB_PATH + ".upload_tmp"
        uploaded_file.save(temp_path)
        try:
            test_conn = sqlite3.connect(temp_path)
            test_conn.execute("SELECT COUNT(*) FROM sitepulse_assets").fetchone()
            test_conn.close()
        except Exception:
            os.remove(temp_path)
            flash("That file doesn't look like a valid BuildIQ backup. Nothing was changed.", "error")
            return redirect(url_for("admin_restore"))
        close_db(None)
        os.replace(temp_path, DB_PATH)
        # Restoring swaps in a DB file that may predate newer tables/columns
        # (departments, etc.) -- re-run migrations immediately so the
        # restored DB is brought up to the current schema before anyone
        # hits a route that assumes it.
        init_db()
        flash("Database restored successfully from backup.")
        return redirect(url_for("home"))
    return render_template("admin_restore.html")


@app.route("/admin/export/excel")
@login_required
def admin_export_excel():
    if not is_admin():
        flash("Only admins can export company data.", "error")
        return redirect(url_for("home"))
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    db = get_db()
    wb = Workbook()
    NAVY = "0A1420"
    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor=NAVY)
    title_font = Font(name="Arial", size=16, bold=True, color=NAVY)
    thin = Side(style="thin", color="D1D5DB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")

    def style_title(ws, subtitle):
        ws["A1"] = f"BuildIQ — {subtitle} Export"
        ws["A1"].font = title_font
        ws["A2"] = f"Exported {datetime.now().strftime('%B %d, %Y')}"
        ws["A2"].font = Font(name="Arial", size=9, italic=True, color="6B7280")

    def style_headers(ws, headers, row=4):
        for i, h in enumerate(headers):
            c = ws.cell(row=row, column=i + 1, value=h)
            c.font = header_font
            c.fill = header_fill
            c.alignment = center
            c.border = border

    ws = wb.active
    ws.title = "Equipment"
    style_title(ws, "Equipment")
    style_headers(ws, ["Name", "Description", "Status", "Location", "Value", "Daily Rate"])
    row = 5
    for a in db.execute("SELECT * FROM sitepulse_assets ORDER BY name"):
        for col, val in enumerate([a["name"], a["description"] or "", a["status"], a["location"] or "",
                                    a["value"] or "", a["daily_rate"] or ""], start=1):
            ws.cell(row=row, column=col, value=val).border = border
        row += 1

    ws2 = wb.create_sheet("Rentals")
    style_title(ws2, "Rentals")
    style_headers(ws2, ["Equipment", "Vendor", "Rented", "Due Back", "Returned"])
    row = 5
    for r in db.execute("SELECT * FROM sitepulse_rentals ORDER BY due_date"):
        for col, val in enumerate([r["equipment_description"], r["vendor"], r["rented_date"],
                                    r["due_date"] or "", r["returned_date"] or ""], start=1):
            ws2.cell(row=row, column=col, value=val).border = border
        row += 1

    ws3 = wb.create_sheet("SitePulse")
    style_title(ws3, "SitePulse")
    style_headers(ws3, ["Item", "Site", "Quantity", "Unit", "Shelf/Location"])
    row = 5
    for m in db.execute("SELECT * FROM inventory_materials ORDER BY site, item_name"):
        for col, val in enumerate([m["item_name"], m["site"], m["quantity"] or "", m["unit"] or "",
                                    m["shelf_location"] or ""], start=1):
            ws3.cell(row=row, column=col, value=val).border = border
        row += 1

    ws4 = wb.create_sheet("Project Hunt")
    style_title(ws4, "Project Hunt")
    style_headers(ws4, ["Project", "Client", "Status", "Due Date", "Value"])
    row = 5
    for p in db.execute("SELECT * FROM tracker_projects ORDER BY bid_due_date"):
        for col, val in enumerate([p["name"], p["client"] or "", p["status"], p["bid_due_date"] or "",
                                    p["estimated_value"] or ""], start=1):
            ws4.cell(row=row, column=col, value=val).border = border
        row += 1

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    filename = f"buildiq_export_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return send_file(buffer, as_attachment=True, download_name=filename,
                      mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/import", methods=["GET", "POST"])
@login_required
def admin_import():
    if not is_admin():
        flash("Not authorized.", "error")
        return redirect(url_for("home"))

    if request.method == "POST":
        db = get_db()
        results = []

        sp_file = request.files.get("sitepulse_backup")
        if sp_file and sp_file.filename:
            tmp_path = "/tmp/import_sitepulse.db"
            sp_file.save(tmp_path)
            try:
                src = sqlite3.connect(tmp_path)
                src.row_factory = sqlite3.Row
                now = datetime.utcnow().isoformat()

                asset_id_map = {}
                for a in src.execute("SELECT * FROM assets"):
                    cur = db.execute(
                        """INSERT INTO sitepulse_assets (name, description, year, serial_number, value,
                           daily_rate, weekly_rate, monthly_rate, status, location, hours_mileage,
                           created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (a["name"], a["description"], a["year"], a["serial_number"], a["value"],
                         a["daily_rate"], a["weekly_rate"], a["monthly_rate"], a["status"],
                         a["location"], a["hours_mileage"], a["created_at"] or now, a["updated_at"] or now))
                    asset_id_map[a["id"]] = cur.lastrowid
                results.append(f"{len(asset_id_map)} equipment assets")

                usage_count = 0
                for u in src.execute("SELECT * FROM usage_log"):
                    new_asset_id = asset_id_map.get(u["asset_id"])
                    if new_asset_id:
                        db.execute(
                            """INSERT INTO sitepulse_usage_log (asset_id, usage_type, job_name, job_address,
                               client, out_date, return_date, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                            (new_asset_id, u["usage_type"], u["job_name"], u["job_address"], u["client"],
                             u["out_date"], u["return_date"], u["notes"], u["created_at"] or now))
                        usage_count += 1
                results.append(f"{usage_count} usage log entries")

                maint_count = 0
                for m in src.execute("SELECT * FROM maintenance_log"):
                    new_asset_id = asset_id_map.get(m["asset_id"])
                    if new_asset_id:
                        db.execute(
                            """INSERT INTO sitepulse_maintenance_log (asset_id, entry_date, work_done, parts,
                               hours_at_service, reported_by, resolved, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                            (new_asset_id, m["entry_date"], m["work_done"], m["parts"], m["hours_at_service"],
                             m["reported_by"], m["resolved"], m["created_at"] or now))
                        maint_count += 1
                results.append(f"{maint_count} maintenance log entries")

                rental_count = 0
                for r in src.execute("SELECT * FROM rentals"):
                    db.execute(
                        """INSERT INTO sitepulse_rentals (vendor, equipment_description, job_name, rate_amount,
                           rate_period, rented_date, due_date, returned_date, notes, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (r["vendor"], r["equipment_description"], r["job_name"], r["rate_amount"],
                         r["rate_period"], r["rented_date"], r["due_date"], r["returned_date"], r["notes"],
                         r["created_at"] or now, r["updated_at"] or now))
                    rental_count += 1
                results.append(f"{rental_count} rentals")

                mat_count = 0
                try:
                    for mtl in src.execute("SELECT * FROM materials"):
                        db.execute(
                            """INSERT INTO inventory_materials (item_name, site, quantity, unit, shelf_location,
                               notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                            (mtl["item_name"], mtl["site"], mtl["quantity"], mtl["unit"], mtl["shelf_location"],
                             mtl["notes"], mtl["created_at"] or now, mtl["updated_at"] or now))
                        mat_count += 1
                except sqlite3.OperationalError:
                    pass
                results.append(f"{mat_count} inventory materials")

                cr_count = 0
                try:
                    for c in src.execute("SELECT * FROM concrete_requests"):
                        db.execute(
                            """INSERT INTO inventory_concrete_requests (project, job_site_address,
                               area_description, pour_date, pour_time, mix_design_psi, mix_slump,
                               concrete_amount, truck_spacing, pump_size, pump_arrival_time, lab_required,
                               lab_time, drilling_required, drilling_time, requested_by, requested_signature,
                               requested_date, ordered_by, ordered_signature, ordered_date, created_at, updated_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (c["project"], c["job_site_address"], c["area_description"], c["pour_date"],
                             c["pour_time"], c["mix_design_psi"], c["mix_slump"], c["concrete_amount"],
                             c["truck_spacing"], c["pump_size"], c["pump_arrival_time"], c["lab_required"],
                             c["lab_time"], c["drilling_required"], c["drilling_time"], c["requested_by"],
                             c["requested_signature"], c["requested_date"], c["ordered_by"],
                             c["ordered_signature"], c["ordered_date"], c["created_at"] or now, c["updated_at"] or now))
                        cr_count += 1
                except sqlite3.OperationalError:
                    pass
                results.append(f"{cr_count} concrete requests")

                src.close()
                os.remove(tmp_path)
            except Exception as e:
                flash(f"SitePulse import failed: {e}", "error")
                return redirect(url_for("admin_import"))

        cc_file = request.files.get("tracker_backup")
        if cc_file and cc_file.filename:
            tmp_path = "/tmp/import_tracker.db"
            cc_file.save(tmp_path)
            try:
                src = sqlite3.connect(tmp_path)
                src.row_factory = sqlite3.Row
                now = datetime.utcnow().isoformat()

                project_id_map = {}
                for p in src.execute("SELECT * FROM projects"):
                    cur = db.execute(
                        """INSERT INTO tracker_projects (name, client, address, bid_due_date, estimated_value,
                           status, assigned_to, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (p["name"], p["client"], p["address"], p["bid_due_date"], p["estimated_value"],
                         p["status"], p["assigned_to"], p["notes"], p["created_at"] or now, p["updated_at"] or now))
                    project_id_map[p["id"]] = cur.lastrowid
                results.append(f"{len(project_id_map)} bid tracker projects")

                quote_count = 0
                for q in src.execute("SELECT * FROM quotes"):
                    new_project_id = project_id_map.get(q["project_id"])
                    if new_project_id:
                        db.execute(
                            """INSERT INTO tracker_quotes (project_id, trade, vendor_name, vendor_contact,
                               vendor_email, vendor_phone, rfq_sent_date, status, amount, notes,
                               created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (new_project_id, q["trade"], q["vendor_name"], q["vendor_contact"], q["vendor_email"],
                             q["vendor_phone"], q["rfq_sent_date"], q["status"], q["amount"], q["notes"],
                             q["created_at"] or now, q["updated_at"] or now))
                        quote_count += 1
                results.append(f"{quote_count} vendor quotes")

                src.close()
                os.remove(tmp_path)
            except Exception as e:
                flash(f"Bid Tracker import failed: {e}", "error")
                return redirect(url_for("admin_import"))

        db.commit()
        flash("Imported: " + ", ".join(results) if results else "No files uploaded.")
        return redirect(url_for("admin_import"))

    return render_template("admin_import.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
