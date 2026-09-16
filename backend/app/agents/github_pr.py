"""GitHub PR Agent (Step 9).

Given a `migration_runs` row, finds every `file_tasks` row that meets Step
7's PR-eligibility contract (see CLAUDE.md's `human_review_queue` section
-- reproduced verbatim below), branches off a local copy of the target
repo, applies each eligible file's `new_code_snippet`, commits, "pushes"
and opens a PR via a pluggable `GitHubClient`, and records the PR on the
`migration_runs` row so a second call is idempotent.

**PR-eligibility query (verbatim from CLAUDE.md/Step 7):**
    status = 'validated'
       OR (status = 'needs_review' AND reviewer_decision = 'approved')

**Hard restriction this session, per the run-while-away/overnight kickoff:**
there is no real `GITHUB_TOKEN` configured, and even if there were, this
session does not push or open a PR against any real GitHub repository.
`RealGitHubClient` (PyGithub-backed) is fully implemented below -- not a
stub -- but is never instantiated or called anywhere in this session's
tests; every test uses `MockGitHubClient`. Local git operations (branch,
commit) ARE real, run against a throwaway temp copy of the target repo
(never the original checkout) -- they touch nothing external, so mocking
them would only reduce genuine coverage for no safety benefit. Only the
"push to a remote" + "open a PR via the GitHub API" step -- the part that
would touch a real external service -- goes through the mockable
`GitHubClient` interface.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class GitOperationError(Exception):
    """Raised when a local git command fails."""


@dataclass(frozen=True)
class PullRequestResult:
    number: int
    url: str


class GitHubClient(Protocol):
    """The one thing that would touch a real external service. Real
    implementation (`RealGitHubClient`) exists below but is never invoked
    live this session -- every test uses `MockGitHubClient`.
    """

    def push_and_open_pr(
        self,
        *,
        repo_dir: Path,
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequestResult:
        """Push `branch_name` to the remote and open a PR against
        `base_branch`. Returns the created PR's number and URL."""
        ...


class RealGitHubClient:
    """PyGithub-backed implementation -- fully implemented, NOT a stub, but
    not exercised live in this session (no real GITHUB_TOKEN is configured,
    and the hard restriction for this session is: don't push or open a PR
    against any real repository regardless).
    """

    def __init__(self, token: str, repo_full_name: str, remote_name: str = "origin"):
        if not token:
            raise ValueError("GITHUB_TOKEN is required for RealGitHubClient")
        from github import Github  # deferred import -- PyGithub

        self._gh = Github(token)
        self._repo_full_name = repo_full_name
        self._remote_name = remote_name

    def push_and_open_pr(
        self,
        *,
        repo_dir: Path,
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequestResult:
        _run_git(repo_dir, ["push", self._remote_name, branch_name])
        repo = self._gh.get_repo(self._repo_full_name)
        pr = repo.create_pull(
            title=title, body=body, head=branch_name, base=base_branch
        )
        return PullRequestResult(number=pr.number, url=pr.html_url)


@dataclass
class MockGitHubClient:
    """Records calls instead of touching any real service. Used by every
    test this session -- this is the ONLY GitHubClient implementation
    actually exercised tonight."""

    calls: list[dict] = field(default_factory=list)
    next_pr_number: int = 101
    fake_repo_full_name: str = "example-org/example-repo"

    def push_and_open_pr(
        self,
        *,
        repo_dir: Path,
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequestResult:
        self.calls.append(
            {
                "repo_dir": str(repo_dir),
                "branch_name": branch_name,
                "base_branch": base_branch,
                "title": title,
                "body": body,
            }
        )
        number = self.next_pr_number
        self.next_pr_number += 1
        return PullRequestResult(
            number=number,
            url=f"https://github.com/{self.fake_repo_full_name}/pull/{number}",
        )


def _run_git(cwd: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise GitOperationError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr}"
        )
    return proc.stdout


def get_pr_eligible_file_tasks(session, run_id: uuid.UUID) -> list:
    """Step 7's exact PR-eligibility contract, applied here verbatim:
    `status='validated' OR (status='needs_review' AND
    reviewer_decision='approved')`.
    """

    from db.models import FileTask, HumanReviewQueue

    validated = (
        session.query(FileTask)
        .filter(FileTask.run_id == run_id, FileTask.status == "validated")
        .all()
    )
    approved = (
        session.query(FileTask)
        .join(HumanReviewQueue, HumanReviewQueue.file_task_id == FileTask.id)
        .filter(
            FileTask.run_id == run_id,
            FileTask.status == "needs_review",
            HumanReviewQueue.reviewer_decision == "approved",
        )
        .all()
    )
    seen_ids = set()
    result = []
    for task in [*validated, *approved]:
        if task.id not in seen_ids:
            seen_ids.add(task.id)
            result.append(task)
    return result


def get_excluded_file_tasks(session, run_id: uuid.UUID, eligible_ids: set) -> list:
    """Every non-eligible file_task for the run -- named explicitly in the
    PR description (done-criterion: "the PR description explicitly lists
    what was excluded and why"), not silently dropped."""

    from db.models import FileTask

    all_tasks = session.query(FileTask).filter(FileTask.run_id == run_id).all()
    return [t for t in all_tasks if t.id not in eligible_ids]


def _exclusion_reason(session, file_task) -> str:
    from db.models import HumanReviewQueue

    if file_task.status == "failed":
        return file_task.failure_reason or "status=failed, no further detail recorded"
    if file_task.status == "needs_review":
        review = (
            session.query(HumanReviewQueue)
            .filter_by(file_task_id=file_task.id)
            .order_by(HumanReviewQueue.created_at.desc())
            .first()
        )
        if review is not None and review.reviewer_decision == "rejected":
            return f"needs_review, rejected by reviewer: {review.reason}"
        if review is not None:
            return f"needs_review, awaiting/undecided: {review.reason}"
        return "needs_review, no human_review_queue row found"
    return f"status={file_task.status}, not yet eligible"


def _apply_snippet_patch(repo_dir: Path, file_task) -> bool:
    """Splice `new_code_snippet` into `repo_dir` (already a working git
    checkout). Returns False (and applies nothing) if `old_code_snippet` no
    longer matches the real file's current content -- same hard safety
    check as Step 5's `_apply_patch_to_temp_copy`, never guess at a stale
    splice location.
    """

    source_path = repo_dir / file_task.file_path
    original_lines = source_path.read_text().splitlines()
    actual_snippet = "\n".join(
        original_lines[file_task.line_start - 1 : file_task.line_end]
    )
    if actual_snippet != file_task.old_code_snippet:
        return False

    first_line = original_lines[file_task.line_start - 1]
    indent = first_line[: len(first_line) - len(first_line.lstrip())]
    new_lines = [
        indent + line if line.strip() else line
        for line in file_task.new_code_snippet.splitlines()
    ]
    patched_lines = (
        original_lines[: file_task.line_start - 1]
        + new_lines
        + original_lines[file_task.line_end :]
    )
    source_path.write_text("\n".join(patched_lines) + "\n")
    return True


def _build_pr_description(
    run, eligible_tasks: list, excluded: list, session
) -> str:
    lines = [
        f"Automated migration: `{run.api_name}` {run.version_from} -> {run.version_to}",
        "",
        "## Files changed",
    ]
    for t in eligible_tasks:
        lines.append(
            f"- `{t.file_path}` (line {t.line_start}, symbol `{t.matched_symbol}`, "
            f"confidence {t.confidence_score:.2f})"
        )
    if not eligible_tasks:
        lines.append("(none)")

    lines += ["", "## Excluded from this PR"]
    if not excluded:
        lines.append("(none -- every matched file_task was PR-eligible)")
    for t in excluded:
        reason = _exclusion_reason(session, t)
        lines.append(f"- `{t.file_path}` (line {t.line_start}): {reason}")

    return "\n".join(lines)


def open_pr_for_run(
    session,
    run_id: uuid.UUID,
    repo_root: Path,
    github_client: GitHubClient,
    base_branch: str = "main",
) -> PullRequestResult | None:
    """Idempotent: a run that already has `pr_url`/`pr_number` set returns
    that existing PR info without doing anything else -- no duplicate PR.

    Returns `None` if there's nothing PR-eligible for this run (a
    legitimate "no work to do" outcome, not an error).
    """

    from db.models import MigrationRun

    run = session.get(MigrationRun, run_id)
    if run is None:
        raise ValueError(f"migration_runs row {run_id} not found")

    if run.pr_url is not None:
        return PullRequestResult(number=run.pr_number, url=run.pr_url)

    eligible_tasks = get_pr_eligible_file_tasks(session, run_id)
    if not eligible_tasks:
        return None

    excluded_tasks = get_excluded_file_tasks(
        session, run_id, {t.id for t in eligible_tasks}
    )

    temp_dir = Path(tempfile.mkdtemp(prefix="migration-agent-pr-"))
    import shutil

    shutil.copytree(repo_root, temp_dir, dirs_exist_ok=True)
    try:
        _run_git(temp_dir, ["init", "-q"])
        _run_git(temp_dir, ["config", "user.email", "migration-agent@example.com"])
        _run_git(temp_dir, ["config", "user.name", "Migration Agent"])
        _run_git(temp_dir, ["add", "-A"])
        _run_git(temp_dir, ["commit", "-q", "-m", "baseline (pre-migration)"])

        branch_name = f"migration-agent/{run.api_name}-{str(run.id)[:8]}"
        _run_git(temp_dir, ["checkout", "-q", "-b", branch_name])

        applied = []
        for task in eligible_tasks:
            if _apply_snippet_patch(temp_dir, task):
                applied.append(task)
        if not applied:
            return None

        _run_git(temp_dir, ["add", "-A"])
        _run_git(
            temp_dir,
            [
                "commit",
                "-q",
                "-m",
                f"Migrate {run.api_name} {run.version_from} -> {run.version_to}",
            ],
        )

        title = f"Migrate {run.api_name} {run.version_from} -> {run.version_to}"
        body = _build_pr_description(run, applied, excluded_tasks, session)

        pr_result = github_client.push_and_open_pr(
            repo_dir=temp_dir,
            branch_name=branch_name,
            base_branch=base_branch,
            title=title,
            body=body,
        )

        run.pr_url = pr_result.url
        run.pr_number = pr_result.number
        session.commit()
        return pr_result
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
