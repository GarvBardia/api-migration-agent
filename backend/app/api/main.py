"""FastAPI app: REST endpoints + SSE live-progress stream (Step 7).

**Fork #1 (how the SSE endpoint learns about status changes) — DECIDED:
polling, not Redis pub/sub.** See CLAUDE.md §4's dated note for the full
reasoning; in short: polling needs zero changes to Steps 3-6's already-
verified task code, and pub/sub's "client connects after a message was
published and misses it" edge case means a DB read on connect is needed
either way -- polling just does that DB read on every tick instead of
needing a separate publish call wired into every task.

**Fork #2 (what a human review decision does to the pipeline) — see
CLAUDE.md §4 for the full contract.** In short: `approved`/`rejected` just
record the decision (Step 9's PR-eligibility query combines
`file_tasks.status` with `reviewer_decision`); `modified` re-runs Step 5's
validation on the reviewer's code before it counts as PR-eligible, rather
than trusting it outright.

Run locally (alongside `docker compose up -d postgres redis` and a Celery
worker -- see `celery_app.py`'s docstring):

    cd backend
    venv\\Scripts\\python.exe -m uvicorn app.api.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

# Added 2026-08-27, Step 8a session -- see the matching note in
# celery_app.py: nothing loaded the project root .env for a real `uvicorn`
# process either.
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from app.repo_paths import RepoPathError, validate_repo_path
from db import get_db
from db.models import FileTask, HumanReviewQueue, MigrationRun, Repo

from .schemas import (
    FileTaskRead,
    HumanReviewQueueItem,
    HumanReviewQueueRead,
    MigrationRunRead,
    RepoCreateRequest,
    RepoRead,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    RunCreateRequest,
    RunCreateResponse,
)

TERMINAL_RUN_STATUSES = {"completed", "failed"}
SSE_POLL_INTERVAL_SECONDS = 1.0

# Display name only (shown on /docs); renamed 2026-09-23, see CLAUDE.md §4d.
@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Keep the permanent demo change record in place (see app/seed.py).
    # A DB that is not up yet must not stop the API from starting.
    try:
        from app.seed import ensure_demo_event
        from db import SessionLocal

        session = SessionLocal()
        try:
            ensure_demo_event(session)
        finally:
            session.close()
    except Exception:  # noqa: BLE001
        pass
    yield


app = FastAPI(title="Confide API", lifespan=_lifespan)

# Step 8a (added 2026-08-27): the Next.js dev server runs on a different
# origin (localhost:3000 vs. this API's localhost:8000) -- browsers block
# cross-origin fetch/EventSource without this.
#
# Added 2026-09-20 (public-demo deployment, see PROJECT_STATUS.md): the
# static frontend is served from GitHub Pages
# (https://garvbardia.github.io) and calls this API through a Cloudflare
# Tunnel -- a genuinely different origin again. A browser's Origin header
# for a Pages site is just scheme+host (no path), so the bare
# `https://garvbardia.github.io` is the right value even though the site
# itself lives under /api-migration-agent/. Local dev origins are kept:
# this ADDS the deployed origin, it doesn't replace local access.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://garvbardia.github.io",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def public_demo_mode() -> bool:
    """True when PUBLIC_DEMO_MODE is set (docker-compose.yml only, never
    .env). Read per call so tests can toggle it."""

    return os.environ.get("PUBLIC_DEMO_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


MODIFY_BLOCKED_MESSAGE = (
    "Modify is turned off on the public demo. Approve and reject still work."
)


@app.get("/config")
def get_config() -> dict:
    """Lets the UI know which features this server has turned off."""

    return {"public_demo_mode": public_demo_mode()}


MAX_ACTIVE_RUNS_DEMO = 3
RUN_CAP_MESSAGE = (
    "The public demo is busy. It runs at most 3 migrations at a time. "
    "Wait for one to finish and try again."
)


@app.post("/runs", response_model=RunCreateResponse, status_code=201)
def create_run(
    body: RunCreateRequest, db: Session = Depends(get_db)
) -> RunCreateResponse:
    """Kicks off Step 6a's `start_migration_run` (scan -> dispatch ->
    group/chord), which runs asynchronously via Celery. Returns immediately
    with the new run's id -- doesn't wait for scanning to finish."""

    from app.tasks import start_migration_run

    try:
        validate_repo_path(body.repo_url)
    except RepoPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Run cap (2026-10-05): public demo only, so local dev is unaffected.
    if public_demo_mode():
        active = (
            db.query(MigrationRun)
            .filter(MigrationRun.status.in_(["pending", "running"]))
            .count()
        )
        if active >= MAX_ACTIVE_RUNS_DEMO:
            raise HTTPException(status_code=429, detail=RUN_CAP_MESSAGE)

    run_id = start_migration_run(
        body.repo_url, body.api_name, body.version_from, body.version_to
    )
    return RunCreateResponse(run_id=uuid.UUID(run_id))


@app.get("/runs", response_model=list[MigrationRunRead])
def list_runs(db: Session = Depends(get_db)) -> list[MigrationRunRead]:
    """Added 2026-08-27, Step 8a session: Step 7 only built `GET
    /runs/{run_id}` (a single run) -- the frontend's run-list page needs a
    list-all endpoint that didn't exist yet. Newest first, capped at 100
    (no pagination UI exists yet to need more)."""

    runs = db.query(MigrationRun).order_by(MigrationRun.created_at.desc()).limit(100).all()
    return [MigrationRunRead.model_validate(r) for r in runs]


@app.get("/runs/{run_id}", response_model=MigrationRunRead)
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db)) -> MigrationRunRead:
    run = db.get(MigrationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="migration_runs row not found")
    return MigrationRunRead.model_validate(run)


@app.get("/runs/{run_id}/file_tasks", response_model=list[FileTaskRead])
def list_file_tasks(
    run_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[FileTaskRead]:
    run = db.get(MigrationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="migration_runs row not found")
    tasks = (
        db.query(FileTask)
        .filter_by(run_id=run_id)
        .order_by(FileTask.file_path, FileTask.line_start)
        .all()
    )
    return [FileTaskRead.model_validate(t) for t in tasks]


# ---------------------------------------------------------------------------
# repos -- added 2026-09-16, explicitly BEYOND the original 10-step plan
# (see CLAUDE.md's dated note). Basic CRUD: add/list/delete, no update
# endpoint -- not asked for, and "delete and re-add" is a fine substitute
# at this feature's scope.
# ---------------------------------------------------------------------------


@app.post("/repos", response_model=RepoRead, status_code=201)
def create_repo(body: RepoCreateRequest, db: Session = Depends(get_db)) -> RepoRead:
    try:
        validate_repo_path(body.repo_path)
    except RepoPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    repo = Repo(
        name=body.name,
        repo_path=body.repo_path,
        default_api_name=body.default_api_name,
        auto_check_enabled=body.auto_check_enabled,
        check_interval_hours=body.check_interval_hours,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return RepoRead.model_validate(repo)


@app.get("/repos", response_model=list[RepoRead])
def list_repos(db: Session = Depends(get_db)) -> list[RepoRead]:
    repos = db.query(Repo).order_by(Repo.name).all()
    return [RepoRead.model_validate(r) for r in repos]


@app.delete("/repos/{repo_id}", status_code=204, response_model=None)
def delete_repo(repo_id: uuid.UUID, db: Session = Depends(get_db)) -> None:
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="repos row not found")
    db.delete(repo)
    db.commit()


@app.get("/human-review-queue", response_model=list[HumanReviewQueueItem])
def list_human_review_queue(
    db: Session = Depends(get_db),
) -> list[HumanReviewQueueItem]:
    """Pending items only (`reviewer_decision IS NULL`), each joined to its
    `file_tasks` row rather than duplicating diff/failure context into
    `human_review_queue` (see CLAUDE.md)."""

    rows = (
        db.query(HumanReviewQueue)
        .filter(HumanReviewQueue.reviewer_decision.is_(None))
        .order_by(HumanReviewQueue.created_at)
        .all()
    )
    items: list[HumanReviewQueueItem] = []
    for row in rows:
        file_task = db.get(FileTask, row.file_task_id)
        if file_task is None:
            continue  # orphaned row -- shouldn't happen (FK is NOT NULL), skip defensively
        items.append(
            HumanReviewQueueItem(
                review=HumanReviewQueueRead.model_validate(row),
                file_task=FileTaskRead.model_validate(file_task),
            )
        )
    return items


@app.post(
    "/human-review-queue/{review_id}/decision", response_model=ReviewDecisionResponse
)
def post_review_decision(
    review_id: uuid.UUID,
    body: ReviewDecisionRequest,
    db: Session = Depends(get_db),
) -> ReviewDecisionResponse:
    """Implements fork #2's contract:
    - `approved`: just records the decision. Step 9's PR-eligibility query
      is `status='validated' OR (status='needs_review' AND
      reviewer_decision='approved')` -- written here verbatim to match
      CLAUDE.md, since Step 9 needs it exactly.
    - `rejected`: just records the decision -- excluded from any PR by the
      query above (neither disjunct matches a rejected row).
    - `modified`: writes the reviewer's code into `new_code_snippet`, resets
      `status='in_progress'`, and re-dispatches `validate_task` (Step 5) to
      actually re-test it before it can become PR-eligible -- trusting a
      human's edit outright would skip the one check (does it still pass
      the real test suite?) this whole pipeline exists to provide.
    """

    from app.tasks import validate_task

    review = db.get(HumanReviewQueue, review_id)
    if review is None:
        raise HTTPException(
            status_code=404, detail="human_review_queue row not found"
        )
    if review.reviewer_decision is not None:
        raise HTTPException(
            status_code=409, detail="This review item already has a decision"
        )

    file_task = db.get(FileTask, review.file_task_id)
    if file_task is None:
        raise HTTPException(status_code=500, detail="Linked file_tasks row missing")

    if body.decision == "modified" and public_demo_mode():
        raise HTTPException(status_code=403, detail=MODIFY_BLOCKED_MESSAGE)

    if body.decision == "modified" and not body.modified_code:
        raise HTTPException(
            status_code=422,
            detail="modified_code is required when decision='modified'",
        )

    review.reviewer_decision = body.decision
    review.reviewed_at = datetime.now(timezone.utc)

    revalidation_dispatched = False
    if body.decision == "modified":
        run = db.get(MigrationRun, file_task.run_id)
        file_task.new_code_snippet = body.modified_code
        file_task.status = "in_progress"
        file_task.failure_reason = None
        db.commit()
        db.refresh(file_task)
        validate_task.delay(str(file_task.id), run.repo_url)
        revalidation_dispatched = True
    else:
        db.commit()
        db.refresh(file_task)

    db.refresh(review)
    return ReviewDecisionResponse(
        review=HumanReviewQueueRead.model_validate(review),
        file_task=FileTaskRead.model_validate(file_task),
        revalidation_dispatched=revalidation_dispatched,
    )


def _run_snapshot(db: Session, run_id: uuid.UUID) -> dict | None:
    """One poll's worth of state: run status + every file_task's status.
    `None` if the run doesn't exist (caller decides what to do)."""

    run = db.get(MigrationRun, run_id)
    if run is None:
        return None
    tasks = (
        db.query(FileTask)
        .filter_by(run_id=run_id)
        .order_by(FileTask.file_path, FileTask.line_start)
        .all()
    )
    return {
        "run_status": run.status,
        "file_tasks": {str(t.id): t.status for t in tasks},
    }


@app.get("/runs/{run_id}/events")
async def stream_run_events(run_id: uuid.UUID) -> StreamingResponse:
    """SSE stream of status transitions for one run (fork #1: polling).

    Polls every `SSE_POLL_INTERVAL_SECONDS`, diffs against the last
    snapshot actually sent, and emits an `event: status` line only when
    something changed -- so a client connecting mid-run immediately gets
    one event with current state (fork #1's "still need a DB read on
    connect" point), not just future changes. Closes the stream once
    `run_status` reaches a terminal value (`completed`/`failed`).
    """

    async def event_generator():
        from db import SessionLocal

        last_snapshot: dict | None = None
        while True:
            db = SessionLocal()
            try:
                snapshot = _run_snapshot(db, run_id)
            finally:
                db.close()

            if snapshot is None:
                yield (
                    "event: error\n"
                    f"data: {json.dumps({'detail': 'migration_runs row not found'})}\n\n"
                )
                return

            if snapshot != last_snapshot:
                yield f"event: status\ndata: {json.dumps(snapshot)}\n\n"
                last_snapshot = snapshot

            if snapshot["run_status"] in TERMINAL_RUN_STATUSES:
                return

            await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
