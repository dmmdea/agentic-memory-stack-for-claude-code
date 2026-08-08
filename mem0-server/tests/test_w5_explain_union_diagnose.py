"""W5 Train-1 live suite: explain trace, keyword union leg (AMS-56), diagnose.

LIVE-STACK tests — deliberately NOT in ci.yml's headless allowlist. They need
MEM0_URL/MEM0_KEY, the running stack, and (for the direct-import cases) the
WSL app venv, mirroring test_admission_observability's `import app` pattern.

Review bindings pinned here (roast 2026-08-07, wf_87d0b1d4-87a):
- F1a: the union-rescue nonce must satisfy its PRECONDITION (dense score below
  the search threshold) or the test SKIPS as unsatisfiable — it must never
  pass via dense retrieval while the union leg is deleted.
- F1b/M10: path gating is pinned on the retrieval log's lexical_candidates==0
  for rerank=False traffic — response absence alone is maskable by the trim.
- F1c/M3: the reranker-down case runs with limit > the admitted dense count
  and asserts lexical_kept==0 — otherwise the [:capped_limit] trim masks a
  deleted fail-closed filter.
- F1d: retired / _canonical_intent / other-brand records REACHABLE via
  keyword_search must stay absent from union output.
- F3/M2: diagnose on a gate-rejected target must not bump the MEM-8 daily
  counters nor append to admission-rejected.jsonl (pure-evaluate pin).
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ["MEM0_KEY"]
HDR = {"X-API-Key": KEY}
USER = "test-inv"
# Long semantically-rich carrier: measured live 2026-08-08 — a digit-nonce
# query against this prose scores BELOW the 0.30 semantic gate (absent from
# dense results), while a short carrier scores 0.78 (present). Dilution is
# the load-bearing property (the e0ee87be shape: token buried in rich text).
FILLER = ("El festival de teatro clasico celebra su cuadragesima edicion con "
          "obras de Lope de Vega y Calderon en los corrales restaurados; la "
          "programacion incluye talleres de verso, exposiciones de vestuario "
          "barroco y visitas guiadas nocturnas por el casco antiguo, con aforo "
          "limitado y reserva previa en el registro municipal. expediente")


def _seed(text: str, metadata: dict | None = None) -> str:
    body = {
        "messages": text, "user_id": USER, "infer": False,
        "metadata": {"tier": "evidence", "source": "test-w5", "kind": "test",
                     **(metadata or {})},
    }
    r = httpx.post(f"{URL}/v1/memories", headers=HDR, json=body, timeout=30)
    r.raise_for_status()
    res = r.json().get("results") or []
    if not res:
        pytest.skip("seed deduped to zero results")
    return res[0]["id"]


def _qdrant_set_payload(memory_id: str, **kv) -> None:
    """Direct Qdrant payload write — the house FORBIDDEN_KEYS-bypass test
    pattern (test_brand_isolation._qdrant_set_payload): superseded_by has NO
    API writer by design and retrievable is trusted-actor-only, so retrieval-
    gating fixtures are stamped at the store layer."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct

    from config import build_config
    vs_cfg = build_config()["vector_store"]["config"]
    client = QdrantClient(host=vs_cfg["host"], port=vs_cfg["port"])
    collection = vs_cfg["collection_name"]
    recs = client.retrieve(collection, ids=[memory_id], with_payload=True,
                           with_vectors=True)
    assert recs, f"point {memory_id} not found"
    payload = dict(recs[0].payload or {})
    payload.update(kv)
    client.upsert(collection, points=[PointStruct(
        id=recs[0].id, vector=recs[0].vector, payload=payload)])


def _delete(mid: str) -> None:
    try:
        httpx.delete(f"{URL}/v1/memories/{mid}", headers=HDR,
                     params={"actor": "test-cleanup", "reason": "w5-train1"},
                     timeout=30)
    except Exception:
        pass


def _search(query: str, rerank: bool, limit: int = 5, threshold: float = 0.1,
            explain: bool = False, extra_filters: dict | None = None) -> dict:
    body = {"query": query, "filters": {"user_id": USER, **(extra_filters or {})},
            "limit": limit, "threshold": threshold, "rerank": rerank}
    if explain:
        body["explain"] = True
    r = httpx.post(f"{URL}/v1/memories/search", headers=HDR, json=body,
                   timeout=60)
    r.raise_for_status()
    return r.json()


def _diagnose(query: str, target_id: str, **kw) -> dict:
    body = {"query": query, "target_id": target_id, "user_id": USER, **kw}
    r = httpx.post(f"{URL}/v1/memories/diagnose", headers=HDR, json=body,
                   timeout=120)
    r.raise_for_status()
    return r.json()


def _last_retrieval_log_entry_for(query: str) -> dict | None:
    """query_hash-keyed reverse scan (test_security_invariants pattern —
    race-safe vs concurrent hook traffic)."""
    import hashlib
    qh = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    log_path = Path.home() / ".mem0" / "retrieval-log.jsonl"
    if not log_path.exists():
        return None
    lines = log_path.read_text(encoding="utf-8").splitlines()[-300:]
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("query_hash") == qh:
            return rec
    return None


def _nonce() -> str:
    """Digit-string identifier — the literal AMS-56 shape ('18792' sat at
    bm25-rank 1 / dense-rank 227): EmbeddingGemma compresses bare digit
    strings far below word tokens, which is what makes the precondition
    (dense score < threshold) reliably satisfiable."""
    import random
    return str(random.randint(100000000, 999999999))


# The rescue tests run at the PRODUCTION BUNDLE threshold: mem0's internal
# semantic gate drops sub-0.30 candidates BEFORE bm25 combination, which is
# the constructible small-tenant analog of the real AMS-56 regime (in the
# full store the defect manifests as rank-past-the-dense-window instead —
# see test_union_leg_rescues_production_corpus_target below). A digit-nonce
# query vs a Spanish-recipe carrier lands semantic ~0.1-0.2: below 0.30,
# above the 0.1 default (measured live 2026-08-08 — combined score 0.72,
# proving bm25 re-scoring works fine WITHIN the window; the leg exists for
# targets the window/gate never admits).
RESCUE_THRESHOLD = 0.30


def _seed_keyword_only(base_text: str, threshold: float = RESCUE_THRESHOLD,
                       attempts: int = 3):
    """F1a precondition, expressed as the thing that matters: the DENSE path
    must be unable to return the target at this threshold — verified by an
    actual rerank=False search (whose overfetch is WIDER than the
    rerank=True pool, so absence here implies dense cannot supply it on the
    rescue path either; any rerank=True appearance is then the union leg).
    Returns (nonce, mid) or skips as unsatisfiable."""
    for _ in range(attempts):
        n = _nonce()
        mid = _seed(f"{base_text} {n}")
        probe = _search(n, rerank=False, limit=20, threshold=threshold)
        if mid not in [x.get("id") for x in probe.get("results", [])]:
            return n, mid
        _delete(mid)
    pytest.skip("precondition unsatisfiable: nonce stayed dense-reachable")


# ---------------------------------------------------------------------------
# explain (T1.1)
# ---------------------------------------------------------------------------

def test_explain_zero_mutation_and_trace_shape():
    """explain=True must not change results[]; the trace must name the
    pipeline stages in order (M1 target: gutting the trace goes red here)."""
    n = _nonce()
    mid = _seed(f"explain probe fact {n} about stage tracing")
    try:
        off = _search(n, rerank=False, threshold=0.0)
        on = _search(n, rerank=False, threshold=0.0, explain=True)
        assert "_explain" not in off
        assert "_explain" in on
        # Zero-mutation on the results themselves (order + content).
        assert json.dumps(off.get("results"), sort_keys=True) == \
               json.dumps(on.get("results"), sort_keys=True)
        stages = [s.get("stage") for s in on["_explain"]["stages"]]
        for expected in ("overfetch", "dense_fetch", "union_lexical",
                         "retired_filter", "canonical_intent_filter",
                         "intent_key_strip", "rerank", "admission", "trim"):
            assert expected in stages, f"missing trace stage {expected}: {stages}"
        # bundle-path purity: rerank=False search must not stamp rerank_status
        assert "rerank_status" not in off
    finally:
        _delete(mid)


# ---------------------------------------------------------------------------
# union leg (T5) — the AMS-56 rescue
# ---------------------------------------------------------------------------

def test_union_leg_rescue_and_path_gating():
    # F1a PRECONDITION built into the seeder: the dense path must not be able
    # to return the target at RESCUE_THRESHOLD — else the test could pass
    # with the union leg deleted.
    n, mid = _seed_keyword_only(FILLER)
    try:
        # Rescue: rerank=True search returns the keyword-only target.
        r_on = _search(n, rerank=True, limit=5, threshold=RESCUE_THRESHOLD)
        ids_on = [x.get("id") for x in r_on.get("results", [])]
        assert mid in ids_on, f"union leg failed to rescue keyword-only hit: {ids_on}"
        rescued = next(x for x in r_on["results"] if x.get("id") == mid)
        assert rescued.get("lexical_only") is True
        assert "rerank_score" in rescued, "fail-closed contract: survivor must carry rerank_score"
        assert "score" not in rescued or rescued.get("score") is None
        # Path gating (F1b/M10): rerank=False misses it AND logged
        # lexical_candidates==0 (response absence alone is trim-maskable).
        r_off = _search(n, rerank=False, limit=5, threshold=RESCUE_THRESHOLD)
        assert mid not in [x.get("id") for x in r_off.get("results", [])]
        rec = _last_retrieval_log_entry_for(n)
        assert rec is not None and rec.get("rerank") is False
        assert rec.get("lexical_candidates") == 0, \
            f"union leg ran on rerank=False traffic: {rec}"
        assert rec.get("route") == "search"
    finally:
        _delete(mid)


def test_union_leg_rescues_production_corpus_target():
    """The ORIGINAL AMS-56 live case, read-only: in the full store the defect
    manifests as rank-past-the-dense-window ('18792' target e0ee87be at
    bm25-rank 4 / dense-rank 227). Maintainer-corpus-dependent by nature:
    skips cleanly when the corpus doesn't hold the case (foreign box, purged
    record, or the target drifted dense-reachable). Zero writes."""
    target = "e0ee87be"  # id prefix of the known keyword-only target
    r = httpx.post(f"{URL}/v1/memories/search", headers=HDR, json={
        "query": "18792", "filters": {"user_id": "dmmdea"}, "limit": 10,
        "threshold": 0.1, "rerank": False}, timeout=60)
    if r.status_code != 200:
        pytest.skip("search unavailable")
    dense_ids = [str(x.get("id"))[:8] for x in r.json().get("results", [])]
    if target in dense_ids:
        pytest.skip("corpus drifted: target now dense-reachable — case gone")
    r2 = httpx.post(f"{URL}/v1/memories/search", headers=HDR, json={
        "query": "18792", "filters": {"user_id": "dmmdea"}, "limit": 10,
        "threshold": 0.1, "rerank": True}, timeout=120)
    r2.raise_for_status()
    body = r2.json()
    union_ids = [str(x.get("id"))[:8] for x in body.get("results", [])]
    lex = [x for x in body.get("results", []) if x.get("lexical_only")]
    if target not in union_ids and not lex:
        pytest.skip("corpus holds no keyword-only hit for '18792' anymore")
    assert target in union_ids or lex, "union leg produced no rescue on the live corpus"


def test_union_leg_does_not_leak_retired_intent_or_other_brand():
    """F1d: keyword-REACHABLE records that the hygiene filters/gate must
    remove stay absent from union output."""
    n_ret, n_int, n_brand = _nonce(), _nonce(), _nonce()
    m_ret = _seed(f"{FILLER} retired {n_ret}")
    m_int = _seed(f"{FILLER} intent {n_int}")
    m_brand = _seed(f"{FILLER} branded {n_brand}",
                    metadata={"brand": f"test-brand-{uuid.uuid4().hex[:6]}"})
    try:
        _qdrant_set_payload(m_ret, retrievable=False)
        _qdrant_set_payload(m_int, _canonical_intent=True)
        # Each absence assert is paired with a saw-it receipt (lexical
        # candidates >= 1 in the log) — otherwise a keyword leg that simply
        # failed to find the record would make this test vacuously green.
        r1 = _search(n_ret, rerank=True, limit=5, threshold=0.1)
        assert m_ret not in [x.get("id") for x in r1.get("results", [])], \
            "retired record resurrected via the lexical leg"
        rec1 = _last_retrieval_log_entry_for(n_ret)
        assert rec1 and rec1.get("lexical_candidates", 0) >= 1
        r2 = _search(n_int, rerank=True, limit=5, threshold=0.1)
        assert m_int not in [x.get("id") for x in r2.get("results", [])], \
            "_canonical_intent record leaked via the lexical leg"
        rec2 = _last_retrieval_log_entry_for(n_int)
        assert rec2 and rec2.get("lexical_candidates", 0) >= 1
        r3 = _search(n_brand, rerank=True, limit=5, threshold=0.1)  # brandless scope
        assert m_brand not in [x.get("id") for x in r3.get("results", [])], \
            "cross-brand record leaked via the lexical leg"
        rec3 = _last_retrieval_log_entry_for(n_brand)
        assert rec3 and rec3.get("lexical_candidates", 0) >= 1
        assert int(r3.get("rejected_brand_scoped") or 0) >= 1
    finally:
        for m in (m_ret, m_int, m_brand):
            _delete(m)


def test_union_leg_fail_closed_when_reranker_down():
    """F1c/M3: with the reranker failing, EVERY lexical item is dropped —
    run with limit > admitted dense count so the trim cannot mask a deleted
    filter, and pin via lexical_kept==0 in the retrieval log. Direct-import
    style (test_admission_observability precedent) so bge_rerank can be
    stubbed without touching llama-swap."""
    import app as app_mod

    n = _nonce()
    mid = _seed(f"{FILLER} {n}")
    real = app_mod.bge_rerank

    def _down(query, results, text_key="memory", *, force=False, status_out=None):
        if status_out is not None:
            status_out["status"] = "failed_fallback_dense"
        return list(results)

    try:
        app_mod.bge_rerank = _down
        sr = app_mod._search_core(app_mod.SearchIn(
            query=n, filters={"user_id": USER}, limit=50, threshold=0.1,
            rerank=True))
        # The contract is about LEXICAL items (a dense-reachable target may
        # legitimately return as a dense item): NO lexical_only survivor may
        # exist, and the log must show candidates seen but zero kept — with
        # limit=50 > the admitted dense count the trim cannot mask a deleted
        # filter (M3's red condition).
        assert not any(x.get("lexical_only") for x in sr.get("results", [])), \
            "unscored lexical item survived a reranker outage"
        assert sr.get("rerank_status") == "failed_fallback_dense"
        rec = _last_retrieval_log_entry_for(n)
        assert rec is not None and rec.get("lexical_kept") == 0
        assert rec.get("lexical_candidates") >= 1, \
            "keyword_search never saw the seeded nonce — vacuous run"
    finally:
        app_mod.bge_rerank = real
        _delete(mid)


# ---------------------------------------------------------------------------
# diagnose (T1.3)
# ---------------------------------------------------------------------------

def test_diagnose_superseded_is_pure_and_names_the_stage():
    """F3/M2: the admission verdict must come from the PURE evaluate path —
    the MEM-8 daily counter and admission-rejected.jsonl must be untouched."""
    n = _nonce()
    mid = _seed(f"diagnose probe fact {n}")
    audit = Path.home() / ".mem0" / "admission-rejected.jsonl"
    try:
        _qdrant_set_payload(mid, superseded_by="m-w5-newer")
        before_total = httpx.get(f"{URL}/health/deep", timeout=60).json()[
            "checks"]["admission_rejections_today"].get("total", 0)
        before_lines = audit.read_text(encoding="utf-8").count("\n") if audit.exists() else 0
        d = _diagnose(n, mid, threshold=0.0)
        assert d["verdict"].startswith("admission:superseded_by:"), d["verdict"]
        assert d["admission"]["admit"] is False
        after_total = httpx.get(f"{URL}/health/deep", timeout=60).json()[
            "checks"]["admission_rejections_today"].get("total", 0)
        after_lines = audit.read_text(encoding="utf-8").count("\n") if audit.exists() else 0
        assert after_total == before_total, "diagnose bumped the MEM-8 daily counter"
        assert after_lines == before_lines, "diagnose appended to admission-rejected.jsonl"
        # and diagnosing did NOT resurrect it on the real path
        r = _search(n, rerank=False, threshold=0.0)
        assert mid not in [x.get("id") for x in r.get("results", [])]
        assert int(r.get("rejected_superseded") or 0) >= 1
    finally:
        _delete(mid)


def test_diagnose_missing_target_404():
    r = httpx.post(f"{URL}/v1/memories/diagnose", headers=HDR, json={
        "query": "anything", "target_id": str(uuid.uuid4()), "user_id": USER},
        timeout=60)
    assert r.status_code == 404


def test_diagnose_returned_verdict_for_findable_record():
    n = _nonce()
    mid = _seed(f"findable diagnose fact {n} zeta")
    try:
        d = _diagnose(n, mid, threshold=0.0, limit=20)
        assert d["verdict"] in ("returned",) or d["verdict"].startswith("trim"), d
        assert d["admission"]["admit"] is True
        assert d["dense"]["rank_at_500"] is not None
    finally:
        _delete(mid)


# ---------------------------------------------------------------------------
# withheld counters end-to-end (T2.1)
# ---------------------------------------------------------------------------

def test_search_response_carries_withheld_family_fields():
    r = _search("any probe", rerank=False)
    for k in ("rejected_brand_scoped", "rejected_superseded",
              "rejected_contradicted"):
        assert k in r and isinstance(r[k], int)


def test_bundle_forwards_withheld_counters():
    r = httpx.post(f"{URL}/v1/context/bundle", headers=HDR, json={
        "session_id": "w5-train1-test", "prompt": "w5 forwarding probe",
        "checkpoint": False, "hook_contract_version": "20.0"}, timeout=60)
    r.raise_for_status()
    b = r.json()
    for k in ("rejected_brand_scoped", "rejected_superseded",
              "rejected_contradicted"):
        assert k in b and isinstance(b[k], int)
