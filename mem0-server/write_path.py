"""write_path.py — the write path, learned from real write traffic (GET /health/maintenance `write_path`).

A memory server whose embedder cannot start fails every write, and nothing on its health endpoints
noticed: /health/maintenance kept answering `ok: true` for hours while POST /v1/memories answered
500 and 503. The endpoints that do touch the embedder (/health/embedder, /health/deep) load the
model, so polling them every few minutes would keep it resident, and every idle model must unload
after five minutes. The signal therefore has to be PASSIVE: this tracker only counts the outcomes
of writes that really happen, with no I/O and no model load, so an uptime checker can poll the
endpoint that publishes it as often as it likes.

Rules (the docstrings below repeat the ones the code enforces):

* An outcome counts only when it says something about the path: a 2xx is a success and a 5xx a
  failure. A 4xx (admission reject, validation, auth) is the caller's problem, and a 1xx/3xx says
  nothing; neither is recorded, so neither can fail the path nor clear a failure.
* `ok` is false while the MOST RECENT recorded outcome is a failure, and only the next success
  clears it. There is no time decay: silence is not health, so an hour without a write after a
  failure still reads false. The counters, unlike `ok`, cover the last hour only.
* The state lives in this process and nowhere else. A restart forgets it and reads `ok: true`
  with nulls, because there is no evidence yet either way.

The tracker is a plain object (`WritePathTracker`) so a test can own one; the module-level
`record`, `snapshot` and `reset` work on the process-wide `TRACKER`, which is what the server uses.
"""
from __future__ import annotations

import collections
import datetime as dt
import threading
import time
from typing import Optional

WINDOW_S = 3600.0          # errors_1h / writes_1h look back this far; nothing older is kept
MAX_REASON_CHARS = 64      # a reason is a short label, never a message
DEFAULT_REASON = "upstream"


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
