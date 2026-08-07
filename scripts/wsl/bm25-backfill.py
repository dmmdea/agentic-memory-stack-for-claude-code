#!/usr/bin/env python3
"""bm25-backfill.py -- AMS-09: backfill the named sparse vector 'bm25'.

Audit finding AMS-09: points written before the hybrid-search migration (or
through paths that skipped it) lack the named sparse vector 'bm25', so they are
invisible to the lexical half of hybrid retrieval.

This is a ONE-OFF migration with a receipts discipline: every live batch write
appends one JSONL receipt line {ts, script, record_id, action, before, after}
(record_id spans the batch as 'first_id..last_id'; after carries ids_count,
first_id, last_id) to a receipts file under ~/.mem0 (env-overridable), so the
migration leaves a complete audit trail.

--report: count points with/without the bm25 named vector (has_vector filter
through the points/count API), sample missing ids, and run a LEGACY-COMPAT
CHECK: take one bm25-bearing point, pick a distinctive token from its
text_lemmatized (longest token, lexical tiebreak), encode that token with the
CURRENT fastembed SparseTextEmbedding('Qdrant/bm25'), query with using='bm25'
limit 10, and assert the point comes back. A fail means the stored legacy
vectors were produced by an incompatible encoder -- the report then recommends
including the legacy set in scope via --rewrite-legacy.

--apply --dry-run / --apply --live: scroll all points, skip those that already
carry a bm25 vector (idempotent; --rewrite-legacy includes them), take the text
exactly as mem0's insert() does (text_lemmatized, falling back to data), skip
empty text, encode with fastembed, and write via update_vectors in batches --
payload and dense vector UNTOUCHED.

Usage:
  bm25-backfill.py                     # --report (default): read-only scan -> JSON report
  bm25-backfill.py --report
  bm25-backfill.py --apply --dry-run   # zero writes; prints plan + samples
  bm25-backfill.py --apply --live      # OPERATOR-GATED
  bm25-backfill.py --apply --live --rewrite-legacy   # also re-encode bm25-bearing points

Exit codes: 0 success; 2 gating error (bare --apply, bad flag combination);
3 precondition failure (Qdrant not ready, fastembed missing, collection lacks
the bm25 sparse slot). Batch failures during a live run exit 1.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Environment-driven configuration (same defaults as the sibling scripts)
# ---------------------------------------------------------------------------

QDRANT = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.environ.get("MEM0_COLLECTION", "mem0_egemma_768")
REPORT_PATH = Path(os.environ.get("MEM0_BM25_REPORT", str(Path.home() / ".mem0" / "bm25-backfill-report.json")))
RECEIPTS_PATH = Path(os.environ.get("MEM0_BM25_RECEIPTS", str(Path.home() / ".mem0" / "bm25-backfill-receipts.jsonl")))

SCRIPT_NAME = "bm25-backfill"
_SCROLL_PAGE = 256
_BATCH_SIZE = 128
_PROGRESS_EVERY = 500


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def qdrant_ready() -> bool:
    """Readyz preflight -- every mode requires a reachable Qdrant."""
    try:
        httpx.get(f"{QDRANT}/readyz", timeout=5.0).raise_for_status()
        return True
    except Exception as e:
        print(f"ERROR: Qdrant readyz preflight failed: {e}", file=sys.stderr)
        return False


def collection_has_bm25_slot() -> bool | None:
    """True/False for the bm25 sparse slot; None when the collection is
    missing or unreadable."""
    try:
        r = httpx.get(f"{QDRANT}/collections/{COLLECTION}", timeout=10.0)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        params = ((r.json().get("result") or {}).get("config") or {}).get("params") or {}
        return "bm25" in (params.get("sparse_vectors") or {})
    except httpx.HTTPError as e:
        print(f"ERROR: cannot read collection config: {e}", file=sys.stderr)
        return None


def preflight() -> int | None:
    """Shared gate for every mode. Returns an exit code on failure, else None."""
    if not qdrant_ready():
        return 3
    slot = collection_has_bm25_slot()
    if slot is None:
        print(f"ERROR: collection '{COLLECTION}' not found or unreadable.", file=sys.stderr)
        return 3
    if not slot:
        print(f"ERROR: collection '{COLLECTION}' lacks the 'bm25' sparse vector slot; "
              "nothing to backfill into.", file=sys.stderr)
        return 3
    try:
        import fastembed  # noqa: F401
    except Exception as e:
        print(f"ERROR: fastembed not importable in this environment: {e}", file=sys.stderr)
        return 3
    return None


_BM25_MODEL = None


def _bm25_model():
    """Lazy singleton for the fastembed bm25 sparse encoder."""
    global _BM25_MODEL
    if _BM25_MODEL is None:
        from fastembed import SparseTextEmbedding
        _BM25_MODEL = SparseTextEmbedding("Qdrant/bm25")
    return _BM25_MODEL


# ---------------------------------------------------------------------------
# Receipts (one-off migration discipline: one JSONL line per live batch)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_batch_receipt(action: str, ids: list, had_bm25: int) -> None:
    RECEIPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _now_iso(),
        "script": SCRIPT_NAME,
        "record_id": f"{ids[0]}..{ids[-1]}",
        "action": action,
        "before": {"bm25": "absent" if had_bm25 == 0 else f"present on {had_bm25} of {len(ids)}"},
        "after": {
            "bm25": "sparse-vector",
            "ids_count": len(ids),
            "first_id": str(ids[0]),
            "last_id": str(ids[-1]),
        },
    }
    with RECEIPTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Scan (shared by --report fallback and --apply)
# ---------------------------------------------------------------------------

def _record_has_bm25(rec) -> bool:
    return isinstance(rec.vector, dict) and "bm25" in rec.vector


def scan_points(qc) -> list[tuple[Any, str, bool]]:
    """Scroll every point with payload + the bm25 vector selector only.
    Returns (id, text, has_bm25) tuples; text follows mem0's insert() source
    precedence exactly (text_lemmatized, falling back to data)."""
    out: list[tuple[Any, str, bool]] = []
    offset = None
    scanned = 0
    while True:
        records, offset = qc.scroll(
            collection_name=COLLECTION,
            limit=_SCROLL_PAGE,
            offset=offset,
            with_payload=True,
            with_vectors=["bm25"],
        )
        for rec in records:
            payload = rec.payload or {}
            text = payload.get("text_lemmatized") or payload.get("data", "")
            out.append((rec.id, text if isinstance(text, str) else "", _record_has_bm25(rec)))
            scanned += 1
            if scanned % _PROGRESS_EVERY == 0:
                print(f"  scanned {scanned} points...", flush=True)
        if offset is None:
            break
    return out


# ---------------------------------------------------------------------------
# Report mode
# ---------------------------------------------------------------------------

def _counts_via_count_api(qc, models) -> tuple[int, int] | None:
    """(total, with_bm25) via the points/count API using a has_vector filter;
    None when the server/client pair does not support the condition."""
    try:
        flt = models.Filter(must=[models.HasVectorCondition(has_vector="bm25")])
        total = qc.count(collection_name=COLLECTION, exact=True).count
        with_bm25 = qc.count(collection_name=COLLECTION, count_filter=flt, exact=True).count
        return total, with_bm25
    except Exception as e:
        print(f"  NOTE: has_vector count filter unavailable ({e}); "
              "falling back to a full scroll.", flush=True)
        return None


def _sample_missing_ids(qc, models, limit: int = 5) -> list[str] | None:
    try:
        flt = models.Filter(must_not=[models.HasVectorCondition(has_vector="bm25")])
        records, _ = qc.scroll(collection_name=COLLECTION, scroll_filter=flt,
                               limit=limit, with_payload=False, with_vectors=False)
        return [str(r.id) for r in records]
    except Exception:
        return None


def legacy_compat_check(qc, models) -> dict:
    """Encode a distinctive token from a bm25-bearing point with the CURRENT
    encoder and verify the point is retrievable through using='bm25'. A fail
    means legacy vectors are incompatible with the current encoder."""
    try:
        flt = models.Filter(must=[models.HasVectorCondition(has_vector="bm25")])
        records, _ = qc.scroll(collection_name=COLLECTION, scroll_filter=flt,
                               limit=16, with_payload=True, with_vectors=False)
    except Exception as e:
        return {"status": "skipped", "reason": f"cannot scroll bm25-bearing points: {e}"}
    for rec in records:
        payload = rec.payload or {}
        text = payload.get("text_lemmatized") or payload.get("data", "")
        if not isinstance(text, str):
            continue
        tokens = re.findall(r"\S+", text)
        if not tokens:
            continue
        # Distinctive token: longest, lexical tiebreak.
        token = sorted(tokens, key=lambda t: (-len(t), t))[0]
        emb = next(iter(_bm25_model().embed([token])))
        indices = [int(i) for i in emb.indices]
        values = [float(v) for v in emb.values]
        if not indices:
            continue
        resp = qc.query_points(
            collection_name=COLLECTION,
            query=models.SparseVector(indices=indices, values=values),
            using="bm25",
            limit=10,
            with_payload=False,
        )
        hit_ids = {str(p.id) for p in resp.points}
        status = "pass" if str(rec.id) in hit_ids else "fail"
        return {"status": status, "probe_id": str(rec.id), "token": token,
                "hits": sorted(hit_ids)}
    return {"status": "skipped", "reason": "no bm25-bearing point with usable text found"}


def run_report() -> int:
    print(f"=== {SCRIPT_NAME} --report (read-only) ===\n")
    gate = preflight()
    if gate is not None:
        return gate

    from qdrant_client import QdrantClient, models
    qc = QdrantClient(url=QDRANT)

    counts = _counts_via_count_api(qc, models)
    sample = _sample_missing_ids(qc, models)
    if counts is None or sample is None:
        # Fallback path for servers without has_vector filter support.
        points = scan_points(qc)
        total = len(points)
        with_bm25 = sum(1 for _, _, has in points if has)
        sample = [str(pid) for pid, _, has in points if not has][:5]
    else:
        total, with_bm25 = counts
    without_bm25 = total - with_bm25

    print("--- COUNTS ---")
    print(f"  total points        : {total}")
    print(f"  with bm25 vector    : {with_bm25}")
    print(f"  without bm25 vector : {without_bm25}")
    print(f"  sample missing ids  : {sample}")

    print("\n--- LEGACY-COMPAT CHECK ---")
    compat = legacy_compat_check(qc, models)
    print(f"  status: {compat['status']}")
    for k, v in compat.items():
        if k != "status":
            print(f"  {k}: {v}")
    if compat["status"] == "fail":
        print("  RECOMMENDATION: stored legacy bm25 vectors are incompatible with the "
              "current encoder; include them in scope with --apply ... --rewrite-legacy.")

    report = {
        "generated_at": _now_iso(),
        "script": SCRIPT_NAME,
        "collection": COLLECTION,
        "total": total,
        "with_bm25": with_bm25,
        "without_bm25": without_bm25,
        "sample_missing_ids": sample,
        "legacy_compat": compat,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nJSON report written: {REPORT_PATH}")
    print("ZERO writes performed.")
    return 0


# ---------------------------------------------------------------------------
# Apply mode
# ---------------------------------------------------------------------------

def run_apply(*, dry_run: bool, rewrite_legacy: bool) -> int:
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"=== {SCRIPT_NAME} --apply ({mode}, rewrite_legacy={rewrite_legacy}) ===\n")
    gate = preflight()
    if gate is not None:
        return gate

    from qdrant_client import QdrantClient, models
    qc = QdrantClient(url=QDRANT)

    print("Scrolling all points (payload + bm25 vector selector only)...")
    points = scan_points(qc)

    pending: list[tuple[Any, str, bool]] = []
    skipped_existing = 0
    skipped_empty = 0
    for pid, text, has_bm25 in points:
        if has_bm25 and not rewrite_legacy:
            skipped_existing += 1  # idempotent re-run: already backfilled
            continue
        if not text.strip():
            skipped_empty += 1  # nothing to encode; never write an artificial vector
            continue
        pending.append((pid, text, has_bm25))

    print(f"\n  total scanned      : {len(points)}")
    print(f"  already have bm25  : {skipped_existing} (skipped)")
    print(f"  empty text         : {skipped_empty} (skipped)")
    print(f"  to write           : {len(pending)}")

    if dry_run:
        sample = [str(pid) for pid, _, _ in pending[:10]]
        print(f"  sample ids         : {sample}")
        print("\nDRY-RUN complete. ZERO writes performed.")
        return 0

    if not pending:
        print("\nNothing to do.")
        return 0

    action = "bm25-rewrite" if rewrite_legacy else "bm25-backfill"
    model = _bm25_model()
    written = 0
    errors = 0
    for start in range(0, len(pending), _BATCH_SIZE):
        batch = pending[start:start + _BATCH_SIZE]
        ids = [pid for pid, _, _ in batch]
        texts = [text for _, text, _ in batch]
        had_bm25 = sum(1 for _, _, has in batch if has)
        try:
            embeddings = list(model.embed(texts))
            point_vectors = [
                models.PointVectors(
                    id=pid,
                    vector={"bm25": models.SparseVector(
                        indices=[int(i) for i in emb.indices],
                        values=[float(v) for v in emb.values],
                    )},
                )
                for pid, emb in zip(ids, embeddings)
            ]
            # update_vectors touches ONLY the named vectors it carries: the
            # payload and the dense vector stay untouched.
            qc.update_vectors(collection_name=COLLECTION, points=point_vectors, wait=True)
        except Exception as e:
            errors += len(batch)
            print(f"  ERROR batch {ids[0]}..{ids[-1]}: {e}", file=sys.stderr)
            continue
        write_batch_receipt(action, ids, had_bm25)
        prev = written
        written += len(batch)
        if written // _PROGRESS_EVERY != prev // _PROGRESS_EVERY:
            print(f"  written {written} / {len(pending)}...", flush=True)

    print(f"\n=== apply done: written={written} skipped-existing={skipped_existing} "
          f"skipped-empty={skipped_empty} errors={errors} ===")
    print(f"Receipts appended to: {RECEIPTS_PATH}")
    return 0 if errors == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--report", action="store_true", default=False,
                    help="Read-only counts + legacy-compat check -> JSON report "
                         "(default action)")
    ap.add_argument("--apply", action="store_true", default=False,
                    help="Backfill missing bm25 vectors (requires --dry-run or --live; "
                         "bare --apply exits 2)")
    ap.add_argument("--dry-run", action="store_true", default=False, dest="dry_run",
                    help="With --apply: print the plan, zero writes")
    ap.add_argument("--live", action="store_true", default=False,
                    help="With --apply: perform live mutations (OPERATOR-GATED)")
    ap.add_argument("--rewrite-legacy", action="store_true", default=False,
                    dest="rewrite_legacy",
                    help="Also re-encode points that already carry a bm25 vector "
                         "(use when the report's legacy-compat check fails)")
    args = ap.parse_args()

    if args.apply:
        if not args.dry_run and not args.live:
            print(
                "ERROR: --apply requires either --dry-run or --live.\n"
                "  --dry-run : safe preview, zero writes\n"
                "  --live    : OPERATOR-GATED live mutation\n"
                "Bare --apply is intentionally blocked to prevent accidental mutation.",
                file=sys.stderr,
            )
            return 2
        if args.dry_run and args.live:
            print("error: pass exactly one of --dry-run / --live", file=sys.stderr)
            return 2
        return run_apply(dry_run=args.dry_run, rewrite_legacy=args.rewrite_legacy)

    # --report is the default action
    return run_report()


if __name__ == "__main__":
    sys.exit(main())
