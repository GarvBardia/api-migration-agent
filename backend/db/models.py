"""SQLAlchemy ORM models for the Autonomous API Migration Agent.

Five tables back the core 10-step pipeline:

- ``MigrationRun``      one row per end-to-end migration job (a repo + an
                         API version bump). This is the unit of resumability.
- ``FileTask``          one row per source file touched by a run. Tracks
                         per-file status, confidence, and retry count so the
                         orchestrator can resume without reprocessing work.
- ``HumanReviewQueue``  files that fell below the confidence threshold or
                         exhausted retries, awaiting a human decision.
- ``ApiDocChunk``       embedded documentation chunks used for RAG retrieval
                         when the Migration Agent grounds an edit.
- ``ChangelogEvent``    structured output of the Ingestion Agent; the source
                         of truth for "what changed between these versions".

A sixth table, ``Repo`` (added 2026-09-16), is explicitly BEYOND the
original 10-step plan -- see CLAUDE.md's dated note. It backs saved repos,
scheduled auto-checking, and (indirectly) webhook notifications.

All primary keys are UUIDs (generated client-side via ``uuid.uuid4``) so rows
can be created before a transaction commits and referenced immediately by
foreign keys elsewhere in the pipeline.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""

    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunStatus(str, enum.Enum):
    """Lifecycle states for a ``MigrationRun``."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class FileTaskStatus(str, enum.Enum):
    """Lifecycle states for a single file within a run.

    The orchestrator uses this field as its idempotency checkpoint: any file
    already at ``VALIDATED`` is skipped entirely on resume.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    VALIDATED = "validated"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class ReviewerDecision(str, enum.Enum):
    """Outcome of a human review on a flagged file task."""

    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"


class ChangeType(str, enum.Enum):
    """Category of API change described by a changelog event."""

    SIGNATURE_CHANGE = "signature_change"
    RENAME = "rename"
    REMOVAL = "removal"
    DEPRECATION = "deprecation"
    BEHAVIOR_CHANGE = "behavior_change"
    ADDITION = "addition"


class MigrationSource(str, enum.Enum):
    """Where a file's migration edit came from — used for cache-hit metrics."""

    LLM = "llm"
    CACHE = "cache"


def _uuid_pk() -> Mapped[uuid.UUID]:
    """Shared UUID primary-key column factory.

    Generated client-side (``default=uuid.uuid4``) rather than via a DB
    default so newly constructed ORM objects have a usable ``.id`` before
    ``flush()``/``commit()`` — the orchestrator relies on this to wire up
    foreign keys (e.g. ``FileTask.run_id``) within the same unit of work.
    """

    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# ---------------------------------------------------------------------------
# migration_runs
# ---------------------------------------------------------------------------


class MigrationRun(Base):
    """One end-to-end migration job: a target repo migrated across one API's
    version bump.

    This is the top-level resumability unit — the orchestrator loads a run
    by ``id``, finds all ``FileTask`` rows still short of ``VALIDATED``, and
    continues from there regardless of whether the previous process crashed
    mid-file.
    """

    __tablename__ = "migration_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    api_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version_from: Mapped[str] = mapped_column(String(64), nullable=False)
    version_to: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        String(20), nullable=False, default=RunStatus.PENDING, index=True
    )
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    file_tasks: Mapped[list["FileTask"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','paused')",
            name="ck_migration_runs_status",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<MigrationRun id={self.id} api={self.api_name} "
            f"{self.version_from}->{self.version_to} status={self.status}>"
        )


# ---------------------------------------------------------------------------
# file_tasks
# ---------------------------------------------------------------------------


class FileTask(Base):
    """A single source file being migrated within a run.

    Holds enough state (snippets, confidence, retry count, test counts) for
    the orchestrator, the Validation Agent's retry loop, and the frontend's
    diff viewer to all work off this one row.
    """

    __tablename__ = "file_tasks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("migration_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[FileTaskStatus] = mapped_column(
        String(20), nullable=False, default=FileTaskStatus.PENDING, index=True
    )
    old_code_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_code_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sweep_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    test_pass_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    test_fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    migration_source: Mapped[MigrationSource | None] = mapped_column(
        String(10), nullable=True
    )
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_symbol: Mapped[str | None] = mapped_column(String(500), nullable=True)
    changelog_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("changelog_events.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    run: Mapped["MigrationRun"] = relationship(back_populates="file_tasks")
    review_entries: Mapped[list["HumanReviewQueue"]] = relationship(
        back_populates="file_task", cascade="all, delete-orphan"
    )
    changelog_event: Mapped["ChangelogEvent | None"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "file_path",
            "matched_symbol",
            "line_start",
            name="uq_file_tasks_run_path_symbol_line",
        ),
        CheckConstraint(
            "status IN ('pending','in_progress','validated','failed','needs_review')",
            name="ck_file_tasks_status",
        ),
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0.0 AND confidence_score <= 1.0)",
            name="ck_file_tasks_confidence_range",
        ),
        CheckConstraint("retry_count >= 0", name="ck_file_tasks_retry_nonneg"),
        CheckConstraint(
            "sweep_attempts >= 0", name="ck_file_tasks_sweep_attempts_nonneg"
        ),
        CheckConstraint(
            "line_start IS NULL OR line_end IS NULL OR line_end >= line_start",
            name="ck_file_tasks_line_range",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<FileTask id={self.id} file={self.file_path} "
            f"status={self.status} retries={self.retry_count}>"
        )


# ---------------------------------------------------------------------------
# human_review_queue
# ---------------------------------------------------------------------------


class HumanReviewQueue(Base):
    """A file task that needs a human decision — either because confidence
    fell below threshold or because it exhausted its retry budget.
    """

    __tablename__ = "human_review_queue"

    id: Mapped[uuid.UUID] = _uuid_pk()
    file_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("file_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer_decision: Mapped[ReviewerDecision | None] = mapped_column(
        String(10), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    file_task: Mapped["FileTask"] = relationship(back_populates="review_entries")

    __table_args__ = (
        CheckConstraint(
            "reviewer_decision IS NULL OR reviewer_decision IN ('approved','rejected','modified')",
            name="ck_human_review_queue_decision",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return f"<HumanReviewQueue id={self.id} file_task_id={self.file_task_id} decision={self.reviewer_decision}>"


# ---------------------------------------------------------------------------
# api_doc_chunks
# ---------------------------------------------------------------------------


class ApiDocChunk(Base):
    """An embedded chunk of the *new* API's documentation, used by the
    Migration Agent's RAG retriever to ground each generated edit.

    ``embedding`` uses the pgvector ``vector(384)`` type, matching the
    local ``sentence-transformers`` model ``all-MiniLM-L6-v2`` (Step 10,
    migration ``0005`` -- resized down from ``vector(1536)``, which was
    sized for OpenAI's ``text-embedding-3-small`` but never had a
    configured API key). Confirmed empirically (384) by actually loading
    and running the model, not assumed from the model name. Resize the
    column again if a different embedding model is swapped in.
    """

    __tablename__ = "api_doc_chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    api_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return f"<ApiDocChunk id={self.id} api={self.api_name} version={self.version}>"


# ---------------------------------------------------------------------------
# changelog_events
# ---------------------------------------------------------------------------


class ChangelogEvent(Base):
    """A single structured breaking-change record produced by the Ingestion
    Agent from a changelog feed (RSS or hardcoded JSON for now).

    This is the schema referenced in the spec: ``{ api_name, version_from,
    version_to, change_type, old_signature, new_signature, migration_notes }``.
    """

    __tablename__ = "changelog_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    api_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version_from: Mapped[str] = mapped_column(String(64), nullable=False)
    version_to: Mapped[str] = mapped_column(String(64), nullable=False)
    change_type: Mapped[ChangeType] = mapped_column(String(20), nullable=False)
    old_signature: Mapped[str] = mapped_column(Text, nullable=False)
    new_signature: Mapped[str] = mapped_column(Text, nullable=False)
    migration_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "change_type IN ('signature_change','rename','removal','deprecation',"
            "'behavior_change','addition')",
            name="ck_changelog_events_change_type",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<ChangelogEvent id={self.id} api={self.api_name} "
            f"{self.version_from}->{self.version_to} type={self.change_type}>"
        )


# ---------------------------------------------------------------------------
# repos -- added 2026-09-16, explicitly BEYOND the original 10-step plan.
# See CLAUDE.md's dated note. Three related additions share this table:
# saved repos (a name/path/api_name shortcut for the run-start form),
# scheduled auto-checking (auto_check_enabled/check_interval_hours/
# last_auto_checked_at, read by app.tasks.periodic_auto_check_sweep), and
# indirectly webhook notifications (which fire off the runs this table's
# sweep task creates, same as any other run).
# ---------------------------------------------------------------------------


class Repo(Base):
    """A saved (name, repo_path, default_api_name) shortcut, optionally
    opted into scheduled auto-checking: periodically look for new
    `changelog_events` matching `default_api_name` and kick off a real run
    automatically instead of requiring a manual `POST /runs` each time.
    """

    __tablename__ = "repos"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    repo_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    default_api_name: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    auto_check_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    # Nullable: a repo can have auto-checking enabled with no interval set
    # yet -- the sweep simply skips it (see app/tasks.py) rather than
    # treating "unset" as "check constantly".
    check_interval_hours: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # The one column this feature needed beyond its own literal spec: "a
    # repo's last auto-checked time compared against its
    # check_interval_hours is enough" requires somewhere to hold that
    # timestamp. Set every sweep tick a repo is actually examined
    # (whether or not anything new was found), not only when a run is
    # triggered -- see periodic_auto_check_sweep's docstring.
    last_auto_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "check_interval_hours IS NULL OR check_interval_hours > 0",
            name="ck_repos_check_interval_positive",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return (
            f"<Repo id={self.id} name={self.name!r} "
            f"api={self.default_api_name} auto_check={self.auto_check_enabled}>"
        )
