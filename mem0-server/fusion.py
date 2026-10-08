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
2026-10-08 lab (docs/systems/fusion.md). Ranks count from 1 (Qdrant's RRF counts from 0, so its
default k = 2 is k = 1 here).

Contract callers depend on (pinned in tests/test_fusion.py):
* the caller's threshold gates the RAW cosine before any fusion (the context-bundle relevance gate,
  the NLI pre-filter and the "dump all canonicals" callers rely on it);
* the candidates are the dense pool only (a keyword-only hit is not a candidate here; the rerank
  path's union leg in app.py is the one place that adds those);
* it returns [{id, score, payload[, score_details]}] sorted by score descending, at most top_k long,
  with score in (0, 1] and monotone with the order;
* each call records the per-id legs (the raw cosine among them) for the request, for the diagnose
  verdict, the admission gate's cosine floors and observability (last_legs, stamp_cosine). The mem0
  mode records the raw cosine too, so those readers compare a cosine whichever formula ranks.

Modes (MEM0_FUSION, read from the process environment): "rrf" (the default) and "mem0" (mem0's own
additive formula: the rollback, and the way to run a mem0 the fusion cannot bind to; see health()).
"""
from __future__ import annotations

import contextvars
import dis
import inspect
import os
from typing import Any, Dict, List, Optional

RRF_K = 2.0            # on 1-based ranks; the lab's surface is flat between 1 and 3
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
        LAST_LEGS.set({str(r["id"]): {"cosine": float(r.get("score") or 0.0)}
                       for r in semantic_results if r.get("id") is not None})
        kw = {"explain": explain} if explain else {}
        return _MEM0_ORIGINAL(semantic_results=semantic_results, bm25_scores=bm25_scores,
                              entity_boosts=entity_boosts, threshold=threshold, top_k=top_k, **kw)
    return rrf(semantic_results, bm25_scores, entity_boosts, threshold, top_k, explain=explain)


# The parameters ams_score_and_rank takes. mem0's own score_and_rank must take no other, or its search
# could pass one the fusion would not honour (2.1.0 added `explain`; the next one would be a TypeError on
# every search).
_HANDLED_PARAMS = frozenset({"semantic_results", "bm25_scores", "entity_boosts", "threshold", "top_k",
                             "explain"})


def _unhandled_params(fn) -> List[str]:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return []
    return [p.name for p in params
            if p.name not in _HANDLED_PARAMS and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]


def _calls_the_global(fn, module) -> bool:
    """True when fn loads score_and_rank as a global OF `module` at call time: its globals are that
    module's namespace and its code has a LOAD_GLOBAL of the name. An attribute call
    (scoring.score_and_rank, self._scorer.score_and_rank) or a method defined in another module and
    re-exported reads False; so does a call moved into a nested helper (loud, never silent)."""
    code = getattr(fn, "__code__", None)
    if code is None or getattr(fn, "__globals__", None) is not vars(module):
        return False
    return any(ins.opname == "LOAD_GLOBAL" and ins.argval == "score_and_rank"
               for ins in dis.get_instructions(code))


def install(main_module=None) -> Dict[str, Any]:
    """Bind ams_score_and_rank as mem0's fusion. Returns the binding status for /health/deep.

    bound is True only when Memory._search_vector_store (the search the server calls) looks
    score_and_rank up as a global of mem0.memory.main at call time AND that global is now this
    function; callers_bound reports the sync and the async search (the server uses the sync one). A
    mem0 whose score_and_rank takes a parameter this function does not is left unbound, with mem0's
    own formula in place. Either way a mem0 release that moves the scoring reads bound=False instead
    of silently falling back to the additive formula."""
    global _MEM0_ORIGINAL
    status: Dict[str, Any] = {"mode": mode(), "bound": False}
    try:
        if main_module is None:
            import mem0.memory.main as main_module  # noqa: PLC0415 (only the server imports mem0)
        current = getattr(main_module, "score_and_rank", None)
        if not callable(current):
            status["error"] = "mem0.memory.main has no score_and_rank"
            return status
        original = _MEM0_ORIGINAL if current is ams_score_and_rank else current
        extra = _unhandled_params(original) if original is not None else []
        if extra:
            status["error"] = f"mem0's score_and_rank takes parameters the fusion does not handle: {extra}"
            return status
        _MEM0_ORIGINAL = original
        main_module.score_and_rank = ams_score_and_rank
        callers = []
        for cls_name in ("Memory", "AsyncMemory"):
            cls = getattr(main_module, cls_name, None)
            fn = getattr(cls, "_search_vector_store", None) if cls is not None else None
            callers.append(fn is not None and _calls_the_global(fn, main_module))
        status["callers_bound"] = callers
        status["bound"] = bool(callers and callers[0]) and main_module.score_and_rank is ams_score_and_rank
    except Exception as e:  # never break the server start: report it, health reads it
        status["error"] = f"{type(e).__name__}: {e}"[:200]
    return status


# Searches since start that returned results through the fusion (reached) and without it (bypassed):
# the observed half of the binding check. One bypassed search means mem0 ranked without calling the
# global, whatever install() read from its code.
SEARCHES = {"reached": 0, "bypassed": 0}


def health(status: Dict[str, Any]) -> Dict[str, Any]:
    """checks.fusion for /health/deep: install()'s status, the search counts and ok. ok is false when
    the fusion is not bound or a search bypassed it, which fails /health/deep and every gate that reads
    it, unless the operator runs mem0's own formula on purpose (MEM0_FUSION=mem0): that mode needs no
    binding, so it is also how a mem0 the fusion cannot bind to is run without failing every deploy."""
    out: Dict[str, Any] = {"ok": (bool(status.get("bound")) and SEARCHES["bypassed"] == 0)
                                 or mode() == "mem0"}
    out.update(status)
    out["mode"] = mode()
    out["searches"] = dict(SEARCHES)
    return out


def last_legs() -> Optional[Dict[str, Dict[str, Any]]]:
    """The per-id legs of the last fusion call in this context (the mem0 mode records only the raw
    cosine), or None."""
    return LAST_LEGS.get()


def begin_search() -> None:
    """Call right before mem.search: this request's legs start empty, so what end_search and
    last_legs read afterwards can only come from that search."""
    LAST_LEGS.set(None)


def end_search(results: Any) -> None:
    """Call right after mem.search. Copies each result's raw cosine from the legs onto it as `cosine`
    (the value the threshold compared, and the one the admission gate's cosine floor reads), and counts
    the search as reached or bypassed. A result that did not come through the dense pool (a
    lexical-only rescue, added later) gets no cosine."""
    items = results.get("results") if isinstance(results, dict) else None
    if not isinstance(items, list) or not items:
        return
    legs = LAST_LEGS.get()
    if legs is None:
        SEARCHES["bypassed"] += 1
        return
    SEARCHES["reached"] += 1
    for r in items:
        leg = legs.get(str(r.get("id"))) if isinstance(r, dict) else None
        if leg is not None and leg.get("cosine") is not None:
            r["cosine"] = leg["cosine"]
