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

Run from anywhere; imports the deployed server module:
    ~/apps/mem0-server/.venv/bin/python scripts/wsl/episode-embed-backfill.py
"""
import os
import sys
from pathlib import Path

# The server modules live in mem0-server/: a sibling of scripts/ in the repo layout, and
# ~/apps/mem0-server when the scripts are deployed flat into ~/apps/mem0-scripts.
_SERVER_DIRS = [Path(__file__).resolve().parents[2] / "mem0-server",
                Path.home() / "apps" / "mem0-server"]
for _d in _SERVER_DIRS:
    if _d.is_dir():
        sys.path.insert(0, str(_d))
        break

MAX_CONSECUTIVE_ERRORS = 5   # a dead embedder must stop the run, not burn the whole cap on failures

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


def backfill(conn, existing, embedder, upsert, limit=None) -> dict:
    """Embed complete episodes missing from `existing` (a set of episode ids).

    conn: an open sqlite3 connection with row_factory=sqlite3.Row (read-only is enough).
    upsert(ep_id, vector, payload): writes one point.
    limit: at most this many embeds per call (None = all, oldest first). With a limit the NEWEST
    episodes go first: recent history is the most useful to recall, and an old gap can wait.
    Returns {embedded, skipped, errors, remaining, total_complete, aborted}; `remaining` counts
    indexable episodes still missing after the cap, so the caller can report the catch-up."""
    from episode_embeddings import embed_episode_summary, _indexable_summary
    rows = conn.execute(COMPLETE_EPISODES_NEWEST_FIRST if limit else COMPLETE_EPISODES_OLDEST_FIRST).fetchall()
    out = {"embedded": 0, "skipped": 0, "errors": 0, "remaining": 0,
           "total_complete": len(rows), "aborted": None}
    consecutive = 0
    missing = 0                        # indexable episodes not yet in the collection
    for r in rows:
        if r["id"] in existing or not _indexable_summary(r["summary_text"]):
            out["skipped"] += 1        # already embedded, or a degenerate-short / test-artifact summary
            continue
        missing += 1
        if out["aborted"] or (limit and out["embedded"] >= limit):
            continue
        try:
            vec = embed_episode_summary(embedder, r["summary_text"])
            if vec is None:
                out["skipped"] += 1
                missing -= 1
                continue
            upsert(r["id"], vec, {"brand": r["brand"], "goal": (r["goal_text"] or "")[:300],
                                  "summary": (r["summary_text"] or "")[:800]})
            out["embedded"] += 1
            consecutive = 0
        except Exception as e:  # fail-open per row
            out["errors"] += 1
            consecutive += 1
            if out["errors"] <= 5:
                print(f"  ERROR ep{r['id']}: {str(e)[:120]}")
            if consecutive >= MAX_CONSECUTIVE_ERRORS:
                out["aborted"] = f"{consecutive} consecutive embed failures (last: {str(e)[:80]})"
    out["remaining"] = missing - out["embedded"]
    return out


def run(limit=None, db_path=None) -> dict:
    """Wire the live pieces (ledger read-only, Qdrant, the mem0 embedder) and run backfill()."""
    import sqlite3
    from config import build_embedder
    from episode_embeddings import (EPISODE_COLLECTION, ensure_episode_collection,
                                    upsert_episode_embedding)
    from qdrant_client import QdrantClient

    db = str(db_path or os.path.expanduser("~/.mem0/episodic.db"))
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    client = QdrantClient(host="localhost", port=6333)
    try:
        ensure_episode_collection(client)
        existing = _existing_ids(client, EPISODE_COLLECTION)
        return backfill(conn, existing, build_embedder(),
                        lambda ep_id, vec, payload: upsert_episode_embedding(client, ep_id, vec, payload),
                        limit=limit)
    finally:
        conn.close()


def main() -> int:
    res = run()
    print(f"backfill done: embedded={res['embedded']} skipped={res['skipped']} errors={res['errors']} "
          f"total_complete={res['total_complete']}")
    return 0 if res["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
