"""write_path.py — the write path, learned from real write traffic (GET /health/maintenance `write_path`).

A memory server whose embedder cannot start fails every write, and nothing on its health endpoints
noticed: /health/maintenance kept answering `ok: true` for hours while POST /v1/memories answered
500 and 503. The endpoints that do touch the embedder (/health/embedder, /health/deep) load the
model, so polling them every few minutes would keep it resident, and every idle model must unload
after five minutes. The signal therefore has to be PASSIVE: this tracker only counts the outcomes
of writes that really happen, with no I/O and no model load, so an uptime checker can poll the
endpoint that publishes it as often as it likes.

Rules:

* An outcome counts only when it says something about the path: a 2xx is a success and a 5xx a
  failure. A 4xx (admission reject, validation, auth) is the caller's problem, and a 1xx/3xx says
  nothing; neither is recorded, so neither can fail the path nor clear a failure.
* `ok` is false while the MOST RECENT recorded outcome is a failure, and only the next success
  clears it. There is no time decay: silence is not health, so an hour without a write after a
  failure still reads false. The counters, unlike `ok`, cover the last hour only.
* The state lives in this process and nowhere else. A restart forgets it and reads `ok: true`
  with nulls, because there is no evidence yet either way.

* A 2xx that never reached the embedder says nothing about the path, so it is NEUTRAL: it is not
  counted and it clears nothing. The clearest case is the add route's content-hash dedup, which
  answers 200 from a payload lookup before any embed call. Automated writers re-post whole
  transcripts, so during an embedder outage most of their writes are exactly that 200, between the
  503s for the new facts; counted as successes they made `ok` flap back to true. The route marks
  such an answer with `mark_neutral(request)`; the middleware then records nothing for it. Neutral
  suppresses a 2xx and only a 2xx: a 5xx or an unhandled exception is recorded whatever the flag says.

The tracker is a plain object (`WritePathTracker`) so a test can own one; the module-level
`record`, `snapshot` and `reset` work on the process-wide `TRACKER`, which is what the server uses.

`install(app)` registers the one HTTP middleware that feeds it: the final status of every
`POST /v1/memories` and `PUT /v1/memories/{id}`. It lives here, not in app.py, so the wiring can be
tested without building the live memory client that `import app` builds.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import threading
import time
from typing import Any, Optional

log = logging.getLogger("mem0-server")

WINDOW_S = 3600.0          # errors_1h / writes_1h look back this far; nothing older is kept
MAX_REASON_CHARS = 64      # a reason is a short label, never a message
DEFAULT_REASON = "upstream"
WRITE_PATH_PREFIX = "/v1/memories"
PEEK_MAX_BYTES = 4096      # a failure body bigger than this is not read for a reason (it is still sent whole)
NEUTRAL_KEY = "write_path_neutral"   # the request.state key a route sets on a 2xx that says nothing about the path


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds")


def _clean_reason(reason: Optional[str]) -> str:
    """One short line: whitespace collapsed, capped, and `upstream` when nothing usable is left."""
    text = " ".join(str(reason).split()) if reason is not None else ""
    return text[:MAX_REASON_CHARS].rstrip() or DEFAULT_REASON


class WritePathTracker:
    """A thread-safe, bounded record of recent write outcomes. Nothing here does I/O."""

    def __init__(self, window_s: float = WINDOW_S) -> None:
        self._window_s = float(window_s)
        self._lock = threading.Lock()
        # Timestamps only, oldest first: every recorded outcome and the failures among them,
        # for the last window. `record` drops what has aged out, so memory follows the write
        # rate of the last hour and never the uptime.
        self._writes: collections.deque[float] = collections.deque()
        self._errors: collections.deque[float] = collections.deque()
        self._failing = False
        self._last_ok_at: Optional[float] = None
        self._last_error_at: Optional[float] = None
        self._last_error: Optional[str] = None

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        for stamps in (self._writes, self._errors):
            while stamps and stamps[0] <= cutoff:
                stamps.popleft()

    def record(self, status_code: int, reason: Optional[str] = None, now: Optional[float] = None) -> None:
        """Record one write outcome: 2xx succeeded, 5xx failed, anything else is not counted.

        `reason` labels a failure ("cold-embedder"); it is `upstream` when the response gave none.
        `now` is epoch seconds (the wall clock when omitted). Never raises on a status it cannot
        read: a health signal must not be the reason a write fails."""
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            return
        if 200 <= status_code < 300:
            failed = False
        elif 500 <= status_code < 600:
            failed = True
        else:
            return
        label = f"{status_code} {_clean_reason(reason)}" if failed else None
        with self._lock:
            # Read the clock under the lock so timestamps stay in order across threads.
            ts = time.time() if now is None else float(now)
            self._prune(ts)
            self._writes.append(ts)
            if failed:
                self._errors.append(ts)
                self._failing = True
                self._last_error_at, self._last_error = ts, label
            else:
                self._failing = False
                self._last_ok_at = ts

    def snapshot(self, now: Optional[float] = None) -> dict:
        """The six published fields. `ok` is false iff the most recent outcome was a failure.

        Counting never mutates the window, so an injected `now` in the past reads back the same
        numbers it would have; only the wall-clock form also drops what has aged out, which is
        how an idle server lets go of the last busy hour."""
        with self._lock:
            ts = time.time() if now is None else float(now)
            if now is None:
                self._prune(ts)
            cutoff = ts - self._window_s
            return {
                "ok": not self._failing,
                "last_ok_at": None if self._last_ok_at is None else _iso(self._last_ok_at),
                "last_error_at": None if self._last_error_at is None else _iso(self._last_error_at),
                "last_error": self._last_error,
                "errors_1h": self._count_after(self._errors, cutoff),
                "writes_1h": self._count_after(self._writes, cutoff),
            }

    @staticmethod
    def _count_after(stamps: "collections.deque[float]", cutoff: float) -> int:
        n = 0
        for t in reversed(stamps):     # newest first: stop at the first one that has aged out
            if t <= cutoff:
                break
            n += 1
        return n

    def reset(self) -> None:
        """Forget everything, as a restart does."""
        with self._lock:
            self._writes.clear()
            self._errors.clear()
            self._failing = False
            self._last_ok_at = self._last_error_at = self._last_error = None


# The process-wide tracker the server records into and /health/maintenance reads. The functions
# below look it up on every call, so a test can swap it (or `reset()` it) without re-importing.
TRACKER = WritePathTracker()


def record(status_code: int, reason: Optional[str] = None, now: Optional[float] = None) -> None:
    TRACKER.record(status_code, reason, now)


def snapshot(now: Optional[float] = None) -> dict:
    return TRACKER.snapshot(now)


def reset() -> None:
    TRACKER.reset()


# ------------------------------------------------------------------ the neutral mark (route -> middleware)
def _log_failure(what: str) -> None:
    """Log the exception being handled. Bookkeeping never fails a request, so not even a broken log handler may."""
    try:
        log.exception("write_path: %s", what)
    except Exception:
        pass


def mark_neutral(request: Any) -> None:
    """A write route calls this on a 2xx that never reached the embedder: a duplicate answered from a payload
    lookup, an add that skipped every message. Such an answer says nothing about the write path, so the
    middleware records nothing for it, neither a success nor a clear. It rides on `request.state`, which the
    route's request and the middleware's share (both wrap one ASGI scope). It never raises: a health signal
    must not be the reason a write fails."""
    try:
        setattr(request.state, NEUTRAL_KEY, True)
    except Exception:
        _log_failure("could not mark a response neutral")


def is_neutral(request: Any) -> bool:
    """True when a route marked this request's response neutral (only a real True counts)."""
    return getattr(request.state, NEUTRAL_KEY, False) is True


def stored_nothing(result: Any) -> bool:
    """True when mem0's answer to an add lists no record at all (`{"results": []}`). With infer=False that is an
    add whose every message was skipped, which never calls the embedder."""
    return isinstance(result, dict) and result.get("results") == []


# --------------------------------------------------------------------------- the HTTP middleware
def is_write_request(method: str, path: str) -> bool:
    """`POST /v1/memories` and `PUT /v1/memories/{id}`, exactly. `/v1/memories/search` and `/diagnose`,
    the tier and metadata PATCHes, DELETE and every read share the prefix but are not the write path,
    and a trailing-slash form is a 307 redirect (a 3xx is never recorded anyway)."""
    verb = (method or "").upper()
    if verb == "POST":
        return path == WRITE_PATH_PREFIX
    if verb == "PUT":
        head, _, memory_id = path.rpartition("/")
        return head == WRITE_PATH_PREFIX and bool(memory_id)
    return False


def _reason_from(chunk: Any) -> Optional[str]:
    """The `reason` string of a small JSON-object body (embedder_503 answers with one), else None."""
    if not isinstance(chunk, (bytes, bytearray)) or not chunk or len(chunk) > PEEK_MAX_BYTES:
        return None
    try:
        doc = json.loads(chunk)
    except Exception:  # not JSON, not UTF-8, absurdly nested: no reason, and never an error
        return None
    reason = doc.get("reason") if isinstance(doc, dict) else None
    return reason if isinstance(reason, str) and reason.strip() else None


async def _reason_of(response: Any) -> Optional[str]:
    """The reason a failure response gives for itself, read WITHOUT changing what the client receives.

    Starlette hands the middleware a response whose body is still a stream. Take its first chunk,
    read the reason from that, and put the same chunk back at the front of the stream: the bytes,
    the headers and the status that go out are exactly what the handler produced. Only the first
    chunk is looked at (an error body is one chunk), so a slow or endless body is never waited on.
    A failure raised by the stream itself is held and raised again at the same point of the replay."""
    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray)):          # a fully rendered response, not a stream
        return _reason_from(body)
    stream = getattr(response, "body_iterator", None)
    if stream is None:
        return None
    first: Any = None
    failure: Optional[Exception] = None
    try:
        first = await stream.__anext__()
    except StopAsyncIteration:
        pass
    except Exception as exc:  # not ours to swallow: raised again by the replay below
        failure = exc

    async def replay():
        if first is not None:
            yield first
        if failure is not None:
            raise failure
        async for chunk in stream:
            yield chunk

    response.body_iterator = replay()
    return _reason_from(first)


def _note(status: int, reason: Optional[str]) -> None:
    try:
        TRACKER.record(status, reason)
    except Exception:  # the tracker must never be the reason a write fails
        log.exception("write_path: could not record a %s outcome", status)


async def middleware(request: Any, call_next: Any) -> Any:
    """Record the final status of a write, and hand the response back exactly as it came.

    Only POST /v1/memories and PUT /v1/memories/{id} are looked at; every other request goes straight
    through. The status is the one the client gets, after the exception handlers ran. An exception
    that no handler takes arrives here as an exception: it is recorded as a 500 and raised again, so
    the server answers it as it always did. A 2xx the route marked neutral (`mark_neutral`) is not
    recorded at all; a 5xx or an exception is recorded whatever the route marked."""
    if not is_write_request(request.method, request.scope.get("path", "")):
        return await call_next(request)
    try:
        response = await call_next(request)
    except Exception:
        _note(500, None)
        raise
    status = response.status_code
    if 200 <= status < 300 and is_neutral(request):
        return response          # a 2xx that never reached the embedder: not a success, so it clears nothing
    _note(status, await _reason_of(response) if status >= 500 else None)
    return response


def install(app: Any) -> None:
    """Register `middleware` on a FastAPI/Starlette app (what `@app.middleware("http")` does).

    Ordering: Starlette stacks ServerErrorMiddleware > user middleware > ExceptionMiddleware > routes
    whatever order things were added in, so the exception handlers (embedder_503's 503, FastAPI's
    HTTPException and validation handlers) run INSIDE this middleware and it records the response
    they produced, not the exception they took."""
    app.middleware("http")(middleware)
