"""Shared contract every Migration Agent LLM provider adapter implements.

The Migration Agent must not be hard-wired to one LLM provider (CLAUDE.md
§4.3). Every adapter — ``claude_adapter.py``, ``openrouter_adapter.py``, and
any future one — implements the same ``propose_edit`` signature and returns
the same ``MigrationEdit`` shape, so the rest of the pipeline (``migration.py``)
never has to know which provider actually produced an edit.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class MigrationEdit(BaseModel):
    """The provider-agnostic result of one migration proposal.

    Every adapter must return this exact shape (or raise
    ``ProviderResponseError``) — never a raw string blob for the caller to
    parse. This is what CLAUDE.md §4.3 means by "no free-text diff parsing
    anywhere."
    """

    new_code: str = Field(..., description="The migrated replacement code.")
    rationale: str = Field(
        ..., description="Short explanation of what changed and why."
    )


class ProviderResponseError(Exception):
    """Raised by an adapter when a provider's response can't be trusted.

    Covers: the API call itself failing, the response not containing the
    expected tool/function call, and the tool-call arguments not validating
    against ``MigrationEdit``. Callers (``migration.py``) catch this single
    exception type and route the file_task to ``needs_review`` — they never
    need to know which provider or failure mode produced it.
    """


class MigrationLLMProvider(Protocol):
    """The one signature every provider adapter implements."""

    def propose_edit(
        self,
        *,
        old_code: str,
        old_signature: str,
        new_signature: str,
        migration_notes: str | None = None,
        failure_context: str | None = None,
        doc_context: str | None = None,
    ) -> MigrationEdit:
        """Propose a migrated replacement for ``old_code``.

        ``failure_context`` (added in the Step 5 session, see CLAUDE.md
        §4.3 dated note) is ``None`` on a first attempt. On a retry, the
        Validation Agent passes a description of the previous attempt and
        why it failed (e.g. the rejected ``new_code`` plus the test failure
        detail) so the provider can propose a corrected edit instead of
        repeating the same mistake blind. It is threaded straight into the
        prompt (see ``build_user_prompt``) -- adapters don't need their own
        retry-specific logic beyond passing it through.

        ``doc_context`` (added in the gap-fix session, see CLAUDE.md's
        dated note): retrieved documentation chunk text for this symbol,
        via Step 10's semantic-cache-backed ``api_doc_chunks`` lookup
        (``migration.py`` does the retrieval before calling this). ``None``
        or empty when nothing relevant was found -- handled gracefully,
        not an error; this is the common case until a real ingestion
        agent exists to populate ``api_doc_chunks`` (see that same note --
        this fix does NOT build that ingestion agent).

        Raises ``ProviderResponseError`` on any failure (API error,
        missing/malformed tool call, schema validation failure) rather than
        returning malformed data.
        """
        ...


_SYSTEM_PROMPT = (
    "You are a precise code-migration assistant. You are given one Python "
    "call site that uses a deprecated/changed API symbol. Propose the "
    "minimal correct replacement code for that call site given the new "
    "signature, and call the propose_edit tool with your answer. Do not "
    "explain in prose outside the tool call. Preserve the surrounding code "
    "style and only change what the signature change requires."
)


def build_user_prompt(
    old_code: str,
    old_signature: str,
    new_signature: str,
    migration_notes: str | None,
    failure_context: str | None = None,
    doc_context: str | None = None,
) -> str:
    """The single prompt body shared by every adapter.

    Kept in one place so adding a new provider never risks the prompt
    drifting from what the other adapters send. ``failure_context`` (Step 5)
    appends a retry section so a corrected attempt has the previous
    attempt's failure in view instead of repeating it blind. ``doc_context``
    (gap-fix session) appends retrieved documentation, when any was found.
    """

    notes = migration_notes or "(none provided)"
    prompt = (
        f"Old (deprecated) signature: {old_signature}\n"
        f"New signature: {new_signature}\n"
        f"Migration notes: {notes}\n\n"
        f"Code to migrate:\n```python\n{old_code}\n```\n\n"
    )
    if doc_context:
        prompt += f"Relevant documentation:\n{doc_context}\n\n"
    if failure_context:
        prompt += (
            "A previous attempt at this migration was tried and failed "
            "validation. Do not repeat the same mistake:\n"
            f"{failure_context}\n\n"
        )
    prompt += "Call propose_edit with the migrated code and a short rationale."
    return prompt


PROPOSE_EDIT_TOOL_NAME = "propose_edit"
PROPOSE_EDIT_TOOL_DESCRIPTION = (
    "Report the migrated replacement code for the given call site."
)
PROPOSE_EDIT_PARAMETERS_SCHEMA = {
    "type": "object",
    "properties": {
        "new_code": {
            "type": "string",
            "description": "The migrated replacement code for the call site.",
        },
        "rationale": {
            "type": "string",
            "description": "Short explanation of what changed and why.",
        },
    },
    "required": ["new_code", "rationale"],
}
