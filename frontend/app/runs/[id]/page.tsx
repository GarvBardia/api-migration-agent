"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import Link from "next/link";
import {
  getRun,
  getFileTasks,
  runEventsUrl,
  MigrationRun,
  FileTask,
  RunEventsSnapshot,
  ApiError,
} from "@/lib/api";
import StatusBadge from "@/components/StatusBadge";
import FileTasksTable from "@/components/FileTasksTable";

const TERMINAL_STATUSES = new Set(["completed", "failed"]);

export default function RunDetailPage({ params }: { params: { id: string } }) {
  const runId = params.id;
  const [run, setRun] = useState<MigrationRun | null>(null);
  const [tasks, setTasks] = useState<FileTask[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [runData, taskData] = await Promise.all([
        getRun(runId),
        getFileTasks(runId),
      ]);
      setRun(runData);
      setTasks(taskData);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load run.");
    }
  }, [runId]);

  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Step 8b: SSE-driven live updates -- GET /runs/{id}/events (Step 7's
  // polling-backed SSE stream) tells us WHEN something changed; we still
  // hit the REST endpoints for the full row data (the SSE snapshot only
  // carries status per file_task, not confidence/test counts/etc.).
  useEffect(() => {
    const source = new EventSource(runEventsUrl(runId));
    setLive(true);

    source.addEventListener("status", (event: MessageEvent) => {
      const snapshot = JSON.parse(event.data) as RunEventsSnapshot;
      refreshRef.current();
      if (TERMINAL_STATUSES.has(snapshot.run_status)) {
        source.close();
        setLive(false);
      }
    });

    source.addEventListener("error", () => {
      // A genuinely dead connection (server restarted, network blip) --
      // browsers auto-retry SSE by default; if the run is already
      // terminal we don't want that, so just stop treating it as live.
      setLive(false);
    });

    return () => {
      source.close();
      setLive(false);
    };
  }, [runId]);

  if (error) {
    return (
      <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
        {error}
      </p>
    );
  }
  if (!run) {
    return <p className="text-sm text-foreground-muted">Loading…</p>;
  }

  return (
    <div className="flex flex-col gap-8">
      <div>
        <Link
          href="/"
          className="mb-3 inline-flex items-center gap-1 text-sm text-foreground-muted transition-colors hover:text-foreground"
        >
          ← All runs
        </Link>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">
            {run.api_name}
          </h1>
          <span className="font-mono text-sm text-foreground-muted">
            {run.version_from} → {run.version_to}
          </span>
          <StatusBadge status={run.status} />
          {live && (
            <span className="flex items-center gap-1.5 text-xs font-medium text-emerald-600">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
              Live
            </span>
          )}
        </div>
        <p className="mt-2 inline-block rounded-md bg-surface-muted px-2 py-1 font-mono text-xs text-foreground-muted">
          {run.repo_url}
        </p>
      </div>
      <section>
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-foreground-muted">
          File tasks ({tasks.length})
        </h2>
        <FileTasksTable tasks={tasks} />
      </section>
    </div>
  );
}
