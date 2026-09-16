# Step 3 Kickoff Prompt — paste this as the first message in a new Claude Code session

---

Read `CLAUDE.md` and `TASKS.md` in the repo root before doing anything else. This
session builds **Step 3 only: the Impact Analysis Agent (tree-sitter AST
scanner)**. Do not start Step 4 or touch the Migration Agent, orchestrator, or
frontend in this session.

## You may reorganize, and you may edit the docs

The folder layout under `backend/app/` is a minimal starting scaffold, not a
fixed contract — organize it however fits idiomatic FastAPI/Python project
structure as you build. Note any structural decision briefly in your summary
at the end so it's easy to follow later.

`CLAUDE.md` and `TASKS.md` were written and cross-checked against the real
`models.py`/migrations as of today, but if you find another mismatch between
what they describe and what the code actually does — a column, a status
value, a file path — **edit the doc to match reality and leave a one-line
note at the top of the section you changed** (what was wrong, what you fixed
it to). Don't silently work around a mismatch in code while leaving the doc
wrong; the doc is what the next session reads first.

This is different from a schema/database change: if fixing a doc mismatch
would require altering `file_tasks`/`human_review_queue`/etc. beyond what
migration `0002` already does, stop and report it instead of writing a new
migration yourself — that decision goes back to the user.

Target language for scanned/migrated codebases is **Python-only** for this
project (confirmed). Use the `tree-sitter-python` grammar; don't build partial
JS/TS support.

## Step 0 — apply the pending migration first

Before writing any scanner code:
1. Run `alembic upgrade head` to apply `0002_file_task_match_metadata` (adds
   `line_start`, `line_end`, `matched_symbol`, `changelog_event_id` to
   `file_tasks`, and replaces the old one-row-per-file uniqueness constraint
   with one-row-per-match).
2. Update `models.py`'s `FileTask` class to add those four columns and the
   `changelog_event` relationship (the migration only changes the DB, not
   the ORM).
3. Confirm with a quick query (e.g. `\d file_tasks` in psql, or a SQLAlchemy
   `inspect`) that the new columns and constraint exist before proceeding.

If step 0 can't be completed cleanly (e.g. the migration fails to apply),
stop and report the exact error — don't work around it by writing
match-location data into an existing text column as a workaround.

## What to build

A module (`app/agents/impact_analysis.py` or similar — follow the existing
repo layout) that, given:
- a `run_id` with a target repo already checked out locally,
- the relevant rows in `changelog_events` for that run's `api_name` /
  `version_from` / `version_to`,

does the following:

1. Parse every source file in the target repo with tree-sitter (skip
   `venv/`, `node_modules/`, `.git/`, and your own `tests/fixtures/`
   directory — don't scan your own repo's fixtures as if they were the
   target).
2. For each `changelog_events` row, search for AST references to its
   `old_signature`: direct calls, attribute access (`module.symbol(...)`),
   aliased imports (`from x import symbol as s`), and instantiations.
3. For **each individual match** (not each file), write one `file_tasks`
   row:
   - `run_id`, `file_path`, `status='pending'`
   - `matched_symbol` (the actual symbol name matched)
   - `line_start`, `line_end`
   - `changelog_event_id` (FK to the `changelog_events` row that triggered
     the match)
   - `confidence_score`:
     - **High (≥0.8):** direct, statically-resolvable call/reference.
     - **Medium (0.4–0.8):** resolvable but through an alias or re-export.
     - **Low (<0.4):** plausible match the scanner can't statically confirm
       — e.g. via `getattr`, a symbol stored in a variable and called
       indirectly, or `**kwargs`/`*args` spreading. Write these, don't drop
       them — they're what `human_review_queue` exists for downstream.
   A file with 3 separate matches produces 3 rows, all sharing `file_path`
   and `run_id` but differing in `matched_symbol`/`line_start`.
4. No regex anywhere in the matching path. Tree-sitter query/traversal only.
   (Regex is fine only for trivial non-semantic filtering, like filenames by
   extension.)
5. Re-running the scan for the same `run_id` must not create duplicate
   `file_tasks` rows — rely on the DB's `uq_file_tasks_run_path_symbol_line`
   constraint via upsert, or check for an existing row first.

## Test fixtures you need to create

Build a small fixture repo under `tests/fixtures/step3_sample_repo/`:

```
tests/fixtures/step3_sample_repo/
  service_a.py         # 2 direct calls to the deprecated function
  service_b.py         # 1 call via an aliased import
  multi_match.py        # 2 DIFFERENT deprecated calls in the SAME file —
                         # tests that 0002's match-level granularity works
  utils/helpers.py      # 1 call via a re-exported wrapper (medium confidence)
  dynamic_dispatch.py   # 1 call via getattr(module, "deprecated_fn")(...) (low confidence)
  unrelated.py          # 0 matches — contains a LOCAL function with the SAME
                         # NAME as the target symbol but is unrelated (tests
                         # that matching resolves imports/scope, not just
                         # bare symbol name — this is the likelier failure
                         # point, more than the dynamic-dispatch case)
```

Add a fixture-loading helper that inserts the corresponding `changelog_events`
row(s) for the deprecated symbol(s) these files reference, so tests don't
depend on live data.

Write a hand-labeled expected-results file
(`tests/fixtures/step3_expected.json`) listing exactly which matches should
be found, at which file/line/confidence tier, so the test asserts against
ground truth.

## Done criteria — do not report this step complete until all of these hold

1. Running the scanner against the fixture repo produces exactly the matches
   in `step3_expected.json` — no missed matches, no false positives on
   `unrelated.py`.
2. `multi_match.py` produces two distinct `file_tasks` rows, both persisted
   successfully (proves the `0002` constraint change actually works end to
   end, not just that the migration applied).
3. `dynamic_dispatch.py`'s match is present with low confidence, not
   silently dropped or incorrectly marked high-confidence.
4. Aliased import in `service_b.py` resolves to the same `matched_symbol` as
   the direct calls.
5. `unrelated.py`'s local same-named function produces zero matches.
6. Re-running the scan twice against the same `run_id` results in the same
   row count both times (idempotency test).
7. No regex appears in the AST-matching code path — grep `import re` in the
   new module and justify any hit that isn't filename filtering.
8. Unit tests for all of the above pass and are included in the diff.

If any of these can't be met, stop, report exactly which criterion failed
and why, and propose the smallest fix. Do not mark the step done with a
caveat buried in a comment.

## Explicitly out of scope for this session

- Calling the Migration Agent or Claude's tool-use API (Step 4).
- Writing to `human_review_queue` directly (that's a Step 5/6 concern based
  on validation outcome — Step 3 only sets low `confidence_score` on
  `file_tasks`).
- Orchestration/Celery wiring (Step 6).
