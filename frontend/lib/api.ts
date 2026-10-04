/**
 * API client for the Step 7 FastAPI backend.
 *
 * Base URL is configurable via NEXT_PUBLIC_API_BASE_URL (defaults to the
 * local dev backend at http://localhost:8000 -- see backend/app/api/main.py
 * for how to run it). Every type here mirrors backend/app/api/schemas.py
 * field-for-field; keep them in sync if the backend schema changes.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type RunStatus = "pending" | "running" | "completed" | "failed" | "paused";

export type FileTaskStatus =
  | "pending"
  | "in_progress"
  | "validated"
  | "failed"
  | "needs_review";

export type ReviewerDecision = "approved" | "rejected" | "modified";

export interface MigrationRun {
  id: string;
  repo_url: string;
  api_name: string;
  version_from: string;
  version_to: string;
  status: RunStatus;
  created_at: string;
  updated_at: string;
}

export interface FileTask {
  id: string;
  run_id: string;
  file_path: string;
  status: FileTaskStatus;
  old_code_snippet: string | null;
  new_code_snippet: string | null;
  confidence_score: number | null;
  retry_count: number;
  sweep_attempts: number;
  test_pass_count: number;
  test_fail_count: number;
  failure_reason: string | null;
  migration_source: "llm" | "cache" | null;
  line_start: number | null;
  line_end: number | null;
  matched_symbol: string | null;
  changelog_event_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface HumanReviewQueueEntry {
  id: string;
  file_task_id: string;
  reason: string;
  reviewer_decision: ReviewerDecision | null;
  reviewed_at: string | null;
  created_at: string;
}

export interface HumanReviewQueueItem {
  review: HumanReviewQueueEntry;
  file_task: FileTask;
}

export interface RunCreateRequest {
  repo_url: string;
  api_name: string;
  version_from: string;
  version_to: string;
}

export interface ReviewDecisionRequest {
  decision: ReviewerDecision;
  modified_code?: string | null;
}

export interface ReviewDecisionResponse {
  review: HumanReviewQueueEntry;
  file_task: FileTask;
  revalidation_dispatched: boolean;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    // FastAPI sends {"detail": "<plain message>"} for HTTPException. Show
    // just the message when it is a string, so users see the reason and
    // not raw JSON.
    const plain =
      typeof detail === "string"
        ? detail
        : detail &&
          typeof detail === "object" &&
          typeof (detail as { detail?: unknown }).detail === "string"
        ? ((detail as { detail: string }).detail)
        : null;
    super(plain ?? `API request failed (${status}): ${JSON.stringify(detail)}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = await res.text();
    }
    throw new ApiError(res.status, detail);
  }
  // 204/empty bodies aren't used by this API today, but guard anyway.
  const text = await res.text();
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

export function createRun(
  body: RunCreateRequest
): Promise<{ run_id: string }> {
  return request("/runs", { method: "POST", body: JSON.stringify(body) });
}

export interface ServerConfig {
  public_demo_mode: boolean;
}

export function getConfig(): Promise<ServerConfig> {
  return request("/config");
}

export function listRuns(): Promise<MigrationRun[]> {
  return request("/runs");
}

export function getRun(runId: string): Promise<MigrationRun> {
  return request(`/runs/${runId}`);
}

export function getFileTasks(runId: string): Promise<FileTask[]> {
  return request(`/runs/${runId}/file_tasks`);
}

export function getHumanReviewQueue(): Promise<HumanReviewQueueItem[]> {
  return request("/human-review-queue");
}

export function postReviewDecision(
  reviewId: string,
  body: ReviewDecisionRequest
): Promise<ReviewDecisionResponse> {
  return request(`/human-review-queue/${reviewId}/decision`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** SSE event payload shape from GET /runs/{run_id}/events (event: status). */
export interface RunEventsSnapshot {
  run_status: RunStatus;
  file_tasks: Record<string, FileTaskStatus>;
}

export function runEventsUrl(runId: string): string {
  return `${API_BASE_URL}/runs/${runId}/events`;
}

// ---------------------------------------------------------------------------
// repos -- added 2026-09-16, explicitly beyond the original 10-step plan.
// ---------------------------------------------------------------------------

export interface Repo {
  id: string;
  name: string;
  repo_path: string;
  default_api_name: string;
  auto_check_enabled: boolean;
  check_interval_hours: number | null;
  last_auto_checked_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RepoCreateRequest {
  name: string;
  repo_path: string;
  default_api_name: string;
  auto_check_enabled?: boolean;
  check_interval_hours?: number | null;
}

export function listRepos(): Promise<Repo[]> {
  return request("/repos");
}

export function createRepo(body: RepoCreateRequest): Promise<Repo> {
  return request("/repos", { method: "POST", body: JSON.stringify(body) });
}

export function deleteRepo(repoId: string): Promise<void> {
  return request(`/repos/${repoId}`, { method: "DELETE" });
}
