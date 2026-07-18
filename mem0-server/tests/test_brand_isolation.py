"""v0.19 Phase B (M4): server-boundary brand/scope isolation integration tests.

Runs against the LIVE mem0-server (:18791). Seeds canonical-tier probe records
via the direct-Qdrant-upsert precedent (_qdrant_set_payload mirrors
test_episodic._qdrant_set_tier — the API path is intentionally HMAC-gated for
callers, not for operator-level test tooling).

Property under test (fail-closed brand gate):
  - a brandless query_class='canonical' search admits ONLY null-brand records;
  - the same search WITH a matching filters.brand admits the branded record;
  - a mismatched filters.brand never returns it;
  - filters.allow_cross_brand=True is the explicit opt-in restoring cross-brand
    results on a brandless search (and must be stripped before Qdrant).

All records are created under user_id='test-inv' (in TEST_USER_IDS — the
conftest session-cleanup backstop deletes any new record under it) and deleted
inline in finally.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ["MEM0_KEY"]
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
TEST_USER = "test-inv"


def _qdrant_set_payload(memory_id: str, **kv) -> None:
    """Set payload keys on a memory by writing directly through the Qdrant
    client — mirrors test_episodic._qdrant_set_tier (MED-19 FORBIDDEN_KEYS-bypass
    precedent) but takes arbitrary keys so brand+tier seed in one round trip."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct

    from config import build_config
    vs_cfg = build_config()["vector_store"]["config"]
    client = QdrantClient(host=vs_cfg["host"], port=vs_cfg["port"])
    collection = vs_cfg["collection_name"]

    recs = client.retrieve(collection, ids=[memory_id], with_payload=True, with_vectors=True)
    assert recs, f"point {memory_id} not found in Qdrant collection {collection!r}"
    rec = recs[0]
    payload = dict(rec.payload or {})
    payload.update(kv)
    client.upsert(collection, points=[PointStruct(id=rec.id, vector=rec.vector, payload=payload)])
    check = client.retrieve(collection, ids=[memory_id], with_payload=True)
    assert check and all(check[0].payload.get(k) == v for k, v in kv.items()), (
        f"payload upsert did not persist for {memory_id}: {kv}"
    )


def _seed(text: str, brand: str | None) -> str:
    md = {"tier": "evidence", "source": "test-v019-brandgate", "kind": "test"}
    if brand:
        md["brand"] = brand
    r = httpx.post(
        f"{URL}/v1/memories",
        json={"messages": text, "user_id": TEST_USER, "infer": False, "metadata": md},
        headers=H, timeout=15,
    )
    assert r.status_code == 200, f"seed add failed: {r.text}"
    results = r.json().get("results", [])
    if not results:
        pytest.skip("seed add returned 0 results (mem0 dedup)")
    return results[0]["id"]


def _search(query: str, filters: dict, query_class: str = "canonical") -> set[str]:
    r = httpx.post(
        f"{URL}/v1/memories/search",
        json={"query": query, "filters": filters, "limit": 10,
              "threshold": 0.01, "query_class": query_class},
        headers=H, timeout=15,
    )
    assert r.status_code == 200, f"search failed: {r.status_code}: {r.text}"
    return r.json()


def _cleanup(mid: str) -> None:
    try:
        _qdrant_set_payload(mid, tier="evidence")
    except Exception:
        pass
    httpx.delete(
        f"{URL}/v1/memories/{mid}?actor=test-cleanup&reason=v019+brand+isolation+cleanup",
        headers=H, timeout=10,
    )


def test_brandless_canonical_search_admits_only_null_brand():
    """v0.19 M4 end-to-end: one branded + one null-brand canonical record; a
    brandless query_class='canonical' search must return the null-brand record
    and must NOT return the branded one (fail-closed at the server boundary).
    A matching filters.brand admits the branded record; a mismatched brand and
    no-opt-in brandless search never do; allow_cross_brand=True opts back in."""
    tag = uuid.uuid4().hex[:8]
    unique_kw = f"v019-brandgate-probe-{tag}"
    brand_a = f"test-brand-{tag}"

    branded_id = _seed(f"{unique_kw} branded canonical fact", brand=brand_a)
    null_id = _seed(f"{unique_kw} brand neutral canonical fact", brand=None)

    try:
        _qdrant_set_payload(branded_id, tier="canonical")
        _qdrant_set_payload(null_id, tier="canonical")

        # (a) brandless canonical-class search: null-brand only
        body = _search(unique_kw, {"user_id": TEST_USER})
        ids = {x["id"] for x in body.get("results", [])}
        assert null_id in ids, (
            f"null-brand canonical record {null_id} must be admitted in a "
            f"brandless canonical search; got {ids}"
        )
        assert branded_id not in ids, (
            f"M4 regression: branded canonical record {branded_id} leaked into "
            f"a brandless canonical search (fail-open boundary)"
        )
        for x in body.get("results", []):
            assert not (x.get("metadata") or {}).get("brand"), (
                f"brandless canonical search returned a brand-scoped record: {x.get('id')}"
            )

        # (b) matching brand scope admits the branded record
        body_b = _search(unique_kw, {"user_id": TEST_USER, "brand": brand_a})
        ids_b = {x["id"] for x in body_b.get("results", [])}
        assert branded_id in ids_b, (
            f"branded canonical record {branded_id} must be admitted when the "
            f"request scope carries its brand; got {ids_b}"
        )

        # (c) mismatched brand scope never returns it
        body_c = _search(unique_kw, {"user_id": TEST_USER, "brand": f"test-brand-other-{tag}"})
        ids_c = {x["id"] for x in body_c.get("results", [])}
        assert branded_id not in ids_c, (
            f"branded canonical record {branded_id} returned under a MISMATCHED brand scope"
        )

        # (d) explicit opt-in restores cross-brand results on a brandless search
        # (also proves allow_cross_brand is stripped before Qdrant — else 500)
        body_d = _search(unique_kw, {"user_id": TEST_USER, "allow_cross_brand": True})
        ids_d = {x["id"] for x in body_d.get("results", [])}
        assert branded_id in ids_d and null_id in ids_d, (
            f"allow_cross_brand=True must restore cross-brand admission; got {ids_d}"
        )
    finally:
        _cleanup(branded_id)
        _cleanup(null_id)


def test_branded_search_admits_null_brand_neutral_but_not_other_brand():
    """v1.0 (Phase B / A2 follow-up): a BRANDED search must admit brand-NEUTRAL
    (null-brand) facts - general knowledge relevant to every brand - while STILL
    rejecting OTHER brands. Fixes the branded-recall starvation A2 measured (37.5%):
    the Qdrant pre-filter dropped null-brand candidates before the admission gate
    (which is DESIGNED to admit them - admission_gate.py only rejects a *different*
    brand) ever saw them. The cross-brand isolation boundary is unchanged: a different
    brand stays excluded (the leak guard)."""
    tag = uuid.uuid4().hex[:8]
    kw = f"v1-brandnull-probe-{tag}"
    brand_a = f"test-brand-{tag}"
    brand_other = f"test-brand-other-{tag}"
    a_id = _seed(f"{kw} brand-A scoped fact", brand=brand_a)
    null_id = _seed(f"{kw} brand-neutral general fact", brand=None)
    other_id = _seed(f"{kw} other-brand scoped fact", brand=brand_other)
    try:
        body = _search(kw, {"user_id": TEST_USER, "brand": brand_a}, query_class="durable")
        ids = {x["id"] for x in body.get("results", [])}
        assert a_id in ids, f"brand-A record must be admitted under brand_a scope; got {ids}"
        assert null_id in ids, (
            f"brand-NEUTRAL (null) record {null_id} must be admitted under a branded "
            f"search (the Phase-B fix); got {ids}"
        )
        assert other_id not in ids, (
            f"ISOLATION BREACH: other-brand record {other_id} leaked into a brand_a "
            f"search; got {ids}"
        )
    finally:
        _cleanup(a_id)
        _cleanup(null_id)
        _cleanup(other_id)
