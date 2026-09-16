"""Tests for the Step 9 GitHub PR Agent (`app/agents/github_pr.py`).

**Hard restriction, confirmed:** every test in this file uses
`MockGitHubClient` -- nothing is ever pushed or PR'd against any real
GitHub repository. There is no real `GITHUB_TOKEN` configured in this
environment, and this session's kickoff explicitly forbids attempting a
real push/PR regardless. `RealGitHubClient` is fully implemented in
`github_pr.py` but is never imported or instantiated anywhere in this file.

Local git operations (branch, commit) ARE real -- run against a throwaway
temp copy of `step6_sample_repo` (never the original checkout, which is
deleted after each test). That's safe (100% local, nothing external) and
gives genuine coverage of the actual diff-application logic rather than
mocking it away too.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from app.agents.github_pr import (
    MockGitHubClient,
    get_pr_eligible_file_tasks,
    open_pr_for_run,
)

STEP6_REPO = Path(__file__).parent / "fixtures" / "step6_sample_repo"


def _session():
    from db import SessionLocal

    return SessionLocal()


def _make_run(session, api_name: str):
    from db.models import ChangelogEvent, MigrationRun

    run = MigrationRun(
        repo_url=str(STEP6_REPO),
        api_name=api_name,
        version_from="1.x",
        version_to="2.0",
        status="completed",
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
    session.commit()
    return run, event


def _make_file_task(session, run, event, **overrides):
    from db.models import FileTask

    defaults = dict(
        run_id=run.id,
        file_path="service_a.py",
        status="validated",
        matched_symbol="legacy_call",
        line_start=11,
        line_end=11,
        confidence_score=0.95,
        old_code_snippet="    return oldapi.legacy_call(a, b)",
        new_code_snippet="return oldapi.new_call(a, b)",
        changelog_event_id=event.id,
    )
    defaults.update(overrides)
    task = FileTask(**defaults)
    session.add(task)
    session.flush()
    session.commit()
    return task


def _cleanup(run_id, event_id) -> None:
    from db.models import ChangelogEvent, FileTask, HumanReviewQueue, MigrationRun

    session = _session()
    try:
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
        session.query(MigrationRun).filter_by(id=run_id).delete(
            synchronize_session=False
        )
        session.query(ChangelogEvent).filter_by(id=event_id).delete(
            synchronize_session=False
        )
        session.commit()
    finally:
        session.close()


class _InspectingMockGitHubClient(MockGitHubClient):
    """Same as MockGitHubClient (records calls, never touches anything
    real) but also captures file contents + git log from `repo_dir` at
    call time, before the caller's `finally: shutil.rmtree` deletes it --
    lets tests verify the diffs were actually applied correctly, not just
    that a PR "was opened"."""

    def __init__(self, *args, files_to_capture: list[str] = (), **kwargs):
        super().__init__(*args, **kwargs)
        self._files_to_capture = list(files_to_capture)
        self.captured_files: dict[str, str] = {}
        self.captured_git_log: str = ""

    def push_and_open_pr(self, *, repo_dir, branch_name, base_branch, title, body):
        for rel_path in self._files_to_capture:
            self.captured_files[rel_path] = (repo_dir / rel_path).read_text()
        self.captured_git_log = subprocess.run(
            ["git", "log", "--oneline"], cwd=repo_dir, capture_output=True, text=True
        ).stdout
        return super().push_and_open_pr(
            repo_dir=repo_dir,
            branch_name=branch_name,
            base_branch=base_branch,
            title=title,
            body=body,
        )


# ---------------------------------------------------------------------------
# Pure query logic (Step 7's exact PR-eligibility contract).
# ---------------------------------------------------------------------------


class TestPrEligibilityQuery:
    def test_validated_and_approved_needs_review_are_eligible_others_not(self):
        from db.models import HumanReviewQueue

        session = _session()
        run, event = _make_run(session, "pr-eligibility")
        validated = _make_file_task(
            session, run, event, file_path="service_a.py", status="validated"
        )
        approved = _make_file_task(
            session,
            run,
            event,
            file_path="service_b.py",
            line_start=19,
            line_end=19,
            matched_symbol="legacy_call_approved",
            status="needs_review",
        )
        session.add(
            HumanReviewQueue(
                file_task_id=approved.id, reason="x", reviewer_decision="approved"
            )
        )
        rejected = _make_file_task(
            session,
            run,
            event,
            file_path="service_b.py",
            line_start=19,
            line_end=19,
            matched_symbol="legacy_call_rejected",
            status="needs_review",
        )
        session.add(
            HumanReviewQueue(
                file_task_id=rejected.id, reason="x", reviewer_decision="rejected"
            )
        )
        undecided = _make_file_task(
            session,
            run,
            event,
            file_path="service_b.py",
            line_start=19,
            line_end=19,
            matched_symbol="legacy_call_undecided",
            status="needs_review",
        )
        session.add(HumanReviewQueue(file_task_id=undecided.id, reason="x"))
        failed = _make_file_task(
            session,
            run,
            event,
            file_path="service_b.py",
            line_start=19,
            line_end=19,
            matched_symbol="legacy_call_failed",
            status="failed",
        )
        session.commit()
        try:
            eligible = get_pr_eligible_file_tasks(session, run.id)
            eligible_ids = {t.id for t in eligible}
            assert eligible_ids == {validated.id, approved.id}
            assert rejected.id not in eligible_ids
            assert undecided.id not in eligible_ids
            assert failed.id not in eligible_ids
        finally:
            _cleanup(run.id, event.id)


# ---------------------------------------------------------------------------
# Done-criterion: 2+ validated files -> one PR, both diffs correctly applied.
# ---------------------------------------------------------------------------


class TestMultiFilePrCreation:
    def test_pr_contains_both_diffs_correctly_applied(self):
        session = _session()
        run, event = _make_run(session, "pr-multifile")
        _make_file_task(
            session,
            run,
            event,
            file_path="service_a.py",
            status="validated",
            line_start=11,
            line_end=11,
            matched_symbol="legacy_call",
            old_code_snippet="    return oldapi.legacy_call(a, b)",
            new_code_snippet="return oldapi.new_call(a, b)",
        )
        _make_file_task(
            session,
            run,
            event,
            file_path="service_b.py",
            status="validated",
            line_start=19,
            line_end=19,
            matched_symbol="legacy_call",
            confidence_score=0.9,
            old_code_snippet="    return lc(a, b)",
            new_code_snippet="return oldapi.new_call(a, b)",
        )
        session.close()

        client = _InspectingMockGitHubClient(
            files_to_capture=["service_a.py", "service_b.py"]
        )
        session = _session()
        try:
            result = open_pr_for_run(session, run.id, STEP6_REPO, client)
            assert result is not None
            assert len(client.calls) == 1

            call = client.calls[0]
            assert "service_a.py" in call["body"]
            assert "service_b.py" in call["body"]
            assert "0.95" in call["body"]  # service_a's confidence score
            assert "0.90" in call["body"]  # service_b's confidence score
            assert call["base_branch"] == "main"
            assert run.api_name in call["branch_name"]

            # The actual diff, verified in the real (throwaway) git checkout
            # before it was deleted -- not just "a PR was opened." Line
            # 11 (the matched span) is patched; nothing else in the file
            # changed, and the deprecated call is gone.
            service_a_lines = client.captured_files["service_a.py"].splitlines()
            assert service_a_lines[10] == "    return oldapi.new_call(a, b)"
            assert "oldapi.legacy_call" not in client.captured_files["service_a.py"]

            assert "return oldapi.new_call(a, b)" in client.captured_files["service_b.py"]
            assert "return lc(a, b)" not in client.captured_files["service_b.py"]

            # Two commits: baseline (pre-migration) + the migration itself.
            assert len(client.captured_git_log.strip().splitlines()) == 2

            from db.models import MigrationRun

            refreshed = session.get(MigrationRun, run.id)
            assert refreshed.pr_url == result.url
            assert refreshed.pr_number == result.number
        finally:
            _cleanup(run.id, event.id)


# ---------------------------------------------------------------------------
# Done-criterion: needs_review items excluded, named explicitly with why.
# ---------------------------------------------------------------------------


class TestExcludedFilesNamedInDescription:
    def test_needs_review_excluded_and_named_with_reason(self):
        from db.models import HumanReviewQueue

        session = _session()
        run, event = _make_run(session, "pr-excluded")
        _make_file_task(
            session,
            run,
            event,
            file_path="service_a.py",
            status="validated",
            line_start=11,
            line_end=11,
            matched_symbol="legacy_call",
        )
        excluded_task = _make_file_task(
            session,
            run,
            event,
            file_path="service_b.py",
            status="needs_review",
            line_start=19,
            line_end=19,
            matched_symbol="legacy_call_excluded",
            confidence_score=0.6,
            old_code_snippet="    return lc(a, b)",
            new_code_snippet="return oldapi.legacy_call(a, b)",
        )
        session.add(
            HumanReviewQueue(
                file_task_id=excluded_task.id,
                reason="low_confidence_despite_passing_tests: score=0.60",
            )
        )
        session.commit()
        session.close()

        client = MockGitHubClient()
        session = _session()
        try:
            result = open_pr_for_run(session, run.id, STEP6_REPO, client)
            assert result is not None
            assert len(client.calls) == 1

            body = client.calls[0]["body"]
            assert "service_a.py" in body  # included
            assert "## Excluded from this PR" in body
            assert "service_b.py" in body  # named, not silently dropped
            assert "low_confidence_despite_passing_tests" in body
        finally:
            _cleanup(run.id, event.id)


# ---------------------------------------------------------------------------
# Done-criterion: re-running for an already-PR'd run doesn't duplicate.
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_second_call_does_not_open_a_duplicate_pr(self):
        session = _session()
        run, event = _make_run(session, "pr-idempotent")
        _make_file_task(session, run, event, status="validated")
        session.close()

        client = MockGitHubClient()
        session = _session()
        try:
            first = open_pr_for_run(session, run.id, STEP6_REPO, client)
            session.close()

            session = _session()
            second = open_pr_for_run(session, run.id, STEP6_REPO, client)

            assert len(client.calls) == 1  # not called twice
            assert second.number == first.number
            assert second.url == first.url
        finally:
            _cleanup(run.id, event.id)

    def test_no_eligible_files_returns_none_and_opens_nothing(self):
        session = _session()
        run, event = _make_run(session, "pr-nothing-eligible")
        _make_file_task(session, run, event, status="needs_review")
        session.close()

        client = MockGitHubClient()
        session = _session()
        try:
            result = open_pr_for_run(session, run.id, STEP6_REPO, client)
            assert result is None
            assert len(client.calls) == 0
        finally:
            _cleanup(run.id, event.id)
