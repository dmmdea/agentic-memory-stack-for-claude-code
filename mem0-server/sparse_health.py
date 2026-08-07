"""AMS-09 (audit 2026-08-07): BM25 sparse-leg liveness probe for /health/deep.

The lexical search leg died SILENTLY for 33 days: fastembed vanished from the
runtime venv, mem0 fail-softed to dense-only with a single one-shot log
warning, and shallow health stayed green while exact-identifier lookups
returned confidently wrong answers. This probe is the structural answer:
the leg's aliveness is now a GATING /health/deep check (a dead leg fails the
deploy gate — a blocked deploy on a rebuilt venv is the CORRECT outcome; the
2026-07 venv rebuild that killed the leg is precisely the event this must
catch).

Split for headless testability (house pattern: canonical_key_health):
- ``evaluate_sparse_leg(...)`` — pure verdict from observed facts.
- ``sparse_leg_health(...)`` — thin I/O wrapper; NEVER raises (a probe bug
  must not take down /health/deep for its other consumers — review F11).

Canary determinism (review F6): probing "some recent point" would go green
after a tokenizer/hash change that strands the legacy corpus — the exact
drift the canary exists to catch. So the canary always targets the OLDEST
bm25-bearing point (created_at min over a filtered scroll) and derives its
token deterministically (longest token, lexicographic tiebreak) from that
point's ``text_lemmatized``. The point id must appear anywhere in the top-5
keyword results; a ``None`` from keyword_search means the encoder is dead.
"""

import importlib.util


def pick_canary_token(text_lemmatized):
    """Deterministic token choice: longest token, lexicographic tiebreak."""
    tokens = [t for t in (text_lemmatized or "").split() if t]
    if not tokens:
        return None
    return sorted(tokens, key=lambda t: (-len(t), t))[0]


def evaluate_sparse_leg(fastembed_present, bm25_slot, points, with_bm25,
                        canary, error=None):
    """Pure verdict. GATING failures: fastembed missing, slot missing,
    canary did not hit (including 'no bm25-bearing point to probe').
    Coverage is reported but does NOT gate here — Test-MemoryStack WARNs
    below 0.95; a half-backfilled corpus with a live encoder is degraded,
    not dead."""
    coverage = (float(with_bm25) / float(points)) if points else 0.0
    ok = bool(fastembed_present and bm25_slot
              and canary and canary.get("ran") and canary.get("hit"))
    out = {
        "ok": ok,
        "fastembed": bool(fastembed_present),
        "bm25_slot": bool(bm25_slot),
        "points": int(points or 0),
        "with_bm25": int(with_bm25 or 0),
        "coverage": round(coverage, 4),
        "canary": canary,
    }
    if error:
        out["error"] = str(error)[:160]
    return out


def sparse_leg_health(client, collection_name, keyword_search_fn):
    """I/O wrapper. ``keyword_search_fn(query, top_k)`` is mem0's
    ``vector_store.keyword_search`` (returns a result list, or ``None`` when
    the encoder is unavailable). Never raises."""
    canary = {"ran": False, "hit": False, "token": None}
    try:
        from qdrant_client import models as _qm

        fastembed_present = importlib.util.find_spec("fastembed") is not None

        info = client.get_collection(collection_name)
        sparse_cfg = getattr(info.config.params, "sparse_vectors", None) or {}
        bm25_slot = "bm25" in sparse_cfg

        points = client.count(collection_name, exact=True).count
        bm25_filter = _qm.Filter(must=[_qm.HasVectorCondition(has_vector="bm25")])
        with_bm25 = client.count(
            collection_name, count_filter=bm25_filter, exact=True
        ).count

        # Oldest bm25-bearing point: filtered scroll, min created_at (no
        # payload index on created_at exists, so order_by is unavailable —
        # a full filtered walk at page 1000 measured ~0.2s on the live corpus).
        oldest = None  # (created_at, id, text_lemmatized)
        offset = None
        while True:
            pts, offset = client.scroll(
                collection_name, scroll_filter=bm25_filter,
                with_payload=["created_at", "text_lemmatized"],
                with_vectors=False, limit=1000, offset=offset,
            )
            for pt in pts:
                p = pt.payload or {}
                key = (str(p.get("created_at") or "~"), str(pt.id))
                if oldest is None or key < oldest[0]:
                    oldest = (key, str(pt.id), p.get("text_lemmatized") or "")
            if offset is None:
                break

        if oldest is None:
            return evaluate_sparse_leg(
                fastembed_present, bm25_slot, points, with_bm25, canary,
                error="no bm25-bearing point to canary",
            )

        token = pick_canary_token(oldest[2])
        canary["token"] = token
        if token:
            canary["ran"] = True
            hits = keyword_search_fn(token, 5)
            ids = {str(getattr(h, "id", None) or (h.get("id") if isinstance(h, dict) else None))
                   for h in (hits or [])}
            canary["hit"] = oldest[1] in ids
        return evaluate_sparse_leg(
            fastembed_present, bm25_slot, points, with_bm25, canary,
        )
    except Exception as e:
        return evaluate_sparse_leg(False, False, 0, 0, canary, error=e)
