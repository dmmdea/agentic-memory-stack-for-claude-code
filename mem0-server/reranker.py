"""bge-reranker-v2-m3 HTTP client + reorder helper.

Server: llama-swap @ http://127.0.0.1:11436 (always_loaded persistent group).
Endpoint: POST /v1/rerank   (llama-server `--reranking` flag exposes this).

Failure policy (lens A4): any error from the reranker (timeout, 5xx,
connection refused) returns the input order unchanged + logs a WARN. Search
never errors because reranking failed. To surface silent degradation, we
track consecutive failures and log on the 1st + every 10th.

W4 / review F11 — PASSIVE liveness, never an active probe. The capability
manifest needs a reranker verdict, but an ACTIVE rerank probe must NOT live in
/health/deep: this is a CPU cross-encoder (Test-MemoryStack budgets it 90s and
still WARNs on cold-model timeouts), and scripts/wsl/deploy.sh gates on
/health/deep immediately after a restart — an active probe there would hang
deploys on a cold or contended model. So ``rerank_stats`` below is bumped from
REAL rerank calls on the search path (in-process, zero I/O, mirroring
hook_contract.py's ``hook_contract_stats``) and capabilities.py reads it. The
active 3-doc probe stays where it can afford to be slow: TMS check L5.

Zero signal (no rerank traffic since this process started) maps to 'unknown',
never 'alive' (F9) — the counters are in-process and a restart zeroes them."""
from __future__ import annotations
import logging
import threading
import time
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

# Failure surfacing (lens A4) + W4 passive liveness counters (F11).
# ONE source of truth for the consecutive-failure count: the log-throttling
# ladder below and /health/deep's checks.reranker read the same number, so a
# manifest verdict can never disagree with what the WARN lines said.
# Fixed key names (cross-track contract — capabilities.py reads these):
#   last_rerank_ok_ts            epoch seconds of the last SUCCESSFUL rerank
#   consecutive_rerank_failures  reset to 0 on every success
rerank_stats: dict = {
    "last_rerank_ok_ts": None,
    "consecutive_rerank_failures": 0,
    "ok_total": 0,
    "fail_total": 0,
    "last_error": None,
}
_failure_lock = threading.Lock()


def skip_reason(results: Sequence[dict]) -> str | None:
    """W5 T1.2: pure skip-reason — None (rerank), 'small_n', or 'confident'.
    The two reasons were collapsed inside should_rerank for a year; the
    rerank_status stamp needs them apart."""
    if len(results) < RERANK_MIN_N:
        return "small_n"
    top = (results[0] or {}).get("score")
    if isinstance(top, (int, float)) and top >= RERANK_SKIP_IF_TOP_SCORE:
        return "confident"
    return None


def should_rerank(results: Sequence[dict]) -> bool:
    return skip_reason(results) is None


def rerank_health() -> dict:
    """Snapshot of the passive counters for /health/deep (zero I/O, never
    raises). Informational — it never flips the endpoint's ok."""
    with _failure_lock:
        return dict(rerank_stats)


def rerank(query: str, results: list[dict], text_key: str = "memory", *,
           force: bool = False, status_out: dict | None = None) -> list[dict]:
    """Reorder `results` by bge-reranker scores. Idempotent; original list is not mutated.

    A skipped rerank (should_rerank False) is NOT an attempt and bumps nothing:
    the counters must mean 'the transport was exercised', not 'search ran'.

    W5 T1.2/T5.3 (out-param, house stats_out pattern — the list-return
    signature is pinned): ``status_out['status']`` is set to one of
    ran | skipped_small_n | skipped_confident | failed_fallback_dense.
    ``force=True`` bypasses BOTH skip heuristics — the union leg passes it
    when lexical_only candidates are present, because a silent skip would
    delete every lexical rescue via the fail-closed drop (exactly the
    confidently-wrong-dense shape AMS-56 exists to fix; a 2-doc CPU rerank
    is cheap)."""
    reason = skip_reason(results)
    if reason is not None and not force:
        if status_out is not None:
            status_out["status"] = f"skipped_{reason}"
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
            rerank_stats["consecutive_rerank_failures"] = 0   # reset on success
            rerank_stats["last_rerank_ok_ts"] = time.time()
            rerank_stats["ok_total"] += 1
        if status_out is not None:
            status_out["status"] = "ran"
        return out
    except (httpx.HTTPError, ValueError, KeyError) as e:
        with _failure_lock:
            rerank_stats["consecutive_rerank_failures"] += 1
            rerank_stats["fail_total"] += 1
            rerank_stats["last_error"] = f"{e.__class__.__name__}: {e}"[:160]
            local_n = rerank_stats["consecutive_rerank_failures"]
        # Surface silent-degradation: WARN on first failure + every 10th thereafter
        if local_n == 1 or local_n % 10 == 0:
            log.warning("reranker unavailable (consecutive=%d), returning dense-only order: %s",
                        local_n, e)
        if status_out is not None:
            status_out["status"] = "failed_fallback_dense"
        return list(results)
