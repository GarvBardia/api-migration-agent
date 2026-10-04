# Project Status: Confide

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

## Public demo (live while this computer is running it)

**Live at: https://garvbardia.github.io/api-migration-agent/**

That address opens the Confide landing page, which explains what the tool
does and how to use it. The tool itself is one click in, at
https://garvbardia.github.io/api-migration-agent/app/. The landing page
works even when this computer is off. The tool does not.

Anyone with that link can open the website from any device (phone, other
wifi, anywhere) and use it for real — start a migration, watch it run
live, review fixes. It is a real, working public demo. It is **not** a
24/7 hosted service: the website itself is on GitHub and is always
reachable, but everything that does the actual work (the backend, the AI
calls, the safe-testing sandbox) runs on this one computer and is reached
through a temporary tunnel. So it only *works* while all three of these are
true at once:

1. this computer is on and awake,
2. Docker Desktop is running (`docker compose up -d` — the same 5 services
   as always), and
3. the tunnel is running (see step 2 below).

If any of those stops, the site still loads but shows an error where data
should be — nothing is broken permanently, it just needs restarting.

**How it's wired:** GitHub Pages (free static website) → Cloudflare Tunnel
(free, no account) → this computer's backend on port 8000. The website's
code lives in the `gh-pages` branch of
https://github.com/GarvBardia/api-migration-agent (regenerated by a script,
never edited by hand).

### Restarting it (exact steps)

1. **Start the backend.** Open Docker Desktop, wait until it says running,
   then in a terminal in the project folder: `docker compose up -d`.
2. **Start the tunnel** — from the *project folder* (not from inside any
   subfolder — a tunnel started inside `frontend/out` locks that folder and
   breaks the next build):
   ```
   "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000
   ```
   After a few seconds it prints a line like
   `https://some-random-words.trycloudflare.com`. Copy that address. Leave
   this window open — closing it stops the tunnel.
3. **Point the website at the new tunnel address** (this is required every
   time the tunnel is restarted — see the warning below). In Git Bash, from
   the project folder:
   ```
   bash deploy/deploy_pages.sh https://some-random-words.trycloudflare.com
   ```
   It rebuilds the website with that address and publishes it (about 2
   minutes). GitHub then takes ~30 seconds to go live. Done.

**Important — the tunnel address changes every restart.** The free "quick"
tunnel gets a brand-new random address every time it starts, and the
website has the address built into it. So *every* tunnel restart means
re-running step 3, or the public site will point at a dead address. (If
only Docker restarts but the tunnel window stayed open, nothing needs
redoing.) Also: on this computer, a brand-new tunnel address can take a few
minutes before *this computer's* internet lookup finds it, even though it
already works for everyone else — if it "doesn't load" right after a
restart, wait a couple of minutes before assuming something is wrong.

**Avoiding the changing address later (not done, optional):** a *named*
Cloudflare tunnel gives a permanent address, but it needs a domain name you
own added to a free Cloudflare account (a domain costs roughly $10/year — so
it's not fully free). A free alternative is ngrok, which offers one free
permanent address per account (needs a free ngrok account and a one-time
setup). Either would remove step 3 from every restart.

### Honest limits of this demo

- **Anyone with the link can use it — there is no login.** That means
  anyone who has the address can start migrations (which use the free AI
  quota and run tests on this computer) and approve or reject fixes. It's
  fine as a demo shared with people you trust; don't post the link publicly
  or leave it running unattended. Adding a login is the natural next step
  before treating this as anything more than a demo.
- **Speed and reliability depend on this computer and its internet
  connection**, and on the free AI tier, which is sometimes slow.
- The tunnel is Cloudflare's free no-account "quick tunnel", which has no
  uptime promise.

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
4. Open your browser to: **http://localhost:3000**. That's the landing page.
   The tool itself is at **http://localhost:3000/app**.

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

**September 20, 2026:** Put the website on the public internet at
https://garvbardia.github.io/api-migration-agent/ — see "Public demo" above
for how it works, its limits, and the exact restart steps. Verified for real
from the live site (not just locally): the page loads, talks to this
computer's backend through the tunnel, and a full test migration started
from the public page ran all the way through the safe-testing sandbox and
finished with the expected result. One change to how the site is built: the
page for viewing a single run now lives at `run/?id=…` instead of
`runs/<id>`, because a fully static website can't pre-build a page for
every future run.

**September 30, 2026:** The product is now called Confide. The name changed
everywhere a person sees it: the website header, the browser tab, the page
descriptions, and a new README on the GitHub page. The GitHub repository,
code folders, and internal names were left as they were on purpose, so the
public link keeps working and the project history stays intact. The website
also has a new front page that explains in plain words what Confide does,
how a run works step by step, how to use the tool, and what each status
label means. The tool itself moved one click in, to `/app`. Nothing about how
the tool works changed.

**October 4, 2026:** Locked down the public demo before sharing it wider.
Nothing is deployed yet. The live site is unchanged until you confirm.

- **The demo only reads the example folder.** Anyone who types any other
  folder (or a folder that does not exist, or a file) gets a plain message
  and no run starts. A folder that does not exist can no longer show up as a
  finished run with nothing in it.
- **Modify is switched off on the public demo.** The button is greyed out
  with a one-line explanation, and the server refuses the request too.
  Approve and reject still work.
- **Checked what happens when you Modify a low-confidence fix.** A corrected
  fix that passes its tests but started below the 0.80 confidence line ends up
  in the review list again, not marked validated. This was tested against the
  real sandbox, not just read from the code. The message shown after you press
  Modify now says exactly that.
- **The test sandbox is a little tighter.** It already had no internet and
  memory and CPU limits. It now also limits the number of processes, drops
  extra system permissions, and blocks privilege escalation. Not done yet: a
  read-only filesystem and a non-root user (both need more testing).
- **The example is permanent.** The example change record (`oldapi` 1.x to
  2.0) is recreated automatically every time the backend starts, so cleaning
  the database cannot break the demo.
- **The landing page has a "Try it with this example" block** with the exact
  folder, library name and versions to type.
- **Side effect to know about:** these limits are set in the Docker setup, so
  the Docker version you run locally also only accepts the example folder and
  has Modify off. Running the backend outside Docker is unrestricted.
- The full automated test suite passes (144 checks, 1 skipped on Windows; the
  skipped one also passes inside Docker). The first full run had one timeout
  in a pipeline test that passed alone and on a second full run.

**October 5, 2026:** Added a limit to the public demo: if 3 migrations are
already waiting or running, a 4th is refused with a plain message ("The public
demo is busy..."). It only applies to the public demo, not local use. If a run
ever gets stuck in "running", it counts toward the limit until it is cleared.
The landing page also now says Modify is switched off on the public demo.

**October 5, 2026 (later):** The hardened demo is live at
https://garvbardia.github.io/api-migration-agent/ and was checked from a real
browser: the example run finishes, a folder outside the example is refused with
a clear message, Modify is greyed out and refused by the server, and a 4th
simultaneous run is told the demo is busy. The tunnel was started to run for
about 2 hours. After that, restart it and re-run the deploy script (steps
under "Restarting it"). The review list still holds a few "service_b.py" items
from these checks. They are harmless demo data.

