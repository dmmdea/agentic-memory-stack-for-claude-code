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
