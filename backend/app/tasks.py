"""Celery task graph wiring Steps 3->4->5 into an orchestrated run (Step 6a).

    scan_task(run_id)
            |  (chain)
            v
    dispatch_file_tasks(file_task_ids, run_id, repo_root)
            |  builds one migrate_task->validate_task chain per file_task,
            |  fans them out as a Celery group, wrapped in a chord
            v
    chord( group[ chain(migrate_task, validate_task), ... ] )(finalize_run)
            |
            v
    finalize_run(run_id)  -- fires once every chain has reached a terminal
                             file_task status; sets migration_runs.status

Each per-file chain is exactly two tasks: `migrate_task` (Step 4's
`migrate_file_task`) then `validate_task` (Step 5's `validate_file_task`,
which now includes the Phase 0 confidence-gate fix). `validate_file_task`'s
own retry loop is self-contained -- it calls back into `migrate_file_task`
directly on a test failure -- so this chain does NOT need its own separate
retry task; that would duplicate Step 5's retry logic at the orchestration
layer.

**Local-checkout simplification, not yet a real GitHub clone step:**
`migration_runs.repo_url` is used here as a local filesystem path to an
already-checked-out target repo, not a remote GitHub URL. There is no
clone/checkout agent yet -- that's Step 9's GitHub PR agent territory, which
this session deliberately doesn't touch. `start_migration_run` documents
this; revisit when Step 9 adds real repo cloning.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from celery import chain, chord, group
from sqlalchemy import func

from app.celery_app import celery_app
from app.agents.resume import ResumeAction, plan_and_claim_resume

TERMINAL_FILE_TASK_STATUSES = {"validated", "failed", "needs_review"}


def _session():
    from db import SessionLocal

    return SessionLocal()


# ---------------------------------------------------------------------------
# Webhook notifications (added 2026-09-16, beyond the original 10-step
# plan -- see CLAUDE.md's dated note and app/agents/notifications.py's
# module docstring for the full "best-effort, never blocks the pipeline"
# contract). Hooked in HERE, in the Celery-glue layer, rather than inside
# migration.py/validation.py's own status-assignment branches: those
# modules stay DB-only/Celery-independent (same split app/agents/resume.py
# already established), and there are only two call sites to check after
# a commit here (migrate_task, validate_task) versus several scattered
# `status = "needs_review"` assignments across the agent modules.
# ---------------------------------------------------------------------------


def _notify_if_needs_review(file_task) -> None:
    if file_task.status == "needs_review":
        from app.agents.notifications import notify_needs_review

        notify_needs_review(file_task)


def _notify_run_terminal(session, run) -> None:
    from db.models import FileTask
    from app.agents.notifications import notify_run_terminal

    rows = (
        session.query(FileTask.status, func.count(FileTask.id))
        .filter_by(run_id=run.id)
        .group_by(FileTask.status)
        .all()
    )
    notify_run_terminal(run, {status: count for status, count in rows})


@celery_app.task(name="app.tasks.scan_task", bind=True)
def scan_task(self, run_id: str) -> list[str]:
    """Step 3: scan the run's target repo, persist `file_tasks` rows, then
    mark the run 'running'. Returns the created file_task ids (as strings)
    so `dispatch_file_tasks` can fan out per-file chains.

    On any scan error, sets `migration_runs.status = 'failed'` and
    re-raises -- a run whose scan itself errors is a failed run (done-
    criterion 5), distinct from a scan that runs cleanly and finds zero
    matches (done-criterion 4: that's a legitimate completed run with no
    work to do, not a failure).
    """

    from db.models import ChangelogEvent, FileTask, MigrationRun
    from app.agents.impact_analysis import persist_matches, scan_repo

    session = _session()
    try:
        run = session.get(MigrationRun, uuid.UUID(run_id))
        if run is None:
            raise ValueError(f"migration_runs row {run_id} not found")

        from app.repo_paths import validate_repo_path

        # Raises RepoPathError (-> run marked failed below) for a missing,
        # non-directory, or out-of-root path. Never "completed" with 0 results.
        repo_root = validate_repo_path(run.repo_url)
        changelog_events = (
            session.query(ChangelogEvent)
            .filter_by(
                api_name=run.api_name,
                version_from=run.version_from,
                version_to=run.version_to,
            )
            .all()
        )

        matches = scan_repo(repo_root, changelog_events)
        persist_matches(session, run.id, matches)

        run.status = "running"
        session.commit()

        file_task_ids = [
            str(t.id)
            for t in session.query(FileTask).filter_by(run_id=run.id).all()
        ]
        return file_task_ids
    except Exception:
        session.rollback()
        run = session.get(MigrationRun, uuid.UUID(run_id))
        if run is not None:
            run.status = "failed"
            session.commit()
            _notify_run_terminal(session, run)
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.migrate_task")
def migrate_task(file_task_id: str, repo_root: str) -> str:
    """Step 4. Idempotent: skipped once migration is actually done (a
    terminal status, or `new_code_snippet` already populated) -- already-
    migrated work is never re-sent to the Migration Agent (CLAUDE.md §4.1).

    **Updated 2026-08-26, Step 6b session:** this used to skip on ANY
    status other than `'pending'`. That broke crash-resume (fork #2, see
    `app/agents/resume.py`): the atomic claim guard for a stale `pending`
    row flips its status straight to `'in_progress'` as the claim itself
    (matching the kickoff's `UPDATE ... WHERE status='pending' ...`
    pattern) -- so by the time a resumed chain's `migrate_task` actually
    ran, the old `status != 'pending'` check saw `'in_progress'` and
    silently skipped calling the Migration Agent entirely, leaving
    `new_code_snippet` stuck at `None` forever. The real signal for
    "migration already happened" is `new_code_snippet IS NOT NULL`
    (CLAUDE.md §3 already documents this), not the status value alone --
    fixed to check that instead.

    **Updated 2026-08-27, gap-fix session:** now passes a real Redis
    client through to `migrate_file_task`, enabling the Step 10 semantic-
    cache-backed doc-context retrieval step for real Celery-dispatched
    migrations (previously the cache module existed but nothing in the
    actual call path ever used it).
    """

    from db.models import FileTask
    from app.agents.llm_providers import get_provider
    from app.agents.migration import migrate_file_task

    session = _session()
    try:
        file_task = session.get(FileTask, uuid.UUID(file_task_id))
        if file_task is None:
            raise ValueError(f"file_tasks row {file_task_id} not found")
        if (
            file_task.status in TERMINAL_FILE_TASK_STATUSES
            or file_task.new_code_snippet is not None
        ):
            return file_task_id

        provider = get_provider()
        redis_client = celery_app.backend.client  # reuse Celery's own Redis connection
        migrate_file_task(
            session, file_task, Path(repo_root), provider, redis_client=redis_client
        )
        session.commit()
        _notify_if_needs_review(file_task)
        return file_task_id
    finally:
        session.close()


@celery_app.task(name="app.tasks.validate_task")
def validate_task(file_task_id: str, repo_root: str) -> str:
    """Step 5 (with the Phase 0 confidence-gate fix). `validate_file_task`
    is already idempotent for an already-`validated` task, and its retry
    loop is self-contained -- this task does not add its own retry logic."""

    from db.models import FileTask
    from app.agents.llm_providers import get_provider
    from app.agents.validation import validate_file_task

    session = _session()
    try:
        file_task = session.get(FileTask, uuid.UUID(file_task_id))
        if file_task is None:
            raise ValueError(f"file_tasks row {file_task_id} not found")

        provider = get_provider()
        validate_file_task(session, file_task, Path(repo_root), provider)
        session.commit()
        _notify_if_needs_review(file_task)
        return file_task_id
    finally:
        session.close()


@celery_app.task(name="app.tasks.finalize_run")
def finalize_run(_group_results, run_id: str) -> str:
    """Chord callback -- fires once every chain in the group has reached a
    terminal file_task status. Sets `migration_runs.status = 'completed'`.

    Does not itself re-check each file_task's terminal-ness: each chain's
    last task (`validate_task`) only returns once `validate_file_task` has
    put its file_task into a terminal state (`validated`/`needs_review`; a
    task crash before that would fail the chain/chord instead of reaching
    here at all), so by the time the chord fires, every linked file_task is
    already terminal.
    """

    from db.models import MigrationRun

    session = _session()
    try:
        run = session.get(MigrationRun, uuid.UUID(run_id))
        if run is None:
            raise ValueError(f"migration_runs row {run_id} not found")
        run.status = "completed"
        session.commit()
        _notify_run_terminal(session, run)
        return run_id
    finally:
        session.close()


def _build_file_chain(file_task_id: str, repo_root: str):
    return chain(
        migrate_task.si(file_task_id, repo_root),
        validate_task.si(file_task_id, repo_root),
    )


@celery_app.task(name="app.tasks.dispatch_file_tasks")
def dispatch_file_tasks(file_task_ids: list[str], run_id: str, repo_root: str) -> str:
    """Fan out one migrate->validate chain per file_task as a Celery group,
    wrapped in a chord whose callback (`finalize_run`) fires once every
    chain has finished.

    A run whose scan found zero matches (`file_task_ids` empty) is NOT a
    failure -- it's a legitimate completed run with no work to do (done-
    criterion 4), so `finalize_run` is invoked directly rather than trying
    to build a chord over an empty group (Celery chords require at least
    one task).
    """

    if not file_task_ids:
        finalize_run.delay(None, run_id)
        return run_id

    file_chains = [_build_file_chain(fid, repo_root) for fid in file_task_ids]
    chord(group(file_chains))(finalize_run.s(run_id))
    return run_id


def start_migration_run(
    repo_path: str, api_name: str, version_from: str, version_to: str
) -> str:
    """Entry point: creates the `migration_runs` row at `status='pending'`
    and kicks off scan -> dispatch -> group/chord. Returns the new run's id
    (str) immediately; the pipeline runs asynchronously via Celery.
    """

    from db.models import MigrationRun

    session = _session()
    try:
        run = MigrationRun(
            repo_url=str(repo_path),
            api_name=api_name,
            version_from=version_from,
            version_to=version_to,
            status="pending",
        )
        session.add(run)
        session.commit()
        run_id = str(run.id)
    finally:
        session.close()

    chain(
        scan_task.s(run_id),
        dispatch_file_tasks.s(run_id, str(repo_path)),
    ).apply_async()

    return run_id


# ---------------------------------------------------------------------------
# Step 6b: crash-resume, dead-task detection, orchestration-level retry.
#
# `app/agents/resume.py` holds the decision logic (claims + what-to-do),
# kept DB-only/Celery-independent so it's directly unit-testable. This
# section is the Celery-specific glue: actually dispatching tasks per that
# decision, and the periodic sweep task itself.
# ---------------------------------------------------------------------------


def _dispatch_resume_action(action: "ResumeAction", file_task_id: str, repo_root: str) -> bool:
    """Perform the Celery dispatch for one `plan_and_claim_resume` decision.
    Returns True if this file_task was acted on in any way (dispatched or
    given up on) -- used by callers to report how many rows a sweep/resume
    call actually touched.
    """

    if action == ResumeAction.DISPATCH_CHAIN:
        _build_file_chain(file_task_id, repo_root).apply_async()
        return True
    if action == ResumeAction.DISPATCH_VALIDATE_ONLY:
        validate_task.si(file_task_id, repo_root).apply_async()
        return True
    if action == ResumeAction.GAVE_UP:
        return True
    return False  # SKIP -- terminal already, or lost the claim race


def _mark_run_failed_if_stuck(session, run_id: uuid.UUID) -> None:
    """After giving up on a permanently-stuck file_task (sweep_attempts cap
    exceeded), the parent run counts as failed too -- per the kickoff's
    explicit instruction that a run unresolvable after the resume-attempt
    cap must not be left `running` forever, nor silently `completed` by an
    earlier chord callback that fired without knowing this file_task never
    actually finished. Idempotent: a run already `completed`/`failed` is
    left alone.
    """

    from db.models import MigrationRun

    run = session.get(MigrationRun, run_id)
    if run is not None and run.status not in ("completed", "failed"):
        run.status = "failed"
        session.commit()
        _notify_run_terminal(session, run)


@celery_app.task(name="app.tasks.periodic_resume_sweep")
def periodic_resume_sweep() -> int:
    """Backstop dead-task detection (Step 6b, fork #1).

    `acks_late`/`task_reject_on_worker_lost` (see `celery_app.py`) is the
    PRIMARY mechanism -- it handles a worker vanishing before acking a
    task, causing the broker to redeliver it under the same task id (which
    then still feeds back into any chord/group tracking it). This sweep is
    the backstop for what that CAN'T catch: a task Celery considers
    successfully acked/completed, but whose worker crashed mid-way through
    this app's own DB writes -- leaving a `file_tasks` row stuck at
    `status='in_progress'` (or even `'pending'`, if the crash happened
    before the claim) with no Celery-level signal anything is wrong.
    Scheduled via Celery Beat (`celery_app.py`'s `beat_schedule`).

    Scans across ALL non-terminal file_tasks (not scoped to one run) since
    it's a global backstop, not something a caller invokes per-run.
    Returns the number of file_tasks actually acted on this sweep.
    """

    from db.models import FileTask, MigrationRun

    session = _session()
    try:
        candidates = (
            session.query(FileTask)
            .filter(FileTask.status.in_(["pending", "in_progress"]))
            .all()
        )
        acted = 0
        for file_task in candidates:
            run = session.get(MigrationRun, file_task.run_id)
            if run is None:
                continue
            action = plan_and_claim_resume(session, file_task)
            if _dispatch_resume_action(action, str(file_task.id), run.repo_url):
                acted += 1
            if action == ResumeAction.GAVE_UP:
                _mark_run_failed_if_stuck(session, run.id)
        return acted
    finally:
        session.close()


def resume_migration_run(run_id: str) -> int:
    """Manual/explicit resume entry point -- e.g. called by an operator
    after restarting a crashed worker. Finds every `file_tasks` row under
    `run_id` and re-dispatches each per `plan_and_claim_resume`'s decision.

    Uses the SAME staleness-gated claim as `periodic_resume_sweep` for
    `in_progress` rows: an explicit resume call still trusts the
    `updated_at` signal to decide whether a row is genuinely stuck versus
    still being actively worked on elsewhere -- it does not forcibly
    override a task that might still be legitimately running just because
    a human triggered this. `pending` rows are always claimable (no
    staleness ambiguity: nothing legitimately "in flight" holds a row at
    `pending`).

    Returns the number of file_tasks actually acted on (dispatched or
    given up on).
    """

    from db.models import FileTask, MigrationRun

    session = _session()
    try:
        run = session.get(MigrationRun, uuid.UUID(run_id))
        if run is None:
            raise ValueError(f"migration_runs row {run_id} not found")

        file_tasks = session.query(FileTask).filter_by(run_id=run.id).all()
        acted = 0
        for file_task in file_tasks:
            action = plan_and_claim_resume(session, file_task)
            if _dispatch_resume_action(action, str(file_task.id), run.repo_url):
                acted += 1
            if action == ResumeAction.GAVE_UP:
                _mark_run_failed_if_stuck(session, run.id)
        return acted
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Scheduled auto-checking (added 2026-09-16, explicitly BEYOND the
# original 10-step plan -- see CLAUDE.md's dated note). Same overall shape
# as Step 6b's periodic resume sweep above (a Celery Beat periodic task,
# scanning a small table and acting on rows that qualify), reused
# deliberately rather than inventing a different pattern for a second
# scheduled task in the same codebase.
# ---------------------------------------------------------------------------


def _repo_is_due(repo, now) -> bool:
    """A repo qualifies for a check this tick if auto-checking is enabled,
    an interval is actually configured (see `Repo.check_interval_hours`'s
    docstring -- enabled-with-no-interval is a valid, intentionally-inert
    state, not an error), and either it has never been checked or enough
    time has elapsed since the last check. Kept as a plain function (not
    a query filter) since "elapsed since last_auto_checked_at" needs
    `now` computed once per sweep call, not re-evaluated per-row in SQL --
    simpler to read, and this table is small enough that filtering in
    Python after one query is not a real cost.
    """

    if not repo.auto_check_enabled or repo.check_interval_hours is None:
        return False
    if repo.last_auto_checked_at is None:
        return True
    elapsed_hours = (now - repo.last_auto_checked_at).total_seconds() / 3600
    return elapsed_hours >= repo.check_interval_hours


@celery_app.task(name="app.tasks.periodic_auto_check_sweep")
def periodic_auto_check_sweep() -> int:
    """Per enabled+due repo (see `_repo_is_due`): look for unprocessed
    `changelog_events` (`processed_at IS NULL`) matching the repo's
    `default_api_name`, and if any exist, kick off a real migration run
    automatically -- then mark those events `processed_at`. Scheduled via
    Celery Beat (`celery_app.py`'s `beat_schedule`), same pattern as
    `periodic_resume_sweep`.

    **Design decision, not in the feature's literal spec, documented
    here:** a migration run needs a specific `(version_from, version_to)`
    pair (`start_migration_run`'s existing signature, unchanged) -- a
    `Repo` row only carries `default_api_name`, no version. Unprocessed
    changelog_events for that api_name may span more than one version
    pair (e.g. a repo that fell behind two releases). Rather than pick
    one arbitrarily or require a new column, this groups the repo's
    unprocessed events by their OWN `(version_from, version_to)` and
    dispatches one run per distinct pair found -- consistent with
    `scan_task` already matching changelog_events to a run by that same
    triple. Simple, and correct for the common case (one pending version
    bump per repo); a repo that's fallen behind multiple releases gets
    multiple runs dispatched in the same sweep, which is the honest
    behavior rather than silently dropping the extra ones.

    `last_auto_checked_at` is updated for every DUE repo examined this
    tick, whether or not anything new was found -- "checked, found
    nothing" and "haven't checked yet" are different states, and only the
    timestamp captures that distinction.

    Returns the number of runs actually dispatched this sweep (not the
    number of repos examined).
    """

    from db.models import ChangelogEvent, Repo

    session = _session()
    try:
        now = datetime.now(timezone.utc)
        repos = session.query(Repo).filter_by(auto_check_enabled=True).all()
        runs_dispatched = 0
        for repo in repos:
            if not _repo_is_due(repo, now):
                continue

            pending = (
                session.query(ChangelogEvent)
                .filter_by(
                    api_name=repo.default_api_name, processed_at=None
                )
                .all()
            )

            version_pairs = sorted(
                {(e.version_from, e.version_to) for e in pending}
            )
            for version_from, version_to in version_pairs:
                start_migration_run(
                    repo.repo_path, repo.default_api_name, version_from, version_to
                )
                runs_dispatched += 1

            for event in pending:
                event.processed_at = now
            repo.last_auto_checked_at = now
            session.commit()

        return runs_dispatched
    finally:
        session.close()
