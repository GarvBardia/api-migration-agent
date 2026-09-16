"""Tests for the Step 10 Redis semantic cache (`app/agents/semantic_cache.py`).

Runs against the REAL Redis instance (`docker-compose`'s `redis` service,
same one Celery uses) and REAL Postgres, in a dedicated namespace so it
never collides with anything else using that Redis instance. Uses the
REAL local `sentence-transformers` model for the primary call-count test
(genuine end-to-end proof, not a mocked embedding call) -- and a
monkeypatched `embed_text` only where a test needs a controlled,
deterministic vector (the fuzzy-similarity match).
"""

from __future__ import annotations

import time
import uuid

import pytest
import redis as redis_lib

from app.agents.semantic_cache import (
    EMBEDDING_DIMENSION,
    SemanticCache,
    embed_text,
    get_doc_context_for_symbol,
)

REDIS_URL = "redis://localhost:6379/0"


def _session():
    from db import SessionLocal

    return SessionLocal()


def _redis_client():
    return redis_lib.from_url(REDIS_URL, decode_responses=True)


def _fresh_namespace() -> str:
    return f"test_semantic_cache_{uuid.uuid4().hex[:8]}"


def _cleanup_namespace(r, namespace: str) -> None:
    for key in r.scan_iter(f"{namespace}:*"):
        r.delete(key)


def _make_doc_chunk(session, api_name: str, chunk_text: str):
    from db.models import ApiDocChunk

    embedding = embed_text(chunk_text)
    chunk = ApiDocChunk(
        api_name=api_name, version="2.0", chunk_text=chunk_text, embedding=embedding
    )
    session.add(chunk)
    session.commit()
    return chunk


def _cleanup_doc_chunks(api_name: str) -> None:
    from db.models import ApiDocChunk

    session = _session()
    try:
        session.query(ApiDocChunk).filter_by(api_name=api_name).delete()
        session.commit()
    finally:
        session.close()


class TestEmbeddingModel:
    def test_embed_text_returns_confirmed_dimension(self):
        vec = embed_text("oldapi.legacy_call is deprecated")
        assert len(vec) == EMBEDDING_DIMENSION == 384


# ---------------------------------------------------------------------------
# Done-criterion: two file_tasks referencing the same symbol -> one
# embedding computation, one Postgres lookup, not two.
# ---------------------------------------------------------------------------


class TestCacheAvoidsRedundantWork:
    def test_same_query_text_computes_once(self):
        r = _redis_client()
        namespace = _fresh_namespace()
        cache = SemanticCache(r, namespace=namespace)

        compute_calls = []

        def compute_fn(embedding):
            compute_calls.append(embedding)
            return {"chunk_text": "some doc chunk about legacy_call"}

        try:
            result1, source1 = cache.get_or_compute("oldapi:legacy_call", compute_fn)
            result2, source2 = cache.get_or_compute("oldapi:legacy_call", compute_fn)

            assert source1 == "computed"
            assert source2 == "cache"
            assert result1 == result2
            assert len(compute_calls) == 1  # NOT called twice
        finally:
            _cleanup_namespace(r, namespace)

    def test_same_query_computes_embedding_once(self, monkeypatch):
        """Same as above, but proves the EMBEDDING call count too (not just
        the Postgres/compute_fn call count) -- both parts of "one embedding
        computation and one Postgres lookup, not two" are covered."""

        import app.agents.semantic_cache as sc

        embed_calls = []
        real_embed = sc.embed_text

        def counting_embed(text):
            embed_calls.append(text)
            return real_embed(text)

        monkeypatch.setattr(sc, "embed_text", counting_embed)

        r = _redis_client()
        namespace = _fresh_namespace()
        cache = SemanticCache(r, namespace=namespace)
        try:
            cache.get_or_compute("oldapi:legacy_call", lambda e: {"x": 1})
            cache.get_or_compute("oldapi:legacy_call", lambda e: {"x": 1})
            assert len(embed_calls) == 1
        finally:
            _cleanup_namespace(r, namespace)

    def test_get_doc_context_for_symbol_end_to_end(self):
        """Full integration: real Postgres lookup via pgvector, real
        embedding model, real Redis -- two calls for the same symbol hit
        Postgres once (the second call's `source == "cache"` IS the proof:
        `SemanticCache.get_or_compute` only calls `compute_fn` -- which
        wraps `lookup_doc_chunks_in_postgres` -- on a cache miss, by
        construction; no separate spy needed to show the second call
        skipped it)."""

        api_name = f"semcache-{uuid.uuid4().hex[:8]}"
        session = _session()
        _make_doc_chunk(session, api_name, "legacy_call is deprecated, use new_call")
        _make_doc_chunk(session, api_name, "unrelated doc chunk about something else")
        session.close()

        r = _redis_client()
        namespace = _fresh_namespace()
        cache = SemanticCache(r, namespace=namespace)

        session = _session()
        try:
            chunks1, source1 = get_doc_context_for_symbol(
                session, r, api_name, "legacy_call", cache=cache
            )
            chunks2, source2 = get_doc_context_for_symbol(
                session, r, api_name, "legacy_call", cache=cache
            )

            assert source1 == "computed"
            assert source2 == "cache"
            assert len(chunks1) >= 1
            assert chunks1 == chunks2
        finally:
            session.close()
            _cleanup_namespace(r, namespace)
            _cleanup_doc_chunks(api_name)


# ---------------------------------------------------------------------------
# Done-criterion: cache has a defined TTL or invalidation path.
# ---------------------------------------------------------------------------


class TestTTL:
    def test_cached_entry_has_a_ttl(self):
        r = _redis_client()
        namespace = _fresh_namespace()
        cache = SemanticCache(r, namespace=namespace, ttl_seconds=120)
        try:
            cache.get_or_compute("oldapi:some_symbol", lambda e: {"x": 1})
            entry_key = cache._entry_key(_key_for("oldapi:some_symbol"))
            ttl = r.ttl(entry_key)
            assert 0 < ttl <= 120
        finally:
            _cleanup_namespace(r, namespace)


def _key_for(text: str) -> str:
    from app.agents.semantic_cache import _normalize_key

    return _normalize_key(text)


# ---------------------------------------------------------------------------
# Fuzzy similarity match (not just exact-text cache hits).
# ---------------------------------------------------------------------------


class TestFuzzySimilarityMatch:
    def test_near_duplicate_embedding_hits_cache(self, monkeypatch):
        import app.agents.semantic_cache as sc

        # Deterministic, controlled vectors -- avoids depending on the real
        # model happening to consider two specific phrases "similar enough."
        vectors = {
            "query one": [1.0, 0.0, 0.0],
            "query two": [0.99, 0.01, 0.0],  # cosine similarity ~0.999 with the above
        }
        monkeypatch.setattr(sc, "embed_text", lambda text: vectors[text])

        r = _redis_client()
        namespace = _fresh_namespace()
        cache = SemanticCache(r, namespace=namespace, similarity_threshold=0.95)
        calls = []
        try:
            cache.get_or_compute("query one", lambda e: calls.append(1) or {"x": 1})
            result, source = cache.get_or_compute(
                "query two", lambda e: calls.append(1) or {"x": 1}
            )
            assert source == "cache"
            assert len(calls) == 1  # second (similar) query never computed
        finally:
            _cleanup_namespace(r, namespace)

    def test_dissimilar_query_does_not_hit_cache(self, monkeypatch):
        import app.agents.semantic_cache as sc

        vectors = {
            "query one": [1.0, 0.0, 0.0],
            "totally different": [0.0, 1.0, 0.0],  # orthogonal -- similarity 0
        }
        monkeypatch.setattr(sc, "embed_text", lambda text: vectors[text])

        r = _redis_client()
        namespace = _fresh_namespace()
        cache = SemanticCache(r, namespace=namespace, similarity_threshold=0.95)
        calls = []
        try:
            cache.get_or_compute("query one", lambda e: calls.append(1) or {"x": 1})
            result, source = cache.get_or_compute(
                "totally different", lambda e: calls.append(1) or {"x": 2}
            )
            assert source == "computed"
            assert len(calls) == 2  # both computed -- genuinely different queries
        finally:
            _cleanup_namespace(r, namespace)


class TestConcurrentEmbedTextDoesNotDeadlock:
    """Regression test for a real deadlock found live 2026-09-03 (see the
    dated CLAUDE.md note): `_model` in `semantic_cache.py` is a module-
    level singleton shared by every Celery worker thread (`pool="threads"`,
    both the embedded test worker and a real containerized `celery-worker`
    run this way). Two threads calling `.encode()` on that SAME model
    instance at once deadlocked in practice -- reproduced by
    `TestSSEStream::test_stream_emits_events_ending_in_terminal_status`
    hanging indefinitely (confirmed via `celery inspect active` showing
    two `migrate_task` threads frozen, and a file-based diagnostic trace
    showing both stuck inside `embed_text` with neither ever returning),
    while `docker ps`/`pg_stat_activity`/`pg_locks` all stayed clean --
    i.e. NOT a Docker, DB-lock, or network issue.

    Fixed by (1) `TOKENIZERS_PARALLELISM=false`, set at module import time
    before `sentence_transformers`/`tokenizers` is loaded (HuggingFace's
    own documented fix for this deadlock class), and (2) a
    `threading.Lock` around the actual `.encode()` call in `embed_text`,
    deliberately not relying on (1) alone.

    This test dispatches concurrent `embed_text` calls via a real thread
    pool -- the same execution shape as the Celery worker threads that
    actually hit the bug -- and asserts they all return correctly within a
    bounded time. Before the fix, this test would hang forever (no
    timeout of its own); `pytest-timeout`-style protection isn't assumed
    to be configured, so the bound is enforced by hand via a background
    thread joined with a timeout, so the test fails loudly with a clear
    assertion instead of hanging the whole suite the way the bug itself
    did.
    """

    def test_concurrent_calls_return_within_bounded_time(self):
        import queue
        import threading

        texts = [f"concurrent embed_text regression test query {i}" for i in range(6)]
        results: queue.Queue = queue.Queue()

        def worker(text: str) -> None:
            try:
                vector = embed_text(text)
                results.put(("ok", text, vector))
            except Exception as exc:  # pragma: no cover -- reported via the queue
                results.put(("error", text, exc))

        threads = [threading.Thread(target=worker, args=(t,)) for t in texts]
        start = time.monotonic()
        for th in threads:
            th.start()
        for th in threads:
            # Generous per-thread join bound -- real embedding compute,
            # not mocked, but this must never approach it if the fix
            # holds: pre-fix, this reliably deadlocked forever instead of
            # merely running slowly.
            th.join(timeout=15.0)

        elapsed = time.monotonic() - start
        still_alive = [th for th in threads if th.is_alive()]
        assert not still_alive, (
            f"{len(still_alive)}/{len(threads)} embed_text() calls never "
            f"returned within the bounded join -- this is the deadlock "
            f"regressing, not a mere slowdown."
        )
        assert elapsed < 15.0

        outcomes = []
        while not results.empty():
            outcomes.append(results.get_nowait())
        assert len(outcomes) == len(texts)
        for status, text, payload in outcomes:
            assert status == "ok", f"embed_text({text!r}) raised: {payload!r}"
            assert len(payload) == EMBEDDING_DIMENSION
