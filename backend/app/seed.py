"""Permanent demo data (added 2026-10-04).

The landing page tells visitors to run `oldapi` 1.x to 2.0 on the example
folder. That only works if a change record for it exists. This inserts it
if it is missing. Safe to run any number of times. It runs at API startup,
so a database cleanup cannot leave the public demo without its record.
"""

from __future__ import annotations

DEMO_EVENT = {
    "api_name": "oldapi",
    "version_from": "1.x",
    "version_to": "2.0",
    "change_type": "deprecation",
    "old_signature": "oldapi.legacy_call",
    "new_signature": "oldapi.new_call",
    "migration_notes": "Use new_call with the same arguments.",
}


def ensure_demo_event(session) -> bool:
    """Insert the demo change record if absent. Returns True if inserted."""

    from db.models import ChangelogEvent

    exists = (
        session.query(ChangelogEvent)
        .filter_by(
            api_name=DEMO_EVENT["api_name"],
            version_from=DEMO_EVENT["version_from"],
            version_to=DEMO_EVENT["version_to"],
            old_signature=DEMO_EVENT["old_signature"],
        )
        .first()
    )
    if exists is not None:
        return False
    session.add(ChangelogEvent(**DEMO_EVENT))
    session.commit()
    return True
