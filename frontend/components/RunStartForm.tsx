"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { createRun, listRepos, Repo, ApiError } from "@/lib/api";

const inputClass =
  "rounded-lg border border-border-strong bg-surface px-3 py-2 text-sm text-foreground placeholder:text-foreground-muted/60 outline-none transition-colors focus:border-accent";
const labelClass = "flex flex-col gap-1.5 text-sm font-medium text-foreground";

// Sentinel for the "type it manually" option in the saved-repo dropdown --
// not a real repo id, just a value the <select> can hold that means
// "don't prefill anything, leave the fields as the user typed them."
const MANUAL_ENTRY_VALUE = "";

export default function RunStartForm() {
  const router = useRouter();
  const [repoUrl, setRepoUrl] = useState("");
  const [apiName, setApiName] = useState("");
  const [versionFrom, setVersionFrom] = useState("");
  const [versionTo, setVersionTo] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Saved-repos dropdown -- added 2026-09-16, beyond the original 10-step
  // plan (see CLAUDE.md's dated note). A pure shortcut: selecting a saved
  // repo just prefills repo path + API name, same as typing them by hand.
  // Nothing here is required -- an empty repos list, or a failed fetch,
  // just means the dropdown has nothing but "Type manually" in it; typing
  // the path in by hand (the ORIGINAL behavior) keeps working exactly as
  // before either way.
  const [repos, setRepos] = useState<Repo[]>([]);
  const [selectedRepoId, setSelectedRepoId] = useState(MANUAL_ENTRY_VALUE);

  useEffect(() => {
    listRepos()
      .then(setRepos)
      .catch(() => setRepos([])); // shortcut just stays empty on failure
  }, []);

  function handleRepoSelect(repoId: string) {
    setSelectedRepoId(repoId);
    if (repoId === MANUAL_ENTRY_VALUE) return;
    const repo = repos.find((r) => r.id === repoId);
    if (!repo) return;
    setRepoUrl(repo.repo_path);
    setApiName(repo.default_api_name);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const { run_id } = await createRun({
        repo_url: repoUrl,
        api_name: apiName,
        version_from: versionFrom,
        version_to: versionTo,
      });
      router.push(`/run?id=${run_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to start run.");
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex max-w-xl flex-col gap-4 rounded-xl border border-border bg-surface p-5"
    >
      {repos.length > 0 && (
        <label className={labelClass}>
          Saved repo{" "}
          <span className="font-normal text-foreground-muted">
            (optional shortcut)
          </span>
          <select
            value={selectedRepoId}
            onChange={(e) => handleRepoSelect(e.target.value)}
            className={inputClass}
          >
            <option value={MANUAL_ENTRY_VALUE}>Type manually…</option>
            {repos.map((repo) => (
              <option key={repo.id} value={repo.id}>
                {repo.name} ({repo.default_api_name})
              </option>
            ))}
          </select>
        </label>
      )}

      <label className={labelClass}>
        Target repo path
        <input
          required
          value={repoUrl}
          onChange={(e) => {
            setRepoUrl(e.target.value);
            setSelectedRepoId(MANUAL_ENTRY_VALUE);
          }}
          placeholder="C:\path\to\target\repo"
          className={`${inputClass} font-mono text-[13px]`}
        />
      </label>
      <label className={labelClass}>
        API name
        <input
          required
          value={apiName}
          onChange={(e) => {
            setApiName(e.target.value);
            setSelectedRepoId(MANUAL_ENTRY_VALUE);
          }}
          placeholder="oldapi"
          className={inputClass}
        />
      </label>
      <div className="flex gap-3">
        <label className={`${labelClass} flex-1`}>
          Version from
          <input
            required
            value={versionFrom}
            onChange={(e) => setVersionFrom(e.target.value)}
            placeholder="1.x"
            className={inputClass}
          />
        </label>
        <label className={`${labelClass} flex-1`}>
          Version to
          <input
            required
            value={versionTo}
            onChange={(e) => setVersionTo(e.target.value)}
            placeholder="2.0"
            className={inputClass}
          />
        </label>
      </div>

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
        {submitting ? "Starting…" : "Start migration run"}
      </button>
    </form>
  );
}
