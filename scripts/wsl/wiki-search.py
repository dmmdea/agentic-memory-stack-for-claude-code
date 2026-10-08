#!/usr/bin/env python3
"""Semantic search over the Obsidian LLM Wiki (the wiki profile's collection) — CM5.

Embeds the query with the SAME EmbeddingGemma prefix-shim mem0 uses (query
prefix, asymmetric to the document-prefixed index) and returns the top-K wiki
pages as JSON lines. Used by /wiki-research to answer "does the wiki already
cover this?" before spending web-research budget, and available to any session
as a retrieval CLI.

The Qdrant target defaults to the local loopback and takes WIKI_QDRANT_HOST /
WIKI_QDRANT_PORT (2026-09-20) — the same knobs wiki-index-build.py honours, so
an index built through an ssh tunnel to the authority is searched the same way.

Run with the deployed server venv:
    ~/apps/mem0-server/.venv/bin/python ~/apps/mem0-scripts/wiki-search.py "query" [--k 5]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

QDRANT_HOST = os.environ.get("WIKI_QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("WIKI_QDRANT_PORT", "6333"))

_SERVER_DIRS = [Path(__file__).resolve().parents[2] / "mem0-server",
                Path.home() / "apps" / "mem0-server"]
for _d in _SERVER_DIRS:
    if _d.is_dir():
        sys.path.insert(0, str(_d))
        break
import embedder_profile as _ep  # noqa: E402

# Queries go through the wiki space's hot alias: for a short query it returns the same vector as the
# long alias the pages were built with (one space).
WIKI_PROFILE = _ep.wiki_profile()
COLLECTION = _ep.collection("wiki", WIKI_PROFILE)


def main() -> int:
    ap = argparse.ArgumentParser(description="Semantic search over the LLM Wiki index")
    ap.add_argument("query", help="natural-language query")
    ap.add_argument("--k", type=int, default=5, help="results to return (default 5)")
    args = ap.parse_args()

    from config import build_embedder
    from qdrant_client import QdrantClient

    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    try:
        client.get_collection(COLLECTION)
    except Exception:
        print(f"wiki-search: collection {COLLECTION} missing on {QDRANT_HOST}:{QDRANT_PORT} "
              f"— run wiki-index-build.py first (a dormant replica Qdrant has no collections)")
        return 1

    vec = build_embedder(profile=WIKI_PROFILE).embed(args.query, memory_action="search")
    hits = client.query_points(collection_name=COLLECTION, query=list(vec), limit=args.k).points
    for h in hits:
        pl = h.payload or {}
        print(json.dumps({
            "score": round(float(h.score), 4),
            "path": pl.get("path", ""),
            "title": pl.get("title", ""),
            "type": pl.get("type", ""),
            "summary": pl.get("summary", ""),
        }, ensure_ascii=False))
    if not hits:
        print(json.dumps({"result": "empty"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
