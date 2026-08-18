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
import requests
import markdown as md_lib
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, flash, g, send_file, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
csrf = CSRFProtect(app)

DB_DIR = os.environ.get("DATA_DIR", ".")
DB_PATH = os.path.join(DB_DIR, "buildiq.db")
UPLOAD_DIR = os.path.join(DB_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "heic", "webp"}
MAX_PHOTO_SIZE_MB = 10

ADMIN_EMAILS = ["ayoub@darycet.com"]
ALLOWED_DOMAIN = "@darycet.com"
# Extra emails allowed to sign up even though they're outside the main
# company email domain.
EXTRA_ALLOWED_SIGNUP_EMAILS = {"hghuneim@nomaengineering.com"}
# Full access to every section, including Project Hunt.
FULL_ACCESS_EMAILS = {"ayoub@darycet.com", "rebecca@darycet.com", "marilu@darycet.com", "hghuneim@nomaengineering.com"}
# Only these can actually place a concrete/material order -- everyone else
# can submit a request, but "Scheduled/Ordered" plus the vendor/contact
# details is procurement's call.
PROCUREMENT_EMAILS = {"ayoub@darycet.com", "rebecca@darycet.com", "marilu@darycet.com"}


def is_project_hunt_allowed():
    return current_user.is_authenticated and current_user.email in FULL_ACCESS_EMAILS


def is_procurement():
    return current_user.is_authenticated and current_user.email in PROCUREMENT_EMAILS

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
        "is_procurement": current_user.is_authenticated and current_user.email in PROCUREMENT_EMAILS,
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


def apply_due_scheduled_moves(asset_id=None):
    """Scheduled location moves apply themselves once their date arrives --
    there's no cron here, so we check for anything due whenever an asset
    page (or the dashboard) loads and apply it right then. asset_id=None
    checks every asset (used on the dashboard); a specific id scopes it to
    one asset's page load.
    """
    db = get_db()
    today = date.today().isoformat()
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
        db.execute("UPDATE sitepulse_assets SET location=?, updated_at=? WHERE id=?",
                   (move["to_location"], datetime.utcnow().isoformat(), move["asset_id"]))
        db.execute("UPDATE sitepulse_usage_log SET move_status='Applied' WHERE id=?", (move["id"],))
        log_activity("sitepulse", "move", move["id"], "applied", asset_id=move["asset_id"],
                     field="location", old_value=move["from_location"], new_value=move["to_location"])
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
        CREATE TABLE IF NOT EXISTS inventory_concrete_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
            job_site_address TEXT, area_description TEXT, pour_date TEXT NOT NULL,
            pour_time TEXT, mix_design_psi TEXT, mix_slump TEXT, concrete_amount TEXT,
            truck_spacing TEXT, pump_size TEXT, pump_arrival_time TEXT,
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
            item TEXT, description TEXT, supplier TEXT, qty TEXT,
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
        # Location-move auto-tracking (item 15) -- Status & Location now
        # writes movement entries straight into the usage log, including
        # ones scheduled for a future date.
        "ALTER TABLE sitepulse_usage_log ADD COLUMN entry_kind TEXT DEFAULT 'usage'",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN from_location TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN to_location TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN mileage_hours TEXT",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN move_status TEXT DEFAULT 'Applied'",
        "ALTER TABLE sitepulse_usage_log ADD COLUMN scheduled_date TEXT",
    ]:
        try:
            db.execute(column_sql)
        except sqlite3.OperationalError:
            pass

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
        if not email.endswith(ALLOWED_DOMAIN) and email not in EXTRA_ALLOWED_SIGNUP_EMAILS:
            flash(f"Sign up with your {ALLOWED_DOMAIN} email.", "error")
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
    usage = db.execute("SELECT * FROM sitepulse_usage_log WHERE asset_id = ? AND move_status != 'Scheduled' ORDER BY COALESCE(out_date, created_at) DESC", (asset_id,)).fetchall()
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
    old = db.execute("SELECT status, location FROM sitepulse_assets WHERE id = ?", (asset_id,)).fetchone()
    new_status = request.form["status"]
    new_location = request.form.get("location", "").strip()
    hours_mileage = request.form.get("hours_mileage", "")
    schedule_date = request.form.get("schedule_date", "").strip()
    now = datetime.utcnow().isoformat()
    today = date.today().isoformat()
    old_location = old["location"] or ""

    location_changed = new_location != old_location and new_location != ""

    if location_changed and schedule_date and schedule_date > today:
        # Future move: don't touch the asset's location yet -- park it as a
        # Scheduled entry in the usage log. apply_due_scheduled_moves()
        # applies it automatically once that date arrives.
        db.execute("UPDATE sitepulse_assets SET status=?, hours_mileage=?, updated_at=? WHERE id=?",
                   (new_status, hours_mileage, now, asset_id))
        cur = db.execute(
            """INSERT INTO sitepulse_usage_log (asset_id, entry_kind, from_location, to_location,
               mileage_hours, move_status, scheduled_date, out_date, created_at)
               VALUES (?, 'move', ?, ?, ?, 'Scheduled', ?, ?, ?)""",
            (asset_id, old_location, new_location, hours_mileage, schedule_date, schedule_date, now)
        )
        log_activity("sitepulse", "move", cur.lastrowid, "scheduled", asset_id=asset_id,
                     field="location", old_value=old_location, new_value=new_location)
        db.commit()
        flash(f"Move to {new_location} scheduled for {schedule_date}.")
        return redirect(url_for("sitepulse_view_asset", asset_id=asset_id))

    db.execute("UPDATE sitepulse_assets SET status=?, location=?, hours_mileage=?, updated_at=? WHERE id=?",
               (new_status, new_location, hours_mileage, now, asset_id))

    if location_changed:
        cur = db.execute(
            """INSERT INTO sitepulse_usage_log (asset_id, entry_kind, from_location, to_location,
               mileage_hours, move_status, scheduled_date, out_date, created_at)
               VALUES (?, 'move', ?, ?, ?, 'Applied', ?, ?, ?)""",
            (asset_id, old_location, new_location, hours_mileage, today, today, now)
        )
        log_activity("sitepulse", "move", cur.lastrowid, "created", asset_id=asset_id,
                     field="location", old_value=old_location, new_value=new_location)

    if old["status"] != new_status:
        log_activity("sitepulse", "asset", asset_id, "updated", field="status", old_value=old["status"], new_value=new_status)
    db.commit()
    flash("Asset updated.")
    return redirect(url_for("sitepulse_view_asset", asset_id=asset_id))


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


@app.route("/inventory/concrete")
@login_required
def inventory_concrete_list():
    db = get_db()
    rows = db.execute("SELECT * FROM inventory_concrete_requests ORDER BY pour_date DESC").fetchall()
    return render_template("inventory/concrete_requests.html", requests=rows)


@app.route("/inventory/concrete/new", methods=["GET", "POST"])
@login_required
def inventory_new_concrete():
    if request.method == "POST":
        db = get_db()
        now = datetime.utcnow().isoformat()
        cur = db.execute(
            """INSERT INTO inventory_concrete_requests (project, job_site_address, area_description,
               pour_date, pour_time, mix_design_psi, mix_slump, concrete_amount, truck_spacing,
               pump_size, pump_arrival_time, lab_required, lab_time, drilling_required, drilling_time,
               requested_by, requested_signature, requested_date, ordered_by, ordered_signature,
               ordered_date, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (request.form.get("project", ""), request.form.get("job_site_address", ""),
             request.form.get("area_description", ""), request.form["pour_date"], request.form.get("pour_time", ""),
             request.form.get("mix_design_psi", ""), request.form.get("mix_slump", ""),
             request.form.get("concrete_amount", ""), request.form.get("truck_spacing", ""),
             request.form.get("pump_size", ""), request.form.get("pump_arrival_time", ""),
             request.form.get("lab_required", "No"), request.form.get("lab_time", ""),
             request.form.get("drilling_required", "No"), request.form.get("drilling_time", ""),
             current_user.name or current_user.email, request.form.get("requested_signature", ""),
             request.form.get("requested_date", ""), request.form.get("ordered_by", ""),
             request.form.get("ordered_signature", ""), request.form.get("ordered_date", ""),
             "Submitted", now, now)
        )
        log_activity("inventory", "concrete_request", cur.lastrowid, "created", new_value=request.form.get("project", ""))
        db.commit()
        flash("Concrete request submitted.")
        return redirect(url_for("inventory_concrete_list"))
    return render_template("inventory/new_concrete_request.html", today=date.today().isoformat())


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
        pump_line = "Pump"
        if r["pump_company"]:
            pump_line += f"-{r['pump_company']}"
        if r["pump_company_phone"]:
            pump_line += f" #{r['pump_company_phone']}"
        if pump_time:
            pump_line += f" @{pump_time}"
        lines.append(pump_line)

    if r["concrete_company"]:
        concrete_line = r["concrete_company"]
        if pour_time:
            concrete_line += f" @{pour_time}"
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
        db.execute(
            """UPDATE inventory_concrete_requests SET project=?, job_site_address=?, area_description=?,
               pour_date=?, pour_time=?, mix_design_psi=?, mix_slump=?, concrete_amount=?, truck_spacing=?,
               pump_size=?, pump_arrival_time=?, lab_required=?, lab_time=?, drilling_required=?,
               drilling_time=?, ordered_by=?, ordered_signature=?, ordered_date=?, updated_at=?
               WHERE id=?""",
            (request.form.get("project", ""), request.form.get("job_site_address", ""),
             request.form.get("area_description", ""), request.form["pour_date"], request.form.get("pour_time", ""),
             request.form.get("mix_design_psi", ""), request.form.get("mix_slump", ""),
             request.form.get("concrete_amount", ""), request.form.get("truck_spacing", ""),
             request.form.get("pump_size", ""), request.form.get("pump_arrival_time", ""),
             request.form.get("lab_required", "No"), request.form.get("lab_time", ""),
             request.form.get("drilling_required", "No"), request.form.get("drilling_time", ""),
             request.form.get("ordered_by", ""), request.form.get("ordered_signature", ""),
             request.form.get("ordered_date", ""), datetime.utcnow().isoformat(), request_id)
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
               concrete_company=?, concrete_company_phone=?, pump_company=?, pump_company_phone=?,
               pump_arrival_time=?, lab_company=?, lab_time=?, drilling_company=?, drilling_company_phone=?,
               drilling_time=?, ordered_by=?, ordered_date=?, status='Scheduled', updated_at=?
               WHERE id=?""",
            (request.form.get("concrete_company", ""), request.form.get("concrete_company_phone", ""),
             request.form.get("pump_company", ""), request.form.get("pump_company_phone", ""),
             request.form.get("pump_arrival_time", ""), request.form.get("lab_company", ""),
             request.form.get("lab_time", ""), request.form.get("drilling_company", ""),
             request.form.get("drilling_company_phone", ""), request.form.get("drilling_time", ""),
             current_user.name or current_user.email, date.today().isoformat(), now, request_id)
        )
        log_activity("inventory", "concrete_request", request_id, "updated", field="status",
                     old_value=r["status"], new_value="Scheduled")
        db.commit()
        flash("Order placed and marked Scheduled.")
        return redirect(url_for("inventory_view_concrete", request_id=request_id))
    return render_template("inventory/place_concrete_order.html", r=r, today=date.today().isoformat())


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


@app.route("/inventory/purchase/new", methods=["GET", "POST"])
@login_required
def inventory_new_purchase():
    if request.method == "POST":
        db = get_db()
        now = datetime.utcnow().isoformat()
        cur = db.execute(
            """INSERT INTO inventory_purchase_requests (pr_number, request_date, job_name,
               location_description, requested_by, needed_on, source_of_supply,
               requestor_signature, requestor_date, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (request.form.get("pr_number", ""), request.form["request_date"], request.form.get("job_name", ""),
             request.form.get("location_description", ""), current_user.name or current_user.email,
             request.form.get("needed_on", ""), request.form.get("source_of_supply", ""),
             request.form.get("requestor_signature", ""), request.form.get("requestor_date", ""),
             "Submitted", now, now)
        )
        pr_id = cur.lastrowid
        items = request.form.getlist("item[]")
        descriptions = request.form.getlist("description[]")
        suppliers = request.form.getlist("supplier[]")
        qtys = request.form.getlist("qty[]")
        for item, desc, sup, qty in zip(items, descriptions, suppliers, qtys):
            if item.strip() or desc.strip():
                db.execute(
                    "INSERT INTO inventory_purchase_request_items (purchase_request_id, item, description, supplier, qty) VALUES (?,?,?,?,?)",
                    (pr_id, item, desc, sup, qty)
                )
        log_activity("inventory", "purchase_request", pr_id, "created", new_value=request.form.get("job_name", ""))
        db.commit()
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
        for item, desc, sup, qty in zip(items, descriptions, suppliers, qtys):
            if item.strip() or desc.strip():
                db.execute(
                    "INSERT INTO inventory_purchase_request_items (purchase_request_id, item, description, supplier, qty) VALUES (?,?,?,?,?)",
                    (request_id, item, desc, sup, qty)
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
               ordered_by=?, ordered_date=?, status='Scheduled', updated_at=? WHERE id=?""",
            (request.form.get("vendor_company", ""), request.form.get("vendor_company_phone", ""),
             current_user.name or current_user.email, date.today().isoformat(),
             datetime.utcnow().isoformat(), request_id)
        )
        log_activity("inventory", "purchase_request", request_id, "updated", field="status",
                     old_value=r["status"], new_value="Scheduled")
        db.commit()
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


def tr_call_claude(prompt, max_tokens=800):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "[No ANTHROPIC_API_KEY set.]"
    import urllib.request
    import urllib.error
    body = json.dumps({
        "model": "claude-sonnet-4-6", "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        return f"[AI generation error: {e.code} {e.reason}]"
    except Exception as e:
        return f"[AI generation error: {str(e)}]"


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
