"""Shared repo-path validation (added 2026-10-04, public-demo hardening).

Two rules, applied by POST /runs, POST /repos and `scan_task`:

1. In EVERY mode, the path must exist and be a directory. A nonexistent
   path must never end as a "completed" run with zero results.
2. If `ALLOWED_REPO_ROOTS` is set (os.pathsep-separated list), the path,
   after resolving `..` and symlinks, must sit inside one of those roots.
   When unset (local dev), no confinement applies.

`ALLOWED_REPO_ROOTS` is set only in docker-compose.yml for the backend and
celery-worker, never in `.env` (host-run processes also load `.env`).
"""

from __future__ import annotations

import os
from pathlib import Path


class RepoPathError(ValueError):
    """The path is unusable. The message is safe to show to a user."""


def _allowed_roots() -> list[Path]:
    raw = os.environ.get("ALLOWED_REPO_ROOTS", "").strip()
    if not raw:
        return []
    return [Path(p).resolve() for p in raw.split(os.pathsep) if p.strip()]


def validate_repo_path(path_str: str) -> Path:
    """Return the resolved directory, or raise `RepoPathError`."""

    if not path_str or not path_str.strip():
        raise RepoPathError("The folder path is empty.")

    resolved = Path(path_str).resolve()

    if not resolved.exists():
        raise RepoPathError(f"The folder does not exist: {path_str}")
    if not resolved.is_dir():
        raise RepoPathError(f"The path is not a folder: {path_str}")

    roots = _allowed_roots()
    if roots and not any(resolved == r or r in resolved.parents for r in roots):
        allowed = ", ".join(str(r) for r in roots)
        raise RepoPathError(
            f"This server only accepts folders inside: {allowed}"
        )

    return resolved
