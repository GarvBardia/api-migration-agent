"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { listRuns, MigrationRun, ApiError } from "@/lib/api";
import StatusBadge from "./StatusBadge";

function EmptyPanel({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-border-strong bg-surface-muted/40 px-4 py-8 text-center text-sm text-foreground-muted">
      {children}
    </div>
  );
}

export default function RunList() {
  const [runs, setRuns] = useState<MigrationRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await listRuns();
      setRuns(data);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load runs.");
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 3000);
    return () => clearInterval(interval);
  }, [refresh]);

  if (error) {
    return (
      <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
        {error}
      </p>
    );
  }
  if (runs === null) {
    return <EmptyPanel>Loading runs…</EmptyPanel>;
  }
  if (runs.length === 0) {
    return <EmptyPanel>No runs yet — start one above.</EmptyPanel>;
  }

  return (
    <div className="overflow-hidden rounded-xl border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-surface-muted/60 text-left text-xs font-medium uppercase tracking-wide text-foreground-muted">
            <th className="px-4 py-2.5">API</th>
            <th className="px-4 py-2.5">Version</th>
            <th className="px-4 py-2.5">Status</th>
            <th className="px-4 py-2.5">Created</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run, i) => (
            <tr
              key={run.id}
              className={`transition-colors hover:bg-surface-muted/60 ${
                i !== runs.length - 1 ? "border-b border-border" : ""
              }`}
            >
              <td className="px-4 py-3">
                <Link
                  href={`/runs/${run.id}`}
                  className="font-medium text-foreground transition-colors hover:text-accent hover:underline"
                >
                  {run.api_name}
                </Link>
              </td>
              <td className="px-4 py-3 font-mono text-xs tabular-nums text-foreground-muted">
                {run.version_from} → {run.version_to}
              </td>
              <td className="px-4 py-3">
                <StatusBadge status={run.status} />
              </td>
              <td className="px-4 py-3 text-foreground-muted">
                {new Date(run.created_at).toLocaleString()}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
