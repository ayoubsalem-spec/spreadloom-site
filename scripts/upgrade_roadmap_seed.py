"""
Explicit, deliberately-run migration: upgrades a database still holding
the OLD roadmap_items seed (SitePulse/Project Hunt/Equipment Center/
BidFlow/Atlas/Engineering/Finance, from before Product Intelligence
2.0's Build Direction story) to the new NOW/NEXT/EVOLVING/LATER story.

NOT run automatically by application startup (CTO audit finding: an
earlier version called this from init_db() unconditionally, which
meant ANY existing database -- eventually including a real production
one -- would have its roadmap silently rewritten on every boot). This
script must be run manually and deliberately, exactly once, by someone
who has decided that's what they want.

Idempotent and safe: only fires if every one of the 7 original rows is
still present with its EXACT original note text -- i.e. nothing an
admin has ever hand-edited via the roadmap UI. If even one has been
edited (or is missing), it does nothing and says so. Administrator-
edited roadmap data is never touched.

Newly-inserted rows deliberately use progress_pct = 0 for every item --
there is no defensible measurable source for a completion percentage on
any of these, so none is invented. progress_pct remains a real,
admin-editable field afterward.

Usage (from the project root):
    APP_ENV=development python3 scripts/upgrade_roadmap_seed.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from datetime import datetime

import app as appmod

ORIGINAL_SEED = {
    "SitePulse": "Filters and delivery dates shipped. Polishing purchase request flow next.",
    "Project Hunt": "Bid tracker filters and KPI theme done. Vendor follow-up automation left.",
    "Equipment Center": "Matching Product Intelligence's KPI colors and filters.",
    "BidFlow": "Takeoff + bid system. Waiting on final Excel sheets from estimating.",
    "Atlas": "Voice assistant -- scoping starts after BidFlow's data model is settled.",
    "Engineering": "Parked intentionally.",
    "Finance": "Parked intentionally.",
}

NEW_SEED = [
    ("Product Core", "now", "Canonical Project Identity is live -- concrete, purchase, and rental records link to real projects. Currently extending that connectivity into more of Project Hunt/SitePulse.", 0, 1),
    ("Product Intelligence", "now", "Command Center experience refinement -- visual hierarchy, real-data intelligence, and honest empty states.", 0, 2),
    ("Project Connectivity", "next", "Turning canonical Project Identity into useful connected project intelligence across modules.", 0, 3),
    ("Atlas", "evolving", "BuildIQ's intelligence and action layer -- read tools shipped; continuously gaining capability rather than reaching a fixed 100%.", 0, 4),
    ("BidFlow", "later", "Takeoff + bid system. Parked until the real estimating workflow/Excel sheets are available.", 0, 5),
    ("Redline", "later", "Parked intentionally.", 0, 6),
    ("Finance", "later", "Parked intentionally.", 0, 7),
]


def main():
    db = sqlite3.connect(appmod.DB_PATH)
    rows = {name: note for name, note in db.execute(
        "SELECT name, note FROM roadmap_items WHERE name IN (%s)" % ",".join("?" * len(ORIGINAL_SEED)),
        list(ORIGINAL_SEED.keys())
    ).fetchall()}

    if rows != ORIGINAL_SEED:
        print("Roadmap does not exactly match the original legacy seed (either already")
        print("upgraded, hand-edited by an admin, or never had these rows). Doing nothing.")
        for name, expected_note in ORIGINAL_SEED.items():
            actual = rows.get(name)
            if actual is None:
                print(f"  - {name}: not found")
            elif actual != expected_note:
                print(f"  - {name}: note differs from the original seed (likely hand-edited)")
        return

    confirm = input(
        f"This will DELETE {len(ORIGINAL_SEED)} legacy roadmap rows and insert "
        f"{len(NEW_SEED)} new ones at {appmod.DB_PATH}. Type 'yes' to proceed: "
    )
    if confirm.strip().lower() != "yes":
        print("Aborted -- no changes made.")
        return

    now = datetime.utcnow().isoformat()
    db.execute("DELETE FROM roadmap_items WHERE name IN (%s)" % ",".join("?" * len(ORIGINAL_SEED)), list(ORIGINAL_SEED.keys()))
    for name, lane, note, pct, order in NEW_SEED:
        db.execute(
            "INSERT INTO roadmap_items (name, lane, note, progress_pct, sort_order, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, lane, note, pct, order, now)
        )
    db.commit()
    print(f"Upgraded {len(ORIGINAL_SEED)} legacy roadmap rows to the new Build Direction story.")


if __name__ == "__main__":
    main()
