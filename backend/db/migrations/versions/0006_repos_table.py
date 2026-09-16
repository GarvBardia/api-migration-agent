"""repos table -- saved repos + scheduled auto-checking (beyond the
original 10-step plan; see CLAUDE.md's dated note for the full context)

Revision ID: 0006_repos_table
Revises: 0005_api_doc_chunks_embed_dim
Create Date: 2026-09-16 00:00:00

New, standalone feature added 2026-09-16, explicitly beyond the original
10-step plan: lets a user save a (name, repo_path, default_api_name)
triple instead of retyping a repo path every run, and optionally opt a
saved repo into scheduled auto-checking (a periodic Celery Beat task,
`app.tasks.periodic_auto_check_sweep`, checks for unprocessed
`changelog_events` matching the repo's `default_api_name` and, if any
exist and the repo's check interval has elapsed, kicks off a real run
automatically).

`last_auto_checked_at` isn't explicitly named in the feature's own spec,
but is required to implement "a repo's last auto-checked time compared
against its check_interval_hours is enough" (the spec's own stated
gating rule) -- there is no other column that could hold that timestamp,
so it's added here as the one necessary addition beyond the literal
column list given.

`21 chars` for this revision id -- comfortably under `alembic_version.
version_num`'s `VARCHAR(32)` (see migration `0005`'s dated note on why
that limit gets checked explicitly now, after being hit once already).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_repos_table"
down_revision: Union[str, None] = "0005_api_doc_chunks_embed_dim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "repos",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("repo_path", sa.String(length=1024), nullable=False),
        sa.Column("default_api_name", sa.String(length=255), nullable=False),
        sa.Column(
            "auto_check_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        # Nullable: auto-checking can be enabled with no interval set yet
        # (the sweep simply skips a repo whose interval is unset -- see
        # app/tasks.py's periodic_auto_check_sweep).
        sa.Column("check_interval_hours", sa.Integer(), nullable=True),
        sa.Column(
            "last_auto_checked_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "check_interval_hours IS NULL OR check_interval_hours > 0",
            name="ck_repos_check_interval_positive",
        ),
    )
    op.create_index("ix_repos_default_api_name", "repos", ["default_api_name"])
    op.create_index(
        "ix_repos_auto_check_enabled", "repos", ["auto_check_enabled"]
    )


def downgrade() -> None:
    op.drop_index("ix_repos_auto_check_enabled", table_name="repos")
    op.drop_index("ix_repos_default_api_name", table_name="repos")
    op.drop_table("repos")
