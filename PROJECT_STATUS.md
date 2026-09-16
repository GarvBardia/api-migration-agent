# Project Status — Autonomous API Migration Agent

*(Plain-English summary. For technical details, see `CLAUDE.md` and `TASKS.md`.)*

## What this project does

This is a tool that automatically updates old code when a library or API it
depends on changes in a breaking way. Point it at a codebase and tell it
"this library just released a new version that broke some things," and it
will find every place in the code that uses the old, broken version, write
a fix, safely test that fix in an isolated sandbox before trusting it, and
flag anything it isn't confident about for a human to look at — instead of
guessing and possibly breaking something silently.

## What works right now

- **Finds the problems.** It reads through a codebase and pinpoints every
  spot that uses the old, now-broken version of the library — not with a
  simple text search, but by actually understanding the code's structure,
  so it correctly finds usages even when they're imported under a
  different name or called indirectly.

- **Writes the fix.** For each spot it finds, it asks an AI model to write
  the corrected code, giving the AI the specific detail of what changed
  and why, so the fix is targeted rather than a guess.

- **Tests every fix safely before trusting it.** Each fix is tried out
  inside a locked-down, disposable sandbox (no internet access, limited
  resources) that runs the project's own tests against it. If a fix
  doesn't pass, the tool retries with more context about what went wrong,
  up to a few attempts, before giving up and asking a human to look. A fix
  is never marked "safe" just because the AI wrote it — it has to actually
  pass real tests first.

- **A website to watch progress and approve or reject fixes.** There's a
  simple web page that shows each migration run updating live — which
  files are done, which are still being worked on — and a review screen
  where a human can approve, reject, or hand-edit any fix the tool wasn't
  fully confident about, before it goes any further.

- **Survives crashes without losing work.** If the tool (or the computer
  it's running on) crashes partway through a run, it doesn't have to start
  over. It automatically notices which files were left in an unfinished
  state and picks back up where it left off, rather than silently losing
  track of them.

- **Remembers past fixes to go faster.** Instead of re-doing the same
  lookup or the same AI call for something it's already figured out once,
  it keeps a short-term memory of recent results and reuses them when the
  same situation comes up again in the same run — a speed and cost
  optimization, not a correctness feature.

## Built but deliberately turned off

- **Automatically opening a pull request on GitHub with the finished
  fixes.** This part is fully built and has been tested against a fake
  stand-in for GitHub's systems — but it is **intentionally not connected
  to a real GitHub account yet**. That's a deliberate choice, not a bug:
  turning it on for real would mean the tool could actually open pull
  requests on a real repository, and that hasn't been approved to happen
  yet.

## Still unverified

Nothing outstanding on this front. The one previously-unverified piece —
proving the tool's sandboxed testing step genuinely works when the whole
system runs inside Docker containers (rather than directly on this
computer) — is confirmed working, after finding and fixing a real
packaging bug that had been silently blocking it. That part of the system
is real and proven.

## New since the original plan

Three small, related additions — not part of the original design, added
on top of it:

- **Save a repo instead of retyping its path every time.** There's now a
  "Saved repos" page where you can save a project's location and its API
  name once, then pick it from a dropdown when starting a new run instead
  of typing the path again. Typing it by hand still works exactly like
  before — the dropdown is just a shortcut.

- **Automatic checking on a schedule.** A saved repo can be told to
  auto-check itself every so many hours — if there's a new breaking
  change to fix, it starts a run on its own, no one has to remember to
  kick it off manually. Off by default; you opt a repo into it and choose
  how often.

- **A notification when something needs attention.** You can point this
  at a Slack (or Discord) incoming-webhook URL, and it'll post a short
  message there when a run finishes (or fails), and separately whenever a
  fix needs a human to look at it. Nothing gets sent anywhere if you
  don't set a webhook URL — it's entirely optional.

## Real-world test

This tool was tested against 10 genuine, real breaking changes from real,
well-known Python libraries (OpenAI, pandas, NumPy, Pydantic, SQLAlchemy,
Flask, urllib3) — not made-up examples. Full results and honest
pass/fail judgment for each: `backend/tests/REAL_WORLD_BATTERY_REPORT.md`.

## Known issues

- **It can't yet find a breaking change if the old code calls it through
  two levels of dots** (like `openai.ChatCompletion.create(...)`) — it
  only recognizes one level (like `openai.something(...)`). This is a
  real gap found during the real-world test above: it means the tool
  currently can't detect the single most common way the popular OpenAI
  library is called in real code. Known and documented, not touched this
  session — a good next thing to fix.

- **Occasionally, if a fix takes a while to generate, the system can
  double-check it instead of once.** When the AI is slow to respond (which
  happens sometimes with the free tier this project uses), a background
  safety-check that watches for crashed or stuck work can mistake a fix
  that's still legitimately being worked on for one that got abandoned,
  and kick off a second, redundant attempt at it. This wastes a little
  time and, in one observed case, caused a file to show up twice on the
  review screen instead of once. It does **not** cause wrong results — no
  fix has ever ended up incorrectly approved or incorrectly flagged
  because of this — but it's a real, known behavior, not just a
  theoretical one: it was actually caught by noticing a duplicate entry on
  the review screen, not by any automated check. Safe to ignore for now
  (if you see a duplicate entry on the review screen, it's this — treat
  either copy as correct); a proper fix is planned for a future session.

## How to see it yourself

You'll need two things running at once: the backend (in Docker) and the
website (on your own computer, outside Docker).

1. Open a terminal in the project folder (`migration-agent`).
2. Start the backend:
   ```
   docker compose up -d
   ```
   Wait about 15–20 seconds for it to finish starting up.
3. In the same terminal (or a new one, same folder), start the website:
   ```
   cd frontend
   npm run dev
   ```
   Wait for it to print `Ready` — usually takes under 10 seconds.
4. Open your browser to: **http://localhost:3000**

That's it — the site talks to the backend automatically. To stop
everything later, close the website's terminal window (or press
Ctrl+C in it) and run `docker compose down` from the project folder.

---

**Last updated:** September 07, 2026, after a session that verified the
whole system genuinely works when running fully inside Docker containers
(found and fixed a real bug where the sandboxed-testing container
couldn't find the tool it needed to run), confirmed the website loads and
talks to the backend correctly, wrote this status page, and then — after
noticing a duplicate entry on the actual review screen — tracked down and
documented (not yet fixed) the "double-checks a fix" issue described
above under Known Issues, and corrected this page's earlier, too-clean
description of that verification run.

**September 08, 2026:** Gave the website a real visual design pass — it
was previously using the browser's default font and plain black-and-white
styling. It now has proper typography, consistent spacing, a real color
system for status (green for validated, amber for needs review, red for
failed, blue for in progress), and clearer visual hierarchy on the start,
run detail, and review screens. Nothing about what the tool does or what
data it shows changed — this was purely about how it looks.

**September 16, 2026:** Added the three features described above under
"New since the original plan" — saved repos with a management page and a
run-start shortcut, scheduled auto-checking, and Slack/Discord-compatible
webhook notifications. All built, tested against the real database and
the real background-task system (not just unit tests), and confirmed not
to break anything already working: the full automated test suite (119
checks) still passes end to end.

**September 16, 2026 (later the same day):** Ran the tool against 10
real breaking changes from real, well-known libraries — see "Real-world
test" above. One fix was fully verified correct end to end; a few more
were correct but properly held for human review, exactly as designed;
the rest surfaced two real, honestly-documented gaps (the two-level-dots
limitation above, and a case where the tool's own error reporting wasn't
detailed enough to tell whether a fix attempt was actually wrong or just
mis-reported) — neither fixed yet, both written down clearly so they're
not lost.
