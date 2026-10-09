"""Hybrid fusion (mem0-server/fusion.py): pure-function contract tests, no mem0/Qdrant import.

fusion.ams_score_and_rank replaces mem0.memory.main.score_and_rank with weighted reciprocal rank
fusion over the dense pool. These pin what its callers rely on: the raw-cosine gate before fusion,
a score in (0, 1] that orders the fusion's output, an order that depends on ranks only (so a
compressed cosine space ranks the same), keyword evidence that lifts a near-top candidate without
burying the dense leader, top ranks spaced wider than a freshness weight of 0.8, the "mem0" rollback
mode (which still records the raw cosine), a binding self-check that reads False when mem0 stops
loading the global, the per-search bypass count and the health verdict built from them."""
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import fusion  # noqa: E402

K, WB, WE = fusion.RRF_K, fusion.RRF_W_BM25, fusion.RRF_W_ENTITY
NORM = (1 + WB + WE) / (K + 1)


def _pool(*cosines):
    return [{"id": f"m{i}", "score": c, "payload": {"data": f"text {i}"}} for i, c in enumerate(cosines)]


def _ids(out):
    return [r["id"] for r in out]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("MEM0_FUSION", raising=False)
    # install() and end_search() write module state; every test starts from (and leaves) a clean one
    monkeypatch.setattr(fusion, "_MEM0_ORIGINAL", None)
    monkeypatch.setattr(fusion, "SEARCHES", {"reached": 0, "bypassed": 0})
    fusion.begin_search()


def test_the_shipped_constants():
    # the lab's chosen point (docs/systems/fusion.md); a change here is a ranking change: re-run the lab
    # (1.35.0: retuned for EmbeddingGemma-2 alone; 1.34.0 shipped 2.0 / 0.4 for EmbeddingGemma-300m)
    assert (fusion.RRF_K, fusion.RRF_W_BM25, fusion.RRF_W_ENTITY) == (1.0, 0.5, 0.25)


def test_the_threshold_gates_the_raw_cosine_before_any_fusion():
    pool = _pool(0.62, 0.29, 0.55)
    out = fusion.ams_score_and_rank(pool, {"m1": 1.0}, {"m1": 0.5}, threshold=0.30, top_k=10)
    assert _ids(out) == ["m0", "m2"]                         # m1's maximal evidence cannot lift it in
    assert "m1" in _ids(fusion.ams_score_and_rank(pool, {}, {}, threshold=0.29, top_k=10))


def test_the_formula_on_a_small_pool():
    pool = _pool(0.70, 0.65, 0.60)                            # dense ranks m0=1, m1=2, m2=3
    out = {r["id"]: r["score"] for r in fusion.ams_score_and_rank(
        pool, {"m2": 0.9, "m1": 0.4}, {"m0": 0.3}, threshold=0.0, top_k=10)}
    assert out["m0"] == pytest.approx((1 / (K + 1) + WE / (K + 1)) / NORM)
    assert out["m1"] == pytest.approx((1 / (K + 2) + WB / (K + 2)) / NORM)
    assert out["m2"] == pytest.approx((1 / (K + 3) + WB / (K + 1)) / NORM)
    # the same three numbers as literals, so the weights cannot drift with the formula
    assert (out["m0"], out["m1"], out["m2"]) == pytest.approx((0.714286, 0.571429, 0.571429), abs=1e-6)


def test_without_keyword_or_entity_evidence_the_order_is_the_cosine_order():
    out = fusion.ams_score_and_rank(_pool(0.58, 0.71, 0.64), {}, {}, threshold=0.0, top_k=10)
    assert _ids(out) == ["m1", "m2", "m0"]
    scores = [r["score"] for r in out]
    assert scores == sorted(scores, reverse=True) and len(set(scores)) == 3


def test_equal_fused_scores_keep_the_dense_order():
    # dense rank 1 alone == dense rank 3 + keyword rank 1 (1/2 == 1/4 + 0.5/2), exactly in floats
    for pool in (_pool(0.80, 0.70, 0.60), list(reversed(_pool(0.80, 0.70, 0.60)))):
        out = fusion.ams_score_and_rank(pool, {"m2": 0.9}, {}, threshold=0.0, top_k=10)
        assert out[0]["score"] == out[1]["score"]
        assert _ids(out) == ["m0", "m2", "m1"]


def test_scores_are_in_zero_one_and_one_only_for_rank_one_everywhere():
    out = fusion.ams_score_and_rank(_pool(0.9, 0.8, 0.7), {"m0": 0.5}, {"m0": 0.2}, threshold=0.0, top_k=10)
    assert out[0]["id"] == "m0" and out[0]["score"] == 1.0     # exactly: the raw sum is 1.0000000000000002
    assert all(0.0 < r["score"] <= 1.0 for r in out)
    assert all(r["score"] < 1.0 for r in out[1:])


def test_the_reranker_skips_only_a_head_that_every_leg_ranks_first():
    import reranker
    unanimous = fusion.ams_score_and_rank(_pool(0.9, 0.8, 0.7), {"m0": 0.5}, {"m0": 0.2},
                                          threshold=0.0, top_k=10)
    assert reranker.skip_reason(unanimous) == "confident"
    split = fusion.ams_score_and_rank(_pool(0.9, 0.8, 0.7), {"m1": 0.5}, {"m0": 0.2},
                                      threshold=0.0, top_k=10)
    assert reranker.skip_reason(split) is None


def test_a_keyword_match_lifts_a_near_top_candidate_but_cannot_bury_the_dense_leader():
    pool = _pool(0.80, 0.79, 0.78, 0.77, 0.70, 0.60)
    assert _ids(fusion.ams_score_and_rank(pool, {"m1": 1.0}, {}, threshold=0.0, top_k=10))[0] == "m1"
    out = _ids(fusion.ams_score_and_rank(pool, {"m3": 1.0}, {}, threshold=0.0, top_k=10))
    assert out[0] == "m0"                                     # dense rank 4 + keyword rank 1 stays below


def test_the_order_and_the_scores_depend_on_ranks_only():
    # EmbeddingGemma-2-like: every cosine squeezed into a narrow, high band; same evidence
    wide = _pool(0.80, 0.78, 0.70, 0.62, 0.55)
    narrow = [dict(r, score=0.80 + 0.25 * (r["score"] - 0.55)) for r in wide]
    bm, ent = {"m2": 0.9, "m4": 0.4}, {"m3": 0.5}
    a = [(r["id"], r["score"]) for r in fusion.ams_score_and_rank(wide, bm, ent, threshold=0.0, top_k=10)]
    b = [(r["id"], r["score"]) for r in fusion.ams_score_and_rank(narrow, bm, ent, threshold=0.0, top_k=10)]
    assert a == b


def test_the_top_ranks_are_spaced_wider_than_a_freshness_weight_of_08():
    # durable freshness multiplies evidence-tier scores by 0.77-0.90 and re-sorts; at the shipped k a
    # weight of 0.8 on the dense leader alone must not drop it below the undecayed second
    out = fusion.ams_score_and_rank(_pool(0.9, 0.8), {}, {}, threshold=0.0, top_k=10)
    assert out[1]["score"] / out[0]["score"] < 0.8


def test_an_empty_pool_a_single_candidate_and_none_inputs():
    assert fusion.ams_score_and_rank([], {"x": 1.0}, {}, threshold=0.0, top_k=5) == []
    one = fusion.ams_score_and_rank(_pool(0.5), {"m0": 1.0}, {"m0": 0.5}, threshold=0.0, top_k=5)
    assert one[0]["score"] == pytest.approx(1.0)
    out = fusion.ams_score_and_rank(_pool(0.6, 0.5, 0.05), None, None, threshold=None, top_k=5)
    assert _ids(out) == ["m0", "m1"]                          # None threshold -> mem0's 0.1 guard, not 0


def test_top_k_cuts_after_ranking():
    # dense ranks m1, m3, m2, m0; m0's keyword rank 1 lifts it to second, so a cut before ranking
    # (the cosine top 2: m1, m3) would return a different pair
    out = fusion.ams_score_and_rank(_pool(0.5, 0.7, 0.6, 0.65), {"m0": 0.9}, {}, threshold=0.0, top_k=2)
    assert _ids(out) == ["m1", "m0"]


def test_explain_adds_score_details_and_the_legs_keep_the_raw_cosine():
    out = fusion.ams_score_and_rank(_pool(0.7, 0.6), {"m1": 0.8}, {}, threshold=0.0, top_k=5, explain=True)
    d = {r["id"]: r["score_details"] for r in out}
    assert d["m1"]["semantic_score"] == 0.6 and d["m1"]["rank_bm25"] == 1 and d["m1"]["fusion"] == "rrf"
    assert fusion.last_legs()["m1"]["cosine"] == 0.6 and fusion.last_legs()["m0"]["rank_bm25"] is None


def test_the_mem0_mode_delegates_to_mem0s_own_formula_and_still_records_the_cosine(monkeypatch):
    calls = []

    def original(semantic_results, bm25_scores, entity_boosts, threshold, top_k, **kw):
        calls.append((len(semantic_results), threshold, top_k, kw))
        return [{"id": "orig", "score": 1.0, "payload": {}}]

    monkeypatch.setattr(fusion, "_MEM0_ORIGINAL", original)
    monkeypatch.setenv("MEM0_FUSION", "mem0")
    assert fusion.ams_score_and_rank(_pool(0.6), {}, {}, threshold=0.2, top_k=4)[0]["id"] == "orig"
    assert calls == [(1, 0.2, 4, {})]
    assert fusion.last_legs() == {"m0": {"cosine": 0.6}}     # diagnose and the cosine floor read it
    monkeypatch.setenv("MEM0_FUSION", "nonsense")             # an unknown mode is the default, not a crash
    assert fusion.mode() == fusion.DEFAULT_MODE


# --- the server's search hooks: begin_search / end_search ---

def _response(*ids):
    return {"results": [{"id": i, "memory": f"text {i}", "score": 0.5} for i in ids]}


def test_end_search_stamps_the_raw_cosine_and_counts_a_search_that_reached_the_fusion():
    fusion.begin_search()
    fusion.ams_score_and_rank(_pool(0.7, 0.6), {}, {}, threshold=0.0, top_k=5)
    resp = _response("m1", "m0", "lexical-only")
    fusion.end_search(resp)
    assert [r.get("cosine") for r in resp["results"]] == [0.6, 0.7, None]
    assert fusion.SEARCHES == {"reached": 1, "bypassed": 0}


def test_results_without_a_fusion_call_count_as_a_bypass_and_get_no_cosine():
    fusion.ams_score_and_rank(_pool(0.7), {}, {}, threshold=0.0, top_k=5)   # an earlier search's legs
    fusion.begin_search()                                                    # ...cleared for this one
    resp = _response("m0")
    fusion.end_search(resp)
    assert "cosine" not in resp["results"][0]
    assert fusion.SEARCHES == {"reached": 0, "bypassed": 1}
    fusion.end_search({"results": []})                                       # nothing returned: no verdict
    fusion.end_search(None)
    assert fusion.SEARCHES == {"reached": 0, "bypassed": 1}


def test_the_mem0_mode_stamps_the_cosine_too(monkeypatch):
    monkeypatch.setattr(fusion, "_MEM0_ORIGINAL", lambda **kw: [{"id": "m0", "score": 0.9, "payload": {}}])
    monkeypatch.setenv("MEM0_FUSION", "mem0")
    fusion.begin_search()
    fusion.ams_score_and_rank(_pool(0.42), {}, {}, threshold=0.0, top_k=5)
    resp = _response("m0")
    fusion.end_search(resp)
    assert resp["results"][0]["cosine"] == 0.42 and fusion.SEARCHES["reached"] == 1


# --- health: the verdict /health/deep folds into ok ---

def test_health_is_ok_only_when_bound_and_never_bypassed(monkeypatch):
    bound = {"mode": "rrf", "bound": True, "callers_bound": [True, True]}
    h = fusion.health(bound)
    assert h["ok"] is True and h["searches"] == {"reached": 0, "bypassed": 0} and h["callers_bound"] == [True, True]
    assert fusion.health({"mode": "rrf", "bound": False, "error": "x"})["ok"] is False
    fusion.SEARCHES["bypassed"] = 1
    assert fusion.health(bound)["ok"] is False


def test_the_mem0_mode_is_healthy_without_a_binding(monkeypatch):
    # the documented way to run a mem0 the fusion cannot bind to, without failing every deploy gate
    monkeypatch.setenv("MEM0_FUSION", "mem0")
    h = fusion.health({"mode": "rrf", "bound": False, "error": "moved"})
    assert h["ok"] is True and h["mode"] == "mem0" and h["error"] == "moved"


# --- install: the binding self-check ---

def _fake_main(body: str, sig: str = "semantic_results, bm25_scores, entity_boosts, threshold, top_k",
               other_module: bool = False):
    """A stand-in for mem0.memory.main whose Memory._search_vector_store runs `body`."""
    mod = types.ModuleType("fake_mem0_main")
    ns = {}
    exec(f"def score_and_rank({sig}):\n    return []\n", mod.__dict__)
    mod.scoring = types.SimpleNamespace(score_and_rank=mod.score_and_rank)
    src = f"def _search_vector_store(self, q):\n    return {body}\n"
    exec(src, {} if other_module else mod.__dict__, ns)
    mod.Memory = type("Memory", (), {"_search_vector_store": ns["_search_vector_store"]})
    return mod, mod.score_and_rank


GLOBAL_CALL = "score_and_rank(semantic_results=[], bm25_scores={}, entity_boosts={}, threshold=0, top_k=1)"


def test_install_binds_the_global_mem0s_search_loads():
    mod, original = _fake_main(GLOBAL_CALL)
    st = fusion.install(mod)
    assert st["bound"] is True and mod.score_and_rank is fusion.ams_score_and_rank
    assert fusion._MEM0_ORIGINAL is original
    again = fusion.install(mod)                               # idempotent: never wraps itself
    assert again["bound"] is True and fusion._MEM0_ORIGINAL is original


def test_install_reads_unbound_when_mem0_no_longer_calls_the_global():
    mod, _ = _fake_main("[]")
    assert fusion.install(mod)["bound"] is False


def test_an_attribute_call_of_the_same_name_is_not_the_global():
    # scoring.score_and_rank(...) with the old import left behind: co_names holds the name, but the
    # global is never loaded, so the swap would not be reached
    mod, _ = _fake_main(GLOBAL_CALL.replace("score_and_rank(", "scoring.score_and_rank(", 1))
    st = fusion.install(mod)
    assert "score_and_rank" in mod.Memory._search_vector_store.__code__.co_names
    assert st["bound"] is False


def test_a_search_defined_in_another_module_is_not_bound():
    # the implementation moved and main re-exports it: its globals are not the patched namespace
    mod, _ = _fake_main(GLOBAL_CALL, other_module=True)
    assert fusion.install(mod)["bound"] is False


def test_a_scoring_function_with_a_parameter_the_fusion_does_not_take_is_left_alone():
    sig = "semantic_results, bm25_scores, entity_boosts, threshold, top_k, explain=False, boost=None"
    mod, original = _fake_main(GLOBAL_CALL, sig=sig)
    st = fusion.install(mod)
    assert st["bound"] is False and "boost" in st["error"]
    assert mod.score_and_rank is original                     # mem0 keeps its own formula, loudly


def test_install_reports_a_missing_scoring_function_instead_of_raising():
    st = fusion.install(types.ModuleType("empty"))
    assert st["bound"] is False and "no score_and_rank" in st["error"]


# --- the server wiring (app.py cannot be imported headless: it builds the live Memory client) ---

def _app_source():
    return (pathlib.Path(fusion.__file__).resolve().parent / "app.py").read_text(encoding="utf-8")


def test_the_server_binds_the_fusion_at_start_and_deep_health_folds_its_verdict():
    src = _app_source()
    bind = src.index("FUSION_STATUS = _fusion.install()")
    assert src.index("mem.embedding_model = build_embedder()") < bind      # mem0 is imported by then
    assert ('    out["checks"]["fusion"] = fusion_check = _fusion.health(FUSION_STATUS)\n'
            '    if not fusion_check["ok"]:\n'
            '        out["ok"] = False\n') in src


def test_every_server_search_is_bracketed_by_the_fusion_hooks():
    src = _app_source()
    assert src.count("mem.search(") == 2                      # the search core and diagnose
    # 1.35.0: inside the try that scopes a media search's query vector (QUERY_MEDIA) to this search
    assert ("        _fusion.begin_search()\n"
            "        results = mem.search(\n") in src
    assert ("        )\n"
            "        _fusion.end_search(results)\n") in src
    assert ("        _fusion.begin_search()\n"
            "        probe = mem.search(") in src
    assert "        _fusion.end_search(probe)\n" in src


def test_the_constants_are_the_active_spaces():
    """1.35.0: measured per model. The module defaults are the default space's; the server sets the active
    space's before it binds the fusion, so a box still on EmbeddingGemma-300m keeps the 1.34.0 constants."""
    import embedder_profile as ep
    assert ep.get(ep.DEFAULT_PROFILE).fusion == (fusion.RRF_K, fusion.RRF_W_BM25, fusion.RRF_W_ENTITY)
    assert ep.get("egemma-300m").fusion == (2.0, 0.4, 0.25)
    src = _app_source()
    assert src.index("_fusion.configure(*EMBED_PROFILE.fusion)") < src.index("FUSION_STATUS = _fusion.install()")
    saved = (fusion.RRF_K, fusion.RRF_W_BM25, fusion.RRF_W_ENTITY)
    try:
        fusion.configure(*ep.get("egemma-300m").fusion)
        assert (fusion.RRF_K, fusion.RRF_W_BM25) == (2.0, 0.4)
        assert fusion.health({"bound": True})["constants"] == {"k": 2.0, "w_bm25": 0.4, "w_entity": 0.25}
        with pytest.raises(ValueError):
            fusion.configure(0, 0.4, 0.25)
    finally:
        fusion.configure(*saved)
