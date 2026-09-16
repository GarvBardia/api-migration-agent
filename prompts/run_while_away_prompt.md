# Run-while-away prompt — paste this whole thing as your first message

I'm stepping away for about 2 hours and won't be able to answer questions
during that time. Work autonomously. Read `CLAUDE.md` and `TASKS.md` in this
folder first, then follow the plan below in order.

**Ground rule for this unsupervised session:** normally you should stop and
ask before an ambiguous decision. I can't answer right now, so instead:
make the most reasonable decision yourself, write down what you chose and
why in your running notes, and keep going — unless the decision involves
something genuinely risky (deleting data, spending real money, pushing to a
real GitHub repo, or anything outside this local project folder). For those,
stop, don't do it, and note it clearly in your final summary instead of
guessing. Local file edits, local database changes in this dev database,
and local test runs are all fine to do without asking.

If you hit a wall you can't resolve after a couple of honest attempts, don't
loop on it — write down exactly what failed, what you tried, and move on to
whatever else in the plan doesn't depend on it. A clear "I got stuck here
and here's why" is far more useful to me than silence or a fake success.

## Phase 1 — Environment setup

1. Check whether Docker is running (e.g. `docker info`). If it's not
   running, stop and note this clearly at the top of your final summary —
   you can't proceed without it, and there's no point continuing to phases
   that need a database.
2. Start Postgres and Redis only: `docker compose up -d postgres redis`
   (not the full stack — there's no `backend/Dockerfile` yet, that's
   expected, don't try to fix it now, it's out of scope for this session).
3. Confirm both are healthy (`docker compose ps`).
4. In `backend/`, create a Python virtual environment, activate it, and
   `pip install -r requirements.txt`.
5. Copy `.env.example` to `.env` at the project root if `.env` doesn't
   already exist. The default values should work for local dev as-is.
6. Run `alembic upgrade head` from `backend/` to apply both migrations
   (`0001_initial_schema` and `0002_file_task_match_metadata`). Confirm the
   new columns on `file_tasks` (`line_start`, `line_end`, `matched_symbol`,
   `changelog_event_id`) actually exist afterward — don't just trust that
   the command exited without error, check.
7. Update `backend/db/models.py`'s `FileTask` class to add those same four
   columns and the `changelog_event` relationship, matching what `0002`
   added to the database. The migration alone doesn't update the ORM model.

If any step in Phase 1 fails, fix what you reasonably can yourself (missing
system libraries, etc.) and note anything you couldn't fix.

## Phase 2 — Step 3: the Impact Analysis Agent

Once Phase 1 is done, open `prompts/step3_kickoff_prompt.md` in this same
folder and follow it in full — it has the detailed spec, the test fixtures
to build, and the done-criteria checklist. Build the tree-sitter scanner,
write the fixture repo and expected-results file it describes, and run the
tests yourself to confirm they pass before calling this done.

You have permission to reorganize `backend/app/` however fits idiomatic
Python/FastAPI structure as you build, and to edit `CLAUDE.md`/`TASKS.md`
directly if you find another mismatch between what they describe and what
the real code does — leave a one-line note at the top of whatever section
you changed.

## When you're done (or when you stop for the day)

Write a clear summary as your final message covering:
- What you completed, matched against Step 3's done-criteria checklist item
  by item (yes/no/partial for each).
- Any decision you made autonomously that I should know about, and why.
- Anything you got stuck on, exactly what you tried, and what's needed to
  unblock it.
- Anything you changed in `CLAUDE.md` or `TASKS.md` and why.

Do not start Step 4 or touch the Migration Agent, orchestrator, or frontend
in this session — Step 3 only.
