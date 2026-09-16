"""Tests for Step 7: REST + SSE API (`app/api/main.py`).

Run against the REAL Postgres database and a REAL embedded Celery worker
(`celery.contrib.testing.worker.start_worker`, same pattern as
`test_orchestrator.py`/`test_resume.py`) -- `POST /runs` triggers the real
Step 6a pipeline, and the SSE stream polls real DB rows while that
pipeline progresses in worker threads. Only the Migration Agent (LLM
provider) is mocked; Steps 4/5 already each did their own live-provider
smoke test.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.llm_providers import MigrationEdit
from app.api.main import app
from app.celery_app import celery_app

STEP6_REPO = Path(__file__).parent / "fixtures" / "step6_sample_repo"

client = TestClient(app)


def _session():
    from db import SessionLocal

    return SessionLocal()


def _insert_changelog_event(
    api_name: str,
    version_from: str = "1.x",
    version_to: str = "2.0",
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


def _cleanup(run_id=None, event_id=None) -> None:
    from db.models import ChangelogEvent, FileTask, HumanReviewQueue, MigrationRun

    session = _session()
    try:
        if run_id is not None:
            rid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
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
        if event_id is not None:
            session.query(ChangelogEvent).filter_by(id=event_id).delete(
                synchronize_session=False
            )
        session.commit()
    finally:
        session.close()


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


class _FakeProvider:
    # 2026-09-04: `doc_context` added -- the gap-fix session's Fix 2 wired
    # Step 10's semantic cache into `migrate_file_task`, which now always
    # passes `doc_context=...` (None in the common case) to
    # `provider.propose_edit(...)`. This fixture was never updated to
    # accept it, so every full-pipeline test in this file (POST /runs ->
    # scan -> migrate -> validate) was raising a TypeError inside
    # `migrate_task`, surfacing as a ChordError that left `finalize_run`
    # never invoked and `migration_runs.status` stuck at 'running'
    # forever. Tests using `_wait_for_run_status`'s 60s client-side
    # timeout merely failed slowly; `TestSSEStream` (no timeout of its
    # own) hung indefinitely -- see the dated CLAUDE.md note.
    def propose_edit(
        self, *, old_code, old_signature, new_signature, migration_notes=None,
        failure_context=None, doc_context=None,
    ) -> MigrationEdit:
        return MigrationEdit(
            new_code="return oldapi.new_call(a, b)", rationale="mock: api test"
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
# Done-criterion 1: POST /runs actually triggers the pipeline.
# ---------------------------------------------------------------------------


class TestCreateRunEndpoint:
    def test_post_runs_triggers_pipeline_and_creates_file_tasks(
        self, celery_worker, fake_provider
    ):
        from db.models import FileTask

        event_id = _insert_changelog_event("api-step7-create")
        run_id = None
        try:
            resp = client.post(
                "/runs",
                json={
                    "repo_url": str(STEP6_REPO),
                    "api_name": "api-step7-create",
                    "version_from": "1.x",
                    "version_to": "2.0",
                },
            )
            assert resp.status_code == 201
            run_id = resp.json()["run_id"]

            _wait_for_run_status(run_id, {"completed", "failed"})

            s = _session()
            try:
                count = (
                    s.query(FileTask).filter_by(run_id=uuid.UUID(run_id)).count()
                )
            finally:
                s.close()
            # step6_sample_repo has 2 matches (service_a.py, service_b.py) --
            # not just "the endpoint returned 200," the pipeline actually ran.
            assert count == 2
        finally:
            _cleanup(run_id, event_id)


# ---------------------------------------------------------------------------
# Done-criterion 2: GET endpoints return accurate data.
# ---------------------------------------------------------------------------


class TestListRunsEndpoint:
    """GET /runs -- added in the Step 8a session; Step 7 only built the
    single-run GET, and the frontend's run-list page needs a list-all
    endpoint."""

    def test_list_includes_created_run_newest_first(self, celery_worker, fake_provider):
        event_id = _insert_changelog_event("api-step8a-listruns")
        run_id = None
        try:
            resp = client.post(
                "/runs",
                json={
                    "repo_url": str(STEP6_REPO),
                    "api_name": "api-step8a-listruns",
                    "version_from": "1.x",
                    "version_to": "2.0",
                },
            )
            run_id = resp.json()["run_id"]

            list_resp = client.get("/runs")
            assert list_resp.status_code == 200
            body = list_resp.json()
            assert body[0]["id"] == run_id  # newest first
            assert any(r["id"] == run_id for r in body)
        finally:
            # 2026-08-31 fix: _wait_for_run_status and _cleanup used to be
            # two statements in one `finally` block -- if the wait raised
            # TimeoutError (e.g. the pipeline genuinely stalled), _cleanup
            # never ran, permanently leaking this run's file_tasks row AND
            # its already-dispatched Celery tasks. Confirmed live: this is
            # exactly how `api-step8a-listruns` was found stuck at
            # status='running' for 2+ hours during this session's hang
            # investigation, contributing to worker-thread starvation for
            # later tests sharing the same module-scoped celery_worker.
            # _cleanup must run unconditionally.
            try:
                _wait_for_run_status(run_id, {"completed", "failed"})
            finally:
                _cleanup(run_id, event_id)


class TestGetEndpoints:
    def test_get_run_matches_db(self, celery_worker, fake_provider):
        from db.models import MigrationRun

        event_id = _insert_changelog_event("api-step7-getrun")
        run_id = None
        try:
            resp = client.post(
                "/runs",
                json={
                    "repo_url": str(STEP6_REPO),
                    "api_name": "api-step7-getrun",
                    "version_from": "1.x",
                    "version_to": "2.0",
                },
            )
            run_id = resp.json()["run_id"]
            _wait_for_run_status(run_id, {"completed", "failed"})

            get_resp = client.get(f"/runs/{run_id}")
            assert get_resp.status_code == 200
            body = get_resp.json()

            s = _session()
            try:
                run = s.get(MigrationRun, uuid.UUID(run_id))
                assert body["status"] == run.status
                assert body["api_name"] == run.api_name
                assert body["repo_url"] == run.repo_url
                assert body["version_from"] == run.version_from
            finally:
                s.close()
        finally:
            _cleanup(run_id, event_id)

    def test_get_run_404_for_unknown_id(self):
        resp = client.get(f"/runs/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_get_file_tasks_matches_db(self, celery_worker, fake_provider):
        from db.models import FileTask

        event_id = _insert_changelog_event("api-step7-getfiletasks")
        run_id = None
        try:
            resp = client.post(
                "/runs",
                json={
                    "repo_url": str(STEP6_REPO),
                    "api_name": "api-step7-getfiletasks",
                    "version_from": "1.x",
                    "version_to": "2.0",
                },
            )
            run_id = resp.json()["run_id"]
            _wait_for_run_status(run_id, {"completed", "failed"})

            get_resp = client.get(f"/runs/{run_id}/file_tasks")
            assert get_resp.status_code == 200
            body = get_resp.json()

            s = _session()
            try:
                db_tasks = (
                    s.query(FileTask)
                    .filter_by(run_id=uuid.UUID(run_id))
                    .order_by(FileTask.file_path, FileTask.line_start)
                    .all()
                )
                assert len(body) == len(db_tasks)
                for api_task, db_task in zip(body, db_tasks):
                    assert api_task["id"] == str(db_task.id)
                    assert api_task["file_path"] == db_task.file_path
                    assert api_task["status"] == db_task.status
                    assert api_task["matched_symbol"] == db_task.matched_symbol
                    assert api_task["line_start"] == db_task.line_start
                    assert api_task["confidence_score"] == pytest.approx(
                        db_task.confidence_score
                    )
            finally:
                s.close()
        finally:
            _cleanup(run_id, event_id)

    def test_get_file_tasks_404_for_unknown_run(self):
        resp = client.get(f"/runs/{uuid.uuid4()}/file_tasks")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# human_review_queue listing + decision endpoints (done-criteria 2 & 4).
# ---------------------------------------------------------------------------


def _make_run_event_task_review(
    session, api_name: str, decided: bool = False, confidence_score: float = 0.6
):
    from db.models import ChangelogEvent, FileTask, HumanReviewQueue, MigrationRun

    run = MigrationRun(
        repo_url=str(STEP6_REPO),
        api_name=api_name,
        version_from="1.x",
        version_to="2.0",
        status="running",
    )
    session.add(run)
    session.flush()
    event = ChangelogEvent(
        api_name=api_name,
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
        status="needs_review",
        matched_symbol="legacy_call",
        line_start=11,
        line_end=11,
        confidence_score=confidence_score,
        old_code_snippet="    return oldapi.legacy_call(a, b)",
        new_code_snippet="return oldapi.legacy_call(a, b)",  # deliberately still-wrong
        changelog_event_id=event.id,
    )
    session.add(task)
    session.flush()
    review = HumanReviewQueue(
        file_task_id=task.id,
        reason="low_confidence_despite_passing_tests: score=0.60",
    )
    if decided:
        review.reviewer_decision = "approved"
        from datetime import datetime, timezone

        review.reviewed_at = datetime.now(timezone.utc)
    session.add(review)
    session.flush()
    session.commit()
    return run, event, task, review


class TestHumanReviewQueueList:
    def test_list_only_includes_pending_items(self):
        session = _session()
        run1, event1, task1, pending_review = _make_run_event_task_review(
            session, "api-step7-hrq-pending"
        )
        run2, event2, task2, decided_review = _make_run_event_task_review(
            session, "api-step7-hrq-decided", decided=True
        )
        session.close()
        try:
            resp = client.get("/human-review-queue")
            assert resp.status_code == 200
            ids = {item["review"]["id"] for item in resp.json()}
            assert str(pending_review.id) in ids
            assert str(decided_review.id) not in ids

            matching = next(
                item for item in resp.json() if item["review"]["id"] == str(pending_review.id)
            )
            assert matching["file_task"]["id"] == str(task1.id)
            assert matching["file_task"]["file_path"] == "service_a.py"
            assert matching["review"]["reason"].startswith(
                "low_confidence_despite_passing_tests"
            )
        finally:
            _cleanup(run1.id, event1.id)
            _cleanup(run2.id, event2.id)


class TestReviewDecisionApprovedAndRejected:
    def test_approved_records_decision_without_revalidating(self):
        session = _session()
        run, event, task, review = _make_run_event_task_review(
            session, "api-step7-decision-approved"
        )
        session.close()
        try:
            resp = client.post(
                f"/human-review-queue/{review.id}/decision",
                json={"decision": "approved"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["review"]["reviewer_decision"] == "approved"
            assert body["review"]["reviewed_at"] is not None
            assert body["revalidation_dispatched"] is False
            assert body["file_task"]["status"] == "needs_review"  # untouched
        finally:
            _cleanup(run.id, event.id)

    def test_rejected_records_decision(self):
        session = _session()
        run, event, task, review = _make_run_event_task_review(
            session, "api-step7-decision-rejected"
        )
        session.close()
        try:
            resp = client.post(
                f"/human-review-queue/{review.id}/decision",
                json={"decision": "rejected"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["review"]["reviewer_decision"] == "rejected"
            assert body["revalidation_dispatched"] is False
            assert body["file_task"]["status"] == "needs_review"  # untouched
        finally:
            _cleanup(run.id, event.id)

    def test_double_decision_is_conflict(self):
        session = _session()
        run, event, task, review = _make_run_event_task_review(
            session, "api-step7-decision-double"
        )
        session.close()
        try:
            first = client.post(
                f"/human-review-queue/{review.id}/decision",
                json={"decision": "approved"},
            )
            assert first.status_code == 200
            second = client.post(
                f"/human-review-queue/{review.id}/decision",
                json={"decision": "rejected"},
            )
            assert second.status_code == 409
        finally:
            _cleanup(run.id, event.id)

    def test_modified_without_code_is_unprocessable(self):
        session = _session()
        run, event, task, review = _make_run_event_task_review(
            session, "api-step7-decision-nocode"
        )
        session.close()
        try:
            resp = client.post(
                f"/human-review-queue/{review.id}/decision",
                json={"decision": "modified"},
            )
            assert resp.status_code == 422
        finally:
            _cleanup(run.id, event.id)

    def test_unknown_review_id_is_404(self):
        resp = client.post(
            f"/human-review-queue/{uuid.uuid4()}/decision",
            json={"decision": "approved"},
        )
        assert resp.status_code == 404


class TestReviewDecisionModifiedRevalidates:
    """Done-criterion 4: 'modified' actually re-runs Step 5's validation."""

    def test_modified_dispatches_revalidation_and_reaches_validated(
        self, celery_worker, fake_provider
    ):
        session = _session()
        # confidence_score=0.95 so the confidence gate (Phase 0) doesn't
        # itself route the re-passed test to needs_review -- this test is
        # specifically about the revalidation path reaching 'validated'.
        run, event, task, review = _make_run_event_task_review(
            session, "api-step7-decision-modified", confidence_score=0.95
        )
        session.close()
        try:
            resp = client.post(
                f"/human-review-queue/{review.id}/decision",
                json={
                    "decision": "modified",
                    "modified_code": "return oldapi.new_call(a, b)",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["review"]["reviewer_decision"] == "modified"
            assert body["revalidation_dispatched"] is True
            assert body["file_task"]["status"] == "in_progress"
            assert body["file_task"]["new_code_snippet"] == "return oldapi.new_call(a, b)"

            from db.models import FileTask

            deadline = time.time() + 60
            final_status = None
            while time.time() < deadline:
                s = _session()
                try:
                    refreshed = s.get(FileTask, task.id)
                    if refreshed.status in ("validated", "failed", "needs_review"):
                        final_status = refreshed.status
                        break
                finally:
                    s.close()
                time.sleep(0.5)

            assert final_status == "validated"  # correct code -> re-passes tests
        finally:
            _cleanup(run.id, event.id)


# ---------------------------------------------------------------------------
# Done-criterion 3: SSE stream, correct order, ends on terminal status.
# ---------------------------------------------------------------------------


class TestSSEStream:
    def test_stream_emits_events_ending_in_terminal_status(
        self, celery_worker, fake_provider
    ):
        event_id = _insert_changelog_event("api-step7-sse")
        run_id = None
        try:
            resp = client.post(
                "/runs",
                json={
                    "repo_url": str(STEP6_REPO),
                    "api_name": "api-step7-sse",
                    "version_from": "1.x",
                    "version_to": "2.0",
                },
            )
            run_id = resp.json()["run_id"]

            events: list[dict] = []
            with client.stream("GET", f"/runs/{run_id}/events") as stream_resp:
                assert stream_resp.status_code == 200
                data_line = None
                for line in stream_resp.iter_lines():
                    if line.startswith("data: "):
                        data_line = line[len("data: ") :]
                    elif line == "" and data_line is not None:
                        events.append(json.loads(data_line))
                        data_line = None
                        if events[-1]["run_status"] in ("completed", "failed"):
                            break

            assert len(events) >= 1
            assert events[-1]["run_status"] == "completed"
            # Snapshots only ever get appended when something changed --
            # no two consecutive identical snapshots.
            for a, b in zip(events, events[1:]):
                assert a != b
        finally:
            _cleanup(run_id, event_id)

    def test_stream_errors_for_unknown_run(self):
        with client.stream("GET", f"/runs/{uuid.uuid4()}/events") as stream_resp:
            assert stream_resp.status_code == 200
            body = "".join(stream_resp.iter_text())
        assert "event: error" in body
