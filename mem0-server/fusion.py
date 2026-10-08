"""Hybrid fusion for mem0's search: one ranking over the dense candidate pool that holds in every
embedding space and survives durable freshness.

mem0 (2.0.4 and 2.1.0) ranks its dense candidates by min((cosine + sigmoid(bm25) + entity_boost) /
max_possible, 1.0). The three terms live on different scales: the keyword term spans most of 0..1
and the entity term up to 0.5, while the cosines of one query's candidates sit within ~0.2 of each
other (EmbeddingGemma-300m) or ~0.1 (EmbeddingGemma-2). The keyword and entity terms outvote the
meaning of the query, and the server's durable freshness then multiplies these closely spaced scores
by 0.77-0.90 and re-sorts, so the age of a memory outvotes it too (docs/systems/fusion.md).

This module replaces that ranking at the one place mem0 calls it - the module global
mem0.memory.main.score_and_rank, looked up by name in both the sync and the async search - without
forking mem0. It is weighted reciprocal rank fusion over the dense pool:

    score = [1/(k + rank_dense) + w_bm25/(k + rank_bm25) + w_entity/(k + rank_entity)]
            / [(1 + w_bm25 + w_entity) / (k + 1)]

rank_dense orders the pool by cosine; rank_bm25 and rank_entity order the candidates that have a
keyword match / an entity boost (a candidate without one gets no term for that leg). Ranks do not
care how compressed a model's cosine scale is, and the score keeps the top ranks well apart (with
k=2 the dense leg alone gives rank 1 / 2 / 3 the values 0.61 / 0.45 / 0.36), so a freshness weight
of 0.8 moves a memory a rank or two instead of to the bottom. The constants were chosen on the
2026-10-08 lab (docs/systems/fusion.md): k is Qdrant's own RRF default.

Contract callers depend on (pinned in tests/test_fusion.py):
* the caller's threshold gates the RAW cosine before any fusion (the context-bundle relevance gate,
  the NLI pre-filter and the "dump all canonicals" callers rely on it);
* the candidates are the dense pool only (a keyword-only hit is not a candidate here; the rerank
  path's union leg in app.py is the one place that adds those);
* it returns [{id, score, payload[, score_details]}] sorted by score descending, at most top_k long,
  with score in (0, 1] and monotone with the order;
* each call records the per-id legs (the raw cosine among them) for the request, for the diagnose
  verdict and observability (last_legs).

Modes (MEM0_FUSION): "rrf" (the default) and "mem0" (mem0's own additive formula, the rollback).
"""
from __future__ import annotations

import contextvars
import os
from typing import Any, Dict, List, Optional

RRF_K = 2.0            # Qdrant's RRF default; the lab's surface is flat between 1 and 3
RRF_W_BM25 = 0.4       # the keyword leg's weight relative to the dense leg (1.0)
RRF_W_ENTITY = 0.25    # the entity leg's weight (neutral on the lab sets, kept for entity-led queries)
MODES = ("rrf", "mem0")
DEFAULT_MODE = "rrf"

# The per-id legs of the last fusion call in this context (one request). Never shared across requests.
LAST_LEGS: contextvars.ContextVar = contextvars.ContextVar("ams_fusion_last_legs", default=None)


def mode() -> str:
    m = (os.environ.get("MEM0_FUSION") or DEFAULT_MODE).strip().lower()
    return m if m in MODES else DEFAULT_MODE


def _rank_positive(values: Dict[str, float]) -> Dict[str, int]:
    """1-based ranks of the ids whose value is > 0, highest first (ties keep the pool order)."""
    order = [i for i, v in values.items() if v > 0]
    order.sort(key=lambda i: -values[i])
    return {i: n + 1 for n, i in enumerate(order)}


def rrf(semantic_results: List[Dict[str, Any]], bm25_scores: Dict[str, float],
        entity_boosts: Dict[str, float], threshold: float, top_k: int,
        k: float = RRF_K, w_bm25: float = RRF_W_BM25, w_entity: float = RRF_W_ENTITY,
        explain: bool = False) -> List[Dict[str, Any]]:
    pool = []
    for r in semantic_results:
        mid = r.get("id")
        if mid is None:
            continue
        cos = r.get("score") or 0.0
        if cos < threshold:
            continue                      # the raw-cosine gate, before any fusion
        pool.append((str(mid), float(cos), r.get("payload")))
    pool.sort(key=lambda x: -x[1])        # mem0 hands the pool in cosine order; do not rely on it
    rank_dense = {mid: n + 1 for n, (mid, _, _) in enumerate(pool)}
    ids = list(rank_dense)
    rank_bm25 = _rank_positive({i: float(bm25_scores.get(i, 0.0) or 0.0) for i in ids})
    rank_entity = _rank_positive({i: float(entity_boosts.get(i, 0.0) or 0.0) for i in ids})
    norm = (1.0 + w_bm25 + w_entity) / (k + 1.0)
    legs: Dict[str, Dict[str, Any]] = {}
    scored = []
    for mid, cos, payload in pool:
        raw = 1.0 / (k + rank_dense[mid])
        if mid in rank_bm25:
            raw += w_bm25 / (k + rank_bm25[mid])
        if mid in rank_entity:
            raw += w_entity / (k + rank_entity[mid])
        score = min(raw / norm, 1.0)      # exactly 1.0 for rank 1 on every leg (float rounding aside)
        legs[mid] = {"cosine": cos, "rank_dense": rank_dense[mid], "rank_bm25": rank_bm25.get(mid),
                     "rank_entity": rank_entity.get(mid), "bm25": float(bm25_scores.get(mid, 0.0) or 0.0),
                     "entity_boost": float(entity_boosts.get(mid, 0.0) or 0.0), "score": score}
        item = {"id": mid, "score": score, "payload": payload}
        if explain:
            item["score_details"] = {"semantic_score": cos, "bm25_score": legs[mid]["bm25"],
                                     "entity_boost": legs[mid]["entity_boost"],
                                     "rank_dense": rank_dense[mid], "rank_bm25": rank_bm25.get(mid),
                                     "rank_entity": rank_entity.get(mid), "k": k, "w_bm25": w_bm25,
                                     "w_entity": w_entity, "final_score": score, "threshold": threshold,
                                     "fusion": "rrf"}
        scored.append(item)
    scored.sort(key=lambda x: x["score"], reverse=True)
    LAST_LEGS.set(legs)
    return scored[:top_k]


_MEM0_ORIGINAL = None          # mem0's own score_and_rank, kept for the "mem0" mode and the rollback


def ams_score_and_rank(semantic_results, bm25_scores, entity_boosts, threshold, top_k,
                       explain: bool = False):
    """Drop-in for mem0.memory.main.score_and_rank (2.0.4 calls it without explain, 2.1.0 with)."""
    if threshold is None:
        threshold = 0.1                   # mem0's own guard
    bm25_scores = bm25_scores or {}
    entity_boosts = entity_boosts or {}
    if mode() == "mem0" and _MEM0_ORIGINAL is not None:
        LAST_LEGS.set(None)
        kw = {"explain": explain} if explain else {}
        return _MEM0_ORIGINAL(semantic_results=semantic_results, bm25_scores=bm25_scores,
                              entity_boosts=entity_boosts, threshold=threshold, top_k=top_k, **kw)
    return rrf(semantic_results, bm25_scores, entity_boosts, threshold, top_k, explain=explain)


def install(main_module=None) -> Dict[str, Any]:
    """Bind ams_score_and_rank as mem0's fusion. Returns the binding status for /health/deep.

    bound is True only when mem0's search still looks score_and_rank up as a module global (the code
    object of Memory._search_vector_store names it) AND that global is now this function: a mem0
    release that moves the scoring elsewhere reads bound=False instead of silently falling back to
    the additive formula."""
    global _MEM0_ORIGINAL
    status: Dict[str, Any] = {"mode": mode(), "bound": False}
    try:
        if main_module is None:
            import mem0.memory.main as main_module  # noqa: PLC0415 (only the server imports mem0)
        current = getattr(main_module, "score_and_rank", None)
        if not callable(current):
            status["error"] = "mem0.memory.main has no score_and_rank"
            return status
        if current is not ams_score_and_rank:
            _MEM0_ORIGINAL = current
        main_module.score_and_rank = ams_score_and_rank
        callers = []
        for cls_name in ("Memory", "AsyncMemory"):
            cls = getattr(main_module, cls_name, None)
            fn = getattr(cls, "_search_vector_store", None) if cls is not None else None
            code = getattr(fn, "__code__", None)
            callers.append(bool(code is not None and "score_and_rank" in code.co_names))
        status["callers_bound"] = callers
        status["bound"] = bool(callers and callers[0]) and main_module.score_and_rank is ams_score_and_rank
    except Exception as e:  # never break the server start: report it, health reads it
        status["error"] = f"{type(e).__name__}: {e}"[:200]
    return status


def last_legs() -> Optional[Dict[str, Dict[str, Any]]]:
    """The per-id legs of the last rrf fusion in this context, or None."""
    return LAST_LEGS.get()
