type StatusStyle = { chip: string; dot: string };

// One deliberate color per state, kept desaturated so it reads as a status
// language rather than decoration -- green/amber/red/blue/neutral, each
// with a matching dot so status is never conveyed by hue alone.
const STYLES: Record<string, StatusStyle> = {
  pending: { chip: "bg-zinc-100 text-zinc-600", dot: "bg-zinc-400" },
  paused: { chip: "bg-zinc-100 text-zinc-600", dot: "bg-zinc-400" },
  running: { chip: "bg-blue-50 text-blue-700", dot: "bg-blue-500" },
  in_progress: { chip: "bg-blue-50 text-blue-700", dot: "bg-blue-500" },
  completed: { chip: "bg-emerald-50 text-emerald-700", dot: "bg-emerald-500" },
  validated: { chip: "bg-emerald-50 text-emerald-700", dot: "bg-emerald-500" },
  needs_review: { chip: "bg-amber-50 text-amber-700", dot: "bg-amber-500" },
  failed: { chip: "bg-rose-50 text-rose-700", dot: "bg-rose-500" },
};

const FALLBACK: StatusStyle = { chip: "bg-zinc-100 text-zinc-600", dot: "bg-zinc-400" };

export default function StatusBadge({ status }: { status: string }) {
  const style = STYLES[status] ?? FALLBACK;
  const pulse = status === "running" || status === "in_progress";

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${style.chip}`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${style.dot} ${pulse ? "animate-pulse" : ""}`}
      />
      {status.replace(/_/g, " ")}
    </span>
  );
}
