"""embedder_profile: one definition of the embedding space, resolved the same way everywhere.

The load-bearing claims:
  * with nothing configured, the server stays in the space existing stores were built in
    (EmbeddingGemma-300m, mem0_egemma_768), so a code upgrade never moves a store by itself;
  * a profile switch moves the model AND every collection together, and the legacy unscoped
    MEM0_EMBED_MODEL (an EmbeddingGemma-300m file name on the native authority) cannot follow
    the switch into the new space;
  * an unknown profile name fails loud instead of falling back to a space the store was not
    built in;
  * the shim reads its prefixes and its token budget from the profile, so EmbeddingGemma-2's
    8,192-token window is actually used where a box serves its long alias;
  * a fresh install records the default (EmbeddingGemma-2 since 1.35.0); only an installer applies it.
"""
import pytest

import embedder_profile as ep

_KEYS = ("MEM0_EMBED_PROFILE", "MEM0_EMBED_MODEL", "MEM0_EMBED_MODEL_EGEMMA2", "MEM0_EMBED_MODEL_EGEMMA_300M",
         "MEM0_EMBED_LONG_MODEL_EGEMMA2", "MEM0_WIKI_EMBED_PROFILE",
         "MEM0_QDRANT_COLLECTION", "MEM0_COLLECTION", "MEM0_EPISODES_COLLECTION", "MEM0_WIKI_COLLECTION",
         "MEM0_EMBED_BASE_URL", "MEM0_MEDIA_EMBEDDER")


@pytest.fixture
def clean(isolated_home, monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    (isolated_home / ".mem0").mkdir()
    return isolated_home


def _stack_env(home, **kv):
    (home / ".mem0" / "stack.env").write_text("".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")


def test_the_default_is_what_a_fresh_install_records_and_never_a_fallback(clean):
    """1.35.0: EmbeddingGemma-2 (multimodal) is the default a fresh install records; a box that records
    nothing is the legacy space, so the default's change cannot rebind a store."""
    assert ep.DEFAULT_PROFILE == "egemma2" and ep.get(ep.DEFAULT_PROFILE).media is True
    assert ep.LEGACY_PROFILE == "egemma-300m" and ep.get(ep.LEGACY_PROFILE).media is False
    assert ep.profile_name() == ep.LEGACY_PROFILE != ep.DEFAULT_PROFILE


def test_default_is_the_existing_space(clean):
    p = ep.active()
    assert p.name == "egemma-300m"
    assert ep.collection("memories") == "mem0_egemma_768"
    assert ep.collection("entities") == "mem0_egemma_768_entities"
    assert ep.collection("episodes") == "episodes_egemma_768"
    assert ep.collection("wiki") == "wiki_pages_egemma_768"
    assert ep.embed_model() == "embeddinggemma"
    assert ep.base_url() == "http://localhost:11436/v1"


def test_profile_switch_moves_model_and_every_collection(clean, monkeypatch):
    monkeypatch.setenv("MEM0_EMBED_PROFILE", "egemma2")
    d = ep.describe()
    assert d["profile"] == "egemma2"
    assert d["model"] == "embeddinggemma2"
    assert d["ctx_tokens"] == 2048 and d["media"] is True
    # no long alias by default: whole pages go through the hot alias at the hot budget
    assert (d["long_model"], d["long_token_budget"]) == ("embeddinggemma2", 1900)
    assert d["collections"] == {"memories": "mem0_eg2_768", "entities": "mem0_eg2_768_entities",
                                "episodes": "episodes_eg2_768"}
    assert d["wiki"] == {"profile": "egemma2", "model": "embeddinggemma2", "collection": "wiki_pages_eg2_768"}


def test_stack_env_selects_the_profile(clean):
    _stack_env(clean, MEM0_EMBED_PROFILE="egemma2", MEM0_EMBED_MODEL_EGEMMA2="embeddinggemma2-ams")
    assert ep.active().name == "egemma2"
    assert ep.embed_model() == "embeddinggemma2-ams"


def test_env_beats_stack_env(clean, monkeypatch):
    _stack_env(clean, MEM0_EMBED_PROFILE="egemma2")
    monkeypatch.setenv("MEM0_EMBED_PROFILE", "egemma-300m")
    assert ep.active().name == "egemma-300m"


def test_legacy_model_override_does_not_follow_a_profile_switch(clean):
    # The native authority's stack.env names its EmbeddingGemma-300m GGUF this way. After a switch
    # to egemma2 it must NOT be used: queries would be embedded by the old model against new vectors.
    _stack_env(clean, MEM0_EMBED_MODEL="embeddinggemma-ams", MEM0_EMBED_PROFILE="egemma2")
    assert ep.embed_model() == "embeddinggemma2"
    assert ep.embed_model(ep.get("egemma-300m")) == "embeddinggemma-ams"


def test_scoped_model_override_applies_to_its_profile_only(clean, monkeypatch):
    monkeypatch.setenv("MEM0_EMBED_MODEL_EGEMMA2", "eg2-local")
    assert ep.embed_model(ep.get("egemma2")) == "eg2-local"
    assert ep.embed_model(ep.get("egemma-300m")) == "embeddinggemma"


def test_collection_overrides(clean, monkeypatch):
    monkeypatch.setenv("MEM0_COLLECTION", "legacy_name")
    assert ep.collection("memories") == "legacy_name"
    assert ep.collection("entities") == "legacy_name_entities"
    monkeypatch.setenv("MEM0_QDRANT_COLLECTION", "preferred")
    assert ep.collection("memories") == "preferred"
    monkeypatch.setenv("MEM0_EPISODES_COLLECTION", "eps")
    monkeypatch.setenv("MEM0_WIKI_COLLECTION", "wk")
    assert ep.collection("episodes") == "eps"
    assert ep.collection("wiki") == "wk"
    with pytest.raises(ValueError):
        ep.collection("bogus")


def test_unknown_profile_fails_loud(clean, monkeypatch):
    monkeypatch.setenv("MEM0_EMBED_PROFILE", "nomic")
    with pytest.raises(SystemExit) as e:
        ep.active()
    assert "not a known embedding profile" in str(e.value)


def test_spaces_never_share_a_collection():
    names = [getattr(p, k) for p in ep.PROFILES.values() for k in ("memories", "episodes", "wiki")]
    assert len(names) == len(set(names))


def test_both_generations_use_the_model_card_prefixes():
    for p in ep.PROFILES.values():
        assert p.query_prefix == "task: search result | query: "
        assert p.doc_prefix == "title: none | text: "


def test_shim_reads_prefixes_and_budget_from_the_profile():
    pytest.importorskip("mem0")  # the shim subclasses mem0's embedder; CI's headless lane has no mem0
    from mem0.configs.embeddings.base import BaseEmbedderConfig
    from egemma_embedder import EmbeddingGemmaEmbedder, budget_for, _truncate_for_embedding
    cfg = BaseEmbedderConfig(model="embeddinggemma2", openai_base_url="http://127.0.0.1:9/v1",
                             api_key="noop", embedding_dims=768)
    eg2 = ep.get("egemma2")
    shim = EmbeddingGemmaEmbedder(cfg, profile=eg2)
    assert shim._prefix("search") == eg2.query_prefix
    assert shim._prefix("add") == eg2.doc_prefix
    assert shim._budget == budget_for(eg2)
    # ~4,000 chars of hex is ~3,600 estimated tokens: cut under the hot-path budget (ubatch 2048, served
    # with the projector), embedded whole under EmbeddingGemma-2's long budget.
    blob = "0123456789abcdef" * 250
    assert len(_truncate_for_embedding(blob, budget_for(eg2))) < len(blob)
    assert _truncate_for_embedding(blob, eg2.long_token_budget) == blob


def test_long_alias_resolution(clean, monkeypatch):
    eg2, eg1 = ep.get("egemma2"), ep.get("egemma-300m")
    assert ep.long_model(eg2) == ("embeddinggemma2", 1900)
    # A profile without a long alias embeds long documents through the hot one, hot budget.
    assert ep.long_model(eg1) == ("embeddinggemma", 1900)
    monkeypatch.setenv("MEM0_EMBED_LONG_MODEL_EGEMMA2", "eg2-long-ams")
    assert ep.long_model(eg2) == ("eg2-long-ams", 7900)
    monkeypatch.setenv("MEM0_EMBED_LONG_MODEL_EGEMMA2", "none")
    assert ep.long_model(eg2) == ("embeddinggemma2", 1900)


def test_thresholds_default_profile_unchanged(clean):
    # The default space keeps every value the stack was calibrated with.
    t = ep.get("egemma-300m").thresholds
    assert (t.relevance_gate, t.episode_floor, t.nli_floor, t.evidence_sim_floor, t.sibling) == (0.30, 0.20, 0.5, 0.45, 0.6)
    assert t.dedup_for("canonical") == 0.97 and t.dedup_for("evidence") == 0.94 and t.dedup_for("unknown") == 0.92
    assert ep.threshold("relevance_gate") == 0.30 and ep.threshold("rerank_skip") == 1.0


def test_the_rerank_skip_is_the_same_in_every_space(clean):
    # it reads the fused score, which is reciprocal rank fusion (fusion.py): rank-based, so no model's
    # cosine scale moves it; 1.0 = every leg ranks the head first
    assert {p.thresholds.rerank_skip for p in ep.PROFILES.values()} == {1.0}


def test_thresholds_follow_the_profile_and_env_knobs(clean, monkeypatch):
    monkeypatch.setenv("MEM0_EMBED_PROFILE", "egemma2")
    eg2 = ep.get("egemma2").thresholds
    assert ep.threshold("relevance_gate") == eg2.relevance_gate > 0.5
    # Dedup DELETES: every EmbeddingGemma-2 tier sits at or above the floor that deleted none of the
    # pairs EmbeddingGemma-300m kept (0.988, lab 2026-10-08).
    assert min(c for _, c in eg2.dedup) >= 0.988 and eg2.dedup_fallback >= 0.988
    monkeypatch.setenv("MEM0_RELEVANCE_THRESHOLD", "0.71")
    monkeypatch.setenv("MEM0_RAW_FALLBACK_COSINE_FLOOR", "0.66")
    assert ep.threshold("relevance_gate") == 0.71 and ep.threshold("episode_floor") == 0.66
    # sibling / dedup are calibrated values, not knobs: no env reaches them
    monkeypatch.setenv("MEM0_SIBLING_THRESHOLD", "0.1")
    assert ep.threshold("sibling") == eg2.sibling


def test_every_profile_carries_thresholds():
    for p in ep.PROFILES.values():
        assert p.thresholds is not None
        assert {t for t, _ in p.thresholds.dedup} == {"canonical", "stable", "evidence", "temporal", "insight"}


def test_wiki_can_live_in_another_space(clean):
    # The shipped split: memories stay on EmbeddingGemma-300m, the wiki moves to EmbeddingGemma-2.
    _stack_env(clean, MEM0_EMBED_MODEL="embeddinggemma-ams", MEM0_WIKI_EMBED_PROFILE="egemma2")
    assert ep.active().name == "egemma-300m"
    assert ep.collection("memories") == "mem0_egemma_768"
    assert ep.collection("episodes") == "episodes_egemma_768"
    assert ep.embed_model() == "embeddinggemma-ams"
    w = ep.wiki_profile()
    assert w.name == "egemma2"
    assert ep.collection("wiki") == "wiki_pages_eg2_768"
    assert ep.embed_model(w) == "embeddinggemma2"       # the legacy 300m override never leaks into it
    assert ep.describe()["wiki"]["collection"] == "wiki_pages_eg2_768"


def test_build_embedder_in_the_wiki_space(clean, monkeypatch):
    pytest.importorskip("mem0")
    import importlib
    monkeypatch.setenv("MEM0_WIKI_EMBED_PROFILE", "egemma2")
    import config
    importlib.reload(config)
    emb = config.build_embedder(long=True, profile=ep.wiki_profile())
    assert emb.profile.name == "egemma2" and emb.config.model == "embeddinggemma2"
    assert emb._budget == 1900 - 16
    hot = config.build_embedder()
    assert hot.profile.name == "egemma-300m" and hot.config.model == "embeddinggemma"


def test_threshold_overrides_are_reported_with_the_value_they_replace(clean, monkeypatch):
    assert ep.threshold_overrides() == {}
    monkeypatch.setenv("MEM0_EMBED_PROFILE", "egemma2")
    monkeypatch.setenv("MEM0_RAW_FALLBACK_COSINE_FLOOR", "0.25")      # a 300m-era value left behind
    ov = ep.threshold_overrides()
    assert ov == {"episode_floor": {"env": "MEM0_RAW_FALLBACK_COSINE_FLOOR", "value": "0.25", "profile_value": 0.68}}
    assert ep.describe()["threshold_overrides"] == ov


@pytest.mark.parametrize("value,expected", [("", True), ("on", True), ("off", False), ("OFF", False), ("0", False),
                                            ("false", False), ("no", False)])
def test_media_enabled_follows_the_profile_and_the_box_knob(clean, monkeypatch, value, expected):
    """1.35.1: a box that serves its embedding alias text-only (no --mmproj; a replica on a small card) records
    MEM0_MEDIA_EMBEDDER=off. The space still has a media embedder; this box just does not run it."""
    if value:
        monkeypatch.setenv("MEM0_MEDIA_EMBEDDER", value)
    assert ep.media_enabled(ep.get("egemma2")) is expected
    assert ep.media_enabled(ep.get("egemma-300m")) is False, "a space without a media embedder never has one"


def test_media_enabled_reads_stack_env_and_the_environment_wins(clean, monkeypatch):
    _stack_env(clean, MEM0_MEDIA_EMBEDDER="off")
    assert ep.media_enabled(ep.get("egemma2")) is False
    assert ep.describe(ep.get("egemma2"))["media"] is False, "/health/deep reports this box's capability"
    monkeypatch.setenv("MEM0_MEDIA_EMBEDDER", "on")
    assert ep.media_enabled(ep.get("egemma2")) is True
