"""Migration Agent (Step 4).

Per `file_tasks` row with `status='pending'` (written by Step 3's scanner):
looks up the linked `changelog_events` row, calls the configured LLM
provider adapter (see `llm_providers/`) for a migrated replacement, validates
the result is syntactically valid Python, and writes `new_code_snippet` +
`status='in_progress'`.

Deliberately NOT in scope here (belongs to later steps):
- Any retry loop beyond "don't crash, mark needs_review" (Step 5).
- Running the resulting code / tests (Step 5, Docker sandbox).
- Orchestration/state-machine wiring across file_tasks (Step 6).

**Updated 2026-08-26 (Step 5 session):** `migrate_file_task` gained an
optional `failure_context` parameter, threaded straight into
`provider.propose_edit(...)`. This is fork #1 from the Step 5 kickoff: the
Validation Agent's retry loop calls back into this exact function (not a
parallel retry path) with the previous attempt's failure detail, so a
retried migration is informed by what went wrong rather than repeating it
blind. See `app/agents/validation.py` and CLAUDE.md §4.3.

**Updated 2026-08-27 (gap-fix session):** `migrate_file_task` gained an
optional `redis_client` parameter. When provided, a retrieval step runs
before `propose_edit`: build a query from the linked `changelog_events`
row's `old_signature`/`migration_notes`, look it up via Step 10's
semantic-cache-backed `get_doc_context_for_symbol` (embed the query, check
Redis, fall back to a direct pgvector `api_doc_chunks` query on a cache
miss), and thread whatever chunk text comes back into `propose_edit` as
`doc_context`. Zero chunks found is the expected common case (handled as
`doc_context=None`, not an error) until a real ingestion agent exists to
populate `api_doc_chunks` from actual API documentation -- **that
ingestion agent does not exist and is not built by this fix; see
CLAUDE.md's dated note.** Retrieval failures of any kind (Redis down, no
`changelog_event`, etc.) degrade to `doc_context=None` rather than
blocking migration -- this is a best-effort enhancement layer, not a hard
dependency.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from .llm_providers import MigrationEdit, MigrationLLMProvider, ProviderResponseError


def _read_old_code_snippet(
    repo_root: Path, file_path: str, line_start: int, line_end: int
) -> str:
    """1-indexed, inclusive line range, matching Step 3's `line_start`/`line_end`."""

    full_path = repo_root / file_path
    lines = full_path.read_text().splitlines()
    return "\n".join(lines[line_start - 1 : line_end])


def _retrieve_doc_context(session, redis_client, changelog_event) -> str | None:
    """Best-effort retrieval of relevant `api_doc_chunks` text for this
    changelog event, via Step 10's semantic cache. Returns `None` on a
    genuine "nothing found" (the expected common case today -- nothing
    populates `api_doc_chunks` from real documentation yet) AND on any
    retrieval failure (this is an enhancement layer; losing it should
    never itself route a file_task to `needs_review`).
    """

    if redis_client is None:
        return None

    try:
        from .semantic_cache import get_doc_context_for_symbol

        query_text = changelog_event.old_signature
        if changelog_event.migration_notes:
            query_text = f"{query_text} {changelog_event.migration_notes}"

        chunks, _source = get_doc_context_for_symbol(
            session, redis_client, changelog_event.api_name, query_text
        )
    except Exception:
        return None

    if not chunks:
        return None
    return "\n\n".join(c["chunk_text"] for c in chunks)


def migrate_file_task(
    session,
    file_task,
    repo_root: Path,
    provider: MigrationLLMProvider,
    failure_context: str | None = None,
    redis_client=None,
) -> None:
    """Process one `file_task` end to end through the Migration Agent.

    Never raises for an expected failure mode (missing changelog event,
    provider error, invalid resulting Python) -- those route the task to
    `status='needs_review'` with `failure_reason` set and the function
    returns normally, per CLAUDE.md's "silent wrongness is the failure mode
    to design against, not exceptions" principle applied to this step:
    an unresolvable file_task is not a crash, it's a review-queue candidate.
    Only genuine programmer/session errors propagate.

    `failure_context` (Step 5): pass the previous attempt's failure detail
    when re-invoking this function as part of the Validation Agent's retry
    loop. `None` (the default) is a first attempt.

    `redis_client` (gap-fix session): when provided, enables the
    doc-context retrieval step (Step 10's semantic cache fronting
    `api_doc_chunks`). `None` (the default) skips retrieval entirely --
    `doc_context` is simply never populated, same observable behavior as
    a retrieval that found zero chunks.
    """

    from db.models import ChangelogEvent  # lazy: keep adapters DB-independent

    changelog_event = None
    if file_task.changelog_event_id is not None:
        changelog_event = session.get(ChangelogEvent, file_task.changelog_event_id)

    if changelog_event is None:
        file_task.status = "needs_review"
        file_task.failure_reason = (
            "No changelog_events row found for changelog_event_id="
            f"{file_task.changelog_event_id!r}; cannot determine new_signature."
        )
        session.flush()
        return

    old_code = file_task.old_code_snippet
    if not old_code:
        try:
            old_code = _read_old_code_snippet(
                repo_root,
                file_task.file_path,
                file_task.line_start,
                file_task.line_end,
            )
        except OSError as exc:
            file_task.status = "needs_review"
            file_task.failure_reason = (
                f"Could not read source file to populate old_code_snippet: {exc}"
            )
            session.flush()
            return

    doc_context = _retrieve_doc_context(session, redis_client, changelog_event)

    try:
        edit: MigrationEdit = provider.propose_edit(
            old_code=old_code,
            old_signature=changelog_event.old_signature,
            new_signature=changelog_event.new_signature,
            migration_notes=changelog_event.migration_notes,
            failure_context=failure_context,
            doc_context=doc_context,
        )
    except ProviderResponseError as exc:
        file_task.status = "needs_review"
        file_task.failure_reason = f"LLM provider error: {exc}"
        session.flush()
        return

    # The source snippet is extracted from within its original indentation
    # context (e.g. inside a function body); providers often echo that
    # indentation back. Dedent before validating/storing so a correct
    # single-statement edit isn't rejected purely for a leading-whitespace
    # IndentationError that has no meaning outside its original context.
    new_code = textwrap.dedent(edit.new_code)

    try:
        ast.parse(new_code)
    except SyntaxError as exc:
        file_task.status = "needs_review"
        file_task.failure_reason = (
            f"Proposed new_code_snippet is not valid Python: {exc}"
        )
        session.flush()
        return

    file_task.old_code_snippet = old_code
    file_task.new_code_snippet = new_code
    file_task.status = "in_progress"
    session.flush()
