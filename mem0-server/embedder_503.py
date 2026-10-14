"""embedder_503.py — map embedder outages to 503 + Retry-After (spec §4, P1-6 server half).

The embedder on the authority unloads after 5 idle minutes and takes ~3.4 s to come back
(Phase 0 measurement); while it is cold (or down) the OpenAI-compatible client raises
connection / timeout / 5xx errors that used to surface as 500s, so the hook client dropped
the write. A 503 with Retry-After tells the shim to queue the write and the hook client to
log `cold-embedder` instead of an empty bundle. A 4xx from the embedder is not an outage and
is left to FastAPI's default handling."""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

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


# ---------------------------------------------------------------------------
# Hot-path readiness for the per-prompt bundle.
#
# llama-swap does not refuse a request for a model that is loading: it holds a non-streaming
# request until the upstream's health check passes and then serves it. A cold embedder therefore
# raises none of the outage types above on the bundle path; the search simply waits out the whole
# load (measured 2026-10-13: 5.75 s with EmbeddingGemma-2 and its projector, against 0.38 s warm)
# and the hook client, whose budget is 5 s, times out with no block at all. In real use that was
# 22-27 % of typed prompts, because every prompt that follows more than five idle minutes finds
# the embedder unloaded.
#
# So the bundle asks llama-swap's listing first (loopback, a few ms). A model listed as not loaded
# is warmed in a background thread, at most one warm at a time, and that prompt's bundle is served
# without the dense search: no memories, which is the abstain the hook already handles, instead of
# a timeout. The prompts after it find the embedder warm. A listing that cannot be read, or that
# does not report a state (an embedder that is not behind llama-swap), keeps the old behaviour.
# The model still unloads after its idle TTL: the warm is work for the prompt in hand, not residency.
# ---------------------------------------------------------------------------

_warm_lock = threading.Lock()
_warm_state: dict = {"running": False, "started_at": None, "last_ok_at": None, "last_error": None,
                     "started": 0}


def embedder_listed_ready(list_models: Callable[[], list], model: str) -> Optional[bool]:
    """Whether llama-swap lists `model` as loaded: True, False, or None when unknown.

    `list_models` returns the `data` array of GET /v1/models. Any failure reading it is None, as is
    a listing with no entry for the model or an entry that reports no state."""
    try:
        entries = list_models() or []
    except Exception:
        return None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id", "")) == model:
            return listing_loaded(entry)
    return None


def _spawn_daemon(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="embedder-warm", daemon=True).start()


def start_background_warm(warm: Callable[[], None],
                          spawn: Callable[[Callable[[], None]], None] = _spawn_daemon) -> bool:
    """Run `warm` once in the background unless a warm is already running. True when one started."""
    with _warm_lock:
        if _warm_state["running"]:
            return False
        _warm_state["running"] = True
        _warm_state["started_at"] = time.time()
        _warm_state["started"] += 1

    def _run() -> None:
        try:
            warm()
            _warm_state["last_ok_at"] = time.time()
            _warm_state["last_error"] = None
        except Exception as exc:  # reported on /health/deep, never raised into a request
            _warm_state["last_error"] = type(exc).__name__
        finally:
            with _warm_lock:
                _warm_state["running"] = False

    try:
        spawn(_run)
    except Exception as exc:
        with _warm_lock:
            _warm_state["running"] = False
        _warm_state["last_error"] = type(exc).__name__
        return False
    return True


def bundle_embed_gate(list_models: Callable[[], list], warm: Callable[[], None], model: str,
                      spawn: Callable[[Callable[[], None]], None] = _spawn_daemon) -> dict:
    """Decide whether the per-prompt bundle may run its dense search now.

    {"embed": True, "state": "loaded"|"unknown"} -> search as before.
    {"embed": False, "state": "warming", "warm_started": bool} -> skip the search; a warm is running."""
    ready = embedder_listed_ready(list_models, model)
    if ready is None:
        return {"embed": True, "state": "unknown"}
    if ready:
        return {"embed": True, "state": "loaded"}
    return {"embed": False, "state": "warming", "warm_started": start_background_warm(warm, spawn)}


def warm_state() -> dict:
    """A copy of the background-warm bookkeeping, for /health/deep."""
    with _warm_lock:
        return dict(_warm_state)


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
