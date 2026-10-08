"""Hybrid fusion (mem0-server/fusion.py): pure-function contract tests, no mem0/Qdrant import.

fusion.ams_score_and_rank replaces mem0.memory.main.score_and_rank with weighted reciprocal rank
fusion over the dense pool. These pin what its callers rely on: the raw-cosine gate before fusion,
a score in (0, 1] that is monotone with the order, an order that depends on ranks only (so a
compressed cosine space ranks the same), keyword evidence that lifts a near-top candidate without
burying the dense leader, top ranks spaced wider than a freshness weight of 0.8, the "mem0" rollback
mode, and a binding self-check that reads False when mem0 stops calling the global."""
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
def _default_mode(monkeypatch):
    monkeypatch.delenv("MEM0_FUSION", raising=False)


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


def test_without_keyword_or_entity_evidence_the_order_is_the_cosine_order():
    out = fusion.ams_score_and_rank(_pool(0.58, 0.71, 0.64), {}, {}, threshold=0.0, top_k=10)
    assert _ids(out) == ["m1", "m2", "m0"]
    scores = [r["score"] for r in out]
    assert scores == sorted(scores, reverse=True) and len(set(scores)) == 3


def test_scores_are_in_zero_one_and_one_only_for_rank_one_everywhere():
    out = fusion.ams_score_and_rank(_pool(0.9, 0.8, 0.7), {"m0": 0.5}, {"m0": 0.2}, threshold=0.0, top_k=10)
    assert out[0]["id"] == "m0" and out[0]["score"] == pytest.approx(1.0)
    assert all(0.0 < r["score"] <= 1.0 for r in out)


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
    out = fusion.ams_score_and_rank(_pool(0.6, 0.5), None, None, threshold=None, top_k=5)
    assert _ids(out) == ["m0", "m1"]                          # None threshold -> mem0's 0.1 guard


def test_top_k_cuts_after_ranking():
    out = fusion.ams_score_and_rank(_pool(0.5, 0.7, 0.6, 0.65), {"m0": 0.9}, {}, threshold=0.0, top_k=2)
    assert len(out) == 2


def test_explain_adds_score_details_and_the_legs_keep_the_raw_cosine():
    out = fusion.ams_score_and_rank(_pool(0.7, 0.6), {"m1": 0.8}, {}, threshold=0.0, top_k=5, explain=True)
    d = {r["id"]: r["score_details"] for r in out}
    assert d["m1"]["semantic_score"] == 0.6 and d["m1"]["rank_bm25"] == 1 and d["m1"]["fusion"] == "rrf"
    assert fusion.last_legs()["m1"]["cosine"] == 0.6 and fusion.last_legs()["m0"]["rank_bm25"] is None


def test_the_mem0_mode_delegates_to_mem0s_own_formula(monkeypatch):
    calls = []

    def original(semantic_results, bm25_scores, entity_boosts, threshold, top_k, **kw):
        calls.append((len(semantic_results), threshold, top_k, kw))
        return [{"id": "orig", "score": 1.0, "payload": {}}]

    monkeypatch.setattr(fusion, "_MEM0_ORIGINAL", original)
    monkeypatch.setenv("MEM0_FUSION", "mem0")
    assert fusion.ams_score_and_rank(_pool(0.6), {}, {}, threshold=0.2, top_k=4)[0]["id"] == "orig"
    assert calls == [(1, 0.2, 4, {})]
    assert fusion.last_legs() is None
    monkeypatch.setenv("MEM0_FUSION", "nonsense")             # an unknown mode is the default, not a crash
    assert fusion.mode() == fusion.DEFAULT_MODE


def _fake_main(calls_global: bool):
    mod = types.ModuleType("fake_mem0_main")

    def score_and_rank(**kw):
        return []

    mod.score_and_rank = score_and_rank
    if calls_global:
        src = ("def _search_vector_store(self, q):\n"
               "    return score_and_rank(semantic_results=[], bm25_scores={}, entity_boosts={}, threshold=0, top_k=1)\n")
    else:
        src = "def _search_vector_store(self, q):\n    return []\n"
    ns = {}
    exec(src, mod.__dict__, ns)
    mod.Memory = type("Memory", (), {"_search_vector_store": ns["_search_vector_store"]})
    return mod, score_and_rank


def test_install_binds_the_global_mem0s_search_calls():
    mod, original = _fake_main(calls_global=True)
    st = fusion.install(mod)
    assert st["bound"] is True and mod.score_and_rank is fusion.ams_score_and_rank
    assert fusion._MEM0_ORIGINAL is original
    assert fusion.install(mod)["bound"] is True              # idempotent: never wraps itself


def test_install_reads_unbound_when_mem0_no_longer_calls_the_global():
    mod, _ = _fake_main(calls_global=False)
    assert fusion.install(mod)["bound"] is False


def test_install_reports_a_missing_scoring_function_instead_of_raising():
    st = fusion.install(types.ModuleType("empty"))
    assert st["bound"] is False and "no score_and_rank" in st["error"]


def test_the_server_binds_the_fusion_at_start_and_deep_health_fails_when_it_is_unbound():
    # app.py cannot be imported headless (it builds the live Memory client), so pin the wiring in source
    src = (pathlib.Path(fusion.__file__).resolve().parent / "app.py").read_text(encoding="utf-8")
    bind = src.index("FUSION_STATUS = _fusion.install()")
    assert src.index("mem.embedding_model = build_embedder()") < bind      # mem0 is imported by then
    check = src.index('out["checks"]["fusion"]')
    assert 'if not FUSION_STATUS.get("bound"):' in src[check:check + 300]
    assert 'out["ok"] = False' in src[check:check + 300]
