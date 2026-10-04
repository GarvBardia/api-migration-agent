"""Validation Agent (Step 5).

Given a `file_tasks` row with `new_code_snippet` set (produced by Step 4's
Migration Agent), applies the patch to a throwaway copy of the target repo,
runs the repo's test suite inside an ephemeral, network-disabled, resource-
limited Docker container, and routes the task to `validated` or (after
exhausting retries) `needs_review` based on a structured test-result report
-- never by scraping raw pytest stdout.

**Where this runs (2026-08-26 note):** the backend runs directly on the
host via the Python venv, not inside the `docker-compose.yml` `backend`
service (no `backend/Dockerfile` exists yet -- a known gap since Step 1,
relevant only once Step 7 containerizes the backend). This module talks to
the local Docker daemon directly via the `docker` CLI (subprocess), not
through any docker-in-docker socket-mount indirection. See CLAUDE.md §4.4.

Deliberately NOT in scope here (belongs to later steps):
- Orchestrator/Celery wiring -- this module is called directly for now.
- FastAPI/SSE endpoints.
- GitHub PR agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .llm_providers import MigrationLLMProvider
from .migration import migrate_file_task

DEFAULT_SANDBOX_IMAGE = "migration-agent-sandbox:latest"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MEMORY = "512m"
DEFAULT_CPUS = "1"
DEFAULT_MAX_RETRIES = 3
# Matches the "high >= 0.8" confidence tier already defined in the Step 3
# kickoff prompt. CLAUDE.md documents confidence_score as a trust gate --
# below this, a file_task must not be marked validated even if its tests
# passed. Added 2026-08-26 (Step 6a session, Phase 0): validate_file_task
# previously only routed to needs_review on exhausted retries, never on
# low confidence with passing tests -- see the dated note in CLAUDE.md.
DEFAULT_CONFIDENCE_THRESHOLD = 0.8

REPORT_FILENAME = ".step5_pytest_report.json"

# Added 2026-08-27, containerization session -- see the dated note in
# CLAUDE.md. When this process itself runs inside `celery-worker`
# (docker-compose.yml), it talks to the HOST's Docker daemon via the
# mounted socket, not a daemon of its own. `docker run -v <src>:<dst>`'s
# `<src>` is resolved by that daemon against ITS OWN filesystem (the host's)
# -- it has no visibility into paths that only exist inside celery-worker's
# own container. A `tempfile.mkdtemp()`'d path (celery-worker-local) passed
# straight through, as this module always did when running on the host
# directly, would silently bind-mount an empty/nonexistent directory on the
# host instead of the patched repo copy. Fixed by bind-mounting one shared
# directory into celery-worker at a known path (`SANDBOX_CONTAINER_TMP_DIR`)
# from a known HOST path (`SANDBOX_HOST_TMP_DIR`), creating temp copies
# inside it, and translating that prefix when constructing the `-v` flag.
# Both unset (the default, and always true for the host-run test suite)
# means container-local and host paths are identical -- no translation.
SANDBOX_CONTAINER_TMP_DIR = os.environ.get("SANDBOX_CONTAINER_TMP_DIR")
SANDBOX_HOST_TMP_DIR = os.environ.get("SANDBOX_HOST_TMP_DIR")


def _host_visible_path(container_path: Path) -> str:
    """Translate a path as seen by THIS process into the equivalent path
    as seen by the Docker daemon `docker run -v` will actually talk to.
    See the module-level note above."""

    if SANDBOX_CONTAINER_TMP_DIR and SANDBOX_HOST_TMP_DIR:
        container_prefix = str(Path(SANDBOX_CONTAINER_TMP_DIR).resolve())
        container_str = str(container_path)
        if container_str.startswith(container_prefix):
            return SANDBOX_HOST_TMP_DIR.rstrip("/\\") + container_str[
                len(container_prefix) :
            ].replace("\\", "/")
    return str(container_path)


class SandboxSetupError(Exception):
    """Raised when the sandbox itself can't run (docker missing, image
    missing, etc.) -- a genuine infrastructure problem, distinct from a
    test failure inside the sandbox."""


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of one sandboxed test run, parsed from a structured report."""

    passed: bool
    pass_count: int
    fail_count: int
    failure_detail: str | None
    timed_out: bool
    raw_returncode: int | None


def _docker_binary() -> str:
    # On Windows, Docker Desktop's bin dir contains both a bare `docker`
    # (a non-executable wrapper script) and `docker.exe` -- shutil.which
    # matches the bare name first if it exists on PATH, which then fails at
    # spawn time with WinError 193 ("not a valid Win32 application"). Prefer
    # the explicit `.exe` name first to avoid that trap.
    found = shutil.which("docker.exe") or shutil.which("docker")
    if found:
        return found
    # Fallback for this dev machine's per-user Docker Desktop install, which
    # doesn't put docker.exe on PATH by default.
    candidate = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs"
        / "DockerDesktop"
        / "resources"
        / "bin"
        / "docker.exe"
    )
    if candidate.exists():
        return str(candidate)
    raise SandboxSetupError(
        "Could not locate the `docker` CLI on PATH or at the known Docker "
        "Desktop install location. Is Docker Desktop installed and running?"
    )


def run_pytest_in_sandbox(
    repo_dir: Path,
    *,
    test_target: str | None = None,
    image: str = DEFAULT_SANDBOX_IMAGE,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    memory: str = DEFAULT_MEMORY,
    cpus: str = DEFAULT_CPUS,
) -> SandboxResult:
    """Run pytest against `repo_dir` inside one ephemeral container.

    Uses `pytest --json-report` (pytest-json-report plugin, baked into the
    sandbox image at build time -- see `backend/sandbox/Dockerfile`) written
    to a file inside the mounted repo dir and read back afterward, rather
    than parsing stdout with regex -- same "no fragile heuristics on
    unstructured output" principle as CLAUDE.md §4.2 applied to code
    matching.

    `test_target` (added 2026-08-26, Step 6a session): a relative path,
    scoping the run to one test file/dir instead of the whole repo. Needed
    once more than one file_task can be mid-migration in the same repo
    copy at once (Step 6a's group/chord fan-out): `_apply_patch_to_temp_copy`
    only patches ONE file per validation run, so every other file_task's
    file is still in its pre-migration state in that same temp copy -- an
    unscoped whole-suite run would attribute a still-broken, unrelated
    file's test failures to the file actually being validated. `None` (the
    default) runs the whole suite, preserving prior single-file-repo
    behavior (e.g. Step 5's fixture).
    """

    docker = _docker_binary()
    repo_dir = repo_dir.resolve()  # docker -v requires an absolute host path
    container_name = f"migration-agent-sandbox-{uuid.uuid4().hex[:12]}"
    report_path_in_container = f"/repo/{REPORT_FILENAME}"
    # `report_path_on_host` here means "on the filesystem THIS process
    # reads from" -- i.e. still `repo_dir`, container-local when running
    # inside celery-worker. Only the `-v` source (below) needs translating
    # to what the daemon itself can see.
    report_path_on_host = repo_dir / REPORT_FILENAME

    cmd = [
        docker,
        "run",
        "--name",
        container_name,
        "--rm",
        "--network",
        "none",
        f"--memory={memory}",
        f"--cpus={cpus}",
        # Added 2026-10-04 (public-demo hardening): cheap isolation flags.
        "--pids-limit=256",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "-v",
        f"{_host_visible_path(repo_dir)}:/repo",
        "-w",
        "/repo",
        image,
        "pytest",
        "-q",
        "--json-report",
        f"--json-report-file={report_path_in_container}",
    ]
    if test_target:
        cmd.append(test_target)

    # Added 2026-08-27, Step 8a session: `.env`'s DOCKER_HOST
    # (unix:///var/run/docker.sock) is explicitly documented as "unused
    # until Step 7 containerizes the backend" -- but now that something
    # loads .env into the process (celery_app.py/app/api/main.py), a
    # locally-run `docker` subprocess call inherits it and tries to
    # connect via a Linux unix socket instead of the Windows named pipe,
    # breaking the sandbox entirely. Strip it for this subprocess only, so
    # `docker` falls back to its platform default regardless of what the
    # parent process's environment carries.
    docker_env = {k: v for k, v in os.environ.items() if k != "DOCKER_HOST"}

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_seconds, env=docker_env
        )
    except subprocess.TimeoutExpired:
        # `--rm` cleans up on any stop, including a kill -- but the
        # subprocess timeout only kills our client process, not the
        # container the daemon is still running. Kill it explicitly.
        subprocess.run(
            [docker, "kill", container_name], capture_output=True, env=docker_env
        )
        return SandboxResult(
            passed=False,
            pass_count=0,
            fail_count=0,
            failure_detail=f"Sandbox run timed out after {timeout_seconds}s and was killed.",
            timed_out=True,
            raw_returncode=None,
        )

    if not report_path_on_host.exists():
        return SandboxResult(
            passed=False,
            pass_count=0,
            fail_count=0,
            failure_detail=(
                "Sandbox produced no structured test report "
                f"(returncode={proc.returncode}); stdout={proc.stdout!r} "
                f"stderr={proc.stderr!r}"
            ),
            timed_out=False,
            raw_returncode=proc.returncode,
        )

    try:
        report = json.loads(report_path_on_host.read_text())
    finally:
        report_path_on_host.unlink(missing_ok=True)

    summary = report.get("summary", {})
    pass_count = summary.get("passed", 0)
    fail_count = summary.get("failed", 0) + summary.get("error", 0)
    passed = fail_count == 0 and pass_count > 0

    failure_detail = None
    if not passed:
        failing = [
            t for t in report.get("tests", []) if t.get("outcome") != "passed"
        ]
        details = []
        for t in failing:
            nodeid = t.get("nodeid", "<unknown test>")
            longrepr = (
                t.get("call", {}).get("longrepr")
                or t.get("setup", {}).get("longrepr")
                or t.get("longrepr")
                or "(no failure detail captured)"
            )
            details.append(f"{nodeid}: {longrepr}")
        failure_detail = "; ".join(details) if details else (
            f"pytest reported {fail_count} failure(s) but no per-test detail "
            f"was found in the report (pass_count={pass_count})"
        )

    return SandboxResult(
        passed=passed,
        pass_count=pass_count,
        fail_count=fail_count,
        failure_detail=failure_detail,
        timed_out=False,
        raw_returncode=proc.returncode,
    )


def _guess_test_target(repo_root: Path, file_path: str) -> str | None:
    """Best-effort convention-based mapping from a source file to its test
    file: `<dir>/service_a.py` -> `<dir>/test_service_a.py`.

    Known simplification (2026-08-26, Step 6a session): assumes a simple
    1:1 naming convention between source and test files, which holds for
    this project's fixture repos but not for arbitrary real-world test
    suites (e.g. integration tests spanning multiple modules, or a
    `tests/` directory mirroring `src/`). Returns `None` (run the whole
    suite) if no such file exists, rather than guessing further -- same
    "surface uncertainty, don't guess" principle as the rest of this
    pipeline.
    """

    source_path = Path(file_path)
    candidate = source_path.parent / f"test_{source_path.stem}.py"
    if (repo_root / candidate).exists():
        return candidate.as_posix()
    return None


def _apply_patch_to_temp_copy(repo_root: Path, file_task) -> Path | None:
    """Splice `new_code_snippet` into a throwaway copy of `repo_root`.

    Fork #2 from the Step 5 kickoff: before splicing, confirm the real
    file's current content at `line_start`-`line_end` still matches
    `old_code_snippet` exactly. A mismatch means the file changed since
    Step 3 scanned it (or something's misaligned) -- that's a hard error,
    routed to `needs_review` with a clear reason, never guessed at. Returns
    `None` in that case (with `file_task` already updated); otherwise
    returns the path to the patched temp copy.

    The original snippet's leading indentation is re-applied to the
    (already-dedented, see migration.py) `new_code_snippet` so the spliced
    file stays syntactically consistent with its surrounding block.
    """

    source_path = repo_root / file_task.file_path
    original_lines = source_path.read_text().splitlines()
    actual_snippet = "\n".join(
        original_lines[file_task.line_start - 1 : file_task.line_end]
    )

    if actual_snippet != file_task.old_code_snippet:
        file_task.status = "needs_review"
        file_task.failure_reason = (
            "old_code_snippet no longer matches the real file at "
            f"{file_task.file_path}:{file_task.line_start}-{file_task.line_end}. "
            f"Expected {file_task.old_code_snippet!r}, found {actual_snippet!r}. "
            "The file may have changed since Step 3 scanned it, or "
            "line_start/line_end is misaligned -- not applying a guessed patch."
        )
        return None

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

    # Create the temp copy inside the shared sandbox-tmp dir when one is
    # configured (celery-worker, containerized) so it's visible to the
    # host Docker daemon under a translatable path -- see the
    # SANDBOX_CONTAINER_TMP_DIR note above `_host_visible_path`. Falls back
    # to the OS default temp dir when unset (host-run dev/tests, unchanged
    # from before this session).
    temp_dir = Path(
        tempfile.mkdtemp(
            prefix="migration-agent-validate-", dir=SANDBOX_CONTAINER_TMP_DIR
        )
    )
    shutil.copytree(repo_root, temp_dir, dirs_exist_ok=True)
    (temp_dir / file_task.file_path).write_text("\n".join(patched_lines) + "\n")
    return temp_dir


def validate_file_task(
    session,
    file_task,
    repo_root: Path,
    provider: MigrationLLMProvider,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE,
    sandbox_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    sandbox_memory: str = DEFAULT_MEMORY,
    sandbox_cpus: str = DEFAULT_CPUS,
) -> None:
    """Validate one `file_task`, retrying through the Migration Agent on
    failure up to `max_retries`, per CLAUDE.md's documented default.

    Idempotent: a task already at `status='validated'` is a no-op (Step 5
    done-criterion 9) -- re-running validation never re-runs a passing task.

    Confidence gate (Phase 0, added 2026-08-26): passing tests is necessary
    but not sufficient for `status='validated'`. If the file_task's
    confidence_score is still below `confidence_threshold` even though the
    sandbox run passed, it routes to `needs_review` instead, with a
    `human_review_queue` row explaining why -- passing tests alone doesn't
    override a low-confidence match (e.g. one the Impact Analysis Agent
    could only resolve via dynamic dispatch).
    """

    from db.models import HumanReviewQueue

    if file_task.status == "validated":
        return

    test_target = _guess_test_target(repo_root, file_task.file_path)

    while True:
        temp_repo = _apply_patch_to_temp_copy(repo_root, file_task)
        if temp_repo is None:
            # _apply_patch_to_temp_copy already set needs_review.
            session.flush()
            return

        try:
            result = run_pytest_in_sandbox(
                temp_repo,
                test_target=test_target,
                image=sandbox_image,
                timeout_seconds=sandbox_timeout_seconds,
                memory=sandbox_memory,
                cpus=sandbox_cpus,
            )
        finally:
            shutil.rmtree(temp_repo, ignore_errors=True)

        if result.passed:
            file_task.test_pass_count += 1
            file_task.confidence_score = min(
                1.0, (file_task.confidence_score or 0.0) + 0.15
            )
            file_task.failure_reason = None

            if file_task.confidence_score < confidence_threshold:
                file_task.status = "needs_review"
                session.add(
                    HumanReviewQueue(
                        file_task_id=file_task.id,
                        reason=(
                            "low_confidence_despite_passing_tests: score="
                            f"{file_task.confidence_score:.2f} is below the "
                            f"{confidence_threshold:.2f} threshold"
                        ),
                    )
                )
                session.flush()
                return

            file_task.status = "validated"
            session.flush()
            return

        file_task.test_fail_count += 1
        file_task.retry_count += 1
        file_task.failure_reason = result.failure_detail or (
            "Sandbox test run failed with no further detail."
        )

        if file_task.retry_count < max_retries:
            failure_context = (
                f"Previous new_code_snippet:\n{file_task.new_code_snippet}\n"
                f"Test failure: {file_task.failure_reason}"
            )
            migrate_file_task(
                session,
                file_task,
                repo_root,
                provider,
                failure_context=failure_context,
            )
            if file_task.status == "needs_review":
                # The Migration Agent itself failed on retry (provider
                # error / invalid Python) -- it already set failure_reason.
                session.flush()
                return
            session.flush()
            continue  # loop back and validate the newly re-migrated code

        file_task.status = "needs_review"
        session.add(
            HumanReviewQueue(
                file_task_id=file_task.id,
                reason=f"retries_exhausted: {file_task.failure_reason}",
            )
        )
        session.flush()
        return
