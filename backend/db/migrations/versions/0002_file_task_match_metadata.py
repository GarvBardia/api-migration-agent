"""add match metadata to file_tasks; allow multiple matches per file

Revision ID: 0002_file_task_match_metadata
Revises: 0001_initial_schema
Create Date: 2026-08-25 00:00:00

Why this migration exists (read before applying):

Step 3 (Impact Analysis Agent) needs to record, per AST match: which lines
it's on, which symbol it matched, and which changelog_events row caused the
match. None of that existed in 0001. Additionally, 0001's
`uq_file_tasks_run_path` constraint allows only ONE file_tasks row per
(run_id, file_path) — but a single file can contain multiple independent
breaking-change matches (e.g. two different deprecated calls in the same
file). That constraint has to be loosened or Step 3 cannot write its
second match in a file without violating it.

This migration:
  1. Adds `line_start`, `line_end`, `matched_symbol`, `changelog_event_id`
     to `file_tasks`.
  2. Drops `uq_file_tasks_run_path` and replaces it with a constraint over
     (run_id, file_path, matched_symbol, line_start) — still prevents
     duplicate rows on a re-scan (idempotency), but permits multiple
     distinct matches within the same file.

Do not hand-edit 0001 to make this change — 0001 may already be applied.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_file_task_match_metadata"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "file_tasks",
        sa.Column("line_start", sa.Integer(), nullable=True),
    )
    op.add_column(
        "file_tasks",
        sa.Column("line_end", sa.Integer(), nullable=True),
    )
    op.add_column(
        "file_tasks",
        sa.Column("matched_symbol", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "file_tasks",
        sa.Column(
            "changelog_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("changelog_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_file_tasks_changelog_event_id",
        "file_tasks",
        ["changelog_event_id"],
    )

    # Replace the one-row-per-file constraint with one-row-per-match.
    op.drop_constraint(
        "uq_file_tasks_run_path", "file_tasks", type_="unique"
    )
    op.create_unique_constraint(
        "uq_file_tasks_run_path_symbol_line",
        "file_tasks",
        ["run_id", "file_path", "matched_symbol", "line_start"],
    )

    op.create_check_constraint(
        "ck_file_tasks_line_range",
        "file_tasks",
        "line_start IS NULL OR line_end IS NULL OR line_end >= line_start",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_file_tasks_line_range", "file_tasks", type_="check"
    )
    op.drop_constraint(
        "uq_file_tasks_run_path_symbol_line", "file_tasks", type_="unique"
    )
    op.create_unique_constraint(
        "uq_file_tasks_run_path", "file_tasks", ["run_id", "file_path"]
    )
    op.drop_index(
        "ix_file_tasks_changelog_event_id", table_name="file_tasks"
    )
    op.drop_column("file_tasks", "changelog_event_id")
    op.drop_column("file_tasks", "matched_symbol")
    op.drop_column("file_tasks", "line_end")
    op.drop_column("file_tasks", "line_start")
