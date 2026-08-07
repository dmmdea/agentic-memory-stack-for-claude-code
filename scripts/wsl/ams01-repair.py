#!/usr/bin/env python3
"""ams01-repair.py -- AMS-01: disposition engine for PUT-metadata-wipe damage.

Audit finding AMS-01: mem0's _update_memory rebuilt the Qdrant payload keeping
only {created_at, data, hash, role, text_lemmatized, tier, updated_at, user_id}
(the 8-key fingerprint; tier was restored by the old server code), destroying
every custom key (source, brand, workspace, project, retrievable, retired_at,
superseded_by, extracted_at, event, replaces, ...) on any record that went
through a PUT.

This is a ONE-OFF migration with a receipts discipline: every live write
appends one JSONL receipt line {ts, script, record_id, action, before, after}
(before/after limited to the fields that changed) to a receipts file under
~/.mem0 (env-overridable), so the migration leaves a complete audit trail.

PHASES
  --report (default): enumerate live damaged candidates, join history.db
    (read-only) for ADD/first-UPDATE timestamps, inventory available snapshots,
    check the snapshot restore window, classify dispositions, and write the
    full disposition register JSON.

    Candidate = payload key-set equals the 8-key fingerprint exactly; OR equals
    the 7-key variant (8-key minus tier) AND history.db holds an UPDATE row for
    the id (a 7-key record with no UPDATE row is an old no-source add -- out of
    scope); OR is a near-miss (8-key plus a non-empty subset of
    {contradiction_checked_at, nli_gate_checked_at, tier_actor} only).

    RESTORABLE = some snapshot timestamp falls in [ADD ts, first UPDATE ts).

    Dispositions:
      known test-debris (data exactly 'updated evidence text' or 'validly
        updated canonical text' AND user_id starting 'test')  -> SURFACE-DELETE
      restorable, tier == canonical                            -> SURFACE
      restorable, tier != canonical                            -> RESTORE
      everything else                                          -> MARK

  --apply --dry-run / --apply --live: execute ONLY the RESTORE and MARK
    classes. SURFACE and SURFACE-DELETE are printed and counted, never executed
    (review F1: deletions and canonical writes exceed standing authorisation).
    A separate --execute-surfaced flag exists but REFUSES to run without
    --operator-ack "<free-text reason>"; using it records an
    operator-authorised action in the receipts.

RESTORE mechanics: recover the chosen snapshot into a TEMP collection via the
snapshots recover API (local file URI, resolved by the Qdrant server process),
read the point's pristine payload, and merge back ONLY the destroyed custom
keys via set_payload -- merge-only, no updated_at churn. The temp collection is
dropped at the end and on error. NO snapshot file is ever deleted.

MARK mechanics: set_payload {'provenance_lost': 'ams01-put-wipe',
'provenance_lost_at': <first UPDATE ts from history.db>} -- a factual marker
only; no brand/source value is ever invented (review F1/F2).

Usage:
  ams01-repair.py                     # --report (default): scan -> disposition register
  ams01-repair.py --report
  ams01-repair.py --apply --dry-run   # zero writes; prints plan + samples
  ams01-repair.py --apply --live      # OPERATOR-GATED (RESTORE + MARK only)
  ams01-repair.py --apply --live --execute-surfaced --operator-ack "reason"

Exit codes: 0 success; 2 gating error (bare --apply, --execute-surfaced
without --operator-ack, bad flag combination); 3 precondition failure (Qdrant
not ready, history.db or register missing). Per-record failures during a live
run exit 1.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Environment-driven configuration (same defaults as the sibling scripts)
# ---------------------------------------------------------------------------

QDRANT = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.environ.get("MEM0_COLLECTION", "mem0_egemma_768")
HISTORY_DB = Path(os.environ.get("MEM0_HISTORY_DB", str(Path.home() / ".mem0" / "history.db")))
SNAPSHOT_DIR = Path(os.environ.get(
    "MEM0_QDRANT_SNAPSHOT_DIR",
    str(Path.home() / "qdrant-server" / "snapshots" / COLLECTION),
))
BACKUP_DIR = Path(os.environ.get("MEM0_BACKUP_DIR", str(Path.home() / ".mem0" / "backups")))
REPORT_PATH = Path(os.environ.get("MEM0_AMS01_REPORT", str(Path.home() / ".mem0" / "ams01-repair-report.json")))
RECEIPTS_PATH = Path(os.environ.get("MEM0_AMS01_RECEIPTS", str(Path.home() / ".mem0" / "ams01-repair-receipts.jsonl")))
TMP_COLLECTION = os.environ.get("MEM0_AMS01_TMP_COLLECTION", "ams01-restore-tmp")

# Identity guard (diff-review finding 5): the temp collection is DROPPED by
# this script. A mis-set env var pointing it at the live collection would make
# the script delete production as its first act. Refuse to start unless the
# temp name is distinct AND carries the script's own prefix.
if TMP_COLLECTION == COLLECTION or not TMP_COLLECTION.startswith("ams01-"):
    print(f"FATAL: MEM0_AMS01_TMP_COLLECTION={TMP_COLLECTION!r} is unsafe — it "
          f"must differ from the live collection ({COLLECTION!r}) and start "
          "with 'ams01-' (this collection gets DROPPED).", file=sys.stderr)
    sys.exit(2)

SCRIPT_NAME = "ams01-repair"
_SCROLL_PAGE = 256

# ---------------------------------------------------------------------------
# Damage fingerprints
# ---------------------------------------------------------------------------

_FP8 = frozenset({
    "created_at", "data", "hash", "role", "text_lemmatized", "tier",
    "updated_at", "user_id",
})
_FP7 = _FP8 - {"tier"}
_NEARMISS_EXTRAS = frozenset({
    "contradiction_checked_at", "nli_gate_checked_at", "tier_actor",
})

# Keys never restored from a snapshot: content/lifecycle/identity fields whose
# LIVE values are authoritative (the wipe did not damage them), plus tier
# (tier restoration would be a tier write, which exceeds this script's remit).
_RESTORE_EXCLUDE = frozenset({
    "data", "hash", "text_lemmatized", "created_at", "updated_at", "user_id",
    "agent_id", "run_id", "actor_id", "role", "tier",
})

_TEST_DEBRIS_DATA = frozenset({
    "updated evidence text",
    "validly updated canonical text",
})


def damage_variant(keys: frozenset[str], has_update_row: bool) -> str | None:
    """Return the damage variant name, or None when the key-set is not a
    recognised wipe pattern."""
    if keys == _FP8:
        return "8-key"
    if keys == _FP7:
        # 7-key WITHOUT an UPDATE row = old no-source add, out of scope.
        return "7-key" if has_update_row else None
    if keys > _FP8 and (keys - _FP8) <= _NEARMISS_EXTRAS:
        return "near-miss"
    return None


def is_test_debris(payload: dict) -> bool:
    data = payload.get("data")
    user_id = str(payload.get("user_id") or "")
    return data in _TEST_DEBRIS_DATA and user_id.startswith("test")


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def parse_ts(s: Any) -> dt.datetime | None:
    """Parse an ISO-ish timestamp; naive values are treated as UTC."""
    if not s or not isinstance(s, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# history.db (read-only)
# ---------------------------------------------------------------------------

def load_history_index(db_path: Path) -> dict[str, dict]:
    """One pass over history.db -> {memory_id: {add_ts, first_update_ts,
    update_rows}}. Opened read-only; this script never writes history.

    COLUMN SEMANTICS (live-verified 2026-08-07, and the cause of a shipped
    defect): mem0's add_history stamps the RECORD's creation time into the
    history row's created_at column on EVERY event — an UPDATE row's
    created_at equals the ADD row's. The event's own timestamp lives in
    updated_at. Reading created_at for UPDATE rows makes the restore window
    [ADD, first UPDATE) empty for every record, silently degrading every
    RESTORE to MARK."""
    index: dict[str, dict] = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for memory_id, event, created_at, updated_at in con.execute(
            "SELECT memory_id, event, created_at, updated_at FROM history"
        ):
            rec = index.setdefault(str(memory_id), {
                "add_ts": None, "first_update_ts": None, "update_rows": 0,
            })
            ev = (event or "").upper()
            if ev == "ADD":
                ts = parse_ts(created_at)
                if ts is not None and (rec["add_ts"] is None or ts < rec["add_ts"]):
                    rec["add_ts"] = ts
            elif ev == "UPDATE":
                rec["update_rows"] += 1
                # updated_at = when THIS update happened; created_at would be
                # the record's birth time (see docstring).
                ts = parse_ts(updated_at) or parse_ts(created_at)
                if ts is not None and (rec["first_update_ts"] is None or ts < rec["first_update_ts"]):
                    rec["first_update_ts"] = ts
    finally:
        con.close()
    return index


# ---------------------------------------------------------------------------
# Snapshot inventory
# ---------------------------------------------------------------------------

_TS_FILENAME_PATTERNS = (
    # Qdrant snapshot style: ...-YYYY-MM-DD-HH-MM-SS.snapshot
    re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})"),
    # ISO-ish: YYYY-MM-DDTHH-MM-SS / YYYY-MM-DD_HH:MM:SS variants
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[T_](\d{2})[:\-](\d{2})[:\-](\d{2})"),
    # Compact: YYYYMMDD-HHMMSS / YYYYMMDD_HHMMSS / YYYYMMDDHHMMSS
    re.compile(r"(\d{4})(\d{2})(\d{2})[T_\-]?(\d{2})(\d{2})(\d{2})"),
)


def parse_snapshot_filename_ts(name: str) -> dt.datetime | None:
    for pattern in _TS_FILENAME_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        try:
            parts = [int(g) for g in m.groups()]
            return dt.datetime(*parts, tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def list_snapshots() -> list[dict]:
    """Inventory snapshot files from the Qdrant snapshots directory and the
    backups directory. Timestamps are parsed from filenames; when a filename
    carries no parseable timestamp the file mtime is used (marked as such so
    the register shows the weaker provenance). Snapshot files are NEVER
    deleted or modified by this script."""
    entries: list[dict] = []
    sources = []
    if SNAPSHOT_DIR.is_dir():
        sources.append(("snapshot-dir", sorted(SNAPSHOT_DIR.glob("*.snapshot"))))
    if BACKUP_DIR.is_dir():
        sources.append(("backup-dir", sorted(BACKUP_DIR.glob("qdrant-*.snapshot"))))
    for origin, paths in sources:
        for path in paths:
            ts = parse_snapshot_filename_ts(path.name)
            ts_source = "filename"
            if ts is None:
                ts = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
                ts_source = "mtime"
            entries.append({
                "path": str(path),
                "ts": ts.isoformat(),
                "ts_source": ts_source,
                "origin": origin,
            })
    entries.sort(key=lambda e: e["ts"])
    return entries


def choose_snapshot(snapshots: list[dict], add_ts: dt.datetime | None,
                    first_update_ts: dt.datetime | None) -> dict | None:
    """Latest snapshot whose timestamp falls inside [ADD ts, first UPDATE ts).
    The latest eligible snapshot carries the most complete pre-wipe payload."""
    if add_ts is None or first_update_ts is None:
        return None
    chosen = None
    for snap in snapshots:
        ts = parse_ts(snap["ts"])
        if ts is None:
            continue
        if add_ts <= ts < first_update_ts:
            chosen = snap
    return chosen


# ---------------------------------------------------------------------------
# Qdrant helpers
# ---------------------------------------------------------------------------

def qdrant_ready() -> bool:
    """Readyz preflight -- every mode requires a reachable Qdrant."""
    try:
        httpx.get(f"{QDRANT}/readyz", timeout=5.0).raise_for_status()
        return True
    except Exception as e:
        print(f"ERROR: Qdrant readyz preflight failed: {e}", file=sys.stderr)
        return False


def scroll_all_qdrant_points(client: httpx.Client) -> list[dict]:
    """Page through every point in the memory collection via the Qdrant scroll
    API (same idiom as ship_log_reclassify.py / l10-audit.py)."""
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
            timeout=30.0,
        )
        r.raise_for_status()
        result = r.json().get("result", {})
        points.extend(result.get("points", []))
        next_page = result.get("next_page_offset")
        if not next_page:
            break
    return points


# ---------------------------------------------------------------------------
# Receipts (one-off migration discipline: one JSONL line per live write)
# ---------------------------------------------------------------------------

def write_receipt(record_id: str, action: str, before: dict, after: dict,
                  extra: dict | None = None) -> None:
    RECEIPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {
        "ts": _now_iso(),
        "script": SCRIPT_NAME,
        "record_id": str(record_id),
        "action": action,
        "before": before,
        "after": after,
    }
    if extra:
        rec.update(extra)
    with RECEIPTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Report mode
# ---------------------------------------------------------------------------

def run_report() -> int:
    print(f"=== {SCRIPT_NAME} --report (read-only) ===\n")
    if not qdrant_ready():
        return 3
    if not HISTORY_DB.exists():
        print(f"ERROR: history database not found: {HISTORY_DB}", file=sys.stderr)
        return 3

    print("Loading history index (read-only)...")
    history = load_history_index(HISTORY_DB)

    print("Inventorying snapshots...")
    snapshots = list_snapshots()
    print(f"  snapshot files found: {len(snapshots)}")
    if not snapshots:
        print("  NOTE: no snapshots found -- no candidate can classify as RESTORE/SURFACE.")

    print("Scanning all points via Qdrant scroll...")
    try:
        with httpx.Client() as client:
            points = scroll_all_qdrant_points(client)
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        print(f"ERROR: Qdrant scroll failed: {e}", file=sys.stderr)
        return 3
    print(f"  points scanned: {len(points)}")

    candidates: list[dict] = []
    for pt in points:
        payload = pt.get("payload") or {}
        keys = frozenset(payload)
        hist = history.get(str(pt["id"]))
        has_update_row = bool(hist and hist["update_rows"] > 0)
        variant = damage_variant(keys, has_update_row)
        if variant is None:
            continue
        add_ts = hist["add_ts"] if hist else None
        first_update_ts = hist["first_update_ts"] if hist else None
        snapshot = choose_snapshot(snapshots, add_ts, first_update_ts)
        restorable = snapshot is not None
        tier = payload.get("tier")

        if is_test_debris(payload):
            disposition = "SURFACE-DELETE"
        elif restorable and tier == "canonical":
            disposition = "SURFACE"
        elif restorable:
            disposition = "RESTORE"
        else:
            disposition = "MARK"

        candidates.append({
            "id": str(pt["id"]),
            "variant": variant,
            "payload_keys": sorted(keys),
            "tier": tier,
            "user_id": payload.get("user_id"),
            "data_preview": (payload.get("data") or "")[:80],
            "add_ts": add_ts.isoformat() if add_ts else None,
            "first_update_ts": first_update_ts.isoformat() if first_update_ts else None,
            "restorable": restorable,
            "snapshot": snapshot,
            "disposition": disposition,
        })

    counts: dict[str, int] = {}
    for c in candidates:
        counts[c["disposition"]] = counts.get(c["disposition"], 0) + 1

    register = {
        "generated_at": _now_iso(),
        "script": SCRIPT_NAME,
        "collection": COLLECTION,
        "snapshots": snapshots,
        "counts": counts,
        "candidates": candidates,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(register, indent=2), encoding="utf-8")

    print("\n--- DISPOSITIONS ---")
    for disp in ("RESTORE", "MARK", "SURFACE", "SURFACE-DELETE"):
        print(f"  {disp:<15}: {counts.get(disp, 0)}")
    print("\n--- CANDIDATE TABLE ---")
    print(f"  {'id[:8]':<10} {'variant':<9} {'tier':<10} {'disposition':<15} restorable")
    for c in candidates:
        print(f"  {c['id'][:8]:<10} {c['variant']:<9} {str(c['tier']):<10} "
              f"{c['disposition']:<15} {c['restorable']}")
    print(f"\nDisposition register written: {REPORT_PATH}")
    print("ZERO writes performed.")
    return 0


# ---------------------------------------------------------------------------
# Apply mode
# ---------------------------------------------------------------------------

def _retrieve_payload(qc, collection: str, point_id: Any) -> dict | None:
    """Live payload for one point, or None when the point no longer exists."""
    recs = qc.retrieve(collection_name=collection, ids=[point_id],
                       with_payload=True, with_vectors=False)
    if not recs:
        return None
    return dict(recs[0].payload or {})


def _recover_snapshot_to_tmp(qc, snapshot_path: str) -> None:
    """Recover *snapshot_path* into the temp collection. The file URI is
    resolved by the Qdrant server process, so the path must be readable by the
    server (same-host deployment). The snapshot FILE itself is read-only to
    this script -- never deleted, never modified."""
    try:
        qc.delete_collection(collection_name=TMP_COLLECTION)
    except Exception:
        pass  # temp collection may not exist yet
    location = Path(snapshot_path).resolve().as_uri()
    qc.recover_snapshot(collection_name=TMP_COLLECTION, location=location, wait=True)


def _drop_tmp(qc) -> None:
    try:
        qc.delete_collection(collection_name=TMP_COLLECTION)
    except Exception as e:
        print(f"  WARNING: could not drop temp collection '{TMP_COLLECTION}': {e}",
              file=sys.stderr)


def _restore_one(qc, cand: dict, stats: dict, operator_ack: str | None = None) -> None:
    """Merge-only payload restore for one candidate; the snapshot has already
    been recovered into the temp collection by the caller."""
    pid = cand["id"]
    live_payload = _retrieve_payload(qc, COLLECTION, pid)
    if live_payload is None:
        stats["missing"] += 1
        print(f"  MISSING {pid[:8]} (point no longer exists live)")
        return
    pristine = _retrieve_payload(qc, TMP_COLLECTION, pid)
    if pristine is None:
        stats["errors"] += 1
        print(f"  ERROR {pid[:8]}: point absent from recovered snapshot", file=sys.stderr)
        return
    restore_keys = {
        k: v for k, v in pristine.items()
        if k not in _RESTORE_EXCLUDE and k not in live_payload
    }
    if not restore_keys:
        stats["noop"] += 1
        print(f"  NOOP {pid[:8]} (nothing left to restore -- already repaired?)")
        return
    # Merge-only set_payload: existing live keys are untouched, updated_at does
    # not churn (a provenance restore is not a semantic update).
    qc.set_payload(collection_name=COLLECTION, payload=restore_keys,
                   points=[pid], wait=True)
    extra: dict[str, Any] = {
        "snapshot": cand.get("snapshot"),
        "pristine_keys": sorted(pristine),
        "live_keys_at_restore": sorted(live_payload),
    }
    if operator_ack:
        extra["operator_ack"] = operator_ack
    write_receipt(pid, "restore-payload",
                  {k: None for k in restore_keys},
                  restore_keys, extra=extra)
    stats["restored"] += 1
    print(f"  RESTORED {pid[:8]} keys={sorted(restore_keys)}")


def _mark_one(qc, cand: dict, stats: dict) -> None:
    pid = cand["id"]
    live_payload = _retrieve_payload(qc, COLLECTION, pid)
    if live_payload is None:
        stats["missing"] += 1
        print(f"  MISSING {pid[:8]} (point no longer exists live)")
        return
    if "provenance_lost" in live_payload:
        stats["noop"] += 1
        print(f"  NOOP {pid[:8]} (already marked)")
        return
    # Diff-review finding 6: the register may be stale — a record that
    # regained its provenance keys since --report must NOT be stamped with a
    # false factual claim. Re-verify the LIVE key-set still matches a damage
    # fingerprint before writing anything.
    live_keys = frozenset(live_payload.keys())
    if damage_variant(live_keys, bool(cand.get("first_update_ts"))) is None:
        stats["healed"] = stats.get("healed", 0) + 1
        print(f"  HEALED {pid[:8]} (live key-set no longer matches a damage "
              "fingerprint; not marking)")
        return
    # Factual marker only -- no invented brand/source values (review F1/F2).
    # provenance_lost_at: 'unknown' is an honest value; None would be a typed
    # lie (finding 6, minor leg).
    marker = {
        "provenance_lost": "ams01-put-wipe",
        "provenance_lost_at": cand.get("first_update_ts") or "unknown",
    }
    qc.set_payload(collection_name=COLLECTION, payload=marker, points=[pid], wait=True)
    write_receipt(pid, "mark-provenance-lost", {}, marker)
    stats["marked"] += 1
    print(f"  MARKED {pid[:8]}")


def _delete_one(qc, cand: dict, stats: dict, operator_ack: str) -> None:
    from qdrant_client import models
    pid = cand["id"]
    live_payload = _retrieve_payload(qc, COLLECTION, pid)
    if live_payload is None:
        stats["noop"] += 1
        print(f"  NOOP {pid[:8]} (already deleted)")
        return
    qc.delete(collection_name=COLLECTION,
              points_selector=models.PointIdsList(points=[pid]), wait=True)
    # Receipt captures identity fields only, not the full payload/vector —
    # deliberate: deletion is operator-acked test debris, and the pre-repair
    # snapshots retain the complete point should it ever matter.
    write_receipt(
        pid, "surface-delete",
        {"exists": True, "data": live_payload.get("data"),
         "user_id": live_payload.get("user_id"), "tier": live_payload.get("tier")},
        {"exists": False},
        extra={"operator_ack": operator_ack},
    )
    stats["deleted"] += 1
    print(f"  DELETED {pid[:8]} (operator-authorised)")


def run_apply(args: argparse.Namespace) -> int:
    dry_run: bool = args.dry_run
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"=== {SCRIPT_NAME} --apply ({mode}) ===\n")
    if not qdrant_ready():
        return 3
    if not REPORT_PATH.exists():
        print(f"ERROR: disposition register not found: {REPORT_PATH}\n"
              "Run --report first to generate it.", file=sys.stderr)
        return 3

    register = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    candidates: list[dict] = register.get("candidates", [])
    by_disp: dict[str, list[dict]] = {}
    for c in candidates:
        by_disp.setdefault(c["disposition"], []).append(c)

    restores = by_disp.get("RESTORE", [])
    marks = by_disp.get("MARK", [])
    surfaced = by_disp.get("SURFACE", [])
    surfaced_delete = by_disp.get("SURFACE-DELETE", [])

    print(f"  register: {len(candidates)} candidate(s) "
          f"(RESTORE={len(restores)} MARK={len(marks)} "
          f"SURFACE={len(surfaced)} SURFACE-DELETE={len(surfaced_delete)})")

    # SURFACE classes are surfaced for the operator, never executed by a plain
    # --apply (review F1: deletions and canonical writes exceed standing
    # authorisation).
    if surfaced:
        print("\n--- SURFACED (canonical restores; operator ack required) ---")
        for c in surfaced:
            print(f"  {c['id'][:8]} tier={c['tier']} snapshot={(c.get('snapshot') or {}).get('path')}")
    if surfaced_delete:
        print("\n--- SURFACED-DELETE (test debris; operator ack required) ---")
        for c in surfaced_delete:
            print(f"  {c['id'][:8]} user_id={c['user_id']} data={c['data_preview']!r}")

    execute_surfaced = bool(args.execute_surfaced)
    if execute_surfaced:
        print("\nNOTE: --execute-surfaced given with --operator-ack; the surfaced "
              "dispositions below will be executed and the ack recorded in the "
              "receipts as an operator-authorised action.")

    stats = {"restored": 0, "marked": 0, "deleted": 0, "noop": 0,
             "missing": 0, "errors": 0, "planned": 0}

    if dry_run:
        for c in restores:
            stats["planned"] += 1
            snap = (c.get("snapshot") or {}).get("path")
            print(f"  WOULD-RESTORE {c['id'][:8]} from snapshot {snap} "
                  "(pristine payload read needs a temp-collection recovery, "
                  "a write -- not performed in dry-run)")
        for c in marks:
            stats["planned"] += 1
            print(f"  WOULD-MARK {c['id'][:8]} provenance_lost_at={c.get('first_update_ts')}")
        if execute_surfaced:
            for c in surfaced:
                stats["planned"] += 1
                print(f"  WOULD-RESTORE-SURFACED {c['id'][:8]} (canonical; operator-acked)")
            for c in surfaced_delete:
                stats["planned"] += 1
                print(f"  WOULD-DELETE-SURFACED {c['id'][:8]} (test debris; operator-acked)")
        print(f"\nDRY-RUN complete: planned={stats['planned']}. ZERO writes performed.")
        return 0

    # ---- LIVE apply ----
    from qdrant_client import QdrantClient
    qc = QdrantClient(url=QDRANT)

    # MARK first: cheap, idempotent, and independent of snapshots.
    print("\n--- MARK ---")
    for c in marks:
        try:
            _mark_one(qc, c, stats)
        except Exception as e:
            stats["errors"] += 1
            print(f"  ERROR {c['id'][:8]}: {e}", file=sys.stderr)

    # RESTORE grouped by snapshot so each snapshot is recovered exactly once.
    restore_work: list[tuple[dict, str | None]] = [(c, None) for c in restores]
    if execute_surfaced:
        restore_work += [(c, args.operator_ack) for c in surfaced]
    groups: dict[str, list[tuple[dict, str | None]]] = {}
    for cand, ack in restore_work:
        snap = cand.get("snapshot") or {}
        path = snap.get("path")
        if not path:
            stats["errors"] += 1
            print(f"  ERROR {cand['id'][:8]}: no snapshot recorded in register", file=sys.stderr)
            continue
        groups.setdefault(path, []).append((cand, ack))

    print("\n--- RESTORE ---")
    for snap_path, group in groups.items():
        print(f"  snapshot: {snap_path} ({len(group)} candidate(s))")
        try:
            _recover_snapshot_to_tmp(qc, snap_path)
        except Exception as e:
            stats["errors"] += len(group)
            print(f"  ERROR: snapshot recovery failed: {e}", file=sys.stderr)
            _drop_tmp(qc)
            continue
        try:
            for cand, ack in group:
                try:
                    _restore_one(qc, cand, stats, operator_ack=ack)
                except Exception as e:
                    stats["errors"] += 1
                    print(f"  ERROR {cand['id'][:8]}: {e}", file=sys.stderr)
        finally:
            _drop_tmp(qc)

    if execute_surfaced and surfaced_delete:
        print("\n--- SURFACE-DELETE (operator-authorised) ---")
        for c in surfaced_delete:
            try:
                _delete_one(qc, c, stats, operator_ack=args.operator_ack)
            except Exception as e:
                stats["errors"] += 1
                print(f"  ERROR {c['id'][:8]}: {e}", file=sys.stderr)

    print(f"\n=== apply done: restored={stats['restored']} marked={stats['marked']} "
          f"deleted={stats['deleted']} noop={stats['noop']} missing={stats['missing']} "
          f"errors={stats['errors']} ===")
    if not execute_surfaced and (surfaced or surfaced_delete):
        print(f"  SURFACED (not executed): {len(surfaced)} canonical restore(s), "
              f"{len(surfaced_delete)} test-debris delete(s). Re-run with "
              "--execute-surfaced --operator-ack \"<reason>\" to execute them.")
    print(f"Receipts appended to: {RECEIPTS_PATH}")
    return 0 if stats["errors"] == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--report", action="store_true", default=False,
                    help="Read-only scan -> disposition register JSON (default action)")
    ap.add_argument("--apply", action="store_true", default=False,
                    help="Execute RESTORE + MARK from the register (requires --dry-run "
                         "or --live; bare --apply exits 2)")
    ap.add_argument("--dry-run", action="store_true", default=False, dest="dry_run",
                    help="With --apply: print the plan, zero writes")
    ap.add_argument("--live", action="store_true", default=False,
                    help="With --apply: perform live mutations (OPERATOR-GATED)")
    ap.add_argument("--execute-surfaced", action="store_true", default=False,
                    dest="execute_surfaced",
                    help="Also execute SURFACE (canonical restore) and SURFACE-DELETE "
                         "(test-debris delete) dispositions. REFUSES to run without "
                         "--operator-ack; the ack is recorded in the receipts as an "
                         "operator-authorised action")
    ap.add_argument("--operator-ack", default="", dest="operator_ack",
                    help="Free-text reason authorising --execute-surfaced (recorded "
                         "in the receipts)")
    args = ap.parse_args()

    if args.operator_ack and not args.execute_surfaced:
        print("ERROR: --operator-ack is only valid together with --execute-surfaced.",
              file=sys.stderr)
        return 2
    if args.execute_surfaced:
        if not args.apply:
            print("ERROR: --execute-surfaced requires --apply.", file=sys.stderr)
            return 2
        if not args.operator_ack.strip():
            print(
                "ERROR: --execute-surfaced REFUSES to run without --operator-ack "
                "\"<free-text reason>\".\n"
                "Deletions and canonical writes exceed standing authorisation "
                "(review F1); the ack records an operator-authorised action.",
                file=sys.stderr,
            )
            return 2

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
        return run_apply(args)

    # --report is the default action
    return run_report()


if __name__ == "__main__":
    sys.exit(main())
