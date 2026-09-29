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

WP-4 (session-12 audit): the job compared NOTHING for weeks - every point's vector is a
named-vector dict ({"": dense, "bm25": sparse}) and the old loop skipped anything that was not a
bare list, then reported ok with deletions=0. It now extracts the dense vector through
ams_env.dense_vector(), scores each (tier, partition) group with blocked numpy matrix products,
and PROVES its work: every run's summary line carries scanned / skipped_no_vector /
compared_pairs / candidates / deleted, the step-outcome line reads degraded when it compared
nothing on a large corpus (or skipped more than 1 % for want of a vector), and the dedup-job
health capability reads those counts rather than the report file's mtime. --max-deletions
(default 50) bounds a run so the backlog drains over a few nights; the surest duplicates (highest
cosine) go first. Never deleted: canonical, an automemory-migrated record, an operator-sourced
insight. --dry-run writes EVERY candidate to dedup-report.dryrun.jsonl for review.

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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # deployed flat: ~/apps/mem0-scripts
import ams_env  # noqa: E402  (spec §4: URL from authority-url, key from the systemd credential)

QDRANT = "http://127.0.0.1:6333"
COLLECTION = os.environ.get("MEM0_QDRANT_COLLECTION", "mem0_egemma_768")  # env-overridable; default is the live collection (was the dead pre-egemma 'memories' -> 404)
MEM0 = ams_env.mem0_url()
try:
    KEY = ams_env.api_key() or os.environ.get("MEM0_API_KEY", "")
    if not KEY:
        raise OSError("api key unresolved (MEM0_API_KEY_FILE / ~/.mem0/api-key)")
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

def _append_summary(outcome: str, deletions: int = 0, dry_run: bool = False, counts=None) -> None:
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
        # WP-4: the work counts (scanned / skipped_no_vector / compared_pairs / candidates /
        # deleted ...) - the dedup-job health gate reads THESE, not the report file's mtime.
        if counts:
            rec.update(counts)
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
    """Every point with its payload and DENSE vector as a float32 array.

    Memory: the raw JSON vectors are converted page by page, so the resident set is one numpy
    row per point (about 3 KB) instead of 768 python floats. A point whose vector has no dense
    entry keeps vector=None and is counted by plan_dedup, never silently dropped."""
    import numpy as np
    points, off = [], None
    while True:
        body = {"limit": 256, "with_payload": True, "with_vector": True}
        if off is not None: body["offset"] = off
        r = httpx.post(f"{QDRANT}/collections/{COLLECTION}/points/scroll", json=body, timeout=30.0)
        r.raise_for_status()
        res = r.json()["result"]
        for p in res.get("points", []):
            dense = ams_env.dense_vector(p)
            p["vector"] = np.asarray(dense, dtype=np.float32) if dense is not None else None
            points.append(p)
        off = res.get("next_page_offset")
        if not off: break
    return points


# Rows of the similarity matrix computed per block: a 1024 x 13.7k float32 block is ~56 MB, where
# the full square of the largest partition would be ~750 MB.
BLOCK_ROWS = 1024
DEFAULT_MAX_DELETIONS = 50   # per run: the backlog drains over a few nights instead of in one
DEGRADE_MIN_SCANNED = 1000   # below this, "compared nothing" is a small corpus, not a defect
DEGRADE_SKIP_FRACTION = 0.01


def _is_operator_insight(payload) -> bool:
    """An insight the operator wrote or promoted, as opposed to the consolidator's own output.

    Insights can only be added through the consolidator's source; any other insight arrived by
    the operator's hand (or a user-direct promotion) and is never auto-merged. The consolidator's
    own near-duplicate insights are exactly what this job exists to drain, so they stay eligible."""
    if payload.get("tier") != "insight":
        return False
    return "c1-consolidator" not in str(payload.get("source") or "")


def _is_protected(payload) -> bool:
    """Records the job may never delete: canonical (user-locked), a record migrated out of an
    auto-memory store (the only live copy) and an operator-sourced insight."""
    return (payload.get("tier") == "canonical" or _is_migration_protected(payload)
            or _is_operator_insight(payload))


def decide_pair(p_older, p_newer) -> str:
    """Which side of a near-duplicate pair goes: 'delete-newer' (the normal case), 'delete-older'
    (the newer side is protected and the older one is not) or 'skip' (nothing may be deleted).

    The deletion normally targets `newer`, so protecting a record means either skipping the pair
    or moving the deletion onto `older`. A canonical newer side is always a skip: a user-locked
    record is never the one to win a trade. Canonical on the OLDER side needs no action: the newer
    record is the ordinary one and is the one that should go. Dead for canonical today (the
    same-tier filter keeps canonical out of every pair), kept correct for the day that changes."""
    if p_newer.get("tier") == "canonical":
        return "skip"
    if _is_protected(p_newer):
        return "skip" if _is_protected(p_older) else "delete-older"
    return "delete-newer"


def _candidate_pairs(pts):
    """All same-(tier, partition) pairs at or above their tier threshold, plus the work counts.

    Vectorised per group: rows are L2-normalised float32, and each block of rows is multiplied
    against the rows from its own start onward, so every unordered pair is scored exactly once.
    Returns ([(i, j, cosine)], stats) with i < j indexing `pts`."""
    import numpy as np
    stats = {"scanned": len(pts), "skipped_no_vector": 0, "compared_pairs": 0}
    dims: dict = {}
    dense: list = []
    for p in pts:
        v = p.get("vector") if isinstance(p.get("vector"), np.ndarray) else ams_env.dense_vector(p)
        dense.append(v)
        if v is not None:
            dims[len(v)] = dims.get(len(v), 0) + 1
    want = max(dims, key=dims.get) if dims else 0
    groups: dict = {}
    for idx, p in enumerate(pts):
        v = dense[idx]
        if v is None or len(v) != want:
            stats["skipped_no_vector"] += 1
            continue
        pl = p.get("payload") or {}
        tier = pl.get("tier")
        # canonical never enters a comparison, and a record with no tier never matched anything
        # in the old pairwise loop either (its None tier was compared against the other side)
        if tier is None or tier == "canonical":
            continue
        groups.setdefault((tier, _partition_key(pl)), []).append(idx)
    pairs = []
    for (tier, _), members in groups.items():
        n = len(members)
        if n < 2:
            continue
        threshold = TIER_THRESHOLDS.get(tier, 0.92)
        mat = np.asarray([dense[i] for i in members], dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        mat = mat / np.maximum(norms, 1e-9)
        stats["compared_pairs"] += n * (n - 1) // 2
        for start in range(0, n, BLOCK_ROWS):
            stop = min(start + BLOCK_ROWS, n)
            sims = mat[start:stop] @ mat[start:].T          # rows start..stop against start..n
            rows, cols = np.nonzero(sims >= threshold)
            for r, c in zip(rows.tolist(), cols.tolist()):
                i, j = start + r, start + c
                if j > i:
                    pairs.append((members[i], members[j], float(sims[r, c])))
    return pairs, stats


def plan_dedup(pts, max_deletions=DEFAULT_MAX_DELETIONS):
    """Decide what the job would delete. PURE: no HTTP, no files.

    Returns (decisions, stats). Each decision carries the pair, the cosine, the deleted record's
    payload (the restore record) and `within_cap`. Pairs are taken in descending cosine order, so
    the surest duplicates go first when the cap bites, and a record is decided once. max_deletions
    <= 0 means no cap. `candidates` is the full backlog; `deleted` is what this run acts on."""
    pairs, stats = _candidate_pairs(pts)
    pairs.sort(key=lambda t: (-t[2], str(pts[t[0]]["id"]), str(pts[t[1]]["id"])))
    gone: set = set()
    decisions = []
    protected_skips = 0
    for i, j, sim in pairs:
        a, b = pts[i], pts[j]
        if str(a["id"]) in gone or str(b["id"]) in gone:
            continue
        pa, pb = a.get("payload") or {}, b.get("payload") or {}
        older, newer = (a, b) if pa.get("created_at", "") <= pb.get("created_at", "") else (b, a)
        p_older, p_newer = older.get("payload") or {}, newer.get("payload") or {}
        verdict = decide_pair(p_older, p_newer)
        if verdict == "skip":
            protected_skips += 1
            continue
        if verdict == "delete-older":
            newer, older = older, newer
            p_newer, p_older = p_older, p_newer
        gone.add(str(newer["id"]))
        decisions.append({
            "deleted_id": str(newer["id"]), "kept_id": str(older["id"]),
            "cosine": round(sim, 4), "tier": pa.get("tier"),
            "threshold": TIER_THRESHOLDS.get(pa.get("tier"), 0.92),
            "deleted_full_payload": dict(p_newer),
            "kept_text": (p_older.get("data") or "")[:120],
            "within_cap": max_deletions <= 0 or len(decisions) < max_deletions,
        })
    stats.update({
        "candidates": len(decisions),
        "deleted": sum(1 for d in decisions if d["within_cap"]),
        "protected_skips": protected_skips,
        "max_deletions": max_deletions,
        "capped": any(not d["within_cap"] for d in decisions),
    })
    return decisions, stats


def run_outcome(stats) -> str:
    """'ok' or 'degraded:<reason>' from the run's own work counts. Exit-0-but-did-nothing is the
    failure this job had for weeks, so the counts decide, not the exit code."""
    scanned = stats.get("scanned", 0)
    if scanned > DEGRADE_MIN_SCANNED and stats.get("compared_pairs", 0) == 0:
        return "degraded:compared-0"
    if scanned and stats.get("skipped_no_vector", 0) / scanned > DEGRADE_SKIP_FRACTION:
        return "degraded:skipped-no-vector"
    return "ok"


def _write_outcome(status: str, counts: dict) -> None:
    """The step-outcome line ams-step.sh reads: '<status>[:<reason>] <compact json counts>'."""
    path = os.environ.get("AMS_OUTCOME_FILE")
    if not path:
        return
    try:
        Path(path).write_text(f"{status} {json.dumps(counts, separators=(',', ':'))}\n", encoding="utf-8")
    except OSError as e:
        print(f"semantic-dedup: outcome file write failed (non-fatal): {e}", flush=True)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="tier-sensitive semantic dedup")
    ap.add_argument("--dry-run", action="store_true", help="report every candidate, delete nothing")
    ap.add_argument("--max-deletions", type=int, default=DEFAULT_MAX_DELETIONS,
                    help=f"deletions per run (default {DEFAULT_MAX_DELETIONS}; 0 = no cap)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    dry_run = args.dry_run
    lock_fd = _acquire_dedup_lock()
    if lock_fd is None:
        print("semantic-dedup: another instance holds the lock; aborting", flush=True)
        _append_summary("no-op:lock-held", dry_run=dry_run)
        _write_outcome("degraded:lock-held", {})
        return 0
    try:
        return _run(dry_run, max_deletions=args.max_deletions)
    finally:
        _release_dedup_lock(lock_fd)

def _run(dry_run=False, max_deletions=DEFAULT_MAX_DELETIONS):
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
        _write_outcome("degraded:backend-unreachable", {})
        return 0
    stats: dict = {}
    try:
        pts = scroll_all_with_vectors()
        print(f"loaded {len(pts)} points")
        decisions, stats = plan_dedup(pts, max_deletions)
        print(f"semantic-dedup: scanned={stats['scanned']} skipped_no_vector={stats['skipped_no_vector']} "
              f"compared_pairs={stats['compared_pairs']} candidates={stats['candidates']} "
              f"protected_skips={stats['protected_skips']} max_deletions={max_deletions}", flush=True)
        # F6: dry runs write (and unlink) ONLY their own report file — the
        # real report is the restore record and only a real run replaces it.
        report_path = REPORT_DRY if dry_run else REPORT
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.unlink(missing_ok=True)
        with httpx.Client(headers=H, timeout=15.0) as c, report_path.open("a", encoding="utf-8") as report:
            for d in decisions:
                report_rec = {k: v for k, v in d.items() if k != "within_cap"}
                if dry_run:
                    # the whole backlog goes to the review file, marked by what a live run would do
                    report.write(json.dumps(dict(report_rec, would_delete_this_run=d["within_cap"])) + "\n")
                    if d["within_cap"]:
                        deletions += 1   # would-delete; no API delete, no ledger
                    continue
                if not d["within_cap"]:
                    continue             # over the cap: stays for a later night, not in the restore record
                rid = d["deleted_id"]
                r = c.delete(f"{MEM0}/v1/memories/{rid}")
                if r.status_code == 200:
                    deletions += 1
                    report.write(json.dumps(report_rec) + "\n")
                    # Lens S2: every destructive op appends to the central tier-ledger
                    _append_ledger({
                        "event": "decay-delete", "memory_id": rid,
                        "reason": f"semantic-dedup cosine={d['cosine']} >= threshold={d['threshold']} (tier={d['tier']})",
                        "kept_id": d["kept_id"],
                        "actor": "semantic-dedup",
                    })
    except (httpx.HTTPError, OSError) as e:
        _append_ledger({"event": "dedup-scan-abort", "actor": "semantic-dedup", "reason": f"mid-run failure: {type(e).__name__}: {str(e)[:120]}", "partial_deletions": deletions})
        print(f"semantic-dedup: ABORT mid-run after deletions={deletions} ({e})", flush=True)
        _append_summary(f"degraded:aborted:{type(e).__name__}", deletions=deletions, dry_run=dry_run, counts=stats)
        return 1
    stats["deleted"] = deletions   # what actually happened (a failed API delete is not a deletion)
    outcome = run_outcome(stats)
    label = "DRY-RUN would_delete" if dry_run else "deletions"
    print(f"semantic-dedup: {label}={deletions}, outcome={outcome}, tier_thresholds={TIER_THRESHOLDS}, report={report_path}")
    _append_summary(outcome, deletions=deletions, dry_run=dry_run, counts=stats)
    _write_outcome(outcome, stats)
    return 0

if __name__ == "__main__":
    sys.exit(main())
