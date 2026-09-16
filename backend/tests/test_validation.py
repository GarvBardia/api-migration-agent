"""Tests for the Step 5 Validation Agent (`app/agents/validation.py`).

Unlike Step 3/4's tests, most of these actually spin up real Docker
containers (`migration-agent-sandbox:latest`, built from
`backend/sandbox/Dockerfile` -- see CLAUDE.md §4.4) rather than mocking the
sandbox itself: the done-criteria this session cares about (network
isolation, container teardown/no state leak, timeout+kill, structured
report parsing) are properties of the real sandbox mechanism, not of
application logic that can be meaningfully faked. Only the Migration Agent
call (LLM provider) is mocked for the retry-loop tests, per the kickoff
prompt -- a live model retrying would be nondeterministic.

Requires Docker Desktop running and the sandbox image built:
    docker build -t migration-agent-sandbox:latest backend/sandbox
"""

from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.llm_providers import MigrationEdit
from app.agents.migration import migrate_file_task
from app.agents.validation import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_SANDBOX_IMAGE,
    run_pytest_in_sandbox,
    validate_file_task,
)

SAMPLE_REPO = Path(__file__).parent / "fixtures" / "step5_sample_repo"
NETWORK_CHECK_REPO = Path(__file__).parent / "fixtures" / "step5_network_check"
STATE_LEAK_REPO = Path(__file__).parent / "fixtures" / "step5_state_leak_check"

WRONG_NEW_CODE = "return oldapi.legacy_call(a, b)"  # still broken -- doesn't fix anything
CORRECT_NEW_CODE = "return oldapi.new_call(a, b)"


def _docker_available() -> bool:
    try:
        run_pytest_in_sandbox(NETWORK_CHECK_REPO, image=DEFAULT_SANDBOX_IMAGE)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon or migration-agent-sandbox:latest image not available.",
)


class _FakeProvider:
    """Always returns the same edit, regardless of failure_context -- used
    where a test needs a deterministic single-shot or always-wrong mock."""

    def __init__(self, new_code: str, rationale: str = "mock"):
        self._new_code = new_code
        self._rationale = rationale
        self.call_count = 0

    def propose_edit(self, **kwargs):
        self.call_count += 1
        return MigrationEdit(new_code=self._new_code, rationale=self._rationale)


class _ExplodingProvider:
    """A provider that must never actually be called -- for asserting the
    happy path and idempotency never touch the Migration Agent."""

    def propose_edit(self, **kwargs):
        raise AssertionError(
            "propose_edit was called but should not have been for this test"
        )


@pytest.fixture()
def sqlite_session():
    from db.models import Base, ChangelogEvent, FileTask, HumanReviewQueue, MigrationRun

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            MigrationRun.__table__,
            ChangelogEvent.__table__,
            FileTask.__table__,
            HumanReviewQueue.__table__,
        ],
    )
    with Session(engine) as session:
        yield session


def _make_run_and_task(session, **task_overrides):
    from db.models import ChangelogEvent, FileTask, MigrationRun

    run = MigrationRun(
        repo_url="https://example.com/step5-fixture-repo.git",
        api_name="oldapi",
        version_from="1.x",
        version_to="2.0",
        status="running",
    )
    session.add(run)
    session.flush()

    event = ChangelogEvent(
        api_name="oldapi",
        version_from="1.x",
        version_to="2.0",
        change_type="deprecation",
        old_signature="oldapi.legacy_call",
        new_signature="oldapi.new_call",
        migration_notes="Use new_call with the same arguments.",
    )
    session.add(event)
    session.flush()

    defaults = dict(
        run_id=run.id,
        file_path="service.py",
        status="in_progress",
        matched_symbol="legacy_call",
        line_start=5,
        line_end=5,
        confidence_score=0.95,
        old_code_snippet="    return oldapi.legacy_call(a, b)",
        new_code_snippet=CORRECT_NEW_CODE,
        changelog_event_id=event.id,
    )
    defaults.update(task_overrides)
    task = FileTask(**defaults)
    session.add(task)
    session.flush()
    return run, event, task


# ---------------------------------------------------------------------------
# Done-criterion 1: passing patch -> validated, counters/confidence updated.
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_passing_patch_marks_validated(self, sqlite_session):
        # 0.7 + 0.15 = 0.85, comfortably above DEFAULT_CONFIDENCE_THRESHOLD
        # (0.8) -- this test is about the pass-path/counters, not the gate;
        # see TestConfidenceGate below for the gate itself.
        _, _, task = _make_run_and_task(
            sqlite_session, new_code_snippet=CORRECT_NEW_CODE, confidence_score=0.7
        )
        provider = _ExplodingProvider()  # must never be called -- no failure occurs

        validate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.status == "validated"
        assert task.test_pass_count == 1
        assert task.test_fail_count == 0
        assert task.confidence_score == pytest.approx(0.7 + 0.15)
        assert task.retry_count == 0
        assert task.failure_reason is None

    def test_confidence_score_capped_at_one(self, sqlite_session):
        _, _, task = _make_run_and_task(
            sqlite_session, new_code_snippet=CORRECT_NEW_CODE, confidence_score=0.95
        )
        provider = _ExplodingProvider()

        validate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.confidence_score == 1.0  # min(1.0, 0.95 + 0.15) not 1.10


# ---------------------------------------------------------------------------
# Phase 0 (2026-08-26, Step 6a session): the confidence trust gate.
# CLAUDE.md documents confidence_score as a gate independent of test
# outcome -- passing tests must NOT override a still-low confidence_score.
# ---------------------------------------------------------------------------


class TestConfidenceGate:
    def test_low_confidence_despite_passing_tests_routes_to_needs_review(
        self, sqlite_session
    ):
        from db.models import HumanReviewQueue

        # 0.3 + 0.15 = 0.45, still well below DEFAULT_CONFIDENCE_THRESHOLD
        # (0.8) even though the sandbox run will pass outright on attempt 1.
        _, _, task = _make_run_and_task(
            sqlite_session, new_code_snippet=CORRECT_NEW_CODE, confidence_score=0.3
        )
        provider = _ExplodingProvider()  # must never be called -- no failure occurs

        validate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.status == "needs_review"
        assert task.test_pass_count == 1  # tests genuinely passed
        assert task.test_fail_count == 0
        assert task.retry_count == 0  # not a retry-exhaustion path
        assert task.confidence_score == pytest.approx(0.3 + 0.15)

        review_rows = (
            sqlite_session.query(HumanReviewQueue)
            .filter_by(file_task_id=task.id)
            .all()
        )
        assert len(review_rows) == 1
        assert review_rows[0].reason.startswith("low_confidence_despite_passing_tests:")
        assert "0.45" in review_rows[0].reason  # the actual score, not just the label

    def test_confidence_at_or_above_threshold_is_validated(self, sqlite_session):
        # 0.65 + 0.15 = 0.80 == threshold exactly -- the gate is "< threshold",
        # so meeting it exactly still validates (not an off-by-one on the
        # boundary in the wrong direction).
        _, _, task = _make_run_and_task(
            sqlite_session, new_code_snippet=CORRECT_NEW_CODE, confidence_score=0.65
        )
        provider = _ExplodingProvider()

        validate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.confidence_score == pytest.approx(0.8)
        assert task.status == "validated"


# ---------------------------------------------------------------------------
# Done-criteria 2 & 4: retry recovers; boundary is exact.
# ---------------------------------------------------------------------------


class TestRetryLoop:
    def test_fails_attempt_one_recovers_attempt_two(self, sqlite_session):
        """Wrong fix on attempt 1 (simulating Step 4's initial output),
        correct fix from a mocked Migration Agent on the retry -> validated
        with retry_count == 1, not more."""

        _, _, task = _make_run_and_task(sqlite_session, new_code_snippet=WRONG_NEW_CODE)
        recovering_provider = _FakeProvider(new_code=CORRECT_NEW_CODE)

        validate_file_task(sqlite_session, task, SAMPLE_REPO, recovering_provider)
        sqlite_session.commit()

        assert task.status == "validated"
        assert task.retry_count == 1
        assert task.test_fail_count == 1
        assert task.test_pass_count == 1
        assert recovering_provider.call_count == 1

    def test_boundary_one_below_cap_still_retries_at_cap_routes_to_review(
        self, sqlite_session
    ):
        """max_retries=2: retry_count reaching 1 (one below cap) still
        retries; reaching 2 (== cap) routes to needs_review. Exercised as
        one scenario since it's the same run's progression through the
        boundary."""

        from db.models import HumanReviewQueue

        _, _, task = _make_run_and_task(sqlite_session, new_code_snippet=WRONG_NEW_CODE)
        always_wrong_provider = _FakeProvider(new_code=WRONG_NEW_CODE)

        validate_file_task(
            sqlite_session, task, SAMPLE_REPO, always_wrong_provider, max_retries=2
        )
        sqlite_session.commit()

        assert task.retry_count == 2  # hit the cap, not 1 and not 3
        assert task.status == "needs_review"
        # One retry happened at retry_count==1 (< cap of 2); the cap itself
        # (retry_count==2) stops without triggering another Migration Agent
        # call, so the provider is invoked exactly (max_retries - 1) times.
        assert always_wrong_provider.call_count == 1

        review_rows = (
            sqlite_session.query(HumanReviewQueue)
            .filter_by(file_task_id=task.id)
            .all()
        )
        assert len(review_rows) == 1
        assert review_rows[0].reason.startswith("retries_exhausted:")
        assert "NotImplementedError" in review_rows[0].reason  # real failure detail


# ---------------------------------------------------------------------------
# Done-criterion 3: retries exhausted -> needs_review + human_review_queue
# with real failure detail (covered above too; this test isolates it at the
# default max_retries=3).
# ---------------------------------------------------------------------------


class TestRetriesExhaustedAtDefaultCap:
    def test_default_max_retries_three(self, sqlite_session):
        from db.models import HumanReviewQueue

        _, _, task = _make_run_and_task(sqlite_session, new_code_snippet=WRONG_NEW_CODE)
        always_wrong_provider = _FakeProvider(new_code=WRONG_NEW_CODE)

        validate_file_task(sqlite_session, task, SAMPLE_REPO, always_wrong_provider)
        sqlite_session.commit()

        assert task.status == "needs_review"
        assert task.retry_count == 3
        # retry_count reaches the cap (3) on the 3rd failed attempt; the
        # Migration Agent is only re-invoked when retry_count < max_retries,
        # so it's called on the 1st and 2nd failures (2 calls), not the 3rd.
        assert always_wrong_provider.call_count == 2
        assert task.test_fail_count == 3  # every attempt (1st, 2nd, 3rd) failed

        review_rows = (
            sqlite_session.query(HumanReviewQueue)
            .filter_by(file_task_id=task.id)
            .all()
        )
        assert len(review_rows) == 1
        assert review_rows[0].reason.startswith("retries_exhausted:")
        assert "legacy_call" in review_rows[0].reason or "NotImplementedError" in review_rows[0].reason


# ---------------------------------------------------------------------------
# Done-criterion 7: old_code_snippet mismatch -> needs_review, no guessing.
# ---------------------------------------------------------------------------


class TestOldCodeSnippetMismatch:
    def test_mismatch_routes_to_needs_review_without_running_sandbox(self, sqlite_session, monkeypatch):
        _, _, task = _make_run_and_task(
            sqlite_session,
            old_code_snippet="    return oldapi.THIS_DOES_NOT_MATCH_THE_REAL_FILE(a, b)",
        )
        provider = _ExplodingProvider()

        def _explode(*args, **kwargs):
            raise AssertionError("run_pytest_in_sandbox should not be called on a mismatch")

        monkeypatch.setattr(
            "app.agents.validation.run_pytest_in_sandbox", _explode
        )

        validate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.status == "needs_review"
        assert task.failure_reason
        assert "no longer matches the real file" in task.failure_reason
        assert task.new_code_snippet == CORRECT_NEW_CODE  # untouched, not clobbered


# ---------------------------------------------------------------------------
# Done-criterion 9: idempotency.
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_already_validated_task_is_a_noop(self, sqlite_session, monkeypatch):
        _, _, task = _make_run_and_task(sqlite_session, status="validated", test_pass_count=1)
        provider = _ExplodingProvider()

        def _explode(*args, **kwargs):
            raise AssertionError("run_pytest_in_sandbox should not run for an already-validated task")

        monkeypatch.setattr("app.agents.validation.run_pytest_in_sandbox", _explode)

        validate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.status == "validated"
        assert task.test_pass_count == 1  # unchanged, not re-incremented


# ---------------------------------------------------------------------------
# Done-criterion 5: no network access in the sandbox.
# ---------------------------------------------------------------------------


class TestNetworkIsolation:
    def test_external_connection_fails_inside_sandbox(self):
        result = run_pytest_in_sandbox(NETWORK_CHECK_REPO)
        assert result.passed is True  # the fixture test passes BECAUSE the connection failed
        assert result.pass_count == 1
        assert result.fail_count == 0


# ---------------------------------------------------------------------------
# Done-criterion 6: containers are torn down between runs, no state leak.
# ---------------------------------------------------------------------------


class TestNoStateLeakBetweenContainers:
    def test_marker_does_not_persist_across_two_separate_containers(self):
        results = []
        for _ in range(2):
            temp_dir = Path(tempfile.mkdtemp(prefix="step5-state-leak-test-"))
            shutil.copytree(STATE_LEAK_REPO, temp_dir, dirs_exist_ok=True)
            try:
                results.append(run_pytest_in_sandbox(temp_dir))
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        # If a container's filesystem leaked into the next, the second run's
        # "marker must not already exist" assertion would fail.
        assert all(r.passed for r in results), results


# ---------------------------------------------------------------------------
# Timeout enforcement (part of done-criterion 6's "provably torn down" +
# the kickoff's explicit timeout requirement).
# ---------------------------------------------------------------------------


class TestTimeoutEnforcement:
    def test_hanging_test_is_killed_at_timeout(self):
        hang_dir = Path(tempfile.mkdtemp(prefix="step5-timeout-test-"))
        (hang_dir / "test_hang.py").write_text(
            "import time\n\n\ndef test_hangs_forever():\n    time.sleep(120)\n"
        )
        try:
            start = time.time()
            result = run_pytest_in_sandbox(hang_dir, timeout_seconds=8)
            elapsed = time.time() - start
        finally:
            shutil.rmtree(hang_dir, ignore_errors=True)

        assert result.timed_out is True
        assert result.passed is False
        assert elapsed < 20  # killed well before the test's own 120s sleep would finish


# ---------------------------------------------------------------------------
# Done-criterion 8: structured report parsing, not regex on raw stdout.
# ---------------------------------------------------------------------------


class TestOpenRouterLiveSmoke:
    """At most one real live call, per the kickoff prompt -- same pattern
    Step 4 used. Reads the project root `.env`; skipped if unavailable."""

    @staticmethod
    def _load_live_config():
        from dotenv import dotenv_values

        root_env_path = Path(__file__).resolve().parents[2] / ".env"
        env = dotenv_values(root_env_path) if root_env_path.exists() else {}
        return env.get("OPENROUTER_API_KEY") or "", env.get("MIGRATION_LLM_MODEL") or ""

    def test_real_retry_recovers_via_live_openrouter_call(self, sqlite_session):
        api_key, model = self._load_live_config()
        if not api_key or not model:
            pytest.skip(
                "No OPENROUTER_API_KEY/MIGRATION_LLM_MODEL in the project .env; "
                "skipping live call."
            )

        from app.agents.llm_providers import OpenRouterAdapter

        _, _, task = _make_run_and_task(sqlite_session, new_code_snippet=WRONG_NEW_CODE)
        adapter = OpenRouterAdapter(api_key=api_key, model=model)

        validate_file_task(sqlite_session, task, SAMPLE_REPO, adapter)
        sqlite_session.commit()

        # A live model may occasionally misbehave across retries -- but
        # either terminal state is an acceptable pass for a smoke test; a
        # crash or an inconsistent row (e.g. validated with no passing
        # test) is not.
        assert task.status in ("validated", "needs_review")
        if task.status == "validated":
            assert task.test_pass_count >= 1
        else:
            assert task.failure_reason


class TestNoRegexInResultParsing:
    def test_no_re_module_import(self):
        source = Path("app/agents/validation.py").read_text()
        assert "import re" not in source, (
            "validation.py must parse test results via the structured "
            "pytest-json-report output, not regex against raw stdout "
            "(CLAUDE.md §4.2's principle applied to test-result parsing)"
        )

    def test_uses_json_report_flag(self):
        source = Path("app/agents/validation.py").read_text()
        assert "--json-report" in source
