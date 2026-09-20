#!/usr/bin/env python3
"""Build/refresh the wiki_pages_egemma_768 Qdrant collection from the operator's LLM Wiki.

Embeds every page under a snapshot of the wiki's curated `wiki/` tree (entities, concepts,
sources, syntheses) with the SAME EmbeddingGemma prefix-shim embedder mem0 uses (document
prefix), into a dedicated Cosine collection — the semantic index behind wiki-search.py and
the wiki-research "does the wiki already know this?" check. The walk is scoped STRICTLY to
that tree: small, curated, markdown-only.

Idempotent + incremental: each point carries a content sha256; unchanged pages are skipped,
changed pages re-embedded, deleted pages removed from the collection.

Inputs (docs/systems/wiki-index.md):
  WIKI_ROOT          the wiki/ tree to index (default ~/wiki-index/wiki, the snapshot the
                     refresh paths write; the vault itself lives on a cloud-synced folder the
                     brain box does not mount)
  WIKI_QDRANT_HOST / WIKI_QDRANT_PORT   the Qdrant to build into (default localhost:6333 —
                     the brain box's own; a replica box reaches it through wiki-index.sh's tunnel)
  MEM0_EMBED_MODEL   the llama-swap model name (config.py; the store's exact GGUF)

Run with the deployed server venv (same as memory-index-build.py):
    ~/apps/mem0-server/.venv/bin/python ~/apps/mem0-scripts/wiki-index-build.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path

WIKI_ROOT = Path(os.environ.get("WIKI_ROOT") or (Path.home() / "wiki-index" / "wiki"))
COLLECTION = "wiki_pages_egemma_768"
DIMS = 768
EMBED_BODY_CHARS = 1200  # title + summary + this much body — one vector per page
SUMMARY_CAP = 300
QDRANT_HOST = os.environ.get("WIKI_QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("WIKI_QDRANT_PORT", "6333"))

sys.path.insert(0, str(Path.home() / "apps" / "mem0-server"))


DIR_TYPE = {"entities": "entity", "concepts": "concept", "sources": "source", "syntheses": "synthesis"}


def parse_page(text: str):
    """Split a wiki page into (frontmatter dict-ish, body). Naive single-level YAML —
    the vault schema only uses scalar/inline-list fields, full YAML is overkill."""
    fm = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    return fm, body


def first_body_line(body: str) -> str:
    """The page's one-sentence definition (vault schema: first non-heading body line)."""
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s[:SUMMARY_CAP]
    return ""


def page_point(path: Path):
    """Build (id, embed_text, payload) for one wiki page. Returns None for empty files."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return None
    rel = path.relative_to(WIKI_ROOT).as_posix()
    fm, body = parse_page(text)
    title = path.stem
    summary = first_body_line(body)
    embed_text = f"{title}\n{summary}\n{body[:EMBED_BODY_CHARS]}"
    pid = str(uuid.uuid5(uuid.NAMESPACE_URL, "wiki:" + rel))
    payload = {
        "path": rel,
        "title": title,
        "type": fm.get("type") or DIR_TYPE.get(path.parent.name, path.parent.name),
        "tags": fm.get("tags", ""),
        "updated": fm.get("updated", ""),
        "summary": summary,
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    return pid, embed_text, payload


def existing_points(client) -> dict:
    """id -> (hash, path) for every point already in the collection."""
    out = {}
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION, limit=256, offset=offset,
            with_payload=["hash", "path"], with_vectors=False,
        )
        for p in points:
            pl = p.payload or {}
            out[str(p.id)] = (pl.get("hash", ""), pl.get("path", ""))
        if offset is None:
            break
    return out


def main() -> int:
    from config import build_embedder
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    if not WIKI_ROOT.is_dir():
        print(f"wiki-index-build: wiki root not found: {WIKI_ROOT} (set WIKI_ROOT, or run a refresh path first)")
        return 1

    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    try:
        client.get_collection(COLLECTION)
    except Exception:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=DIMS, distance=Distance.COSINE),
        )
        print(f"created collection {COLLECTION} on {QDRANT_HOST}:{QDRANT_PORT}")

    emb = build_embedder()
    existing = existing_points(client)

    seen_ids, upserted, skipped, errors = set(), 0, 0, 0
    for path in sorted(WIKI_ROOT.rglob("*.md")):
        built = page_point(path)
        if built is None:
            continue
        pid, embed_text, payload = built
        seen_ids.add(pid)
        if pid in existing and existing[pid][0] == payload["hash"]:
            skipped += 1
            continue
        try:
            vec = emb.embed(embed_text, memory_action="add")
            client.upsert(COLLECTION, points=[
                PointStruct(id=pid, vector=list(vec), payload=payload)])
            upserted += 1
        except Exception as e:  # fail-open per page
            errors += 1
            if errors <= 5:
                print(f"  ERROR {payload['path']}: {str(e)[:120]}")

    stale = [pid for pid in existing if pid not in seen_ids]
    if stale:
        client.delete(COLLECTION, points_selector=stale)
    print(f"wiki-index-build: root={WIKI_ROOT} target={QDRANT_HOST}:{QDRANT_PORT} "
          f"upserted={upserted} unchanged={skipped} deleted={len(stale)} errors={errors} total={len(seen_ids)}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
