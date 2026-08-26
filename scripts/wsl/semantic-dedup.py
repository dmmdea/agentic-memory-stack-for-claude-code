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
import fcntl
import json
import os
import sys
import datetime as dt
from pathlib import Path
import httpx

QDRANT = "http://127.0.0.1:6333"
COLLECTION = os.environ.get("MEM0_QDRANT_COLLECTION", "mem0_egemma_768")  # env-overridable; default is the live collection (was the dead pre-egemma 'memories' -> 404)
MEM0 = "http://127.0.0.1:18791"
try:
    KEY = (Path.home() / ".mem0" / "api-key").read_text().strip()
except OSError:
    # Importable without the live key (unit tests exercise the pure helpers); a real run
    # still fails loudly at the first authenticated call.
    KEY = os.environ.get("MEM0_API_KEY", "")
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
TIER_THRESHOLDS = {"canonical": 0.97, "stable": 0.95, "evidence": 0.94, "temporal": 0.94, "insight": 0.95}
# 2026-06-10: evidence/temporal bumped 0.92 -> 0.94 per the operator's direction.
# Rationale: port directory entries (P:\Port Directory\) and similar IP/port/SHA-change facts
# read as semantically near-identical (~0.92-0.93 cosine) but are factually distinct. Earlier
# 0.92 threshold deleted 27 atomic facts on the v0.13 inaugural run, some of which may have
# been such distinctions. Tighter 0.94 trades dedup compression for variation preservation.
REPORT = Path.home() / ".mem0" / "dedup-report.jsonl"
# W6 (roast F6): --dry-run writes its would-delete report HERE — the real
# report is the ONLY holder of deleted_full_payload (the restore record) and
# a dry-run must never unlink it.
REPORT_DRY = Path.home() / ".mem0" / "dedup-report.dryrun.jsonl"
# W6 PR-D (roast F1c): the job's own outcome-coded receipt — per-adopter
# file, never the shared monthly ledger, never the unlink-rewritten report.
SUMMARY = Path.home() / ".mem0" / "dedup-summary.jsonl"
LEDGER_DIR = Path.home() / ".mem0"
DEDUP_LOCK = Path.home() / ".mem0" / "dedup.lock"

def _is_migration_protected(payload) -> bool:
    """True for a record migrated out of a workspace auto-memory store.

    2026-08-26: the auto-memory compactor moves a fact from a workspace index into mem0 and
    then DELETES the index line and the fact file — mem0 becomes the only live copy. Those
    records are always the NEWER side of any near-duplicate pair (the L1a extractor has often
    already captured the same fact from the session transcript), and this job deletes the
    newer side, so an unguarded run would evict a just-migrated fact the very next morning.
    The payload is preserved in the report/ledger, but the operator would never know to
    restore it. Marked by source='automemory:<workspace>/<file>.md'.
    """
    return str(payload.get("source") or "").startswith("automemory:")


def _partition_key(payload):
    """v0.14 C: dedup pairs must share (user_id, workspace, project). Prevents cross-brand merges.
    Legacy records without workspace/project fields yield (user_id, None, None) — they still
    dedup against each other, preserving pre-v0.14 behaviour for existing data."""
    return (
        payload.get("user_id"),
        payload.get("workspace") or payload.get("legacy_workspace"),
        payload.get("project") or payload.get("legacy_project"),
    )

def _ledger_path() -> Path:
    # MEM-16 (2026-07-03): append to the CURRENT-MONTH segment
    # (tier-ledger-YYYY-MM.jsonl), same naming as app.py _append_ledger — the
    # legacy tier-ledger.jsonl is a frozen historical archive.
    return LEDGER_DIR / f"tier-ledger-{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m')}.jsonl"

def _append_ledger(rec):
    rec.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
    rec.setdefault("schema_version", "v17")  # v0.17 F.4.4: every entry stamps schema version
    ledger = _ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

def _append_summary(outcome: str, deletions: int = 0, dry_run: bool = False) -> None:
    """W6 PR-D (roast F1e): a ts-bearing, outcome-coded line on EVERY exit
    path — preflight-skip, lock-held, mid-run abort, dry-run, success — into
    the job's OWN receipt file (never the shared monthly ledger; never the
    unlink-rewritten report). Carries jobs_key from JOBS_IDEMPOTENCY_KEY so
    the queue's observation predicate can attribute THIS run. Advisory:
    never crashes the dedup."""
    try:
        rec = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "outcome": outcome, "deletions": deletions, "dry_run": dry_run,
        }
        key = os.environ.get("JOBS_IDEMPOTENCY_KEY")
        if key:
            rec["jobs_key"] = key
        SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        with SUMMARY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        print(f"semantic-dedup: summary append failed (non-fatal): {e}", flush=True)


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
    import sys
    dry_run = "--dry-run" in sys.argv
    lock_fd = _acquire_dedup_lock()
    if lock_fd is None:
        print("semantic-dedup: another instance holds the lock; aborting", flush=True)
        _append_summary("no-op:lock-held", dry_run=dry_run)
        return 0
    try:
        return _run(dry_run)
    finally:
        _release_dedup_lock(lock_fd)

def _run(dry_run=False):
    deletions = 0
    # Preflight: confirm both backends are reachable
    try:
        with httpx.Client(timeout=5.0) as probe:
            probe.get(f"{QDRANT}/readyz").raise_for_status()
            probe.get(f"{MEM0}/health").raise_for_status()
    except (httpx.HTTPError, httpx.ConnectError, OSError) as e:
        _append_ledger({"event": "dedup-scan-skip", "actor": "semantic-dedup", "reason": f"backend unreachable: {type(e).__name__}: {str(e)[:120]}"})
        print(f"semantic-dedup: SKIP - backend unreachable ({e})", flush=True)
        _append_summary(f"no-op:backend-unreachable:{type(e).__name__}", dry_run=dry_run)
        return 0
    try:
        pts = scroll_all_with_vectors()
        print(f"loaded {len(pts)} points")
        # F6: dry runs write (and unlink) ONLY their own report file — the
        # real report is the restore record and only a real run replaces it.
        report_path = REPORT_DRY if dry_run else REPORT
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.unlink(missing_ok=True)
        keep = {str(p["id"]): True for p in pts}
        with httpx.Client(headers=H, timeout=15.0) as c, report_path.open("a", encoding="utf-8") as report:
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
                    elif _is_migration_protected(p_newer):
                        # An auto-memory migration is the only live copy of a fact whose index
                        # line is already gone. If the older side is an ordinary record, keep
                        # the migration instead; if BOTH sides are migrations, delete neither.
                        if _is_migration_protected(p_older):
                            continue
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
                    if dry_run:
                        keep[rid] = False; deletions += 1   # would-delete; no API delete, no ledger
                        continue
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
        _append_summary(f"degraded:aborted:{type(e).__name__}", deletions=deletions, dry_run=dry_run)
        return 1
    label = "DRY-RUN would_delete" if dry_run else "deletions"
    print(f"semantic-dedup: {label}={deletions}, tier_thresholds={TIER_THRESHOLDS}, report={report_path}")
    _append_summary("ok", deletions=deletions, dry_run=dry_run)
    return 0

if __name__ == "__main__":
    sys.exit(main())
