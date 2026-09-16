"""Driver for the 10-case real-world test battery
(tests/REAL_WORLD_BATTERY_REPORT.md). NOT a pytest file (no `test_`
prefix, deliberately -- pytest won't collect it) -- this makes real,
rate-limited calls to a real free-tier LLM and drives real Docker
sandbox validation runs, so it's an on-demand stress-test tool, not part
of the regular automated suite.

Run directly (containerized backend must already be up -- `docker
compose up -d` from the project root):

    cd backend
    venv\\Scripts\\python.exe tests\\real_world_battery_runner.py

Seeds 10 changelog_events, one per real breaking change, then drives each
through the real containerized pipeline (POST /runs against the actual
`backend` container, exactly like the containerization session's own
live verification) SEQUENTIALLY -- one case's run completes (or times
out) before the next one starts, per the kickoff's explicit "bounded
timeouts" instruction. Writes results incrementally to
`real_world_battery_results.json` (so a partial run is recoverable) and,
at the end, `../REAL_WORLD_BATTERY_REPORT.md`.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# `from db import ...`/`from app import ...` resolve because every other
# test in this suite runs via `python -m pytest` from `backend/`, which
# prepends the CWD to sys.path automatically. This script is meant to be
# run directly (`python tests/real_world_battery_runner.py`), not via
# `-m`, so it needs the same path added explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API_BASE = "http://localhost:8000"
PER_CASE_TIMEOUT_SECONDS = 240
POLL_INTERVAL_SECONDS = 3

RESULTS_PATH = Path(__file__).parent / "real_world_battery_results.json"
REPORT_PATH = Path(__file__).parent.parent.parent / "REAL_WORLD_BATTERY_REPORT.md"

# Container-visible path prefix (celery-worker/backend bind-mount
# ./backend:/app) -- these fixtures live on the host at
# backend/tests/fixtures/real_world_battery/..., which the containers see
# at /app/tests/fixtures/real_world_battery/...
FIXTURE_PREFIX = "/app/tests/fixtures/real_world_battery"

CASES = [
    {
        "case": "01",
        "library": "OpenAI SDK",
        "change": "ChatCompletion.create -> client.chat.completions.create",
        "fixture_dir": "case_01_openai_chatcompletion",
        "old_signature": "openai.ChatCompletion.create",
        "new_signature": "client.chat.completions.create",
        "migration_notes": (
            "OpenAI Python SDK v1.0 (Nov 2023) replaced the module-level "
            "openai.ChatCompletion.create(...) call with an instantiated "
            "client: client = OpenAI(); client.chat.completions.create(...)."
        ),
        "version_from": "0.28",
        "version_to": "1.51",
        "expected_scanner_result": "MISS (documented two-level-chain scanner limitation)",
    },
    {
        "case": "02",
        "library": "OpenAI SDK",
        "change": "Embedding.create -> client.embeddings.create",
        "fixture_dir": "case_02_openai_embedding",
        "old_signature": "openai.Embedding.create",
        "new_signature": "client.embeddings.create",
        "migration_notes": (
            "OpenAI Python SDK v1.0 replaced openai.Embedding.create(...) "
            "with client.embeddings.create(...) on an instantiated client."
        ),
        "version_from": "0.28",
        "version_to": "1.51",
        "expected_scanner_result": "MISS (documented two-level-chain scanner limitation)",
    },
    {
        "case": "03",
        "library": "pandas",
        "change": "pandas.io.json.json_normalize -> pandas.json_normalize",
        "fixture_dir": "case_03_pandas_json_normalize",
        "old_signature": "pandas.io.json.json_normalize",
        "new_signature": "pandas.json_normalize",
        "migration_notes": (
            "pandas 1.0 moved json_normalize from the internal "
            "pandas.io.json module to the top-level pandas namespace."
        ),
        "version_from": "0.25",
        "version_to": "2.2",
        "expected_scanner_result": "MATCH",
    },
    {
        "case": "04",
        "library": "pandas",
        "change": "pandas.SparseDataFrame removed -> .sparse accessor",
        "fixture_dir": "case_04_pandas_sparse_dataframe",
        "old_signature": "pandas.SparseDataFrame",
        "new_signature": "pandas.DataFrame(...).astype(pd.SparseDtype(...))",
        "migration_notes": (
            "pandas 1.0 removed SparseDataFrame/SparseSeries entirely, "
            "replaced by the .sparse accessor on a regular DataFrame/"
            "Series backed by a SparseDtype column."
        ),
        "version_from": "0.25",
        "version_to": "2.2",
        "expected_scanner_result": "MATCH",
    },
    {
        "case": "05",
        "library": "NumPy",
        "change": "numpy.float removed -> builtin float",
        "fixture_dir": "case_05_numpy_float",
        "old_signature": "numpy.float",
        "new_signature": "float",
        "migration_notes": (
            "numpy.float (a deprecated alias for the Python builtin "
            "float) was removed in NumPy 1.24 -- use the builtin float "
            "directly."
        ),
        "version_from": "1.20",
        "version_to": "2.1",
        "expected_scanner_result": "MATCH",
    },
    {
        "case": "06",
        "library": "Pydantic",
        "change": "@validator -> @field_validator",
        "fixture_dir": "case_06_pydantic_validator",
        "old_signature": "pydantic.validator",
        "new_signature": "pydantic.field_validator",
        "migration_notes": (
            "Pydantic v1's @validator decorator is deprecated in v2 in "
            "favor of @field_validator, which requires @classmethod and "
            "a mode= argument for pre/post behavior."
        ),
        "version_from": "1.10",
        "version_to": "2.9",
        "expected_scanner_result": "MATCH",
    },
    {
        "case": "07",
        "library": "Pydantic",
        "change": "parse_obj_as -> TypeAdapter(...).validate_python",
        "fixture_dir": "case_07_pydantic_parse_obj_as",
        "old_signature": "pydantic.parse_obj_as",
        "new_signature": "pydantic.TypeAdapter(...).validate_python",
        "migration_notes": (
            "Pydantic v1's parse_obj_as(Type, data) is deprecated in v2 "
            "in favor of TypeAdapter(Type).validate_python(data)."
        ),
        "version_from": "1.10",
        "version_to": "2.9",
        "expected_scanner_result": "MATCH",
    },
    {
        "case": "08",
        "library": "SQLAlchemy",
        "change": "ext.declarative.declarative_base -> orm.declarative_base",
        "fixture_dir": "case_08_sqlalchemy_declarative_base",
        "old_signature": "sqlalchemy.ext.declarative.declarative_base",
        "new_signature": "sqlalchemy.orm.declarative_base",
        "migration_notes": (
            "sqlalchemy.ext.declarative.declarative_base was moved to "
            "sqlalchemy.orm.declarative_base in SQLAlchemy 1.4; the old "
            "import path is deprecated."
        ),
        "version_from": "1.3",
        "version_to": "2.0",
        "expected_scanner_result": "MATCH",
    },
    {
        "case": "09",
        "library": "Flask / MarkupSafe",
        "change": "flask.Markup removed -> markupsafe.Markup",
        "fixture_dir": "case_09_flask_markup",
        "old_signature": "flask.Markup",
        "new_signature": "markupsafe.Markup",
        "migration_notes": (
            "flask.Markup was a re-export of markupsafe.Markup, removed "
            "from the flask namespace entirely in Flask 2.3 -- import "
            "from markupsafe directly."
        ),
        "version_from": "2.2",
        "version_to": "3.0",
        "expected_scanner_result": "MATCH",
    },
    {
        "case": "10",
        "library": "urllib3",
        "change": "contrib.pyopenssl.inject_into_urllib3 removed",
        "fixture_dir": "case_10_urllib3_pyopenssl",
        "old_signature": "urllib3.contrib.pyopenssl.inject_into_urllib3",
        "new_signature": "(removed -- urllib3 v2 uses stdlib ssl directly, no replacement call needed)",
        "migration_notes": (
            "urllib3 v2 dropped pyOpenSSL support entirely (stdlib ssl "
            "now handles SNI universally) -- urllib3.contrib.pyopenssl, "
            "including inject_into_urllib3(), no longer exists. There is "
            "no direct 1:1 replacement call; the correct fix is to stop "
            "calling it."
        ),
        "version_from": "1.26",
        "version_to": "2.2",
        "expected_scanner_result": "MATCH",
    },
]


def _post_json(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _get_json(path: str) -> dict:
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=30) as resp:
        return json.loads(resp.read())


def seed_changelog_event(case: dict) -> str:
    """Inserts directly via SQLAlchemy (not through any API -- there's no
    changelog-ingestion endpoint; every other step's tests seed this table
    directly too, same pattern)."""

    from db import SessionLocal
    from db.models import ChangelogEvent

    api_name = f"realworld-case{case['case']}-{uuid.uuid4().hex[:6]}"
    session = SessionLocal()
    try:
        event = ChangelogEvent(
            api_name=api_name,
            version_from=case["version_from"],
            version_to=case["version_to"],
            change_type="removal",
            old_signature=case["old_signature"],
            new_signature=case["new_signature"],
            migration_notes=case["migration_notes"],
        )
        session.add(event)
        session.commit()
        return api_name
    finally:
        session.close()


def run_one_case(case: dict) -> dict:
    api_name = seed_changelog_event(case)
    repo_url = f"{FIXTURE_PREFIX}/{case['fixture_dir']}"

    result: dict = {
        "case": case["case"],
        "library": case["library"],
        "change": case["change"],
        "api_name": api_name,
        "expected_scanner_result": case["expected_scanner_result"],
    }

    start = time.monotonic()
    try:
        resp = _post_json(
            "/runs",
            {
                "repo_url": repo_url,
                "api_name": api_name,
                "version_from": case["version_from"],
                "version_to": case["version_to"],
            },
        )
    except Exception as exc:  # noqa: BLE001 -- report, don't crash the battery
        result["outcome"] = "DISPATCH_ERROR"
        result["detail"] = repr(exc)
        result["elapsed_seconds"] = round(time.monotonic() - start, 1)
        return result

    run_id = resp["run_id"]
    result["run_id"] = run_id

    run_status = None
    while time.monotonic() - start < PER_CASE_TIMEOUT_SECONDS:
        try:
            run = _get_json(f"/runs/{run_id}")
        except Exception:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        run_status = run["status"]
        if run_status in ("completed", "failed"):
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    elapsed = round(time.monotonic() - start, 1)
    result["elapsed_seconds"] = elapsed

    if run_status not in ("completed", "failed"):
        result["outcome"] = "TIMEOUT"
        result["run_status_at_timeout"] = run_status
        return result

    result["outcome"] = run_status
    try:
        file_tasks = _get_json(f"/runs/{run_id}/file_tasks")
    except Exception as exc:  # noqa: BLE001
        result["file_tasks"] = []
        result["file_tasks_error"] = repr(exc)
        return result

    result["file_tasks"] = [
        {
            "file_path": t["file_path"],
            "status": t["status"],
            "confidence_score": t["confidence_score"],
            "test_pass_count": t["test_pass_count"],
            "test_fail_count": t["test_fail_count"],
            "retry_count": t["retry_count"],
            "failure_reason": t["failure_reason"],
        }
        for t in file_tasks
    ]
    return result


def main() -> None:
    results = []
    if RESULTS_PATH.exists():
        print(f"Resuming: {RESULTS_PATH} already has partial results, overwriting fresh.")

    for case in CASES:
        print(f"--- case {case['case']}: {case['library']} — {case['change']} ---")
        result = run_one_case(case)
        print(f"    outcome={result['outcome']} elapsed={result['elapsed_seconds']}s")
        results.append(result)
        RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))

    print(f"\nAll {len(results)} cases done. Results written to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
