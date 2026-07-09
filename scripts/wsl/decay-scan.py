#!/usr/bin/env python3
"""Weekly decay scanner.
1. Find tier=temporal records whose metadata.expires_at < now -> soft-delete (DELETE).
2. Find tier=evidence records older than 90 days with no incoming `replaces` references
   and that have NEVER been touched by C1/dream -- flag as 'decay-candidate' (manual review).
3. Print summary; no other side effects on tier=stable/canonical/insight.

EVERY destructive op appends to the central tier-ledger (lens S2).

v0.13.1: preflight probe of Qdrant + mem0 health. Emits decay-scan-skip ledger event
and exits 0 cleanly if either backend is unreachable. Mid-run httpx failures emit
decay-scan-abort with partial counts."""
from __future__ import annotations
import datetime as dt, json, os, sys
from pathlib import Path
import httpx

QDRANT = "http://127.0.0.1:6333"
COLLECTION = os.environ.get("MEM0_QDRANT_COLLECTION", "mem0_egemma_768")  # env-overridable; default is the live collection (was the dead pre-egemma 'memories' -> 404)
MEM0 = "http://127.0.0.1:18791"
KEY = (Path.home() / ".mem0" / "api-key").read_text().strip()
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
EVIDENCE_AGE_DAYS = 90
REPORT = Path.home() / ".mem0" / "decay-report.jsonl"
LEDGER_DIR = Path.home() / ".mem0"

def _ledger_path() -> Path:
    # MEM-16 (2026-07-03): append to the CURRENT-MONTH segment
    # (tier-ledger-YYYY-MM.jsonl), same naming as app.py _append_ledger — the
    # legacy tier-ledger.jsonl is a frozen historical archive. ALL writers moved
    # in the same change so ledger-audit.py's chronological walk (legacy first,
    # then segments) stays monotonic across the cutover.
    return LEDGER_DIR / f"tier-ledger-{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m')}.jsonl"

def _ledger(rec):
    rec.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
    rec.setdefault("schema_version", "v17")  # v0.17 F.4.4: every entry stamps schema version
    ledger = _ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

def scroll_all():
    points, off = [], None
    while True:
        body = {"limit": 256, "with_payload": True, "with_vector": False}
        if off is not None: body["offset"] = off
        r = httpx.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll", json=body, timeout=15.0)
        r.raise_for_status()
        res = r.json()["result"]
        points.extend(res.get("points", []))
        off = res.get("next_page_offset")
        if not off: break
    return points

def main():
    now = dt.datetime.now(dt.timezone.utc)
    deleted, flagged = 0, 0
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.unlink(missing_ok=True)
    # Preflight: confirm both backends are reachable
    try:
        with httpx.Client(timeout=5.0) as probe:
            probe.get(f"{QDRANT}/readyz").raise_for_status()
            probe.get(f"{MEM0}/health").raise_for_status()
    except (httpx.HTTPError, httpx.ConnectError, OSError) as e:
        _ledger({"event": "decay-scan-skip", "actor": "decay-scan", "reason": f"backend unreachable: {type(e).__name__}: {str(e)[:120]}"})
        print(f"decay-scan: SKIP - backend unreachable ({e})", flush=True)
        return 0
    try:
        with httpx.Client(headers=H, timeout=15.0) as c, REPORT.open("a", encoding="utf-8") as report:
            for p in scroll_all():
                payload = p.get("payload") or {}
                tier = payload.get("tier", "evidence")
                mid = str(p.get("id"))
                if tier == "temporal":
                    exp = payload.get("expires_at")
                    if exp:
                        try:
                            exp_dt = dt.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                            if exp_dt < now:
                                r = c.delete(f"{MEM0}/v1/memories/{mid}")
                                if r.status_code == 200:
                                    deleted += 1
                                    # Preserve full payload in report for restore (lens S3)
                                    report.write(json.dumps({"id": mid, "action": "deleted-expired", "tier": tier, "full_payload": payload}) + "\n")
                                    _ledger({"event": "decay-delete", "memory_id": mid, "reason": f"temporal expired at {exp}", "actor": "decay-scan"})
                        except ValueError:
                            pass
                elif tier == "evidence":
                    created = payload.get("created_at")
                    if created:
                        try:
                            c_dt = dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                            age = (now - c_dt).days
                            if age > EVIDENCE_AGE_DAYS and not payload.get("replaces") and not payload.get("touched_by_dream"):
                                report.write(json.dumps({"id": mid, "action": "flagged-decay-candidate", "age_days": age, "preview": (payload.get("data") or "")[:80]}) + "\n")
                                flagged += 1
                        except ValueError:
                            pass
    except (httpx.HTTPError, OSError) as e:
        _ledger({"event": "decay-scan-abort", "actor": "decay-scan", "reason": f"mid-run failure: {type(e).__name__}: {str(e)[:120]}", "partial_deleted": deleted, "partial_flagged": flagged})
        print(f"decay-scan: ABORT mid-run after deleted={deleted}, flagged={flagged} ({e})", flush=True)
        return 1
    print(f"decay-scan: deleted_temporal={deleted}, flagged_evidence_candidates={flagged}, report={REPORT}")

if __name__ == "__main__":
    sys.exit(main())
