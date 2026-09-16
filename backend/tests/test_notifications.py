"""Tests for webhook notifications (`app/agents/notifications.py`) --
added 2026-09-16, explicitly BEYOND the original 10-step plan (see
CLAUDE.md's dated note).

Two layers, per the kickoff's own instruction ("mock the webhook call in
tests -- don't require a real webhook URL to run the test suite"):

- Unit tests of `send_webhook`/`notify_run_terminal`/`notify_needs_review`
  in isolation, with `httpx.post` monkeypatched -- never touches the
  network, never requires NOTIFICATION_WEBHOOK_URL to be genuinely set.
- One integration test that runs a REAL fixture migration (mocked LLM
  provider only, same as every other integration test in this suite)
  through the real Celery pipeline with NOTIFICATION_WEBHOOK_URL set (to
  a fake value -- httpx.post is still monkeypatched, so nothing ever
  leaves the process) and confirms the webhook actually fires at the
  right moments: once the run reaches a terminal status, and once for the
  fixture's file that lands in needs_review.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from app.agents.llm_providers import MigrationEdit
from app.celery_app import celery_app

STEP6_REPO = Path(__file__).parent / "fixtures" / "step6_sample_repo"


def _session():
    from db import SessionLocal

    return SessionLocal()


# ---------------------------------------------------------------------------
# Unit tests -- app/agents/notifications.py in isolation.
# ---------------------------------------------------------------------------


class TestSendWebhookSkipsWhenUnconfigured:
    def test_no_call_and_no_error_when_url_unset(self, monkeypatch):
        import app.agents.notifications as notifications

        monkeypatch.delenv("NOTIFICATION_WEBHOOK_URL", raising=False)
        calls = []
        monkeypatch.setattr(
            notifications.httpx, "post", lambda *a, **kw: calls.append((a, kw))
        )

        notifications.send_webhook("this should never be sent")

        assert calls == []


class TestSendWebhookPostsSlackCompatibleShape:
    def test_posts_text_field_to_configured_url(self, monkeypatch):
        import app.agents.notifications as notifications

        monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "https://example.invalid/webhook")
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append({"url": url, "json": json, "timeout": timeout})

        monkeypatch.setattr(notifications.httpx, "post", fake_post)

        notifications.send_webhook("hello from the test suite")

        assert len(calls) == 1
        assert calls[0]["url"] == "https://example.invalid/webhook"
        # Slack/Discord-incoming-webhook-compatible shape -- exactly
        # {"text": "..."}, nothing else, per the kickoff's explicit steer.
        assert calls[0]["json"] == {"text": "hello from the test suite"}


class TestSendWebhookSwallowsDeliveryFailures:
    def test_network_error_does_not_propagate(self, monkeypatch):
        import app.agents.notifications as notifications

        monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "https://example.invalid/webhook")

        def raising_post(*a, **kw):
            raise ConnectionError("simulated network failure")

        monkeypatch.setattr(notifications.httpx, "post", raising_post)

        # Must not raise -- a flaky webhook receiver must never fail a
        # real migration task.
        notifications.send_webhook("this delivery will fail")


class TestNotifyRunTerminalPayload:
    def test_message_includes_run_id_status_api_name_and_counts(self, monkeypatch):
        import app.agents.notifications as notifications

        monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "https://example.invalid/webhook")
        captured = {}
        monkeypatch.setattr(
            notifications.httpx,
            "post",
            lambda url, json=None, timeout=None: captured.update(json),
        )

        class _FakeRun:
            id = uuid.uuid4()
            api_name = "some-api"
            version_from = "1.x"
            version_to = "2.0"
            status = "completed"

        notifications.notify_run_terminal(
            _FakeRun(), {"validated": 2, "needs_review": 1}
        )

        text = captured["text"]
        assert "some-api" in text
        assert "1.x" in text and "2.0" in text
        assert "completed" in text
        assert str(_FakeRun.id) in text
        assert "validated=2" in text
        assert "needs_review=1" in text


class TestNotifyNeedsReviewPayload:
    def test_message_includes_file_path_and_run_id(self, monkeypatch):
        import app.agents.notifications as notifications

        monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "https://example.invalid/webhook")
        captured = {}
        monkeypatch.setattr(
            notifications.httpx,
            "post",
            lambda url, json=None, timeout=None: captured.update(json),
        )

        class _FakeFileTask:
            file_path = "service_b.py"
            run_id = uuid.uuid4()
            failure_reason = "low_confidence_despite_passing_tests: score=0.75"

        notifications.notify_needs_review(_FakeFileTask())

        text = captured["text"]
        assert "service_b.py" in text
        assert str(_FakeFileTask.run_id) in text
        assert "low_confidence_despite_passing_tests" in text


# ---------------------------------------------------------------------------
# Integration: a real fixture migration through the real Celery pipeline
# fires the webhook at the right moments. Mocked provider (same as every
# other integration test here), mocked httpx.post (never touches the
# network), a genuinely-set NOTIFICATION_WEBHOOK_URL (so the "unconfigured
# -> skip" path is NOT what's being exercised here).
# ---------------------------------------------------------------------------


class _FakeProvider:
    def propose_edit(
        self, *, old_code, old_signature, new_signature, migration_notes=None,
        failure_context=None, doc_context=None,
    ) -> MigrationEdit:
        return MigrationEdit(
            new_code="return oldapi.new_call(a, b)", rationale="mock: notifications test"
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


def _cleanup(run_id, event_id) -> None:
    from db.models import ChangelogEvent, FileTask, HumanReviewQueue, MigrationRun

    session = _session()
    try:
        file_task_ids = [t.id for t in session.query(FileTask).filter_by(run_id=run_id).all()]
        if file_task_ids:
            session.query(HumanReviewQueue).filter(
                HumanReviewQueue.file_task_id.in_(file_task_ids)
            ).delete(synchronize_session=False)
        session.query(FileTask).filter_by(run_id=run_id).delete(synchronize_session=False)
        session.query(MigrationRun).filter_by(id=run_id).delete(synchronize_session=False)
        session.query(ChangelogEvent).filter_by(id=event_id).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


class TestWebhookFiresThroughRealPipeline:
    def test_terminal_and_needs_review_webhooks_fire(
        self, celery_worker, fake_provider, monkeypatch
    ):
        from app.tasks import start_migration_run

        monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "https://example.invalid/webhook")
        sent: list[str] = []

        # Patched where it's actually called from (app.agents.notifications),
        # not where httpx itself lives -- same reasoning as every other
        # monkeypatch in this suite that targets a specific module's
        # imported reference.
        import app.agents.notifications as notifications

        def fake_post(url, json=None, timeout=None):
            sent.append(json["text"])

        monkeypatch.setattr(notifications.httpx, "post", fake_post)

        api_name = f"webhook-test-{uuid.uuid4().hex[:8]}"
        session = _session()
        from db.models import ChangelogEvent

        event = ChangelogEvent(
            api_name=api_name,
            version_from="1.x",
            version_to="2.0",
            change_type="deprecation",
            old_signature="oldapi.legacy_call",
            new_signature="oldapi.new_call",
            migration_notes="Use new_call with the same arguments.",
        )
        session.add(event)
        session.commit()
        event_id = event.id
        session.close()

        run_id = start_migration_run(str(STEP6_REPO), api_name, "1.x", "2.0")

        try:
            _wait_for_run_status(run_id, {"completed", "failed"})

            # needs_review fired for service_b.py (medium-confidence match
            # -- see the fixture's own docstring: 0.6 base + 0.15 stays
            # below the 0.8 threshold even though its tests pass).
            assert any(
                "service_b.py" in text and "needs review" in text for text in sent
            ), sent

            # Terminal-state notification fired for the run itself.
            assert any(
                api_name in text and "completed" in text for text in sent
            ), sent
        finally:
            _cleanup(uuid.UUID(run_id), event_id)
