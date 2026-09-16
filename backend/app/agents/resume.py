"""Step 6b: crash-resume, dead-task detection, and orchestration-level
retry/backoff.

**This is a different failure mode from Step 5's `retry_count`.** Step 5's
retry loop (inside `validate_file_task`) handles a WRONG migration -- the
Migration/Validation Agent asking for a better fix after a test failure,
bounded by `retry_count`. This module handles INFRASTRUCTURE failing
instead: a worker process crashing mid-task, a network blip calling an LLM
API, the Docker daemon hiccuping. It uses a SEPARATE counter,
`sweep_attempts` (migration `0003`), specifically so a stuck row's history
never conflates "the fix kept failing tests" with "the worker kept
crashing" -- see CLAUDE.md §4's dated note on this fork.

**Fork #1 (detection):** Celery's `acks_late=True` +
`task_reject_on_worker_lost=True` (see `celery_app.py`) is the PRIMARY
mechanism -- the broker redelivers a task if the worker processing it
vanishes before acking. That alone does NOT catch a task that Celery
considers successfully acked/completed, but whose worker crashed mid-way
through this app's own DB writes (e.g. between `migrate_file_task`
succeeding and `session.commit()`), leaving a `file_tasks` row stuck at
`status='in_progress'` with no Celery-level signal anything is wrong. This
module's staleness-based claim (`try_claim_stale_in_progress_file_task`) is
the backstop for exactly that gap, driven off `updated_at` -- a periodic
sweep (see `app/tasks.py`'s `periodic_resume_sweep`, scheduled via Celery
Beat) and the manual `resume_migration_run` entry point both go through it.

**Fork #2 (resume granularity):** a `file_tasks` row stuck at
`in_progress` in this codebase's actual state machine can only have
reached that status via `migrate_file_task`'s success path (see
`migration.py`) -- which never sets `status='in_progress'` until
`new_code_snippet` has already been written. So checking
`new_code_snippet is not None` is both the safe, general rule the kickoff
asked for AND (today) always true for an `in_progress` row -- checking it
explicitly rather than hardcoding "always resume into validate" keeps this
correct even if that invariant ever changes. Resuming into `validate_task`
is always safe against stale data: `validate_file_task` (Step 5) already
re-checks `old_code_snippet` against the real file's current content
before applying any patch, and routes to `needs_review` on a mismatch --
this module doesn't need to (and must not) duplicate or bypass that check.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import func, text, update

STALENESS_WINDOW_SECONDS = int(os.environ.get("STALENESS_WINDOW_SECONDS", "300"))
MAX_SWEEP_ATTEMPTS = int(os.environ.get("MAX_SWEEP_ATTEMPTS", "3"))

TERMINAL_FILE_TASK_STATUSES = frozenset({"validated", "failed", "needs_review"})


class ResumeAction(str, Enum):
    """What `plan_and_claim_resume` decided for one file_task."""

    DISPATCH_CHAIN = "dispatch_chain"  # restart from migrate_task
    DISPATCH_VALIDATE_ONLY = "dispatch_validate_only"  # migrate already succeeded
    GAVE_UP = "gave_up"  # sweep_attempts cap exceeded -> file_task now 'failed'
    SKIP = "skip"  # terminal already, or lost the claim race


# Added 2026-08-31 (post-containerization debugging session) -- see the
# dated note in CLAUDE.md. A hung full-suite run was traced to worker-
# thread starvation (orphaned Celery tasks from `_cleanup()`-deleted rows
# occupying the embedded test worker's limited thread pool forever), NOT
# a stuck row lock -- pg_stat_activity/pg_locks were checked live during
# the hang and showed nothing blocked. Still, an uncommitted transaction
# from some other future crash-mid-claim scenario holding this row's lock
# is a real, plausible failure mode this atomic claim has no defense
# against today: it would hang silently forever (no statement_timeout),
# same "stuck for hours, zero CPU, invisible" symptom we just spent real
# time distinguishing from the actual cause. Bounding it here means that
# specific class of failure fails loudly and fast instead.
CLAIM_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("CLAIM_STATEMENT_TIMEOUT_MS", "5000")
)


def try_claim_pending_file_task(session, file_task_id: uuid.UUID) -> bool:
    """Atomic conditional claim: `pending` -> `in_progress`.

    An ordinary `UPDATE ... WHERE id=:id AND status='pending'` -- Postgres
    guarantees only one concurrent transaction can win this (row-level
    locking during the UPDATE), so two callers racing to claim the same
    row (a resume sweep racing a worker that's also about to pick it up)
    can never both succeed. Returns True iff THIS call won the claim.

    Bounded by `CLAIM_STATEMENT_TIMEOUT_MS` (2026-08-31) -- see the note
    above. `SET LOCAL` scopes the timeout to this transaction only.
    """

    from db.models import FileTask

    session.execute(text(f"SET LOCAL statement_timeout = {CLAIM_STATEMENT_TIMEOUT_MS}"))
    stmt = (
        update(FileTask)
        .where(FileTask.id == file_task_id, FileTask.status == "pending")
        .values(status="in_progress", updated_at=func.now())
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount == 1


def try_claim_stale_in_progress_file_task(
    session,
    file_task_id: uuid.UUID,
    staleness_window_seconds: int = STALENESS_WINDOW_SECONDS,
) -> bool:
    """Atomic conditional claim for an already-`in_progress` row.

    Only wins if `updated_at` is still older than the staleness cutoff AT
    THE MOMENT OF the UPDATE -- bumping `updated_at` forward IS the claim
    marker. No new column needed for this guard: a second concurrent claim
    attempt (or the original still-live task finishing normally and
    touching the row itself) loses the race, because by then `updated_at`
    is no longer stale. This is the same atomic-conditional-UPDATE pattern
    as `try_claim_pending_file_task`, just gated on a timestamp instead of
    a status transition (the status doesn't change here -- it's already
    `in_progress`).

    Bounded by `CLAIM_STATEMENT_TIMEOUT_MS` (2026-08-31) -- see the note
    above `try_claim_pending_file_task`.
    """

    from db.models import FileTask

    session.execute(text(f"SET LOCAL statement_timeout = {CLAIM_STATEMENT_TIMEOUT_MS}"))
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=staleness_window_seconds)
    stmt = (
        update(FileTask)
        .where(
            FileTask.id == file_task_id,
            FileTask.status == "in_progress",
            FileTask.updated_at < cutoff,
        )
        .values(updated_at=func.now())
    )
    result = session.execute(stmt)
    session.commit()
    return result.rowcount == 1


@dataclass(frozen=True)
class ResumeDecision:
    file_task_id: uuid.UUID
    action: ResumeAction


def plan_and_claim_resume(
    session, file_task, staleness_window_seconds: int = STALENESS_WINDOW_SECONDS
) -> ResumeAction:
    """Decide what to do with one non-terminal `file_task`, ATOMICALLY
    claiming it first so two concurrent callers (two resume calls, or a
    resume racing a still-live worker) can't both act on the same row.

    Used identically by both the manual `resume_migration_run` entry point
    and the automatic periodic sweep (see `app/tasks.py`) -- an explicit
    resume call still trusts the same `updated_at` staleness signal rather
    than forcibly overriding a task that might genuinely still be running;
    it does not get a "no staleness check" shortcut just because a human
    triggered it.

    On `GAVE_UP`, this function itself sets `file_task.status='failed'`
    and a `failure_reason`, and commits -- the caller is only responsible
    for then deciding whether the parent run should also be marked failed
    (see `app/tasks.py::_mark_run_failed_if_stuck`). For
    `DISPATCH_CHAIN`/`DISPATCH_VALIDATE_ONLY`, this function does NOT
    dispatch anything itself -- it only claims the row; the caller (which
    has access to Celery's task objects) is responsible for the actual
    `.apply_async()`/`.delay()` call.
    """

    if file_task.status in TERMINAL_FILE_TASK_STATUSES:
        return ResumeAction.SKIP

    if file_task.status == "pending":
        if not try_claim_pending_file_task(session, file_task.id):
            return ResumeAction.SKIP  # lost the race to another claimer
        session.refresh(file_task)
        return ResumeAction.DISPATCH_CHAIN

    if file_task.status == "in_progress":
        if not try_claim_stale_in_progress_file_task(
            session, file_task.id, staleness_window_seconds
        ):
            return ResumeAction.SKIP  # not stale yet, or already claimed
        session.refresh(file_task)

        if file_task.sweep_attempts >= MAX_SWEEP_ATTEMPTS:
            file_task.status = "failed"
            file_task.failure_reason = (
                f"Orchestration gave up after {file_task.sweep_attempts} "
                "resume attempt(s) stuck at 'in_progress' (infrastructure-"
                "level resume budget -- distinct from retry_count, which "
                "tracks the Migration/Validation Agent's own fix-and-retest "
                "attempts for a wrong migration, not a crashed worker)."
            )
            session.commit()
            return ResumeAction.GAVE_UP

        file_task.sweep_attempts += 1
        session.commit()
        return (
            ResumeAction.DISPATCH_VALIDATE_ONLY
            if file_task.new_code_snippet
            else ResumeAction.DISPATCH_CHAIN
        )

    return ResumeAction.SKIP
