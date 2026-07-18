"""v0.29 R4 — tests for episode_embeddings.py (the SEMANTIC raw-trace gate).

Two layers:
  * PURE tests for _brand_admits — the load-bearing fail-closed brand gate
    (byte-for-byte the goals/OQ $brandGate: an unknown-brand session must NEVER
    receive a branded episode). No deps.
  * LIVE integration tests against a TEMP Qdrant collection (:6333) + the real
    EmbeddingGemma embedder — proving embed/upsert/search round-trips and that
    brand isolation holds end-to-end with real vectors.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from episode_embeddings import (  # noqa: E402
    EPISODE_COLLECTION,
    MIN_SUMMARY_CHARS,
    ensure_episode_collection,
    embed_episode_summary,
    upsert_episode_embedding,
    search_episodes_semantic,
    _brand_admits,
    _embed_with_429_retry,
    _indexable_summary,
    _is_rate_limit,
)


# ---------------------------------------------------------------------------
# PURE: MEM-12 (2026-07-03) — one bounded 429 retry on the episode embed path.
# The raw-trace fallback embeds the LIVE prompt right after the bundle search
# saturated the same llama-swap queue — exactly where all 25 RateLimitErrors/7d
# landed. Contract pinned here: ONE retry, ONLY on a duck-typed 429, second
# failure propagates, self-retrying embedders (the production shim) are NOT
# re-wrapped (max attempts stays 2, never 2x2).
# ---------------------------------------------------------------------------

class _FakeRateLimit(Exception):
    """Duck-typed stand-in for openai.RateLimitError (module stays openai-free)."""
    status_code = 429


class _FlakyEmbedder:
    def __init__(self, failures: int, exc: Exception):
        self.failures = failures
        self.exc = exc
        self.calls = 0

    def embed(self, text, memory_action=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return [0.1] * 768


def test_is_rate_limit_duck_typing():
    assert _is_rate_limit(_FakeRateLimit()) is True

    class RateLimitError(Exception):   # name-based recognition (no status_code)
        pass
    assert _is_rate_limit(RateLimitError()) is True
    assert _is_rate_limit(ValueError("500 ctx overflow")) is False


def test_embed_retry_429_then_success(monkeypatch):
    import episode_embeddings as ee
    slept = []
    monkeypatch.setattr(ee.time, "sleep", lambda s: slept.append(s))
    emb = _FlakyEmbedder(failures=1, exc=_FakeRateLimit("saturated"))
    assert _embed_with_429_retry(emb, "prompt text", "search") == [0.1] * 768
    assert emb.calls == 2
    assert len(slept) == 1 and 0.25 <= slept[0] < 0.5


def test_embed_retry_second_429_propagates(monkeypatch):
    import episode_embeddings as ee
    monkeypatch.setattr(ee.time, "sleep", lambda s: None)
    emb = _FlakyEmbedder(failures=99, exc=_FakeRateLimit("still saturated"))
    with pytest.raises(_FakeRateLimit):
        _embed_with_429_retry(emb, "prompt text", "search")
    assert emb.calls == 2, "bounded: never more than one retry"


def test_embed_retry_never_touches_non_429():
    emb = _FlakyEmbedder(failures=99, exc=ValueError("llama-server 500"))
    with pytest.raises(ValueError):
        _embed_with_429_retry(emb, "prompt text", "add")
    assert emb.calls == 1, "non-429 errors are never retried"


def test_embed_retry_skips_self_retrying_shim():
    """The production EmbeddingGemmaEmbedder retries internally
    (handles_429_retry marker) — wrapping it again would allow 2x2 attempts."""
    emb = _FlakyEmbedder(failures=99, exc=_FakeRateLimit("saturated"))
    emb.handles_429_retry = True
    with pytest.raises(_FakeRateLimit):
        _embed_with_429_retry(emb, "prompt text", "add")
    assert emb.calls == 1, "self-retrying embedder must not be double-wrapped"


def test_embed_episode_summary_retries_via_helper(monkeypatch):
    import episode_embeddings as ee
    monkeypatch.setattr(ee.time, "sleep", lambda s: None)
    emb = _FlakyEmbedder(failures=1, exc=_FakeRateLimit("burst"))
    vec = embed_episode_summary(emb, "x" * MIN_SUMMARY_CHARS)
    assert vec == [0.1] * 768 and emb.calls == 2


def test_search_episodes_semantic_retries_query_embed(monkeypatch):
    import episode_embeddings as ee
    monkeypatch.setattr(ee.time, "sleep", lambda s: None)
    emb = _FlakyEmbedder(failures=1, exc=_FakeRateLimit("burst"))

    class _StubQdrant:
        def query_points(self, collection_name, query, limit, with_payload):
            class _R:
                points = []
            return _R()

    out = search_episodes_semantic(_StubQdrant(), emb, "live prompt", brand=None)
    assert out == [] and emb.calls == 2


# ---------------------------------------------------------------------------
# PURE: _indexable_summary — quality gate that keeps degenerate-short summaries
# OUT of the semantic collection (a live check showed ~39-char test-artifact
# summaries get inflated cosine to unrelated queries -> junk fallbacks; real
# episode summaries start at ~76 chars, so a min-length floor cleanly separates).
# ---------------------------------------------------------------------------

def test_indexable_summary_min_length():
    assert _indexable_summary("Session created for resolve smoke test.") is False  # 39 chars (test artifact)
    assert _indexable_summary("x" * MIN_SUMMARY_CHARS) is True
    assert _indexable_summary("x" * (MIN_SUMMARY_CHARS - 1)) is False
    assert _indexable_summary("short") is False
    assert _indexable_summary("") is False
    assert _indexable_summary(None) is False
    assert _indexable_summary("   " + ("y" * MIN_SUMMARY_CHARS) + "   ") is True  # strips before measuring
    assert _indexable_summary("The assistant verified 19 country files, including the key region corrections.") is True


# ---------------------------------------------------------------------------
# PURE: _brand_admits — fail-closed brand gate (mirrors goals/OQ $brandGate)
# ---------------------------------------------------------------------------

def test_brand_admits_known_brand_same_or_neutral():
    """A known session brand admits same-brand + brand-neutral; rejects cross-brand."""
    assert _brand_admits("ai-ecosystem", "ai-ecosystem", False) is True   # same brand
    assert _brand_admits(None, "ai-ecosystem", False) is True             # neutral admitted
    assert _brand_admits("", "ai-ecosystem", False) is True               # empty == neutral
    assert _brand_admits("  ", "ai-ecosystem", False) is True             # whitespace == neutral
    assert _brand_admits("brand-a", "ai-ecosystem", False) is False      # cross-brand rejected


def test_brand_admits_unknown_brand_fail_closed():
    """An unknown-brand session (brand falsy) + only_brand_neutral admits ONLY
    brand-neutral episodes — a branded episode must never leak."""
    assert _brand_admits(None, None, True) is True
    assert _brand_admits("", None, True) is True
    assert _brand_admits("brand-a", None, True) is False                 # branded never leaks
    assert _brand_admits("ai-ecosystem", None, True) is False


def test_brand_admits_unscoped_admits_all():
    """Admin/unscoped (brand falsy, only_brand_neutral False) admits everything."""
    assert _brand_admits("brand-a", None, False) is True
    assert _brand_admits(None, None, False) is True
    assert _brand_admits("ai-ecosystem", "", False) is True               # empty brand == unscoped


# ---------------------------------------------------------------------------
# LIVE: embed / upsert / semantic search against a temp Qdrant collection
# ---------------------------------------------------------------------------

@pytest.fixture()
def live():
    """Temp Qdrant collection + real embedder; torn down after the test."""
    pytest.importorskip("qdrant_client")
    from qdrant_client import QdrantClient
    from config import build_embedder

    client = QdrantClient(host="localhost", port=6333)
    emb = build_embedder()
    coll = f"test_episodes_{uuid.uuid4().hex[:8]}"
    ensure_episode_collection(client, collection=coll)
    try:
        yield client, emb, coll
    finally:
        try:
            client.delete_collection(coll)
        except Exception:
            pass


def test_embed_episode_summary_dims_and_empty(live):
    client, emb, coll = live
    vec = embed_episode_summary(emb, "Deploy the agentic memory stack v0.29")
    assert isinstance(vec, list) and len(vec) == 768
    assert embed_episode_summary(emb, "") is None
    assert embed_episode_summary(emb, "   ") is None
    assert embed_episode_summary(emb, None) is None


def test_search_round_trip_and_floor(live):
    """A relevant query surfaces the episode above a low floor; a high floor
    rejects it (the cosine-floor gate works)."""
    client, emb, coll = live
    vec = embed_episode_summary(emb, "Traced the contradiction-sweep crash to a missing collection name and fixed it")
    upsert_episode_embedding(client, 2001, vec,
                             {"brand": None, "goal": "fix contradiction sweep",
                              "summary": "Traced the contradiction-sweep crash to a missing collection name."},
                             collection=coll)
    hits = search_episodes_semantic(client, emb, "contradiction sweep crash collection",
                                    brand=None, only_brand_neutral=True, limit=5, floor=0.0, collection=coll)
    assert any(h[0] == 2001 for h in hits)
    top = [h for h in hits if h[0] == 2001][0]
    assert 0.0 <= top[1] <= 1.0  # raw cosine in range
    # an impossibly-high floor rejects everything
    none_hits = search_episodes_semantic(client, emb, "contradiction sweep crash collection",
                                         brand=None, only_brand_neutral=True, limit=5, floor=0.999, collection=coll)
    assert none_hits == []


def test_search_brand_isolation_live(live):
    """End-to-end fail-closed: an unknown-brand session never receives the
    branded episode; a same-brand session does."""
    client, emb, coll = live
    v_neutral = embed_episode_summary(emb, "Investigate the deploy pipeline failure and patch the env var")
    v_branded = embed_episode_summary(emb, "Fix the brand-a deploy pipeline regression from a token typo")
    upsert_episode_embedding(client, 3001, v_neutral,
                             {"brand": None, "goal": "deploy", "summary": "neutral deploy episode"}, collection=coll)
    upsert_episode_embedding(client, 3002, v_branded,
                             {"brand": "brand-a", "goal": "deploy", "summary": "brand-a deploy episode"}, collection=coll)

    unknown = search_episodes_semantic(client, emb, "deploy pipeline env var failure",
                                       brand=None, only_brand_neutral=True, limit=10, floor=0.0, collection=coll)
    ids = [h[0] for h in unknown]
    assert 3001 in ids
    assert 3002 not in ids, "branded episode leaked into an unknown-brand session"

    branded = search_episodes_semantic(client, emb, "brand-a deploy pipeline regression token",
                                       brand="brand-a", only_brand_neutral=False, limit=10, floor=0.0, collection=coll)
    ids2 = [h[0] for h in branded]
    assert 3002 in ids2  # same-brand session sees the branded episode
