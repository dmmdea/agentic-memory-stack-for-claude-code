"""bge-reranker-v2-m3 HTTP client + reorder helper.

Server: llama-swap @ http://127.0.0.1:11436 (always_loaded persistent group).
Endpoint: POST /v1/rerank   (llama-server `--reranking` flag exposes this).

Failure policy (lens A4): any error from the reranker (timeout, 5xx,
connection refused) returns the input order unchanged + logs a WARN. Search
never errors because reranking failed. To surface silent degradation, we
track consecutive failures and log on the 1st + every 10th."""
from __future__ import annotations
import logging
import threading
from typing import Sequence

import httpx

log = logging.getLogger("mem0-server.reranker")

RERANK_URL = "http://127.0.0.1:11436/v1/rerank"
RERANK_MODEL = "bge-reranker-v2-m3"  # v0.14: upgraded from base (ctx 512) to v2-m3 (ctx 8192)
RERANK_TIMEOUT_S = 8.0
# Don't bother reranking trivially small or very confident result sets.
RERANK_MIN_N = 3
RERANK_SKIP_IF_TOP_SCORE = 0.92
# v2-m3 has 8192-token ctx; 6000 chars gives safe headroom for query + special tokens.
# (was 380 chars for bge-reranker-base which had 512-token ctx)
RERANK_DOC_MAX_CHARS = 6000  # v2-m3 ctx=8192; 6000 chars is safe room for query + special tokens

# Failure surfacing (lens A4)
_consecutive_failures = 0
_failure_lock = threading.Lock()


def should_rerank(results: Sequence[dict]) -> bool:
    if len(results) < RERANK_MIN_N:
        return False
    top = (results[0] or {}).get("score")
    if isinstance(top, (int, float)) and top >= RERANK_SKIP_IF_TOP_SCORE:
        return False
    return True


def rerank(query: str, results: list[dict], text_key: str = "memory") -> list[dict]:
    """Reorder `results` by bge-reranker scores. Idempotent; original list is not mutated."""
    global _consecutive_failures
    if not should_rerank(results):
        return list(results)
    docs = [str(r.get(text_key, "") or "")[:RERANK_DOC_MAX_CHARS] for r in results]
    try:
        r = httpx.post(
            RERANK_URL,
            json={"model": RERANK_MODEL, "query": query, "documents": docs, "top_n": len(docs)},
            timeout=RERANK_TIMEOUT_S,
        )
        r.raise_for_status()
        body = r.json()
        items = body.get("results") or body.get("data") or []
        # llama-server returns [{"index": int, "relevance_score": float}, ...]
        ordered = sorted(items, key=lambda x: float(x.get("relevance_score", 0.0)), reverse=True)
        out = []
        for it in ordered:
            idx = int(it.get("index", -1))
            if 0 <= idx < len(results):
                clone = dict(results[idx])
                clone["rerank_score"] = float(it.get("relevance_score", 0.0))
                out.append(clone)
        # Append any results the reranker didn't touch (defensive)
        seen = {id(results[int(it.get("index", -1))]) for it in ordered if 0 <= int(it.get("index", -1)) < len(results)}
        for r in results:
            if id(r) not in seen:
                out.append(r)
        with _failure_lock:
            _consecutive_failures = 0  # reset on success
        return out
    except (httpx.HTTPError, ValueError, KeyError) as e:
        with _failure_lock:
            _consecutive_failures += 1
            local_n = _consecutive_failures
        # Surface silent-degradation: WARN on first failure + every 10th thereafter
        if local_n == 1 or local_n % 10 == 0:
            log.warning("reranker unavailable (consecutive=%d), returning dense-only order: %s",
                        local_n, e)
        return list(results)
