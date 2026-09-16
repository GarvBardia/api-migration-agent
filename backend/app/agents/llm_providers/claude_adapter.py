"""Claude tool-use adapter — the documented production-mode default.

Not used unless ``MIGRATION_LLM_PROVIDER=claude`` in ``.env``. Fully
implemented (not a stub) per the Step 4 kickoff prompt, even though the
default `.env` on this dev machine points at OpenRouter (no live
``ANTHROPIC_API_KEY`` was available in this session — see the session
summary for what was verified live vs. mocked).

Uses Claude's native tool-use API (``tools`` + forced ``tool_choice``),
matching the "structured tool calls only" constraint in CLAUDE.md §4.3.
"""

from __future__ import annotations

import anthropic
from pydantic import ValidationError

from .base import (
    PROPOSE_EDIT_PARAMETERS_SCHEMA,
    PROPOSE_EDIT_TOOL_DESCRIPTION,
    PROPOSE_EDIT_TOOL_NAME,
    MigrationEdit,
    ProviderResponseError,
    _SYSTEM_PROMPT,
    build_user_prompt,
)

DEFAULT_MODEL = "claude-3-5-sonnet-20241022"

_TOOL = {
    "name": PROPOSE_EDIT_TOOL_NAME,
    "description": PROPOSE_EDIT_TOOL_DESCRIPTION,
    "input_schema": PROPOSE_EDIT_PARAMETERS_SCHEMA,
}


class ClaudeAdapter:
    """Migration Agent provider backed by Claude's tool-use API."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        if not api_key:
            raise ProviderResponseError(
                "ANTHROPIC_API_KEY is not set; cannot construct ClaudeAdapter"
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

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
        user_prompt = build_user_prompt(
            old_code,
            old_signature,
            new_signature,
            migration_notes,
            failure_context,
            doc_context,
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": PROPOSE_EDIT_TOOL_NAME},
                messages=[{"role": "user", "content": user_prompt}],
            )
        except anthropic.APIError as exc:
            raise ProviderResponseError(f"Claude API call failed: {exc}") from exc

        tool_use = next(
            (
                block
                for block in response.content
                if getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == PROPOSE_EDIT_TOOL_NAME
            ),
            None,
        )
        if tool_use is None:
            raise ProviderResponseError(
                "Claude response did not contain a propose_edit tool_use block "
                f"(stop_reason={getattr(response, 'stop_reason', None)!r})"
            )

        try:
            return MigrationEdit.model_validate(tool_use.input)
        except ValidationError as exc:
            raise ProviderResponseError(
                f"Claude propose_edit arguments failed schema validation: {exc}"
            ) from exc
