"""nli_write_gate.py — R5 NLI write-gate decision (v0.27.2).

A self-writing memory store with no write-gate is a drift/poisoning surface (SSGM
2603.11768; AgentPoison). This gate rejects/flags an added fact that CONTRADICTS an
existing canonical/core fact — judged by **Codex** (the model-routing rule: all LLM
judgment uses Codex, never a local model), reached via the Windows Codex HTTP shim.

DESIGN (never block the hot write path):
  - Env-gated OFF by default (the caller only invokes this when MEM0_NLI_GATE_ENABLED).
  - FAST PRE-FILTER first: a canonical-tier search with a high SEMANTIC threshold floor.
    Codex is invoked ONLY when a genuinely high-cosine canonical neighbor exists; every
    other write admits immediately (no LLM call on the common path).
  - FAIL OPEN: empty text, no neighbor, a search error, the shim being down/timing out,
    an unparseable verdict, or a NO verdict all ADMIT. The gate only acts on a CONFIDENT
    contradiction.

This module is PURE: the search and judge are injected, so it is unit-testable without
a live server, Qdrant, or Codex. app.py wires in `_search_core` (canonical class) and
`codex_shim_client.judge_contradiction`.
"""
from __future__ import annotations

import logging

log = logging.getLogger("mem0.nli_gate")


def evaluate(text, user_id, brand, *, cosine_floor=0.5, topk=3,
             timeout_s=20, search_fn=None, judge_fn=None) -> dict:
    """Decide whether an added record contradicts a canonical fact. Returns a dict:
        {"action": "admit"|"flag", "canonical_id": <id|None>, "detail": <str>}

    'flag' (only on a CONFIDENT contradiction) means the caller should stamp
    contradicts_canonical on the record so the admission gate hides it. v0.27.2 runs this in a
    BACKGROUND task post-admit (so add() never blocks on Codex); there is no synchronous
    'reject' (it would re-introduce the hot-path block + a client-timeout/duplicate-write storm).
    search_fn(query, filters, threshold, topk) -> list[neighbor dicts] (each with id + memory).
    judge_fn(statement_a_canonical, statement_b_new, timeout_s) -> {ok, contradicts: bool|None}.
    Default-ADMIT on anything uncertain (fail open).
    """
    if not text or not str(text).strip():
        return {"action": "admit", "canonical_id": None, "detail": "empty-text"}
    if search_fn is None or judge_fn is None:
        return {"action": "admit", "canonical_id": None, "detail": "no-deps"}

    filters = {"user_id": user_id}
    if brand is not None:
        filters["brand"] = brand

    try:
        neighbors = search_fn(text, filters, cosine_floor, topk) or []
    except Exception:  # noqa: BLE001 — fail open
        log.exception("nli write-gate pre-filter search failed; admitting")
        return {"action": "admit", "canonical_id": None, "detail": "search-error"}

    if not neighbors:
        return {"action": "admit", "canonical_id": None, "detail": "no-canonical-neighbor"}

    top = neighbors[0] if isinstance(neighbors[0], dict) else {}
    canonical_text = top.get("memory") or top.get("data") or ""
    canonical_id = top.get("id")
    if not canonical_text:
        return {"action": "admit", "canonical_id": canonical_id, "detail": "neighbor-no-text"}

    try:
        verdict = judge_fn(canonical_text, text, timeout_s) or {}
    except Exception:  # noqa: BLE001 — fail open
        log.exception("nli write-gate judge raised; admitting (fail-open)")
        return {"action": "admit", "canonical_id": canonical_id, "detail": "judge-error"}

    if not verdict.get("ok"):
        # shim down / timeout / lock-contended -> admit (fail open)
        return {"action": "admit", "canonical_id": canonical_id,
                "detail": f"judge-unavailable:{verdict.get('error_type')}"}
    if verdict.get("contradicts") is not True:
        # NO or unparseable (None) -> not a confident contradiction -> admit
        return {"action": "admit", "canonical_id": canonical_id, "detail": "no-contradiction"}

    # Confident contradiction against a high-cosine canonical neighbor.
    return {"action": "flag", "canonical_id": canonical_id, "detail": "contradicts-canonical"}


def stamp_contradictions(records, user_id, brand, *, cosine_floor=0.5, topk=3, timeout_s=45,
                         search_fn=None, judge_fn=None, stamp_fn=None) -> list:
    """Background helper: evaluate each just-written record and, on a 'flag', call
    stamp_fn(memory_id, canonical_id). PURE (search/judge/stamp injected) so it is unit-testable
    without the server. Returns the list of {memory_id, canonical_id} actually stamped. Fail-soft:
    a per-record evaluate/stamp error is logged and skipped, never raised.

    records: iterable of {"id": <mid>, "memory": <text>}.
    """
    stamped = []
    for rec in (records or []):
        mid = (rec or {}).get("id")
        text = (rec or {}).get("memory") or (rec or {}).get("data") or ""
        if not mid or not text:
            continue
        try:
            decision = evaluate(text, user_id, brand, cosine_floor=cosine_floor, topk=topk,
                                timeout_s=timeout_s, search_fn=search_fn, judge_fn=judge_fn)
        except Exception:  # noqa: BLE001 — fail soft
            log.exception("stamp_contradictions: evaluate failed for %s", mid)
            continue
        if decision.get("action") != "flag":
            continue
        cid = decision.get("canonical_id")
        try:
            if stamp_fn is not None:
                stamp_fn(mid, cid)
            stamped.append({"memory_id": mid, "canonical_id": cid})
        except Exception:  # noqa: BLE001 — fail soft
            log.exception("stamp_contradictions: stamp_fn failed for %s", mid)
    return stamped
