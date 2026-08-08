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

Canary determinism (review F6, hardened by the diff review): probing "some
recent point" would go green after a tokenizer/hash change that strands the
legacy corpus — the exact drift the canary exists to catch. So the canary
always targets the OLDEST bm25-bearing point (created_at min over a filtered
scroll) and derives its token deterministically (longest token, lexicographic
tiebreak) from that point's ``text_lemmatized``. The retrieval probe queries
``using='bm25'`` WITH a ``HasIdCondition`` filter on that exact point
(limit 1): the point returns iff its STORED sparse vector overlaps the
CURRENT encoder's token index — precisely the drift signal, and immune to
top-k displacement as the corpus grows (a top-5 membership check could go
red on a perfectly healthy leg once five newer points outscore the fixed
probe point). An encoder that cannot encode the query (returns ``None``)
means the leg is dead.

Empty-collection carve-out (diff review finding 2): a brand-new box with
ZERO points has nothing that can be dead — gating there would block every
deploy on a fresh install until the first memory lands. ``points == 0`` is
green with a note; a POPULATED corpus with zero bm25-bearing points still
gates (that is a dead leg's signature, not a fresh box's).
"""

import importlib.util


def _fastembed_available():
    """Module-local seam so headless tests (runners without fastembed) can
    stub availability without monkey-patching the shared importlib module —
    the W1 near-miss class."""
    return importlib.util.find_spec("fastembed") is not None


def encode_with_selfheal(store, text):
    """AMS-09b (2026-08-07, the leg's SECOND death): bounded un-poison of
    mem0's lazy BM25 encoder sentinel.

    mem0's ``_get_bm25_encoder`` caches ``False`` forever after one failed
    init. The second production death proved that failure can be transient:
    the fastembed model cache defaulted to /tmp (wiped by the WSL reboot) and
    the server runs ``HF_HUB_OFFLINE=1``, so one cache-missing init at boot
    poisoned the leg until a manual restart — even after the cache returned.
    This wrapper resets the poisoned sentinel at most ONCE per call and
    retries — and only when fastembed is importable, so a box genuinely
    missing the dependency never pays a doomed re-init on every health call.
    Under ``HF_HUB_OFFLINE`` the retry is a local-disk load or an immediate
    miss — it can never hang on a network fetch (verified: the 2026-08-07
    boot-window failures errored instantly on the local_files_only path).
    Healing here heals the WRITE path too (same store object, same encoder).
    Never raises.
    """
    try:
        sv = store._encode_bm25(text)
        if sv is not None:
            return sv
        if (getattr(store, "_bm25_encoder", None) is False
                and _fastembed_available()):
            store._bm25_encoder = None  # un-poison: allow exactly one fresh init
            sv = store._encode_bm25(text)
        return sv
    except Exception:
        return None


def pick_canary_token(text_lemmatized):
    """Deterministic token choice: longest token, lexicographic tiebreak."""
    tokens = [t for t in (text_lemmatized or "").split() if t]
    if not tokens:
        return None
    return sorted(tokens, key=lambda t: (-len(t), t))[0]


def evaluate_sparse_leg(fastembed_present, bm25_slot, points, with_bm25,
                        canary, error=None):
    """Pure verdict. GATING failures: fastembed missing, slot missing,
    canary did not hit (including 'no bm25-bearing point to probe' on a
    populated corpus). Coverage is reported but does NOT gate here —
    Test-MemoryStack WARNs below 0.95; a half-backfilled corpus with a live
    encoder is degraded, not dead."""
    coverage = (float(with_bm25) / float(points)) if points else 0.0
    if points == 0 and fastembed_present and bm25_slot:
        # Fresh install: nothing exists to probe, nothing can be dead.
        return {
            "ok": True,
            "fastembed": bool(fastembed_present),
            "bm25_slot": bool(bm25_slot),
            "points": 0, "with_bm25": 0, "coverage": 0.0,
            "canary": canary,
            "note": "empty collection — canary skipped",
        }
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


def sparse_leg_health(client, collection_name, encode_query_fn):
    """I/O wrapper. ``encode_query_fn(text)`` mirrors mem0's query-side
    encoder (``vector_store._encode_bm25`` — returns a sparse query object,
    or ``None`` when the encoder is unavailable). Never raises."""
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

        if points == 0:
            return evaluate_sparse_leg(
                fastembed_present, bm25_slot, 0, 0, canary,
            )

        # Oldest bm25-bearing point: filtered scroll, min created_at (no
        # payload index on created_at exists, so order_by is unavailable —
        # a full filtered walk at page 1000 measured ~0.2s on the live corpus.
        # String-min on ISO timestamps is deterministic, which is what the
        # canary needs; true age ordering is not load-bearing).
        oldest = None  # (sort_key, id, text_lemmatized)
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
                error="no bm25-bearing point to canary (populated corpus, zero sparse vectors)",
            )

        token = pick_canary_token(oldest[2])
        canary["token"] = token
        if token:
            sq = encode_query_fn(token)
            if sq is not None:
                canary["ran"] = True
                # Displacement-proof probe (diff review finding 3): filter to
                # the probe point's own id — it returns iff its STORED sparse
                # vector overlaps the CURRENT encoder's tokens. Top-k
                # membership would degrade as the corpus grows; this cannot.
                hits = client.query_points(
                    collection_name, query=sq, using="bm25",
                    query_filter=_qm.Filter(
                        must=[_qm.HasIdCondition(has_id=[oldest[1]])]),
                    limit=1,
                )
                ids = {str(h.id) for h in (getattr(hits, "points", None) or [])}
                canary["hit"] = oldest[1] in ids
        return evaluate_sparse_leg(
            fastembed_present, bm25_slot, points, with_bm25, canary,
        )
    except Exception as e:
        return evaluate_sparse_leg(False, False, 0, 0, canary, error=e)
