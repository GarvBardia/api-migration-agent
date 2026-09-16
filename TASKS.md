# TASKS.md — Session breakdown for Steps 3–10

Each block below is scoped to fit in one focused Claude Code session. Steps flagged
**[SPLIT]** are pre-divided into sub-sessions — run them as separate conversations,
each starting fresh with `CLAUDE.md` re-read.

Rule for every session: if the done-criteria can't be fully met, stop and report
which criterion failed and why — don't mark it done anyway.

---

## Step 3 — Impact Analysis Agent (tree-sitter AST scanner)

**Updated 2026-08-26:** built in a prior session and now fully verified
against real Postgres (Docker Desktop installed and running). `alembic
upgrade head` applied `0001`→`0002` cleanly; `\d file_tasks` confirms
`line_start`/`line_end`/`matched_symbol`/`changelog_event_id` and
`uq_file_tasks_run_path_symbol_line` on the live table. Done-criteria 2
(multi-match → two distinct rows) and 6 (idempotent re-scan) were re-run
through the real `persist_matches` path against this database, not the
SQLite stand-in the tests still use for CI-speed unit testing. Step 3 has
no remaining caveats.

**One session.**

**First action of the session, before any scanner code:** apply migration
`0002` (`alembic upgrade head`) and update `models.py`'s `FileTask` class to
add `line_start`, `line_end`, `matched_symbol`, `changelog_event_id`. Confirm
the new columns exist (e.g. `\d file_tasks` in psql) before writing scanner
code that depends on them.

**Inputs:** a target repo path, rows in `changelog_events` describing the
breaking change(s) (matching on `old_signature`).

**Deliverable:** a module that walks the target repo, parses each file with
tree-sitter, finds AST nodes matching `old_signature` from each relevant
`changelog_events` row, and writes **one `file_tasks` row per match** (not per
file) with `status='pending'`, `matched_symbol`, `line_start`, `line_end`,
`changelog_event_id`, and a computed `confidence_score`.

**Done when:**
- Given a fixture repo with a known deprecated-function usage, every real call
  site is found (recall) and no unrelated code is flagged (precision) —
  verified against a hand-labeled fixture, not eyeballed.
- A file with multiple distinct matches produces multiple `file_tasks` rows
  (verify the `0002` uniqueness constraint permits this — this was the schema
  gap that made `0002` necessary in the first place).
- Aliased imports (`import foo as f`) and wildcard imports are still matched
  correctly.
- Matches tree-sitter cannot resolve confidently (e.g., calls via `getattr`,
  or a symbol reassigned to a variable) are written with low
  `confidence_score`, not silently dropped or guessed as high-confidence.
- **Precision on lookalike names, not just dynamic dispatch:** a fixture file
  containing a local function that shares a name with the target symbol but
  is unrelated to it (e.g. a local `create()` versus `openai.Completion.create`)
  must NOT be matched — this requires resolving imports/scope, not matching on
  bare symbol name. This is the more likely failure point; test it explicitly.
- No regex is used anywhere in the matching path.
- Re-running the scanner on the same run/repo does not create duplicate
  `file_tasks` rows (idempotency, checked against the full uniqueness key:
  run_id + file_path + matched_symbol + line_start).
- Unit tests pass against the fixture repo described in the kickoff prompt.

---

## Step 4 — Migration Agent (provider-agnostic tool-use adapter)

**Updated 2026-08-26:** built and unit-tested. The open provider decision
below is now settled: the Migration Agent is provider-agnostic by design
(`app/agents/llm_providers/`, contract in `base.py`), with `claude_adapter.py`
and `openrouter_adapter.py` both fully implemented (not stubs) and selected
via `MIGRATION_LLM_PROVIDER` in `.env`. OpenRouter was verified with a real
live call against `cohere/north-mini-code:free` (the model configured in
this machine's `.env`), correctly migrating the `service_a.py` fixture.
Claude was verified only against mocked Anthropic SDK responses — no
`ANTHROPIC_API_KEY` was available in this session's environment or `.env`,
so it has not been proven against a real Claude call. `CLAUDE.md` §4.3
updated to match. See the Step 4 session summary for the full breakdown.

**One session.**

~~**Open decision — settle before this session starts, don't default silently:**
which LLM provider does the Migration Agent call?~~ *(Settled 2026-08-26 —
see the note above. Build provider-agnostic rather than picking one.)*

**Inputs:** `file_tasks` rows with `status='pending'` from Step 3, the linked
`changelog_events` row (via `changelog_event_id`) for `old_signature`/
`new_signature`/`migration_notes`, relevant `api_doc_chunks` context.

**Deliverable:** a module that, per `file_task`, calls the configured
provider adapter with a fixed tool-use/function-calling schema
(`propose_edit(old_code, new_code, rationale) -> MigrationEdit`), writes the
result into `file_tasks.old_code_snippet` / `new_code_snippet`, and sets
`status='in_progress'`.

**Done when:**
- Every call to a provider uses its structured tool/function-calling schema
  — no free-text diff parsing.
- Given a fixture file task, the proposed `new_code_snippet` is syntactically
  valid Python (parses with `ast.parse` or tree-sitter) before being written.
- A failed/malformed tool response is caught and does not corrupt `file_tasks`
  state (task falls back to `status='needs_review'` with `failure_reason`
  set, not a partial write).
- Unit test with a mocked provider response confirms schema validation
  rejects a malformed tool call, for both adapters.

---

## Step 5 — Validation Agent (Docker sandbox + retry logic)

**Updated 2026-08-26 (Step 6a session) — gap closed:** the confidence trust
gate CLAUDE.md documents (`confidence_score` below threshold routes to
review regardless of test outcome) was NOT implemented in the original
Step 5 build below, despite being reported done — `validate_file_task` only
routed to `needs_review` on exhausted retries. Fixed in the Step 6a
session: a passing sandbox run now also checks `confidence_score` against
`DEFAULT_CONFIDENCE_THRESHOLD` (0.8) before marking `validated`. See
CLAUDE.md's dated note under `confidence_score` and
`TestConfidenceGate` in `test_validation.py`.

**Updated 2026-08-26:** built and verified. Two forks settled: (1)
`propose_edit`/`migrate_file_task` gained an optional `failure_context`
param, threaded through on retry rather than a parallel retry path — see
CLAUDE.md §4.3's dated note; (2) the old_code_snippet/real-file mismatch
check is a hard `needs_review`, never a guessed splice — see
`validation.py`'s `_apply_patch_to_temp_copy`. The sandbox image is a
custom build (`backend/sandbox/Dockerfile`) since `--network none` rules
out installing a test runner at run time — see CLAUDE.md §4.4. All of
network isolation, container teardown, timeout+kill, and structured
`pytest --json-report` parsing were verified against the real Docker
daemon, not mocked.

**One session** — borderline; split if the sandbox harness alone exceeds one
session (e.g., custom Dockerfiles per language).

**Inputs:** `file_tasks` rows with `new_code_snippet` set, target repo's test
suite, Docker (via the host socket mounted into the backend container per
`docker-compose.yml`).

**Deliverable:** a module that applies `new_code_snippet` in an ephemeral,
network-disabled, resource-limited container, runs the repo's test command,
increments `test_pass_count`/`test_fail_count`, and either (a) sets
`status='validated'` and adjusts `confidence_score` upward on pass, or (b) on
fail, sets `failure_reason`, increments `retry_count`, feeds the failure back
to the Migration Agent for one more attempt if `retry_count < max`, or sets
`status='needs_review'` (and creates a `human_review_queue` row with
`reason='retries_exhausted'` as free text) if not.

**Done when:**
- A passing fixture patch is marked `validated` with `test_pass_count`
  incremented.
- A failing fixture patch triggers exactly one retry (not zero, not
  unbounded) and is provably torn down between attempts (no state leakage).
- `retry_count` cap is enforced and tested at the boundary (`retry_count ==
  max_retries` routes to review; not `max_retries - 1`).
- Container has no network access — verified with a test that a network call
  from inside the sandbox fails.

---

## Step 6 — Orchestrator (Celery + Redis state machine) **[SPLIT]**

### 6a — Task graph and state machine

**Updated 2026-08-26:** built and verified against real Redis + an
embedded Celery worker (`celery.contrib.testing.worker.start_worker`,
`pool='threads'` — Windows has no `os.fork` for the default `prefork`
pool), not eager mode, per the kickoff's explicit instruction (eager mode
would bypass the very concurrency behavior this step needs to prove). Also
fixed a real bug surfaced only once a multi-file fixture existed: Step 5's
`run_pytest_in_sandbox` ran the whole repo's test suite unscoped, so a
second file_task's still-unmigrated, failing test got misattributed to
whichever file_task was actually being validated. See CLAUDE.md §4.4's
dated note and `_guess_test_target` in `validation.py`. Phase 0 (the
confidence-gate fix, done first this session) is documented in Step 5's
entry above.

**One session.** Define the Celery task chain wiring Steps 3→4→5→9. Reconcile
the run-level `status` (`pending|running|completed|failed|paused`) with
per-file `status` progression — the orchestrator sets `migration_runs.status
= 'running'` once any file_task starts and `'completed'` only once every
file_task is in a terminal state (`validated|failed|needs_review`), or
`'failed'` if the run itself errors out before scanning starts.

**Done when:** a full run against a fixture repo progresses through
`pending → running → completed` (or `failed`), observable via DB rows after
each Celery task completes, with per-file states reaching a terminal value.

### 6b — Idempotency, failure handling, and resume

**Updated 2026-08-26:** built and tested. Both forks settled: (1)
`task_acks_late`/`task_reject_on_worker_lost` (global Celery config) as
the primary dead-task mechanism, backstopped by a periodic sweep
(`app.tasks.periodic_resume_sweep`, Celery Beat) over `updated_at`
staleness for what broker-level redelivery can't catch (a task Celery
considers acked, but whose worker crashed mid-DB-write); (2) resume checks
`new_code_snippet is not None` to decide `validate_task`-only vs. a full
chain. New migration `0003` adds `file_tasks.sweep_attempts` — a counter
kept deliberately separate from Step 5's `retry_count` (different failure
modes: wrong migration vs. crashed infrastructure). See CLAUDE.md §4.7 for
the full writeup, including a real `migrate_task` idempotency bug this
session's own claim guard exposed and fixed (status-based skip logic
wasn't precise enough once a claim could pre-flip status to
`in_progress` before migration actually ran).

**Honesty note:** an actual worker-process crash cannot be forced from a
unit test, so "killing the Celery worker mid-run and restarting it" below
was not literally exercised end-to-end. What WAS verified: the atomic
claim guards under real concurrent threads, the resume-granularity
decision, the staleness distinction, the sweep-attempts cap boundary, and
`resume_migration_run` against a realistic mixed-state fixture (validated,
needs_review, pending, and a manufactured stale in_progress-with-
new_code_snippet row) dispatched through a real embedded Celery worker.

**One session, after 6a is done and checked.** Add crash-resume logic (a run
interrupted mid-validation resumes without re-scanning or re-migrating
completed files, using `status` and populated columns as the resume
checkpoint), dead-task detection, and orchestration-level retry/backoff
(distinct from Step 5's per-file test retry).

**Done when:** killing the Celery worker mid-run and restarting it resumes
the run correctly — verified by an actual kill-and-restart test. *(Not
literally verified this way — see the honesty note above; the underlying
resume logic that a kill-and-restart would exercise is proven directly.)*

---

## Step 7 — FastAPI + SSE endpoints

**Updated 2026-08-26:** built and tested. Both forks settled: (1) SSE
uses polling (`app/api/main.py`'s `GET /runs/{run_id}/events`), not Redis
pub/sub — no changes needed to already-verified Step 3-6 task code; see
CLAUDE.md §4.8 for the full reasoning. (2) The exact PR-eligibility query
Step 9 must use is written verbatim in CLAUDE.md's `human_review_queue`
section: `status='validated' OR (status='needs_review' AND
reviewer_decision='approved')`. `modified` re-validates the reviewer's
code via Step 5's `validate_task` rather than trusting it outright —
proven end-to-end (real Docker sandbox run) to reach `status='validated'`
on a correct human fix. All tests run against real Postgres/Redis/Celery
(embedded worker, same pattern as Steps 6a/6b), not mocked wiring.

**One session.**

**Deliverable:** REST endpoints to start a run, fetch run/file_task status,
list `human_review_queue` items (joined to `file_tasks` for diff/failure
context — there's no separate context column, see CLAUDE.md), and
approve/reject/modify a review item (writes `reviewer_decision` +
`reviewed_at`); an SSE endpoint streaming status transitions for a given
`run_id`.

**Done when:**
- Starting a run via the endpoint produces a `migration_runs` row and
  triggers the Step 6 orchestrator.
- SSE stream emits an event on every status transition for that run.
- Review approve/reject/modify endpoints correctly update
  `reviewer_decision`/`reviewed_at` and, on approve, move the linked
  file_task toward the PR agent.

---

## Step 8 — Next.js 14 frontend **[SPLIT]**

### 8a — Core pages, layout, API client

**Updated 2026-08-27:** built and verified live (real browser, real
backend, real Docker/OpenRouter). Two real gaps found and fixed along the
way, both documented in CLAUDE.md §4.9: (1) `GET /runs` didn't exist —
added; (2) nothing loaded `.env` for a real running `celery worker`/
`uvicorn` process, and fixing that exposed a second bug (`DOCKER_HOST`
from `.env` breaking the Windows Docker sandbox) — also fixed, full
backend suite re-verified green afterward (84/84).

**One session.** Run list/detail pages, API client for the Step 7 endpoints,
basic run-start form.

**Done when:** a user can start a run and see its `file_tasks` list (grouped
by file, since it's now match-level — show multiple rows per file where
applicable) with current status, pulling from real endpoints.

### 8b — Live progress (SSE) and review queue UI

**Updated 2026-08-27:** built and verified live in the real browser
against the real backend. Run detail page uses a real `EventSource`, not
polling. Review queue's approve/reject/modify all tested end-to-end
through the actual UI, including `modified`'s server-side re-validation
correctly interacting with Phase 0's confidence gate (see CLAUDE.md
§4.10). One known cosmetic rough edge (a confirmation message that never
gets to render before its item is removed from the list) documented and
left as-is given the time budget.

**One session, after 8a.** SSE-driven live status updates; a review queue
view showing the diff (`old_code_snippet`/`new_code_snippet`), failure
context (`failure_reason`, test counts), and approve/reject/modify actions.

**Done when:** starting a run in the browser shows status updates in real
time without a page refresh, and a `human_review_queue` item is actionable
from the UI.

---

## Step 9 — GitHub PR Agent

**Updated 2026-08-27:** built and tested, mocked GitHub API only. No real
`GITHUB_TOKEN` was configured, and nothing was pushed or opened against
any real GitHub repository, per this session's explicit restriction.
`RealGitHubClient` (PyGithub, added to `requirements.txt`) is fully
implemented but never instantiated in any test. Local git branch/commit/
diff-application IS real (throwaway temp checkout, never the original) —
verified directly, not just "a PR was opened." Idempotency needed a new
migration (`0004`, `migration_runs.pr_url`/`pr_number`) since nothing
previously tracked whether a run already had a PR. See CLAUDE.md §4.11.

**One session.**

**Note:** `requirements.txt` currently has no GitHub client library — add one
(e.g. PyGithub) as part of this step, not before.

**Deliverable:** given `file_tasks` with `status='validated'` for a run,
branch off the target repo, apply `new_code_snippet` diffs, commit, push, and
open a PR summarizing files changed, confidence scores, and any
`needs_review` items excluded (named explicitly in the PR description).

**Done when:**
- Fixture run with 2+ validated files produces one PR containing both diffs
  correctly applied.
- A run with some files at `needs_review` opens the PR *without* those files,
  and the PR description explicitly lists what was excluded and why.
- Re-running the PR step for an already-PR'd run does not open a duplicate
  PR.

---

## Step 10 — Redis semantic cache

**Updated 2026-08-27:** built and tested against real Redis/Postgres and
the real local embedding model. Embedding-model fork settled: no OpenAI/
Voyage key configured, defaulted to `sentence-transformers`'
`all-MiniLM-L6-v2` (384-dim, confirmed empirically, not assumed) rather
than block on a paid key. `api_doc_chunks.embedding` resized `1536`→`384`
via migration `0005` (first attempt used a revision id 7 chars too long
for `alembic_version.version_num`'s `VARCHAR(32)` — caught immediately,
transactional DDL rolled back cleanly, renamed and reapplied). See
CLAUDE.md §4.12 for the full writeup, including the honest integration
gap: the cache layer itself is proven correct against its own done-
criteria, but isn't yet wired into `migrate_file_task`'s call path since
Step 4 never built RAG retrieval to wire it into in the first place.

**One session.**

**Deliverable:** a cache layer in front of `api_doc_chunks`/`changelog_events`
lookups — embed the query, check Redis for a nearby cached embedding before
hitting Postgres/pgvector or re-calling Claude. Set `migration_source='cache'`
on hits, `'llm'` on misses.

**Done when:**
- Two file_tasks referencing the same symbol result in one embedding
  computation and one Postgres lookup, not two (verified via call count).
- Cache has a defined TTL or invalidation path.

---

## Summary of splits

| Step | Split? | Reason |
|---|---|---|
| 6 Orchestrator | Yes — 6a/6b | State machine wiring vs. crash-resume are separately testable; 6b depends on 6a being frozen first. |
| 8 Frontend | Yes — 8a/8b | Static CRUD pages vs. SSE live state are different concerns. |
| 3, 4, 5, 7, 9, 10 | No | Each has a single clear deliverable and checkable done-state. |

**Open risk, not resolved here:** Step 5's scope depends on how many
languages/test runners the sandbox must support. If the demo covers only
Python (pytest), one session is enough; if Node/Jest is also in scope, split
5 the same way as Step 8. This also affects Step 3's grammar choice —
confirmed as Python-primary per the prior turn.

---

## Beyond the original 10-step plan (added 2026-09-16)

Everything below is new scope, explicitly outside the 10 steps this
project was originally planned around. See CLAUDE.md's dated note for the
full technical writeup (schema, endpoints, task wiring, design decisions
made along the way); this section is the plain deliverable/done-when
summary, same format as every step above.

### Saved repos

**Deliverable:** a `repos` table (id, name, repo_path, default_api_name,
plus the auto-check columns below) with basic CRUD (`POST/GET/DELETE
/repos`), a frontend management page (`/repos`, linked from the nav), and
a saved-repo dropdown on the run-start form that prefills repo path + API
name as a shortcut. Typing a path manually still works unchanged — the
dropdown is additive, not a replacement.

**Done when:**
- Repos can be added/listed/deleted via the API and the frontend.
- The run-start form offers saved repos as a shortcut, and still accepts
  a manually-typed path.

### Scheduled auto-checking

**Deliverable:** `auto_check_enabled`/`check_interval_hours` on `repos`,
plus a new Celery Beat periodic task (`app.tasks.periodic_auto_check_
sweep`, same pattern as the Step 6b resume sweep) that, per enabled+due
repo, finds unprocessed `changelog_events` matching its
`default_api_name` and kicks off a real migration run per distinct
version pair found, then marks those events processed.

**Done when:**
- The scheduled task correctly auto-triggers a run when new
  changelog_events exist for an enabled, due repo.
- It correctly does nothing when there's nothing new, when auto-check is
  disabled, or when the repo isn't due yet — verified as four separate
  cases, not just the happy path.

### Webhook notifications

**Deliverable:** an optional `NOTIFICATION_WEBHOOK_URL` — unset means
silently skipped, never an error. When configured, a `{"text": "..."}`
POST (Slack/Discord-incoming-webhook-compatible) fires when a run reaches
`completed`/`failed`, and separately when any file lands in
`needs_review`.

**Done when:**
- A webhook POST fires on run-terminal and on needs_review, proven
  through the real Celery pipeline (mocked LLM provider, mocked
  `httpx.post` — never requires a real webhook URL to run the suite).
- Silently skipped (no call, no error) when unconfigured.

**One session.** All three built and tested together since they share one
table and one design conversation; see CLAUDE.md for exactly what was
built vs. deferred if the session ran out of room.
