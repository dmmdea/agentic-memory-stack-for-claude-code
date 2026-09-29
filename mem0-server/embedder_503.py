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
    # A direct llama-swap call (GET /health/embedder) raising on a 5xx while the seat loads is the
    # same outage as a refused connection; a 4xx is a real error and stays one.
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None and exc.response.status_code >= 500:
        return RETRY_AFTER_S
    ot = _openai_types()
    if ot:
        conn, timeout, status = ot
        if isinstance(exc, (conn, timeout)):
            return RETRY_AFTER_S
        if isinstance(exc, status) and int(getattr(exc, "status_code", 0) or 0) >= 500:
            return RETRY_AFTER_S
    return None


# llama-swap reports a model whose llama-server died at load as HTTP 500 with this message
# (measured: no VRAM headroom beside a resident vLLM seat). A bare 500 can also be a real
# error (context overflow), so the marker is what tells "cannot start right now" apart.
_START_FAILURE_MARKER = "exited prematurely"
_GATEWAY_STATUSES = (502, 503, 504)


def _status_of(exc: BaseException) -> Optional[int]:
    """HTTP status carried by an openai.APIStatusError or httpx.HTTPStatusError, else None."""
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _upstream_text(exc: BaseException) -> str:
    """The exception message plus the upstream response body, best effort (never raises)."""
    parts = [str(exc)]
    try:
        parts.append(str(getattr(getattr(exc, "response", None), "text", "") or ""))
    except Exception:  # a streamed/closed response has no readable body
        pass
    return " ".join(parts).lower()


def retry_later(exc: BaseException) -> Optional[int]:
    """Seconds to wait when `exc` means the embedder cannot serve RIGHT NOW, else None.

    Narrower than `classify` on purpose: this is the mapper the endpoints use for exceptions
    they caught themselves, and a 503 there tells the shim to queue the write. Retryable are
    a refused/timed-out connection, a gateway status (502/503/504) and llama-swap's
    500 'upstream command exited prematurely' (the seat could not start). Every other 500
    (context overflow, a coding error) stays a 500: replaying it only doubles the damage."""
    if isinstance(exc, _HTTPX_OUTAGES):
        return RETRY_AFTER_S
    ot = _openai_types()
    if ot and isinstance(exc, ot[:2]):  # APIConnectionError (incl. APITimeoutError)
        return RETRY_AFTER_S
    status = _status_of(exc)
    if status in _GATEWAY_STATUSES:
        return RETRY_AFTER_S
    if status == 500 and _START_FAILURE_MARKER in _upstream_text(exc):
        return RETRY_AFTER_S
    return None


_LOADED_STATES = ("loaded", "ready", "running")


def listing_loaded(entry: dict) -> Optional[bool]:
    """Whether a llama-swap /v1/models entry says the model is loaded; None when it does not say.

    Two schemas: the flat one (`state` or `status` is a string) and llama-swap >= v256, where
    `status` is an object `{"value": "loaded"}`."""
    state = entry.get("state")
    if state is None:
        state = entry.get("status")
    if isinstance(state, dict):
        state = state.get("value")
    if state is None:
        return None
    return str(state).lower() in _LOADED_STATES


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
