"""Celery application for the orchestrator (Steps 6a/6b).

Broker and result backend are both the Redis instance already running from
`docker-compose.yml` (`REDIS_URL`) -- no separate message queue is stood up.

**Where this runs:** same note as Step 5 (see CLAUDE.md §4.4) -- the backend
runs directly on the host via the Python venv, not inside a container.
There is no containerized entrypoint for a worker yet. To run a worker
locally (alongside the existing Postgres/Redis containers from
`docker compose up -d postgres redis`):

    cd backend
    venv\\Scripts\\python.exe -m celery -A app.celery_app worker --loglevel=info --pool=threads --concurrency=4

`--pool=threads` (not the default `prefork`) because `prefork` uses
`os.fork`, which doesn't exist on Windows. `--concurrency=4` (or higher)
is required for the group/chord fan-out in `app/tasks.py` to actually run
per-file chains concurrently rather than one at a time.

**Step 6b addition -- Celery Beat, for the periodic resume sweep:** the
backstop dead-task detection (see `app/agents/resume.py`) needs a
scheduler ticking `app.tasks.periodic_resume_sweep` on an interval. Run
Beat as a separate process alongside the worker:

    venv\\Scripts\\python.exe -m celery -A app.celery_app beat --loglevel=info

(or combine both in one process for local dev: add `-B` to the worker
command above instead of running Beat separately -- fine for this project's
scale, not recommended for a real production deployment where worker and
beat should be separate processes/failure domains).
"""

from __future__ import annotations

import os
from pathlib import Path

from celery import Celery
from dotenv import load_dotenv

# Added 2026-08-27, Step 8a session: nothing previously loaded the project
# root .env when running `celery worker`/`beat` directly from the command
# line (only tests did this manually, via dotenv_values()) -- so a real
# worker process had no MIGRATION_LLM_PROVIDER/API keys/REDIS_URL beyond
# whatever the shell happened to export. Loads .env once, here, before any
# other module in this process reads os.environ for config.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# How often the periodic resume sweep runs (see app/tasks.py). Independent
# of STALENESS_WINDOW_SECONDS (app/agents/resume.py) -- this is "how often
# do we look," that's "how old does in_progress have to be before we act."
RESUME_SWEEP_INTERVAL_SECONDS = int(
    os.environ.get("RESUME_SWEEP_INTERVAL_SECONDS", "60")
)

# Added 2026-09-16, beyond the original 10-step plan -- see CLAUDE.md's
# dated note. How often the scheduled auto-check sweep TICKS (looks at
# every enabled repo); a repo's own `check_interval_hours` is the real
# gate on how often it's actually acted on (see app/tasks.py's
# _repo_is_due). Defaults to 5 minutes -- frequent enough that even a
# repo configured for hourly checks doesn't drift far past its interval,
# without being a noisy no-op tick for repos checked far less often.
AUTO_CHECK_SWEEP_INTERVAL_SECONDS = int(
    os.environ.get("AUTO_CHECK_SWEEP_INTERVAL_SECONDS", "300")
)

celery_app = Celery(
    "migration_agent",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    result_expires=3600,
    # Windows has no os.fork -- prefork (the default pool) can't run here.
    worker_pool="threads",
    # Step 6b, fork #1 (primary dead-task detection mechanism): redeliver a
    # task if the worker processing it vanishes before acking completion,
    # rather than silently losing it. Applied globally (not per-task) --
    # every task in app/tasks.py is a "relevant" task per the kickoff
    # prompt, and CLAUDE.md §4.1 already requires every task to be
    # idempotent, so acks_late's at-least-once redelivery semantics are
    # safe here by construction (a redelivered scan_task/migrate_task/
    # validate_task/finalize_run re-checks status before doing real work).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "periodic-resume-sweep": {
            "task": "app.tasks.periodic_resume_sweep",
            "schedule": RESUME_SWEEP_INTERVAL_SECONDS,
        },
        # Added 2026-09-16, beyond the original 10-step plan.
        "periodic-auto-check-sweep": {
            "task": "app.tasks.periodic_auto_check_sweep",
            "schedule": AUTO_CHECK_SWEEP_INTERVAL_SECONDS,
        },
    },
)
