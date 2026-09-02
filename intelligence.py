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
