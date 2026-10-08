"""episode_embeddings.py — v0.29 R4 semantic raw-trace gate.

Episode SUMMARIES are embedded with the SAME embedder mem0 uses (the active
embedder_profile's model behind the asymmetric prefix-shim) and stored in that
space's dedicated Qdrant collection (``episodes_egemma_768`` for
EmbeddingGemma-300m, ``episodes_eg2_768`` for EmbeddingGemma-2) keyed by episode id. The context_bundle low-confidence
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

import logging
import random
import threading
import time
from typing import Optional

import embedder_profile as _embedder_profile

log = logging.getLogger("mem0-server")   # the server's own logger: the greppable lines below land in its journal

# The active embedding space's episode collection (embedder_profile; MEM0_EPISODES_COLLECTION overrides).
EPISODE_COLLECTION = _embedder_profile.collection("episodes")
EPISODE_DIMS = _embedder_profile.active().dims
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
    production shim, which as of 2026-07-26 makes 3 attempts with backoff) so
    composed layers never multiply attempts."""
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


# 1.32.4: a cold embedder used to drop an episode vector for good. The embedder unloads after 5 idle
# minutes and a restart or a failed start answers 500 'exited prematurely' / 503 / a refused
# connection for a while; the shim's own retry covers 429 only (its contract is pinned) and the
# finalize POST cannot wait (its hook times out at 5 s). The cold retry therefore lives HERE, above
# the embedder, and both callers share it: the finalize-time background retry in app.py and the
# episode-embed-backfill script. Waits are seconds-scale (embedder_503.RETRY_AFTER_S is 10), not the
# shim's sub-second 429 backoff.
COLD_RETRY_DELAYS_S = (10, 20)
COLD_RETRY_BUDGET_S = 60.0


def is_cold_embed_error(exc: BaseException) -> bool:
    """True when `exc` means the embedder cannot serve RIGHT NOW (embedder_503.retry_later). What
    embed_with_cold_retry raises after giving up on a cold embedder is still such an error, so this is how
    a caller tells 'the seat never came up' from 'the request itself is broken'."""
    from embedder_503 import retry_later  # lazy: it pulls fastapi, and this module stays import-light
    return retry_later(exc) is not None


def embed_with_cold_retry(embedder, text: Optional[str], *, delays=COLD_RETRY_DELAYS_S,
                          budget_s: float = COLD_RETRY_BUDGET_S, sleep=time.sleep,
                          clock=time.monotonic) -> Optional[list]:
    """embed_episode_summary that rides out a cold or restarting embedder.

    Retries ONLY when embedder_503.retry_later(exc) says the embedder cannot serve right now (refused or
    timed-out connection, 502/503/504, llama-swap's 500 'exited prematurely'): sleeps max(retry_after,
    delays[n]) and tries again, at most len(delays) times and never sleeping past `budget_s` since the
    call began. Any other exception (a context-overflow 500, a 4xx, a coding error) is raised at once,
    unretried, and so is the last cold one when the retries or the budget run out."""
    start = clock()
    attempt = 0
    while True:
        try:
            return embed_episode_summary(embedder, text)
        except Exception as e:
            from embedder_503 import retry_later  # lazy, see is_cold_embed_error
            retry_after = retry_later(e)
            if retry_after is None or attempt >= len(delays):
                raise
            wait = max(retry_after, delays[attempt])
            if clock() - start + wait > budget_s:
                raise
            sleep(wait)
            attempt += 1


class DeferredEmbedGate:
    """Caps the finalize-time background retries: at most one in flight per episode id and `cap` overall.
    A background task holds a worker thread while it sleeps through the backoff, so an outage that
    finalizes a burst of sessions must not queue unbounded sleepers. A retry refused here is not lost:
    the daily upkeep step (episodic-reconcile --upkeep) embeds whatever is still missing."""

    def __init__(self, cap: int = 4):
        self.cap = cap
        self._active: set = set()
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return len(self._active)

    def acquire(self, episode_id) -> bool:
        with self._lock:
            if episode_id in self._active or len(self._active) >= self.cap:
                return False
            self._active.add(episode_id)
            return True

    def release(self, episode_id) -> None:
        with self._lock:
            self._active.discard(episode_id)


def run_deferred_embed(embedder, upsert, gate: DeferredEmbedGate, episode_id: int, summary: str, payload: dict,
                       **retry_kw) -> None:
    """The background half of create_episode's embed: cold-retry the embed, then upsert the vector with the
    same payload the in-request attempt would have written. Never raises (it runs after the response) and
    always frees its gate slot. `upsert(ep_id, vector, payload)` writes one point; `retry_kw` is
    embed_with_cold_retry's delays / budget_s / sleep / clock. Greppable outcomes:
    'episode embed recovered ep=<id>' and 'episode embed gave up ep=<id> ...'."""
    try:
        vec = embed_with_cold_retry(embedder, summary, **retry_kw)
        if vec is None:
            log.warning("episode embed gave up ep=%s (empty summary)", episode_id)
            return
        upsert(episode_id, vec, payload)
        log.info("episode embed recovered ep=%s", episode_id)
    except Exception as e:  # noqa: BLE001 - a background task has nowhere to raise to
        log.warning("episode embed gave up ep=%s (%s: %s); the daily upkeep step will retry it",
                    episode_id, type(e).__name__, str(e)[:120])
    finally:
        gate.release(episode_id)


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
