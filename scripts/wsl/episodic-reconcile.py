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

The link/orphan reconciliation stays READ-ONLY by construction: its SQLite connection is opened
mode=ro and orphaned links are reported for awareness, never deleted. WP-4 (session-12 audit) added
two bounded, receipted maintenance steps the weekly run now performs before it reads:
  * stale checkpoints: an `in_progress` episode whose last checkpoint (ended_at) is older than
    --stale-days (default 7) is set to `abandoned` - the documented stale-sweep that was never written.
    Every prompt opens an in_progress checkpoint that only a later extraction finalizes, so sessions
    that produced nothing stayed in_progress forever. The receipt says how many and which. This is the ONE
    write, made over its own short read-write connection; rows are never deleted or otherwise edited.
  * embedding backfill: up to --backfill-limit (default 500; 0 disables) complete episodes missing
    from the episode-vector collection are embedded, newest first, through
    scripts/wsl/episode-embed-backfill.py, after polling /health/embedder for a cold seat (skipped when it
    stays down for the whole window). Coverage below 90 %
    of eligible episodes reads `degraded:embedding-coverage-<pct>` (a catching-up gap: reported, exit 0).

1.32.4: the same two upkeep steps also run DAILY as their own chain step, `--upkeep` (systemd/
ams-step-episode-upkeep.service): abandon stale checkpoints + a bounded (200) vector backfill, and nothing
else (the orphan / drift / coverage pass stays Sunday-only). The abandon sweep selects ids, closes them under
one write lock, receipts which (`abandoned_sample`, `abandoned_oldest_ended_at`, `in_progress_remaining`,
`would_abandon`), runs BEFORE the Qdrant readiness gate (it is SQLite-only) and has a `--dry-run` that writes
nothing. The upkeep receipt (`"mode": "upkeep"`) carries the backfill's exact per-id gap (`missing`,
`missing_ids`, `remaining`), and a degraded state (`embedder-down`, `embed-errors-<n>`, `remaining-<n>`,
`backfill-failed`, `abandon-failed`) is reported through the step outcome line with exit 0.

Weekly systemd-user timer: episodic-reconcile.timer (after contradiction-sweep).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # deployed flat: ~/apps/mem0-scripts
import ams_env  # noqa: E402  (URL from authority-url)

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
#                 in the tier-ledger. Measured live 2026-08-24: history.db is the
#                 SUPERSET (every deleter goes through DELETE /v1/memories, 63/63),
#                 the ledger explains 49/63 and is the ACTOR/REASON source plus the
#                 hedge for the day a mem0ai upgrade rebuilds history.db.
#   unexplained = the memory vanished from Qdrant with NO trace = corruption,
#                 bypass, or data loss — the page-worthy class.
# "Explained" means "did not silently vanish", NOT "benign": a buggy deletion
# travels the same authorized transport, so the explained breakdown stays in the
# receipt (count + actor/reason sample) and the tier-ledger actor is surfaced.
# The degraded threshold applies to UNEXPLAINED only, and it is ZERO: with the
# deletion noise removed, even one traceless orphan is store-level integrity loss.
UNEXPLAINED_DEGRADE_THRESHOLD = 0
LEDGER_DELETE_EVENTS = ("delete", "decay-delete")
EVIDENCE_SOURCES = ("history.db", "tier-ledger")


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


COVERAGE_DEGRADE_PCT = 90           # embedded / eligible episodes below this reads degraded
STALE_IN_PROGRESS_DAYS = 7          # an in_progress checkpoint untouched this long is orphaned
BACKFILL_PER_RUN = 500              # bounded embeds per weekly run
UPKEEP_BACKFILL_PER_RUN = 200       # bounded embeds per DAILY upkeep run (the daily step has little to catch up)
EMBEDDER_WAIT_S = 120               # how long the backfill preflight waits for a cold embedder ...
EMBEDDER_STEP_S = 15                # ... probing this often (a cold start is seconds; a dead seat is not)
COVERAGE_OUTCOME_PREFIX = "degraded:embedding-coverage-"


def exit_code_for(outcome: str) -> int:
    """degraded:* -> 1 (the unit visibly fails) EXCEPT an embedding-coverage gap: that is a backlog
    the bounded backfill is working down, so it is reported (outcome + step receipt) and exits 0."""
    o = str(outcome)
    if o.startswith(COVERAGE_OUTCOME_PREFIX):
        return 0
    return 1 if o.startswith("degraded") else 0


def coverage_pct(cov: dict):
    """Embedded / eligible as a whole percent (capped at 100: stale points for retired episodes can
    push the collection past the eligible count), or None when nothing can be said."""
    try:
        eligible, embedded = int(cov.get("eligible")), int(cov.get("embedded"))
    except (TypeError, ValueError):
        return None
    if eligible <= 0 or cov.get("error"):
        return None
    return int(min(embedded, eligible) * 100 / eligible)


def coverage_outcome(cov: dict):
    """'degraded:embedding-coverage-<pct>' when fewer than COVERAGE_DEGRADE_PCT % of eligible episodes
    have a vector, else None. The gap used to be measured every week and reported ok."""
    pct = coverage_pct(cov)
    if pct is not None and pct < COVERAGE_DEGRADE_PCT:
        return f"{COVERAGE_OUTCOME_PREFIX}{pct}"
    return None


def _write_outcome(outcome: str, counts: dict) -> None:
    """The step-outcome line ams-step.sh reads: '<status>[:<reason>] <compact json counts>'."""
    path = os.environ.get("AMS_OUTCOME_FILE")
    if not path:
        return
    try:
        Path(path).write_text(f"{outcome} {json.dumps(counts, separators=(',', ':'))}\n", encoding="utf-8")
    except OSError as e:
        print(f"episodic-reconcile: outcome file write failed (non-fatal): {e}", flush=True)


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


def select_stale_in_progress(conn: sqlite3.Connection, cutoff: str) -> list:
    """(id, session_id, ended_at) of every `in_progress` episode whose last checkpoint (ended_at) is
    older than `cutoff`, oldest first. An unparseable ended_at is left alone rather than guessed at."""
    return conn.execute(
        "SELECT id, session_id, ended_at FROM episodes WHERE state = 'in_progress' "
        "AND julianday(ended_at) IS NOT NULL AND julianday(ended_at) < julianday(?) "
        "ORDER BY julianday(ended_at) ASC, id ASC", (cutoff,)).fetchall()


def abandon_ids(conn: sqlite3.Connection, ids: list, cutoff: str) -> int:
    """Set the selected episodes to `abandoned`; returns how many changed. Every row is re-checked at
    write time (still `in_progress`, still older than `cutoff`), so a checkpoint or a finalize that landed
    after the SELECT is never clobbered: the sweep only ever closes what is STILL stale."""
    changed = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        # REFUTED semgrep sqlalchemy-execute-raw-query: the only thing concatenated is the PLACEHOLDER
        # count ("?,?,?"); every value is bound as a parameter, so no input text reaches the SQL string.
        q = ("UPDATE episodes SET state = 'abandoned' WHERE state = 'in_progress' "
             "AND julianday(ended_at) IS NOT NULL AND julianday(ended_at) < julianday(?) "
             "AND id IN (" + ",".join("?" * len(chunk)) + ")")
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        changed += conn.execute(q, [cutoff, *chunk]).rowcount
    return changed


def sweep_stale_in_progress(db_path: Path, days: int = STALE_IN_PROGRESS_DAYS, now=None, *,
                            dry_run: bool = False, sample_cap: int = 20) -> dict:
    """Close the stale checkpoints and say exactly which. Returns the receipt fields:
    would_abandon (the rows selected), abandoned (the rows changed; 0 on a dry run),
    abandoned_sample ({id, session_id, ended_at}, at most `sample_cap`), abandoned_oldest_ended_at,
    in_progress_remaining (after the sweep) and dry_run.

    The one write this script makes to the ledger, over its own short read-write connection (every other
    ledger connection here is mode=ro; a dry run uses one too). It is SQLite-only: it needs neither Qdrant
    nor the embedder. SELECT and UPDATE share one BEGIN IMMEDIATE, so a checkpoint cannot slip in between
    them (it waits for the commit, then finds no in_progress row and opens a fresh one), and the UPDATE
    re-checks each row anyway. Rows are never deleted and their text is not touched. Idempotent."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(days=days)).isoformat()
    conn = open_ledger_ro(db_path) if dry_run else sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    try:
        if not dry_run:
            conn.execute("BEGIN IMMEDIATE")
        try:
            rows = select_stale_in_progress(conn, cutoff)
            abandoned = 0 if dry_run else abandon_ids(conn, [r[0] for r in rows], cutoff)
            remaining = int(conn.execute("SELECT COUNT(*) FROM episodes WHERE state = 'in_progress'").fetchone()[0])
            if not dry_run:
                conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return {"would_abandon": len(rows), "abandoned": abandoned,
                "abandoned_sample": [{"id": r[0], "session_id": r[1], "ended_at": r[2]} for r in rows[:sample_cap]],
                "abandoned_oldest_ended_at": rows[0][2] if rows else None,
                "in_progress_remaining": remaining, "dry_run": dry_run}
    finally:
        conn.close()


def abandon_stale_in_progress(db_path: Path, days: int = STALE_IN_PROGRESS_DAYS, now=None) -> int:
    """How many stale `in_progress` episodes sweep_stale_in_progress set to `abandoned`. Idempotent."""
    return sweep_stale_in_progress(db_path, days, now)["abandoned"]


def try_sweep_stale(db_path: Path, days: int = STALE_IN_PROGRESS_DAYS, now=None, *,
                    dry_run: bool = False, sample_cap: int = 20):
    """(receipt fields, error): sweep_stale_in_progress that never raises - the reconcile must still write
    its receipt when the sweep cannot run."""
    try:
        return sweep_stale_in_progress(db_path, days, now, dry_run=dry_run, sample_cap=sample_cap), None
    except (sqlite3.Error, OSError) as e:
        return ({"would_abandon": 0, "abandoned": 0, "abandoned_sample": [], "abandoned_oldest_ended_at": None,
                 "in_progress_remaining": None, "dry_run": dry_run}, f"{type(e).__name__}: {str(e)[:100]}")


def try_abandon_stale(db_path: Path, days: int = STALE_IN_PROGRESS_DAYS, now=None):
    """(count, error): abandon_stale_in_progress that never raises."""
    info, error = try_sweep_stale(db_path, days, now)
    return info["abandoned"], error


def _sweep_fields(sweep: dict, abandon_error) -> dict:
    """The sweep's part of a receipt line (weekly and upkeep alike)."""
    return {"abandoned_stale_in_progress": sweep["abandoned"], "abandon_error": abandon_error,
            "would_abandon": sweep["would_abandon"], "abandoned_sample": sweep["abandoned_sample"],
            "abandoned_oldest_ended_at": sweep["abandoned_oldest_ended_at"],
            "in_progress_remaining": sweep["in_progress_remaining"], "dry_run": sweep["dry_run"]}


def _load_backfill():
    """The sibling episode-embed-backfill.py (hyphenated name: loaded by path, deployed flat)."""
    path = Path(__file__).resolve().with_name("episode-embed-backfill.py")
    spec = importlib.util.spec_from_file_location("episode_embed_backfill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_embedding_backfill(limit: int, db_path: Path, *, wait_s: float = EMBEDDER_WAIT_S,
                           step_s: float = EMBEDDER_STEP_S, sleep=time.sleep) -> dict:
    """Embed up to `limit` missing episode summaries. Not run (never an error) when the embedder stays
    down for the whole wait window: the seat unloads when idle and /health/embedder warms it as ACTIVE
    work, so a healthy answer means the backfill can run. A cold seat answers 503 while it loads, so the
    preflight POLLS (ams_env.wait_for_embedder) instead of reading one 503 as 'down' and skipping the run.
    Fail-soft: any failure is reported in the result, not raised."""
    if not ams_env.wait_for_embedder(ams_env.mem0_url(), wait_s, step_s, sleep=sleep):
        print(f"episodic-reconcile: embedder unavailable for {wait_s:g} s - backfill skipped", flush=True)
        return {"not_run": "embedder-down"}
    try:
        res = _load_backfill().run(limit=limit, db_path=db_path)
    except Exception as e:  # noqa: BLE001 - the reconcile must still write its receipt
        return {"embedded": 0, "error": f"{type(e).__name__}: {str(e)[:100]}"}
    return dict(res)


def run_upkeep_backfill(limit: int, db_path: Path, *, dry_run: bool = False,
                        wait_s: float = EMBEDDER_WAIT_S) -> dict:
    """The upkeep's bounded vector backfill. episode-embed-backfill.py DIFFS FIRST (SQL-eligible episodes
    minus the ids in Qdrant), so it polls /health/embedder (`wait_s`) and builds the embedder only when
    something is missing: a clean night makes no embedder call and keeps the 5-minute idle unload. On a dry
    run it only reports the gap. Fail-soft: a failure (Qdrant unreachable, ...) is reported in the result."""
    try:
        res = _load_backfill().run(limit=limit, db_path=db_path, dry_run=dry_run, wait_embedder_s=wait_s)
    except Exception as e:  # noqa: BLE001 - the upkeep must still write its receipt
        return {"embedded": 0, "error": f"{type(e).__name__}: {str(e)[:100]}"}
    return dict(res)


def backfill_reason(res: dict):
    """The degraded reason a backfill result earns, None when it is clean. Mirrors episode-embed-backfill.py
    outcome_for (a test pins the two together): the job could not diff at all, the embedder never came up,
    rows failed, or a backlog is left after the cap."""
    if res.get("error"):
        return "backfill-failed"
    if res.get("aborted") == "embedder-down" or res.get("not_run") == "embedder-down":
        return "embedder-down"
    errors = int(res.get("errors") or 0)
    if errors:
        return f"embed-errors-{errors}"
    remaining = int(res.get("remaining") or 0)
    if remaining:
        return f"remaining-{remaining}"
    return None


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
LEDGER_PARSE_ERRORS: dict = {}   # per-segment bad-line counts from the last ledger_deleted()


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
            q = ("SELECT DISTINCT memory_id FROM history WHERE UPPER(event) = 'DELETE' "
                 "AND memory_id IN (" + ",".join("?" * len(chunk)) + ")")
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            out.update(str(r[0]) for r in conn.execute(q, chunk).fetchall())
        return out
    finally:
        conn.close()


def history_delete_row_count(db_path: Path = HISTORY_DB) -> int:
    """Total DELETE rows in history.db (READ-ONLY; RAISES if unreadable). A live
    store carries tens of thousands; ZERO alongside existing orphans means the
    table was rebuilt/rotated (a mem0ai migration) - evidence loss that would
    otherwise read as 'no deletions' with no error anywhere (review R1 HIGH)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM history WHERE UPPER(event) = 'DELETE'").fetchone()[0])
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
        parsed = bad = 0
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"memory_id"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(rec, dict):
                    # a bare JSON string/array containing "memory_id" passes the
                    # prefilter; .get on it would crash the run with no receipt
                    bad += 1
                    continue
                parsed += 1
                if rec.get("event") not in LEDGER_DELETE_EVENTS:
                    continue
                mid = str(rec.get("memory_id"))
                if mid in wanted:
                    found[mid] = {"actor": rec.get("actor"), "reason": rec.get("reason"),
                                  "event": rec.get("event")}
        if bad and not parsed:
            # a wholly unparseable segment (truncated by disk-full, wrong encoding) is
            # missing evidence, not "no deletions" - raise so the caller records it
            raise OSError(f"{p.name}: {bad} unparseable line(s), 0 parseable - ledger segment unreadable")
        LEDGER_PARSE_ERRORS[p.name] = bad
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


def _record(record: dict, dry_run: bool) -> None:
    """Append the receipt line, or on a dry run print it and write nothing (a dry run must not look like a
    real run to the freshness row that reads the JSONL)."""
    if dry_run:
        record.setdefault("ts", _iso_now())
        print(f"episodic-reconcile: dry-run {json.dumps(record)}", flush=True)
    else:
        _append_summary(record)


def run_upkeep(args, db_path: Path, run_ts: str, backfill_limit: int) -> int:
    """`--upkeep`, the daily chain step: close the stale checkpoints and run the bounded vector backfill,
    nothing else (no orphan, drift or coverage pass: those stay in the Sunday run). Both halves are
    fail-soft and independent: the sweep is SQLite-only and the backfill talks to Qdrant and the embedder
    itself, so an outage on one never stops the other. A degraded state is reported through the outcome
    line and exits 0 (the chain's next steps still run); only a missing ledger fails the step."""
    sweep, abandon_error = try_sweep_stale(db_path, args.stale_days, dry_run=args.dry_run,
                                           sample_cap=args.limit_sample)
    if abandon_error:
        print(f"episodic-reconcile: stale-checkpoint sweep skipped ({abandon_error})", flush=True)
    backfill = (run_upkeep_backfill(backfill_limit, db_path, dry_run=args.dry_run) if backfill_limit > 0
                else {"not_run": "disabled"})
    reasons = (["abandon-failed"] if abandon_error else []) + [r for r in [backfill_reason(backfill)] if r]
    outcome = "dry-run" if args.dry_run else (f"degraded:{','.join(reasons)}" if reasons else "ok")
    cap = args.limit_sample
    summary = {
        "ts": run_ts,
        "mode": "upkeep",
        "stale_days": args.stale_days,
        **_sweep_fields(sweep, abandon_error),
        # the backfill's own per-id diff: exact, unlike the coverage probe's count (SQL TRIM vs Python strip,
        # stale points masking real gaps), so a gap of 4 reads as 4 and names the 4 episodes
        "missing": backfill.get("missing"),
        "missing_ids": list(backfill.get("missing_ids") or [])[:cap],
        "remaining": backfill.get("remaining"),
        "remaining_ids": list(backfill.get("remaining_ids") or [])[:cap],
        "embedding_backfill": {k: v for k, v in backfill.items() if k not in ("missing_ids", "remaining_ids")},
        "outcome": outcome,
    }
    _record(summary, args.dry_run)
    if not args.dry_run:
        _write_outcome(outcome, {"abandoned": sweep["abandoned"], "in_progress_remaining": sweep["in_progress_remaining"],
                                 "embedded": backfill.get("embedded", 0), "missing": backfill.get("missing"),
                                 "remaining": backfill.get("remaining"), "errors": backfill.get("errors", 0)})
    print(f"episodic-reconcile: upkeep done. abandoned_stale={sweep['abandoned']} (would_abandon="
          f"{sweep['would_abandon']}) in_progress_remaining={sweep['in_progress_remaining']} "
          f"missing={backfill.get('missing')} embedded={backfill.get('embedded', 0)} "
          f"remaining={backfill.get('remaining')} outcome={outcome} -> {RECON_LOG}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="v0.27.4 R5: non-destructive episodic-ledger reconciliation")
    parser.add_argument("--limit-sample", type=int, default=20,
                        help="max orphaned/dangling/abandoned/missing ids recorded in a JSONL sample (default 20)")
    parser.add_argument("--db", default=str(EPISODIC_DB), help="episode ledger path (default ~/.mem0/episodic.db)")
    parser.add_argument("--stale-days", type=int, default=STALE_IN_PROGRESS_DAYS,
                        help=f"abandon in_progress episodes untouched this many days (default {STALE_IN_PROGRESS_DAYS})")
    parser.add_argument("--backfill-limit", type=int, default=None,
                        help=f"embed at most this many missing episode summaries per run (default {BACKFILL_PER_RUN}; "
                             f"{UPKEEP_BACKFILL_PER_RUN} with --upkeep; 0 disables)")
    parser.add_argument("--upkeep", action="store_true",
                        help="the daily step: abandon stale checkpoints + the bounded vector backfill ONLY (no "
                             "orphan / drift / coverage pass)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be abandoned (and, with --upkeep, how many vectors are missing); "
                             "write nothing: no ledger change, no embeds, no receipt line")
    args = parser.parse_args()
    run_ts = _iso_now()
    db_path = Path(args.db)
    backfill_limit = args.backfill_limit if args.backfill_limit is not None else (
        UPKEEP_BACKFILL_PER_RUN if args.upkeep else BACKFILL_PER_RUN)

    if not db_path.exists():
        print(f"episodic-reconcile: episodic.db not found at {db_path}", flush=True)
        _record({"outcome": "degraded:no-episodic-db", "ts": run_ts, "db": str(db_path),
                 **({"mode": "upkeep"} if args.upkeep else {})}, args.dry_run)
        return 1

    if args.upkeep:
        return run_upkeep(args, db_path, run_ts, backfill_limit)

    # WP-4 / 1.32.4: abandon orphaned checkpoints FIRST. The sweep is SQLite-only, so a Qdrant outage must
    # not stop it (it used to sit behind the readiness gate below).
    sweep, abandon_error = try_sweep_stale(db_path, args.stale_days, dry_run=args.dry_run,
                                           sample_cap=args.limit_sample)
    abandoned = sweep["abandoned"]
    if abandon_error:
        print(f"episodic-reconcile: stale-checkpoint sweep skipped ({abandon_error})", flush=True)

    qdrant_ok = True
    try:
        httpx.get(f"{QDRANT}/readyz", timeout=5.0).raise_for_status()
    except (httpx.HTTPError, OSError) as e:
        qdrant_ok = False
        print(f"episodic-reconcile: Qdrant unreachable: {e}", flush=True)
        _record({"outcome": "degraded:qdrant-unreachable", "ts": run_ts, "skipped": str(e)[:120],
                 **_sweep_fields(sweep, abandon_error)}, args.dry_run)
        return 1

    # Then backfill missing embeddings, BEFORE the read-only pass below so the coverage figure it reports
    # is the one AFTER this run's catch-up.
    if args.dry_run:
        backfill = {"not_run": "dry-run"}
    elif backfill_limit > 0:
        backfill = run_embedding_backfill(backfill_limit, db_path)
    else:
        backfill = {"not_run": "disabled"}

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
        _record({"outcome": "degraded:qdrant-fetch-failed", "ts": run_ts, "skipped": str(e)[:120],
                 **_sweep_fields(sweep, abandon_error)}, args.dry_run)
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
    history_delete_total = None
    try:
        # probed UNCONDITIONALLY (even with zero orphans) so a dead source is a
        # standing health field, not something discovered the week orphans appear
        history_delete_total = history_delete_row_count()
        hist_del = history_deleted_ids(orphan_ids)
        if n_orphan and history_delete_total == 0:
            # rotated/rebuilt table: "no DELETE rows at all" alongside live orphans is
            # evidence LOSS, not a clean store (review R1 HIGH)
            evidence_errors["history.db"] = "zero DELETE rows in history table (rotated/rebuilt?)"
    except (sqlite3.Error, OSError) as e:
        evidence_errors["history.db"] = f"{type(e).__name__}: {str(e)[:80]}"
    try:
        led_del = ledger_deleted(orphan_ids if orphan_ids else ["__probe__"])
    except OSError as e:
        evidence_errors["tier-ledger"] = f"{type(e).__name__}: {str(e)[:80]}"

    if n_orphan and set(evidence_errors) >= set(EVIDENCE_SOURCES):
        split = None
        outcome = f"degraded:orphan-evidence-unavailable:{n_orphan}"
        print("episodic-reconcile: orphan deletion-evidence sources BOTH unreadable "
              f"({evidence_errors}) - abstaining from the explained/unexplained split", flush=True)
    else:
        split = explain_orphans(result["orphaned_link"], hist_del, led_del)
        outcome = reconcile_outcome(db_present=True, qdrant_ok=qdrant_ok,
                                    orphaned_count=n_orphan,
                                    unexplained_count=len(split["unexplained"]))
        if n_orphan and evidence_errors:
            # ONE source failed: the split ran on half the evidence. A history-only
            # failure would otherwise page "14 unexplained = data loss" with the real
            # cause buried in JSONL; a ledger-only failure would report ok while half
            # the evidence pipeline is dead (review R1 HIGH). Distinct outcome, always.
            missing = ",".join(sorted(evidence_errors))
            outcome = (f"degraded:orphan-evidence-partial:{missing}:"
                       f"unexplained={len(split['unexplained'])}")

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
        "history_delete_rows_total": history_delete_total,
        "ledger_parse_errors": dict(LEDGER_PARSE_ERRORS),
        "embedding_coverage": coverage,   # AMS-19
        **_sweep_fields(sweep, abandon_error),
        "embedding_backfill": backfill,
        "outcome": outcome,
    }
    # WP-4: coverage is a verdict now, not only a number. It never masks a worse outcome (infra,
    # orphans): the headline stays whatever failed first.
    if outcome == "ok":
        outcome = coverage_outcome(coverage) or "ok"
        summary["outcome"] = outcome
    _record(summary, args.dry_run)
    if not args.dry_run:
        _write_outcome(outcome, {"abandoned": abandoned, "embedded": backfill.get("embedded", 0),
                                 "coverage_pct": coverage_pct(coverage),
                                 "orphaned": n_orphan, "dangling": n_dangling})
    n_ex = len(split["explained"]) if split else "n/a"
    n_un = len(split["unexplained"]) if split else "n/a"
    print(f"episodic-reconcile: done. links={len(links)} memory_links={result['memory_links']} "
          f"orphaned={n_orphan} (explained={n_ex} unexplained={n_un} "
          f"evidence_errors={evidence_errors or 'none'}) dangling={n_dangling} "
          f"episode-embeddings missing={coverage.get('missing')}/"
          f"{coverage.get('eligible')} abandoned_stale={abandoned} "
          f"backfilled={backfill.get('embedded', 0)} outcome={outcome} -> {RECON_LOG}",
          flush=True)
    return exit_code_for(outcome)


if __name__ == "__main__":
    sys.exit(main())
