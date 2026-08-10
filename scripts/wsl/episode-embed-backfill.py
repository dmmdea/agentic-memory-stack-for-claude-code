#!/usr/bin/env python3
"""Backfill episodes_egemma_768 with embeddings of complete episode summaries (v0.29 R4).

One-shot, idempotent migration: embeds the SUMMARY of every state='complete'
episode that is not already in the collection, with the SAME EmbeddingGemma
embedder mem0 uses (document prefix). Brand comes from a JOIN on sessions (the
embed-on-finalize hook in app.py uses the POST's session brand identically).

Excludes state='in_progress' rows on purpose: their summaries are noisy
accumulating checkpoints (and the lone eval-probe-polluted ep3997 is in_progress),
so only authoritative finalized summaries are indexed — matching the live hook.

Run from anywhere; imports the deployed server module:
    ~/apps/mem0-server/.venv/bin/python scripts/wsl/episode-embed-backfill.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "apps" / "mem0-server"))

from episode_embeddings import (  # noqa: E402
    EPISODE_COLLECTION,
    ensure_episode_collection,
    embed_episode_summary,
    upsert_episode_embedding,
    _indexable_summary,
)


def _existing_ids(client) -> set:
    """All point ids already in the collection (idempotent skip set)."""
    ids = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=EPISODE_COLLECTION,
            limit=256, offset=offset, with_payload=False, with_vectors=False,
        )
        ids.update(p.id for p in points)
        if offset is None:
            break
    return ids


def main() -> int:
    import sqlite3
    from config import build_embedder
    from qdrant_client import QdrantClient

    db = os.path.expanduser("~/.mem0/episodic.db")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    client = QdrantClient(host="localhost", port=6333)
    emb = build_embedder()

    ensure_episode_collection(client)
    existing = _existing_ids(client)

    rows = conn.execute(
        """
        SELECT e.id AS id, e.goal_text AS goal_text, e.summary_text AS summary_text, s.brand AS brand
        FROM episodes e
        LEFT JOIN sessions s ON e.session_id = s.session_id
        WHERE e.state = 'complete'
          AND e.summary_text IS NOT NULL AND TRIM(e.summary_text) <> ''
        ORDER BY e.id
        """
    ).fetchall()

    total, embedded, skipped, errors = len(rows), 0, 0, 0
    for r in rows:
        if r["id"] in existing:
            skipped += 1
            continue
        if not _indexable_summary(r["summary_text"]):
            skipped += 1  # degenerate-short / test-artifact summary — don't index
            continue
        try:
            vec = embed_episode_summary(emb, r["summary_text"])
            if vec is None:
                skipped += 1
                continue
            upsert_episode_embedding(
                client, r["id"], vec,
                {"brand": r["brand"], "goal": (r["goal_text"] or "")[:300],
                 "summary": (r["summary_text"] or "")[:800]},
            )
            embedded += 1
            if embedded % 50 == 0:
                print(f"  embedded {embedded}/{total}")
        except Exception as e:  # fail-open per row
            errors += 1
            if errors <= 5:
                print(f"  ERROR ep{r['id']}: {str(e)[:120]}")

    conn.close()
    print(f"backfill done: embedded={embedded} skipped={skipped} errors={errors} total_complete={total}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
