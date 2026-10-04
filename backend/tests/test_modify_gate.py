"""Modify decision and the 0.80 confidence gate (added 2026-10-04).

Question these tests answer: if a reviewer's modified fix passes its tests
but the file started below the 0.80 confidence threshold, does it end in
`needs_review` (not `validated`)?

Runs the real sandbox via `validate_file_task` directly (no Celery worker
needed: a passing fix never calls the provider). Own helpers on purpose;
`test_api.py` is not touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.validation import validate_file_task

STEP6_REPO = Path(__file__).parent / "fixtures" / "step6_sample_repo"
CORRECT_FIX = "return oldapi.new_call(a, b)"


def _session():
    from db import SessionLocal

    return SessionLocal()


class _NoProvider:
    """A passing fix never reaches the LLM provider. Fail loudly if it does."""

    def __getattr__(self, name):
        raise AssertionError("provider must not be called when the fix passes")


def _make_task(session, api_name: str, confidence: float):
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
    # State right after POST .../decision with decision=modified:
    # the reviewer's code is in place and the task is back in_progress.
    task = FileTask(
        run_id=run.id,
        file_path="service_a.py",
        status="in_progress",
        matched_symbol="legacy_call",
        line_start=11,
        line_end=11,
        confidence_score=confidence,
        old_code_snippet="    return oldapi.legacy_call(a, b)",
        new_code_snippet=CORRECT_FIX,
        changelog_event_id=event.id,
    )
    session.add(task)
    session.flush()
    ids = (run.id, event.id, task.id)
    session.commit()
    return ids


def _cleanup(run_id, event_id) -> None:
    from db.models import ChangelogEvent, FileTask, HumanReviewQueue, MigrationRun

    s = _session()
    try:
        task_ids = [t.id for t in s.query(FileTask).filter_by(run_id=run_id)]
        if task_ids:
            s.query(HumanReviewQueue).filter(
                HumanReviewQueue.file_task_id.in_(task_ids)
            ).delete(synchronize_session=False)
        s.query(FileTask).filter_by(run_id=run_id).delete(synchronize_session=False)
        s.query(MigrationRun).filter_by(id=run_id).delete(synchronize_session=False)
        s.query(ChangelogEvent).filter_by(id=event_id).delete(
            synchronize_session=False
        )
        s.commit()
    finally:
        s.close()


class TestModifiedFixConfidenceGate:
    def test_passing_fix_below_threshold_ends_in_needs_review(self):
        """0.60 start + 0.15 on a pass = 0.75, which is below 0.80."""
        from db.models import FileTask, HumanReviewQueue

        s = _session()
        run_id, event_id, task_id = _make_task(s, "modify-gate-low", confidence=0.60)
        try:
            task = s.get(FileTask, task_id)
            validate_file_task(s, task, STEP6_REPO, _NoProvider())
            s.commit()

            s.expire_all()
            task = s.get(FileTask, task_id)
            assert task.test_pass_count == 1  # the fix did pass its tests
            assert task.status == "needs_review"
            assert round(task.confidence_score, 2) == 0.75
            reviews = s.query(HumanReviewQueue).filter_by(file_task_id=task_id).all()
            assert len(reviews) == 1
            assert reviews[0].reviewer_decision is None
            assert reviews[0].reason.startswith(
                "low_confidence_despite_passing_tests: score=0.75"
            )
        finally:
            s.close()
            _cleanup(run_id, event_id)

    def test_passing_fix_at_high_confidence_is_validated(self):
        """Control: same fix, 0.95 start, clears the threshold."""
        from db.models import FileTask, HumanReviewQueue

        s = _session()
        run_id, event_id, task_id = _make_task(s, "modify-gate-high", confidence=0.95)
        try:
            task = s.get(FileTask, task_id)
            validate_file_task(s, task, STEP6_REPO, _NoProvider())
            s.commit()

            s.expire_all()
            task = s.get(FileTask, task_id)
            assert task.status == "validated"
            assert (
                s.query(HumanReviewQueue).filter_by(file_task_id=task_id).count() == 0
            )
        finally:
            s.close()
            _cleanup(run_id, event_id)


class TestPublicDemoModifyGate:
    """PUBLIC_DEMO_MODE blocks Modify (403) but not approve or reject."""

    def _review(self, api_name):
        from db.models import FileTask, HumanReviewQueue

        s = _session()
        run_id, event_id, task_id = _make_task(s, api_name, confidence=0.60)
        task = s.get(FileTask, task_id)
        task.status = "needs_review"
        review = HumanReviewQueue(file_task_id=task_id, reason="test")
        s.add(review)
        s.commit()
        review_id = review.id
        s.close()
        return run_id, event_id, task_id, review_id

    def _state(self, task_id, review_id):
        from db.models import FileTask, HumanReviewQueue

        s = _session()
        try:
            return (
                s.get(FileTask, task_id).status,
                s.get(HumanReviewQueue, review_id).reviewer_decision,
            )
        finally:
            s.close()

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app.api.main import app

        return TestClient(app)

    def test_modify_blocked_with_403_and_changes_nothing(
        self, client, monkeypatch
    ):
        monkeypatch.setenv("PUBLIC_DEMO_MODE", "1")
        run_id, event_id, task_id, review_id = self._review("modify-gate-403")
        try:
            resp = client.post(
                f"/human-review-queue/{review_id}/decision",
                json={"decision": "modified", "modified_code": CORRECT_FIX},
            )
            assert resp.status_code == 403
            assert "Modify is turned off" in resp.json()["detail"]
            assert self._state(task_id, review_id) == ("needs_review", None)
        finally:
            _cleanup(run_id, event_id)

    @pytest.mark.parametrize("decision", ["approved", "rejected"])
    def test_approve_and_reject_still_work(self, client, monkeypatch, decision):
        monkeypatch.setenv("PUBLIC_DEMO_MODE", "1")
        run_id, event_id, task_id, review_id = self._review("modify-gate-ok")
        try:
            resp = client.post(
                f"/human-review-queue/{review_id}/decision",
                json={"decision": decision},
            )
            assert resp.status_code == 200
            assert self._state(task_id, review_id)[1] == decision
        finally:
            _cleanup(run_id, event_id)

    def test_modify_allowed_when_flag_unset(self, client, monkeypatch):
        from unittest.mock import patch

        monkeypatch.delenv("PUBLIC_DEMO_MODE", raising=False)
        run_id, event_id, task_id, review_id = self._review("modify-gate-dev")
        try:
            with patch("app.tasks.validate_task.delay") as delay:
                resp = client.post(
                    f"/human-review-queue/{review_id}/decision",
                    json={"decision": "modified", "modified_code": CORRECT_FIX},
                )
            assert resp.status_code == 200
            assert resp.json()["revalidation_dispatched"] is True
            delay.assert_called_once()
            assert self._state(task_id, review_id) == ("in_progress", "modified")
        finally:
            _cleanup(run_id, event_id)

    @pytest.mark.parametrize(
        "value,expected", [("1", True), ("true", True), ("", False), ("0", False)]
    )
    def test_config_endpoint_reflects_flag(
        self, client, monkeypatch, value, expected
    ):
        monkeypatch.setenv("PUBLIC_DEMO_MODE", value)
        assert client.get("/config").json() == {"public_demo_mode": expected}
