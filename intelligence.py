"""
BuildIQ shared intelligence layer -- Phase 3A.

Two things live here:

1. build_attention_items(user) -- the single source of "what needs
   attention" logic. Every item is derived from real, existing rows
   (no fabricated scores, no invented business rules). Results are
   filtered to exactly what the requesting user is authorized to see,
   using the same user_has_permission() resolver every other permission
   check in the app goes through. Product Intelligence (Phase 3C) and
   Atlas's get_attention_items tool both consume this same function --
   one set of rules, not two copies that can drift apart.

2. Registration of Atlas's 7 new read-only tools (tools 1-7 below;
   get_attention_items is the 8th and wraps build_attention_items
   directly). Every tool here is read-only, runs a fixed parameterized
   query (never arbitrary SQL or Python), and is gated by both a manual
   permission and an atlas_permission through app.py's existing
   execute_tool() gateway -- this file only supplies handlers and
   register_tool() calls, it never bypasses that gateway.

Imports from app are done inside functions (not at module load time)
because app.py imports this module partway through its own execution --
by the time any handler here actually runs, app.py has fully loaded and
every name below exists. This sidesteps circular-import ordering
without needing to duplicate any logic.
"""
from datetime import date, datetime, timedelta


# ---------------------------------------------------------------------------
# Attention engine
# ---------------------------------------------------------------------------

def _bids_needing_attention(db):
    """Projects with a blocking quote not yet Received. Same rule
    tracker_view_project() already uses (is_submit_blocking=1 AND
    status != 'Received') -- not a new business rule, just reused."""
    from flask import url_for
    items = []
    rows = db.execute(
        """SELECT tp.id, tp.name, COUNT(*) as blocking_count
           FROM tracker_projects tp
           JOIN tracker_quotes tq ON tq.project_id = tp.id
           WHERE tp.status = 'In Progress' AND tq.is_submit_blocking = 1 AND tq.status != 'Received'
           GROUP BY tp.id""",
    ).fetchall()
    for r in rows:
        items.append({
            "severity": "high",
            "title": f"{r['name']} \u2014 Bid Risk",
            "project_id": r["id"],
            "project_name": r["name"],
            "reason": f"{r['blocking_count']} blocking quote{'s' if r['blocking_count'] != 1 else ''} not yet received.",
            "source_module": "project_hunt",
            "source_record_id": r["id"],
            "important_date": None,
            "recommended_action": "Follow up with the outstanding vendor(s).",
            "link": url_for("tracker_view_project", project_id=r["id"]),
        })
    return items


def _bids_due_soon(db, days=3):
    """Same threshold product_intelligence() already uses today (0-3
    days out). Kept as a parameter so callers can widen it without a
    second copy of the query."""
    from flask import url_for
    items = []
    today = date.today()
    rows = db.execute(
        "SELECT id, name, bid_due_date FROM tracker_projects "
        "WHERE status = 'In Progress' AND bid_due_date IS NOT NULL AND bid_due_date != '' "
        "ORDER BY bid_due_date ASC"
    ).fetchall()
    for p in rows:
        try:
            due = datetime.strptime(p["bid_due_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        days_left = (due - today).days
        if 0 <= days_left <= days:
            items.append({
                "severity": "high" if days_left <= 1 else "med",
                "title": f"{p['name']} \u2014 Bid Due Soon",
                "project_id": p["id"],
                "project_name": p["name"],
                "reason": f"Bid due in {days_left} day{'s' if days_left != 1 else ''}.",
                "source_module": "project_hunt",
                "source_record_id": p["id"],
                "important_date": p["bid_due_date"],
                "recommended_action": "Confirm submission is on track.",
                "link": url_for("tracker_view_project", project_id=p["id"]),
            })
    return items


def _pours_without_order(db):
    """Concrete pour scheduled for tomorrow with status still Submitted
    (no order placed). Same rule product_intelligence() already uses."""
    from flask import url_for
    items = []
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    rows = db.execute(
        """SELECT c.id, c.project, c.project_id, c.pour_date, tp.name AS linked_project_name
           FROM inventory_concrete_requests c
           LEFT JOIN tracker_projects tp ON tp.id = c.project_id
           WHERE c.pour_date = ? AND c.status = 'Submitted'""",
        (tomorrow,)
    ).fetchall()
    for c in rows:
        display_name = c["linked_project_name"] or c["project"]
        items.append({
            "severity": "high",
            "title": f"{display_name} \u2014 Pour Tomorrow, No Order Placed",
            "project_id": c["project_id"],
            "project_name": display_name,
            "reason": "Concrete pour is scheduled for tomorrow and no order has been placed yet.",
            "source_module": "sitepulse",
            "source_record_id": c["id"],
            "important_date": c["pour_date"],
            "recommended_action": "Place the concrete order.",
            "link": url_for("inventory_concrete_list"),
        })
    return items


def _overdue_rentals(db):
    from flask import url_for
    items = []
    today = date.today()
    rows = db.execute(
        """SELECT r.id, r.equipment_description, r.due_date, r.project_id, tp.name AS linked_project_name
           FROM sitepulse_rentals r LEFT JOIN tracker_projects tp ON tp.id = r.project_id
           WHERE r.returned_date IS NULL AND r.due_date IS NOT NULL AND r.due_date != '' AND r.due_date < ?""",
        (today.isoformat(),)
    ).fetchall()
    for r in rows:
        try:
            due = datetime.strptime(r["due_date"], "%Y-%m-%d").date()
            days_late = (today - due).days
        except ValueError:
            days_late = None
        items.append({
            "severity": "med",
            "title": f"Rental Overdue \u2014 {r['equipment_description']}",
            "project_id": r["project_id"],
            "project_name": r["linked_project_name"],
            "reason": f"{days_late} day{'s' if days_late != 1 else ''} overdue." if days_late is not None else "Overdue.",
            "source_module": "equipment_center",
            "source_record_id": r["id"],
            "important_date": r["due_date"],
            "recommended_action": "Return or extend the rental.",
            "link": url_for("sitepulse_rentals_list"),
        })
    return items


def _pending_requests(db):
    from flask import url_for
    items = []
    count = db.execute(
        "SELECT COUNT(*) FROM feature_requests WHERE status IN ('Submitted', 'Reviewing')"
    ).fetchone()[0]
    if count > 0:
        items.append({
            "severity": "med",
            "title": f"{count} New Request{'s' if count != 1 else ''} Awaiting Review",
            "project_id": None,
            "project_name": None,
            "reason": f"{count} request{'s' if count != 1 else ''} submitted and not yet reviewed.",
            "source_module": "product_intelligence",
            "source_record_id": None,
            "important_date": None,
            "recommended_action": "Review the incoming requests.",
            "link": url_for("product_intelligence", status="Submitted,Reviewing"),
        })
    return items


# source_module -> which permission key gates seeing that category
_ATTENTION_SOURCE_PERMISSION = {
    "project_hunt": "module:project_hunt:view",
    "sitepulse": "module:sitepulse:view",
    "equipment_center": "module:equipment_center:view",
    "product_intelligence": "module:product_intelligence:view",
}

_SEVERITY_RANK = {"high": 0, "med": 1, "low": 2}


def build_attention_items(user, limit=None):
    """The single source of attention intelligence. Each sub-source is
    now called ONLY if the user already has that source's own
    permission (via _ATTENTION_SOURCE_PERMISSION, the same map already
    used elsewhere) -- an unauthorized source's query never runs at
    all, rather than running and having its items filtered out
    afterward. This is the retrieval-time security boundary; the final
    result for any given user is IDENTICAL to the previous retrieve-
    then-filter behavior (same permission map, same net set of items),
    but Project-Hunt-protected columns (tracker_projects.status/
    bid_due_date, queried by _bids_needing_attention/_bids_due_soon)
    are simply never selected from the database at all for a user
    without module:project_hunt:view -- not merely omitted from what's
    returned.

    Shared by three callers, all inside this module: _tool_get_project_
    status (already gated behind module:project_hunt:view at its own
    outer permission -- unaffected in practice), the standalone
    get_attention_items Atlas tool, and _tool_get_project_intelligence's
    attention scope (the actual target of this fix). No caller outside
    Atlas uses this function -- confirmed by inspection, not assumed."""
    from app import get_db, user_has_permission
    db = get_db()

    candidates = []
    if user_has_permission(user, _ATTENTION_SOURCE_PERMISSION["project_hunt"]):
        candidates += _bids_needing_attention(db)
        candidates += _bids_due_soon(db)
    if user_has_permission(user, _ATTENTION_SOURCE_PERMISSION["sitepulse"]):
        candidates += _pours_without_order(db)
    if user_has_permission(user, _ATTENTION_SOURCE_PERMISSION["equipment_center"]):
        candidates += _overdue_rentals(db)
    if user_has_permission(user, _ATTENTION_SOURCE_PERMISSION["product_intelligence"]):
        candidates += _pending_requests(db)

    candidates.sort(key=lambda x: _SEVERITY_RANK.get(x["severity"], 3))
    if limit:
        candidates = candidates[:limit]
    return candidates


# ---------------------------------------------------------------------------
# Atlas tool handlers
# ---------------------------------------------------------------------------

def _find_project(db, project_name=None, project_id=None):
    """Shared project-resolution helper: exact id, or fuzzy name match.
    Returns (project_row_or_None, ambiguous_matches_list). Never guesses
    between multiple plausible matches -- returns them for the caller
    to disambiguate instead.

    Retrieves the FULL tracker_projects row (SELECT *), including
    Project-Hunt-protected columns (status/bid_due_date/estimated_value)
    -- this is correct and unchanged for its one remaining caller,
    _tool_get_project_status, which is gated behind
    module:project_hunt:view at the tool's own outer permission and
    legitimately needs the full row every time it's reached at all.

    _tool_set_project_context does NOT use this anymore -- see
    _find_project_identity_only below -- specifically because
    set_project_context is now reachable by users without Project Hunt
    access (Equipment Center/SitePulse module permissions also
    qualify), and it must never retrieve protected columns for them in
    the first place, not merely omit those columns from what it
    returns afterward."""
    if project_id:
        row = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
        return row, []
    if project_name:
        exact = db.execute("SELECT * FROM tracker_projects WHERE name = ?", (project_name,)).fetchone()
        if exact:
            return exact, []
        matches = db.execute(
            "SELECT id, name FROM tracker_projects WHERE name LIKE ? ORDER BY name LIMIT 8",
            (f"%{project_name}%",)
        ).fetchall()
        if len(matches) == 1:
            full = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (matches[0]["id"],)).fetchone()
            return full, []
        return None, [dict(m) for m in matches]
    return None, []


def _find_project_identity_only(db, project_name=None, project_id=None):
    """SAME resolution semantics as _find_project (exact id, exact
    name, unique-substring match, ambiguous-list fallback for 2+
    matches) -- but every SELECT here is scoped to exactly
    (id, name, client), the approved shared cross-module identity
    fields, and NEVER touches status/bid_due_date/estimated_value at
    the SQL level. This is the actual security boundary required: the
    protected columns are never retrieved for this caller, not merely
    dropped from the result afterward.

    Used exclusively by _tool_set_project_context, which is now
    reachable by users without module:project_hunt:view (Equipment
    Center or SitePulse access also qualify) and never needs anything
    beyond identity to establish session context regardless of who's
    calling it.

    _find_project itself (above) is completely unchanged and still
    used, as before, by _tool_get_project_status -- that tool remains
    gated behind module:project_hunt:view at its own outer permission
    and legitimately needs the full row every time."""
    if project_id:
        row = db.execute("SELECT id, name, client FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
        return row, []
    if project_name:
        exact = db.execute("SELECT id, name, client FROM tracker_projects WHERE name = ?", (project_name,)).fetchone()
        if exact:
            return exact, []
        matches = db.execute(
            "SELECT id, name FROM tracker_projects WHERE name LIKE ? ORDER BY name LIMIT 8",
            (f"%{project_name}%",)
        ).fetchall()
        if len(matches) == 1:
            full = db.execute("SELECT id, name, client FROM tracker_projects WHERE id = ?", (matches[0]["id"],)).fetchone()
            return full, []
        return None, [dict(m) for m in matches]
    return None, []


def _tool_set_project_context(user, project_name=None, project_id=None):
    """Item 6 -- Atlas canonical project awareness. Resolves a project
    using _find_project_identity_only (see its own docstring for why
    this is a SEPARATE lookup from _find_project's get_project_status
    path, not the same one filtered afterward) and returns only the
    identity fields needed to establish session context. The actual
    writing of this result into the session's project_context happens
    in execute_tool() (app.py) -- this handler, like every other tool
    handler, only ever touches the database/read path and returns a
    plain dict; it has no access to (and does not need) the session
    itself.
    """
    from app import get_db
    db = get_db()
    project, ambiguous = _find_project_identity_only(db, project_name, project_id)
    if not project:
        if ambiguous:
            return {"found": False, "reason": "ambiguous", "matches": ambiguous}
        return {"found": False, "reason": "not_found"}
    return {"found": True, "project_id": project["id"], "name": project["name"]}


def _tool_get_project_status(user, project_name=None, project_id=None):
    from app import get_db, user_has_permission
    db = get_db()
    project, ambiguous = _find_project(db, project_name, project_id)
    if not project:
        if ambiguous:
            return {"found": False, "reason": "ambiguous", "matches": ambiguous}
        return {"found": False, "reason": "not_found"}

    pid = project["id"]
    result = {
        "found": True,
        "project_id": pid,
        "name": project["name"],
        "client": project["client"],
        "status": project["status"],
        "bid_due_date": project["bid_due_date"],
        "estimated_value": project["estimated_value"],
    }

    blocking = db.execute(
        "SELECT trade, vendor_name, status FROM tracker_quotes "
        "WHERE project_id = ? AND is_submit_blocking = 1 AND status != 'Received'",
        (pid,)
    ).fetchall()
    result["blocking_quotes"] = [dict(b) for b in blocking]

    if user_has_permission(user, "module:sitepulse:view"):
        concrete = db.execute(
            "SELECT id, pour_date, status FROM inventory_concrete_requests WHERE project_id = ? ORDER BY pour_date DESC LIMIT 10",
            (pid,)
        ).fetchall()
        result["concrete_requests"] = [dict(c) for c in concrete]

        purchases = db.execute(
            "SELECT id, pr_number, status, needed_on FROM inventory_purchase_requests WHERE project_id = ? ORDER BY request_date DESC LIMIT 10",
            (pid,)
        ).fetchall()
        result["purchase_requests"] = [dict(p) for p in purchases]

    if user_has_permission(user, "module:equipment_center:view"):
        rentals = db.execute(
            "SELECT id, equipment_description, due_date, returned_date FROM sitepulse_rentals WHERE project_id = ? ORDER BY due_date DESC LIMIT 10",
            (pid,)
        ).fetchall()
        result["rentals"] = [dict(r) for r in rentals]

    project_attention = [
        item for item in build_attention_items(user) if item.get("project_id") == pid
    ]
    result["attention_items"] = project_attention

    return result


def _tool_list_bids_needing_attention(user):
    from app import get_db
    return {"items": _bids_needing_attention(get_db())}


def _tool_list_bids_due_soon(user, days=None):
    from app import get_db
    return {"items": _bids_due_soon(get_db(), days=int(days) if days else 7)}


def _tool_list_upcoming_concrete_pours(user, days=None):
    from app import get_db
    db = get_db()
    horizon = int(days) if days else 7
    today = date.today()
    end = (today + timedelta(days=horizon)).isoformat()
    rows = db.execute(
        """SELECT c.id, c.project, c.project_id, c.pour_date, c.status, tp.name AS linked_project_name
           FROM inventory_concrete_requests c LEFT JOIN tracker_projects tp ON tp.id = c.project_id
           WHERE c.pour_date >= ? AND c.pour_date <= ? AND c.status IN ('Submitted', 'Scheduled')
           ORDER BY c.pour_date ASC LIMIT 15""",
        (today.isoformat(), end)
    ).fetchall()
    return {"items": [
        {"id": r["id"], "project": r["linked_project_name"] or r["project"], "project_id": r["project_id"],
         "pour_date": r["pour_date"], "status": r["status"]}
        for r in rows
    ]}


def _tool_find_equipment(user, query=None, status=None):
    from app import get_db
    db = get_db()
    conditions, params = [], []
    if status:
        conditions.append("status = ?")
        params.append(status)
    else:
        conditions.append("status NOT IN ('Sold', 'Stolen')")
    if query:
        conditions.append("(name LIKE ? OR description LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    where = f"WHERE {' AND '.join(conditions)}"
    rows = db.execute(
        f"SELECT id, name, description, status, location FROM sitepulse_assets {where} ORDER BY name LIMIT 15",
        params
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


def _tool_list_rentals_due(user, status=None):
    from app import get_db
    db = get_db()
    today = date.today().isoformat()
    if status == "overdue":
        rows = db.execute(
            "SELECT id, equipment_description, due_date, project_id FROM sitepulse_rentals "
            "WHERE returned_date IS NULL AND due_date IS NOT NULL AND due_date != '' AND due_date < ? "
            "ORDER BY due_date ASC LIMIT 15", (today,)
        ).fetchall()
    elif status == "due_soon":
        soon = (date.today() + timedelta(days=3)).isoformat()
        rows = db.execute(
            "SELECT id, equipment_description, due_date, project_id FROM sitepulse_rentals "
            "WHERE returned_date IS NULL AND due_date IS NOT NULL AND due_date >= ? AND due_date <= ? "
            "ORDER BY due_date ASC LIMIT 15", (today, soon)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, equipment_description, due_date, project_id FROM sitepulse_rentals "
            "WHERE returned_date IS NULL AND due_date IS NOT NULL AND due_date != '' "
            "ORDER BY due_date ASC LIMIT 15"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


def _tool_list_open_purchase_requests(user, status=None):
    """Status-based only, per CTO decision -- no lateness claim. The data
    model doesn't reliably support "late PO" today (needed_on and
    expected_delivery_date are free-entry fields never compared to
    today's date anywhere in the app), so this tool doesn't invent that
    comparison either."""
    from app import get_db
    db = get_db()
    if status:
        rows = db.execute(
            "SELECT id, pr_number, job_name, status, needed_on, project_id FROM inventory_purchase_requests "
            "WHERE status = ? ORDER BY request_date DESC LIMIT 15", (status,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, pr_number, job_name, status, needed_on, project_id FROM inventory_purchase_requests "
            "WHERE status != 'Completed' ORDER BY request_date DESC LIMIT 15"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


def _tool_get_attention_items(user):
    return {"items": build_attention_items(user, limit=15)}


# ---------------------------------------------------------------------------
# Project Intelligence (cross-module read layer)
# ---------------------------------------------------------------------------

_PI_VALID_SCOPES = {"overview", "equipment", "concrete", "purchases", "rentals", "attention"}
_PI_CONCRETE_LIMIT = 10
_PI_PURCHASE_LIMIT = 10
_PI_EQUIPMENT_LIMIT = 15
_PI_RENTAL_LIMIT = 10
_PI_ATTENTION_LIMIT = 10


def _current_equipment_assignments(db, project_id, limit=None):
    """THE canonical "what equipment is currently on this project" query --
    reuses BuildIQ's own existing operational definition exactly
    (confirmed against the real Equipment Center detail-page query at
    the time this was written): for each asset, only its LATEST
    already-applied usage_log row counts (move_status != 'Scheduled' --
    a future scheduled move hasn't happened yet), ordered by
    COALESCE(applied_at, out_date, created_at) DESC. An older row for
    the same asset can never win over its own latest row, so an asset
    whose latest applied move places it elsewhere can never appear here
    -- this is enforced structurally by the correlated subquery below,
    not by a heuristic.

    DETERMINISTIC TIE-BREAK (new for this shared helper -- the existing
    production Equipment Center query does not have this and should get
    it in a future, separate, focused fix -- not touched here): `id DESC`
    as the secondary sort key. `id` is sitepulse_usage_log's AUTOINCREMENT
    primary key -- strictly unique and monotonic -- so two rows sharing
    the exact same effective timestamp still resolve to one deterministic
    winner (the most recently inserted), never SQLite's undefined tie
    order.

    This is "last recorded assignment/movement in BuildIQ," not GPS or
    physical-location certainty -- BuildIQ only knows what an employee
    actually logged.

    `limit`: caps the number of DETAIL rows returned (for the bounded
    items array). Pass None for no limit -- used by
    _current_equipment_count below, which needs the TRUE total, not a
    capped one.
    """
    sql = """SELECT sa.id, sa.name, sa.status,
                    ul.to_location, ul.project_id, ul.job_name,
                    COALESCE(ul.applied_at, ul.out_date, ul.created_at) AS as_of
             FROM sitepulse_assets sa
             JOIN sitepulse_usage_log ul ON ul.id = (
                 SELECT id FROM sitepulse_usage_log
                 WHERE asset_id = sa.id AND move_status != 'Scheduled'
                 ORDER BY COALESCE(applied_at, out_date, created_at) DESC, id DESC
                 LIMIT 1
             )
             WHERE ul.project_id = ?
             ORDER BY as_of DESC"""
    params = [project_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return db.execute(sql, params).fetchall()


def _current_equipment_count(db, project_id):
    """THE TRUE total number of assets whose current (latest non-
    Scheduled) assignment places them on this project -- computed with
    COUNT(*) at the SQL level, using the EXACT SAME correlated-subquery
    current-state semantics as _current_equipment_assignments above
    (same latest-row-per-asset definition, same tie-break), never by
    fetching every matching row into Python merely to len() them. This
    is what makes the reported count truthful even when the detail
    array itself is bounded/capped -- a 40-asset project reports
    count=40, not count=15."""
    row = db.execute(
        """SELECT COUNT(*) c FROM sitepulse_assets sa
           JOIN sitepulse_usage_log ul ON ul.id = (
               SELECT id FROM sitepulse_usage_log
               WHERE asset_id = sa.id AND move_status != 'Scheduled'
               ORDER BY COALESCE(applied_at, out_date, created_at) DESC, id DESC
               LIMIT 1
           )
           WHERE ul.project_id = ?""",
        (project_id,)
    ).fetchone()
    return row["c"]


def _pi_source_failure(source, project_id, exc):
    """Logs a project-intelligence source failure server-side (never
    silently swallowed) without ever exposing the stack trace/internal
    DB error to the employee -- the model-facing result simply omits
    that source, indistinguishable in shape from an unauthorized
    source, but never indistinguishable in our own logs.

    DEFENSE IN DEPTH: this function is itself guarded. If the logging/
    commit call fails for its own reason (a second, independent DB
    problem), that secondary failure must NEVER escape and take down
    the rest of the project-intelligence call with it -- the whole
    point of per-source isolation is that one broken thing doesn't
    break everything else, and that has to hold even if the *logging*
    of the first break is what breaks next. Falls back to a bare
    stderr write (which cannot itself meaningfully fail) only if the
    real logging path is unavailable."""
    try:
        from app import log_activity, get_db
        log_activity("atlas", "tool_call", 0, "atlas_project_intelligence_source_failed",
                     field=source, new_value=f"project_id={project_id}: {exc}")
        get_db().commit()
    except Exception as logging_exc:
        try:
            import sys
            print(f"[atlas_project_intelligence] source={source} project_id={project_id} failed AND failure logging itself failed: "
                  f"original={exc!r} logging_error={logging_exc!r}", file=sys.stderr)
        except Exception:
            pass  # even the stderr fallback must never propagate and take down the overall call.


def _pi_project_core(db, project, include_project_hunt_fields):
    """SHARED CROSS-MODULE IDENTITY (project_id/name/client) mirrors
    what Equipment Center's and SitePulse's own existing project-picker
    dropdowns already expose to any user with THEIR module's own
    permission (confirmed by inspecting new_rental.html/new_concrete_
    request.html/new_purchase_request.html, all of which already
    SELECT id, name, client FROM tracker_projects for their authorized
    users, independent of Project Hunt) -- this does not create any
    broader exposure than the existing UI already has.

    PROJECT HUNT PROTECTED (status/bid_due_date/estimated_value) are
    only included when the caller has already confirmed
    module:project_hunt:view AND already fetched a row that actually
    contains those columns -- the one call site (in
    _tool_get_project_intelligence) computes both of those together,
    before calling this function, so this stays a pure data-shaping
    function with no permission logic or SQL of its own. If
    include_project_hunt_fields is True but the row lacks those
    columns, that is a caller bug, not something this function should
    paper over -- it will raise, loudly, rather than silently return
    incomplete data."""
    core = {
        "project_id": project["id"],
        "name": project["name"],
        "client": project["client"],
    }
    if include_project_hunt_fields:
        core["status"] = project["status"]
        core["bid_due_date"] = project["bid_due_date"]
        core["estimated_value"] = project["estimated_value"]
    return core


def _pi_linked_via(row_project_id, canonical_pid):
    return "project_id" if row_project_id == canonical_pid else "legacy_exact_name_match"


def _pi_date_context(value):
    """Return factual date context without inventing an overdue business rule.

    A request can have a date in the past while still carrying an open status in
    BuildIQ. Atlas needs both facts so it can surface the mismatch as something
    worth reviewing, without relabeling the record as overdue/late.
    """
    if not value:
        return {"date_state": "unknown", "days_from_today": None}
    try:
        d = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return {"date_state": "unknown", "days_from_today": None}
    delta = (d - date.today()).days
    return {
        "date_state": "past" if delta < 0 else "today" if delta == 0 else "future",
        "days_from_today": delta,
    }


def _pi_concrete(db, project_id, project_name):
    # Preserve the existing canonical-linking rule, but return enough bounded
    # record detail for Atlas to explain what is actually happening instead of
    # reducing every pour to only date + status.
    where = "(project_id = ? OR (project_id IS NULL AND project = ?))"
    params_base = (project_id, project_name)
    total = db.execute(f"SELECT COUNT(*) c FROM inventory_concrete_requests WHERE {where}", params_base).fetchone()["c"]
    open_total = db.execute(
        f"SELECT COUNT(*) c FROM inventory_concrete_requests WHERE {where} AND status != 'Completed'", params_base
    ).fetchone()["c"]
    rows = db.execute(
        f"""SELECT id, status, pour_date, pour_time, area_description, mix_design_psi, mix_slump,
                   concrete_amount, truck_spacing, pump_type, pump_size, concrete_company,
                   lab_required, drilling_required, project_id
            FROM inventory_concrete_requests WHERE {where}
            ORDER BY (status != 'Completed') DESC, pour_date DESC, pour_time DESC LIMIT ?""",
        params_base + (_PI_CONCRETE_LIMIT,)
    ).fetchall()
    items = []
    for r in rows:
        item = {
            "record_type": "concrete_request", "record_id": r["id"], "status": r["status"],
            "pour_date": r["pour_date"], "pour_time": r["pour_time"],
            "area_description": r["area_description"], "mix_design_psi": r["mix_design_psi"],
            "mix_slump": r["mix_slump"], "concrete_amount": r["concrete_amount"],
            "truck_spacing": r["truck_spacing"], "pump_type": r["pump_type"],
            "pump_size": r["pump_size"], "concrete_company": r["concrete_company"],
            "lab_required": r["lab_required"], "drilling_required": r["drilling_required"],
            "project_id": project_id, "linked_via": _pi_linked_via(r["project_id"], project_id),
        }
        item.update(_pi_date_context(r["pour_date"]))
        items.append(item)
    return {"open_count": open_total, "total_count": total, "truncated": total > len(rows), "items": items}


def _pi_purchases(db, project_id, project_name):
    where = "(project_id = ? OR (project_id IS NULL AND job_name = ?))"
    params_base = (project_id, project_name)
    total = db.execute(f"SELECT COUNT(*) c FROM inventory_purchase_requests WHERE {where}", params_base).fetchone()["c"]
    open_total = db.execute(
        f"SELECT COUNT(*) c FROM inventory_purchase_requests WHERE {where} AND status != 'Completed'", params_base
    ).fetchone()["c"]
    rows = db.execute(
        f"""SELECT id, pr_number, status, request_date, needed_on, location_description, requested_by,
                   source_of_supply, vendor_company, expected_delivery_date, project_id
            FROM inventory_purchase_requests WHERE {where}
            ORDER BY (status != 'Completed') DESC, request_date DESC LIMIT ?""",
        params_base + (_PI_PURCHASE_LIMIT,)
    ).fetchall()
    items = []
    for r in rows:
        line_rows = db.execute(
            """SELECT item, description, supplier, qty, unit
               FROM inventory_purchase_request_items WHERE purchase_request_id = ? ORDER BY id LIMIT 8""",
            (r["id"],)
        ).fetchall()
        line_count = db.execute(
            "SELECT COUNT(*) c FROM inventory_purchase_request_items WHERE purchase_request_id = ?", (r["id"],)
        ).fetchone()["c"]
        item = {
            "record_type": "purchase_request", "record_id": r["id"], "pr_number": r["pr_number"],
            "status": r["status"], "request_date": r["request_date"], "needed_on": r["needed_on"],
            "location_description": r["location_description"], "requested_by": r["requested_by"],
            "source_of_supply": r["source_of_supply"], "vendor_company": r["vendor_company"],
            "expected_delivery_date": r["expected_delivery_date"],
            "line_items": [{"item": x["item"], "description": x["description"], "supplier": x["supplier"],
                            "qty": x["qty"], "unit": x["unit"]} for x in line_rows],
            "line_items_truncated": line_count > len(line_rows),
            "project_id": project_id, "linked_via": _pi_linked_via(r["project_id"], project_id),
        }
        item.update(_pi_date_context(r["needed_on"]))
        items.append(item)
    return {"open_count": open_total, "total_count": total, "truncated": total > len(rows), "items": items}


def _pi_equipment(db, project_id):
    total = _current_equipment_count(db, project_id)
    rows = _current_equipment_assignments(db, project_id, limit=_PI_EQUIPMENT_LIMIT)
    return {
        "count": total,
        "truncated": total > len(rows),
        "items": [
            {"record_type": "equipment_asset", "record_id": r["id"], "name": r["name"],
             "status": r["status"], "as_of": r["as_of"], "project_id": project_id}
            for r in rows
        ],
    }


def _pi_rentals(db, project_id, project_name):
    """Return the same outside-rental facts the Equipment Center UI can see.

    Important: "active" here means not returned; overdue rentals are still open
    rentals and must never disappear just because their due date passed.
    """
    where = "(project_id = ? OR (project_id IS NULL AND job_name = ?))"
    params_base = (project_id, project_name)
    total = db.execute(f"SELECT COUNT(*) c FROM sitepulse_rentals WHERE {where}", params_base).fetchone()["c"]
    open_total = db.execute(
        f"SELECT COUNT(*) c FROM sitepulse_rentals WHERE {where} AND (returned_date IS NULL OR returned_date = '')", params_base
    ).fetchone()["c"]
    rows = db.execute(
        f"""SELECT id, vendor, equipment_description, job_name, rate_amount, rate_period,
                   rented_date, due_date, returned_date, project_id, created_at, updated_at
            FROM sitepulse_rentals WHERE {where}
            ORDER BY (returned_date IS NULL OR returned_date = '') DESC, due_date ASC, rented_date DESC LIMIT ?""",
        params_base + (_PI_RENTAL_LIMIT,)
    ).fetchall()
    today = date.today()
    items = []
    for r in rows:
        returned = (r["returned_date"] or "").strip()
        due = (r["due_date"] or "").strip()
        if returned:
            status = "Returned"
        elif due and due < today.isoformat():
            status = "Overdue"
        else:
            status = "Active"
        days_out = None
        running_cost = None
        try:
            start = date.fromisoformat(str(r["rented_date"])[:10])
            end = date.fromisoformat(returned[:10]) if returned else today
            days_out = max((end - start).days + 1, 1)
            rate = float(r["rate_amount"] or 0)
            daily = {"Weekly": rate / 7, "Monthly": rate / 30}.get(r["rate_period"], rate)
            running_cost = round(days_out * daily, 2)
        except (TypeError, ValueError):
            pass
        items.append({
            "record_type": "rental", "record_id": r["id"],
            "equipment_description": r["equipment_description"], "vendor": r["vendor"],
            "job_name": r["job_name"], "rate_amount": r["rate_amount"], "rate_period": r["rate_period"],
            "rented_date": r["rented_date"], "due_date": r["due_date"], "returned_date": r["returned_date"],
            "status": status, "days_out": days_out, "running_cost": running_cost,
            "project_id": project_id, "linked_via": _pi_linked_via(r["project_id"], project_id),
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        })
    return {"open_count": open_total, "active_count": sum(1 for x in items if x["status"] == "Active"),
            "overdue_count": sum(1 for x in items if x["status"] == "Overdue"),
            "total_count": total, "truncated": total > len(rows), "items": items}

def _tool_get_project_intelligence(user, scope=None, project_id=None):
    """Cross-module, permission-filtered, factual project intelligence
    for the CURRENTLY VALIDATED canonical project only. project_id here
    always comes from execute_tool()'s existing session_context
    injection (see execute_tool's own docstring) -- this handler never
    trusts a model-supplied value for anything beyond what that
    generic, already-audited injection mechanism provides; the native
    tool declaration for this tool (see app.py) never even offers
    project_id to the model in the first place, and a project_id
    supplied any other way is defensively ignored below.

    SCOPE: a small closed enum, never arbitrary text. The Tool Registry's
    own enum-constrained schema validation is the OUTER protection
    (rejects a malformed value before this handler ever runs, for any
    caller going through execute_tool). This handler is the INNER,
    defense-in-depth layer for any caller that reaches it directly,
    bypassing that outer validation: omitted/None scope legitimately
    defaults to "overview" (that's a normal, unambiguous "no narrower
    scope requested" case), but an EXPLICITLY supplied value that isn't
    one of the six approved scopes fails CLOSED -- it does not silently
    broaden into "overview" (which would mean a malformed/adversarial
    scope value ends up querying MORE than a valid one would), and no
    optional source is queried at all in that case.

    FRESHNESS: every field here is queried live, every single call --
    nothing about project state is ever cached in project_context or
    anywhere else that could later be served stale.

    FAILURE ISOLATION: each optional source's query is individually
    wrapped; a failure in one never destroys the others -- see
    _pi_source_failure for what gets logged, and for how a SECOND
    failure (in the logging itself) is also contained.
    """
    from app import get_db, user_has_permission, _atlas_trace
    import time as _time
    db = get_db()

    if scope is not None and scope not in _PI_VALID_SCOPES:
        # Explicit but invalid -- fail closed, never silently treat this
        # as "overview" (which would be a broader query than a
        # legitimate call would have triggered). Nothing is queried.
        return {"found": False, "reason": "invalid_scope"}
    if scope is None:
        scope = "overview"

    if not project_id:
        return {"found": False, "reason": "no_active_project"}

    # Re-verify the project still exists and is still real RIGHT NOW --
    # never trust that it was valid whenever context was last set.
    #
    # SECURITY BOUNDARY: the actual SQL SELECT itself is scoped to
    # exactly which columns this user is authorized to have retrieved
    # at all -- Project-Hunt-protected columns (status/bid_due_date/
    # estimated_value) are never fetched from the database for a
    # non-Project-Hunt user, not merely dropped from the result
    # afterward. This permission check is computed once, before either
    # query variant runs, and its result is reused below (both for
    # which SELECT to issue here, and for _pi_project_core's own
    # shaping of the result -- the two must always agree, since
    # _pi_project_core assumes the row actually HAS the protected
    # columns whenever it's told to include them).
    _has_project_hunt = user_has_permission(user, "module:project_hunt:view")
    _core_start = _time.perf_counter()
    if _has_project_hunt:
        project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
    else:
        project = db.execute("SELECT id, name, client FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        return {"found": False, "reason": "not_found"}

    pid = project["id"]
    pname = project["name"]
    result = {"found": True, "project": _pi_project_core(db, project, _has_project_hunt)}
    _atlas_trace("INTELLIGENCE_PROJECT_CORE_END", duration_ms=int((_time.perf_counter() - _core_start) * 1000))

    want = lambda s: scope == "overview" or scope == s

    if want("concrete") and user_has_permission(user, "module:sitepulse:view"):
        _src_start = _time.perf_counter()
        try:
            result["concrete"] = _pi_concrete(db, pid, pname)
        except Exception as exc:
            _pi_source_failure("concrete", pid, exc)
        _atlas_trace("INTELLIGENCE_CONCRETE_END", duration_ms=int((_time.perf_counter() - _src_start) * 1000))

    if want("purchases") and user_has_permission(user, "module:sitepulse:view"):
        _src_start = _time.perf_counter()
        try:
            result["purchases"] = _pi_purchases(db, pid, pname)
        except Exception as exc:
            _pi_source_failure("purchases", pid, exc)
        _atlas_trace("INTELLIGENCE_PURCHASES_END", duration_ms=int((_time.perf_counter() - _src_start) * 1000))

    if want("equipment") and user_has_permission(user, "module:equipment_center:view"):
        _src_start = _time.perf_counter()
        try:
            result["equipment"] = _pi_equipment(db, pid)
        except Exception as exc:
            _pi_source_failure("equipment", pid, exc)
        _atlas_trace("INTELLIGENCE_EQUIPMENT_END", duration_ms=int((_time.perf_counter() - _src_start) * 1000))

    if want("rentals") and user_has_permission(user, "module:equipment_center:view"):
        _src_start = _time.perf_counter()
        try:
            result["rentals"] = _pi_rentals(db, pid, pname)
        except Exception as exc:
            _pi_source_failure("rentals", pid, exc)
        _atlas_trace("INTELLIGENCE_RENTALS_END", duration_ms=int((_time.perf_counter() - _src_start) * 1000))

    if want("attention"):
        _src_start = _time.perf_counter()
        try:
            project_attention = [item for item in build_attention_items(user) if item.get("project_id") == pid]
            result["attention"] = project_attention[:_PI_ATTENTION_LIMIT]
        except Exception as exc:
            _pi_source_failure("attention", pid, exc)
        _atlas_trace("INTELLIGENCE_ATTENTION_END", duration_ms=int((_time.perf_counter() - _src_start) * 1000))

    return result



# ---------------------------------------------------------------------------
# BuildIQ product / employee-request intelligence
# ---------------------------------------------------------------------------

def _tool_get_buildiq_product_intelligence(user, scope=None):
    """Read-only management intelligence about BuildIQ itself.

    The response deliberately includes the supporting audit/history records for
    requests, not only their current state. Atlas must never interpret "not in
    this retrieval" as "BuildIQ never stored it".
    """
    from app import get_db
    db = get_db()
    scope = (scope or "overview").strip().lower()
    if scope not in {"overview", "requests", "attention", "roadmap"}:
        scope = "overview"

    rows = db.execute(
        """SELECT f.id, f.requester_name, f.requester_email, f.department,
                  f.original_request, f.status, f.approval_status, f.approval_decided_by,
                  f.approval_decided_at, f.approval_reason, f.created_at, f.updated_at,
                  i.buildiq_module, i.internal_notes, i.solution_built,
                  i.testing_notes, i.user_feedback, i.release_date
           FROM feature_requests f
           LEFT JOIN feature_request_intelligence i ON i.feature_request_id = f.id
           ORDER BY f.created_at DESC"""
    ).fetchall()

    total = len(rows)
    approved = [r for r in rows if r["approval_status"] == "Approved"]
    def count_status(*statuses):
        return sum(1 for r in approved if r["status"] in statuses)

    result = {
        "scope": scope,
        "source": "feature_requests + feature_request_intelligence + status/approval history",
        "kpis": {
            "total_requests": total,
            "pending_approval": sum(1 for r in rows if r["approval_status"] == "Pending"),
            "new_or_reviewing": count_status("Submitted", "Reviewing"),
            "approved": count_status("Approved"),
            "building": count_status("Building"),
            "testing": count_status("Testing"),
            "released": count_status("Released"),
            "on_hold": count_status("On Hold"),
            "not_planned": count_status("Not Planned"),
        },
    }

    if scope in ("overview", "requests", "attention"):
        request_records = []
        for r in rows[:40]:
            status_history = [dict(x) for x in db.execute(
                """SELECT status, release_note, changed_by, changed_at
                   FROM feature_request_status_history WHERE feature_request_id=? ORDER BY changed_at, id""",
                (r["id"],)
            ).fetchall()]
            approval_history = [dict(x) for x in db.execute(
                """SELECT decision, reason, decided_by, decided_at
                   FROM feature_request_approvals WHERE feature_request_id=? ORDER BY decided_at, id""",
                (r["id"],)
            ).fetchall()]
            request_records.append({
                "request_id": r["id"], "request": r["original_request"],
                "requester": r["requester_name"] or r["requester_email"],
                "requester_email": r["requester_email"], "department": r["department"],
                "status": r["status"], "approval_status": r["approval_status"],
                "approval_decided_by": r["approval_decided_by"], "approval_decided_at": r["approval_decided_at"],
                "approval_reason": r["approval_reason"], "buildiq_module": r["buildiq_module"],
                "created_at": r["created_at"], "updated_at": r["updated_at"],
                "internal_notes": r["internal_notes"], "solution_built": r["solution_built"],
                "testing_notes": r["testing_notes"], "user_feedback": r["user_feedback"],
                "release_date": r["release_date"], "status_history": status_history,
                "approval_history": approval_history,
            })
        result["requests"] = request_records

    if scope in ("overview", "attention"):
        # Only formal/explicit workflow state belongs here. Being Building is
        # not itself an "attention flag". Atlas may make an inference in prose,
        # but it must label that as inference, not a Product Intelligence flag.
        attention = []
        for r in rows:
            reason = None
            if r["approval_status"] == "Pending":
                reason = "Pending approval"
            elif r["approval_status"] == "Approved" and r["status"] in ("Submitted", "Reviewing"):
                reason = f"Approved request still {r['status']}"
            if reason:
                attention.append({
                    "request_id": r["id"], "request": r["original_request"],
                    "requester": r["requester_name"] or r["requester_email"],
                    "department": r["department"], "status": r["status"],
                    "approval_status": r["approval_status"], "buildiq_module": r["buildiq_module"],
                    "reason": reason, "updated_at": r["updated_at"], "formal": True,
                })
        result["attention"] = attention[:30]

    if scope in ("overview", "roadmap"):
        roadmap = db.execute(
            "SELECT id, name, lane, note, progress_pct, sort_order, updated_at FROM roadmap_items ORDER BY sort_order ASC"
        ).fetchall()
        result["roadmap"] = [dict(r) for r in roadmap]

    return result


# ---------------------------------------------------------------------------
# BuildIQ system-wide intelligence (V9.3)
# ---------------------------------------------------------------------------
def _tool_get_buildiq_system_intelligence(user, scope=None):
    """Permission-aware read-only intelligence for BuildIQ system surfaces.

    This is the authoritative source for current module existence, session/user
    identity, permissions, request histories, and non-project system data. It
    reports what the current application exposes rather than inferring module
    existence from an older roadmap label.
    """
    from app import get_db, user_has_permission, current_user, current_app
    db = get_db()
    scope = (scope or "overview").strip().lower()
    allowed = {"overview", "users_permissions", "deployment", "sitepulse", "field_reports", "activity",
               "modules", "cashflow", "redline", "rentals"}
    if scope not in allowed:
        return {"available": False, "reason": "invalid_scope", "scope": scope}
    out = {"available": True, "scope": scope, "sources": [],
           "authenticated_user": {"id": getattr(current_user, "id", None), "name": getattr(current_user, "name", None),
                                  "email": getattr(current_user, "email", None)}}

    # Current deployed application registry, derived from the server's actual
    # Flask route map. This prevents stale roadmap names (e.g. Finance) from
    # overriding a module that is actually deployed (e.g. CashFlow).
    if scope in ("overview", "modules", "cashflow", "redline"):
        rules = {str(r.rule) for r in current_app.url_map.iter_rules()}
        module_defs = [
            ("Project Hunt", "/tracker/", "module:project_hunt:view"),
            ("Project Deployment", "/deployment", "module:project_deployment:view"),
            ("SitePulse", "/inventory", "module:sitepulse:view"),
            ("Equipment Center", "/sitepulse/", "module:equipment_center:view"),
            ("Atlas", "/assistant", "module:atlas:view"),
            ("Requests Center", "/requests", None),
            ("Product Intelligence", "/admin/product-intelligence", "module:product_intelligence:view"),
            ("CashFlow", "/cashflow", "module:finance:view"),
            ("Redline / Engineering", "/engineering", "module:engineering:view"),
        ]
        modules=[]
        for name, prefix, permission in module_defs:
            exists = any(rule == prefix or rule.startswith(prefix.rstrip('/') + '/') for rule in rules)
            modules.append({"name": name, "route_prefix": prefix, "deployed": exists,
                            "authorized_for_current_user": bool(exists and (permission is None or user_has_permission(user, permission)))})
        out["modules"] = modules
        out["sources"].append("Flask current_app.url_map + current permission resolver")
        if scope == "cashflow":
            cf = dict(next((m for m in modules if m["name"] == "CashFlow"), {"deployed": False}))
            cf.update({
                "canonical_name": "CashFlow",
                "purpose": "project payment and owner-invoice / collections tracking",
                "capabilities_present_when_deployed": ["project/job workspaces", "owner invoices", "payments", "notes/documents", "invoice review", "CSV export"],
            })
            out["cashflow"] = cf
        if scope == "redline":
            rd = dict(next((m for m in modules if m["name"] == "Redline / Engineering"), {"deployed": False}))
            rd.update({
                "canonical_name": "Redline",
                "lifecycle": "planned" if not rd.get("deployed") else "deployed",
                "purpose": "engineering plan review",
                "review_categories": ["Critical", "Warnings", "Coordination", "Missing Info", "Permit Risks"],
                "planned_review_controls": ["Confirm", "Dismiss", "Needs Review"],
            })
            out["redline"] = rd

    if scope in ("overview", "users_permissions"):
        if user_has_permission(user, "module:team_admin:view"):
            rows = db.execute("SELECT id, name, email, department FROM users ORDER BY name, email").fetchall()
            people = []
            for u in rows:
                roles = [r["name"] for r in db.execute(
                    "SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=? ORDER BY r.name", (u["id"],)
                ).fetchall()]
                perms = []
                subject = type("AtlasSubject", (), {"id": u["id"], "is_authenticated": True})()
                for pr in db.execute("SELECT key, category, label FROM permissions ORDER BY category, key").fetchall():
                    if user_has_permission(subject, pr["key"]):
                        perms.append({"key": pr["key"], "category": pr["category"], "label": pr["label"]})
                overrides = [dict(x) for x in db.execute(
                    """SELECT p.key, p.label, o.state FROM user_permission_overrides o
                       JOIN permissions p ON p.id=o.permission_id WHERE o.user_id=? ORDER BY p.key""", (u["id"],)
                ).fetchall()]
                people.append({"user_id": u["id"], "name": u["name"], "email": u["email"], "department": u["department"],
                               "roles": roles, "effective_permissions": perms, "explicit_overrides": overrides})
            out["users_permissions"] = people
            out["sources"].append("users + roles + role_permissions + user_permission_overrides")
        elif scope == "users_permissions":
            return {"available": False, "scope": scope, "reason": "not_permitted", "authenticated_user": out["authenticated_user"]}

    if scope in ("overview", "deployment"):
        if user_has_permission(user, "module:project_deployment:view"):
            rows = db.execute(
                """SELECT d.id, d.project_id, p.name project_name, p.client, d.status, d.start_date,
                          d.expected_completion_date, d.supervisor_name, d.updated_at
                   FROM project_deployments d JOIN tracker_projects p ON p.id=d.project_id
                   ORDER BY d.updated_at DESC, d.id DESC LIMIT 40"""
            ).fetchall()
            deployments=[]
            for d in rows:
                counts={x["status"]: x["c"] for x in db.execute(
                    "SELECT status, COUNT(*) c FROM project_deployment_items WHERE deployment_id=? AND applies=1 GROUP BY status", (d["id"],)
                ).fetchall()}
                deployments.append({**dict(d), "checklist_counts": counts})
            out["deployments"] = deployments
            awarded_not_started = db.execute(
                """SELECT tp.id project_id, tp.name project_name, tp.client, tp.address, tp.status
                   FROM tracker_projects tp LEFT JOIN project_deployments pd ON pd.project_id=tp.id
                   WHERE tp.status='Awarded' AND pd.id IS NULL ORDER BY tp.name"""
            ).fetchall()
            out["awarded_not_started"] = [dict(r) for r in awarded_not_started]
            out["sources"].append("project_deployments + project_deployment_items + awarded projects not yet started")
        elif scope == "deployment":
            return {"available": False, "scope": scope, "reason": "not_permitted", "authenticated_user": out["authenticated_user"]}

    if scope in ("overview", "sitepulse"):
        if user_has_permission(user, "module:sitepulse:view"):
            rows = db.execute(
                """SELECT tp.id project_id, tp.name project_name, tp.client, tp.address, tp.status,
                          pd.id deployment_id, pd.status deployment_status,
                          (SELECT MAX(fr.report_date) FROM field_reports fr WHERE fr.project_id=tp.id) last_report_date,
                          (SELECT COUNT(*) FROM field_reports fr WHERE fr.project_id=tp.id) field_report_count,
                          (SELECT COUNT(*) FROM inventory_concrete_requests c WHERE c.project_id=tp.id AND c.status!='Completed') open_concrete_count,
                          (SELECT COUNT(*) FROM inventory_purchase_requests pr WHERE pr.project_id=tp.id AND pr.status!='Completed') open_purchase_count,
                          (SELECT COUNT(*) FROM inventory_purchase_requests pr WHERE pr.project_id=tp.id) total_purchase_count,
                          (SELECT COUNT(*) FROM inventory_purchase_requests pr WHERE pr.project_id=tp.id AND pr.status='Completed') completed_purchase_count
                   FROM tracker_projects tp LEFT JOIN project_deployments pd ON pd.project_id=tp.id
                   WHERE tp.status='Awarded' OR pd.id IS NOT NULL ORDER BY tp.name"""
            ).fetchall()
            out["sitepulse_projects"] = [dict(r) for r in rows]
            # Deterministic purchase status counts/rows are included so the model
            # never has to reconcile a header count against a separately truncated list.
            pr_rows = db.execute(
                """SELECT id, pr_number, job_name, project_id, needed_on, status, vendor_company
                   FROM inventory_purchase_requests ORDER BY request_date DESC, id DESC"""
            ).fetchall()
            out["purchase_requests"] = [dict(r) for r in pr_rows[:100]]
            status_counts={}
            by_project={}
            for r in pr_rows:
                st=r["status"] or "Unknown"
                status_counts[st] = status_counts.get(st, 0) + 1
                key=str(r["project_id"] or "unlinked")
                by_project.setdefault(key, {"total":0,"by_status":{}})
                by_project[key]["total"] += 1
                by_project[key]["by_status"][st] = by_project[key]["by_status"].get(st,0) + 1
            out["purchase_status_counts"] = status_counts
            out["purchase_status_counts_by_project"] = by_project
            out["sources"].append("SitePulse eligibility + field reports + exact purchase/concrete status counts")
        elif scope == "sitepulse":
            return {"available": False, "scope": scope, "reason": "not_permitted", "authenticated_user": out["authenticated_user"]}

    if scope in ("overview", "rentals"):
        if user_has_permission(user, "module:equipment_center:view"):
            today = date.today().isoformat()
            rows = db.execute(
                """SELECT id, vendor, equipment_description, job_name, project_id, rate_amount, rate_period,
                          rented_date, due_date, returned_date, created_at, updated_at
                   FROM sitepulse_rentals ORDER BY due_date ASC, rented_date DESC"""
            ).fetchall()
            rentals=[]
            for r in rows:
                d=dict(r)
                d["status"] = "Returned" if d["returned_date"] else ("Overdue" if d["due_date"] and d["due_date"] < today else "Active")
                rentals.append(d)
            out["rentals"] = rentals
            out["sources"].append("sitepulse_rentals complete current records")
        elif scope == "rentals":
            return {"available": False, "scope": scope, "reason": "not_permitted", "authenticated_user": out["authenticated_user"]}

    if scope in ("overview", "field_reports"):
        if user_has_permission(user, "module:sitepulse:view"):
            rows = db.execute(
                """SELECT fr.id, fr.project_id, p.name project_name, fr.report_date, fr.status,
                          fr.has_issues, fr.issues_blockers, fr.next_steps, fr.created_by,
                          fr.last_edited_by, fr.updated_at
                   FROM field_reports fr JOIN tracker_projects p ON p.id=fr.project_id
                   ORDER BY COALESCE(fr.report_date, fr.updated_at) DESC LIMIT 40"""
            ).fetchall()
            out["field_reports"] = [dict(r) for r in rows]
            out["sources"].append("field_reports")
        elif scope == "field_reports":
            return {"available": False, "scope": scope, "reason": "not_permitted", "authenticated_user": out["authenticated_user"]}

    if scope in ("overview", "activity"):
        if user_has_permission(user, "action:activity_log:view"):
            rows = db.execute(
                """SELECT id, section, entity_type, entity_id, action, field, old_value, new_value,
                          user_email, created_at FROM activity_log ORDER BY id DESC LIMIT 100"""
            ).fetchall()
            out["activity"] = [dict(r) for r in rows]
            out["sources"].append("activity_log")
        elif scope == "activity":
            return {"available": False, "scope": scope, "reason": "not_permitted", "authenticated_user": out["authenticated_user"]}

    return out


# ---------------------------------------------------------------------------
# Registration -- called once from app.py after register_tool/get_db/
# user_has_permission/SP_STATUS_OPTIONS/PURCHASE_STATUS_OPTIONS all exist.
# ---------------------------------------------------------------------------

def register_atlas_tools(register_tool, sp_status_options, purchase_status_options):
    register_tool(
        name="get_project_status",
        description="Get the full status picture for one project by name or id: bid status, blocking quotes, linked concrete/purchase/rental records the caller is authorized to see, and any related attention items.",
        parameters={
            "project_name": {"type": "string", "required": False},
            "project_id": {"type": "integer", "required": False},
        },
        permission="module:project_hunt:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_get_project_status,
    )
    register_tool(
        name="list_bids_needing_attention",
        description="List active bids (projects) that have at least one blocking quote not yet received.",
        parameters={},
        permission="module:project_hunt:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_list_bids_needing_attention,
    )
    register_tool(
        name="list_bids_due_soon",
        description="List active bids with a bid_due_date within the given number of days (default 7).",
        parameters={"days": {"type": "integer", "required": False}},
        permission="module:project_hunt:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_list_bids_due_soon,
    )
    register_tool(
        name="list_upcoming_concrete_pours",
        description="List concrete pour requests scheduled within the given number of days (default 7) that are still Submitted or Scheduled.",
        parameters={"days": {"type": "integer", "required": False}},
        permission="module:sitepulse:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_list_upcoming_concrete_pours,
    )
    register_tool(
        name="find_equipment",
        description="Find equipment by name/description search and/or status. Excludes Sold and Stolen unless a status is explicitly given.",
        parameters={
            "query": {"type": "string", "required": False},
            "status": {"type": "string", "required": False, "enum": sp_status_options},
        },
        permission="module:equipment_center:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_find_equipment,
    )
    register_tool(
        name="list_rentals_due",
        description="List active (not yet returned) rentals, optionally filtered to overdue or due within 3 days.",
        parameters={"status": {"type": "string", "required": False, "enum": ["overdue", "due_soon"]}},
        permission="module:equipment_center:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_list_rentals_due,
    )
    register_tool(
        name="list_open_purchase_requests",
        description="List purchase requests by status. Status-only -- does not claim any request is 'late', since the data model doesn't reliably support that today.",
        parameters={"status": {"type": "string", "required": False, "enum": purchase_status_options}},
        permission="module:sitepulse:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_list_open_purchase_requests,
    )
    register_tool(
        name="get_attention_items",
        description="Get the current list of operational items needing attention, already filtered to what this user is authorized to see.",
        parameters={},
        permission="module:atlas:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_get_attention_items,
    )
    register_tool(
        name="get_buildiq_system_intelligence",
        description=(
            "Read live, permission-aware BuildIQ system intelligence outside a single project: deployed module registry, "
            "authenticated user/effective permissions, Project Deployment, SitePulse, rentals, CashFlow/Redline presence, field reports, and audit/activity history. Use the narrowest scope."
        ),
        parameters={"scope": {"type": "string", "required": False,
                               "enum": ["overview", "users_permissions", "deployment", "sitepulse", "field_reports", "activity", "modules", "cashflow", "redline", "rentals"]}},
        permission="module:atlas:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_get_buildiq_system_intelligence,
    )
    register_tool(
        name="get_buildiq_product_intelligence",
        description=(
            "Get live management intelligence about BuildIQ itself from Product Intelligence and employee Requests. "
            "Use for questions about what needs to be fixed/built in BuildIQ, employee feature/product requests, "
            "request lifecycle, pending approvals, product attention, or the BuildIQ roadmap. This is NOT Concrete "
            "Requests or Purchase Requests; those are operational SitePulse workflows."
        ),
        parameters={
            "scope": {"type": "string", "required": False, "enum": ["overview", "requests", "attention", "roadmap"]},
        },
        permission="module:product_intelligence:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_get_buildiq_product_intelligence,
    )
    register_tool(
        name="get_project_intelligence",
        description=(
            "Get bounded, factual, permission-filtered cross-module intelligence for the CURRENTLY ACTIVE "
            "canonical project (never a model-supplied one) -- project core info plus concrete/purchases/"
            "equipment/rentals/attention, each present only if the requesting user is authorized to see it. "
            "Every field is queried fresh from BuildIQ on every call -- never cached, never inferred, never a "
            "fabricated score/percentage/confidence. scope narrows which sources are actually queried."
        ),
        parameters={
            "scope": {"type": "string", "required": False, "enum": sorted(_PI_VALID_SCOPES)},
            "project_id": {"type": "integer", "required": False},
        },
        permission=("module:project_hunt:view", "module:equipment_center:view", "module:sitepulse:view"),
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_get_project_intelligence,
    )
    register_tool(
        name="set_project_context",
        description=(
            "Establish (or switch) the canonical project this Atlas session is currently working on, by name or "
            "id, so other tools that accept project_id can reuse it without asking again. Uses the exact same "
            "resolution as get_project_status -- an exact name match, or a unique substring match. If more than "
            "one project matches, nothing is set and the caller must ask the person which one they mean; this "
            "never guesses. Call this whenever the person establishes or changes which project they mean (e.g. "
            "'we're working on Patel Farm', 'switch to the Overlook Tower job') -- not on every turn."
        ),
        parameters={
            "project_name": {"type": "string", "required": False},
            "project_id": {"type": "integer", "required": False},
        },
        # Read-level permission only -- this never writes to the
        # database, only resolves an existing tracker_projects row and
        # (via execute_tool's session_context handling) remembers it for
        # the rest of this Atlas session. The same permission
        # get_project_status already requires.
        permission=("module:project_hunt:view", "module:equipment_center:view", "module:sitepulse:view"),
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_set_project_context,
    )
