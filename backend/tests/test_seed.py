"""The permanent demo change record (app/seed.py)."""

from __future__ import annotations

from app.seed import DEMO_EVENT, ensure_demo_event


def _count(session) -> int:
    from db.models import ChangelogEvent

    return (
        session.query(ChangelogEvent)
        .filter_by(
            api_name=DEMO_EVENT["api_name"],
            version_from=DEMO_EVENT["version_from"],
            version_to=DEMO_EVENT["version_to"],
            old_signature=DEMO_EVENT["old_signature"],
        )
        .count()
    )


def test_ensure_demo_event_is_idempotent_and_restores_after_delete():
    from db import SessionLocal
    from db.models import ChangelogEvent, FileTask

    s = SessionLocal()
    try:
        ensure_demo_event(s)
        assert _count(s) == 1
        assert ensure_demo_event(s) is False  # second call adds nothing
        assert _count(s) == 1

        # Simulate a cleanup. Only possible when no file_task references it.
        ev = s.query(ChangelogEvent).filter_by(
            api_name="oldapi", version_from="1.x", version_to="2.0"
        ).first()
        if s.query(FileTask).filter_by(changelog_event_id=ev.id).count() == 0:
            s.delete(ev)
            s.commit()
            assert _count(s) == 0
            assert ensure_demo_event(s) is True
            assert _count(s) == 1
    finally:
        s.close()
