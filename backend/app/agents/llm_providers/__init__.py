"""Provider-agnostic adapter layer for the Migration Agent (Step 4).

Which model actually backs ``propose_edit`` is config-driven
(``MIGRATION_LLM_PROVIDER`` in ``.env``), never a code change — see
``get_provider`` below.
"""

from __future__ import annotations

import os

from .base import MigrationEdit, MigrationLLMProvider, ProviderResponseError
from .claude_adapter import DEFAULT_MODEL as CLAUDE_DEFAULT_MODEL
from .claude_adapter import ClaudeAdapter
from .openrouter_adapter import OpenRouterAdapter

__all__ = [
    "MigrationEdit",
    "MigrationLLMProvider",
    "ProviderResponseError",
    "ClaudeAdapter",
    "OpenRouterAdapter",
    "get_provider",
]


def get_provider(env: os._Environ | dict | None = None) -> MigrationLLMProvider:
    """Construct the configured provider adapter from environment variables.

    ``MIGRATION_LLM_PROVIDER`` selects the adapter (``claude`` |
    ``openrouter``); Claude is the documented production-mode default, used
    only if explicitly selected. Reads ``env`` (defaults to
    ``os.environ``) so tests can inject a fake environment instead of
    monkeypatching global process state.
    """

    env = os.environ if env is None else env
    provider = (env.get("MIGRATION_LLM_PROVIDER") or "claude").strip().lower()

    if provider == "claude":
        return ClaudeAdapter(
            api_key=env.get("ANTHROPIC_API_KEY", ""),
            model=env.get("ANTHROPIC_MODEL") or CLAUDE_DEFAULT_MODEL,
        )
    if provider == "openrouter":
        return OpenRouterAdapter(
            api_key=env.get("OPENROUTER_API_KEY", ""),
            model=env.get("MIGRATION_LLM_MODEL", ""),
        )

    raise ProviderResponseError(
        f"Unknown MIGRATION_LLM_PROVIDER {provider!r}; expected 'claude' or "
        "'openrouter'"
    )
