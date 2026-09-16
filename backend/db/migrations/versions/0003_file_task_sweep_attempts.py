"""add sweep_attempts to file_tasks (Step 6b: crash-resume/dead-task detection)

Revision ID: 0003_file_task_sweep_attempts
Revises: 0002_file_task_match_metadata
Create Date: 2026-08-26 00:00:00

Why this migration exists (read before applying):

Step 6b needs to cap how many times the orchestrator's crash-resume logic
will retry a `file_tasks` row stuck at `status='in_progress'` before giving
up and marking the run `failed`. This is a DIFFERENT counter from
`retry_count` (Step 5's Migration/Validation Agent fix-and-retest budget,
for a WRONG migration) -- `sweep_attempts` counts INFRASTRUCTURE-failure
resume attempts (a worker crashing, a network blip, a Docker daemon
hiccup). Conflating the two would make it impossible to tell "the fix kept
failing tests" apart from "the worker kept crashing" when reading a stuck
row's history -- CLAUDE.md's §4 note on this fork says explicitly not to
reuse `retry_count` for this.

Deliberately NOT adding a new file_tasks status value: the check
constraint (`ck_file_tasks_status`) still only allows the original five
values. A file_task that exceeds the sweep-attempts cap lands at the
existing `status='failed'` (already a valid terminal status), not a new
enum member.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_file_task_sweep_attempts"
down_revision: Union[str, None] = "0002_file_task_match_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "file_tasks",
        sa.Column(
            "sweep_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "ck_file_tasks_sweep_attempts_nonneg",
        "file_tasks",
        "sweep_attempts >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_file_tasks_sweep_attempts_nonneg", "file_tasks", type_="check"
    )
    op.drop_column("file_tasks", "sweep_attempts")
