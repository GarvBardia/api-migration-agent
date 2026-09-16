"""Integration tests for Step 6a's Celery task graph (`app/tasks.py`).

These run against the REAL Redis broker/backend (`docker-compose`'s
`redis` service) via a REAL embedded Celery worker
(`celery.contrib.testing.worker.start_worker`, `pool='threads'`) -- not
Celery's eager mode. Eager mode executes every task as a synchronous
in-process function call and could never prove the group/chord fan-out
actually runs file_tasks concurrently (done-criterion 6), so it isn't used
anywhere in this file, per the kickoff prompt's explicit instruction.

They also run against the REAL Postgres database (not the SQLite stand-in
Steps 3-5's unit tests use for CI speed): Celery tasks execute in worker
threads, each opening its own `SessionLocal()`, so an in-memory SQLite
session created in the test process wouldn't be visible to them anyway.
Rows created during each test are cleaned up in a `finally` block.

Only the Migration Agent (LLM provider) is mocked, via monkeypatching
`app.agents.llm_providers.get_provider` -- Steps 4 and 5 already each did
one live provider smoke test; this session doesn't add a third.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from app.agents.llm_providers import MigrationEdit
from app.celery_app import celery_app
from app.tasks import start_migration_run

STEP6_REPO = Path(__file__).parent / "fixtures" / "step6_sample_repo"


def _session():
    from db import SessionLocal

    return SessionLocal()


def _insert_changelog_event(
    api_name: str,
    version_from: str,
    version_to: str,
    old_signature: str = "oldapi.legacy_call",
    new_signature: str = "oldapi.new_call",
) -> uuid.UUID:
    from db.models import ChangelogEvent

    session = _session()
    try:
        event = ChangelogEvent(
            api_name=api_name,
            version_from=version_from,
            version_to=version_to,
            change_type="deprecation",
            old_signature=old_signature,
            new_signature=new_signature,
            migration_notes="Use new_call with the same arguments.",
        )
        session.add(event)
        session.commit()
        return event.id
    finally:
        session.close()


def _wait_for_terminal_run_status(
    run_id: str, timeout: float = 90.0, poll_interval: float = 0.5
) -> str:
    """Poll (via fresh sessions, so committed writes from worker threads are
    actually visible) until `migration_runs.status` reaches a terminal
    value or `timeout` elapses."""

    from db.models import MigrationRun

    deadline = time.time() + timeout
    while time.time() < deadline:
        session = _session()
        try:
            run = session.get(MigrationRun, uuid.UUID(run_id))
            if run is not None and run.status in ("completed", "failed"):
                return run.status
        finally:
            session.close()
        time.sleep(poll_interval)
    raise TimeoutError(
        f"migration_runs {run_id} did not reach a terminal status within {timeout}s"
    )


def _cleanup(run_id: str | None, changelog_event_ids=()) -> None:
    from db.models import ChangelogEvent, FileTask, HumanReviewQueue, MigrationRun

    session = _session()
    try:
        if run_id is not None:
            rid = uuid.UUID(run_id)
            file_task_ids = [
                t.id for t in session.query(FileTask).filter_by(run_id=rid).all()
            ]
            if file_task_ids:
                session.query(HumanReviewQueue).filter(
                    HumanReviewQueue.file_task_id.in_(file_task_ids)
                ).delete(synchronize_session=False)
            session.query(FileTask).filter_by(run_id=rid).delete(
                synchronize_session=False
            )
            session.query(MigrationRun).filter_by(id=rid).delete(
                synchronize_session=False
            )
        for eid in changelog_event_ids:
            session.query(ChangelogEvent).filter_by(id=eid).delete(
                synchronize_session=False
            )
        session.commit()
    finally:
        session.close()


class _FakeProvider:
    """Deterministic Migration Agent mock -- always proposes the correct
    fix. Sleep durations are controllable per call site so
    `TestConcurrentFanOut` can prove real concurrency without a live model:
    service_a.py's direct call site includes the literal substring
    "legacy_call" in its old_code; service_b.py's aliased call site (`lc(a,
    b)`) does not, so the two are distinguishable without extra plumbing.
    """

    def __init__(self, fast_sleep: float = 0.0, slow_sleep: float = 0.0):
        self.fast_sleep = fast_sleep
        self.slow_sleep = slow_sleep

    def propose_edit(
        self, *, old_code, old_signature, new_signature, migration_notes=None,
        failure_context=None, doc_context=None,
    ) -> MigrationEdit:
        # 2026-09-04: doc_context added -- see test_api.py's _FakeProvider
        # for the full story (same gap-fix-session omission, same fix).
        time.sleep(self.fast_sleep if "legacy_call" in old_code else self.slow_sleep)
        return MigrationEdit(
            new_code="return oldapi.new_call(a, b)",
            rationale="mock: renamed per changelog_events row",
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


class TestFullRunLifecycle:
    """Done-criteria 2 & 3: pending -> running -> completed, both fixture
    file_tasks reaching their expected terminal states."""

    def test_run_completes_with_expected_terminal_states(
        self, celery_worker, fake_provider
    ):
        from db.models import FileTask, HumanReviewQueue, MigrationRun

        event_id = _insert_changelog_event("oldapi-step6a-lifecycle", "1.x", "2.0")
        run_id = None
        try:
            run_id = start_migration_run(
                str(STEP6_REPO), "oldapi-step6a-lifecycle", "1.x", "2.0"
            )
            status = _wait_for_terminal_run_status(run_id)
            assert status == "completed"

            session = _session()
            try:
                run = session.get(MigrationRun, uuid.UUID(run_id))
                assert run.status == "completed"

                tasks = session.query(FileTask).filter_by(run_id=run.id).all()
                by_file = {t.file_path: t for t in tasks}
                assert set(by_file.keys()) == {"service_a.py", "service_b.py"}

                assert by_file["service_a.py"].status == "validated"
                assert by_file["service_a.py"].test_pass_count == 1

                assert by_file["service_b.py"].status == "needs_review"
                assert by_file["service_b.py"].test_pass_count == 1  # passed anyway
                review_rows = (
                    session.query(HumanReviewQueue)
                    .filter_by(file_task_id=by_file["service_b.py"].id)
                    .all()
                )
                assert len(review_rows) == 1
                assert review_rows[0].reason.startswith(
                    "low_confidence_despite_passing_tests"
                )
            finally:
                session.close()
        finally:
            _cleanup(run_id, [event_id])


class TestZeroMatchesCompletes:
    """Done-criterion 4: a scan that cleanly finds zero matches is a
    legitimate completed run, not a failure."""

    def test_scan_with_no_matches_completes(self, celery_worker, fake_provider):
        from db.models import FileTask

        event_id = _insert_changelog_event(
            "oldapi-step6a-zero",
            "1.x",
            "2.0",
            old_signature="oldapi.totally_unrelated_symbol",
            new_signature="oldapi.also_unrelated",
        )
        run_id = None
        try:
            run_id = start_migration_run(
                str(STEP6_REPO), "oldapi-step6a-zero", "1.x", "2.0"
            )
            status = _wait_for_terminal_run_status(run_id)
            assert status == "completed"

            session = _session()
            try:
                count = (
                    session.query(FileTask)
                    .filter_by(run_id=uuid.UUID(run_id))
                    .count()
                )
                assert count == 0
            finally:
                session.close()
        finally:
            _cleanup(run_id, [event_id])


class TestScanErrorRoutesToFailed:
    """Done-criterion 5: the scan step itself erroring (not "zero matches")
    ends the run at status='failed'. Forced deterministically via a
    changelog_events row whose old_signature isn't dotted -- scan_repo's
    TargetSymbol.from_changelog_event raises ValueError on it before ever
    touching the filesystem."""

    def test_scan_error_routes_to_failed(self, celery_worker, fake_provider):
        from db.models import FileTask

        event_id = _insert_changelog_event(
            "oldapi-step6a-scanerror",
            "1.x",
            "2.0",
            old_signature="not_a_dotted_signature",
            new_signature="whatever",
        )
        run_id = None
        try:
            run_id = start_migration_run(
                str(STEP6_REPO), "oldapi-step6a-scanerror", "1.x", "2.0"
            )
            status = _wait_for_terminal_run_status(run_id)
            assert status == "failed"

            session = _session()
            try:
                count = (
                    session.query(FileTask)
                    .filter_by(run_id=uuid.UUID(run_id))
                    .count()
                )
                assert count == 0  # failed before any file_tasks were created
            finally:
                session.close()
        finally:
            _cleanup(run_id, [event_id])


class TestConcurrentFanOut:
    """Done-criterion 6: the group/chord fan-out actually runs file_tasks
    concurrently, not one at a time."""

    def test_group_chord_runs_file_tasks_concurrently(self, celery_worker, monkeypatch):
        provider = _FakeProvider(fast_sleep=2.0, slow_sleep=4.0)
        monkeypatch.setattr(
            "app.agents.llm_providers.get_provider", lambda env=None: provider
        )

        event_id = _insert_changelog_event("oldapi-step6a-concurrency", "1.x", "2.0")
        run_id = None
        try:
            start = time.time()
            run_id = start_migration_run(
                str(STEP6_REPO), "oldapi-step6a-concurrency", "1.x", "2.0"
            )
            status = _wait_for_terminal_run_status(run_id, timeout=60)
            elapsed = time.time() - start

            assert status == "completed"
            # Sequential execution of just the two mocked LLM calls alone
            # would already take >= fast_sleep + slow_sleep = 6s, before
            # even adding each chain's own real Docker sandbox run on top.
            # If the group/chord fan-out is genuinely concurrent, elapsed
            # stays well under that sum -- proving the chord waited for
            # BOTH chains (not just the first to finish) while they ran in
            # parallel, not proving nothing ran at all.
            assert elapsed < (provider.fast_sleep + provider.slow_sleep), (
                f"elapsed={elapsed:.1f}s suggests the two file_task chains "
                "ran sequentially rather than concurrently"
            )
        finally:
            _cleanup(run_id, [event_id])


class TestTaskLevelIdempotency:
    """migrate_task must not re-invoke the Migration Agent for a file_task
    that isn't status='pending' -- CLAUDE.md §4.1's idempotency rule,
    enforced at the Celery task boundary, not just inside migrate_file_task
    itself."""

    def test_migrate_task_skips_non_pending_file_task(self, celery_worker):
        from db.models import ChangelogEvent, FileTask, MigrationRun
        from app.tasks import migrate_task

        class _ExplodingProvider:
            def propose_edit(self, **kwargs):
                raise AssertionError("propose_edit should not be called")

        session = _session()
        try:
            run = MigrationRun(
                repo_url=str(STEP6_REPO),
                api_name="oldapi-step6a-idempotency",
                version_from="1.x",
                version_to="2.0",
                status="running",
            )
            session.add(run)
            session.flush()
            event = ChangelogEvent(
                api_name="oldapi-step6a-idempotency",
                version_from="1.x",
                version_to="2.0",
                change_type="deprecation",
                old_signature="oldapi.legacy_call",
                new_signature="oldapi.new_call",
            )
            session.add(event)
            session.flush()
            task = FileTask(
                run_id=run.id,
                file_path="service_a.py",
                status="validated",  # already past 'pending'
                matched_symbol="legacy_call",
                line_start=11,
                line_end=11,
                confidence_score=1.0,
                old_code_snippet="    return oldapi.legacy_call(a, b)",
                new_code_snippet="return oldapi.new_call(a, b)",
                changelog_event_id=event.id,
            )
            session.add(task)
            session.commit()
            run_id, event_id, task_id = str(run.id), event.id, str(task.id)
        finally:
            session.close()

        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    "app.agents.llm_providers.get_provider",
                    lambda env=None: _ExplodingProvider(),
                )
                result = migrate_task.delay(task_id, str(STEP6_REPO))
                returned_id = result.get(timeout=30)
            assert returned_id == task_id
        finally:
            _cleanup(run_id, [event_id])
