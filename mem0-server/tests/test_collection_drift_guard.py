"""v1.0 drift guard — operator directive: qdrant-level references can never go stale.

The EmbeddingGemma migration renamed the mem0 collection `memories` ->
`mem0_egemma_768`. `_debris_patterns.QDRANT_COLLECTION` kept the stale `memories`
literal, so the test-cleanup Qdrant scroll 404'd, the conftest snapshot/teardown
silently no-op'd, and 363 `kind=test` records accumulated in the LIVE store before
this was caught (the same stale-name bug was fixed for contradiction-sweep at
v0.27.3 — this module was missed).

This guard fails loudly the moment the test-cleanup collection name diverges from
the collection the SERVER is actually bound to (reported live at /health/deep), so
the same class of silent staleness can never recur.
"""
import os

import httpx

import _debris_patterns

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")


def test_debris_cleanup_collection_tracks_live_binding():
    """The test-cleanup must scroll the SAME Qdrant collection the server is bound to.
    If it drifts, list_user_memories_full 404s and test records silently leak."""
    live = httpx.get(f"{URL}/health/deep", timeout=10).json().get("collection")
    assert live, "/health/deep did not report a live collection name"
    assert _debris_patterns.QDRANT_COLLECTION == live, (
        f"STALE Qdrant collection reference in the test-cleanup: "
        f"_debris_patterns.QDRANT_COLLECTION={_debris_patterns.QDRANT_COLLECTION!r} "
        f"but the server is bound to {live!r}. The cleanup scroll would 404 and silently "
        f"leak test records into the live store. Update QDRANT_COLLECTION (or set "
        f"MEM0_QDRANT_COLLECTION) to match the live binding."
    )
