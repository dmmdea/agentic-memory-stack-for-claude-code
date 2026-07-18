"""v0.29 R4 — unit tests for app._episode_raw_fallback (the SEMANTIC fallback).

The helper embeds the prompt + semantic-searches episodes_egemma_768. These tests
monkeypatch app.search_episodes_semantic to return controlled (id, cosine, payload)
hits, isolating the helper's arg-passing / snippet / empty / fail-soft logic. The
fail-closed brand gate + the live semantic round-trip are covered in
test_episode_embeddings.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402  (heavy import; mem0 init runs once)


@pytest.fixture()
def patched(monkeypatch):
    calls = {}

    def fake_search(client, embedder, query, brand, only_brand_neutral=False, limit=20, floor=0.0):
        calls["args"] = dict(query=query, brand=brand, only_brand_neutral=only_brand_neutral,
                             limit=limit, floor=floor)
        return calls.get("ret", [])

    monkeypatch.setattr(app, "search_episodes_semantic", fake_search)
    monkeypatch.setattr(app, "RAW_FALLBACK_SNIPPET_CHARS", 300)
    monkeypatch.setattr(app, "RAW_FALLBACK_COSINE_FLOOR", 0.20)
    monkeypatch.setattr(app, "RAW_FALLBACK_TOPK", 10)
    return calls


def test_surfaces_top_hit(patched):
    patched["ret"] = [
        (42, 0.55, {"brand": None, "goal": "Fix the deploy pipeline", "summary": "Patched the missing env var."}),
        (9, 0.41, {"brand": None, "goal": "other", "summary": "other"}),
    ]
    rf = app._episode_raw_fallback("how do I fix the deploy pipeline", brand="ai-ecosystem")
    assert rf == {"episode_id": 42, "brand": None,
                  "snippet": "Fix the deploy pipeline — Patched the missing env var."}
    # fail-closed args: a known brand -> only_brand_neutral False; floor + topk from config
    assert patched["args"]["brand"] == "ai-ecosystem"
    assert patched["args"]["only_brand_neutral"] is False
    assert patched["args"]["floor"] == 0.20
    assert patched["args"]["limit"] == 10


def test_unknown_brand_sets_only_brand_neutral(patched):
    patched["ret"] = [(1, 0.5, {"brand": None, "goal": "g", "summary": "s"})]
    app._episode_raw_fallback("a relevant prompt", brand=None)
    assert patched["args"]["only_brand_neutral"] is True


def test_whitespace_brand_fails_closed(patched):
    """Audit MED: a whitespace-only session brand must collapse to unknown
    (only_brand_neutral=True) BEFORE the gate, else `not '  '` is False and the
    gate falls through to admit-all (cross-brand leak)."""
    patched["ret"] = [(1, 0.5, {"brand": None, "goal": "g", "summary": "s"})]
    app._episode_raw_fallback("a relevant prompt", brand="   ")
    assert patched["args"]["only_brand_neutral"] is True
    assert not (patched["args"]["brand"] or "").strip()  # normalized to falsy


def test_empty_prompt_no_search(patched):
    assert app._episode_raw_fallback("", brand=None) is None
    assert app._episode_raw_fallback("   ", brand=None) is None
    assert app._episode_raw_fallback(None, brand=None) is None
    assert "args" not in patched  # search never invoked on an empty prompt


def test_no_hits_returns_none(patched):
    patched["ret"] = []
    assert app._episode_raw_fallback("prompt", brand=None) is None


def test_fail_soft_on_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("qdrant down")
    monkeypatch.setattr(app, "search_episodes_semantic", boom)
    assert app._episode_raw_fallback("prompt", brand=None) is None


def test_snippet_truncation(patched):
    patched["ret"] = [(1, 0.5, {"brand": None, "goal": "A very long goal text indeed",
                                "summary": "and a long summary that keeps going"})]
    import app as _app
    _app.RAW_FALLBACK_SNIPPET_CHARS = 20
    rf = app._episode_raw_fallback("prompt", brand=None)
    assert len(rf["snippet"]) <= 20


def test_goal_or_summary_only(patched):
    patched["ret"] = [(7, 0.5, {"brand": "ai-ecosystem", "goal": "", "summary": "summary only"})]
    rf = app._episode_raw_fallback("p", brand="ai-ecosystem")
    assert rf["snippet"] == "summary only"
    assert rf["brand"] == "ai-ecosystem"
    assert rf["episode_id"] == 7
