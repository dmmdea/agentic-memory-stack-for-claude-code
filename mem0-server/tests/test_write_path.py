"""The passive write-path tracker (write_path.py): what a real write outcome says about the path.

A dead embedder made every memory write fail for hours while /health/maintenance kept answering
ok:true, because nothing on that endpoint had ever looked at a write. The tracker learns from the
traffic itself (no I/O, no model load), so it can sit on an endpoint that is polled every few
minutes without keeping a model resident. These tests pin its rules with injected clocks:

  * a 2xx is a success and a 5xx a failure; every other status says nothing about the path, and
    neither does a 2xx a route marks neutral (an answer that never reached the embedder);
  * ok is false while the MOST RECENT outcome is a failure, and only a later success clears it;
  * there is no time decay (silence is not health) but the counters cover the last hour only;
  * the state is bounded, thread-safe, and exactly the six fields the endpoint publishes.

Headless: pure Python, no server, no network.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import write_path as wp  # noqa: E402

T0 = 1_790_000_000.0   # a fixed epoch second; every test injects `now`
HOUR = 3600.0
FIELDS = {"ok", "last_ok_at", "last_error_at", "last_error", "errors_1h", "writes_1h"}


@pytest.fixture(autouse=True)
def _fresh_tracker(monkeypatch):
    """The module-level API works on one default tracker; give every test its own."""
    monkeypatch.setattr(wp, "TRACKER", wp.WritePathTracker())


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds")


def test_a_fresh_tracker_reads_ok_with_nulls_and_exactly_the_published_fields():
    snap = wp.snapshot(now=T0)
    assert snap == {"ok": True, "last_ok_at": None, "last_error_at": None, "last_error": None,
                    "errors_1h": 0, "writes_1h": 0}
    assert set(snap) == FIELDS


def test_success_then_failure_then_success():
    wp.record(200, None, now=T0)
    snap = wp.snapshot(now=T0 + 1)
    assert snap["ok"] is True and snap["last_ok_at"] == _iso(T0)
    assert (snap["errors_1h"], snap["writes_1h"]) == (0, 1)

    wp.record(503, "cold-embedder", now=T0 + 10)
    snap = wp.snapshot(now=T0 + 11)
    assert snap["ok"] is False
    assert snap["last_error"] == "503 cold-embedder"
    assert snap["last_error_at"] == _iso(T0 + 10)
    assert snap["last_ok_at"] == _iso(T0), "a failure does not touch the last success"
    assert (snap["errors_1h"], snap["writes_1h"]) == (1, 2)

    wp.record(201, None, now=T0 + 20)
    snap = wp.snapshot(now=T0 + 21)
    assert snap["ok"] is True, "the next successful write clears the failure"
    assert snap["last_ok_at"] == _iso(T0 + 20)
    assert snap["last_error"] == "503 cold-embedder" and snap["last_error_at"] == _iso(T0 + 10), \
        "the last error stays on record after it clears"
    assert (snap["errors_1h"], snap["writes_1h"]) == (1, 3)
    assert set(snap) == FIELDS


def test_a_failure_with_no_success_before_it_is_still_a_failure():
    wp.record(500, None, now=T0)
    snap = wp.snapshot(now=T0)
    assert snap["ok"] is False and snap["last_ok_at"] is None and snap["last_error"] == "500 upstream"


def test_the_most_recent_outcome_decides_ok():
    """fail, ok, fail: ok is false again, because the LAST outcome is a failure."""
    wp.record(503, "cold-embedder", now=T0)
    wp.record(200, None, now=T0 + 1)
    wp.record(500, None, now=T0 + 2)
    snap = wp.snapshot(now=T0 + 3)
    assert snap["ok"] is False and snap["last_error"] == "500 upstream"
    assert (snap["errors_1h"], snap["writes_1h"]) == (2, 3)


@pytest.mark.parametrize("reason,expected", [
    (None, "500 upstream"),
    ("", "500 upstream"),
    ("   ", "500 upstream"),
    ("cold-embedder", "500 cold-embedder"),
    ("  cold-embedder \n", "500 cold-embedder"),
    ("two\nlines\tand   gaps", "500 two lines and gaps"),
    ("x" * 500, "500 " + "x" * wp.MAX_REASON_CHARS),
])
def test_last_error_is_status_space_reason_and_the_reason_defaults_to_upstream(reason, expected):
    wp.record(500, reason, now=T0)
    assert wp.snapshot(now=T0)["last_error"] == expected


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 413, 422, 429, 499])
def test_a_4xx_is_the_callers_problem_and_is_not_recorded(code):
    wp.record(code, "whatever", now=T0)
    assert wp.snapshot(now=T0) == {"ok": True, "last_ok_at": None, "last_error_at": None, "last_error": None,
                                   "errors_1h": 0, "writes_1h": 0}


def test_a_4xx_neither_fails_nor_clears():
    """An admission reject after an embedder failure says nothing about the embedder: ok stays false."""
    wp.record(503, "cold-embedder", now=T0)
    wp.record(422, None, now=T0 + 1)
    wp.record(403, None, now=T0 + 2)
    snap = wp.snapshot(now=T0 + 3)
    assert snap["ok"] is False and (snap["errors_1h"], snap["writes_1h"]) == (1, 1)
    assert snap["last_error"] == "503 cold-embedder" and snap["last_ok_at"] is None


@pytest.mark.parametrize("code,counts", [
    (100, False), (199, False), (200, True), (204, True), (299, True), (300, False), (307, False),
    (399, False), (400, False), (499, False), (500, True), (503, True), (599, True), (600, False), (0, False),
])
def test_only_2xx_and_5xx_are_recorded(code, counts):
    wp.record(code, None, now=T0)
    assert wp.snapshot(now=T0)["writes_1h"] == (1 if counts else 0)


@pytest.mark.parametrize("junk", [None, "503", 503.0, True, object()])
def test_a_status_that_is_not_an_int_is_ignored_not_raised(junk):
    wp.record(junk, None, now=T0)
    assert wp.snapshot(now=T0)["writes_1h"] == 0


def test_there_is_no_time_decay_an_unknown_path_is_not_a_healthy_one():
    wp.record(503, "cold-embedder", now=T0)
    snap = wp.snapshot(now=T0 + 5 * HOUR)
    assert snap["ok"] is False, "silence for five hours is not a recovery"
    assert snap["last_error"] == "503 cold-embedder" and snap["last_error_at"] == _iso(T0)
    assert (snap["errors_1h"], snap["writes_1h"]) == (0, 0), "the hour counters, unlike ok, do age out"


def _counts(now: float) -> tuple[int, int]:
    snap = wp.snapshot(now=now)
    return snap["errors_1h"], snap["writes_1h"]


def test_the_one_hour_window_drops_old_timestamps():
    wp.record(500, None, now=T0)                     # fails
    wp.record(200, None, now=T0 + 1800)              # ok
    wp.record(503, "cold-embedder", now=T0 + 3000)   # fails
    assert _counts(T0 + 3599) == (2, 3)
    assert _counts(T0 + 3601) == (1, 2), "the first outcome is now more than an hour old"
    assert _counts(T0 + HOUR) == (1, 2), "an outcome exactly one hour old is out of the window"
    assert _counts(T0 + 5401) == (1, 1)
    assert _counts(T0 + 7000) == (0, 0)


def test_recording_prunes_so_memory_is_bounded_to_the_last_hour():
    t = wp.WritePathTracker()
    for i in range(6000):                       # one outcome every 2 s for 3.3 hours, every fifth a failure
        t.record(503 if i % 5 == 0 else 200, "cold-embedder", now=T0 + 2.0 * i)
    last = T0 + 2.0 * 5999
    # The private deques are the whole state: prove they hold the last hour and no more
    # (3600 s at one outcome per 2 s is 1800 outcomes, every fifth of them a failure).
    assert len(t._writes) == 1800 and len(t._errors) == 360
    snap = t.snapshot(now=last)
    assert snap["writes_1h"] == 1800 and snap["errors_1h"] == 360


def test_an_idle_server_lets_go_of_the_last_busy_hour_on_the_next_wall_clock_snapshot(monkeypatch):
    t = wp.WritePathTracker()
    for i in range(100):
        t.record(503 if i % 2 else 200, "cold-embedder", now=T0 + i)
    monkeypatch.setattr(wp.time, "time", lambda: T0 + 99)
    assert t.snapshot()["writes_1h"] == 100 and len(t._writes) == 100
    monkeypatch.setattr(wp.time, "time", lambda: T0 + 3 * HOUR)
    assert t.snapshot()["writes_1h"] == 0
    assert len(t._writes) == 0 and len(t._errors) == 0, "the aged-out timestamps are gone, not just uncounted"


def test_an_injected_now_only_reads_it_never_changes_the_window():
    """Asking about a later time must not cost a later question about an earlier one its answer."""
    wp.record(503, "cold-embedder", now=T0)
    assert _counts(T0 + 2 * HOUR) == (0, 0)
    assert _counts(T0 + 60) == (1, 1)


def test_the_snapshot_is_json_and_its_timestamps_are_utc_iso8601():
    wp.record(200, None, now=T0)
    wp.record(503, "cold-embedder", now=T0 + 60)
    snap = wp.snapshot(now=T0 + 61)
    assert json.loads(json.dumps(snap)) == snap
    for key, epoch in (("last_ok_at", T0), ("last_error_at", T0 + 60)):
        parsed = dt.datetime.fromisoformat(snap[key])
        assert parsed.tzinfo is not None and parsed.utcoffset() == dt.timedelta(0)
        assert parsed.timestamp() == epoch


def test_now_defaults_to_the_wall_clock(monkeypatch):
    monkeypatch.setattr(wp.time, "time", lambda: T0 + 42)
    wp.record(503, None)
    snap = wp.snapshot()
    assert snap["last_error_at"] == _iso(T0 + 42) and snap["errors_1h"] == 1


def test_reset_is_a_restart_no_state_survives():
    wp.record(503, "cold-embedder", now=T0)
    wp.reset()
    assert wp.snapshot(now=T0) == {"ok": True, "last_ok_at": None, "last_error_at": None, "last_error": None,
                                   "errors_1h": 0, "writes_1h": 0}


def test_the_tracker_keeps_no_state_on_disk(isolated_home, tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    wp.record(503, "cold-embedder", now=T0)
    wp.record(200, None, now=T0 + 1)
    wp.snapshot(now=T0 + 2)
    assert list(isolated_home.rglob("*")) == [] and list(cwd.rglob("*")) == [], "the tracker must not write anything"


def test_many_threads_recording_and_reading_lose_no_counts():
    """16 writers x 500 outcomes (a success, a failure, a 4xx in rotation) while 4 readers snapshot."""
    threads_n, per_thread = 16, 500
    pattern = (200, 503, 400)
    per_ok = sum(1 for i in range(per_thread) if pattern[i % 3] == 200)
    per_err = sum(1 for i in range(per_thread) if pattern[i % 3] == 503)
    tracker = wp.WritePathTracker()
    start = threading.Barrier(threads_n + 4)
    stop = threading.Event()
    seen_shapes: list[set] = []

    def writer():
        start.wait()
        for i in range(per_thread):
            tracker.record(pattern[i % 3], "cold-embedder", now=T0 + (i % 50))

    def reader():
        start.wait()
        while True:                    # paced, so the readers overlap the writers without starving them
            seen_shapes.append(set(tracker.snapshot(now=T0 + 60)))
            if stop.wait(0.001):
                break

    writers = [threading.Thread(target=writer) for _ in range(threads_n)]
    readers = [threading.Thread(target=reader) for _ in range(4)]
    for th in writers + readers:
        th.start()
    for th in writers:
        th.join()
    stop.set()
    for th in readers:
        th.join()

    snap = tracker.snapshot(now=T0 + 60)
    assert snap["writes_1h"] == threads_n * (per_ok + per_err)
    assert snap["errors_1h"] == threads_n * per_err
    assert isinstance(snap["ok"], bool), "which outcome is 'last' depends on the interleaving; the counts do not"
    assert seen_shapes and all(shape == FIELDS for shape in seen_shapes)


def test_concurrent_recording_keeps_exactly_the_last_hour(monkeypatch):
    """With a clock that advances one second per read, 4800 concurrent failures span more than an hour,
    so every record prunes while other threads append. Exactly the newest 3600 must survive, in order."""
    import itertools
    ticks = itertools.count(1)
    monkeypatch.setattr(wp.time, "time", lambda: float(next(ticks)))
    tracker = wp.WritePathTracker()
    threads_n, per_thread = 8, 600
    start = threading.Barrier(threads_n)

    def writer():
        start.wait()
        for _ in range(per_thread):
            tracker.record(503, "cold-embedder")

    threads = [threading.Thread(target=writer) for _ in range(threads_n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    total = threads_n * per_thread
    snap = tracker.snapshot(now=float(total))
    assert snap["writes_1h"] == snap["errors_1h"] == 3600
    assert list(tracker._writes) == [float(t) for t in range(total - 3600 + 1, total + 1)]


def test_the_module_level_api_is_the_default_tracker(monkeypatch):
    mine = wp.WritePathTracker()
    monkeypatch.setattr(wp, "TRACKER", mine)
    wp.record(503, "cold-embedder", now=T0)
    assert mine.snapshot(now=T0)["last_error"] == "503 cold-embedder"
    assert wp.snapshot(now=T0) == mine.snapshot(now=T0)


# ------------------------------------------------ the neutral mark: a 2xx that says nothing about the path
class _Req:
    """Just enough of a Starlette request: a `state` that takes attributes."""

    def __init__(self):
        self.state = types.SimpleNamespace()


def test_mark_neutral_sets_the_flag_the_middleware_reads():
    req = _Req()
    assert wp.is_neutral(req) is False
    wp.mark_neutral(req)
    assert wp.NEUTRAL_KEY == "write_path_neutral"
    assert getattr(req.state, wp.NEUTRAL_KEY) is True and wp.is_neutral(req) is True


def test_a_request_nobody_marked_is_not_neutral():
    assert wp.is_neutral(_Req()) is False
    assert wp.is_neutral(types.SimpleNamespace(state=types.SimpleNamespace(write_path_neutral="yes"))) is False, \
        "only a real True marks a response neutral"


class _NoState:
    @property
    def state(self):
        raise RuntimeError("no state on this request")


@pytest.mark.parametrize("request_", [None, object(), types.SimpleNamespace(), types.SimpleNamespace(state=None),
                                      _NoState()], ids=["none", "object", "no-state", "state-none", "state-raises"])
def test_mark_neutral_never_raises_into_the_route(request_):
    """The route calls it on its way to a 200. A health signal must not be the reason a write fails."""
    wp.mark_neutral(request_)


@pytest.mark.parametrize("result,expected", [
    ({"results": []}, True),
    ({"results": [], "relations": []}, True),
    ({"results": [{"id": "m1", "memory": "x", "event": "ADD"}]}, False),
    ({"results": [{"id": "m1", "event": "NOOP_DUPLICATE"}], "deduplicated": True}, False),
    ({}, False),
    ({"results": None}, False),
    ({"message": "Memory updated successfully!"}, False),
    ([], False),
    (None, False),
    ("", False),
])
def test_stored_nothing_is_true_only_for_an_empty_results_list(result, expected):
    """mem0's add answers {"results": []} when it stored nothing. Any other shape is not that answer."""
    assert wp.stored_nothing(result) is expected
