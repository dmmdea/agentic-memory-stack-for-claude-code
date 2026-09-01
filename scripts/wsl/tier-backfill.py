#!/usr/bin/env python3
"""tier-backfill.py -- one-time backfill: stamp tier='evidence' on tier-less records.

WHY THIS EXISTS (2026-09-01): POST /v1/memories validated metadata.tier when
present but never defaulted it when absent -- its own 403 guidance even said
"(or omit tier)". A record born without a tier is a trap: fetch_current_tier
fail-closes an absent tier to "canonical" (the H1-race shield), so every
mutation of that record (PUT / DELETE / PATCH) demands the user-direct HMAC and
403s ordinary callers. An agent could create a memory it could never correct or
remove. 127 such points existed live when this shipped. The birth-default in
app.py stops new ones; this script unlocks the existing stock.

SAFETY -- why not stamp blindly: the canonical fallback exists to protect
records whose tier was STRIPPED by a transient race after a legitimate
canonical promotion. Stamping those 'evidence' would silently demote canonical
ground truth. So for every candidate this script scans the tier ledgers
(~/.mem0/tier-ledger-*.jsonl) for any entry naming the id with a canonical
target; matches are SKIPPED and reported for the operator -- never stamped.

RECEIPTS: every live write appends one JSONL line {ts, script, record_id,
action, before, after} to ~/.mem0/tier-backfill-receipts.jsonl (the cp437-repair
discipline). Idempotent: stamped records no longer match the is_empty filter.

Usage:
  tier-backfill.py               # report only (default): counts + sample + skips
  tier-backfill.py --apply       # stamp tier='evidence' via Qdrant set_payload
Exit codes: 0 success (report or apply); 1 store unreachable / write failure.
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys

import httpx

QDRANT = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.environ.get("MEM0_COLLECTION", "mem0_egemma_768")
MEM0_HOME = os.path.expanduser(os.environ.get("MEM0_HOME", "~/.mem0"))
RECEIPTS = os.path.join(MEM0_HOME, "tier-backfill-receipts.jsonl")
BATCH = 100


def scroll_tierless(client: httpx.Client):
    """Yield (id, payload-subset) for every point with no tier key."""
    offset = None
    while True:
        body = {
            "filter": {"must": [{"is_empty": {"key": "tier"}}]},
            "limit": BATCH,
            "with_payload": ["data", "user_id", "source", "created_at"],
        }
        if offset is not None:
            body["offset"] = offset
        r = client.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll",
                        json=body, timeout=30.0)
        r.raise_for_status()
        res = r.json()["result"]
        for p in res["points"]:
            yield p["id"], (p.get("payload") or {})
        offset = res.get("next_page_offset")
        if offset is None:
            return


def canonical_ledger_ids() -> set:
    """Ids that any tier ledger ever promoted toward canonical (or insight).

    Field names have drifted across ledger generations, so match broadly: any
    entry whose JSON mentions the id AND carries a canonical/insight tier value
    in a tier-ish field counts. Broad matching over-skips (safe direction).
    """
    flagged = set()
    for path in sorted(glob.glob(os.path.join(MEM0_HOME, "tier-ledger-*.jsonl"))):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or ('"canonical"' not in line and '"insight"' not in line):
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    mid = e.get("memory_id") or e.get("mid") or e.get("id")
                    if mid:
                        flagged.add(str(mid))
        except OSError:
            continue
    return flagged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="stamp tier='evidence' on unflagged tier-less points (default: report only)")
    args = ap.parse_args()

    client = httpx.Client()
    try:
        client.get(f"{QDRANT}/readyz", timeout=5.0).raise_for_status()
    except Exception as e:
        print(f"FATAL: Qdrant unreachable at {QDRANT}: {e}", file=sys.stderr)
        return 1

    flagged = canonical_ledger_ids()
    stamped = skipped = 0
    candidates = list(scroll_tierless(client))
    print(f"tier-less points: {len(candidates)} (ledger ids with canonical/insight history: {len(flagged)})")

    for mid, payload in candidates:
        preview = str(payload.get("data") or "")[:70].replace("\n", " ")
        if str(mid) in flagged:
            skipped += 1
            print(f"SKIP (ledger names it with canonical/insight history -- operator review): {mid} :: {preview}")
            continue
        if not args.apply:
            print(f"would stamp evidence: {mid} :: {preview}")
            continue
        r = client.post(
            f"{QDRANT}/collections/{COLLECTION}/points/payload",
            json={"payload": {"tier": "evidence"}, "points": [mid]},
            timeout=15.0,
        )
        if r.status_code != 200:
            print(f"FATAL: set_payload failed for {mid}: {r.status_code} {r.text[:120]}", file=sys.stderr)
            return 1
        with open(RECEIPTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "script": "tier-backfill.py",
                "record_id": str(mid),
                "action": "stamp-tier",
                "before": {"tier": None},
                "after": {"tier": "evidence"},
            }) + "\n")
        stamped += 1

    mode = "APPLIED" if args.apply else "REPORT (no writes; re-run with --apply)"
    print(f"{mode}: {stamped} stamped, {skipped} skipped-for-review, "
          f"{len(candidates) - stamped - skipped} listed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
