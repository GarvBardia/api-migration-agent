import { FileTask } from "@/lib/api";
import StatusBadge from "./StatusBadge";

export default function FileTasksTable({ tasks }: { tasks: FileTask[] }) {
  if (tasks.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border-strong bg-surface-muted/40 px-4 py-8 text-center text-sm text-foreground-muted">
        No file tasks yet — scan may still be running.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full min-w-[720px] text-sm">
        <thead>
          <tr className="border-b border-border bg-surface-muted/60 text-left text-xs font-medium uppercase tracking-wide text-foreground-muted">
            <th className="px-4 py-2.5">File</th>
            <th className="px-4 py-2.5">Symbol</th>
            <th className="px-4 py-2.5">Line</th>
            <th className="px-4 py-2.5">Status</th>
            <th className="px-4 py-2.5">Confidence</th>
            <th className="px-4 py-2.5">Tests (pass/fail)</th>
            <th className="px-4 py-2.5">Retries</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t, i) => (
            <tr
              key={t.id}
              className={`align-top transition-colors hover:bg-surface-muted/60 ${
                i !== tasks.length - 1 ? "border-b border-border" : ""
              }`}
            >
              <td className="px-4 py-3 font-mono text-xs">{t.file_path}</td>
              <td className="px-4 py-3 font-mono text-xs text-foreground-muted">
                {t.matched_symbol}
              </td>
              <td className="px-4 py-3 tabular-nums text-foreground-muted">
                {t.line_start}
              </td>
              <td className="px-4 py-3">
                <StatusBadge status={t.status} />
              </td>
              <td className="px-4 py-3 tabular-nums">
                {t.confidence_score !== null ? t.confidence_score.toFixed(2) : "—"}
              </td>
              <td className="px-4 py-3 tabular-nums">
                <span className="text-emerald-600">{t.test_pass_count}</span>
                <span className="text-foreground-muted"> / </span>
                <span className="text-rose-600">{t.test_fail_count}</span>
              </td>
              <td className="px-4 py-3 tabular-nums text-foreground-muted">
                {t.retry_count}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
