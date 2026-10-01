#!/usr/bin/env python3
"""Backfill episodes_egemma_768 with embeddings of complete episode summaries (v0.29 R4).

Idempotent: embeds the SUMMARY of every state='complete' episode that is not already in the
collection, with the SAME EmbeddingGemma embedder mem0 uses (document prefix). Brand comes from a
JOIN on sessions (the embed-on-finalize hook in app.py uses the POST's session brand identically).

Excludes state='in_progress' rows on purpose: their summaries are noisy accumulating checkpoints
(and the lone eval-probe-polluted ep3997 is in_progress), so only authoritative finalized summaries
are indexed - matching the live hook.

WP-4: the weekly episodic-reconcile now runs this, BOUNDED (run(limit=500)), newest episodes first,
so the semantic layer catches up over a few Sundays instead of staying at the gap the one-shot
migration left. A hand run with no limit embeds everything, oldest first, as before.

1.32.4: it survives a cold start. Its first run after an embedder restart left four vectors missing
(500 'upstream command exited prematurely' while the seat started; the second run embedded them), so a
cold-shaped failure (embedder_503.retry_later: a refused or timed-out connection, 502/503/504, that 500)
is now retried per row through episode_embeddings.embed_with_cold_retry, inside ONE run budget
(--retry-budget-s); when the budget is spent the run stops as aborted="embedder-down" instead of burning
five rows of retries. It also DIFFS FIRST (the SQL-eligible episodes minus the ids already in Qdrant), so
a run with nothing missing makes no embedder call and builds no embedder, and it reports the exact
per-id gap (`missing`, `missing_ids`, `remaining`, `remaining_ids`). Under ams-step ($AMS_OUTCOME_FILE
set) it writes the step-outcome line and exits 0 for a reported degraded state.

Run from anywhere; imports the deployed server module:
    ~/apps/mem0-server/.venv/bin/python scripts/wsl/episode-embed-backfill.py
    ... --limit 200 --wait-embedder 120 --retry-budget-s 120 [--dry-run] [--db PATH]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# The server modules live in mem0-server/: a sibling of scripts/ in the repo layout, and
# ~/apps/mem0-server when the scripts are deployed flat into ~/apps/mem0-scripts.
_SERVER_DIRS = [Path(__file__).resolve().parents[2] / "mem0-server",
                Path.home() / "apps" / "mem0-server"]
for _d in _SERVER_DIRS:
    if _d.is_dir():
        sys.path.insert(0, str(_d))
        break
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)  # ams_env, a sibling in both layouts; after the server dir, so it can never shadow a server module

MAX_CONSECUTIVE_ERRORS = 5   # a dead embedder must stop the run, not burn the whole cap on failures
RETRY_BUDGET_S = 120.0       # seconds of cold-start backoff ONE run may spend across all its rows
WAIT_STEP_S = 15             # --wait-embedder probes /health/embedder this often

# The two orderings are two literal statements, never one statement with the direction pasted in:
# a query string with a formatted-in fragment is what a SQL audit rightly refuses to read past.
COMPLETE_EPISODES_OLDEST_FIRST = """
        SELECT e.id AS id, e.goal_text AS goal_text, e.summary_text AS summary_text, s.brand AS brand
        FROM episodes e
        LEFT JOIN sessions s ON e.session_id = s.session_id
        WHERE e.state = 'complete'
          AND e.summary_text IS NOT NULL AND TRIM(e.summary_text) <> ''
        ORDER BY e.id ASC
        """
COMPLETE_EPISODES_NEWEST_FIRST = """
        SELECT e.id AS id, e.goal_text AS goal_text, e.summary_text AS summary_text, s.brand AS brand
        FROM episodes e
        LEFT JOIN sessions s ON e.session_id = s.session_id
        WHERE e.state = 'complete'
          AND e.summary_text IS NOT NULL AND TRIM(e.summary_text) <> ''
        ORDER BY e.id DESC
        """


def _existing_ids(client, collection) -> set:
    """All point ids already in the collection (idempotent skip set)."""
    ids = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=256, offset=offset, with_payload=False, with_vectors=False,
        )
        ids.update(p.id for p in points)
        if offset is None:
            break
    return ids


def _missing_rows(rows, existing) -> list:
    """The per-id diff: indexable complete episodes whose id is not in `existing`, in `rows` order.
    'Indexable' is episode_embeddings._indexable_summary (Python strip() >= 64 chars), the indexer's own
    rule, not SQLite's TRIM, which leaves tabs and newlines in place."""
    from episode_embeddings import _indexable_summary
    return [r for r in rows if r["id"] not in existing and _indexable_summary(r["summary_text"])]


def backfill(conn, existing, embedder, upsert, limit=None, *, retry_budget_s=RETRY_BUDGET_S,
             sleep=time.sleep, clock=time.monotonic) -> dict:
    """Embed complete episodes missing from `existing` (a set of episode ids).

    conn: an open sqlite3 connection with row_factory=sqlite3.Row (read-only is enough).
    upsert(ep_id, vector, payload): writes one point.
    limit: at most this many embeds per call (None = all, oldest first). With a limit the NEWEST
    episodes go first: recent history is the most useful to recall, and an old gap can wait.
    retry_budget_s: seconds of cold-start backoff the whole call may spend (shared by every row).
    Returns {embedded, skipped, errors, remaining, total_complete, aborted, missing, missing_ids,
    remaining_ids}: `missing` is the exact per-id gap before this call, `remaining` what is still
    missing after the cap, so the caller can report the catch-up. aborted is "embedder-down" when a
    cold embedder did not come up inside the budget (that row's retries are already spent: stop), or a
    message when MAX_CONSECUTIVE_ERRORS plain failures in a row said the embedder is broken."""
    from episode_embeddings import embed_with_cold_retry, is_cold_embed_error
    rows = conn.execute(COMPLETE_EPISODES_NEWEST_FIRST if limit else COMPLETE_EPISODES_OLDEST_FIRST).fetchall()
    todo = _missing_rows(rows, existing)
    out = {"embedded": 0, "skipped": len(rows) - len(todo), "errors": 0, "remaining": 0,
           "total_complete": len(rows), "aborted": None,
           "missing": len(todo), "missing_ids": [r["id"] for r in todo], "remaining_ids": []}
    consecutive = 0
    backoff_spent = 0.0

    def counted_sleep(seconds):
        nonlocal backoff_spent
        backoff_spent += seconds
        sleep(seconds)

    done = set()                       # ids embedded, or skipped for an empty vector
    for r in todo:
        if out["aborted"] or (limit and out["embedded"] >= limit):
            break
        try:
            vec = embed_with_cold_retry(embedder, r["summary_text"], budget_s=retry_budget_s - backoff_spent,
                                        sleep=counted_sleep, clock=clock)
            if vec is None:
                out["skipped"] += 1
                done.add(r["id"])
                continue
            upsert(r["id"], vec, {"brand": r["brand"], "goal": (r["goal_text"] or "")[:300],
                                  "summary": (r["summary_text"] or "")[:800]})
            out["embedded"] += 1
            done.add(r["id"])
            consecutive = 0
        except Exception as e:  # fail-open per row
            out["errors"] += 1
            if out["errors"] <= 5:
                print(f"  ERROR ep{r['id']}: {str(e)[:120]}")
            if is_cold_embed_error(e):
                # embed_with_cold_retry only lets a cold error out once its delays or the run budget
                # are spent: the seat is not coming up, so every further row would only wait again.
                out["aborted"] = "embedder-down"
                continue
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_ERRORS:
                out["aborted"] = f"{consecutive} consecutive embed failures (last: {str(e)[:80]})"
    out["remaining_ids"] = [r["id"] for r in todo if r["id"] not in done]
    out["remaining"] = len(out["remaining_ids"])
    return out


def _run_core(conn, existing, make_embedder, upsert, *, limit=None, dry_run=False, wait_embedder_s=0,
              retry_budget_s=RETRY_BUDGET_S, wait=None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """The wired run, with every live piece injected (the ledger connection, the Qdrant id set, the embedder
    FACTORY, the upsert, the embedder wait) so it is tested headless. DIFFS FIRST: the embedder is built, and
    /health/embedder polled, only when something is missing (and not on a dry run): a run with nothing to do
    makes no embedder call, which keeps the model's 5-minute idle unload. wait(total_s) -> bool."""
    rows = conn.execute(COMPLETE_EPISODES_NEWEST_FIRST if limit else COMPLETE_EPISODES_OLDEST_FIRST).fetchall()
    todo = _missing_rows(rows, existing)
    idle = {"embedded": 0, "skipped": len(rows) - len(todo), "errors": 0, "remaining": len(todo),
            "total_complete": len(rows), "aborted": None, "missing": len(todo),
            "missing_ids": [r["id"] for r in todo], "remaining_ids": [r["id"] for r in todo]}
    if dry_run:
        return dict(idle, dry_run=True)
    if not todo:
        return idle
    if wait_embedder_s and not (wait or _default_wait(sleep))(wait_embedder_s):
        print(f"episode-embed-backfill: embedder unavailable for {wait_embedder_s:g} s - nothing embedded")
        return dict(idle, aborted="embedder-down")
    return backfill(conn, existing, make_embedder(), upsert, limit=limit, retry_budget_s=retry_budget_s,
                    sleep=sleep, clock=clock)


def _default_wait(sleep):
    import ams_env
    return lambda total_s: ams_env.wait_for_embedder(ams_env.mem0_url(), total_s, WAIT_STEP_S, sleep=sleep)


def run(limit=None, db_path=None, *, dry_run=False, wait_embedder_s=0, retry_budget_s=RETRY_BUDGET_S) -> dict:
    """Wire the live pieces (ledger read-only, Qdrant, the mem0 embedder) and run the diff-first backfill."""
    import sqlite3
    from episode_embeddings import (EPISODE_COLLECTION, ensure_episode_collection,
                                    upsert_episode_embedding)
    from qdrant_client import QdrantClient

    db = str(db_path or os.path.expanduser("~/.mem0/episodic.db"))
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    client = QdrantClient(host="localhost", port=6333)
    try:
        if not dry_run:
            ensure_episode_collection(client)
        existing = _existing_ids(client, EPISODE_COLLECTION)

        def make_embedder():
            from config import build_embedder   # imports mem0: only when there is something to embed
            return build_embedder()

        return _run_core(conn, existing, make_embedder,
                         lambda ep_id, vec, payload: upsert_episode_embedding(client, ep_id, vec, payload),
                         limit=limit, dry_run=dry_run, wait_embedder_s=wait_embedder_s,
                         retry_budget_s=retry_budget_s)
    finally:
        conn.close()


def outcome_for(res: dict) -> str:
    """The ams-step outcome status for a run's result: ok, or degraded:<reason>. The reasons, worst first:
    the run could not diff at all (backfill-failed), the embedder never came up (embedder-down), rows
    failed (embed-errors-<n>), a backlog is left after the cap (remaining-<n>)."""
    if res.get("error"):
        return "degraded:backfill-failed"
    if res.get("aborted") == "embedder-down":
        return "degraded:embedder-down"
    errors = int(res.get("errors") or 0)
    if errors:
        return f"degraded:embed-errors-{errors}"
    remaining = int(res.get("remaining") or 0)
    if remaining:
        return f"degraded:remaining-{remaining}"
    return "ok"


def _write_outcome(outcome: str, counts: dict) -> bool:
    """The step-outcome line ams-step.sh reads: '<status>[:<reason>] <compact json counts>'. True when
    $AMS_OUTCOME_FILE is set (the run is under the chain), whether or not the write succeeded."""
    path = os.environ.get("AMS_OUTCOME_FILE")
    if not path:
        return False
    try:
        Path(path).write_text(f"{outcome} {json.dumps(counts, separators=(',', ':'))}\n", encoding="utf-8")
    except OSError as e:
        print(f"episode-embed-backfill: outcome file write failed (non-fatal): {e}", flush=True)
    return True


def _positive(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Embed complete episode summaries missing from the vector collection")
    ap.add_argument("--limit", type=_positive, default=None,
                    help="embed at most this many (newest first); default all, oldest first")
    ap.add_argument("--db", default=None, help="episode ledger path (default ~/.mem0/episodic.db)")
    ap.add_argument("--wait-embedder", type=float, default=0, metavar="SECONDS",
                    help="when something is missing, poll /health/embedder this long for a cold seat "
                         "(default 0: no wait)")
    ap.add_argument("--retry-budget-s", type=float, default=RETRY_BUDGET_S, metavar="SECONDS",
                    help=f"cold-start backoff one run may spend in total (default {RETRY_BUDGET_S:g})")
    ap.add_argument("--dry-run", action="store_true", help="report the gap; embed and write nothing")
    args = ap.parse_args(argv)
    res = run(limit=args.limit, db_path=args.db, wait_embedder_s=args.wait_embedder,
              retry_budget_s=args.retry_budget_s, dry_run=args.dry_run)
    print(f"backfill done: embedded={res['embedded']} skipped={res['skipped']} errors={res['errors']} "
          f"total_complete={res['total_complete']} missing={res.get('missing')} remaining={res['remaining']}"
          + (f" aborted={res['aborted']}" if res.get("aborted") else ""))
    under_chain = _write_outcome(outcome_for(res), {
        "embedded": res["embedded"], "missing": res.get("missing"), "remaining": res["remaining"],
        "errors": res["errors"]})
    if under_chain:
        return 0                                   # a degraded state is reported by the line, not the exit code
    return 0 if res["errors"] == 0 else 1          # a hand run keeps the legacy exit code


if __name__ == "__main__":
    sys.exit(main())
