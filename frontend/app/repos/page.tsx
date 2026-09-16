"use client";

import { useEffect, useState, useCallback } from "react";
import { listRepos, createRepo, deleteRepo, Repo, ApiError } from "@/lib/api";

const inputClass =
  "rounded-lg border border-border-strong bg-surface px-3 py-2 text-sm text-foreground placeholder:text-foreground-muted/60 outline-none transition-colors focus:border-accent";
const labelClass = "flex flex-col gap-1.5 text-sm font-medium text-foreground";

function EmptyPanel({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-border-strong bg-surface-muted/40 px-4 py-8 text-center text-sm text-foreground-muted">
      {children}
    </div>
  );
}

function AddRepoForm({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [apiName, setApiName] = useState("");
  const [autoCheck, setAutoCheck] = useState(false);
  const [intervalHours, setIntervalHours] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await createRepo({
        name,
        repo_path: repoPath,
        default_api_name: apiName,
        auto_check_enabled: autoCheck,
        check_interval_hours:
          autoCheck && intervalHours ? Number(intervalHours) : null,
      });
      setName("");
      setRepoPath("");
      setApiName("");
      setAutoCheck(false);
      setIntervalHours("");
      onAdded();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save repo.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex max-w-xl flex-col gap-4 rounded-xl border border-border bg-surface p-5"
    >
      <label className={labelClass}>
        Name
        <input
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="My project"
          className={inputClass}
        />
      </label>
      <label className={labelClass}>
        Repo path
        <input
          required
          value={repoPath}
          onChange={(e) => setRepoPath(e.target.value)}
          placeholder="C:\path\to\target\repo"
          className={`${inputClass} font-mono text-[13px]`}
        />
      </label>
      <label className={labelClass}>
        Default API name
        <input
          required
          value={apiName}
          onChange={(e) => setApiName(e.target.value)}
          placeholder="oldapi"
          className={inputClass}
        />
      </label>

      <label className="flex items-center gap-2 text-sm font-medium text-foreground">
        <input
          type="checkbox"
          checked={autoCheck}
          onChange={(e) => setAutoCheck(e.target.checked)}
          className="h-4 w-4 rounded border-border-strong accent-accent"
        />
        Auto-check for new changes
      </label>
      {autoCheck && (
        <label className={labelClass}>
          Check every (hours)
          <input
            type="number"
            min={1}
            required={autoCheck}
            value={intervalHours}
            onChange={(e) => setIntervalHours(e.target.value)}
            placeholder="24"
            className={`${inputClass} max-w-[120px]`}
          />
        </label>
      )}

      {error && (
        <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={submitting}
        className="w-fit rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-foreground transition-all hover:opacity-90 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
      >
        {submitting ? "Saving…" : "Save repo"}
      </button>
    </form>
  );
}

function RepoTable({
  repos,
  onDeleted,
}: {
  repos: Repo[];
  onDeleted: () => void;
}) {
  const [deletingId, setDeletingId] = useState<string | null>(null);

  async function handleDelete(id: string) {
    setDeletingId(id);
    try {
      await deleteRepo(id);
      onDeleted();
    } finally {
      setDeletingId(null);
    }
  }

  if (repos.length === 0) {
    return <EmptyPanel>No saved repos yet — add one above.</EmptyPanel>;
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full min-w-[720px] text-sm">
        <thead>
          <tr className="border-b border-border bg-surface-muted/60 text-left text-xs font-medium uppercase tracking-wide text-foreground-muted">
            <th className="px-4 py-2.5">Name</th>
            <th className="px-4 py-2.5">Path</th>
            <th className="px-4 py-2.5">API</th>
            <th className="px-4 py-2.5">Auto-check</th>
            <th className="px-4 py-2.5">Last checked</th>
            <th className="px-4 py-2.5" />
          </tr>
        </thead>
        <tbody>
          {repos.map((repo, i) => (
            <tr
              key={repo.id}
              className={`align-top transition-colors hover:bg-surface-muted/60 ${
                i !== repos.length - 1 ? "border-b border-border" : ""
              }`}
            >
              <td className="px-4 py-3 font-medium">{repo.name}</td>
              <td className="px-4 py-3 font-mono text-xs text-foreground-muted">
                {repo.repo_path}
              </td>
              <td className="px-4 py-3 font-mono text-xs">
                {repo.default_api_name}
              </td>
              <td className="px-4 py-3 text-foreground-muted">
                {repo.auto_check_enabled
                  ? `every ${repo.check_interval_hours ?? "—"}h`
                  : "off"}
              </td>
              <td className="px-4 py-3 text-foreground-muted">
                {repo.last_auto_checked_at
                  ? new Date(repo.last_auto_checked_at).toLocaleString()
                  : "never"}
              </td>
              <td className="px-4 py-3 text-right">
                <button
                  disabled={deletingId === repo.id}
                  onClick={() => handleDelete(repo.id)}
                  className="rounded-md px-2 py-1 text-xs font-medium text-rose-600 transition-colors hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {deletingId === repo.id ? "Removing…" : "Remove"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ReposPage() {
  const [repos, setRepos] = useState<Repo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await listRepos();
      setRepos(data);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load repos.");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <div className="flex flex-col gap-12">
      <section>
        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-foreground-muted">
          Shortcut
        </p>
        <h1 className="mb-4 text-2xl font-semibold tracking-tight text-balance">
          Saved repos
        </h1>
        <AddRepoForm onAdded={refresh} />
      </section>
      <section>
        <p className="mb-1 text-xs font-medium uppercase tracking-wide text-foreground-muted">
          Saved
        </p>
        <h2 className="mb-4 text-xl font-semibold tracking-tight">Repos</h2>
        {error && (
          <p className="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {error}
          </p>
        )}
        {repos === null ? (
          <EmptyPanel>Loading…</EmptyPanel>
        ) : (
          <RepoTable repos={repos} onDeleted={refresh} />
        )}
      </section>
    </div>
  );
}
