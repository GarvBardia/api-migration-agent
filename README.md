# Confide

Confide fixes your code when a library it depends on changes.

When a library releases a version that breaks old code, Confide finds the
affected lines, writes a fix for each one, and tests every fix before you
rely on it. Fixes it isn't sure about go to a review queue for a person to
approve, reject, or rewrite. It never edits your files directly.

Live demo: https://garvbardia.github.io/api-migration-agent/
The demo runs on a single machine and works only while that machine is on.

## How it works

1. Reads the recorded changes to a library between two versions.
2. Scans your code's structure (not just its text) for every use of what
   changed.
3. Asks an AI model to write replacement code for each use.
4. Runs your project's own tests against each fix, on a copy of your code,
   in a sandbox with no internet access. Retries with the error message if
   the tests fail, up to three attempts.
5. Marks the fix validated, or sends it to the review queue.

## Stack

Python, FastAPI, Celery and Redis, PostgreSQL with pgvector, tree-sitter,
Docker for the test sandbox, and a Next.js frontend.

## Running it

See `PROJECT_STATUS.md` for a plain-language overview and the exact steps
to run it locally or restart the public demo. See `CLAUDE.md` for the full
technical record.

The repository and code are named `api-migration-agent` / `migration-agent`
for historical reasons. The product name is Confide.
