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
# AMS-19: the episode-vector collection (mirrors mem0-server/episode_embeddings.py
# EPISODE_COLLECTION — this script is deployed standalone and does not import it).
EPISODE_COLLECTION = "episodes_egemma_768"
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


ORPHAN_DEGRADE_THRESHOLD = 10

# 2026-08-24 (9-agent review + adversarial council): every one of the 63 live
# orphans had a DELETE event on record (semantic-dedup / decay purges) — legitimate
# lineage debt, not integrity loss — yet the count WARNed forever and trained the
# operator to ignore the row. Orphans are now split by DELETION EVIDENCE:
#   explained   = a DELETE row in mem0's history.db  OR  a delete/decay-delete event
#                 in the tier-ledger (scripted deleters write the ledger; the API
#                 path writes history.db; joining BOTH is what makes this
#                 migration-proof — a mem0ai upgrade could rebuild history.db).
#   unexplained = the memory vanished from Qdrant with NO trace = corruption,
#                 bypass, or data loss — the page-worthy class.
# "Explained" means "did not silently vanish", NOT "benign": a buggy deletion
# travels the same authorized transport, so the explained breakdown stays in the
# receipt (count + actor/reason sample) and the tier-ledger actor is surfaced.
# The degraded threshold applies to UNEXPLAINED only, and it is ZERO: with the
# deletion noise removed, even one traceless orphan is store-level integrity loss.
UNEXPLAINED_DEGRADE_THRESHOLD = 0
LEDGER_DELETE_EVENTS = ("delete", "decay-delete")


def explain_orphans(orphaned: list[dict], history_deleted: set,
                    ledger_deleted: dict) -> dict:
    """PURE split of orphaned links by deletion evidence.

    history_deleted: memory_ids with a DELETE row in history.db.
    ledger_deleted: {memory_id: {"actor", "reason", "event"}} from the tier-ledger.
    Returns {"explained": [...], "unexplained": [...]}; each explained entry carries
    its evidence source(s) and, when the ledger knows, the actor + reason."""
    explained, unexplained = [], []
    for o in orphaned:
        tid = str(o.get("target_id"))
        src = []
        if tid in history_deleted:
            src.append("history.db")
        if tid in ledger_deleted:
            src.append("tier-ledger")
        if src:
            entry = dict(o, evidence=src)
            led = ledger_deleted.get(tid) or {}
            if led:
                entry["actor"] = led.get("actor")
                entry["reason"] = (led.get("reason") or "")[:80]
                entry["event"] = led.get("event")
            explained.append(entry)
        else:
            unexplained.append(dict(o))
    return {"explained": explained, "unexplained": unexplained}


def reconcile_outcome(db_present: bool, qdrant_ok: bool,
                      orphaned_count: int = 0,
                      threshold: int = ORPHAN_DEGRADE_THRESHOLD,
                      unexplained_count: int | None = None) -> str:
    """AMS-20 (2026-08-08): the run used to report `ok` while COUNTING dozens
    of orphaned links — the finding is precisely that the number was computed
    and then contradicted by the verdict, so nothing ever escalated (59 live
    orphans, ~2/day growth, invisible). A count past the threshold now
    degrades: TMS's freshness row and the SessionStart heartbeat both key off
    `outcome`, so the backlog reaches a human without a new alarm channel.

    2026-08-24: when the caller supplies `unexplained_count` (deletion-evidence
    split available), the verdict keys off UNEXPLAINED orphans only, at
    UNEXPLAINED_DEGRADE_THRESHOLD (zero) — explained orphans are reported, never
    degraded. The legacy total-count path (unexplained_count=None) is kept for
    callers/tests that predate the split."""
    if not db_present:
        return "degraded:no-episodic-db"
    if not qdrant_ok:
        return "degraded:qdrant-unreachable"
    if unexplained_count is not None:
        if unexplained_count > UNEXPLAINED_DEGRADE_THRESHOLD:
            return f"degraded:orphaned-links-unexplained:{unexplained_count}"
        return "ok"
    if orphaned_count > threshold:
        return f"degraded:orphaned-links:{orphaned_count}"
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


def embedding_coverage(conn: sqlite3.Connection, http: httpx.Client) -> dict:
    """AMS-19 (2026-08-08): episode embeddings leak — a fail-soft 429 during
    checkpoint drops the vector and NOTHING ever notices or re-embeds, so the
    episodic semantic search is blind to a growing slice of the ledger (274 of
    1842 eligible at audit time). The backfill script exists but has no
    trigger; the durable fix is that the WEEKLY reconcile now MEASURES the
    coverage, so a growing gap surfaces on the same receipt everything else
    reads. Read-only; never raises (a coverage probe must not fail the run)."""
    out = {"eligible": None, "embedded": None, "missing": None}
    try:
        # Eligibility mirrors what the indexing path actually indexes:
        # `summary_text` non-empty and >= MIN_SUMMARY_CHARS (64) after
        # stripping (episode_embeddings._indexable_summary), AND state =
        # 'complete' — in_progress checkpoint summaries are excluded from
        # indexing ON PURPOSE (noisy), so a probe that counts them
        # over-reports the gap. Both halves of this rule were learned by
        # running the probe against the live ledger: the first cut guessed a
        # `summary` column and fail-softed on 'no such column'; the second
        # counted checkpoints and reported 539 missing where the true
        # complete-state gap was ~15.
        eligible = conn.execute(
            "SELECT COUNT(*) FROM episodes"
            " WHERE state = 'complete'"
            "   AND summary_text IS NOT NULL"
            "   AND LENGTH(TRIM(summary_text)) >= 64").fetchone()[0]
        r = http.post(f"{QDRANT}/collections/{EPISODE_COLLECTION}/points/count",
                      json={"exact": True}, timeout=15.0)
        r.raise_for_status()
        embedded = int((r.json().get("result") or {}).get("count") or 0)
        out.update({"eligible": int(eligible), "embedded": embedded,
                    "missing": max(0, int(eligible) - embedded)})
    except (httpx.HTTPError, OSError, sqlite3.Error, ValueError, KeyError) as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


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


HISTORY_DB = Path.home() / ".mem0" / "history.db"
LEDGER_DIR = Path.home() / ".mem0"


def history_deleted_ids(ids: list[str], db_path: Path = HISTORY_DB) -> set:
    """memory_ids among `ids` with a DELETE row in mem0's history.db (READ-ONLY).
    RAISES on any failure — the caller decides whether missing evidence is a
    reason to abstain from classifying (it is; see main)."""
    if not ids:
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            # REFUTED semgrep sqlalchemy-execute-raw-query: the only thing concatenated
            # is the PLACEHOLDER count ("?,?,?"); every value is bound as a parameter
            # via sqlite3's DB-API, so no input text ever reaches the SQL string.
            q = ("SELECT DISTINCT memory_id FROM history WHERE event = 'DELETE' "
                 "AND memory_id IN (" + ",".join("?" * len(chunk)) + ")")
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            out.update(str(r[0]) for r in conn.execute(q, chunk).fetchall())
        return out
    finally:
        conn.close()


def ledger_deleted(ids: list[str], ledger_dir: Path = LEDGER_DIR) -> dict:
    """{memory_id: {actor, reason, event}} for `ids` that carry a delete-class event
    in the tier-ledger (legacy tier-ledger.jsonl + monthly segments). Scripted
    deleters (semantic-dedup, decay-scan) write ONLY here, so this source is what
    explains their purges; the API path writes both. RAISES when no ledger file
    can be read at all (missing evidence is not 'no deletions')."""
    wanted = set(ids)
    found: dict = {}
    if not wanted:
        return found
    paths = sorted(ledger_dir.glob("tier-ledger-[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl"))
    legacy = ledger_dir / "tier-ledger.jsonl"
    if legacy.is_file():
        paths.insert(0, legacy)
    if not paths:
        raise FileNotFoundError(f"no tier-ledger files under {ledger_dir}")
    for p in paths:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"memory_id"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") not in LEDGER_DELETE_EVENTS:
                    continue
                mid = str(rec.get("memory_id"))
                if mid in wanted:
                    found[mid] = {"actor": rec.get("actor"), "reason": rec.get("reason"),
                                  "event": rec.get("event")}
    return found


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
    coverage: dict = {"eligible": None, "embedded": None, "missing": None}
    try:
        links = read_episode_links(conn)
        ep_ids = existing_episode_ids(conn)
        # AMS-19: measure the embedding gap while the read-only handle is open.
        with httpx.Client() as _cov_http:
            coverage = embedding_coverage(conn, _cov_http)
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
    n_orphan = len(result["orphaned_link"])
    n_dangling = len(result["dangling"])

    # 2026-08-24: split orphans by deletion evidence (history.db + tier-ledger).
    # Missing evidence is NOT "no deletions": if NEITHER source can be read the run
    # abstains from the split and degrades distinctly rather than accusing every
    # orphan of being traceless; if one source fails, classify with the other and
    # say so in the receipt.
    orphan_ids = [str(o.get("target_id")) for o in result["orphaned_link"]]
    evidence_errors: dict = {}
    hist_del: set = set()
    led_del: dict = {}
    try:
        hist_del = history_deleted_ids(orphan_ids)
    except (sqlite3.Error, OSError) as e:
        evidence_errors["history.db"] = f"{type(e).__name__}: {str(e)[:80]}"
    try:
        led_del = ledger_deleted(orphan_ids)
    except OSError as e:
        evidence_errors["tier-ledger"] = f"{type(e).__name__}: {str(e)[:80]}"

    if n_orphan and len(evidence_errors) == 2:
        split = None
        outcome = f"degraded:orphan-evidence-unavailable:{n_orphan}"
        print("episodic-reconcile: orphan deletion-evidence sources BOTH unreadable "
              f"({evidence_errors}) - abstaining from the explained/unexplained split", flush=True)
    else:
        split = explain_orphans(result["orphaned_link"], hist_del, led_del)
        outcome = reconcile_outcome(db_present=True, qdrant_ok=qdrant_ok,
                                    orphaned_count=n_orphan,
                                    unexplained_count=len(split["unexplained"]))

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
        # 2026-08-24 deletion-evidence split (explained != benign: the actor/reason
        # sample is there precisely so a suspicious explained burst is visible)
        "orphaned_explained_count": (len(split["explained"]) if split else None),
        "orphaned_unexplained_count": (len(split["unexplained"]) if split else None),
        "orphaned_explained_sample": (split["explained"][: args.limit_sample] if split else []),
        "orphaned_unexplained_sample": (split["unexplained"][: args.limit_sample] if split else []),
        "orphan_evidence_errors": evidence_errors,
        "embedding_coverage": coverage,   # AMS-19
        "outcome": outcome,
    }
    _append_summary(summary)
    print(f"episodic-reconcile: done. links={len(links)} memory_links={result['memory_links']} "
          f"orphaned={n_orphan} dangling={n_dangling} "
          f"episode-embeddings missing={coverage.get('missing')}/"
          f"{coverage.get('eligible')} (READ-ONLY) outcome={outcome} -> {RECON_LOG}",
          flush=True)
    return exit_code_for(outcome)


if __name__ == "__main__":
    sys.exit(main())
