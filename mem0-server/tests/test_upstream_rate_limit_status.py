"""2026-07-26: an upstream rate-limit must surface as a RETRYABLE status, not 500.

llama-swap answers 429 under queue saturation. The embedder absorbs a bounded burst,
but a survivor used to land in every endpoint's generic
``except Exception -> HTTPException(500, str(e))``.

That was data loss, not cosmetics. The MCP shim fails over on CONNECT-level errors
only — deliberately, so a real answer is never masked by a stale replica read — so a
500 was taken as a real answer and the write was DROPPED instead of queued to the
outbox. A memory add was lost exactly this way. 503 + Retry-After is the one status
that means "not an answer, ask again", and the shim now routes it like a connect
failure (see test_shim_offline.py).

Pinned here: the classifier maps ONLY a rate-limit to 503 and everything else keeps 500.

LOCAL-ONLY suite. `import app` builds the live Memory client at import time, so this
file is deliberately outside the headless CI subset — same as test_admission_observability.py.
The companion guard that every endpoint actually routes through the classifier is
source-level and needs no import, so it lives in test_shim_offline.py where CI runs it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402  (heavy import; mem0 init runs once, shared across the suite)


def _openai_rate_limit_error():
    import openai
    req = httpx.Request("POST", "http://127.0.0.1:11436/v1/embeddings")
    return openai.RateLimitError(
        "429 queue saturated", response=httpx.Response(429, request=req), body=None)


def test_openai_rate_limit_maps_to_503_with_retry_after():
    exc = app._upstream_error(_openai_rate_limit_error())
    assert exc.status_code == 503
    assert (exc.headers or {}).get("Retry-After"), "503 must tell the client when to retry"


def test_httpx_429_response_also_maps_to_503():
    """The reranker transport surfaces a 429 as httpx.HTTPStatusError, not
    openai.RateLimitError. Same upstream condition => same retryable status."""
    req = httpx.Request("POST", "http://127.0.0.1:11436/v1/rerank")
    err = httpx.HTTPStatusError(
        "429", request=req, response=httpx.Response(429, request=req))
    assert app._upstream_error(err).status_code == 503


def test_duck_typed_rate_limit_by_name_maps_to_503():
    """Matches episode_embeddings._is_rate_limit: the class NAME alone qualifies,
    so an openai-free or re-wrapped variant is still recognised."""
    class RateLimitError(Exception):
        pass
    assert app._upstream_error(RateLimitError("saturated")).status_code == 503


@pytest.mark.parametrize("exc", [
    ValueError("llama-server 500: context overflow"),
    RuntimeError("qdrant unreachable"),
    KeyError("user_id"),
])
def test_every_other_exception_still_maps_to_500(exc):
    """Scope guard. A ctx-overflow or a coding error must stay LOUD and must never
    be advertised as retryable — a client that retries those just doubles the damage."""
    mapped = app._upstream_error(exc)
    assert mapped.status_code == 500
    assert not (mapped.headers or {}).get("Retry-After")


def test_http_500_response_is_not_treated_as_a_rate_limit():
    req = httpx.Request("POST", "http://127.0.0.1:11436/v1/embeddings")
    err = httpx.HTTPStatusError(
        "500", request=req, response=httpx.Response(500, request=req))
    assert app._upstream_error(err).status_code == 500
