"""episode_embeddings.py — v0.29 R4 semantic raw-trace gate.

Episode SUMMARIES are embedded with the SAME EmbeddingGemma-300m embedder mem0
uses (asymmetric prefix-shim) and stored in a dedicated Qdrant collection
``episodes_egemma_768`` keyed by episode id. The context_bundle low-confidence
fallback embeds the live prompt and does a semantic search over this collection,
then applies a fail-closed brand gate + a calibrated cosine floor.

Why a dedicated collection (not bm25/FTS): a live check against the real store
proved lexical bm25 cannot separate off-domain-but-keyword-dense episodes from
relevant ones. A Cosine collection returns the RAW cosine as the search score, so
the relevance floor is calibrated directly on the semantic scale (the house rule).

This module is mem-free of FastAPI; the caller injects the Qdrant client +
embedder (both live on the `mem` object in app.py), which keeps it unit-testable.
"""
from __future__ import annotations

import random
import time
from typing import Optional

EPISODE_COLLECTION = "episodes_egemma_768"
EPISODE_DIMS = 768
# Minimum summary length to index. A live check showed degenerate-short summaries
# (e.g. ~39-char test-fixture artifacts like "Session created for resolve smoke
# test.") get inflated cosine to unrelated queries and surface as junk fallbacks;
# real Codex-extracted episode summaries start at ~76 chars, so a 64-char floor
# cleanly excludes the short artifacts without dropping any real episode. (The
# broader heterogeneous test-pollution purge of episodic.db is a separate, deletion-
# gated follow-up; enabling the R4 flag in production is gated on it.)
MIN_SUMMARY_CHARS = 64

# MEM-12 (2026-07-03): llama-swap 429 bursts under queue saturation killed the
# raw-trace fallback (every one of the 25 RateLimitErrors/7d hit exactly this
# path — the fallback embeds the LIVE prompt while the bundle search has just
# hammered the same llama-swap queue). ONE bounded retry with ~250ms + jitter
# absorbs the burst; the second 429 propagates so the caller's existing
# fail-soft handling (bundle try/except) still applies. This module stays
# import-light (no openai/mem0 dependency — callers inject the embedder), so
# the 429 is recognized by duck-typing instead of isinstance.
_RETRY_429_BASE_SLEEP_S = 0.25
_RETRY_429_JITTER_S = 0.25


def _is_rate_limit(exc: Exception) -> bool:
    """Duck-typed 429 detection: openai.RateLimitError carries status_code=429
    and is named RateLimitError; either signal qualifies. NOTHING else does —
    other errors must never be retried (a ctx-overflow 500 has to surface)."""
    return getattr(exc, "status_code", None) == 429 or type(exc).__name__ == "RateLimitError"


def _embed_with_429_retry(embedder, text: str, memory_action: str):
    """embedder.embed with the single bounded 429 retry. Skipped entirely when
    the embedder self-retries (EmbeddingGemmaEmbedder.handles_429_retry — the
    production shim) so composed layers never multiply attempts beyond 2."""
    if getattr(embedder, "handles_429_retry", False):
        return embedder.embed(text, memory_action=memory_action)
    try:
        return embedder.embed(text, memory_action=memory_action)
    except Exception as e:
        if not _is_rate_limit(e):
            raise
        time.sleep(_RETRY_429_BASE_SLEEP_S + random.random() * _RETRY_429_JITTER_S)
        return embedder.embed(text, memory_action=memory_action)


def _indexable_summary(summary) -> bool:
    """True if *summary* is substantive enough to embed into the semantic
    collection (non-empty and >= MIN_SUMMARY_CHARS after stripping)."""
    return bool(summary) and len(summary.strip()) >= MIN_SUMMARY_CHARS


def _brand_admits(row_brand: Optional[str], brand: Optional[str], only_brand_neutral: bool) -> bool:
    """Fail-closed brand gate — byte-for-byte the goals/OQ $brandGate semantics.

    * known session brand -> admit same-brand OR brand-neutral (null/empty) rows.
    * unknown brand (falsy) + only_brand_neutral -> admit ONLY brand-neutral rows
      (a branded episode must never leak into an unrecognized session).
    * unknown brand + not only_brand_neutral -> admin/unscoped: admit everything.
    An empty/whitespace brand normalizes to None (review L4)."""
    rb = row_brand.strip() if isinstance(row_brand, str) else row_brand
    b = brand.strip() if isinstance(brand, str) else brand
    if b:
        return (not rb) or (rb == b)
    if only_brand_neutral:
        return not rb
    return True


def embed_episode_summary(embedder, summary_text: Optional[str]) -> Optional[list]:
    """Embed an episode summary as a DOCUMENT (memory_action='add' -> document
    prefix). Returns a 768-d list, or None for empty/whitespace input."""
    if not summary_text or not summary_text.strip():
        return None
    # MEM-12: bounded 429 retry (llama-swap burst) — see _embed_with_429_retry.
    vec = _embed_with_429_retry(embedder, summary_text, memory_action="add")
    return list(vec) if vec is not None else None


def ensure_episode_collection(client, dims: int = EPISODE_DIMS, collection: str = EPISODE_COLLECTION) -> bool:
    """Idempotently ensure the Cosine-distance episode collection exists.
    Returns True if it was created, False if it already existed."""
    from qdrant_client.models import Distance, VectorParams
    try:
        client.get_collection(collection)
        return False
    except Exception:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=dims, distance=Distance.COSINE),
        )
        return True


def upsert_episode_embedding(client, ep_id: int, vector: list, payload: dict,
                             collection: str = EPISODE_COLLECTION) -> None:
    """Upsert one episode point (id=ep_id). Synchronous (wait=True) so a
    subsequent search sees it immediately."""
    from qdrant_client.models import PointStruct
    client.upsert(
        collection_name=collection,
        points=[PointStruct(id=int(ep_id), vector=list(vector), payload=dict(payload or {}))],
        wait=True,
    )


def search_episodes_semantic(client, embedder, query: str, brand: Optional[str],
                             only_brand_neutral: bool = False, limit: int = 20,
                             floor: float = 0.0, collection: str = EPISODE_COLLECTION) -> list:
    """Semantic search over episode summaries.

    Embeds *query* as a SEARCH query (query prefix), fetches the top-`limit` by
    raw cosine, then applies the fail-closed brand gate + the cosine `floor`.
    Returns a list of (episode_id, cosine_score, payload) tuples, best first.
    """
    if not query or not query.strip():
        return []
    # MEM-12: bounded 429 retry — THE call the 429 bursts were killing (the
    # low-confidence fallback embeds the live prompt right after the bundle
    # search saturated the same llama-swap queue).
    qvec = _embed_with_429_retry(embedder, query, memory_action="search")
    # qdrant-client 1.18: .search() was removed in favour of .query_points()
    # (returns a QueryResponse whose .points are ScoredPoint with id/score/payload).
    resp = client.query_points(
        collection_name=collection,
        query=list(qvec),
        limit=limit,
        with_payload=True,
    )
    hits = getattr(resp, "points", resp)
    out = []
    for h in hits:
        score = getattr(h, "score", None)
        if score is None or score < floor:
            continue
        payload = getattr(h, "payload", None) or {}
        if _brand_admits(payload.get("brand"), brand, only_brand_neutral):
            out.append((getattr(h, "id", None), score, payload))
    return out
