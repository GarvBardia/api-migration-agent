"""Webhook notifications -- added 2026-09-16, explicitly BEYOND the
original 10-step plan (see CLAUDE.md's dated note).

Fires a simple JSON POST to `NOTIFICATION_WEBHOOK_URL` when a run reaches
a terminal state (`completed`/`failed`) and separately when a file lands
in `needs_review`. Deliberately best-effort, not part of the pipeline's
correctness contract:

- If `NOTIFICATION_WEBHOOK_URL` is unset, every call here is a silent
  no-op -- not an error, not a log line, nothing. Notifications are an
  optional convenience layer on top of an already-complete pipeline; their
  absence must never look like a problem.
- If the URL IS configured but the POST itself fails (network blip,
  webhook receiver down, timeout), that failure is swallowed here too --
  caught and ignored, never re-raised. A flaky webhook receiver must never
  fail a real migration task. This is a deliberate exception to CLAUDE.md's
  "silent wrongness is the failure mode to design against" principle:
  that principle is about PIPELINE correctness (a wrong migration
  shipping unnoticed), not about a best-effort side notification failing
  to send -- those are different failure classes with different stakes.

**Payload shape, decided deliberately:** `{"text": "<message>"}` -- the
shape Slack- and Discord-compatible incoming webhooks expect for a plain
message, rather than a custom JSON schema. This is what actually works
out of the box with the free tools someone would point
`NOTIFICATION_WEBHOOK_URL` at (a Slack incoming webhook, a Discord webhook
in Slack-compatible mode, or literally any endpoint that just wants a
JSON body -- a custom receiver can read `.text` and ignore the rest, or
this project's own tests inspect the same field). Run/file counts are
folded into that one text field rather than added as separate top-level
keys, keeping the payload genuinely simple per the kickoff's explicit
steer ("keep the payload simple").
"""

from __future__ import annotations

import os

import httpx

WEBHOOK_TIMEOUT_SECONDS = 5.0


def _webhook_url() -> str | None:
    # Read fresh on every call (not module-level) so tests can
    # monkeypatch/setenv NOTIFICATION_WEBHOOK_URL per-test without needing
    # to reload this module.
    return os.environ.get("NOTIFICATION_WEBHOOK_URL") or None


def send_webhook(text: str) -> None:
    """The one place a notification is actually sent. See module
    docstring for the "silently skip when unconfigured, silently swallow
    delivery failures" contract -- both are intentional here.
    """

    url = _webhook_url()
    if not url:
        return
    try:
        httpx.post(url, json={"text": text}, timeout=WEBHOOK_TIMEOUT_SECONDS)
    except Exception:
        pass


def notify_run_terminal(run, file_task_status_counts: dict[str, int]) -> None:
    """Called once a `migration_runs` row reaches `completed`/`failed`.

    `file_task_status_counts` -- e.g. `{"validated": 2, "needs_review": 1}`
    -- is the simple "file counts by status" the kickoff asked for, kept
    as a plain dict built by the caller (a single `GROUP BY status` query
    against `file_tasks`) rather than this module reaching into the DB
    itself -- this module stays DB-independent and directly unit-testable,
    same reasoning as `app/agents/resume.py`'s DB-only/Celery-independent
    split.
    """

    counts_str = (
        ", ".join(f"{k}={v}" for k, v in sorted(file_task_status_counts.items()))
        or "no file tasks"
    )
    send_webhook(
        f"Migration run {run.api_name} ({run.version_from} -> {run.version_to}) "
        f"{run.status}. run_id={run.id}. File tasks: {counts_str}"
    )


def notify_needs_review(file_task) -> None:
    """Called once a `file_tasks` row lands in `needs_review`, regardless
    of which of the (several) code paths in `validation.py`/`migration.py`
    put it there -- callers in `app/tasks.py` check `file_task.status ==
    'needs_review'` after each Celery task commits, rather than this
    notification being threaded into every individual status-assignment
    site in the agent modules. Fewer call sites, same coverage, and it
    keeps webhook side-effects entirely in the Celery-glue layer (see
    `app/tasks.py`'s own docstring note on that split).
    """

    send_webhook(
        f"File {file_task.file_path} needs review "
        f"(run_id={file_task.run_id}): {file_task.failure_reason or 'see review queue'}"
    )
