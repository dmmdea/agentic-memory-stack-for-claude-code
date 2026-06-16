#!/usr/bin/env python3
"""v0.17 F.4.2: one-time idempotent backfill — stamp `retired_at` on all
Qdrant points that have `retrievable=false` but lack a `retired_at` timestamp.

Idempotent: re-running this script is safe; it skips points that already have
`retired_at` set (checks for non-None, non-empty string).

H8 fix (v0.17 Final): the previous fallback to direct Qdrant set_payload when the
mem0 endpoint returned 403 has been REMOVED. It bypassed the Phase A canonical gate
and skipped the ledger entry — both unacceptable. Instead, this script now uses
actor='stamp-retired-v013' which is on the TRUSTED_PATCH_ACTORS allowlist in
security_invariants.py and has permission to PATCH retired_at on canonical/insight
records without a full HMAC user-direct token. If the PATCH returns non-200, the
record is logged as FAILED and must be investigated.

Future retired records will gain `retired_at` at the time the `retrievable=false`
flag is written (documented in tier-policy.md § Retired-Record Purge Plan).

Usage:
    python stamp-retired-at.py            # live run
    python stamp-retired-at.py --dry-run  # print without writing
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import httpx

QDRANT = "http://127.0.0.1:6333"
COLLECTION = "memories"
MEM0 = "http://127.0.0.1:18791"
KEY = (Path.home() / ".mem0" / "api-key").read_text().strip()
H = {"X-API-Key": KEY, "Content-Type": "application/json"}

DRY_RUN = "--dry-run" in sys.argv

def scroll_all():
    """Scroll all Qdrant points (with_payload=True, with_vector=False)."""
    points, offset = [], None
    while True:
        body = {"limit": 256, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        r = httpx.post(
            f"{QDRANT}/collections/{COLLECTION}/points/scroll",
            json=body, timeout=30.0,
        )
        r.raise_for_status()
        res = r.json().get("result", {})
        points.extend(res.get("points", []))
        offset = res.get("next_page_offset")
        if not offset:
            break
    return points


def main():
    run_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"stamp-retired-at: run_ts={run_ts}  dry_run={DRY_RUN}", flush=True)

    # Preflight
    try:
        httpx.get(f"{QDRANT}/readyz", timeout=5).raise_for_status()
    except Exception as e:
        print(f"stamp-retired-at: ABORT — Qdrant unreachable: {e}", flush=True)
        return 1

    points = scroll_all()
    print(f"stamp-retired-at: total Qdrant points scanned: {len(points)}", flush=True)

    to_stamp = []
    already_stamped = 0
    for pt in points:
        payload = pt.get("payload") or {}
        if payload.get("retrievable") is not False:
            continue  # live record — skip
        existing = payload.get("retired_at")
        if existing:  # already stamped
            already_stamped += 1
            continue
        to_stamp.append(pt["id"])

    print(
        f"stamp-retired-at: {len(to_stamp)} to stamp, {already_stamped} already stamped, "
        f"{len(points) - len(to_stamp) - already_stamped} live (skipped)",
        flush=True,
    )

    if not to_stamp:
        print("stamp-retired-at: nothing to do.", flush=True)
        return 0

    if DRY_RUN:
        print("stamp-retired-at: [DRY-RUN] would stamp:", flush=True)
        for mid in to_stamp[:20]:
            print(f"  {mid}", flush=True)
        if len(to_stamp) > 20:
            print(f"  ... and {len(to_stamp) - 20} more", flush=True)
        return 0

    # Live: PATCH metadata via mem0 API ONLY (H8 fix: direct Qdrant fallback removed).
    # actor='stamp-retired-v013' is on TRUSTED_PATCH_ACTORS in security_invariants.py and
    # is allowed to set retired_at on canonical/insight records without an HMAC token.
    # If the PATCH returns non-200, the record is logged as FAILED for manual investigation.
    # DO NOT add a direct Qdrant fallback here -- it bypasses the Phase A gate + ledger.
    stamped = 0
    failed = 0
    for mid in to_stamp:
        try:
            r = httpx.patch(
                f"{MEM0}/v1/memories/{mid}/metadata",
                json={
                    "metadata": {"retired_at": run_ts},
                    "actor": "stamp-retired-v013",
                    "reason": "v0.17 F.4.2 one-time retired_at backfill (H8: trusted-actor path)",
                },
                headers=H, timeout=10.0,
            )
            if r.status_code == 200:
                stamped += 1
            else:
                failed += 1
                print(
                    f"stamp-retired-at: FAIL {mid}: mem0={r.status_code} body={r.text[:200]}. "
                    "Do NOT fall back to direct Qdrant (H8). Investigate manually.",
                    flush=True,
                )
        except Exception as e:
            failed += 1
            print(f"stamp-retired-at: EXCEPTION {mid}: {e}", flush=True)

    print(
        f"stamp-retired-at: done. stamped={stamped} failed={failed} already_had={already_stamped}",
        flush=True,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
