#!/usr/bin/env python3
"""ship-log-reclassify.py — Phase 3 backfill: scanner + gated apply.

Scans tier=evidence mem0 records, identifies ship-log candidates using the
Python port of Test-IsShipLog (memory-common.ps1), and emits a candidate diff
for operator review.  --apply requires --dry-run or --live (bare --apply exits 2
with a guard error so a typo cannot mutate live data).

Usage:
  python3 scripts/wsl/ship_log_reclassify.py            # --report (default)
  python3 scripts/wsl/ship_log_reclassify.py --report
  python3 scripts/wsl/ship_log_reclassify.py --apply --dry-run   # safe: zero writes
  python3 scripts/wsl/ship_log_reclassify.py --apply --live      # OPERATOR-GATED

Operator decisions (binding):
  D1 = CONSERVATIVE: only conservative=true entries (>800 chars) are processed.
  D2 = SOFT-RETIRE (reversible): retrievable=false + retired_at stamp (matches
       stamp-retired-at.py mechanism).  Record stays in Qdrant; drops from search.
  D3 = ONE dedicated episode per reclassified record (clean provenance).
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
# Qdrant scroll — bypasses mem0 GET /v1/memories 500-record cap
# (same idiom as l10-audit.py scroll_all_qdrant_points)
# ---------------------------------------------------------------------------

QDRANT = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
MEM0 = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
COLLECTION = "memories"
_SCROLL_PAGE = 256
_REPORT_JSON_PATH = Path.home() / ".mem0" / "ship-log-reclassify-report.json"


def scroll_all_qdrant_points(client: httpx.Client) -> list[dict]:
    """Page through every point in the memories collection via Qdrant scroll API.

    Returns a list of dicts with ``id`` + ``payload`` (no vector).  The mem0
    REST endpoint caps at 500 and ignores offset; this reads every record.
    Identical idiom to l10-audit.py.
    """
    points: list[dict] = []
    next_page: Any = None
    while True:
        body: dict[str, Any] = {
            "limit": _SCROLL_PAGE,
            "with_payload": True,
            "with_vector": False,
        }
        if next_page is not None:
            body["offset"] = next_page
        r = client.post(
            f"{QDRANT}/collections/{COLLECTION}/points/scroll",
            json=body,
            timeout=10.0,
        )
        r.raise_for_status()
        result = r.json().get("result", {})
        points.extend(result.get("points", []))
        next_page = result.get("next_page_offset")
        if not next_page:
            break
    return points


# ---------------------------------------------------------------------------
# Test-artifact exclusion — mirrors test-debris-purge.py's pattern
# Works with Qdrant payload field names (source is a top-level payload key,
# same as l10-audit.py reads: payload.get("source")).
# ---------------------------------------------------------------------------

_TEST_ID_RE = re.compile(r"qclass-.*-test-", re.IGNORECASE)
_TEST_SOURCE_RE = re.compile(r"test", re.IGNORECASE)


def is_test_artifact(point: dict) -> bool:
    """Exclude records that look like test debris (id or source pattern).

    ``point`` is a Qdrant scroll result dict with top-level ``id`` and
    ``payload`` keys (mirrors l10-audit.py's per-point structure).
    """
    rid = str(point.get("id", "") or "")
    payload = point.get("payload") or {}
    source = payload.get("source", "") or ""
    if _TEST_ID_RE.search(rid):
        return True
    if _TEST_SOURCE_RE.search(source):
        return True
    return False


# ---------------------------------------------------------------------------
# Test-IsShipLog ported to Python — faithful 4-rule port of memory-common.ps1
#
# PS regex flags:
#   -imatch  = case-insensitive match   → re.IGNORECASE
#   -match   = case-sensitive match     → no flag (default)
#   \b       = word boundary            → \b in Python re
#
# multiClause: ($t -split '\r?\n').Count -gt 1  OR  ([regex]::Matches($t, ',')).Count -gt 3
#   → len(text.splitlines()) > 1  OR  text.count(',') > 3
#
# Rule order (EXACTLY as in PS):
#   1. len < 150 AND atomicMarker  → False (KEEP)  ← fires BEFORE shipSignal check
#   2. shipSignal                  → True  (ROUTE)
#   3. len >= 150 AND NOT atomicMarker → True (ROUTE)
#   4. default                     → False (KEEP)
# ---------------------------------------------------------------------------

_STATUS_VERB = re.compile(
    r'\b(shipped|deployed|done|committed|completed|verified|fixed|started|added|updated'
    r'|migrated|landed|merged|refactored|wired|removed|renamed)\b',
    re.IGNORECASE,
)
_DATE_ANCHOR = re.compile(r'\b20\d{2}-\d{2}-\d{2}\b')

# atomicMarker: same alternation as PS, translated to Python.
# PS: '\b(reserved|token|endpoint|credential|secret|password|version|port|path|hash|key|id
#       |anchor|url)\b|https?://|:\d{2,5}\b|\w+\s*=\s*\S|\bset to\b|[A-Za-z]:\\
#       |\.(ps1|py|js|ts|json|md|sh|exe|dll|yaml|yml|toml|cfg|conf)\b'
# Note: PS -imatch = IGNORECASE; the original atomicMarker uses -imatch.
_ATOMIC_MARKER = re.compile(
    r'\b(reserved|token|endpoint|credential|secret|password|version|port|path|hash|key|id'
    r'|anchor|url)\b'
    r'|https?://'
    r'|:\d{2,5}\b'
    r'|\w+\s*=\s*\S'
    r'|\bset to\b'
    r'|[A-Za-z]:\\'
    r'|\.(ps1|py|js|ts|json|md|sh|exe|dll|yaml|yml|toml|cfg|conf)\b',
    re.IGNORECASE,
)


def is_ship_log(text: str) -> bool:
    """Python port of Test-IsShipLog from memory-common.ps1.

    Returns True if the text is a volatile ship-log fact (should route to episodic),
    False if it is a durable value fact (KEEP in mem0).
    Over-KEEP is the hard constraint: a durable fact must never route.
    """
    if not text or not text.strip():
        return False
    t = text.strip()

    # multiClause: more than one line OR more than 3 commas
    multi_clause = (len(t.splitlines()) > 1) or (t.count(',') > 3)
    ship_signal = bool(_STATUS_VERB.search(t)) and (bool(_DATE_ANCHOR.search(t)) or multi_clause)

    # Rule 1: short value facts → absolute KEEP
    if len(t) < 150 and _ATOMIC_MARKER.search(t):
        return False
    # Rule 2: clear status events (ship-signal) → route, any length
    if ship_signal:
        return True
    # Rule 3: long records with NO value marker → crowders → route
    if len(t) >= 150 and not _ATOMIC_MARKER.search(t):
        return True
    # Rule 4: default → KEEP
    return False


# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

_CONSERVATIVE_LEN = 800


def _api_key() -> str:
    """Read mem0 API key from ~/.mem0/api-key."""
    return (Path.home() / ".mem0" / "api-key").read_text(encoding="utf-8").strip()


def _mem0_headers() -> dict[str, str]:
    return {"X-API-Key": _api_key(), "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Apply mode — soft-retire + per-record episode (D1/D2/D3)
# ---------------------------------------------------------------------------

def _is_already_processed(client: httpx.Client, headers: dict, record_id: str) -> bool:
    """Idempotency check: return True if the record is already soft-retired OR
    already carries the reclassify marker.  Queries Qdrant directly (same
    approach as stamp-retired-at.py scroll_all).
    """
    try:
        r = client.post(
            f"{QDRANT}/collections/{COLLECTION}/points",
            json={"ids": [record_id], "with_payload": True, "with_vector": False},
            timeout=10.0,
        )
        r.raise_for_status()
        pts = r.json().get("result", [])
        if not pts:
            return False
        payload = pts[0].get("payload") or {}
        # Already soft-retired (D2): the PATCH stores retrievable=False at the TOP
        # LEVEL of the Qdrant payload (set_payload shallow-merge — no nested
        # metadata wrapper).  Read from the same top-level location that the
        # Task-3 scanner and run_retire_only use for tier/brand/source.
        if payload.get("retrievable") is False:
            return True
        # Legacy: earlier backfill runs stamped a reclassify audit marker.
        if payload.get("reclassified_to_episode"):
            return True
        return False
    except Exception as e:
        print(f"  IDEMPOTENCY-CHECK-FAIL {record_id[:8]}: {e}", file=sys.stderr)
        return False


def _post_episode(client: httpx.Client, headers: dict, rec: dict, now_iso: str) -> str | None:
    """POST one episode for the given record.  Returns the session_id on success,
    None on failure.  Uses the same EpisodeIn shape as l1a-extract.ps1.
    """
    session_id = f"shiplog-reclass-{rec['id']}"
    payload = {
        "session_id": session_id,
        "started_at": now_iso,
        "ended_at": now_iso,
        "goal": "Reclassified ship-log (Phase 3 backfill)",
        "summary": rec["text"],
        "brand": rec.get("brand") or "ecosystem",
        "workspace": "ai-ecosystem",
        "project": "ecosystem",
    }
    try:
        r = client.post(f"{MEM0}/v1/episodes", json=payload, headers=headers, timeout=15.0)
        if r.status_code // 100 == 2:
            return session_id
        print(
            f"  EPISODE-FAIL {rec['id'][:8]}: status={r.status_code} body={r.text[:200]}",
            flush=True,
        )
        return None
    except Exception as e:
        print(f"  EPISODE-EXCEPTION {rec['id'][:8]}: {e}", flush=True)
        return None


def _soft_retire(client: httpx.Client, headers: dict, record_id: str,
                 session_id: str, now_iso: str) -> bool:
    """Soft-retire a mem0 evidence record via PATCH /metadata (retrievable=false).

    Uses actor=backfill-apply-v013, which is the only actor permitted to write
    the `retrievable` key per app.py _LEGACY_ACTOR_KEYS.  That actor may write
    ONLY `retrievable` — sending any additional metadata key will 403.  So the
    body contains exactly {"retrievable": False} and nothing else.

    Idempotency relies on retrievable==False (the _is_already_processed check
    treats that as processed).  The reclassified_to_episode audit marker is NOT
    written here (the actor would 403); provenance lives in the episode record.

    Returns True on success.
    """
    try:
        r = client.patch(
            f"{MEM0}/v1/memories/{record_id}/metadata",
            json={
                "metadata": {
                    "retrievable": False,
                },
                "actor": "backfill-apply-v013",
                "reason": "Phase 3 backfill: ship-log rerouted to episodic (D1 conservative set)",
            },
            headers=headers,
            timeout=10.0,
        )
        if r.status_code == 200:
            return True
        print(
            f"  RETIRE-FAIL {record_id[:8]}: status={r.status_code} body={r.text[:200]}",
            flush=True,
        )
        return False
    except Exception as e:
        print(f"  RETIRE-EXCEPTION {record_id[:8]}: {e}", flush=True)
        return False


# ORPHAN LIMITATION: episode INSERT has no session_id dedupe and there is no
# by-session_id lookup.  If a record's episode POST succeeds but its soft-retire
# PATCH then fails, the record stays retrievable (safe) and is logged as
# "soft-retire failed (episode already created)".  Do NOT blind re-run --live
# (it would duplicate that record's episode).  Instead, after a --live run,
# inspect the error count + logs; for any episode-created/retire-failed record,
# retire it directly (stamp-retired-at.py pattern) — do not re-run the whole batch.


def run_apply(*, dry_run: bool) -> int:
    """Apply mode: process conservative candidates from the report JSON.

    --dry-run: zero writes, prints what would happen + 10-record sample.
    --live: performs the real PATCH/POST calls.

    Per D1: only conservative=true entries (>800 chars) are processed.
    Per D2: soft-retire via PATCH /metadata (retrievable=false + retired_at).
    Per D3: one episode per record.
    Idempotent: re-running is a no-op on already-processed records.
    Never retires a record whose episode POST failed.
    """
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"=== ship-log-reclassify --apply ({mode}) ===\n")

    if not _REPORT_JSON_PATH.exists():
        print(
            f"ERROR: report JSON not found at {_REPORT_JSON_PATH}\n"
            "Run --report first to generate the candidate set.",
            file=sys.stderr,
        )
        return 1

    all_candidates: list[dict] = json.loads(_REPORT_JSON_PATH.read_text(encoding="utf-8"))
    # D1: conservative set only
    candidates = [c for c in all_candidates if c.get("conservative")]
    print(f"  Report JSON: {len(all_candidates)} total candidates")
    print(f"  Conservative subset (D1, >800 chars): {len(candidates)}")

    if dry_run:
        # In dry-run we check idempotency by querying Qdrant (read-only).
        print("\n  Checking idempotency state (read-only Qdrant query)...", flush=True)
        already_done = 0
        to_process: list[dict] = []
        try:
            with httpx.Client() as qclient:
                for c in candidates:
                    if _is_already_processed(qclient, {}, c["id"]):
                        already_done += 1
                    else:
                        to_process.append(c)
        except Exception as e:
            print(f"  WARNING: Qdrant idempotency check failed ({e}); counts may be approximate.")
            to_process = candidates

        print(f"\n  Would process  : {len(to_process)}")
        print(f"  Would skip     : {already_done} (already retired / reclassified)")
        print(f"  ZERO writes performed (dry-run).")

        sample = to_process[:10]
        if sample:
            print(f"\n  --- SAMPLE (up to 10 of {len(to_process)} to-process) ---")
            print(f"  {'id[:8]':<10} {'len':>6}  {'brand':<16}  first 90 chars")
            print(f"  {'-'*8:<10} {'-'*6:>6}  {'-'*16:<16}  {'-'*30}")
            for c in sample:
                brand = (c.get("brand") or "")[:16]
                snippet = c["text"].replace("\n", " ")[:90]
                print(f"  {c['id'][:8]:<10} {c['len']:>6}  {brand:<16}  {snippet!r}")
                print(f"    → would create episode shiplog-reclass-{c['id']} + soft-retire")
        print("\nDRY-RUN complete. No data modified.")
        return 0

    # ---- LIVE apply ----
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"  run_ts={now_iso}")
    try:
        headers = _mem0_headers()
    except Exception as e:
        print(f"ERROR: cannot read API key: {e}", file=sys.stderr)
        return 1

    # Preflight: Qdrant reachable?
    try:
        httpx.get(f"{QDRANT}/readyz", timeout=5).raise_for_status()
    except Exception as e:
        print(f"ERROR: Qdrant unreachable: {e}", file=sys.stderr)
        return 1

    created = 0
    retired = 0
    skipped = 0
    errors = 0
    orphan_retire_failed = 0  # episode created but soft-retire failed → needs manual retire

    with httpx.Client() as client:
        for c in candidates:
            rec_id = c["id"]
            # Idempotency check
            if _is_already_processed(client, headers, rec_id):
                skipped += 1
                print(f"  SKIP {rec_id[:8]} (already processed)", flush=True)
                continue

            # D3: create one episode
            session_id = _post_episode(client, headers, c, now_iso)
            if session_id is None:
                errors += 1
                print(f"  ERROR {rec_id[:8]}: episode POST failed — NOT retiring", flush=True)
                continue
            created += 1
            print(f"  EPISODE {rec_id[:8]} → session_id={session_id}", flush=True)

            # D2: soft-retire ONLY after episode POST succeeds
            ok = _soft_retire(client, headers, rec_id, session_id, now_iso)
            if ok:
                retired += 1
                print(f"  RETIRED {rec_id[:8]}", flush=True)
            else:
                errors += 1
                orphan_retire_failed += 1
                print(
                    f"  ERROR {rec_id[:8]}: soft-retire failed (episode already created: {session_id})",
                    flush=True,
                )

    print(
        f"\n=== apply done: created={created} retired={retired} "
        f"skipped={skipped} errors={errors} "
        f"orphan-retire-failed={orphan_retire_failed} ===",
        flush=True,
    )
    if orphan_retire_failed:
        print(
            f"  WARNING: {orphan_retire_failed} record(s) had episode created but soft-retire failed.\n"
            "  Do NOT re-run --live (would duplicate episodes). Retire these manually via stamp-retired-at.py.",
            file=sys.stderr,
        )
    return 0 if errors == 0 else 1


# ---------------------------------------------------------------------------
# Retire-only mode — recovery path for records whose episode already exists
# ---------------------------------------------------------------------------

def run_retire_only(*, dry_run: bool) -> int:
    """Retire-only mode: soft-retire conservative candidates that are NOT yet retired.

    Use this after a --apply --live run where episodes were created but soft-retire
    failed (the 46-record recovery case).  Does NOT create episodes; calls
    _soft_retire only.

    --dry-run: zero writes; prints count + 10-record sample.
    --live   : performs the PATCH calls (OPERATOR-GATED).

    Idempotent: records already retired (retrievable==False) are silently skipped.
    """
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"=== ship-log-reclassify --retire-only ({mode}) ===\n")

    if not _REPORT_JSON_PATH.exists():
        print(
            f"ERROR: report JSON not found at {_REPORT_JSON_PATH}\n"
            "Run --report first to generate the candidate set.",
            file=sys.stderr,
        )
        return 1

    all_candidates: list[dict] = json.loads(_REPORT_JSON_PATH.read_text(encoding="utf-8"))
    # D1: conservative set only
    candidates = [c for c in all_candidates if c.get("conservative")]
    print(f"  Report JSON: {len(all_candidates)} total candidates")
    print(f"  Conservative subset (D1, >800 chars): {len(candidates)}")

    # Determine which records still need retiring (Qdrant read-only check)
    print("\n  Checking retirement state (read-only Qdrant query)...", flush=True)
    to_retire: list[dict] = []
    already_retired = 0
    check_failed = 0
    try:
        with httpx.Client() as qclient:
            for c in candidates:
                try:
                    r = qclient.post(
                        f"{QDRANT}/collections/{COLLECTION}/points",
                        json={"ids": [c["id"]], "with_payload": True, "with_vector": False},
                        timeout=10.0,
                    )
                    r.raise_for_status()
                    pts = r.json().get("result", [])
                    if pts and (pts[0].get("payload") or {}).get("retrievable") is False:
                        already_retired += 1
                    else:
                        to_retire.append(c)
                except Exception as e:
                    print(f"  CHECK-FAIL {c['id'][:8]}: {e}", file=sys.stderr)
                    check_failed += 1
                    to_retire.append(c)  # conservative: include on check failure
    except Exception as e:
        print(f"  WARNING: Qdrant unreachable ({e}); assuming all need retiring.")
        to_retire = candidates

    print(f"\n  Would retire : {len(to_retire)}")
    print(f"  Already retired (skipped): {already_retired}")
    if check_failed:
        print(f"  Check-failed (included conservatively): {check_failed}")

    sample = to_retire[:10]
    if sample:
        print(f"\n  --- SAMPLE (up to 10 of {len(to_retire)} to-retire) ---")
        print(f"  {'id[:8]':<10} {'len':>6}  {'brand':<16}  first 90 chars")
        print(f"  {'-'*8:<10} {'-'*6:>6}  {'-'*16:<16}  {'-'*30}")
        for c in sample:
            brand = (c.get("brand") or "")[:16]
            snippet = c["text"].replace("\n", " ")[:90]
            print(f"  {c['id'][:8]:<10} {c['len']:>6}  {brand:<16}  {snippet!r}")
            if dry_run:
                print(f"    → would PATCH retrievable=False (actor=backfill-apply-v013)")

    if dry_run:
        print(f"\nDRY-RUN complete. {len(to_retire)} would be retired. No data modified.")
        return 0

    # ---- LIVE retire ----
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"\n  run_ts={now_iso}")
    try:
        headers = _mem0_headers()
    except Exception as e:
        print(f"ERROR: cannot read API key: {e}", file=sys.stderr)
        return 1

    retired = 0
    skipped = 0
    errors = 0

    with httpx.Client() as client:
        for c in to_retire:
            rec_id = c["id"]
            # Re-check idempotency at retire time (Qdrant read, same as run_apply)
            if _is_already_processed(client, headers, rec_id):
                skipped += 1
                print(f"  SKIP {rec_id[:8]} (already retired)", flush=True)
                continue

            ok = _soft_retire(client, headers, rec_id, "", now_iso)
            if ok:
                retired += 1
                print(f"  RETIRED {rec_id[:8]}", flush=True)
            else:
                errors += 1

    print(
        f"\n=== retire-only done: retired={retired} skipped={skipped} errors={errors} ===",
        flush=True,
    )
    return 0 if errors == 0 else 1


# ---------------------------------------------------------------------------
# Report mode
# ---------------------------------------------------------------------------

def run_report() -> int:
    print("=== ship-log-reclassify --report (read-only) ===\n")

    print("Scanning ALL records via Qdrant scroll (bypasses mem0 500-record cap)...")
    try:
        with httpx.Client() as client:
            all_points = scroll_all_qdrant_points(client)
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        print(f"ERROR: Qdrant scroll failed: {e}", file=sys.stderr)
        return 1

    total_points = len(all_points)
    print(f"  Total points scrolled: {total_points}")

    # Filter to tier=evidence (Qdrant payload field: "tier", same as l10-audit.py)
    evidence_points = [p for p in all_points if (p.get("payload") or {}).get("tier") == "evidence"]
    total_evidence = len(evidence_points)
    print(f"  Total tier=evidence: {total_evidence}")

    # Exclude test artifacts
    excluded = [p for p in evidence_points if is_test_artifact(p)]
    workset = [p for p in evidence_points if not is_test_artifact(p)]
    print(f"  Excluded (test-artifact pattern): {len(excluded)}")
    print(f"  Workset for classification: {len(workset)}")

    # Classify
    # Per l10-audit.py: text lives in payload["data"] or payload["memory"].
    # brand and source are also top-level payload keys.
    candidates: list[dict] = []
    for point in workset:
        payload = point.get("payload") or {}
        text = payload.get("data", "") or payload.get("memory", "") or ""
        if not isinstance(text, str):
            text = str(text)
        if is_ship_log(text):
            candidates.append({
                "id": str(point["id"]),
                "len": len(text),
                "conservative": len(text) > _CONSERVATIVE_LEN,
                "brand": payload.get("brand", ""),
                "text": text,
                "source": payload.get("source", ""),
            })

    # Sort by length descending
    candidates.sort(key=lambda x: x["len"], reverse=True)

    conservative_set = [c for c in candidates if c["conservative"]]
    full_predicate = len(candidates)
    conservative_count = len(conservative_set)

    print(f"\n--- COUNTS ---")
    print(f"  Total points scrolled  :  {total_points}")
    print(f"  Total tier=evidence    :  {total_evidence}")
    print(f"  Excluded (test debris) :  {len(excluded)}")
    print(f"  Full-predicate candidates (is_ship_log=True):  {full_predicate}")
    print(f"  Conservative subset (>800 chars):              {conservative_count}")
    print(f"\n  Operator decision D1: conservative ~{conservative_count} (>800 chars)"
          f" vs aggressive ~{full_predicate} (all is_ship_log)")

    print(f"\n--- CANDIDATE TABLE (sorted by length desc) ---")
    print(f"  {'id[:8]':<10} {'len':>6}  {'>800?':<6}  {'brand':<16}  first 90 chars")
    print(f"  {'-'*8:<10} {'-'*6:>6}  {'-'*5:<6}  {'-'*16:<16}  {'-'*30}")
    for c in candidates:
        short_id = c["id"][:8]
        conservative_flag = "YES" if c["conservative"] else "no"
        brand = (c["brand"] or "")[:16]
        snippet = c["text"].replace('\n', ' ')[:90]
        print(f"  {short_id:<10} {c['len']:>6}  {conservative_flag:<6}  {brand:<16}  {snippet!r}")

    # Write JSON artifact for --apply
    _REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_REPORT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)
    print(f"\nJSON artifact written: {_REPORT_JSON_PATH}")
    print(f"  ({len(candidates)} candidate records; --apply --dry-run/--live consumes the approved set)")
    print("\nZERO mem0 writes performed.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--report",
        action="store_true",
        default=False,
        help="Scan and emit candidate diff (default action)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply soft-retirements (requires --dry-run or --live; bare --apply is an error)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="With --apply: print what would happen, zero writes",
    )
    ap.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="With --apply or --retire-only: perform live mutations (OPERATOR-GATED)",
    )
    ap.add_argument(
        "--retire-only",
        action="store_true",
        default=False,
        dest="retire_only",
        help=(
            "Recovery mode: soft-retire conservative candidates that are NOT yet retired, "
            "WITHOUT creating episodes.  Use after a --apply run where episodes were created "
            "but soft-retire 403'd.  Requires --dry-run or --live."
        ),
    )
    args = ap.parse_args()

    if args.retire_only:
        if not args.dry_run and not args.live:
            print(
                "ERROR: --retire-only requires either --dry-run or --live.\n"
                "  --dry-run : safe preview, zero writes\n"
                "  --live    : OPERATOR-GATED live mutation\n"
                "Bare --retire-only is intentionally blocked to prevent accidental mutation.",
                file=sys.stderr,
            )
            return 2
        if args.dry_run and args.live:
            print(
                "error: pass exactly one of --dry-run / --live",
                file=sys.stderr,
            )
            return 2
        return run_retire_only(dry_run=args.dry_run)

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
            print(
                "error: pass exactly one of --dry-run / --live",
                file=sys.stderr,
            )
            return 2
        return run_apply(dry_run=args.dry_run)

    # --report is the default action
    return run_report()


if __name__ == "__main__":
    sys.exit(main())
