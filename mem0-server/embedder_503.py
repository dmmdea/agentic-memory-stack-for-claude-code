"""embedder_503.py — map embedder outages to 503 + Retry-After (spec §4, P1-6 server half).

The embedder on the authority unloads after 5 idle minutes and takes ~3.4 s to come back
(Phase 0 measurement); while it is cold (or down) the OpenAI-compatible client raises
connection / timeout / 5xx errors that used to surface as 500s, so the hook client dropped
the write. A 503 with Retry-After tells the shim to queue the write and the hook client to
log `cold-embedder` instead of an empty bundle. A 4xx from the embedder is not an outage and
is left to FastAPI's default handling."""
from __future__ import annotations

from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

RETRY_AFTER_S = 10
REASON = "cold-embedder"

_HTTPX_OUTAGES: tuple[type, ...] = (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                                     httpx.RemoteProtocolError)


def _openai_types() -> tuple[type, ...]:
    try:
        import openai
    except ImportError:  # pragma: no cover — the server always has it
        return ()
    return (openai.APIConnectionError, openai.APITimeoutError, openai.APIStatusError)


def classify(exc: BaseException) -> Optional[int]:
    """Seconds to wait when `exc` is an embedder outage, else None."""
    if isinstance(exc, _HTTPX_OUTAGES):
        return RETRY_AFTER_S
    ot = _openai_types()
    if ot:
        conn, timeout, status = ot
        if isinstance(exc, (conn, timeout)):
            return RETRY_AFTER_S
        if isinstance(exc, status) and int(getattr(exc, "status_code", 0) or 0) >= 500:
            return RETRY_AFTER_S
    return None


def install(app: FastAPI) -> None:
    """Register one handler for every outage-shaped exception type; non-outages re-raise."""
    async def _handler(request: Request, exc: Exception):
        wait = classify(exc)
        if wait is None:
            raise exc
        return JSONResponse(status_code=503, headers={"Retry-After": str(wait)},
                            content={"detail": "embedder unavailable (cold start or down); retry",
                                     "reason": REASON, "retry_after_s": wait})
    for t in _HTTPX_OUTAGES + _openai_types():
        app.add_exception_handler(t, _handler)
