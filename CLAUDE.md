# CLAUDE.md — Autonomous API Migration Agent

Read this file at the start of every session. It is the persistent memory for this
repo. If something here conflicts with what you observe in the code, the code wins —
but stop and flag the conflict before proceeding.

**Standing rule, added 2026-09-07 — applies to every future session, not just
this one:** at the end of every work session, before ending it, update
`PROJECT_STATUS.md` in the project root with what changed this session, in
plain English (no jargon, no code, no log excerpts — the founder reading it
is non-technical), dated. This is the one artifact he actually reads;
`CLAUDE.md`/`TASKS.md` are for the agent, `PROJECT_STATUS.md` is for him.
Keep it current every time, not just when something big happens.

## 1. What this system does

Given a target GitHub repo and a target API/library whose version is bumping, the
system: (1) detects breaking changes in that API's changelog/docs, (2) scans the
target codebase with tree-sitter to find every call site affected, (3) generates a
migration patch per file via a provider-agnostic LLM adapter (structured tool-use
schema; Claude and OpenRouter adapters implemented, config-selected — see §4.3),
(4) validates the patch by
running the repo's test suite inside an isolated Docker container, retrying on
failure, (5) opens a GitHub PR with the passing changes, and (6) routes anything it
isn't confident about to a human review queue instead of guessing.

**Design principle carried through every step:** silent wrongness is the failure
mode to design against, not exceptions. A crashed task is recoverable. A merged PR
with a wrong migration is not. Every ambiguous case routes to `human_review_queue`
rather than being auto-resolved by a fallback heuristic.

## 2. Architecture

```
Changelog/Docs ingestion ──▶ api_doc_chunks / changelog_events (pgvector)
                                        │
                                        ▼
Target repo ──▶ [Impact Analysis Agent: tree-sitter AST scan]
                                        │  writes file_tasks rows (status=pending)
                                        ▼
                        [Orchestrator: Celery + Redis state machine]
                                        │
                        ┌───────────────┼────────────────┐
                        ▼                                ▼
        [Migration Agent: provider-agnostic        [Redis semantic cache]
         tool-use adapter (Claude/OpenRouter)]      (dedupes doc/symbol lookups)
                        │
                        ▼
        [Validation Agent: Docker-sandboxed test run, retry loop]
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
        status=validated     status=needs_review
        (confidence high,    (confidence low, or
         tests pass)          retry_count exhausted)
              │                   │
              ▼                   ▼
     [GitHub PR Agent]     human_review_queue
              │
              ▼
     FastAPI + SSE ──▶ Next.js 14 frontend (live run status, review UI)
```

Backend: Python 3.12, FastAPI, Celery + Redis (task queue + semantic cache),
PostgreSQL 16 + pgvector (relational state + embeddings), SQLAlchemy + Alembic.
Frontend: Next.js 14, consumes FastAPI SSE stream for live progress.

## 3. Database schema — as actually built (verified against `models.py` and
`0001_initial_schema.py` + `0002_file_task_match_metadata.py`)

**This section was previously wrong in an earlier draft of this file** — it
described columns and status values that don't exist in the real schema. The
version below is checked directly against the migration files. If you find
another mismatch, stop and report it rather than coding around it.

### `migration_runs`
`id` (UUID pk), `repo_url`, `api_name`, `version_from`, `version_to`, `status`,
`created_at`, `updated_at`.

**`status`** — enum, enforced by DB check constraint `ck_migration_runs_status`:
`pending | running | completed | failed | paused`. That's it — five values, no
more. There is **no** `scanning`/`analyzing`/`migrating`/`validating`/`pr_open`
in the database. Finer-grained progress (which agent is currently working on a
run) is communicated over the SSE stream as descriptive text/events, not as
persisted `status` values. Don't add new status strings to this column without
a migration — the check constraint will reject anything not in the list.

**Why `status` exists:** the orchestrator must resume a run after a crash
without re-scanning, re-migrating, or re-opening a PR. `running` means "in
progress somewhere in the pipeline"; which stage is derived by looking at the
run's `file_tasks` (see below), not stored redundantly on the run itself.

### `file_tasks`
`id` (UUID pk), `run_id` (FK), `file_path`, `status`, `old_code_snippet`,
`new_code_snippet`, `confidence_score`, `retry_count`, `test_pass_count`,
`test_fail_count`, `failure_reason`, `migration_source`, `created_at`,
`updated_at` — plus, as of migration `0002`: `line_start`, `line_end`,
`matched_symbol`, `changelog_event_id` (FK to `changelog_events`).

**`status`** — enum, `ck_file_tasks_status`: `pending | in_progress | validated
| failed | needs_review`. Same rule as above: five values, no more. A task
moves `pending → in_progress` once any agent starts working it, and lands on
`validated`, `failed`, or `needs_review` at the end. Intermediate progress
(AST-matched but not yet migrated, migrated but not yet tested) is inferred
from which columns are populated — e.g. `new_code_snippet IS NOT NULL` means
the Migration Agent has produced a candidate; `test_pass_count +
test_fail_count > 0` means the Validation Agent has run it at least once — not
from additional status values.

**Row granularity — read this carefully:** one row is **one match** (one
call-site / symbol reference), not one row per file. A single file with three
separate deprecated calls produces three `file_tasks` rows. This only works
because of migration `0002`, which changed the uniqueness rule from "one row
per (run, file)" to "one row per (run, file, matched_symbol, line_start)" —
before `0002`, the schema physically could not hold more than one match per
file. If you're on a checkout before `0002` is applied, run it first;
everything downstream assumes match-level granularity.

- **`confidence_score`** (float, 0–1, nullable, DB-constrained to the range):
  set initially by the Impact Analysis Agent (high for a direct statically-
  resolvable call, low for anything requiring aliasing/dynamic-dispatch
  guesswork), then revised by the Validation Agent based on test outcome. This
  is the trust gate — below a configured threshold routes to
  `human_review_queue` regardless of whether tests happened to pass.

  **Updated 2026-08-26, Step 6a session (Phase 0 fix):** this gate was
  documented but not implemented — Step 5's `validate_file_task`, as
  originally built, only ever routed to `needs_review` on exhausted
  retries, never on a passing-but-still-low-confidence result. Fixed:
  `validate_file_task` now takes a `confidence_threshold` param (default
  `DEFAULT_CONFIDENCE_THRESHOLD = 0.8`, matching the "high ≥0.8" tier from
  the Step 3 kickoff prompt) and checks it after a passing sandbox run,
  before setting `status='validated'`. Below threshold routes to
  `needs_review` with a `human_review_queue` row
  (`reason='low_confidence_despite_passing_tests: score=...'`) instead.
  See `TestConfidenceGate` in `backend/tests/test_validation.py`.
- **`retry_count`** (int, ≥0 enforced): bounds the Validation Agent's
  fix-and-retest loop (default max 3, enforced in application code, not the
  DB). Hitting the cap routes to `human_review_queue`, not a silent
  best-effort merge.
- **`sweep_attempts`** (int, ≥0 enforced, added in migration `0003`, Step 6b
  session): a DELIBERATELY SEPARATE counter from `retry_count` — see the
  Step 6b architecture note below. `retry_count` is about a wrong
  migration (the fix keeps failing tests); `sweep_attempts` is about
  infrastructure failing (a worker crashing, a network blip, Docker
  hiccuping) while a `file_tasks` row sits stuck at `in_progress`. Never
  conflate the two, and never reuse one counter for the other's job.
- **`test_pass_count` / `test_fail_count`**: raw counters from the Validation
  Agent's sandbox runs — used both for the confidence revision above and as
  evidence displayed in the review queue UI.
- **`failure_reason`** (text, nullable): populated on the last failed
  attempt so a human reviewer (or a retry prompt to the Migration Agent) has
  the actual test output, not just a boolean.
- **`migration_source`** (`llm | cache`): records whether the edit came from a
  fresh Claude call or the Step 10 semantic cache — used for cache-hit
  metrics, not for pipeline logic.
- **`line_start` / `line_end` / `matched_symbol` / `changelog_event_id`**
  (added in `0002`): where the match is and what changed-symbol it's fixing.
  `changelog_event_id` is nullable with `ON DELETE SET NULL` — deleting a
  changelog event does not cascade-delete migration history.

### `human_review_queue`
`id` (UUID pk), `file_task_id` (FK), `reason` (free text — no enum
constraint, write a short human-readable string, not a fixed code),
`reviewer_decision` (`approved | rejected | modified`, nullable —
**`NULL` means still pending**, there is no separate `status` column),
`reviewed_at` (nullable), `created_at`.

There is deliberately **no `context` jsonb column here.** All the context a
reviewer needs (the diff, the failure output, the confidence score) already
lives on the linked `file_tasks` row via `file_task_id` — join to it rather
than duplicating that data into this table. Don't add a jsonb blob to store
the same information twice.

**Updated 2026-08-26, Step 7 session — the exact contract Step 9 relies on
(fork #2, decided this session, don't re-derive it, use this verbatim):**

- `reviewer_decision='approved'`: the file is PR-eligible despite not
  being auto-`validated`. **Step 9's PR-eligibility query must be:**
  ```sql
  status = 'validated'
     OR (status = 'needs_review' AND reviewer_decision = 'approved')
  ```
  (joining `file_tasks` to `human_review_queue` on `file_task_id` to get
  `reviewer_decision`). Nothing else counts as PR-eligible.
- `reviewer_decision='rejected'`: excluded from any PR, permanently, for
  this run — neither disjunct above matches a rejected row, so no special
  filtering is needed beyond the query itself.
- `reviewer_decision='modified'`: the reviewer submitted their own
  corrected code. **Decision: re-validate, don't trust outright.** The
  `POST /human-review-queue/{id}/decision` endpoint (`app/api/main.py`)
  writes the reviewer's code into `new_code_snippet`, resets
  `status='in_progress'`, and re-dispatches Step 5's `validate_task` —
  the file only becomes PR-eligible (matches the query above) once it
  re-passes validation on its own merits. Chosen over trusting the human's
  edit outright because it costs one sandbox run and catches human
  mistakes too (a "fix" that still doesn't pass tests shouldn't reach a PR
  just because a person wrote it) — the whole point of this pipeline is
  not shipping unverified migrations, and a human edit isn't exempt from
  that. Until re-validation completes, a `modified` row is NOT yet
  PR-eligible under the query above (it's back at `status='in_progress'`,
  not `'validated'` or `'needs_review'`) — Step 9 will simply not see it
  yet, which is correct.

### `api_doc_chunks`
`id`, `api_name`, `version`, `chunk_text`, `embedding` (`vector(1536)` —
sized for OpenAI `text-embedding-3-small`; resize the column if a different
embedding model/dimension is used), `created_at`. Has an IVFFlat index
(`lists=100`) — fine for a portfolio-scale doc set; `lists` should scale
toward `sqrt(n_rows)` if doc volume grows substantially.

### `changelog_events`
`id`, `api_name`, `version_from`, `version_to`, `change_type`,
`old_signature`, `new_signature`, `migration_notes` (nullable),
`processed_at` (nullable), `created_at`.

**`change_type`** — enum, `ck_changelog_events_change_type`: `signature_change
| rename | removal | deprecation | behavior_change | addition`. This is what
the Impact Analysis Agent matches AST nodes against — `old_signature` is the
thing to search for, `new_signature` is what the Migration Agent should
produce.

## 4. Non-negotiable engineering constraints

1. **Idempotency everywhere.** Every Celery task checks the relevant `status`
   field, and match-level uniqueness is enforced via
   `uq_file_tasks_run_path_symbol_line` (run_id, file_path, matched_symbol,
   line_start), before acting. No task creates duplicate rows, re-opens a PR,
   or re-calls Claude for work already marked complete.

   **Updated 2026-08-26, Step 6b session — real bug found and fixed:**
   `migrate_task` used to skip whenever `status != 'pending'`. That broke
   crash-resume (see the Step 6b fork below): the atomic claim guard for a
   stale `pending` row flips its status straight to `'in_progress'` AS the
   claim itself (the kickoff's own `UPDATE ... WHERE status='pending' ...`
   pattern) — so a resumed chain's `migrate_task` saw `'in_progress'` and
   silently skipped calling the Migration Agent, leaving `new_code_snippet`
   stuck at `NULL` forever. Fixed: `migrate_task` now skips only once
   migration is actually done — a terminal status, OR `new_code_snippet
   IS NOT NULL` (the signal §3 already documents) — not on status alone.
2. **AST-first, never regex, for code matching.** All call-site detection goes
   through tree-sitter. Regex silently mismatches on aliased imports, string
   literals, comments, and dynamic calls — it produces confident-looking wrong
   answers instead of visible failures. If tree-sitter can't resolve a match
   (e.g., dynamic dispatch), that must surface as a *low-confidence result*,
   not a regex-based guess.
3. **Structured tool calls only, for the Migration Agent — provider-agnostic
   by design.** *(Updated 2026-08-26, Step 4 session: this used to say
   "Claude's tool-use API only" — that was the open decision flagged in
   TASKS.md, now settled. See below.)* The Migration Agent is not hard-wired
   to one LLM provider. A shared contract in
   `app/agents/llm_providers/base.py` defines one function signature every
   provider adapter implements —
   `propose_edit(old_code, old_signature, new_signature, migration_notes) ->
   MigrationEdit` — where `MigrationEdit` is a pydantic model
   (`new_code: str`, `rationale: str`) with the same shape regardless of
   which provider produced it. Every adapter must return a validated
   `MigrationEdit` or raise `ProviderResponseError`; none may ever return a
   raw string blob for the caller to parse heuristically. Two adapters exist:
   - `claude_adapter.py` — Claude's native tool-use API (`tools` +
     forced `tool_choice`). The documented **production-mode default**,
     fully implemented, not used unless explicitly selected.
   - `openrouter_adapter.py` — OpenRouter's OpenAI-compatible
     chat-completions endpoint with function-calling (`tools` as
     `{"type": "function", ...}`, arguments returned as a JSON string that
     must be parsed) — a genuinely different request/response shape from
     Claude's, proving the abstraction isn't just designed for two
     providers in theory.

   Provider selection is config-driven via `.env`: `MIGRATION_LLM_PROVIDER`
   (`claude` | `openrouter`), `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` for
   Claude, `OPENROUTER_API_KEY` / `MIGRATION_LLM_MODEL` for OpenRouter.
   Switching providers is a `.env` edit, never a code change. Store the
   result in `new_code_snippet`.

   **Updated 2026-08-26, Step 5 session:** `propose_edit` (and
   `migrate_file_task`) gained an optional `failure_context: str | None`
   parameter on both adapters. The Validation Agent's retry loop calls back
   into this same function/interface with the previous attempt's failure
   detail rather than building a separate retry path — see
   `app/agents/validation.py` and `app/agents/migration.py`.
4. **Docker isolation for all test execution.** Validation runs happen in
   ephemeral, resource-limited (`--memory=512m --cpus=1`), network-disabled
   (`--network none`) containers, auto-removed (`--rm`) after each run, with
   an enforced wall-clock timeout (default 60s) that kills the container if
   it hangs. No state persists across `file_tasks` — each retry gets a fresh
   container from the same image plus a fresh patched temp copy of the repo.

   **Updated 2026-08-26, Step 5 session:** the line above used to say the
   backend container mounts the host Docker socket per `docker-compose.yml`
   — that's the *eventual* Step 7 containerized-backend setup, not what's
   actually running today. Right now the backend runs directly on the host
   via the Python venv (no `backend/Dockerfile` exists yet — a known gap
   flagged since Step 1, only relevant once Step 7 containerizes the
   backend itself). The Validation Agent therefore talks to the local
   Docker daemon directly via the `docker` CLI (subprocess), not through
   any docker-in-docker socket-mount indirection. Revisit this note when
   Step 7 actually containerizes the backend.

   The sandbox image is a custom build (`backend/sandbox/Dockerfile`, based
   on `python:3.12-slim` + `pytest` + `pytest-json-report` preinstalled),
   not the plain `SANDBOX_IMAGE` in `.env.example` — `--network none`
   means a container can't `pip install` anything at run time, so the test
   runner has to already be baked into the image. Build it once with
   `docker build -t migration-agent-sandbox:latest backend/sandbox` before
   running the Validation Agent; `.env`/`.env.example`'s `SANDBOX_IMAGE`
   now points at this tag.

   Test-result parsing uses `pytest --json-report` written to a file inside
   the container and read back after the run — a structured report, never
   regex/pattern-matching against raw pytest stdout (same principle as
   §4.2's "no regex in the matching path," applied here to test-result
   parsing).

   **Updated 2026-08-26, Step 6a session — real bug found and fixed:**
   Step 5 was verified against a repo with exactly one file/one test file,
   which hid a real gap: `run_pytest_in_sandbox` ran pytest against the
   *whole* repo copy with no target, and `_apply_patch_to_temp_copy` only
   patches the ONE file being validated — so as soon as a second,
   still-unmigrated file existed in the same repo (Step 6a's fixture), its
   still-failing tests got attributed to whichever *other* file_task
   happened to be validated at that moment. Fixed: `validate_file_task` now
   scopes each sandbox run to that file's own test file via
   `_guess_test_target` (convention: `service_a.py` -> `test_service_a.py`,
   falling back to the whole suite if no such file exists). This is a
   known simplification — it assumes a 1:1 source/test naming convention,
   which holds for this project's fixtures but not for arbitrary real repos
   (e.g. integration tests spanning multiple modules) — documented rather
   than hidden. See `test_target` param on `run_pytest_in_sandbox`.
5. **Semantic caching before repeat Claude calls.** Check the Redis semantic
   cache (embedding similarity against `api_doc_chunks`/`changelog_events`)
   before re-calling Claude for context on a symbol already looked up
   elsewhere in the run. Record the outcome via `migration_source`.
6. **Orchestrator (Step 6a, added 2026-08-26): Celery task graph, not a
   custom scheduler.** `app/celery_app.py` configures Celery against
   `REDIS_URL` for both broker and result backend (the same Redis already
   running from `docker-compose.yml` — no separate queue). `app/tasks.py`
   wires `scan_task` (Step 3) -> `dispatch_file_tasks` -> a Celery
   `group` of per-file `chain(migrate_task, validate_task)` (Steps 4-5) ->
   a `chord` callback `finalize_run` that fires once every chain in the
   group has reached a terminal `file_tasks.status`. `validate_task` does
   **not** add its own retry task — `validate_file_task`'s retry loop
   already calls back into `migrate_file_task` directly (see §4.3's
   Step 5 note), so wrapping it in another retry layer here would
   duplicate that logic. A scan error before any `file_tasks` row exists
   sets `migration_runs.status='failed'`; a scan that cleanly finds zero
   matches is a legitimate `'completed'` run with no work to do, not a
   failure — these are different outcomes, don't conflate them.

   **Local-checkout simplification:** `migration_runs.repo_url` is
   currently used as a local filesystem path to an already-checked-out
   target repo, not a remote GitHub URL — there is no clone/checkout step
   yet (that's Step 9's GitHub PR agent territory). Revisit when Step 9
   adds real repo cloning.

   **Windows note:** Celery's default `prefork` pool needs `os.fork`,
   which doesn't exist on Windows. Workers (and this session's tests, via
   `celery.contrib.testing.worker.start_worker`) use `--pool=threads`
   instead. See `app/celery_app.py`'s docstring for the local worker
   command.
7. **Crash-resume and dead-task detection (Step 6b, added 2026-08-26) —
   a DIFFERENT failure mode from Step 5's `retry_count`.** Step 5's retry
   loop handles a WRONG migration (fix keeps failing tests). This handles
   INFRASTRUCTURE failing: a worker process crashing mid-task, a network
   blip, Docker daemon hiccups. Two forks, decided and built:

   - **Fork #1 (detection):** `task_acks_late=True` +
     `task_reject_on_worker_lost=True` (global Celery config,
     `celery_app.py`) is the PRIMARY mechanism — the broker redelivers a
     task under the same id if its worker vanishes before acking, which
     still feeds correctly into any chord/group tracking it. That alone
     can't catch a task Celery considers successfully acked, but whose
     worker crashed mid-way through this app's own DB writes — leaving a
     `file_tasks` row stuck at `in_progress` with no Celery-level signal.
     Built a periodic sweep as the backstop for that gap:
     `app.tasks.periodic_resume_sweep`, scheduled via Celery Beat
     (`beat_schedule` in `celery_app.py`, default every
     `RESUME_SWEEP_INTERVAL_SECONDS=60`s), scanning ALL non-terminal
     `file_tasks` across every run.
   - **Fork #2 (resume granularity):** in this codebase's state machine, a
     `file_tasks` row stuck at `in_progress` can only have reached that
     status via `migrate_file_task`'s success
     path (see `migration.py`), which never sets `status='in_progress'`
     until `new_code_snippet` is already written. So resume checks
     `new_code_snippet is not None` to decide `validate_task`-only vs. a
     full `migrate_task`->`validate_task` chain — checking it explicitly
     (not hardcoding "always validate") keeps this correct even if that
     invariant ever changes. Resuming into `validate_task` is always safe
     against stale data: `validate_file_task` (Step 5) already re-checks
     `old_code_snippet` against the real file's current content before
     patching anything, and this module doesn't bypass or duplicate that.

   **Claim guard:** an atomic conditional `UPDATE ... WHERE id=:id AND
   status='pending'` (or, for an already-`in_progress` row, `... AND
   updated_at < :cutoff`, bumping `updated_at` forward as the claim marker
   — no new column needed for that guard) so two concurrent
   callers (two resume calls, or a resume racing a still-live worker)
   can't both act on the same row. Proven under genuine concurrent threads
   against real Postgres, not simulated sequentially — see
   `TestClaimGuards` in `backend/tests/test_resume.py`.

   **Sweep-attempts cap:** `sweep_attempts` (migration `0003`, default cap
   `MAX_SWEEP_ATTEMPTS=3`) bounds how many times the resume path will
   re-attempt a stuck `in_progress` row before giving up — at the cap, the
   `file_tasks` row is marked `status='failed'` and the PARENT RUN is also
   marked `migration_runs.status='failed'` (a run that can't make progress
   on a file after the cap is not silently left `running` forever, nor
   `completed` by an earlier chord callback that fired without knowing
   this row was actually stuck).

   **A real bug found and fixed while building this:** see item 1's dated
   note above — `migrate_task`'s idempotency check had to change from
   "status != pending" to "not terminal AND new_code_snippet is None" for
   the resume path to actually call the Migration Agent at all.

   **What's genuinely verified vs. not:** an actual worker *process*
   crash cannot be forced from a unit test. What's verified: the atomic
   claim guards (real concurrent threads), the resume-granularity
   decision, the staleness distinction (`updated_at` old vs. fresh), the
   sweep-attempts cap boundary, and `resume_migration_run` against a
   realistic mixed-state fixture dispatched through a real embedded Celery
   worker. `acks_late`/`task_reject_on_worker_lost` themselves are
   Celery/Redis's own mechanism — configured and documented, not
   re-proven from scratch.
8. **REST + SSE API (Step 7, added 2026-08-26): polling, not Redis
   pub/sub, for the SSE endpoint.** `GET /runs/{run_id}/events`
   (`app/api/main.py`) polls `migration_runs`/`file_tasks` every
   `SSE_POLL_INTERVAL_SECONDS` (1s), diffs against the last snapshot it
   actually sent, and emits an event only on a real change, closing once
   `run_status` is terminal (`completed`/`failed`).

   **Fork #1 decision, reasoning:** pub/sub (each task in `app/tasks.py`
   publishing on status change) would be more real-time, but (a) it means
   editing already-verified Step 3-6 code to add publish calls — every
   task gains a new responsibility and a new way to fail silently if a
   publish call is missed or mis-placed — and (b) it doesn't even remove
   the need for a DB read on connect anyway (a client connecting after a
   message was published would otherwise miss it entirely, so pub/sub
   still needs "read current state first, then subscribe for changes").
   For a portfolio-scale project where nothing downstream needs sub-second
   latency, polling gets the same observable behavior (a client always
   sees an accurate, eventually-current stream) for a fraction of the
   surface area and zero risk to already-verified code. Revisit only if a
   real requirement for sub-second updates or very high run concurrency
   emerges — polling every 1s against Postgres does not scale to
   thousands of concurrently-watched runs, but this project isn't at that
   scale and there's no evidence it needs to be.

   **Fork #2 (human review decision contract)** — see the `human_review_queue`
   entry in §3; the exact PR-eligibility query Step 9 must use is written
   there verbatim.
9. **Next.js frontend (Step 8a, added 2026-08-27).** `frontend/` is a
   Next.js 14 App Router project (TypeScript, Tailwind), fetching the Step
   7 API directly from the browser (`frontend/lib/api.ts` mirrors
   `backend/app/api/schemas.py` field-for-field). Client components poll
   (same polling philosophy as the backend's own SSE endpoint, see fork #1
   above) rather than using SSE yet — Step 8b upgrades the run-detail page
   to consume the real SSE stream. CORS (`localhost:3000`) added to
   `app/api/main.py`.

   **Real gap found and fixed: `GET /runs` didn't exist.** Step 7 only
   built `GET /runs/{run_id}` (one run) — the frontend's run-list page
   needs to list all runs, which no endpoint supported. Added `GET /runs`
   (`app/api/main.py`), newest first, capped at 100. Verified via
   `TestListRunsEndpoint` in `test_api.py`.

   **Real gap found and fixed: nothing loaded `.env` for a real running
   process.** Every prior step's tests read `.env` manually (via
   `dotenv_values()`) to construct things like `OpenRouterAdapter`
   directly — but actually running `celery worker`/`uvicorn` from the
   command line, as a real user would, never loaded `.env` into the
   process at all, so `MIGRATION_LLM_PROVIDER`/API keys were silently
   absent the moment this got tested for real end-to-end via the browser.
   Fixed: `load_dotenv()` added to the top of `celery_app.py` and
   `app/api/main.py`, before any other env-dependent code runs.

   **That fix immediately caused a second, subtler regression:** `.env`'s
   `DOCKER_HOST=unix:///var/run/docker.sock` — explicitly documented
   elsewhere in this file as "unused until Step 7 containerizes the
   backend" — was, for the first time, actually loaded into the process
   environment. `validation.py`'s `docker` subprocess calls inherited it
   and tried to connect via a Linux unix socket instead of the Windows
   named pipe, breaking the sandbox for every test that touches real
   Docker. Fixed: `run_pytest_in_sandbox`'s subprocess calls now
   explicitly strip `DOCKER_HOST` from the child process's environment,
   so `docker` always falls back to its own platform default locally,
   regardless of what `.env` says (correct until the backend is actually
   containerized). Full suite re-verified green (84/84) after this fix.

   **Verified live, not just via the test suite:** started the real
   backend (uvicorn + Celery worker, both loading `.env` for real),
   started the Next.js dev server, and drove an actual migration run
   through the browser UI end to end — form submission, navigation to the
   run detail page, real OpenRouter calls, real Docker validation, status
   polling reflecting `pending → running → completed` without a page
   refresh, and the run correctly appearing in the run list afterward.
10. **SSE live progress + review queue UI (Step 8b, added 2026-08-27).**
    The run-detail page (`frontend/app/runs/[id]/page.tsx`) now opens a
    real `EventSource` against Step 7's `GET /runs/{id}/events` instead of
    polling: the SSE snapshot tells the page WHEN something changed
    (closing the connection itself once `run_status` is terminal), and the
    page still hits the REST endpoints for full row detail (the SSE
    snapshot only carries status per file_task, not confidence/test
    counts/etc.) — a deliberate simplification, not a hidden gap.

    The review queue page (`frontend/app/review/page.tsx`) lists pending
    `human_review_queue` items joined to their `file_tasks` row (diff,
    failure detail, test counts), with Approve/Reject/Modify actions
    wired to Step 7's `POST /human-review-queue/{id}/decision`. `Modify`
    submits the reviewer's corrected code through a textarea, which Step
    7 re-validates server-side per its documented contract.

    **Verified live end to end, not just built:** approved a needs_review
    item (recorded, file_task untouched, per Step 7's contract) and
    submitted a `modified` correction through the actual browser form —
    confirmed via the real Celery worker log that `validate_task` was
    re-dispatched and re-ran a real Docker sandbox, which is exactly the
    kind of thing that surfaced a genuinely interesting result: the
    corrected code passed its test (test_pass_count went 0→1) but landed
    back in the review queue anyway, because Phase 0's confidence gate
    fired again (0.75 still below the 0.8 threshold) with a fresh
    `human_review_queue` row and a different `reason` string — proving the
    confidence gate and the modify-then-revalidate path compose correctly
    end to end, not just individually.

    **Known rough edge, not fixed tonight:** `ReviewItemCard`'s
    "re-validation dispatched" confirmation message never actually
    displays — the parent's `onDecided()` callback refetches the queue
    immediately, which removes the now-decided item from the list (correct
    behavior) before its own local "dispatched" state ever gets to render
    (a cosmetic ordering issue, not a functional one — the decision is
    correctly recorded and dispatched either way, confirmed directly
    against the backend). Left as a known gap given the time budget
    tonight; a real fix would decouple "item still visible with a
    confirmation" from "item still counts as pending" (e.g. a short-lived
    local dismissed-items set instead of trusting the refetched list
    directly).
11. **GitHub PR Agent (Step 9, added 2026-08-27) — mocked GitHub API only,
    confirmed nothing was pushed or PR'd against any real repository.**
    `app/agents/github_pr.py` implements `get_pr_eligible_file_tasks`
    (Step 7's exact contract, applied verbatim), `open_pr_for_run`
    (branch/commit/push/open-PR + PR description listing both included
    files with confidence scores and excluded files with their actual
    reason), and idempotency via new `migration_runs.pr_url`/`pr_number`
    columns (migration `0004` — a run that already has a PR returns the
    existing PR info on a second call, never opens a duplicate).

    **What's real vs. mocked, and why:** local git operations (branch,
    commit) are REAL, run via `git` CLI subprocess calls against a
    throwaway temp copy of the target repo (same pattern as
    `_apply_patch_to_temp_copy` in `validation.py`) — 100% local, nothing
    external, so mocking them would only lose genuine coverage for no
    safety benefit. The `GitHubClient` Protocol's `push_and_open_pr` is
    the ONLY piece that would touch a real external service (git push to
    a remote + the GitHub API's create-PR call) — `RealGitHubClient`
    (PyGithub-backed) is fully implemented, not a stub, but is never
    instantiated anywhere in this session; every test uses
    `MockGitHubClient`, which records calls and returns a fake PR
    number/URL. There is no real `GITHUB_TOKEN` configured in this
    environment, and this session's kickoff explicitly forbids a real
    push/PR regardless of that.

    Tests verify the actual diff was correctly applied (reading the real
    patched files from the real, still-live git checkout at mock-call
    time, before the caller's own cleanup deletes it) — not just that "a
    PR was opened." Added `PyGithub==2.4.0` to `requirements.txt`.
12. **Redis semantic cache (Step 10, added 2026-08-27) — local embedding
    model, decided and documented, not defaulted silently.** No embedding
    API key (OpenAI or otherwise) is configured in this environment.
    Rather than block on that, defaulted to a free, locally-run model —
    `sentence-transformers`' `all-MiniLM-L6-v2` — same cost-conscious
    reasoning as Step 4's OpenRouter free tier. **The 384-dim output was
    confirmed empirically** by actually loading the model and calling
    `.encode()` in this session (`len(embedding) == 384`), not assumed
    from the model name. `api_doc_chunks.embedding` was `vector(1536)`
    (sized for OpenAI's `text-embedding-3-small`, never actually used) —
    migration `0005` drops and recreates the column at `vector(384)` and
    rebuilds the IVFFlat index against it (pgvector columns are
    fixed-dimension; there's no in-place resize). No real
    `api_doc_chunks` data existed anywhere this migration has run, so
    there was nothing to re-embed.

    **Naming mistake caught immediately:** the first migration filename/
    revision id (`0005_api_doc_chunks_local_embedding_dim`, 39 chars)
    exceeded `alembic_version.version_num`'s `VARCHAR(32)` — every prior
    migration id already implicitly respected this limit, just never
    written down. Alembic's transactional DDL rolled the whole upgrade
    back cleanly (confirmed via `psql` — schema untouched) when the
    version-table update failed; renamed to a 29-char id and reapplied
    successfully. Noted in the migration file itself.

    **Cache design:** `app/agents/semantic_cache.py`'s `SemanticCache` is
    a cache-aside layer — exact-text cache hit first (a hash of the
    normalized query, checked WITHOUT computing an embedding, so two
    file_tasks referencing the identical symbol trigger exactly one
    embedding computation and one Postgres/pgvector lookup between them);
    on an exact miss, embeds the query and checks a bounded,
    recency-ordered index of recently cached embeddings via cosine
    similarity (threshold 0.95 default) before falling back to a fresh
    Postgres lookup. TTL-based expiry (default 24h,
    `SEMANTIC_CACHE_TTL_SECONDS`).

    **Known simplification, documented not hidden:** the Redis instance
    here (`docker-compose.yml`'s plain `redis:7-alpine`) has no
    vector-search module (RediSearch) installed, so the fuzzy-similarity
    match is a bounded linear scan in Python (capped index size, default
    500), not an approximate-nearest-neighbor index. Fine at this
    project's scale; would need a real vector-search backend (RediSearch,
    or a dedicated vector DB) if the cache ever needed to scale well
    past that.

    **Integration note:** the Migration Agent (`migration.py`) doesn't
    currently query `api_doc_chunks` at all (Step 4 never actually built
    RAG context retrieval, despite the kickoff prompt listing
    `api_doc_chunks` as an input) — so this cache layer is built,
    verified, and provably correct against its own stated done-criteria
    (`get_doc_context_for_symbol`, tested end-to-end against real
    Postgres/Redis/the real embedding model), but is not yet wired into
    `migrate_file_task`'s actual call path. That wiring is a natural
    follow-up, not attempted tonight given the time budget — an honest
    "the cache layer works, it's not yet load-bearing in production
    traffic" rather than overstating integration that wasn't done.

13. **Gap-fix session (2026-08-27/2026-09-03/04) — two documented gaps closed: the
    review-queue toast ordering, and wiring Step 10's cache into the Migration
    Agent.** This item covers what was originally scoped as a short gap-fix
    session; writing it up got delayed by a long debugging detour (item 14
    below) that surfaced while verifying it, so both fixes and the detour are
    documented together, dated by when each actually happened.

    **Fix 1 (2026-08-27) — review-queue "modified" confirmation toast never
    rendered.** `frontend/components/ReviewItemCard.tsx`'s `decide()` built a
    confirmation message but the parent list's `refresh()` (triggered by the
    same `onDecided` callback) removed the item from the list before the
    message could render — a UI timing bug, not a logic bug. Fixed by
    changing `onDecided` from `() => void` to `(message: string) => void`:
    the card now hands its message up to `frontend/app/review/page.tsx`,
    which sets a `toast` state (6s auto-clear timer) and only THEN calls
    `refresh()`. **Verified live in the real browser** (not just by reading
    the code, per this fix's explicit done-criterion): confirmed the toast
    renders immediately for both a plain decision and a "modified" dispatch,
    and auto-clears after 6s.

    **Fix 2 (2026-08-27, code; regression-tested and hardened 2026-09-03/04)
    — Step 10's semantic cache wired into `migrate_file_task`.** Item 12
    above documented the cache as built but not load-bearing. This fix adds
    a retrieval step in `app/agents/migration.py`'s new `_retrieve_doc_context`,
    called from `migrate_file_task` right before `provider.propose_edit`:
    builds a query from the linked `changelog_events` row's
    `old_signature`/`migration_notes`, looks it up via
    `get_doc_context_for_symbol` (cache-aside: exact hash match, then
    fuzzy-similarity, then a fresh Postgres/pgvector lookup), and threads
    whatever comes back into `propose_edit` as a new `doc_context` param
    (added to the `MigrationLLMProvider` protocol and both the Claude and
    OpenRouter adapters' prompt-building). Zero chunks found — the expected
    common case — degrades to `doc_context=None`, not an error; any
    retrieval failure (Redis down, no changelog_event, etc.) degrades the
    same way rather than blocking migration, since this is a best-effort
    enhancement layer. `app/tasks.py`'s `migrate_task` Celery task supplies
    the redis client via `celery_app.backend.client` (confirmed empirically
    to be a real, live `redis.client.Redis` connection).

    **Explicitly flagged, not glossed over: no real ingestion agent exists.**
    Nothing in this system populates `api_doc_chunks` from actual API
    documentation — that pipeline step is not in the original 10-step plan
    and was not built by this fix. `tests/test_doc_context_retrieval.py`
    seeds `api_doc_chunks` rows directly as fixture data standing in for
    that missing pipeline; this is documented in the test file's own
    docstring. **Building a real doc-ingestion agent remains an unbuilt,
    named future step** — this fix makes the retrieval/cache-wiring path
    real and tested, not the data feeding it.

    **Real bug found and fixed while testing this:** `get_doc_context_for_symbol`'s
    default `SemanticCache` used a namespace shared globally across every
    `api_name` — the query text embedded `api_name` as a prefix, but the
    fuzzy-similarity index (`SemanticCache._find_similar`) scanned ONE
    shared Redis sorted set regardless of that prefix, so two different
    APIs whose query text happened to embed as merely similar could serve
    each other's cached doc chunks. A real cross-tenant correctness bug, not
    a test-isolation artifact — caught because writing a proper "two
    different symbols" test exposed cross-contamination between them. Fixed
    by scoping the cache's namespace itself (not just the query text) by
    `api_name` in `get_doc_context_for_symbol`.

14. **Debugging marathon (2026-08-31 through 2026-09-04) — a full-suite hang
    traced through five wrong turns to two real, independent bugs; both
    fixed and regression-tested.** Verifying item 13's Fix 2 against the
    full test suite repeatedly stalled or crashed across several sessions.
    Documenting the whole path here because most of it was ruled OUT with
    real evidence, not assumed — future debugging of anything that looks
    like "the suite hangs" should start from what's below rather than
    re-treading the same dead ends.

    **Ruled out, with live evidence, in order:**
    - *Docker/WSL2 instability* — Docker Desktop genuinely did crash into an
      unresponsive npipe state at least twice this week under many
      consecutive hours of heavy container churn. Fixed each time by fully
      killing every `Docker Desktop`/`com.docker.*` process, `wsl --shutdown`
      to reset the WSL2 backend, and relaunching. A dedicated background
      health logger (`docker info` polled every 2 minutes to a file) proved
      Docker stayed healthy — zero `UNRESPONSIVE` entries across a ~2h50m
      window — through a run that hung anyway, ruling Docker out as *that*
      run's cause even though it had been a real cause on other occasions.
    - *Stuck Postgres lock* — `pg_stat_activity WHERE state != 'idle'` and
      `pg_locks WHERE NOT granted` were checked live, mid-hang, and both
      came back clean. Not a lock.
    - *Celery dispatch never happening / chord callback never firing* —
      `celery -A app.celery_app inspect active/reserved/scheduled` run
      live, mid-hang, showed tasks genuinely `active` (not reserved,
      not scheduled, not absent) — ruling out "nothing dispatched" and
      "group finished but chord didn't fire" (the group's own tasks hadn't
      finished, so the chord never got the chance).

    **Real bug #1 — a test-suite-only leak that self-perpetuated:**
    `tests/test_api.py`'s `_cleanup(run_id, event_id)` deletes a test's
    `file_tasks`/`migration_runs` rows, but `test_list_includes_created_run_newest_first`
    called `_wait_for_run_status(...)` and `_cleanup(...)` as two separate
    statements in one `finally:` block — if the wait raised `TimeoutError`
    (the pipeline having genuinely stalled for any reason), `_cleanup` never
    ran, permanently leaking that run's DB rows AND its already-dispatched
    Celery tasks into the shared, module-scoped `celery_worker` fixture's
    limited thread pool. Confirmed live: a `migration_runs` row was found
    stuck at `status='running'` for 2+ hours, timestamped to exactly this
    test. Fixed by nesting: `try: _wait_for_run_status(...) finally: _cleanup(...)`,
    so cleanup always runs. Also added `CLAIM_STATEMENT_TIMEOUT_MS` (default
    5000ms, `SET LOCAL statement_timeout`) to both atomic claim queries in
    `app/agents/resume.py` as a defense-in-depth safety net for a genuinely
    different future failure mode (an uncommitted transaction from some
    other crash holding a row lock) — not the cause of anything hit this
    session, but the same "hang silently forever, zero CPU, invisible"
    symptom class, now bounded.

    **Real bug #2 — a genuine concurrency deadlock in this session's own
    Fix 2 code, found via a live file-based diagnostic trace:** `_model` in
    `app/agents/semantic_cache.py` is a lazy-loaded module-level singleton
    (a `SentenceTransformer` instance), shared by every Celery worker
    thread (`pool="threads"` — both the embedded test worker and a real
    containerized `celery-worker` run this way). `TestSSEStream`'s fixture
    repo has two files with matches (`service_a.py`, `service_b.py`), so its
    group/chord fan-out dispatches two `migrate_task` threads concurrently,
    both calling `embed_text()` → `.encode()` on that same shared model
    instance ~100ms apart. Confirmed via `celery inspect active` (both
    tasks genuinely `active`, never completing) and a temporary file-based
    diagnostic trace added directly to `_retrieve_doc_context` and
    `SemanticCache.get_or_compute` (bypassing Celery's stdout capture):
    both threads logged "about to embed_text" and neither ever logged
    "embed_text returned" — a real deadlock, not slowness, with `docker ps`/
    `pg_stat_activity`/`pg_locks` all staying clean throughout (idle CPU, no
    container, no lock — the hang was purely inside Python/native code).
    This is a known deadlock class: HuggingFace `tokenizers` spins up its
    own internal thread pool for tokenization, which can deadlock against
    Python-level concurrent calls into one model instance. **Fixed two
    ways, deliberately not relying on either alone:** (1)
    `os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")`, set at
    `semantic_cache.py` import time (before `sentence_transformers`/
    `tokenizers` loads) — HuggingFace's own documented fix for this
    deadlock class; (2) a `threading.Lock` (`_encode_lock`) around the
    actual `.encode()` call in `embed_text`, serializing concurrent access
    to the shared model regardless of whether (1) fully resolves the
    underlying library issue. While instrumenting this, also found and
    fixed a related but distinct bug in the same function: `_get_model()`'s
    `if _model is None` lazy-singleton check had no lock, so two threads
    racing their first call could both see `_model is None` and each
    construct (and load the weights for) their own separate model instance
    — confirmed live via "Loading weights: 100%" printing twice per run.
    Fixed by moving the `_get_model()` call inside `_encode_lock` too, so
    construction happens at most once. A regression test,
    `TestConcurrentEmbedTextDoesNotDeadlock` in `tests/test_semantic_cache.py`,
    dispatches 6 concurrent `embed_text()` calls via real threads (the same
    execution shape that hit the bug) and asserts all return within a
    bounded join — before the fix this reliably deadlocked forever; after,
    it completes in ~14s.

    **Real bug #3 — a genuine, pre-existing test-fixture gap, found only
    once bug #2 stopped masking it.**

    *What it was:* item 13's Fix 2 added a new `doc_context` keyword
    parameter to the `MigrationLLMProvider.propose_edit` protocol
    (`app/agents/llm_providers/base.py`), and `migrate_file_task`
    (`app/agents/migration.py`) was updated to always pass it —
    `doc_context=None` in the common case, real chunk text when retrieval
    finds something. The two REAL adapters (`ClaudeAdapter`,
    `OpenRouterAdapter`) were updated to match at the same time and are
    covered by `test_doc_context_retrieval.py`'s adapter-prompt tests. But
    several TEST FILES each define their own small `_FakeProvider`-style
    stand-in implementing that same protocol by hand (not a shared base
    class, not `**kwargs`-based), so nothing enforced them staying in sync
    with a protocol change made elsewhere. Three were missed: `tests/test_api.py`'s
    `_FakeProvider`, `tests/test_orchestrator.py`'s (unnamed, used by
    `TestConcurrentFanOut`), and `tests/test_resume.py`'s `_FakeProvider`.
    Any real-pipeline test using one of these three raised
    `TypeError: propose_edit() got an unexpected keyword argument
    'doc_context'` from inside `migrate_task`, every time, unconditionally
    — not an edge case, not a race, a guaranteed failure on every call.

    *Why the deadlock was hiding it:* bug #2's deadlock sits earlier in
    the exact same call path — `migrate_file_task` calls
    `_retrieve_doc_context` (which is where bug #2's `embed_text()` hang
    lived) BEFORE it ever reaches the `provider.propose_edit(...)` call
    that bug #3 breaks. With bug #2 unfixed, every affected task froze at
    the retrieval step and never got far enough to reach the
    `propose_edit` call at all — so bug #3's `TypeError` had no chance to
    fire; the process just hung first. Once bug #2 was fixed and tasks
    could actually finish `_retrieve_doc_context` and proceed, they
    immediately hit bug #3 instead. This is why fixing bug #2 didn't make
    the previously-hanging `TestSSEStream` test pass on the first re-run
    — it traded an infinite hang for a fast, clean failure surfacing a
    second, independent bug underneath it.

    *The specific failure mode this produced:* the `TypeError` inside
    `migrate_task` surfaces as a Celery `ChordError` on the group member
    task. `finalize_run` (the chord callback that sets
    `migration_runs.status = 'completed'`) is never invoked when a member
    task errors like this, so the run's status stays at `'running'`
    forever. Every test in an affected file that drives a real pipeline
    run hits this — but the SYMPTOM differed by test depending on how each
    one waits for completion: tests using `_wait_for_run_status(...,
    timeout=60.0)` (a client-side bound written into the test helper
    itself) failed after 60s with a clear `TimeoutError`, which is slow
    but bounded and gets correctly reported as a failure. `TestSSEStream`
    has no such bound — its SSE poll loop (`app/api/main.py`'s
    `event_generator`) only exits when `run_status` reaches a terminal
    value, with no independent max-duration — so it was the one test that
    hung indefinitely rather than merely failing slowly. Earlier in this
    same investigation, before bug #2 was found, the 60s-bounded failures
    from this exact bug were misread as generic DB-contamination noise
    (item 14's "ruled out" list); they weren't wrong to rule out DB
    contamination as *a* cause, but this was a second, real cause
    contributing to the same-looking symptom.

    *Is it fixed, and is it tested?* Fixed in all three files found:
    `_FakeProvider.propose_edit` in `test_api.py`, the analogous provider
    class in `test_orchestrator.py`, and `_FakeProvider.propose_edit` in
    `test_resume.py` all now accept `doc_context=None` in their
    signatures, each with a dated comment pointing back to this note.
    `test_migration.py`, `test_validation.py`, and `test_orchestrator.py`'s
    second fake-provider class were checked too and are fine as-is — they
    already accept `**kwargs`, so a new keyword-only param never breaks
    them. This is a fix to test fixtures, not application code — there is
    no dedicated new regression test for "does `_FakeProvider` accept
    `doc_context`" beyond the fact that every real-pipeline test in these
    three files now exercises that call successfully as a side effect of
    passing at all. **Caught by an actual full-suite run**, not
    inferred: after the first re-run of just `TestSSEStream` passed
    (having only ever exercised `test_api.py`'s copy), the full suite
    (`pytest tests/`, 103 tests) surfaced the other two independently as
    `3 failed, 100 passed in 283.84s` — `test_orchestrator.py::TestFullRunLifecycle::test_run_completes_with_expected_terminal_states`,
    `test_orchestrator.py::TestConcurrentFanOut::test_group_chord_runs_file_tasks_concurrently`,
    and `test_resume.py::TestResumeMigrationRunMixedFixture::test_only_non_terminal_rows_are_redispatched`,
    all with the identical `doc_context` `TypeError`. All three are fixed
    as of this note; a clean full-suite re-run to confirm `103 passed, 0
    failed` was still in progress when this note was written — **do not
    treat this bug as closed until that re-run is confirmed**, and check
    below/TASKS.md-adjacent session history for whether it was.

    **Separately noted, not fixed:** the SSE endpoint's poll loop still
    has no independent timeout of its own — by design it reflects real
    pipeline state, but that means any future stall in the pipeline (for
    whatever reason) will again manifest as an indefinite SSE hang rather
    than a bounded failure. Worth a follow-up if this class of issue
    recurs.

    **End state:** `TestSSEStream::test_stream_emits_events_ending_in_terminal_status`
    passes cleanly in isolation (`1 passed in 20.51s`); the new concurrency
    regression test passes (`1 passed in 13.92s`); a full-suite run
    (bounded to a 15-minute hard timeout, not left open-ended) found and
    led to fixing the two additional `doc_context` fixture gaps above.
    Final confirmation that the full suite is 100% green after those two
    fixes was still pending at the time this note was written.

    **Confirmed 2026-09-06:** after a Docker Desktop restart (the daemon
    had genuinely hung overnight — force-quit + `wsl --shutdown` +
    relaunch, confirmed stable via a 2-minute health-check loop before
    trusting it again), a clean full-suite run against the host venv
    passed completely: **`103 passed in 103.87s`, 0 failures.** The
    pending confirmation above is closed.

15. **Containerization live verification (2026-09-06/07) — the one thing
    this whole project had never actually proven end-to-end, now proven,
    after finding and fixing a real bug.** `docker compose up --build`
    brings up all 5 services healthy. A real fixture migration was run
    twice, container-to-container (via `POST http://localhost:8000/runs`
    hitting the actual `backend` container, not a host-run process),
    through the fully containerized stack.

    **Real bug found and fixed: the Docker CLI binary was never actually
    present inside `celery-worker`'s container**, despite `backend/Dockerfile`
    explicitly running `apt-get install -y --no-install-recommends docker.io`
    (item 13's containerization-session design fork, confirmed correct in
    principle — install the CLI binary rather than refactor `validation.py`
    to `docker-py`). The first live fixture-migration attempt got a real
    `migrate_task` to succeed (a genuine OpenRouter LLM call, ~33s) and
    then `validate_task` failed immediately with `SandboxSetupError: Could
    not locate the docker CLI`. Root-caused live, not guessed: `dpkg -l`
    showed `docker.io` genuinely installed, but `dpkg -L docker.io` showed
    it only provides the DAEMON (`dockerd`, `docker-proxy`, `docker-init`)
    — on this base image's Debian version (`python:3.12-slim` → Debian
    "trixie"/13), the actual `/usr/bin/docker` CLI client ships in a
    separate `docker-cli` package, reachable only via `docker.io`'s
    `Recommends:` (confirmed via `apt-cache show docker.io`), not its
    `Depends:` — so `--no-install-recommends`, a reasonable minimal-image
    default, silently stripped the one binary `validation.py`'s
    `_docker_binary()` actually needs. Fixed by explicitly adding
    `docker-cli` to the Dockerfile's install list (full dated writeup in
    the Dockerfile itself, next to the original design-fork note).

    **After the fix, a fresh fixture migration matched the fixture's
    documented expected outcome exactly:** `service_a.py` → `validated`
    (confidence 1.0, 1 pass/0 fail), `service_b.py` → `needs_review`
    (confidence 0.75, 1 pass/1 fail) — both reached via a genuine
    Docker-in-Docker sandboxed pytest run inside `celery-worker`'s
    container, talking to the HOST's Docker daemon through the mounted
    socket, not a nested daemon. Confirmed via: no `SandboxSetupError`
    anywhere in the container's logs; `validate_task` completing with
    real, differentiated `test_pass_count`/`confidence_score` values
    (impossible to produce without the sandbox subprocess actually
    running); and the frontend (`npm run dev`, unchanged, not
    containerized per design) confirmed reachable at
    `http://localhost:3000` and correctly configured to talk to the
    containerized backend via `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000`
    in `frontend/.env.local`.

    **Correction, added 2026-09-07 (same evening, after this note was
    first written) — the paragraph above is NOT the full story for that
    run.** "Matched the fixture's documented expected outcome exactly"
    was true of the run's FINAL state, but not an accurate description of
    what actually happened during it: this exact run also hit the
    `periodic_resume_sweep` duplicate-dispatch race documented in detail
    below. It wasn't caught by anything in this write-up, and wasn't
    caught by automated testing either — it was caught afterward by a
    human looking at the actual Review Queue page in the browser and
    noticing two identical entries for the same file. Querying
    `human_review_queue` confirmed both rows shared the same
    `file_task_id` AND the same `run_id` as this "clean" verification run.
    The reason it didn't visibly break the outcome reported above is
    exactly the masking mechanism described below (the `file_tasks`
    counter columns silently absorbed the duplication via a lost update,
    while `human_review_queue`'s insert-only rows didn't). Leaving the
    original paragraph in place above, uncorrected in itself, would
    overstate what was actually verified that run — the Docker-in-Docker
    sandbox mechanism itself is still genuinely proven working (that part
    holds), but "this specific run ran clean end to end" does not.

    **Diagnostic dead ends worth recording so they're not re-walked:**
    `docker events --since/--until` returned nothing for a window with
    known container activity — unreliable in this environment, not
    evidence of anything; `docker ps -a` never caught a live
    `migration-agent-sandbox-*` container because `--rm` containers for
    this tiny fixture complete and vanish within ~1-2 seconds, faster than
    a 5s poll interval; `/sandbox_tmp` (the shared bind-mounted temp
    directory) was empty after a successful run because
    `validate_file_task` explicitly `shutil.rmtree`s its temp copy in a
    `finally` block — none of these are signs of failure, they're just
    not the right place to look for proof; the `file_tasks` result data
    itself (real, differentiated pass/fail counts and confidence scores)
    was the actual reliable evidence.

    **Real bug, found across two separate runs, documented precisely here
    (2026-09-07) — NOT fixed yet, deliberately left for a future session.**
    In BOTH the pre-fix-confirmation run and the later "clean" verification
    run (see the correction above), a file_task's `migrate_task`/
    `validate_task` pair was dispatched TWICE for the same `file_task_id`
    within the same `migration_runs` run.

    *Mechanism:* `periodic_resume_sweep` (Step 6b, Celery Beat, fires
    every `RESUME_SWEEP_INTERVAL_SECONDS`, 60s by default) runs on its own
    independent schedule, not reset or delayed by a new run starting. If
    it ticks while a freshly-dispatched file_task's first `migrate_task`
    call is still genuinely in flight against a slow REAL (not mocked)
    LLM call (observed: 21-66s per OpenRouter call in these runs, this
    project's free-tier model), the sweep sees that file_task's row still
    at `status='pending'` — because `migrate_task` does not flip it to
    `in_progress` before the LLM call — and `try_claim_pending_file_task`
    happily reclaims and re-dispatches it. `try_claim_pending_file_task`
    has no staleness gate on `pending` rows, and its own docstring's
    reasoning for that ("pending rows are always claimable, no staleness
    gate needed since a pending row was never claimed by anyone in the
    first place") is correct for a truly-never-claimed row but doesn't
    hold for a row that's genuinely mid-flight under a slow real call —
    the two cases look identical from this claim function's point of view
    (both are `status='pending'`), but only one of them should be
    re-dispatched.

    *The masking mechanism (why this looked clean in `file_tasks` but not
    in `human_review_queue`):* `validate_file_task`'s counter increments
    (`file_task.test_pass_count += 1`, `confidence_score = min(1.0, ... +
    0.15)`, etc.) are an unguarded Python-level read-modify-write, not an
    atomic SQL increment and not protected by row locking. When both
    duplicate invocations run concurrently, each reads its own in-memory
    copy, increments it, and commits — whichever commits LAST silently
    overwrites the other's numeric changes (a lost update), so the final
    `file_tasks` row can look like exactly one clean pass happened even
    when two actually did. `HumanReviewQueue` rows are a different case:
    each duplicate invocation's `session.add(HumanReviewQueue(...))` is a
    brand-new INSERT, not an update to a shared column, so neither is
    lost — both survive as separate rows. This is why the bug was
    invisible in every automated check run against `file_tasks`/aggregate
    outcomes this session, and was only actually caught by a human
    noticing two duplicate entries on the real Review Queue page in the
    browser.

    *Existing idempotency guard's asymmetry:* `validate_file_task` already
    has `if file_task.status == "validated": return` at its top, which
    correctly makes a second duplicate invocation a no-op once the FIRST
    invocation has reached the `validated` terminal state. There is no
    equivalent guard for the `needs_review` terminal state — a second
    invocation arriving after the first has already set `status =
    'needs_review'` still runs the full retry loop again and inserts
    another `HumanReviewQueue` row. This asymmetry is exactly why
    `service_a.py` (which reached `validated`) never showed a visible
    duplicate in either observed run, while `service_b.py` (which reached
    `needs_review`) did both times.

    *Two candidate fixes, NOT implemented, evaluate before picking one:*
    (1) add a staleness/already-in-flight gate to `try_claim_pending_file_task`
    itself — e.g. don't treat `pending` as unconditionally claimable if
    the row was touched (an `updated_at` bump, or an explicit
    `in_progress`-like marker) recently enough to plausibly still be
    in-flight, mirroring the staleness gate `try_claim_stale_in_progress_file_task`
    already has for `in_progress` rows; (2) extend the idempotency guard
    in `validate_file_task` (and possibly `migrate_file_task`) to also
    treat `needs_review` as a terminal no-op state, matching the existing
    `validated` guard — this closes the SYMPTOM (duplicate review-queue
    rows) but not the underlying double-dispatch itself, so the
    `file_tasks` counter lost-update race would still be live for any
    other side effect added later that isn't guarded the same way. Doing
    both is probably right, but that decision and the implementation are
    deliberately left for a future session, not attempted tonight.

    *Not a wrong-result bug, but not "safe to ignore" either:* every
    observed outcome from this race was still eventually correct (no
    migration was ever marked validated or needs_review incorrectly) — but
    it does real redundant work (an extra real LLM call, an extra
    sandbox run) and, as demonstrated, can leave duplicate rows visible in
    a place a human actually looks (the Review Queue page). Treat it as a
    real, reportable bug with a known trigger condition and two named
    candidate fixes, not as background noise.

    **Update, 2026-09-17 — this same guard gap turned out to have a
    worse consequence than "duplicate rows," found by the real-world
    battery: it can silently overwrite a legitimate, informative
    `failure_reason` with a misleading one.** Full mechanism in §4b's
    Finding #1 below. That new evidence is why §4b recommends
    prioritizing this section's second candidate fix (extending the
    idempotency guard to cover `needs_review`, not just `validated`) over
    the first.

## 4b. Real-world test battery findings (2026-09-16/17)

`backend/tests/real_world_battery_runner.py` ran 10 genuine, real
breaking changes from real libraries (OpenAI, pandas, NumPy, Pydantic,
SQLAlchemy, Flask, urllib3) through the full containerized pipeline —
full results and per-case judgment in
`backend/tests/REAL_WORLD_BATTERY_REPORT.md`. Two real, newly-discovered
issues came out of it, precise enough to pick up properly in a future
session rather than only living in that report.

**Finding #1 — `validate_file_task`'s `needs_review` idempotency gap
(§4a's already-documented gap) has a WORSE consequence than previously
known: it can silently destroy a legitimate, informative failure reason.**
Mechanism, confirmed via battery case 6 (`@validator` -> `@field_validator`):

1. `migrate_file_task` (`app/agents/migration.py`) hits one of its
   early-return branches — missing changelog event (line ~125-130),
   `ProviderResponseError` (line ~160-164), or invalid-Python syntax
   (line ~175-181). Each sets `file_task.status = "needs_review"` with
   its OWN specific, useful `failure_reason`, leaves
   `file_task.old_code_snippet` as `None` (that's only ever set at line
   183, AFTER these branches), and returns NORMALLY — no exception.
2. `migrate_task` -> `validate_task` is a Celery `chain` (`app/tasks.py`'s
   `_build_file_chain`), which runs the next task regardless of what
   status the row ended up at — chains don't inspect intermediate DB
   state, only whether the previous task raised.
3. `validate_file_task`'s only idempotency guard
   (`app/agents/validation.py:392`, `if file_task.status == "validated":
   return`) doesn't match on `needs_review`, so it does NOT skip.
4. `validate_file_task` proceeds into `_apply_patch_to_temp_copy`
   (`validation.py:300-329`), which compares the real file's current
   content against `file_task.old_code_snippet` (still `None`) at line
   322, finds a mismatch (anything `!= None`), and OVERWRITES
   `failure_reason` with its own generic "old_code_snippet no longer
   matches the real file... Expected None, found '...'" message —
   silently destroying the actually-useful reason from step 1.

Observed effect: case 6's recorded `failure_reason` blamed a
"file changed since scan" staleness issue that never happened; the real
cause (almost certainly a provider tool-call failure, the same failure
mode independently observed in battery case 7) was lost. **This means
§4a's second candidate fix — extending the idempotency guard to also
treat `needs_review` as a terminal no-op — is worth prioritizing over the
first candidate fix**, since it would prevent this specific
information-destroying failure mode as a side effect, not just the
duplicate-dispatch symptom §4a originally documented it for. Not fixed
this session.

**Finding #2 — sandbox pytest-json-report parsing doesn't distinguish a
collection error from "no detail available".** Confirmed via battery
cases 3, 9, 10 (`pandas.io.json.json_normalize`, `flask.Markup`,
`urllib3.contrib.pyopenssl.inject_into_urllib3` — all three genuinely
raise `ImportError` at collection time pre-migration, since the removed
API can't even be imported under the target library version installed in
the sandbox). `validation.py`'s report-parsing logic (~line 245): reads
`summary.passed`/`summary.failed`/`summary.error` and the `tests` list
from the JSON report. On a genuine collection error, `pytest-json-report`
doesn't populate `tests` the normal way (nothing was ever collected to
report on), so `failing = [t for t in report.get("tests", []) if ...]`
comes back empty, and the code falls through (line ~264-266) to a
generic `f"pytest reported {fail_count} failure(s) but no per-test
detail was found in the report (pass_count={pass_count})"` — actively
unhelpful, and indistinguishable from other genuinely-ambiguous report
shapes. All three retries in each affected case likely saw this same
uninformative message, which may itself be why they never converged (the
Migration Agent's failure_context, built from this vague string, carries
none of the real `ImportError` detail a real developer would use to fix
it). Worth checking `report.get("collectors")` (or equivalent
collection-error indicator in `pytest-json-report`'s schema) and
surfacing that distinctly. Not fixed this session.

## 4a. Beyond the original 10-step plan (added 2026-09-16)

Everything in this section is new scope, explicitly outside the 10 steps
§4 above documents. Flagged as its own section on purpose, not folded
into §4's numbering — these were never part of the original plan, and
that distinction matters (e.g. when reasoning about "is the 10-step plan
done" versus "what else has been added since"). See TASKS.md's matching
"Beyond the original 10-step plan" section for the plain deliverable/
done-when summary.

**Three related additions, one session, one shared table (`repos`,
migration `0006`):** saved repos (a name/path/api_name shortcut, basic
CRUD, a frontend picker), scheduled auto-checking (opt a saved repo into
a periodic sweep that finds new changelog_events and kicks off a run
automatically), and webhook notifications (an optional Slack/Discord-
compatible POST on a run's terminal state and on needs_review).
Deliberately kept minimal over feature-complete, per the kickoff's own
steer — no cron-expression system, no notification-schema invention, no
repo-update endpoint (delete-and-re-add covers that at this scope).

**Saved repos.** `db/models.py`'s new `Repo` model; `POST/GET/DELETE
/repos` in `app/api/main.py`; `frontend/app/repos/page.tsx` (add/list/
remove, linked from `Nav.tsx`); `RunStartForm.tsx` gained an *optional*
saved-repo `<select>` that prefills repo path + API name -- selecting one
is identical in effect to typing those two fields by hand (same state,
same submit path), and typing manually after selecting one clears the
selection back to "Type manually…" rather than leaving a stale
association. The dropdown only renders once at least one repo is saved;
an empty or failed `listRepos()` call degrades to exactly the original
form (never blocks manual entry — see `RunStartForm.tsx`'s dated
comment).

**Scheduled auto-checking.** `Repo.auto_check_enabled`/
`check_interval_hours`/`last_auto_checked_at` (all in migration `0006`);
`app/tasks.py`'s new `periodic_auto_check_sweep`, registered in
`celery_app.py`'s `beat_schedule` (ticks every
`AUTO_CHECK_SWEEP_INTERVAL_SECONDS`, default 300s -- the repo's own
`check_interval_hours` is the real gate, the tick just needs to be
frequent enough not to drift far past it). Same overall shape as Step
6b's `periodic_resume_sweep`, reused deliberately rather than inventing a
second scheduled-task pattern in the same codebase.

*Real design gap in the feature's own spec, resolved and documented,
not glossed over:* a migration run needs a specific `(version_from,
version_to)` pair (`start_migration_run`'s existing, unchanged
signature) -- a `Repo` row only carries `default_api_name`, no version.
The spec's own wording ("checks for changelog_events matching its
default_api_name with processed_at IS NULL, and if any exist, kicks off
a real migration run") doesn't say which version pair to use when
unprocessed events span more than one. Resolved by grouping a due repo's
unprocessed events by their OWN `(version_from, version_to)` and
dispatching one run per distinct pair found (consistent with `scan_task`
already matching changelog_events to a run by that exact triple) --
tested explicitly (`test_auto_check_sweep.py`'s
`test_sweep_return_value_counts_dispatched_runs_not_repos_examined`) so
this isn't just a comment nobody verified.

`last_auto_checked_at` is the one column beyond the feature's literal
list ("id, name, repo_path, default_api_name" for the base table; "auto_
check_enabled, check_interval_hours" for scheduling) -- required to
implement the spec's OWN stated gating rule ("a repo's last auto-checked
time compared against its check_interval_hours is enough"), since
nothing else could hold that timestamp. Updated on every DUE repo
examined each tick, whether or not anything new was found -- "checked,
found nothing" and "never checked" are different states, and only the
timestamp distinguishes them (verified by a dedicated test case, not
assumed).

**Webhook notifications.** `app/agents/notifications.py` (new, DB-
independent, directly unit-testable -- same split as `app/agents/
resume.py`); `NOTIFICATION_WEBHOOK_URL` in `.env`/`.env.example`, unset
by default. Hooked into `app/tasks.py` at exactly two shapes of call
site: after any of the three places a run's status becomes `completed`/
`failed` (`finalize_run`, `scan_task`'s except block, `_mark_run_failed_
if_stuck`) via one shared `_notify_run_terminal` helper; and after
`migrate_task`/`validate_task` each commit, checking `file_task.status ==
"needs_review"` via `_notify_if_needs_review`. Deliberately NOT threaded
into every individual `status = "needs_review"` assignment inside
`migration.py`/`validation.py` (there are several) -- checking the
committed state after the two Celery tasks that can produce it covers
every path with two call sites instead of many, and keeps webhook
side-effects entirely in the Celery-glue layer rather than reaching into
already-verified agent modules again.

*Payload shape, decided deliberately:* `{"text": "<message>"}` -- what
Slack- and Discord-compatible incoming webhooks expect for a plain
message, per the kickoff's own steer ("support that simple text-message
shape rather than inventing a custom schema, since it's what actually
works with the free tools someone would point this at"). Run-terminal
messages fold in `run_id`/`status`/`api_name`/file-counts-by-status (one
`GROUP BY status` query) into that one text field; needs_review messages
fold in the file path, run id, and failure reason.

*Best-effort, explicitly NOT covered by CLAUDE.md's usual "silent
wrongness is the failure mode to design against" principle:* unset URL
-> silent no-op (not an error, not a log line -- notifications are an
optional layer on an already-complete pipeline). Configured URL but the
POST itself fails (network blip, receiver down) -> caught and swallowed,
never re-raised -- a flaky webhook receiver must never fail a real
migration task. This is a deliberate, documented exception: that
principle is about PIPELINE correctness (a wrong migration shipping
unnoticed), a different failure class with different stakes than a
best-effort side notification failing to send.

**What was NOT built, on purpose, at this scope:** no repo-update
endpoint (delete-and-re-add substitutes); no cron-expression scheduling
(a plain hours-interval-since-last-check is what the kickoff explicitly
asked for); no retry/backoff on a failed webhook delivery (one attempt,
swallow failure, move on -- consistent with "best-effort, not part of
the correctness contract" above); no UI surface for the sweep's own
activity (no "last sweep ran at X" indicator beyond each repo's own
`last_auto_checked_at`, already shown in the `/repos` table).

## 4c. Public demo deployment (2026-09-20)

Frontend is a static export on GitHub Pages
(`https://garvbardia.github.io/api-migration-agent/`, `gh-pages` branch),
calling this machine's backend through a Cloudflare *quick* tunnel. Restart
procedure and honest limits are in PROJECT_STATUS.md ("Public demo"); this
section records what a future session would otherwise re-derive.

- **Dynamic route can't be statically exported — verified by a real build,
  not assumed.** `output: "export"` failed with `Page "/runs/[id]" is
  missing "generateStaticParams()"` (run ids are runtime data). Run detail
  moved to `app/run/page.tsx` reading `?id=` via `useSearchParams()` inside a
  `<Suspense>` (required for export). Links in `RunList.tsx`/
  `RunStartForm.tsx` and `Nav.tsx`'s active-state check updated. Backend
  API paths (`/runs/{id}`) are unchanged.
- `next.config.mjs`: `output: "export"` is unconditional (so `next dev`
  catches export-incompatible changes locally), `trailingSlash: true`,
  `basePath` from `NEXT_PUBLIC_BASE_PATH` (Pages project sites live under
  `/<repo>/`; without it every `/_next/` asset 404s). The API URL is
  `NEXT_PUBLIC_API_BASE_URL`, inlined at build time (already how
  `lib/api.ts` worked); a real env var on the command line beats
  `.env.local`.
- **Git Bash mangles leading-slash env values**: `NEXT_PUBLIC_BASE_PATH=
  /api-migration-agent` becomes `C:/Program Files/Git/api-migration-agent`
  and the build aborts. Needs `MSYS_NO_PATHCONV=1` (baked into
  `deploy/deploy_pages.sh`).
- **`.nojekyll` is mandatory** in the published branch — Jekyll drops
  underscore-prefixed dirs, i.e. all of `/_next/`.
- **`gh-pages` branch, not Actions**: pushing a workflow file needs the
  `workflow` OAuth scope (the gh token has `repo` only) → another
  interactive login. `deploy/deploy_pages.sh <tunnel-url>` builds locally and
  force-pushes `out/` to `gh-pages` (a build-artifact branch, so force-push
  is normal).
- **Why the founder saw GitHub's 404 first:** Pages had never been enabled;
  `gh api repos/.../pages` returned 404. GitHub auto-created the Pages site
  when the `gh-pages` branch was first pushed (verified afterwards: source
  `gh-pages` `/`, `https_enforced`, build `built`).
- CORS (`backend/app/api/main.py`): added the bare origin
  `https://garvbardia.github.io` (an Origin header has no path) alongside
  the localhost entries. Verified with real preflights — allowed origin
  echoed, foreign origin rejected — locally and through the tunnel; backend
  container restarted to be sure it loaded the change.
- **Quick tunnel URL changes on every cloudflared restart**, and the URL is
  baked into the static build → restart means re-running the deploy script.
  A process started from inside `frontend/out` inherits that cwd and makes
  `rm -rf out` fail with EBUSY (happened; start long-running processes from
  the project root). New `*.trycloudflare.com` names can take minutes to
  resolve on this machine's resolver though 1.1.1.1 has them immediately —
  verify with `curl --resolve` before concluding the tunnel is broken.
- **Security posture — unauthenticated, publicly reachable API.** Anyone with
  the tunnel URL (it's in the public JS bundle) can `POST /runs` with an
  arbitrary `repo_url` (a filesystem path inside the celery-worker
  container; the scanner reads `.py` files there and sends matched snippets
  to the LLM provider), start work that spends free-tier LLM quota and runs
  sandbox containers on this machine, and approve/reject/delete via the other
  endpoints. No file upload path exists, so it can't inject code to run, and
  the root `.env` isn't inside the container's `/app` mount — but this is a
  demo-grade exposure, not a hardened one. Auth is the obvious next step
  before anything beyond a trusted-audience demo.
- Verified end to end from the live Pages URL in a real browser: page loads,
  cross-origin `/repos` and `/runs` calls to the tunnel return 200, and a run
  started from the public form completed through the real Docker sandbox
  with the fixture's documented outcome (`service_a.py` validated 1.00 1/0,
  `service_b.py` needs_review 0.75 1/1).

## 5. Build order and status

**Updated 2026-08-26:** Docker Desktop is now installed and running. `0002`
was applied to a real Postgres instance with `alembic upgrade head` (clean
linear `0001` → `0002`, no multiple-heads/revision errors), and the
resulting schema was confirmed directly via `psql \d file_tasks` — all four
new columns and `uq_file_tasks_run_path_symbol_line` are live on the actual
table. Step 3's done-criteria 2 and 6 (multi-match persistence,
idempotent re-scan) were re-run through the real `persist_matches` path
against this database and confirmed both via the ORM and independently via
raw `psql` queries. The previous note here about an in-memory SQLite
stand-in (used only because Docker wasn't installed yet) no longer applies.

| Step | Component | Status |
|---|---|---|
| 1 | docker-compose, `.env.example`, `requirements.txt` | ✅ done (requirements.txt gained `tree-sitter`, `tree-sitter-python`, `pytest` in the Step 3 session — needed by the scanner, not previously listed) |
| 2 | SQLAlchemy models (5 tables) + `0001` migration | ✅ done, applied to real Postgres |
| 2b | `0002` migration: match metadata on `file_tasks` + looser uniqueness | ✅ applied to real Postgres and verified via `psql` |
| 3 | Impact Analysis Agent (tree-sitter AST scanner) | ✅ built, unit-tested, and verified end-to-end against real Postgres |
| 4 | Migration Agent (provider-agnostic tool-use adapter: Claude + OpenRouter) | ✅ built and unit-tested; OpenRouter verified live (`cohere/north-mini-code:free`), Claude verified via mocks only — no `ANTHROPIC_API_KEY` available this session, see TASKS.md note |
| 5 | Validation Agent (Docker sandbox, retry loop) | ✅ built and unit-tested against real Docker (network isolation, container teardown/no state leak, timeout+kill, structured `pytest --json-report` parsing all verified live, not mocked); retry loop tested with a mocked provider (deterministic) plus one live OpenRouter smoke test that recovered a real broken fix on retry |
| 6a | Orchestrator: Celery task graph + state machine | ✅ built and verified against real Redis + an embedded Celery worker (`pool=threads`), not eager mode — pending→running→completed lifecycle, zero-match vs. scan-error outcomes, and true concurrent fan-out (group/chord) all confirmed live. Also fixed a real cross-file test-pollution bug in Step 5's sandbox runner, only surfaced once a multi-file fixture existed — see §4.4's dated note |
| 6b | Orchestrator: crash-resume, dead-task detection | ✅ built and tested — atomic claim guards proven under real concurrent threads, resume-granularity decision, staleness distinction, and sweep-attempts cap all verified against real Postgres; mixed-state fixture resumed via a real embedded Celery worker. An actual worker-process crash can't be forced from a unit test — see CLAUDE.md §4.7's honesty note. Also fixed a real `migrate_task` idempotency bug this session's own claim guard exposed |
| 7 | FastAPI + SSE endpoints | ✅ built and tested: `POST /runs` verified to actually trigger the real Step 6a pipeline (file_tasks appear, not just a 200); all GET endpoints checked against direct DB queries; SSE stream proven against a real embedded Celery worker, correct event order, closes on terminal status; all three review-decision types tested, `modified` proven to actually re-dispatch and reach `validated` via real Docker validation |
| 8a | Next.js core pages + API client | ✅ built and verified live in the real browser (not just built/compiled) — start a run via the form, see it appear in the run list, see its file_tasks with real status, real OpenRouter + Docker validation completing end to end, polling reflecting status without a page refresh |
| 8b | SSE live progress + review queue UI | ✅ built and verified live — real EventSource-driven updates (not polling), review queue approve/reject/modify all tested through the actual browser against the real backend, `modified` proven to re-dispatch real validation and interact correctly with the Phase 0 confidence gate. One known cosmetic rough edge left undone, documented above |
| 9 | GitHub PR Agent | ✅ built and tested — mocked GitHub API only (no real `GITHUB_TOKEN`, nothing pushed/PR'd against any real repo, confirmed explicitly); real local git branch/commit/diff-application verified against a real (throwaway) checkout; PR-eligibility contract, exclusion listing, and idempotency (no duplicate PR) all tested |
| 10 | Redis semantic cache | ✅ cache layer built and tested against real Redis/Postgres/the real local embedding model (`all-MiniLM-L6-v2`, 384-dim confirmed empirically). Not yet wired into `migrate_file_task`'s actual call path — Step 4 never built RAG retrieval in the first place, so there's nothing existing to wire it into yet; documented as a known follow-up, not silently left out |

See `TASKS.md` for per-step session scoping and detailed done-criteria.

**Before Step 3 starts:** run `alembic upgrade head` to apply `0002`, and
update `models.py`'s `FileTask` class to add the four new columns
(`line_start`, `line_end`, `matched_symbol`, `changelog_event_id`) and the
`changelog_event` relationship — the migration alone does not update the ORM
model. This is the first action of the Step 3 session (see the kickoff
prompt).

## 6. When to stop and ask (per step)

Stop and ask the user before proceeding, rather than guessing, when:
- A step's done-criteria in `TASKS.md` can't be fully met without a decision
  not yet made in this file.
- You're about to change a column, constraint, or table beyond what `0002`
  already specifies — write a new numbered migration, never hand-edit an
  applied one.
- You're about to add a new `status` value to either enum — the check
  constraints only allow the five listed values each; don't work around them
  by writing a status string outside that list.
- A test fixture doesn't exist yet and creating one requires assuming what a
  "realistic" breaking change looks like.
- Anything in this file appears to contradict the actual code state.

Do not silently reinterpret scope to make a step "complete." An honest partial
result with a named gap is correct behavior, not a failure.
