"""W5 ADOPT-5 (AMS-23/24): hard-gated retrieval families — the deploy gate.

LIVE-STACK suite, deliberately NOT in ci.yml's headless allowlist. deploy.sh
runs this file post-restart (step 5b): each family encodes a distinct
failure class, floors are env-overridable (MEM0_FAMILY_FLOOR_<FAMILY>=0
disables one family LOUDLY), and every assert names its breach. Families are
typed test functions rather than a fixture DSL — the auditability of the
assert IS the gate (a data-driven interpreter would be one more place for
vacuity to hide).

Families:
- paraphrase           semantic recall without exact phrasing
- dilution             one strong fact among near-duplicate weak ones
- temporal             current-vs-superseded (admission withholds the old)
- cross_brand          hard-negative: another brand's fact NEVER leaks into a
                       brandless search (positive control included — R10)
- es_exact_token       Spanish query, exact token (the W2 ES-floor receipt's
                       standing enforcement; floor held at 1.0 @ 0.30)
- keyword_only_tail    a dense-unreachable exact-token target MUST be rescued
                       by the union leg (R3: a dead leg fails the deploy —
                       keyword_search fail-opens to None, so leg death is
                       otherwise indistinguishable from nothing-to-do)

Seeding/cleanup per test_brand_isolation conventions (uuid-unique texts,
infer=False, finally-delete, user 'test-inv' — covered by the maintainer
debris backstop). A dedup-swallowed seed SKIPS loudly, never passes silently.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ["MEM0_KEY"]
HDR = {"X-API-Key": KEY}
USER = "test-inv"


def _floor_enabled(family: str) -> bool:
    v = os.environ.get(f"MEM0_FAMILY_FLOOR_{family.upper()}", "1")
    if v == "0":
        print(f"RETRIEVAL-GATE: family {family} DISABLED via env — record why")
        return False
    return True


def _seed(text: str, metadata: dict | None = None) -> str:
    r = httpx.post(f"{URL}/v1/memories", headers=HDR, json={
        "messages": text, "user_id": USER, "infer": False,
        "metadata": {"tier": "evidence", "source": "test-families",
                     "kind": "test", **(metadata or {})}}, timeout=30)
    r.raise_for_status()
    res = r.json().get("results") or []
    if not res:
        pytest.skip(f"LOUD SKIP: seed deduped to zero results ({text[:40]!r}) "
                    "— family not exercised this run")
    return res[0]["id"]


def _delete(mid: str) -> None:
    try:
        httpx.delete(f"{URL}/v1/memories/{mid}", headers=HDR,
                     params={"actor": "test-cleanup", "reason": "families-gate"},
                     timeout=30)
    except Exception:
        pass


def _search(query: str, rerank: bool = True, limit: int = 5,
            threshold: float = 0.1, brand: str | None = None) -> dict:
    filters: dict = {"user_id": USER}
    if brand:
        filters["brand"] = brand
    r = httpx.post(f"{URL}/v1/memories/search", headers=HDR, json={
        "query": query, "filters": filters, "limit": limit,
        "threshold": threshold, "rerank": rerank}, timeout=120)
    r.raise_for_status()
    return r.json()


def _qdrant_set_payload(memory_id: str, **kv) -> None:
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


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def test_family_paraphrase():
    if not _floor_enabled("paraphrase"):
        pytest.skip("family disabled via env")
    t = _tag()
    mid = _seed(f"The nightly backup pipeline {t} snapshots the vector store "
                f"at half past three every morning")
    try:
        r = _search(f"when does the {t} pipeline copy the database each night?")
        ids = [x.get("id") for x in r.get("results", [])]
        assert mid in ids, (
            f"FAMILY BREACH paraphrase: paraphrased query missed the fact "
            f"(returned {len(ids)} results)")
    finally:
        _delete(mid)


def test_family_dilution():
    if not _floor_enabled("dilution"):
        pytest.skip("family disabled via env")
    t = _tag()
    strong = _seed(f"The relay service {t} listens on port 44819 and is the "
                   f"authoritative endpoint")
    weak = [_seed(f"Note {i}: the relay service {t} was discussed in a "
                  f"meeting about future plans, nothing decided") for i in range(3)]
    try:
        r = _search(f"what port does the relay service {t} listen on?")
        top3 = [x.get("id") for x in r.get("results", [])[:3]]
        assert strong in top3, (
            "FAMILY BREACH dilution: the authoritative fact lost to "
            f"near-duplicate weak notes (top3={top3})")
    finally:
        for m in [strong] + weak:
            _delete(m)


def test_family_temporal_supersession():
    if not _floor_enabled("temporal"):
        pytest.skip("family disabled via env")
    t = _tag()
    old = _seed(f"The ingest worker {t} runs on the staging box")
    new = _seed(f"The ingest worker {t} now runs on the production box "
                f"(moved from staging)")
    try:
        _qdrant_set_payload(old, superseded_by=new)
        r = _search(f"where does the ingest worker {t} run?", threshold=0.0)
        ids = [x.get("id") for x in r.get("results", [])]
        assert new in ids, "FAMILY BREACH temporal: current fact missing"
        assert old not in ids, (
            "FAMILY BREACH temporal: superseded fact leaked past admission")
        assert int(r.get("rejected_superseded") or 0) >= 1, (
            "FAMILY BREACH temporal: withheld counter did not fire")
    finally:
        _delete(old)
        _delete(new)


def test_family_cross_brand_hard_negative():
    if not _floor_enabled("cross_brand"):
        pytest.skip("family disabled via env")
    t = _tag()
    brand = f"test-brand-{t}"
    mid = _seed(f"The {t} campaign budget is approved at the June level",
                metadata={"brand": brand})
    try:
        # R10 positive control FIRST: a branded search must find it — else
        # the zero-leak assert below would pass vacuously on a failed seed.
        r_pos = _search(f"what is the {t} campaign budget status?",
                        brand=brand, threshold=0.0)
        assert mid in [x.get("id") for x in r_pos.get("results", [])], (
            "FAMILY BREACH cross_brand (positive control): the branded "
            "search cannot find its own brand's fact")
        # Hard negative: the brandless search must return ZERO of it.
        r_neg = _search(f"what is the {t} campaign budget status?",
                        threshold=0.0)
        assert mid not in [x.get("id") for x in r_neg.get("results", [])], (
            "FAMILY BREACH cross_brand: ANOTHER BRAND'S FACT LEAKED into a "
            "brandless search — brand isolation regression")
    finally:
        _delete(mid)


def test_family_es_exact_token():
    if not _floor_enabled("es_exact_token"):
        pytest.skip("family disabled via env")
    t = str(uuid.uuid4().int)[:9]
    mid = _seed(f"The lab gateway service is registered under id {t} in the "
                f"port directory")
    try:
        # Spanish query, exact token — the W2 calibration held ES recall 1.0
        # at the production 0.30 threshold; this family is that receipt's
        # standing enforcement (OQ#3).
        r = _search(f"cual es el servicio registrado con el id {t}?",
                    threshold=0.1)
        assert mid in [x.get("id") for x in r.get("results", [])], (
            "FAMILY BREACH es_exact_token: Spanish exact-token query missed "
            "the fact (the W2 ES floor regressed)")
    finally:
        _delete(mid)


FILLER = ("El festival de teatro clasico celebra su cuadragesima edicion con "
          "obras de Lope de Vega y Calderon en los corrales restaurados; la "
          "programacion incluye talleres de verso, exposiciones de vestuario "
          "barroco y visitas guiadas nocturnas por el casco antiguo, con aforo "
          "limitado y reserva previa en el registro municipal. expediente")


def test_family_keyword_only_tail():
    """R3: a DEAD union leg is indistinguishable from nothing-to-do
    (keyword_search fail-opens to None) — this family makes the next deploy
    fail on exactly that shape."""
    if not _floor_enabled("keyword_only_tail"):
        pytest.skip("family disabled via env")
    import random
    for _ in range(3):
        n = str(random.randint(100000000, 999999999))
        mid = _seed(f"{FILLER} {n}")
        try:
            probe = _search(n, rerank=False, limit=20, threshold=0.30)
        except Exception:
            _delete(mid)
            raise
        if mid not in [x.get("id") for x in probe.get("results", [])]:
            break
        _delete(mid)
    else:
        pytest.skip("LOUD SKIP: keyword-only precondition unsatisfiable this run")
    try:
        r = _search(n, rerank=True, limit=5, threshold=0.30)
        rescued = [x for x in r.get("results", []) if x.get("id") == mid]
        assert rescued, (
            "FAMILY BREACH keyword_only_tail: the union leg produced no "
            "rescue — the lexical leg is DEAD or the fail-closed drop ate a "
            "scored survivor")
        assert rescued[0].get("lexical_only") is True
        assert "rerank_score" in rescued[0]
    finally:
        _delete(mid)
