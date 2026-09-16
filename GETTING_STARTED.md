# Getting Started — read this first

Written for the Claude Code **desktop app** (not the terminal version). If
you've never set up a project like this, follow this top to bottom.

---

## Part 0 — What this is, in one paragraph

You give it a code repo and a library that's changing version. It reads the
library's changelog, scans your code with a real parser to find every place
that uses the old, breaking version, asks Claude to rewrite those spots,
tests the rewrite in a sandboxed container, and opens a GitHub PR if it
passes. Anything it's unsure about goes to a human review queue instead of
being guessed at.

## Part 1 — Money: what's free, what isn't, right now

| Piece | Free? |
|---|---|
| Docker, Postgres, Redis running on your own machine | ✅ Free |
| Python, Git | ✅ Free |
| **Today's work (Step 3, the code scanner)** | ✅ Free — it never calls any AI API. It's a parser reading files. $0 no matter how many times you run it. |
| Claude Code itself, to build the project | ❌ Needs a paid plan (Pro, $20/mo) or API credits. Free trial messages exist but run out fast on a real project. |
| Step 4 onward — the part of *your app* that calls an AI model to generate the actual code fixes | ❌ Costs something per call — **unless you use a free-tier provider** (see Part 7). |

**Today, right now, costs you nothing beyond whatever your Claude Code
session itself costs** — Step 3 doesn't touch any AI API at all.

## Part 2 — What's in the zip

```
migration-agent/
  CLAUDE.md                          <- Claude Code reads this every session automatically.
  TASKS.md                           <- The remaining 8 build steps, one session each.
  GETTING_STARTED.md                 <- This file.
  docker-compose.yml / .env.example
  prompts/
    step3_kickoff_prompt.md          <- Manual version, step by step.
    run_while_away_prompt.md         <- Paste-and-leave version — use this one today.
  backend/
    requirements.txt, alembic.ini
    db/  (your 5 tables + migrations)
    app/  (empty — Claude Code fills this in)
    tests/fixtures/  (empty — Claude Code fills this in)
  frontend/  (empty, not needed yet)
```

## Part 3 — One-time setup (5 minutes, do this before you leave)

1. **Docker Desktop** must be installed and *open/running* —
   https://www.docker.com/products/docker-desktop/. Just open the app once;
   you don't need to type anything into it.
2. **Python 3.12** installed — https://www.python.org/downloads/
3. Unzip `migration-agent.zip` somewhere you'll remember (e.g. Desktop).
4. Open the **Claude Code desktop app**. Use its "Open Folder" / "Open
   Project" option and point it at the `migration-agent` folder you just
   unzipped — not a subfolder, the top-level one (so it can see `CLAUDE.md`).

## Part 4 — Turn on autonomous mode before you paste anything

By default, Claude Code stops and asks your permission before running each
terminal command or editing certain files. If you're about to walk away for
two hours, that's a problem — it'll sit there waiting for a click.

Look in the desktop app's **Settings / permissions** for an option like
"auto-accept edits," "autonomous mode," or a permission-mode toggle in the
chat input area itself (often a small mode switch near the send button).
Turn that on for this session. If you can't find it, at minimum accept the
first few tool-use prompts it shows you so it learns the pattern — but a
true "leave it running" session needs the auto-accept mode found, since
otherwise it will pause.

## Part 5 — Paste this prompt, then go

Open `prompts/run_while_away_prompt.md`, copy the whole thing, paste it as
your first message in the Claude Code desktop app, and send it. That's the
"run whilst I'm gone" prompt — it's written to make its own reasonable
decisions and keep working instead of stopping to ask, and it ends with a
clear written summary for you to review when you're back.

## Part 6 — When you're back (after 12)

Read the summary Claude Code left. Check it against the done-criteria list
at the bottom of `prompts/step3_kickoff_prompt.md` — don't take "done" on
faith, the criteria are written to be checkable. If everything checks out,
Step 3 is finished and you move to Step 4 in `TASKS.md`. If something's
incomplete, the summary should say exactly what and why — that's normal,
not a failure, and it's much better than a step that quietly cut a corner.

## Part 7 — Making the AI-calling steps (Step 4+) free or near-free

Claude's API costs money per call. Two real free alternatives exist for
*your app's* Migration Agent step specifically (this doesn't affect today —
Step 3 has no AI calls at all):

- **Google Gemini API** — genuine free tier, no credit card. Covers the
  Flash and Flash-Lite models, rate-limited (roughly 10–15 requests/minute,
  up to ~250–1,500 requests/day depending on model, as of mid-2026 — check
  Google's current numbers, these move). Caveat: free-tier prompts may be
  used by Google to improve their models, so don't run real client code
  through it if that matters to you — fine for your own portfolio test
  fixtures.
- **Groq API** — genuine free tier, no credit card, very fast inference.
  Only runs open-source models (Llama 3.3, GPT-OSS, etc. — not Claude,
  GPT, or Gemini). Roughly 30 requests/minute, ~1,000 requests/day, as of
  mid-2026 — check Groq's current published limits before relying on it.

Either works for testing the Migration Agent at $0. The trade-off: your
`CLAUDE.md` currently specifies "structured tool calls only, via Claude's
tool-use API" — Gemini and Groq both support function calling with a
similar shape, but the exact schema differs, so Step 4 would need to be
built provider-agnostic (or built against whichever one you pick) rather
than hard-locked to Claude's API. That's a real design decision, not a
free swap-in — best made explicitly when you start Step 4, not silently
assumed. I've left a note for it in `TASKS.md`.

## Part 8 — If something breaks while you're away

The autonomous prompt tells Claude Code to write down what it tried and
stop cleanly rather than loop forever if it gets stuck — read its final
message first before touching anything. Common early issues:
- **Docker wasn't running** when it tried to start Postgres — it should
  report this clearly; open Docker Desktop and you can just re-send the
  prompt.
- **`pip install` fails on `pgvector`/`psycopg`** — usually a missing
  system library; the error message names it.
