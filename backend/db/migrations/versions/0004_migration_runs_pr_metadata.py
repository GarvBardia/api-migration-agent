"""add pr_url/pr_number to migration_runs (Step 9: GitHub PR Agent)

Revision ID: 0004_migration_runs_pr_metadata
Revises: 0003_file_task_sweep_attempts
Create Date: 2026-08-27 00:00:00

Why this migration exists:

Step 9's done-criteria require "re-running the PR step for an already-PR'd
run does not open a duplicate PR." There was no persisted signal anywhere
that a run already has an open PR -- `open_pr_for_run` needs somewhere to
record that fact so a second call is a no-op (returns the existing PR
info) rather than opening a second PR for the same run. `pr_url`/
`pr_number` are both nullable: NULL means "no PR opened yet for this run,"
matching the same "NULL means still pending" convention already used for
`human_review_queue.reviewer_decision`.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_migration_runs_pr_metadata"
down_revision: Union[str, None] = "0003_file_task_sweep_attempts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "migration_runs",
        sa.Column("pr_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "migration_runs",
        sa.Column("pr_number", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("migration_runs", "pr_number")
    op.drop_column("migration_runs", "pr_url")
