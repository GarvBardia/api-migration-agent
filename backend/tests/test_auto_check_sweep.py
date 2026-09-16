"""Tests for scheduled auto-checking -- `app.tasks.periodic_auto_check_sweep`
-- added 2026-09-16, explicitly BEYOND the original 10-step plan (see
CLAUDE.md's dated note).

Run against the real Postgres database and a real embedded Celery worker
(same pattern as `test_resume.py`/`test_api.py`) -- a triggered run's
`scan_task`/`migrate_task`/`validate_task` chain actually runs in worker
threads, so this exercises the sweep's real dispatch path (`start_migration_
run`), not just its own DB bookkeeping. Only the Migration Agent (LLM
provider) is mocked.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agents.llm_providers import MigrationEdit
from app.celery_app import celery_app
from app.tasks import periodic_auto_check_sweep

STEP6_REPO = Path(__file__).parent / "fixtures" / "step6_sample_repo"


def _session():
    from db import SessionLocal

    return SessionLocal()


def _fresh_api_name() -> str:
    return f"autocheck-{uuid.uuid4().hex[:8]}"


def _make_repo(
    session,
    api_name: str,
    *,
    auto_check_enabled: bool,
    check_interval_hours: int | None,
    last_auto_checked_at=None,
):
    from db.models import Repo

    repo = Repo(
        name=f"repo-{api_name}",
        repo_path=str(STEP6_REPO),
        default_api_name=api_name,
        auto_check_enabled=auto_check_enabled,
        check_interval_hours=check_interval_hours,
        last_auto_checked_at=last_auto_checked_at,
    )
    session.add(repo)
    session.commit()
    session.refresh(repo)
    return repo


def _make_unprocessed_event(session, api_name: str, version_from="1.x", version_to="2.0"):
    from db.models import ChangelogEvent

    event = ChangelogEvent(
        api_name=api_name,
        version_from=version_from,
        version_to=version_to,
        change_type="deprecation",
        old_signature="oldapi.legacy_call",
        new_signature="oldapi.new_call",
        migration_notes="Use new_call with the same arguments.",
        processed_at=None,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def _wait_for_run_status(run_id, statuses, timeout: float = 60.0) -> str:
    from db.models import MigrationRun

    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _session()
        try:
            run = s.get(MigrationRun, uuid.UUID(str(run_id)))
            if run is not None and run.status in statuses:
                return run.status
        finally:
            s.close()
        time.sleep(0.5)
    raise TimeoutError(f"migration_runs {run_id} never reached {statuses}")


def _cleanup(repo_id=None, event_id=None, api_name=None) -> None:
    from db.models import ChangelogEvent, FileTask, HumanReviewQueue, MigrationRun, Repo

    session = _session()
    try:
        if api_name is not None:
            run_ids = [
                r.id
                for r in session.query(MigrationRun).filter_by(api_name=api_name).all()
            ]
            for run_id in run_ids:
                file_task_ids = [
                    t.id for t in session.query(FileTask).filter_by(run_id=run_id).all()
                ]
                if file_task_ids:
                    session.query(HumanReviewQueue).filter(
                        HumanReviewQueue.file_task_id.in_(file_task_ids)
                    ).delete(synchronize_session=False)
                session.query(FileTask).filter_by(run_id=run_id).delete(
                    synchronize_session=False
                )
            session.query(MigrationRun).filter_by(api_name=api_name).delete(
                synchronize_session=False
            )
            session.query(ChangelogEvent).filter_by(api_name=api_name).delete(
                synchronize_session=False
            )
        if event_id is not None:
            session.query(ChangelogEvent).filter_by(id=event_id).delete(
                synchronize_session=False
            )
        if repo_id is not None:
            session.query(Repo).filter_by(id=repo_id).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


class _FakeProvider:
    def propose_edit(
        self, *, old_code, old_signature, new_signature, migration_notes=None,
        failure_context=None, doc_context=None,
    ) -> MigrationEdit:
        return MigrationEdit(
            new_code="return oldapi.new_call(a, b)", rationale="mock: auto-check sweep test"
        )


@pytest.fixture(scope="module")
def celery_worker():
    from celery.contrib.testing.worker import start_worker

    with start_worker(celery_app, pool="threads", concurrency=4, perform_ping_check=False):
        yield celery_app


@pytest.fixture()
def fake_provider(monkeypatch):
    provider = _FakeProvider()
    monkeypatch.setattr(
        "app.agents.llm_providers.get_provider", lambda env=None: provider
    )
    return provider


# ---------------------------------------------------------------------------
# Done-criterion 3: "the scheduled task correctly auto-triggers a run when
# new changelog_events exist for an enabled repo, and correctly does
# nothing when there's nothing new or auto-check is disabled."
# ---------------------------------------------------------------------------


class TestAutoCheckTriggersRun:
    def test_enabled_due_repo_with_new_event_triggers_a_real_run(
        self, celery_worker, fake_provider
    ):
        api_name = _fresh_api_name()
        session = _session()
        repo = _make_repo(
            session,
            api_name,
            auto_check_enabled=True,
            check_interval_hours=1,
            last_auto_checked_at=None,  # never checked -- always due
        )
        event = _make_unprocessed_event(session, api_name)
        session.close()

        try:
            dispatched = periodic_auto_check_sweep()
            assert dispatched == 1

            # A real migration_runs row was created for this repo/api_name,
            # not just "something happened somewhere".
            from db.models import MigrationRun

            check = _session()
            try:
                run = (
                    check.query(MigrationRun)
                    .filter_by(api_name=api_name)
                    .one_or_none()
                )
                assert run is not None
                assert run.repo_url == str(STEP6_REPO)
                assert run.version_from == "1.x"
                assert run.version_to == "2.0"
                run_id = run.id
            finally:
                check.close()

            # The event that triggered it is marked processed.
            refreshed_event = _session()
            try:
                from db.models import ChangelogEvent

                ev = refreshed_event.get(ChangelogEvent, event.id)
                assert ev.processed_at is not None
            finally:
                refreshed_event.close()

            # The repo's last-checked timestamp advanced.
            refreshed_repo = _session()
            try:
                from db.models import Repo

                r = refreshed_repo.get(Repo, repo.id)
                assert r.last_auto_checked_at is not None
            finally:
                refreshed_repo.close()

            # Let the real dispatched pipeline finish before cleanup, same
            # discipline as every other real-dispatch test in this suite --
            # avoids leaving an in-flight task racing this test's cleanup.
            _wait_for_run_status(run_id, {"completed", "failed"})
        finally:
            _cleanup(repo_id=repo.id, api_name=api_name)

    def test_sweep_return_value_counts_dispatched_runs_not_repos_examined(
        self, celery_worker, fake_provider
    ):
        """Two distinct (version_from, version_to) pairs of unprocessed
        events for the SAME repo -- see periodic_auto_check_sweep's
        documented design decision -- dispatch two runs, not one."""

        api_name = _fresh_api_name()
        session = _session()
        repo = _make_repo(
            session,
            api_name,
            auto_check_enabled=True,
            check_interval_hours=1,
            last_auto_checked_at=None,
        )
        event_a = _make_unprocessed_event(session, api_name, "1.x", "2.0")
        event_b = _make_unprocessed_event(session, api_name, "2.0", "3.0")
        session.close()

        try:
            dispatched = periodic_auto_check_sweep()
            assert dispatched == 2

            from db.models import MigrationRun

            check = _session()
            try:
                runs = (
                    check.query(MigrationRun).filter_by(api_name=api_name).all()
                )
                version_pairs = {(r.version_from, r.version_to) for r in runs}
                assert version_pairs == {("1.x", "2.0"), ("2.0", "3.0")}
                run_ids = [r.id for r in runs]
            finally:
                check.close()

            for run_id in run_ids:
                _wait_for_run_status(run_id, {"completed", "failed"})
        finally:
            _cleanup(repo_id=repo.id, api_name=api_name)


class TestAutoCheckDoesNothing:
    def test_disabled_repo_is_ignored_even_with_new_events(self):
        api_name = _fresh_api_name()
        session = _session()
        repo = _make_repo(
            session,
            api_name,
            auto_check_enabled=False,
            check_interval_hours=1,
            last_auto_checked_at=None,
        )
        event = _make_unprocessed_event(session, api_name)
        session.close()

        try:
            dispatched = periodic_auto_check_sweep()
            assert dispatched == 0

            fresh = _session()
            try:
                from db.models import ChangelogEvent, MigrationRun, Repo

                assert fresh.get(ChangelogEvent, event.id).processed_at is None
                assert fresh.get(Repo, repo.id).last_auto_checked_at is None
                assert (
                    fresh.query(MigrationRun).filter_by(api_name=api_name).count() == 0
                )
            finally:
                fresh.close()
        finally:
            _cleanup(repo_id=repo.id, api_name=api_name)

    def test_enabled_repo_with_no_new_events_does_nothing_but_still_records_the_check(self):
        api_name = _fresh_api_name()
        session = _session()
        repo = _make_repo(
            session,
            api_name,
            auto_check_enabled=True,
            check_interval_hours=1,
            last_auto_checked_at=None,
        )
        session.close()  # deliberately no changelog_events seeded

        try:
            dispatched = periodic_auto_check_sweep()
            assert dispatched == 0

            fresh = _session()
            try:
                from db.models import MigrationRun, Repo

                # Examined (found nothing) is a different state from
                # "never checked" -- last_auto_checked_at should still
                # advance even though nothing was dispatched.
                assert fresh.get(Repo, repo.id).last_auto_checked_at is not None
                assert (
                    fresh.query(MigrationRun).filter_by(api_name=api_name).count() == 0
                )
            finally:
                fresh.close()
        finally:
            _cleanup(repo_id=repo.id, api_name=api_name)

    def test_enabled_repo_not_yet_due_is_skipped(self):
        api_name = _fresh_api_name()
        recently_checked = datetime.now(timezone.utc) - timedelta(hours=1)
        session = _session()
        repo = _make_repo(
            session,
            api_name,
            auto_check_enabled=True,
            check_interval_hours=24,  # 1 hour ago is nowhere near due
            last_auto_checked_at=recently_checked,
        )
        event = _make_unprocessed_event(session, api_name)
        session.close()

        try:
            dispatched = periodic_auto_check_sweep()
            assert dispatched == 0

            fresh = _session()
            try:
                from db.models import ChangelogEvent, Repo

                assert fresh.get(ChangelogEvent, event.id).processed_at is None
                # Untouched -- the repo was skipped entirely, not "checked
                # and found nothing" (it was never examined this tick).
                refreshed = fresh.get(Repo, repo.id)
                assert refreshed.last_auto_checked_at == recently_checked
            finally:
                fresh.close()
        finally:
            _cleanup(repo_id=repo.id, api_name=api_name)

    def test_enabled_repo_with_no_interval_set_is_inert(self):
        """auto_check_enabled=True with check_interval_hours=None is a
        valid, intentionally-inert state (see Repo.check_interval_hours's
        docstring) -- not an error, just never actually triggers."""

        api_name = _fresh_api_name()
        session = _session()
        repo = _make_repo(
            session,
            api_name,
            auto_check_enabled=True,
            check_interval_hours=None,
            last_auto_checked_at=None,
        )
        event = _make_unprocessed_event(session, api_name)
        session.close()

        try:
            dispatched = periodic_auto_check_sweep()
            assert dispatched == 0

            fresh = _session()
            try:
                from db.models import ChangelogEvent, Repo

                assert fresh.get(ChangelogEvent, event.id).processed_at is None
                assert fresh.get(Repo, repo.id).last_auto_checked_at is None
            finally:
                fresh.close()
        finally:
            _cleanup(repo_id=repo.id, api_name=api_name)
