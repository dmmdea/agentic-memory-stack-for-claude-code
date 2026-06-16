#!/usr/bin/env python3
"""v0.27.4 (R5): episodic-ledger reconciliation — NON-DESTRUCTIVE drift detection.

The SQLite episode ledger (~/.mem0/episodic.db: append-only `episodes` + `episode_links`)
cross-references mem0 memory IDs (link_type e.g. 'produced_evidence', target_kind='mem0',
target_id=<mem0 uuid>) and goals. Over time the linked memory can be deleted/retired while the
immutable link remains, or (defensively) a link can reference a missing episode. This job detects
that drift O(N) and REPORTS it — it NEVER mutates the ledger (the ledger's immutability is the
whole point; the audit-trail must stay intact). It is the read-side analogue of decay-scan /
contradiction-sweep: preflight -> read -> classify -> one JSONL summary line.

Findings:
  orphaned_link : a target_kind='mem0' link whose memory is GONE from the live Qdrant collection.
  dangling      : a link whose episode_id is absent from `episodes` (should never happen — episodes
                  are append-only — but reconciliation verifies it).

Output: one JSONL summary line per run -> ~/.mem0/episodic-reconciliation.jsonl
  (read by Test-MemoryStack's reconciliation freshness row). outcome = 'ok' | 'degraded:<reason>'.

This job is READ-ONLY by construction: the SQLite connection is opened mode=ro, and there is no
--apply (there is nothing to mutate — orphaned links are reported for awareness, not deleted).

Weekly systemd-user timer: episodic-reconcile.timer (after contradiction-sweep).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import httpx

QDRANT = "http://127.0.0.1:6333"
COLLECTION = "mem0_egemma_768"  # the live collection (config.py collection_name)
EPISODIC_DB = Path.home() / ".mem0" / "episodic.db"
RECON_LOG = Path.home() / ".mem0" / "episodic-reconciliation.jsonl"
QDRANT_BATCH = 256
# VERIFIED against the live ledger 2026-06-15: produced_evidence links carry target_kind='mem0'
# (NOT 'memory' — the plan's assumption). 'memory' kept defensively for forward-compat.
MEMORY_TARGET_KINDS = ("mem0", "memory")


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in mem0-server/tests/test_episodic_reconcile.py)
# ---------------------------------------------------------------------------

def classify_links(links: list[dict], existing_episode_ids: set,
                   present_memory_ids: set) -> dict:
    """Classify ledger links against the live store. PURE — no I/O.

    links: [{id, episode_id, link_type, target_kind, target_id}, ...]
    existing_episode_ids: episode ids present in `episodes`.
    present_memory_ids: target_ids (target_kind='mem0') confirmed present in Qdrant.

    Returns {"orphaned_link": [...], "dangling": [...], "memory_links": N, "ok": N}.
    A link can be BOTH dangling (missing episode) and orphaned (missing memory); dangling is
    reported first (the episode is the stronger structural anchor) and the link is not double-counted.
    """
    orphaned, dangling = [], []
    memory_links = 0
    for ln in links:
        ep = ln.get("episode_id")
        kind = ln.get("target_kind")
        tid = ln.get("target_id")
        if ep not in existing_episode_ids:
            dangling.append({"link_id": ln.get("id"), "episode_id": ep,
                             "link_type": ln.get("link_type"), "target_kind": kind, "target_id": tid})
            continue
        if kind in MEMORY_TARGET_KINDS:
            memory_links += 1
            if tid not in present_memory_ids:
                orphaned.append({"link_id": ln.get("id"), "episode_id": ep,
                                 "link_type": ln.get("link_type"), "target_id": tid})
    ok = memory_links - len(orphaned)
    return {"orphaned_link": orphaned, "dangling": dangling, "memory_links": memory_links, "ok": ok}


def reconcile_outcome(db_present: bool, qdrant_ok: bool) -> str:
    if not db_present:
        return "degraded:no-episodic-db"
    if not qdrant_ok:
        return "degraded:qdrant-unreachable"
    return "ok"


def exit_code_for(outcome: str) -> int:
    return 1 if str(outcome).startswith("degraded") else 0


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def open_ledger_ro(db_path: Path) -> sqlite3.Connection:
    """Open the episode ledger READ-ONLY (mode=ro) — reconciliation must never mutate it."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_episode_links(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, episode_id, link_type, target_kind, target_id FROM episode_links"
    ).fetchall()
    return [dict(r) for r in rows]


def existing_episode_ids(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT id FROM episodes").fetchall()}


def qdrant_present_ids(http: httpx.Client, ids: list[str]) -> set:
    """Subset of `ids` that EXIST in the live Qdrant collection. RAISES on a transport/HTTP error
    (the caller degrades — a transient failure must NOT be read as 'all memories orphaned')."""
    present = set()
    for i in range(0, len(ids), QDRANT_BATCH):
        chunk = ids[i:i + QDRANT_BATCH]
        r = http.post(f"{QDRANT}/collections/{COLLECTION}/points",
                      json={"ids": chunk, "with_payload": False}, timeout=30.0)
        r.raise_for_status()
        result = r.json().get("result")
        # A 200 whose body is malformed (result missing / null / not a list) must NOT be read as
        # 'all absent' (that would mark every linked memory orphaned). Raise -> the caller degrades.
        if not isinstance(result, list):
            raise ValueError(f"unexpected Qdrant /points response shape (result={type(result).__name__})")
        for p in result:
            present.add(str(p.get("id")))
    return present


def _append_summary(record: dict) -> None:
    record.setdefault("ts", _iso_now())
    record.setdefault("schema_version", "v1")
    try:
        RECON_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RECON_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        print(f"episodic-reconcile: summary append failed (non-fatal): {e}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="v0.27.4 R5: non-destructive episodic-ledger reconciliation")
    parser.add_argument("--limit-sample", type=int, default=20,
                        help="max orphaned/dangling ids recorded in the JSONL sample (default 20)")
    parser.add_argument("--db", default=str(EPISODIC_DB), help="episode ledger path (default ~/.mem0/episodic.db)")
    args = parser.parse_args()
    run_ts = _iso_now()
    db_path = Path(args.db)

    if not db_path.exists():
        print(f"episodic-reconcile: episodic.db not found at {db_path}", flush=True)
        _append_summary({"outcome": "degraded:no-episodic-db", "ts": run_ts, "db": str(db_path)})
        return 1

    qdrant_ok = True
    try:
        httpx.get(f"{QDRANT}/readyz", timeout=5.0).raise_for_status()
    except (httpx.HTTPError, OSError) as e:
        qdrant_ok = False
        print(f"episodic-reconcile: Qdrant unreachable: {e}", flush=True)
        _append_summary({"outcome": "degraded:qdrant-unreachable", "ts": run_ts, "skipped": str(e)[:120]})
        return 1

    conn = open_ledger_ro(db_path)
    try:
        links = read_episode_links(conn)
        ep_ids = existing_episode_ids(conn)
    finally:
        conn.close()

    mem_ids = sorted({str(ln["target_id"]) for ln in links if ln.get("target_kind") in MEMORY_TARGET_KINDS and ln.get("target_id")})
    http = httpx.Client()
    try:
        present = qdrant_present_ids(http, mem_ids) if mem_ids else set()
    except (httpx.HTTPError, OSError, ValueError) as e:
        # ValueError = a malformed 200 (see qdrant_present_ids) — degrade, never false-orphan.
        print(f"episodic-reconcile: Qdrant point-fetch failed: {e}", flush=True)
        _append_summary({"outcome": "degraded:qdrant-fetch-failed", "ts": run_ts, "skipped": str(e)[:120]})
        return 1
    finally:
        http.close()

    result = classify_links(links, ep_ids, present)
    outcome = reconcile_outcome(db_present=True, qdrant_ok=qdrant_ok)
    n_orphan = len(result["orphaned_link"])
    n_dangling = len(result["dangling"])
    summary = {
        "ts": run_ts,
        "total_links": len(links),
        "memory_links": result["memory_links"],
        "episodes": len(ep_ids),
        "orphaned_count": n_orphan,
        "dangling_count": n_dangling,
        "ok_memory_links": result["ok"],
        "orphaned_sample": result["orphaned_link"][: args.limit_sample],
        "dangling_sample": result["dangling"][: args.limit_sample],
        "outcome": outcome,
    }
    _append_summary(summary)
    print(f"episodic-reconcile: done. links={len(links)} memory_links={result['memory_links']} "
          f"orphaned={n_orphan} dangling={n_dangling} (READ-ONLY) outcome={outcome} -> {RECON_LOG}",
          flush=True)
    return exit_code_for(outcome)


if __name__ == "__main__":
    sys.exit(main())
