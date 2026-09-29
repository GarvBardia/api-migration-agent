import Link from "next/link";
import StatusBadge from "@/components/StatusBadge";

// Confide landing page (added 2026-09-23). The tool itself moved to /app.
// Copy rules for this page (and all user-facing text): no em or en dashes,
// no marketing vocabulary, short sentences, no exclamation points. Every
// claim here must match what the pipeline actually does; see CLAUDE.md §4d
// before changing it.

const eyebrow =
  "mb-1 text-xs font-medium uppercase tracking-wide text-foreground-muted";
const card = "rounded-xl border border-border bg-surface p-5";

const STEPS = [
  {
    title: "Reads what changed.",
    body: "Confide looks up the recorded changes to the library between the two versions you pick. For example, a function that was renamed, moved, or removed.",
  },
  {
    title: "Finds where your code uses the old version.",
    body: "It reads the structure of your code, not just the text. That way it also catches code that imports the library under a different name.",
  },
  {
    title: "Writes a fix.",
    body: "For each place it found, an AI model writes replacement code based on exactly what changed.",
  },
  {
    title: "Tests the fix before trusting it.",
    body: "Confide applies the fix to a copy of your project and runs your project's own tests in an isolated sandbox with no internet access. If the tests fail, it writes a new fix using the error message and tests again. It stops after three failed attempts. Your original files are never edited.",
  },
  {
    title: "Finishes it, or asks you.",
    body: "A fix that passes its tests and that Confide is confident about is marked validated. Everything else goes to the Review Queue, where you decide.",
  },
];

const STATUSES: { statuses: string[]; meaning: string }[] = [
  {
    statuses: ["validated"],
    meaning:
      "The fix passed your tests and Confide is confident in it. Nothing for you to do.",
  },
  {
    statuses: ["needs_review"],
    meaning:
      "A person should look. The tests kept failing, Confide could not write a usable fix, or the tests passed but Confide is not sure it changed the right code. It is waiting in the Review Queue.",
  },
  {
    statuses: ["failed"],
    meaning:
      "Confide hit an error it could not recover from, so it stopped. Nothing in your project was changed. You can start the run again.",
  },
  {
    statuses: ["pending", "running", "in_progress"],
    meaning:
      "Still working. Pending means waiting to start. Running and in progress mean Confide is working on it now.",
  },
  {
    statuses: ["completed"],
    meaning:
      "Shown on a whole run. The run is finished. Check each file's status to see how it went.",
  },
];

function UsageCard({ title, items }: { title: string; items: string[] }) {
  return (
    <div className={card}>
      <h3 className="mb-3 text-base font-semibold tracking-tight">{title}</h3>
      <ul className="flex flex-col gap-2 text-sm text-foreground-muted">
        {items.map((item) => (
          <li key={item} className="flex gap-2">
            <span
              aria-hidden
              className="mt-2 h-1 w-1 shrink-0 rounded-full bg-foreground-muted/60"
            />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function LandingPage() {
  return (
    <div className="flex flex-col gap-20">
      {/* 1. What it is */}
      <section className="max-w-2xl pt-6">
        <h1 className="text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
          Confide fixes your code when a library it depends on changes.
        </h1>
        <p className="mt-4 text-base leading-relaxed text-foreground-muted">
          Most projects are built on libraries, code other people write and
          publish. When a library releases a version that breaks old code,
          Confide finds the affected lines, writes a fix for each one, and tests
          every fix before you rely on it.
        </p>
        <div className="mt-6 flex flex-wrap items-center gap-3">
          <Link
            href="/app"
            className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-foreground transition-all hover:opacity-90 active:scale-[0.98]"
          >
            Open the app
          </Link>
          <a
            href="#how-to-use"
            className="rounded-lg px-4 py-2 text-sm font-medium text-foreground-muted transition-colors hover:text-foreground"
          >
            How to use it
          </a>
        </div>
      </section>

      {/* 2. How it works */}
      <section id="how-it-works">
        <p className={eyebrow}>How it works</p>
        <h2 className="mb-6 text-2xl font-semibold tracking-tight">
          What happens during a run
        </h2>
        <ol className="flex max-w-2xl flex-col gap-5">
          {STEPS.map((step, i) => (
            <li key={step.title} className="flex gap-4">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-surface-muted font-mono text-xs font-medium tabular-nums text-foreground">
                {i + 1}
              </span>
              <div>
                <p className="text-sm font-semibold text-foreground">
                  {step.title}
                </p>
                <p className="mt-1 text-sm leading-relaxed text-foreground-muted">
                  {step.body}
                </p>
              </div>
            </li>
          ))}
        </ol>
      </section>

      {/* 3. How to use it */}
      <section id="how-to-use">
        <p className={eyebrow}>How to use it</p>
        <h2 className="mb-6 text-2xl font-semibold tracking-tight">
          Using Confide
        </h2>
        <div className="grid gap-4 md:grid-cols-2">
          <UsageCard
            title="Start a run"
            items={[
              "Open the app and fill in four things: the path to your project's code, the library's name, the version you are moving from, and the version you are moving to.",
              "The path is a folder on the machine that runs Confide, not on your own computer.",
              "If you use the same project often, save it on the Saved repos page. Then you can pick it from a list instead of typing the path each time.",
              "Press Start migration run. Confide opens the run's page and starts working.",
            ]}
          />
          <UsageCard
            title="Follow its progress"
            items={[
              "Every run appears in the Runs list, newest first, with its overall status.",
              "Open a run to see each place Confide found, one row per file and line. While the run is working, the page updates on its own. You don't need to refresh.",
              "Confidence is a score from 0 to 1 for how sure Confide is that it found and fixed the right thing. It goes up when a fix passes its tests.",
              "If a run finishes with no rows, either your code doesn't use anything that changed, or Confide has no recorded changes for that library and version range.",
            ]}
          />
          <UsageCard
            title="Handle the Review Queue"
            items={[
              "The Review Queue lists every fix waiting for a person. Each item shows the old code next to the proposed fix, and why it was sent to you.",
              "Approve it if the fix is correct.",
              "Reject it if the fix is wrong.",
              "Modify it to write the fix yourself. Confide runs your version through the same tests before accepting it.",
            ]}
          />
          <div className={card}>
            <h3 className="mb-3 text-base font-semibold tracking-tight">
              What the status labels mean
            </h3>
            <dl className="flex flex-col gap-3 text-sm">
              {STATUSES.map((s) => (
                <div key={s.statuses.join()} className="flex flex-col gap-1.5">
                  <dt className="flex flex-wrap gap-1.5">
                    {s.statuses.map((status) => (
                      <StatusBadge key={status} status={status} />
                    ))}
                  </dt>
                  <dd className="text-foreground-muted">{s.meaning}</dd>
                </div>
              ))}
            </dl>
          </div>
        </div>
      </section>

      {/* 4. Into the tool */}
      <section className={`${card} flex flex-wrap items-center justify-between gap-4`}>
        <div>
          <p className="text-base font-semibold tracking-tight">Try it</p>
          <p className="mt-1 text-sm text-foreground-muted">
            Start a run, or open one that is already there.
          </p>
        </div>
        <Link
          href="/app"
          className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-foreground transition-all hover:opacity-90 active:scale-[0.98]"
        >
          Open the app
        </Link>
      </section>

      <footer className="border-t border-border pt-6 text-xs text-foreground-muted">
        <p>
          This is a working demo running on a single machine. It is available
          only while that machine is on.
        </p>
        <p className="mt-1">
          Source code:{" "}
          <a
            href="https://github.com/GarvBardia/api-migration-agent"
            className="underline underline-offset-2 hover:text-foreground"
          >
            github.com/GarvBardia/api-migration-agent
          </a>
        </p>
      </footer>
    </div>
  );
}
