"""Pydantic request/response schemas for the Step 7 REST + SSE API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunCreateRequest(BaseModel):
    repo_url: str
    api_name: str
    version_from: str
    version_to: str


class RunCreateResponse(BaseModel):
    run_id: uuid.UUID


class MigrationRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repo_url: str
    api_name: str
    version_from: str
    version_to: str
    status: str
    created_at: datetime
    updated_at: datetime


class FileTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    file_path: str
    status: str
    old_code_snippet: str | None
    new_code_snippet: str | None
    confidence_score: float | None
    retry_count: int
    sweep_attempts: int
    test_pass_count: int
    test_fail_count: int
    failure_reason: str | None
    migration_source: str | None
    line_start: int | None
    line_end: int | None
    matched_symbol: str | None
    changelog_event_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class HumanReviewQueueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_task_id: uuid.UUID
    reason: str
    reviewer_decision: str | None
    reviewed_at: datetime | None
    created_at: datetime


class HumanReviewQueueItem(BaseModel):
    """One pending review item, joined with its `file_tasks` row for the
    diff/failure context a reviewer needs -- CLAUDE.md's `human_review_queue`
    note is explicit that there's no separate `context` column; join rather
    than duplicate that data."""

    review: HumanReviewQueueRead
    file_task: FileTaskRead


class ReviewDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected", "modified"]
    modified_code: str | None = Field(
        default=None,
        description="Required when decision='modified': the reviewer's corrected code.",
    )


class ReviewDecisionResponse(BaseModel):
    review: HumanReviewQueueRead
    file_task: FileTaskRead
    revalidation_dispatched: bool


# ---------------------------------------------------------------------------
# repos -- added 2026-09-16, explicitly BEYOND the original 10-step plan
# (see CLAUDE.md's dated note).
# ---------------------------------------------------------------------------


class RepoCreateRequest(BaseModel):
    name: str
    repo_path: str
    default_api_name: str
    auto_check_enabled: bool = False
    check_interval_hours: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Hours between scheduled auto-checks. Ignored (may be left "
            "null) when auto_check_enabled is false."
        ),
    )


class RepoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    repo_path: str
    default_api_name: str
    auto_check_enabled: bool
    check_interval_hours: int | None
    last_auto_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime
