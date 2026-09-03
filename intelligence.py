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
    """The single source of attention intelligence. Gathers every
    candidate item from real data, then keeps only the ones whose
    source_module the requesting user actually has permission to view --
    an admin has every module permission and so sees the full set
    naturally; a SitePulse-only user sees only sitepulse-sourced items,
    etc. No item is ever returned to a user who lacks the permission for
    its source_module, regardless of what asked for it (Product
    Intelligence page or Atlas)."""
    from app import get_db, user_has_permission
    db = get_db()

    candidates = []
    candidates += _bids_needing_attention(db)
    candidates += _bids_due_soon(db)
    candidates += _pours_without_order(db)
    candidates += _overdue_rentals(db)
    candidates += _pending_requests(db)

    allowed = []
    for item in candidates:
        perm_key = _ATTENTION_SOURCE_PERMISSION.get(item["source_module"])
        if perm_key and user_has_permission(user, perm_key):
            allowed.append(item)

    allowed.sort(key=lambda x: _SEVERITY_RANK.get(x["severity"], 3))
    if limit:
        allowed = allowed[:limit]
    return allowed


# ---------------------------------------------------------------------------
# Atlas tool handlers
# ---------------------------------------------------------------------------

def _find_project(db, project_name=None, project_id=None):
    """Shared project-resolution helper: exact id, or fuzzy name match.
    Returns (project_row_or_None, ambiguous_matches_list). Never guesses
    between multiple plausible matches -- returns them for the caller
    to disambiguate instead."""
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


def _tool_set_project_context(user, project_name=None, project_id=None):
    """Item 6 -- Atlas canonical project awareness. Resolves a project
    exactly the same way get_project_status does (same _find_project
    helper, same never-guess-on-ambiguity behavior) but returns only the
    identity fields needed to establish session context, not the full
    status payload. The actual writing of this result into the
    session's project_context happens in execute_tool() (app.py) --
    this handler, like every other tool handler, only ever touches the
    database/read path and returns a plain dict; it has no access to
    (and does not need) the session itself.
    """
    from app import get_db
    db = get_db()
    project, ambiguous = _find_project(db, project_name, project_id)
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


def _pi_project_core(db, project):
    return {
        "project_id": project["id"],
        "name": project["name"],
        "client": project["client"],
        "status": project["status"],
        "bid_due_date": project["bid_due_date"],
        "estimated_value": project["estimated_value"],
    }


def _pi_linked_via(row_project_id, canonical_pid):
    return "project_id" if row_project_id == canonical_pid else "legacy_exact_name_match"


def _pi_concrete(db, project_id, project_name):
    # LEGACY FALLBACK RULE (explicit precedence, exact match only, no
    # LIKE/substring): a record with a real project_id is matched ONLY
    # by that id, never additionally by free text. A record with
    # project_id IS NULL may match ONLY via an EXACT equality against
    # the canonical project's own name -- the same raw SQLite `=`
    # semantics _find_project()'s own exact-match tier already uses,
    # not a new normalization scheme.
    where = "(project_id = ? OR (project_id IS NULL AND project = ?))"
    params_base = (project_id, project_name)
    total = db.execute(f"SELECT COUNT(*) c FROM inventory_concrete_requests WHERE {where}", params_base).fetchone()["c"]
    open_total = db.execute(
        f"SELECT COUNT(*) c FROM inventory_concrete_requests WHERE {where} AND status != 'Completed'", params_base
    ).fetchone()["c"]
    rows = db.execute(
        f"""SELECT id, status, pour_date, project_id FROM inventory_concrete_requests WHERE {where}
           ORDER BY (status != 'Completed') DESC, pour_date DESC LIMIT ?""",
        params_base + (_PI_CONCRETE_LIMIT,)
    ).fetchall()
    return {
        "open_count": open_total,
        "total_count": total,
        "truncated": total > len(rows),
        "items": [
            {"record_type": "concrete_request", "record_id": r["id"], "status": r["status"],
             "relevant_date": r["pour_date"], "project_id": project_id, "linked_via": _pi_linked_via(r["project_id"], project_id)}
            for r in rows
        ],
    }


def _pi_purchases(db, project_id, project_name):
    where = "(project_id = ? OR (project_id IS NULL AND job_name = ?))"
    params_base = (project_id, project_name)
    total = db.execute(f"SELECT COUNT(*) c FROM inventory_purchase_requests WHERE {where}", params_base).fetchone()["c"]
    open_total = db.execute(
        f"SELECT COUNT(*) c FROM inventory_purchase_requests WHERE {where} AND status != 'Completed'", params_base
    ).fetchone()["c"]
    rows = db.execute(
        f"""SELECT id, status, needed_on, project_id FROM inventory_purchase_requests WHERE {where}
           ORDER BY (status != 'Completed') DESC, request_date DESC LIMIT ?""",
        params_base + (_PI_PURCHASE_LIMIT,)
    ).fetchall()
    return {
        "open_count": open_total,
        "total_count": total,
        "truncated": total > len(rows),
        "items": [
            {"record_type": "purchase_request", "record_id": r["id"], "status": r["status"],
             "relevant_date": r["needed_on"], "project_id": project_id, "linked_via": _pi_linked_via(r["project_id"], project_id)}
            for r in rows
        ],
    }


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
    where = "(project_id = ? OR (project_id IS NULL AND job_name = ?))"
    params_base = (project_id, project_name)
    total = db.execute(f"SELECT COUNT(*) c FROM sitepulse_rentals WHERE {where}", params_base).fetchone()["c"]
    active_total = db.execute(
        f"SELECT COUNT(*) c FROM sitepulse_rentals WHERE {where} AND (returned_date IS NULL OR returned_date = '')", params_base
    ).fetchone()["c"]
    rows = db.execute(
        f"""SELECT id, equipment_description, due_date, returned_date, project_id FROM sitepulse_rentals WHERE {where}
           ORDER BY (returned_date IS NULL OR returned_date = '') DESC, due_date DESC LIMIT ?""",
        params_base + (_PI_RENTAL_LIMIT,)
    ).fetchall()
    return {
        "active_count": active_total,
        "total_count": total,
        "truncated": total > len(rows),
        "items": [
            {"record_type": "rental", "record_id": r["id"], "equipment_description": r["equipment_description"],
             "status": "active" if not r["returned_date"] else "returned",
             "relevant_date": r["due_date"], "project_id": project_id, "linked_via": _pi_linked_via(r["project_id"], project_id)}
            for r in rows
        ],
    }


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
    from app import get_db, user_has_permission
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
    project = db.execute("SELECT * FROM tracker_projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        return {"found": False, "reason": "not_found"}

    pid = project["id"]
    pname = project["name"]
    result = {"found": True, "project": _pi_project_core(db, project)}

    want = lambda s: scope == "overview" or scope == s

    if want("concrete") and user_has_permission(user, "module:sitepulse:view"):
        try:
            result["concrete"] = _pi_concrete(db, pid, pname)
        except Exception as exc:
            _pi_source_failure("concrete", pid, exc)

    if want("purchases") and user_has_permission(user, "module:sitepulse:view"):
        try:
            result["purchases"] = _pi_purchases(db, pid, pname)
        except Exception as exc:
            _pi_source_failure("purchases", pid, exc)

    if want("equipment") and user_has_permission(user, "module:equipment_center:view"):
        try:
            result["equipment"] = _pi_equipment(db, pid)
        except Exception as exc:
            _pi_source_failure("equipment", pid, exc)

    if want("rentals") and user_has_permission(user, "module:equipment_center:view"):
        try:
            result["rentals"] = _pi_rentals(db, pid, pname)
        except Exception as exc:
            _pi_source_failure("rentals", pid, exc)

    if want("attention"):
        try:
            project_attention = [item for item in build_attention_items(user) if item.get("project_id") == pid]
            result["attention"] = project_attention[:_PI_ATTENTION_LIMIT]
        except Exception as exc:
            _pi_source_failure("attention", pid, exc)

    return result


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
        permission="module:project_hunt:view",
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
        permission="module:project_hunt:view",
        atlas_permission="atlas:view_business_data",
        kind="read",
        handler=_tool_set_project_context,
    )
