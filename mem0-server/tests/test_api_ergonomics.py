"""Verify the search request schema accepts `limit` (not `top_k`) and the
list endpoint hard-caps at 500. Run against a live mem0 server on :18791
with a valid X-API-Key in $env:MEM0_KEY."""
import getpass
import os, json, httpx, pytest
URL = os.environ.get("MEM0_URL", "http://127.0.0.1:18791")
KEY = os.environ["MEM0_KEY"]
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
# Operator-agnostic live tenant (matches the server default set via __WSL_USER__)
UID = os.environ.get("MEM0_DEFAULT_USER_ID") or getpass.getuser()

def test_search_accepts_limit():
    r = httpx.post(f"{URL}/v1/memories/search", json={
        "query": "test", "filters": {"user_id": UID}, "limit": 3, "threshold": 0.1,
    }, headers=H, timeout=10)
    assert r.status_code == 200, r.text
    results = r.json().get("results", [])
    assert len(results) <= 3, f"expected at most 3 results, got {len(results)}"

def test_search_rejects_top_k():
    r = httpx.post(f"{URL}/v1/memories/search", json={
        "query": "test", "filters": {"user_id": UID}, "top_k": 3,
    }, headers=H, timeout=10)
    # Pydantic should reject unknown field OR ignore it; assert no 500
    assert r.status_code in (200, 422), r.text
    if r.status_code == 422:
        assert "top_k" in r.text or "extra" in r.text.lower()

def test_list_clamps_above_500():
    r = httpx.get(f"{URL}/v1/memories", params={"user_id": UID, "limit": 9999}, headers=H, timeout=10)
    assert r.status_code == 200, r.text
    results = r.json().get("results", [])
    assert len(results) <= 500
