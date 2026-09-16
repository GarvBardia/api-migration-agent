"""Tests for the gap-fix session: wiring Step 10's semantic cache into the
Migration Agent (`migrate_file_task`).

Since nothing populates `api_doc_chunks` from real API documentation (no
ingestion agent exists in the 10-step plan -- see CLAUDE.md's dated note),
these tests seed `api_doc_chunks` rows directly as test fixture data. This
stands in for a real ingestion pipeline; it is not a claim that real docs
are retrieved in production use today.

Runs against real Postgres and real Redis (same instances every other
step's tests use), and the real local embedding model (`all-MiniLM-L6-v2`)
-- consistent with how Step 10's own tests were verified.
"""

from __future__ import annotations

import json
import types
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import redis as redis_lib

from app.agents.llm_providers import ClaudeAdapter, MigrationEdit, OpenRouterAdapter
from app.agents.migration import migrate_file_task
from app.agents.semantic_cache import embed_text

SAMPLE_REPO = Path(__file__).parent / "fixtures" / "step3_sample_repo"
REDIS_URL = "redis://localhost:6379/0"

FAKE_NEW_CODE = "oldapi.new_call(1, 2)"
FAKE_RATIONALE = "Renamed legacy_call to new_call per the 2.0 signature change."


def _session():
    from db import SessionLocal

    return SessionLocal()


def _redis_client():
    return redis_lib.from_url(REDIS_URL, decode_responses=True)


def _fresh_api_name() -> str:
    return f"docctx-{uuid.uuid4().hex[:8]}"


def _make_doc_chunk(session, api_name: str, chunk_text: str):
    from db.models import ApiDocChunk

    chunk = ApiDocChunk(
        api_name=api_name,
        version="2.0",
        chunk_text=chunk_text,
        embedding=embed_text(chunk_text),
    )
    session.add(chunk)
    session.commit()
    return chunk


def _cleanup(api_name: str, run_id=None) -> None:
    from db.models import ApiDocChunk, ChangelogEvent, FileTask, MigrationRun

    session = _session()
    try:
        if run_id is not None:
            session.query(FileTask).filter_by(run_id=run_id).delete(
                synchronize_session=False
            )
            session.query(MigrationRun).filter_by(id=run_id).delete(
                synchronize_session=False
            )
        session.query(ApiDocChunk).filter_by(api_name=api_name).delete(
            synchronize_session=False
        )
        session.query(ChangelogEvent).filter_by(api_name=api_name).delete(
            synchronize_session=False
        )
        session.commit()
    finally:
        session.close()


def _make_run_event_task(session, api_name: str, old_signature: str, migration_notes=None):
    from db.models import ChangelogEvent, FileTask, MigrationRun

    run = MigrationRun(
        repo_url=str(SAMPLE_REPO),
        api_name=api_name,
        version_from="1.x",
        version_to="2.0",
        status="running",
    )
    session.add(run)
    session.flush()
    event = ChangelogEvent(
        api_name=api_name,
        version_from="1.x",
        version_to="2.0",
        change_type="deprecation",
        old_signature=old_signature,
        new_signature="oldapi.new_call",
        migration_notes=migration_notes,
    )
    session.add(event)
    session.flush()
    task = FileTask(
        run_id=run.id,
        file_path="service_a.py",
        status="pending",
        matched_symbol="legacy_call",
        line_start=7,
        line_end=7,
        confidence_score=0.95,
        changelog_event_id=event.id,
    )
    session.add(task)
    session.flush()
    session.commit()
    return run, event, task


class _CapturingProvider:
    """Records the kwargs it was called with, including doc_context --
    used to prove doc_context actually reaches propose_edit, not just that
    retrieval "happened" somewhere."""

    def __init__(self):
        self.calls: list[dict] = []

    def propose_edit(self, **kwargs):
        self.calls.append(kwargs)
        return MigrationEdit(new_code=FAKE_NEW_CODE, rationale=FAKE_RATIONALE)


# ---------------------------------------------------------------------------
# Done-criterion 2 (adapter half): doc_context reaches the constructed
# prompt/messages sent to each real provider's API shape.
# ---------------------------------------------------------------------------


class TestAdapterPromptIncludesDocContext:
    def test_claude_prompt_includes_doc_context(self, monkeypatch):
        marker = "UNIQUE_DOC_CONTEXT_MARKER_CLAUDE_12345"
        adapter = ClaudeAdapter(api_key="test-key")

        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            block = types.SimpleNamespace(
                type="tool_use",
                name="propose_edit",
                input={"new_code": FAKE_NEW_CODE, "rationale": FAKE_RATIONALE},
            )
            return types.SimpleNamespace(content=[block], stop_reason="tool_use")

        monkeypatch.setattr(adapter._client.messages, "create", fake_create)

        adapter.propose_edit(
            old_code="oldapi.legacy_call(1, 2)",
            old_signature="oldapi.legacy_call",
            new_signature="oldapi.new_call",
            doc_context=marker,
        )

        sent_prompt = captured["messages"][0]["content"]
        assert marker in sent_prompt

    def test_openrouter_prompt_includes_doc_context(self, monkeypatch):
        marker = "UNIQUE_DOC_CONTEXT_MARKER_OPENROUTER_67890"
        adapter = OpenRouterAdapter(api_key="test-key", model="cohere/north-mini-code:free")

        import app.agents.llm_providers.openrouter_adapter as ora

        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            response = MagicMock()
            response.raise_for_status = MagicMock()
            response.json = MagicMock(
                return_value={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "propose_edit",
                                            "arguments": __import__("json").dumps(
                                                {
                                                    "new_code": FAKE_NEW_CODE,
                                                    "rationale": FAKE_RATIONALE,
                                                }
                                            ),
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
            return response

        monkeypatch.setattr(ora.httpx, "post", fake_post)

        adapter.propose_edit(
            old_code="oldapi.legacy_call(1, 2)",
            old_signature="oldapi.legacy_call",
            new_signature="oldapi.new_call",
            doc_context=marker,
        )

        sent_prompt = captured["payload"]["messages"][1]["content"]
        assert marker in sent_prompt


# ---------------------------------------------------------------------------
# Done-criterion 2 (integration half): a real file_tasks row triggers
# retrieval, finds a seeded api_doc_chunks row via the cache, and the
# chunk's content reaches propose_edit.
# ---------------------------------------------------------------------------


class TestMigrateFileTaskRetrievesDocContext:
    def test_seeded_chunk_reaches_propose_edit(self):
        api_name = _fresh_api_name()
        chunk_text = f"legacy_call is deprecated in {api_name}; use new_call instead."

        session = _session()
        _make_doc_chunk(session, api_name, chunk_text)
        run, event, task = _make_run_event_task(
            session, api_name, "oldapi.legacy_call", migration_notes="Use new_call."
        )
        session.close()

        provider = _CapturingProvider()
        redis_client = _redis_client()
        session = _session()
        try:
            migrate_file_task(
                session,
                task,
                SAMPLE_REPO,
                provider,
                redis_client=redis_client,
            )
            assert len(provider.calls) == 1
            doc_context = provider.calls[0]["doc_context"]
            assert doc_context is not None
            assert chunk_text in doc_context
        finally:
            session.close()
            _cleanup(api_name, run.id)

    def test_no_matching_chunks_gives_none_gracefully(self):
        """Zero chunks found -> doc_context=None, not an error -- the
        expected common case until a real ingestion agent exists."""

        api_name = _fresh_api_name()  # deliberately no api_doc_chunks seeded

        session = _session()
        run, event, task = _make_run_event_task(session, api_name, "oldapi.legacy_call")
        session.close()

        provider = _CapturingProvider()
        redis_client = _redis_client()
        session = _session()
        try:
            migrate_file_task(
                session, task, SAMPLE_REPO, provider, redis_client=redis_client
            )
            assert len(provider.calls) == 1
            assert provider.calls[0]["doc_context"] is None
            assert task.status == "in_progress"  # migration still succeeded
        finally:
            session.close()
            _cleanup(api_name, run.id)

    def test_no_redis_client_skips_retrieval_gracefully(self):
        """redis_client=None (the default) -- retrieval is skipped
        entirely, same observable outcome as zero chunks found."""

        api_name = _fresh_api_name()
        session = _session()
        _make_doc_chunk(session, api_name, "some doc chunk text")
        run, event, task = _make_run_event_task(session, api_name, "oldapi.legacy_call")
        session.close()

        provider = _CapturingProvider()
        session = _session()
        try:
            migrate_file_task(session, task, SAMPLE_REPO, provider)  # no redis_client
            assert provider.calls[0]["doc_context"] is None
        finally:
            session.close()
            _cleanup(api_name, run.id)


# ---------------------------------------------------------------------------
# Done-criterion 3: two file_tasks referencing the same symbol -> one
# embedding computation (second is a cache hit), through the REAL call
# path (migrate_file_task), not just the standalone cache module.
# ---------------------------------------------------------------------------


class TestSameSymbolOneEmbeddingComputationThroughRealCallPath:
    def test_second_file_task_is_a_cache_hit(self, monkeypatch):
        import app.agents.semantic_cache as sc

        embed_calls = []
        real_embed = sc.embed_text

        def counting_embed(text):
            embed_calls.append(text)
            return real_embed(text)

        monkeypatch.setattr(sc, "embed_text", counting_embed)

        api_name = _fresh_api_name()
        chunk_text = "legacy_call is deprecated; use new_call instead."

        session = _session()
        _make_doc_chunk(session, api_name, chunk_text)
        # Two DISTINCT file_tasks, same run, both referencing the SAME
        # changelog_event (same old_signature/migration_notes -> the same
        # retrieval query text).
        run, event, task_a = _make_run_event_task(
            session, api_name, "oldapi.legacy_call", migration_notes="Use new_call."
        )
        from db.models import FileTask

        task_b = FileTask(
            run_id=run.id,
            file_path="service_b.py",
            status="pending",
            matched_symbol="legacy_call",
            line_start=7,
            line_end=7,
            confidence_score=0.6,
            changelog_event_id=event.id,
        )
        session.add(task_b)
        session.flush()
        session.commit()
        session.close()

        redis_client = _redis_client()
        session = _session()
        try:
            task_a = session.get(FileTask, task_a.id)
            task_b = session.get(FileTask, task_b.id)

            provider_a = _CapturingProvider()
            migrate_file_task(
                session, task_a, SAMPLE_REPO, provider_a, redis_client=redis_client
            )
            provider_b = _CapturingProvider()
            migrate_file_task(
                session, task_b, SAMPLE_REPO, provider_b, redis_client=redis_client
            )

            assert chunk_text in provider_a.calls[0]["doc_context"]
            assert chunk_text in provider_b.calls[0]["doc_context"]
            # The actual proof: only ONE embedding computation across both
            # file_tasks -- the second's retrieval was served from cache.
            assert len(embed_calls) == 1
        finally:
            session.close()
            _cleanup(api_name, run.id)
