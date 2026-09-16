# Real-world test battery — 10 genuine breaking changes

Run 2026-09-16 against the fully containerized stack (`docker compose up
-d`), via `backend/tests/real_world_battery_runner.py`, sequentially, one
case at a time, each bounded to 240s. All 10 cases reached a terminal
`migration_runs` status within that bound — none timed out. Raw results:
`backend/tests/real_world_battery_results.json`.

Every case is a genuinely real, documented breaking change from the
named library's own changelog — not synthetic. The Migration Agent used
the project's configured free-tier OpenRouter model (no mocking); the
Validation Agent ran real sandboxed `pytest` against the real target
library version in a rebuilt sandbox image (`openai==1.51.0`,
`pandas==2.2.3`, `numpy==2.1.2`, `pydantic==2.9.2`, `sqlalchemy==2.0.35`,
`flask==3.0.3`, `markupsafe==2.1.5`, `urllib3==2.2.3`).

## Results

| # | Library | Old → New | Final status | Confidence | Tests (pass/fail) | Was the fix actually correct? |
|---|---|---|---|---|---|---|
| 1 | OpenAI SDK | `openai.ChatCompletion.create` → `client.chat.completions.create` | **no file_task created** | — | — | N/A — **expected scanner miss** (see below), never reached the Migration Agent |
| 2 | OpenAI SDK | `openai.Embedding.create` → `client.embeddings.create` | **no file_task created** | — | — | N/A — **expected scanner miss**, same cause as #1 |
| 3 | pandas | `pandas.io.json.json_normalize` → `pandas.json_normalize` | needs_review | 0.95 | 0 / 3 | **Inconclusive** — retries exhausted with an uninformative report message (see "Report-parsing gap" below); can't confirm whether the fix content was ever actually right |
| 4 | pandas | `pandas.SparseDataFrame` → `.sparse` accessor | needs_review | 0.75 | 1 / 0 | **Correct** — test passed on the first attempt; held for human review purely by the confidence gate (aliased-import base 0.6 + 0.15 = 0.75, below the 0.8 threshold) — the trust gate working exactly as designed |
| 5 | NumPy | `numpy.float` → builtin `float` | needs_review | 0.75 | 1 / 0 | **Correct** — same shape as #4: passed immediately, held by the confidence gate as designed |
| 6 | Pydantic | `@validator` → `@field_validator` | needs_review | 0.95 | 0 / 0 | **Inconclusive** — never actually validated (see "Stale-patch guard" finding below) |
| 7 | Pydantic | `parse_obj_as` → `TypeAdapter(...).validate_python` | needs_review | 1.0 | 1 / 1 | **Inconclusive** — one pass, one fail, then a retry blocked by an LLM provider failure (see "Provider reliability" below), not by the migration itself |
| 8 | SQLAlchemy | `ext.declarative.declarative_base` → `orm.declarative_base` | **validated** | 1.0 | 1 / 1 | **Correct — full success.** First attempt failed, the agent retried with real failure context, the second attempt passed and cleared the confidence gate. The cleanest complete pipeline run in the battery. |
| 9 | Flask / MarkupSafe | `flask.Markup` → `markupsafe.Markup` | needs_review | 0.95 | 0 / 3 | **Inconclusive** — same report-parsing gap as #3 |
| 10 | urllib3 | `contrib.pyopenssl.inject_into_urllib3` (removed, no direct replacement) | needs_review | 0.95 | 0 / 3 | **Inconclusive** — same report-parsing gap as #3; also the hardest case in the set (no 1:1 replacement call, a genuine judgment call for the LLM) |

**Scoreboard:** 1/10 reached a fully verified `validated` state. 2/10 were correct fixes appropriately held by the confidence gate (not failures — the system doing exactly what it's designed to do). 2/10 were scanner misses, expected and explained. 5/10 ended `needs_review` for reasons that are inconclusive about the fix's actual correctness — three of those five share one root cause below, not five independent problems.

## Real findings from this run

### 1. Two-level attribute-chain scanner limitation (cases 1–2, already known)

`impact_analysis.py`'s AST matcher only resolves single-hop
`module.symbol(...)` calls (an `import`ed module name directly followed
by one attribute access). `openai.ChatCompletion.create(...)` is two
hops (`openai` → `ChatCompletion` → `.create`), and the matcher's
attribute-call branch explicitly requires the call's object to be a bare
identifier bound to a whole-module import — a nested `attribute` node
(itself the object of another attribute access) doesn't qualify, so the
call is silently not found. Both OpenAI cases were deliberately kept in
their real, natural call shape rather than rewritten to dodge this — the
entire OpenAI Python SDK v1.0 migration (arguably the single most common
real-world Python breaking change of the last two years) is built
entirely from this exact two-hop `resource.method()` shape, so this gap
covers a large, important slice of real-world usage. Not fixed this
session; tracked as a known, documented limitation.

### 2. NEW finding — sandbox report-parsing gap on collection errors (cases 3, 9, 10)

Three cases share the identical failure message: *"pytest reported 0
failure(s) but no per-test detail was found in the report
(pass_count=0)"*. Traced to `validation.py`'s `_parse_pytest_report`-style
logic (~line 245): it reads `summary.passed`/`summary.failed`/
`summary.error` and the `tests` list from `pytest-json-report`'s output.
When the sandboxed run fails at **collection time** — e.g. a genuine
`ImportError` because the target library's removed API can't even be
imported, exactly what all three of these real breaking changes cause
pre-migration — `pytest-json-report` doesn't populate the `tests` list
the normal way, so the code falls through to this generic fallback
instead of surfacing the real `ImportError` message. This masks whether
the Migration Agent's actual fix content was correct across all three
retries, or whether the SAME collection error persisted because the
report never told the agent what was actually wrong. **This is a real,
newly-discovered gap, not touched this session** — worth fixing before
trusting `needs_review` reasons from this class of case at face value.

### 3. NEW finding — a `needs_review` idempotency gap silently overwrites the real failure reason (case 6)

Case 6's only file_task shows `retry_count=0`, `test_pass_count=0`,
`test_fail_count=0` — it never actually reached a sandboxed test run. The
recorded failure reason blames a stale patch: *"old_code_snippet no
longer matches the real file at service.py:11-11. Expected None, found
'    @validator(\"name\")'."* **Root-caused precisely** (see CLAUDE.md
§4b Finding #1 for the full mechanism and exact line numbers): this
message is not the real story. `migrate_file_task` hit one of its own
early-return branches (most likely a provider tool-call failure — the
same failure mode independently seen in case 7) and correctly set
`needs_review` with ITS OWN specific, useful reason — but because
`migrate_task`→`validate_task` is an unconditional Celery chain, and
`validate_file_task`'s only idempotency guard checks for `status ==
"validated"` (not `needs_review`), `validate_task` ran anyway,
compared the real file against the still-`None` `old_code_snippet`, and
overwrote the real reason with this generic, misleading one. This is the
SAME guard gap already documented in CLAUDE.md §4a (found earlier this
session via duplicate review-queue rows) — this battery shows it has a
second, worse consequence beyond duplication: it can destroy the actual
diagnostic information a human would need. **Newly discovered this
session, not fixed** — CLAUDE.md §4b now recommends prioritizing the
fix that closes this gap.

### 4. Free-tier LLM provider reliability (case 7)

The second migration attempt for case 7 failed with *"LLM provider error:
OpenRouter response did not contain a propose_edit tool call"* — the
configured free reasoning model spent its entire response budget on
visible chain-of-thought reasoning text and never actually emitted the
required tool call. This is a genuine reliability characteristic of the
free-tier model this project uses, not a bug in this project's own code
— flagged for awareness, not something to "fix" here.

## What this confirms works, for real

- The full pipeline — real scan, real free-tier LLM call, real Docker
  sandbox validation, real confidence gate, real review-queue routing —
  runs correctly end to end against genuine, unmodified real-world
  library code (case 8's full `validated` result, and cases 4/5's
  correctly-gated passes).
- The confidence gate does exactly what it's designed to do: a fix that
  passes its test isn't blindly trusted just because it compiles and
  runs (cases 4 and 5).
- The retry loop genuinely re-attempts with real failure context, not
  just a retry of the identical prompt (case 8's successful second
  attempt).
