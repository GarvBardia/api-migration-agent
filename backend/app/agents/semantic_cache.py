"""Redis semantic cache (Step 10).

Fronts `api_doc_chunks` lookups (the Migration Agent's RAG context, per
CLAUDE.md §4.5) with a cache-aside layer: embed the query, check Redis for
an exact or near-duplicate cached embedding before hitting Postgres/
pgvector. Two file_tasks referencing the same symbol -> the second lookup
is served entirely from Redis: one embedding computation, one Postgres
query, not two.

**Embedding model fork (decided this session, documented here + dated in
CLAUDE.md):** no embedding API key (OpenAI or otherwise) is configured in
this environment. Rather than block on that, this defaults to a free,
locally-run model -- `sentence-transformers`' `all-MiniLM-L6-v2` -- same
cost-conscious approach as Step 4's OpenRouter free tier. Its output
dimension (384) was confirmed by actually loading and running the model
in this session, not assumed from the model name; `api_doc_chunks.embedding`
was resized from `vector(1536)` (OpenAI-sized, never used) to `vector(384)`
via migration `0005`.

**Similarity-matching simplification, documented not hidden:** Redis here
has no vector-search module (RediSearch) installed -- just plain
`redis:7-alpine` from `docker-compose.yml`. Near-duplicate matching (not
just exact-text cache hits) is done via a bounded linear scan over a
capped, recency-ordered index of cached embeddings (`max_index_size`,
default 500) with cosine similarity computed in Python. This is
appropriate at this project's scale; it is NOT an approximate-nearest-
neighbor index and would not scale to a large cache -- if that's ever
needed, swap in RediSearch's vector type or a dedicated vector DB instead
of scaling this linear scan up.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Callable

# Added 2026-09-03 (post-containerization debugging session) -- see the
# dated note in CLAUDE.md. MUST be set before `sentence_transformers` (and
# transitively HuggingFace `tokenizers`) is ever imported/loaded -- it's
# read once at import time, not on every call. `tokenizers` spins up its
# own internal Rust thread pool for tokenization; left at its default, that
# pool can deadlock against Python-level concurrent calls into one model
# instance -- exactly what happened here (see the module-level singleton
# note on `_model` below). This is HuggingFace's own documented fix for
# that deadlock class. `setdefault` so an operator/environment override
# is still possible, but the safe default doesn't depend on one being set.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

DEFAULT_TTL_SECONDS = int(os.environ.get("SEMANTIC_CACHE_TTL_SECONDS", str(24 * 3600)))
DEFAULT_SIMILARITY_THRESHOLD = float(
    os.environ.get("SEMANTIC_CACHE_SIMILARITY_THRESHOLD", "0.95")
)
DEFAULT_MAX_INDEX_SIZE = int(os.environ.get("SEMANTIC_CACHE_MAX_INDEX_SIZE", "500"))

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSION = 384  # confirmed empirically -- see module docstring

_model = None  # lazy singleton -- loading the model is expensive; do it once

# Added 2026-09-03 -- see the dated CLAUDE.md note (same session as the
# TOKENIZERS_PARALLELISM fix above). `_model` is a single instance shared
# by every Celery worker thread (`pool="threads"` -- both the embedded
# test worker and a real containerized `celery-worker` run this way).
# Two threads calling `.encode()` on the SAME SentenceTransformer instance
# at once deadlocked in practice (confirmed live via a file-based
# diagnostic trace: both worker threads logged "about to embed_text" and
# neither ever returned, while `docker ps`/`pg_stat_activity` stayed
# clean -- see CLAUDE.md). Setting TOKENIZERS_PARALLELISM=false above
# addresses the specific library-level thread-pool collision that caused
# it; this lock is deliberate belt-and-suspenders on top of that -- it
# does not rely on the env var alone being sufficient, since the
# underlying model object still isn't documented as safe for concurrent
# `.encode()` calls from multiple threads regardless of the tokenizer
# setting. Serializing here (rather than, say, one model instance per
# thread) keeps memory bounded -- the model is the expensive part.
_encode_lock = threading.Lock()


def _get_model():
    """Lazy singleton. The `if _model is None` check below is only safe
    because every caller of `_get_model()` (just `embed_text`) already
    holds `_encode_lock` -- see 2026-09-04 note. Without that, two
    threads racing their first call could both see `_model is None`
    before either assigns, each constructing (and loading the weights
    for) their own separate model instance -- confirmed live via a
    diagnostic trace showing "Loading weights: 100%" twice per run, a
    real (if less severe than the encode() deadlock) bug found in the
    same debugging session. Reusing `_encode_lock` for construction too,
    rather than a second lock, keeps this simple: construction happens
    at most once, and every caller already serializes through it anyway.
    """

    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _model


def embed_text(text: str) -> list[float]:
    """The one place an embedding is actually computed. Vectors are
    L2-normalized (`normalize_embeddings=True`), so a plain dot product
    already equals cosine similarity -- `_cosine_similarity` below stays
    general/explicit anyway rather than relying on that silently.

    Serialized via `_encode_lock` (2026-09-03/04) -- see the module-level
    note on `_model` above and on `_get_model`. Concurrent callers block
    briefly on each other (both for first-use model construction and for
    every `.encode()` call) rather than deadlocking inside the shared
    model or double-constructing it.
    """

    with _encode_lock:
        model = _get_model()
        vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _normalize_key(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


class SemanticCache:
    """Cache-aside layer in front of any embedding-keyed compute function
    (a Postgres/pgvector lookup, an LLM call for context, etc.).
    """

    def __init__(
        self,
        redis_client,
        namespace: str = "semantic_cache",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        max_index_size: int = DEFAULT_MAX_INDEX_SIZE,
    ):
        self._redis = redis_client
        self._namespace = namespace
        self._ttl_seconds = ttl_seconds
        self._similarity_threshold = similarity_threshold
        self._max_index_size = max_index_size

    def _entry_key(self, cache_key: str) -> str:
        return f"{self._namespace}:entry:{cache_key}"

    def _index_key(self) -> str:
        return f"{self._namespace}:index"

    def get_or_compute(
        self, query_text: str, compute_fn: Callable[[list[float]], object]
    ) -> tuple[object, str]:
        """Returns `(result, source)`, `source` in `("cache", "computed")`.

        Exact-match fast path first (hash of normalized `query_text`) --
        deliberately does NOT compute an embedding for this check, so two
        calls with the identical `query_text` (e.g. two file_tasks
        referencing the same symbol) trigger exactly one embedding
        computation and one call to `compute_fn` (the Postgres lookup),
        not two: the second call hits the exact-match cache entry before
        ever needing to embed anything. Embedding only happens on an exact
        miss, both to check the fuzzy-similarity index and (if that also
        misses) to actually call `compute_fn`.
        """

        cache_key = _normalize_key(query_text)
        cached_raw = self._redis.get(self._entry_key(cache_key))
        if cached_raw is not None:
            return json.loads(cached_raw)["result"], "cache"

        query_embedding = embed_text(query_text)

        similar = self._find_similar(query_embedding)
        if similar is not None:
            return similar, "cache"

        result = compute_fn(query_embedding)
        self._store(cache_key, query_embedding, result)
        return result, "computed"

    def _find_similar(self, query_embedding: list[float]):
        index_key = self._index_key()
        recent_keys = self._redis.zrevrange(index_key, 0, self._max_index_size - 1)

        best_score = -1.0
        best_result = None
        for raw_key in recent_keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            raw_entry = self._redis.get(self._entry_key(key))
            if raw_entry is None:
                continue  # TTL-expired entry whose index row hasn't been swept yet
            payload = json.loads(raw_entry)
            score = _cosine_similarity(query_embedding, payload["embedding"])
            if score > best_score:
                best_score = score
                best_result = payload["result"]

        if best_score >= self._similarity_threshold:
            return best_result
        return None

    def _store(self, cache_key: str, embedding: list[float], result: object) -> None:
        entry = json.dumps({"embedding": embedding, "result": result})
        self._redis.setex(self._entry_key(cache_key), self._ttl_seconds, entry)
        self._redis.zadd(self._index_key(), {cache_key: time.time()})
        # Cap the index (not the entries themselves -- those expire via
        # their own TTL): drop the oldest beyond max_index_size.
        self._redis.zremrangebyrank(self._index_key(), 0, -(self._max_index_size + 1))


def lookup_doc_chunks_in_postgres(
    session, api_name: str, query_embedding: list[float], top_k: int = 3
) -> list[dict]:
    """The actual Postgres/pgvector lookup -- nearest `api_doc_chunks` rows
    for `api_name` by cosine distance. Returns plain dicts (JSON-
    serializable, so `SemanticCache` can store the result directly)."""

    from db.models import ApiDocChunk

    rows = (
        session.query(ApiDocChunk)
        .filter(ApiDocChunk.api_name == api_name)
        .order_by(ApiDocChunk.embedding.cosine_distance(query_embedding))
        .limit(top_k)
        .all()
    )
    return [{"id": str(r.id), "chunk_text": r.chunk_text} for r in rows]


def get_doc_context_for_symbol(
    session,
    redis_client,
    api_name: str,
    symbol_text: str,
    top_k: int = 3,
    cache: SemanticCache | None = None,
) -> tuple[list[dict], str]:
    """Convenience entry point wiring `SemanticCache` in front of
    `lookup_doc_chunks_in_postgres` for one (api_name, symbol_text) query.
    Returns `(chunks, source)` -- `source` is `"cache"` or `"computed"`.

    **Real bug fixed here (gap-fix session):** the default cache namespace
    used to be shared globally across every `api_name` -- the query text
    embedded `api_name` as a *prefix* (`f"{api_name}:{symbol_text}"`), but
    the fuzzy-similarity index (`SemanticCache._find_similar`) scans ONE
    shared sorted set regardless of that prefix. Two different APIs whose
    symbol/notes text happened to embed as merely similar (a short,
    varying prefix isn't enough perturbation to reliably separate general
    sentence embeddings) could then serve each other's cached doc chunks
    -- a real cross-tenant correctness bug, not just a test-isolation
    artifact (it's how it was first noticed: two tests using different
    `api_name`s cross-contaminated). Fixed by scoping the cache's
    NAMESPACE (not just the query text) by `api_name` when this function
    constructs its own default cache, so the fuzzy-similarity index itself
    is partitioned per API and can never cross a boundary a caller didn't
    explicitly share.
    """

    cache = cache or SemanticCache(redis_client, namespace=f"semantic_cache:{api_name}")
    query_text = f"{api_name}:{symbol_text}"
    return cache.get_or_compute(
        query_text,
        lambda embedding: lookup_doc_chunks_in_postgres(
            session, api_name, embedding, top_k
        ),
    )
