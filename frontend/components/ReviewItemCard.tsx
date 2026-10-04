"use client";

import { useEffect, useState } from "react";
import {
  HumanReviewQueueItem,
  postReviewDecision,
  getConfig,
  ApiError,
} from "@/lib/api";
import StatusBadge from "./StatusBadge";

const ACCENT_BORDER: Record<string, string> = {
  needs_review: "border-l-amber-400",
  failed: "border-l-rose-400",
  validated: "border-l-emerald-400",
};

function StatChip({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-md bg-surface-muted px-2 py-1 text-xs text-foreground-muted">
      <span className="text-foreground-muted/70">{label}</span>
      <span className="font-medium tabular-nums text-foreground">{value}</span>
    </span>
  );
}

export default function ReviewItemCard({
  item,
  onDecided,
}: {
  item: HumanReviewQueueItem;
  /**
   * Called after a decision is successfully submitted. `message` is a
   * confirmation string to surface to the user -- the CALLER (the review
   * queue page) is responsible for displaying it, e.g. as a toast that
   * outlives this component. This card is removed from the list the
   * moment the parent refetches (the decided item no longer matches the
   * "pending" filter), so this component cannot reliably show its own
   * post-decision confirmation UI -- it would unmount before ever
   * painting. See CLAUDE.md's dated note on this fix.
   */
  onDecided: (message: string) => void;
}) {
  const { review, file_task: task } = item;
  const [modifying, setModifying] = useState(false);
  const [modifiedCode, setModifiedCode] = useState(task.new_code_snippet ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [demoMode, setDemoMode] = useState(false);

  useEffect(() => {
    getConfig()
      .then((c) => setDemoMode(c.public_demo_mode))
      .catch(() => setDemoMode(false));
  }, []);

  async function decide(
    decision: "approved" | "rejected" | "modified",
    code?: string
  ) {
    setSubmitting(true);
    setError(null);
    try {
      const result = await postReviewDecision(review.id, {
        decision,
        modified_code: code,
      });
      const message = result.revalidation_dispatched
        ? `Retesting your version of ${task.file_path}. It is marked validated only if it passes its tests and clears the confidence threshold. Otherwise it comes back here.`
        : `Recorded "${decision}" for ${task.file_path}.`;
      onDecided(message);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to submit decision.");
      setSubmitting(false);
    }
  }

  const accentBorder = ACCENT_BORDER[task.status] ?? "border-l-border-strong";

  return (
    <div
      className={`flex flex-col gap-4 rounded-xl border border-l-4 border-border bg-surface p-5 ${accentBorder}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-baseline gap-1.5">
          <span className="font-mono text-sm font-medium text-foreground">
            {task.file_path}
          </span>
          <span className="font-mono text-xs text-foreground-muted">
            :{task.line_start}
          </span>
        </div>
        <StatusBadge status={task.status} />
      </div>

      <p className="rounded-md bg-surface-muted px-3 py-2 text-sm text-foreground-muted">
        {review.reason}
      </p>

      {task.failure_reason && (
        <details className="text-xs text-foreground-muted">
          <summary className="cursor-pointer select-none font-medium hover:text-foreground">
            Failure detail
          </summary>
          <pre className="mt-1.5 whitespace-pre-wrap rounded-md bg-surface-muted p-2.5 font-mono">
            {task.failure_reason}
          </pre>
        </details>
      )}

      <div className="grid grid-cols-1 gap-3 text-xs sm:grid-cols-2">
        <div>
          <div className="mb-1.5 font-medium text-rose-700">Old code</div>
          <pre className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-rose-200 bg-rose-50 p-2.5 font-mono text-rose-950">
            {task.old_code_snippet ?? "(none)"}
          </pre>
        </div>
        <div>
          <div className="mb-1.5 font-medium text-emerald-700">
            Proposed new code
          </div>
          <pre className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-emerald-200 bg-emerald-50 p-2.5 font-mono text-emerald-950">
            {task.new_code_snippet ?? "(none)"}
          </pre>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <StatChip label="confidence" value={task.confidence_score?.toFixed(2) ?? "n/a"} />
        <StatChip
          label="tests"
          value={`${task.test_pass_count}/${task.test_fail_count}`}
        />
        <StatChip label="retries" value={task.retry_count} />
      </div>

      {modifying ? (
        <div className="flex flex-col gap-2.5 border-t border-border pt-4">
          <textarea
            value={modifiedCode}
            onChange={(e) => setModifiedCode(e.target.value)}
            rows={4}
            className="rounded-lg border border-border-strong bg-surface p-2.5 font-mono text-xs outline-none transition-colors focus:border-accent"
            placeholder="Your corrected code..."
          />
          <div className="flex gap-2">
            <button
              disabled={submitting || !modifiedCode.trim()}
              onClick={() => decide("modified", modifiedCode)}
              className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-accent-foreground transition-all hover:opacity-90 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
            >
              Submit corrected code (re-validates)
            </button>
            <button
              disabled={submitting}
              onClick={() => setModifying(false)}
              className="rounded-lg px-3 py-1.5 text-sm font-medium text-foreground-muted transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="flex gap-2 border-t border-border pt-4">
          <button
            disabled={submitting}
            onClick={() => decide("approved")}
            className="rounded-lg bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white transition-all hover:bg-emerald-700 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            Approve
          </button>
          <button
            disabled={submitting}
            onClick={() => decide("rejected")}
            className="rounded-lg bg-rose-600 px-3 py-1.5 text-sm font-medium text-white transition-all hover:bg-rose-700 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            Reject
          </button>
          <button
            disabled={submitting || demoMode}
            onClick={() => setModifying(true)}
            className="rounded-lg bg-surface-muted px-3 py-1.5 text-sm font-medium text-foreground transition-all hover:bg-border active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            Modify
          </button>
          {demoMode && (
            <span className="self-center text-xs text-foreground-muted">
              Modify is turned off on the public demo.
            </span>
          )}
        </div>
      )}

      {error && (
        <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}
    </div>
  );
}
