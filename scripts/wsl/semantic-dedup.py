#!/usr/bin/env python3
"""Semantic dedup with TIER-SENSITIVE cosine thresholds.

Lens N3 (neuro): real hippocampus pattern-separates distinct contextual
variations before pattern-completing for retrieval. Uniform 0.92 threshold
across tiers is biologically aggressive -- kills useful variation. Scale
threshold by trust: high-trust tiers require more semantic identity before merging.

  - canonical: 0.97  (almost identical; safer)
  - stable:    0.95  (still cautious)
  - evidence:  0.92  (default; can afford more dedup)
  - temporal:  0.92  (decay scanner deletes these by expiry; dedup is fallback)

For each pair (A, B) above the tier threshold AND same tier, keep the older
(established truth), demote-and-delete the newer. Skips tier=canonical entirely
when newer-of-pair (those are user-locked; never auto-merge).

v0.14 C: pairs must also share the same (user_id, workspace, project) partition key
before cosine comparison. Prevents cross-brand/cross-workspace dedup collisions.
Legacy records with no workspace/project fields get partition key (user_id, None, None)
and can still dedup against each other (no regression vs pre-v0.14 behaviour).

Every delete is appended to BOTH the dedup-report.jsonl AND the central
tier-ledger as event=decay-delete with full payload preserved for restore.

v0.13.1: preflight probe of Qdrant + mem0 health. Emits dedup-scan-skip ledger
event and exits 0 cleanly if either backend is unreachable. Mid-run httpx failures
emit dedup-scan-abort with partial counts. Acquires exclusive fcntl lock on
~/.mem0/dedup.lock so dream-consolidate.ps1 can detect a running dedup and skip
its consolidation phase (prevents insights with source_memory_ids that dedup is
about to delete)."""
from __future__ import annotations
import fcntl, json, os, sys
import datetime as dt
from pathlib import Path
import httpx

QDRANT = "http://127.0.0.1:6333"
COLLECTION = "memories"
MEM0 = "http://127.0.0.1:18791"
KEY = (Path.home() / ".mem0" / "api-key").read_text().strip()
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
TIER_THRESHOLDS = {"canonical": 0.97, "stable": 0.95, "evidence": 0.94, "temporal": 0.94, "insight": 0.95}
# 2026-06-10: evidence/temporal bumped 0.92 -> 0.94 per the operator's direction.
# Rationale: port directory entries (P:\Port Directory\) and similar IP/port/SHA-change facts
# read as semantically near-identical (~0.92-0.93 cosine) but are factually distinct. Earlier
# 0.92 threshold deleted 27 atomic facts on the v0.13 inaugural run, some of which may have
# been such distinctions. Tighter 0.94 trades dedup compression for variation preservation.
REPORT = Path.home() / ".mem0" / "dedup-report.jsonl"
LEDGER = Path.home() / ".mem0" / "tier-ledger.jsonl"
DEDUP_LOCK = Path.home() / ".mem0" / "dedup.lock"

def _partition_key(payload):
    """v0.14 C: dedup pairs must share (user_id, workspace, project). Prevents cross-brand merges.
    Legacy records without workspace/project fields yield (user_id, None, None) — they still
    dedup against each other, preserving pre-v0.14 behaviour for existing data."""
    return (
        payload.get("user_id"),
        payload.get("workspace") or payload.get("legacy_workspace"),
        payload.get("project") or payload.get("legacy_project"),
    )

def _append_ledger(rec):
    rec.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
    rec.setdefault("schema_version", "v17")  # v0.17 F.4.4: every entry stamps schema version
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

def _acquire_dedup_lock() -> int | None:
    """Acquire exclusive flock on DEDUP_LOCK. Returns fd or None."""
    DEDUP_LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(DEDUP_LOCK), os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, f"semantic-dedup pid={os.getpid()} {dt.datetime.now(dt.timezone.utc).isoformat()}\n".encode())
        return fd
    except (BlockingIOError, OSError):
        os.close(fd)
        return None

def _release_dedup_lock(fd: int):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        try: DEDUP_LOCK.unlink(missing_ok=True)
        except: pass
    except: pass

def scroll_all_with_vectors():
    points, off = [], None
    while True:
        body = {"limit": 256, "with_payload": True, "with_vector": True}
        if off is not None: body["offset"] = off
        r = httpx.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll", json=body, timeout=30.0)
        r.raise_for_status()
        res = r.json()["result"]
        points.extend(res.get("points", []))
        off = res.get("next_page_offset")
        if not off: break
    return points

def cosine(a, b):
    import math
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb + 1e-9)

def main():
    lock_fd = _acquire_dedup_lock()
    if lock_fd is None:
        print("semantic-dedup: another instance holds the lock; aborting", flush=True)
        return 0
    try:
        return _run()
    finally:
        _release_dedup_lock(lock_fd)

def _run():
    deletions = 0
    # Preflight: confirm both backends are reachable
    try:
        with httpx.Client(timeout=5.0) as probe:
            probe.get(f"{QDRANT}/readyz").raise_for_status()
            probe.get(f"{MEM0}/health").raise_for_status()
    except (httpx.HTTPError, httpx.ConnectError, OSError) as e:
        _append_ledger({"event": "dedup-scan-skip", "actor": "semantic-dedup", "reason": f"backend unreachable: {type(e).__name__}: {str(e)[:120]}"})
        print(f"semantic-dedup: SKIP - backend unreachable ({e})", flush=True)
        return 0
    try:
        pts = scroll_all_with_vectors()
        print(f"loaded {len(pts)} points")
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.unlink(missing_ok=True)
        keep = {str(p["id"]): True for p in pts}
        with httpx.Client(headers=H, timeout=15.0) as c, REPORT.open("a", encoding="utf-8") as report:
            for i, a in enumerate(pts):
                if not keep.get(str(a["id"])): continue
                pa = a.get("payload") or {}
                ta = pa.get("tier", "evidence")
                if ta == "canonical": continue   # never iterate canonical as primary
                threshold = TIER_THRESHOLDS.get(ta, 0.92)
                va = a.get("vector")
                if not isinstance(va, list): continue
                for b in pts[i+1:]:
                    if not keep.get(str(b["id"])): continue
                    pb = b.get("payload") or {}
                    if pb.get("tier") != ta: continue
                    # v0.14 C: partition guard — only dedup within same (user_id, workspace, project)
                    if _partition_key(pa) != _partition_key(pb): continue
                    vb = b.get("vector")
                    if not isinstance(vb, list): continue
                    sim = cosine(va, vb)
                    if sim < threshold: continue
                    ca = pa.get("created_at", "")
                    cb = pb.get("created_at", "")
                    older, newer = (a, b) if ca <= cb else (b, a)
                    p_older = older.get("payload") or {}
                    p_newer = newer.get("payload") or {}
                    if p_older.get("tier") == "canonical":  # never delete canonical
                        newer, older = older, newer
                        p_older, p_newer = p_newer, p_older
                    rid = str(newer["id"])
                    # Preserve FULL payload of the deletion so restore is possible (lens S3)
                    full_payload = dict(p_newer)
                    report_rec = {
                        "deleted_id": rid, "kept_id": str(older["id"]),
                        "cosine": round(sim, 4), "tier": ta, "threshold": threshold,
                        "deleted_full_payload": full_payload,
                        "kept_text": (p_older.get("data") or "")[:120],
                    }
                    report.write(json.dumps(report_rec) + "\n")
                    r = c.delete(f"{MEM0}/v1/memories/{rid}")
                    if r.status_code == 200:
                        keep[rid] = False
                        deletions += 1
                        # Lens S2: every destructive op appends to the central tier-ledger
                        _append_ledger({
                            "event": "decay-delete", "memory_id": rid,
                            "reason": f"semantic-dedup cosine={round(sim,4)} >= threshold={threshold} (tier={ta})",
                            "kept_id": str(older["id"]),
                            "actor": "semantic-dedup",
                        })
    except (httpx.HTTPError, OSError) as e:
        _append_ledger({"event": "dedup-scan-abort", "actor": "semantic-dedup", "reason": f"mid-run failure: {type(e).__name__}: {str(e)[:120]}", "partial_deletions": deletions})
        print(f"semantic-dedup: ABORT mid-run after deletions={deletions} ({e})", flush=True)
        return 1
    print(f"semantic-dedup: deletions={deletions}, tier_thresholds={TIER_THRESHOLDS}, report={REPORT}")

if __name__ == "__main__":
    sys.exit(main())
