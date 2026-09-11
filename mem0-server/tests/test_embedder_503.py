# mem0-server/tests/test_embedder_503.py
"""Embedder outages answer 503 + Retry-After with reason cold-embedder (spec §4, P1-6 server half)."""
import os
import sys

import httpx
import openai
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import embedder_503 as e5  # noqa: E402


def _status(code):
    req = httpx.Request("POST", "http://127.0.0.1:11436/v1/embeddings")
    return openai.APIStatusError("x", response=httpx.Response(code, request=req), body=None)


@pytest.mark.parametrize("exc", [
    openai.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:11436")),
    openai.APITimeoutError(request=httpx.Request("POST", "http://127.0.0.1:11436")),
    _status(500), _status(503),
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
])
def test_outages_classify_as_503_with_retry(exc):
    assert e5.classify(exc) == 10


@pytest.mark.parametrize("exc", [_status(400), _status(401), _status(429), ValueError("x"), KeyError("k")])
def test_non_outages_are_left_alone(exc):
    assert e5.classify(exc) is None


def test_install_registers_a_handler_that_answers_503():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    e5.install(app)

    @app.get("/boom")
    def boom():
        raise httpx.ConnectError("refused")

    @app.get("/bad")
    def bad():
        raise _status(400)

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/boom")
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "10"
    assert r.json()["reason"] == "cold-embedder"
    r = c.get("/bad")
    assert r.status_code == 500, "a 4xx from the embedder is not an outage and must not be masked as 503"


# ---- GET /health/embedder (P1-6 PC half): the SessionStart pre-warm target ----
# The route reads llama-swap's model listing (loaded state, informational) and embeds one
# token as ACTIVE work — a cold seat answers 503 + Retry-After through the handler above,
# which is exactly what the hook client names `cold-embedder` and retries once.

def _llama_swap(code, payload, method="GET", path="/v1/models"):
    return httpx.Response(code, json=payload, request=httpx.Request(method, "http://127.0.0.1:11436" + path))


def _models(state):
    return {"object": "list", "data": [{"id": "embeddinggemma", "object": "model", "state": state},
                                       {"id": "qwen3.8-27b-vllm", "object": "model", "state": "stopped"}]}


def _embedding_ok(url, **kw):
    return _llama_swap(200, {"data": [{"embedding": [0.0] * 768}]}, "POST", "/v1/embeddings")


@pytest.fixture(scope="module")
def health_client():
    import app as appmod  # heavy import; mem0 init runs once, shared across the suite
    from fastapi.testclient import TestClient
    return TestClient(appmod.app, raise_server_exceptions=False)


def test_health_embedder_warms_and_reports_loaded(health_client, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _llama_swap(200, _models("loaded")))
    monkeypatch.setattr(httpx, "post", _embedding_ok)
    r = health_client.get("/health/embedder")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["loaded"] is True
    assert isinstance(body["warm_ms"], int) and body["warm_ms"] >= 0


def test_health_embedder_loaded_is_none_when_the_listing_is_unreadable(health_client, monkeypatch):
    def listing_down(url, **kw):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", listing_down)
    monkeypatch.setattr(httpx, "post", _embedding_ok)
    r = health_client.get("/health/embedder")
    assert r.status_code == 200, "the listing is informational; the warm embed is the check"
    assert r.json()["loaded"] is None
    assert r.json()["ok"] is True


def test_health_embedder_cold_embedder_answers_503_with_retry_after(health_client, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _llama_swap(200, _models("stopped")))

    def cold(url, **kw):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "post", cold)
    r = health_client.get("/health/embedder")
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "10"
    assert r.json()["reason"] == "cold-embedder"


def test_http_5xx_from_the_embedder_is_an_outage_4xx_is_not():
    import httpx as _h
    import embedder_503 as e5mod
    req = _h.Request("POST", "http://127.0.0.1:11436/v1/embeddings")
    e5 = _h.HTTPStatusError("boom", request=req, response=_h.Response(503, request=req))
    e4 = _h.HTTPStatusError("bad", request=req, response=_h.Response(400, request=req))
    assert e5mod.classify(e5) == e5mod.RETRY_AFTER_S
    assert e5mod.classify(e4) is None


def test_health_embedder_asks_for_the_configured_model(health_client, monkeypatch):
    import app as appmod
    monkeypatch.setitem(appmod.EMBEDDER_CONFIG, "model", "embeddinggemma-ams")
    seen = {}

    def post(url, json=None, timeout=None, **kw):
        seen["model"] = json["model"]
        return _llama_swap(200, {"data": [{"embedding": [0.0] * 768}]}, method="POST", path="/v1/embeddings")
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None, **kw: _llama_swap(200, {"data": [{"id": "embeddinggemma-ams", "state": "ready"}]}))
    monkeypatch.setattr(httpx, "post", post)
    r = health_client.get("/health/embedder")
    assert r.status_code == 200 and seen["model"] == "embeddinggemma-ams" and r.json()["loaded"] is True
