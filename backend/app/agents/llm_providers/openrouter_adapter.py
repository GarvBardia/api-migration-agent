"""OpenRouter adapter — OpenAI-compatible chat-completions endpoint with
function-calling.

Genuinely different request/response shape from the Claude adapter (OpenAI
tool-call convention: ``tools`` as a list of ``{"type": "function", ...}``
entries, forced via ``tool_choice``, arguments returned as a JSON *string*
that must be parsed — vs. Claude's already-parsed ``tool_use.input`` dict).
Both adapters converge on the same ``MigrationEdit`` model, proving the
abstraction across two real provider shapes rather than one.

Free, tool-calling-capable model as configured in ``.env``
(``MIGRATION_LLM_MODEL``) — verified live against ``cohere/north-mini-code:free``
during the Step 4 session. See the session summary for the exact model(s)
this was actually tested against; check openrouter.ai/models before relying
on this if it's been more than a few weeks, since the free-model list rotates.
"""

from __future__ import annotations

import json

import httpx
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

OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"

_TOOL = {
    "type": "function",
    "function": {
        "name": PROPOSE_EDIT_TOOL_NAME,
        "description": PROPOSE_EDIT_TOOL_DESCRIPTION,
        "parameters": PROPOSE_EDIT_PARAMETERS_SCHEMA,
    },
}


class OpenRouterAdapter:
    """Migration Agent provider backed by an OpenRouter model via
    OpenAI-compatible function-calling."""

    def __init__(
        self, api_key: str, model: str, timeout_seconds: float = 60.0
    ) -> None:
        if not api_key:
            raise ProviderResponseError(
                "OPENROUTER_API_KEY is not set; cannot construct OpenRouterAdapter"
            )
        if not model:
            raise ProviderResponseError(
                "MIGRATION_LLM_MODEL is not set; cannot construct OpenRouterAdapter"
            )
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

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
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "tools": [_TOOL],
            "tool_choice": {
                "type": "function",
                "function": {"name": PROPOSE_EDIT_TOOL_NAME},
            },
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = httpx.post(
                OPENROUTER_CHAT_COMPLETIONS_URL,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderResponseError(
                f"OpenRouter API call failed: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                f"OpenRouter response was not valid JSON: {exc}"
            ) from exc

        try:
            choices = data["choices"]
            message = choices[0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(
                f"OpenRouter response missing choices[0].message: {data!r}"
            ) from exc

        tool_calls = message.get("tool_calls") or []
        call = next(
            (
                c
                for c in tool_calls
                if c.get("function", {}).get("name") == PROPOSE_EDIT_TOOL_NAME
            ),
            None,
        )
        if call is None:
            raise ProviderResponseError(
                "OpenRouter response did not contain a propose_edit tool call "
                f"(message={message!r})"
            )

        raw_arguments = call["function"].get("arguments", "")
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(
                f"OpenRouter propose_edit arguments were not valid JSON: "
                f"{raw_arguments!r} ({exc})"
            ) from exc

        try:
            return MigrationEdit.model_validate(arguments)
        except ValidationError as exc:
            raise ProviderResponseError(
                f"OpenRouter propose_edit arguments failed schema validation: {exc}"
            ) from exc
