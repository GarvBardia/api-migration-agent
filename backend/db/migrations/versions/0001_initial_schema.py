"""initial schema: migration_runs, file_tasks, human_review_queue,
api_doc_chunks, changelog_events

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-23 00:00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pgvector must exist before any `vector(...)` column is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---------------------------------------------------------------
    # migration_runs
    # ---------------------------------------------------------------
    op.create_table(
        "migration_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("repo_url", sa.String(length=1024), nullable=False),
        sa.Column("api_name", sa.String(length=255), nullable=False),
        sa.Column("version_from", sa.String(length=64), nullable=False),
        sa.Column("version_to", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
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
            "status IN ('pending','running','completed','failed','paused')",
            name="ck_migration_runs_status",
        ),
    )
    op.create_index(
        "ix_migration_runs_api_name", "migration_runs", ["api_name"]
    )
    op.create_index("ix_migration_runs_status", "migration_runs", ["status"])

    # ---------------------------------------------------------------
    # file_tasks
    # ---------------------------------------------------------------
    op.create_table(
        "file_tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("migration_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_path", sa.String(length=2048), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("old_code_snippet", sa.Text(), nullable=True),
        sa.Column("new_code_snippet", sa.Text(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column(
            "retry_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "test_pass_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "test_fail_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("migration_source", sa.String(length=10), nullable=True),
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
        sa.UniqueConstraint(
            "run_id", "file_path", name="uq_file_tasks_run_path"
        ),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','validated','failed','needs_review')",
            name="ck_file_tasks_status",
        ),
        sa.CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0.0 AND confidence_score <= 1.0)",
            name="ck_file_tasks_confidence_range",
        ),
        sa.CheckConstraint("retry_count >= 0", name="ck_file_tasks_retry_nonneg"),
    )
    op.create_index("ix_file_tasks_run_id", "file_tasks", ["run_id"])
    op.create_index("ix_file_tasks_status", "file_tasks", ["status"])

    # ---------------------------------------------------------------
    # human_review_queue
    # ---------------------------------------------------------------
    op.create_table(
        "human_review_queue",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "file_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("file_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reviewer_decision", sa.String(length=10), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reviewer_decision IS NULL OR reviewer_decision IN ('approved','rejected','modified')",
            name="ck_human_review_queue_decision",
        ),
    )
    op.create_index(
        "ix_human_review_queue_file_task_id",
        "human_review_queue",
        ["file_task_id"],
    )

    # ---------------------------------------------------------------
    # api_doc_chunks
    # ---------------------------------------------------------------
    op.create_table(
        "api_doc_chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("api_name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_api_doc_chunks_api_name", "api_doc_chunks", ["api_name"])
    # IVFFlat index for approximate nearest-neighbor search over embeddings.
    # `lists` is a rough starting point for small/medium doc sets; tune upward
    # (~sqrt(n_rows)) once real doc volume is known.
    op.execute(
        "CREATE INDEX ix_api_doc_chunks_embedding ON api_doc_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    # ---------------------------------------------------------------
    # changelog_events
    # ---------------------------------------------------------------
    op.create_table(
        "changelog_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("api_name", sa.String(length=255), nullable=False),
        sa.Column("version_from", sa.String(length=64), nullable=False),
        sa.Column("version_to", sa.String(length=64), nullable=False),
        sa.Column("change_type", sa.String(length=20), nullable=False),
        sa.Column("old_signature", sa.Text(), nullable=False),
        sa.Column("new_signature", sa.Text(), nullable=False),
        sa.Column("migration_notes", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "change_type IN ('signature_change','rename','removal','deprecation',"
            "'behavior_change','addition')",
            name="ck_changelog_events_change_type",
        ),
    )
    op.create_index(
        "ix_changelog_events_api_name", "changelog_events", ["api_name"]
    )


def downgrade() -> None:
    op.drop_index("ix_changelog_events_api_name", table_name="changelog_events")
    op.drop_table("changelog_events")

    op.execute("DROP INDEX IF EXISTS ix_api_doc_chunks_embedding")
    op.drop_index("ix_api_doc_chunks_api_name", table_name="api_doc_chunks")
    op.drop_table("api_doc_chunks")

    op.drop_index(
        "ix_human_review_queue_file_task_id", table_name="human_review_queue"
    )
    op.drop_table("human_review_queue")

    op.drop_index("ix_file_tasks_status", table_name="file_tasks")
    op.drop_index("ix_file_tasks_run_id", table_name="file_tasks")
    op.drop_table("file_tasks")

    op.drop_index("ix_migration_runs_status", table_name="migration_runs")
    op.drop_index("ix_migration_runs_api_name", table_name="migration_runs")
    op.drop_table("migration_runs")

    # Not dropping the `vector` extension — other objects/DBs may depend on it.
