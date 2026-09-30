"""The write-path middleware sees what the client sees, and changes nothing about it.

write_path.install(app) registers ONE middleware that records the outcome of POST /v1/memories and
PUT /v1/memories/{id} into the passive tracker. It has to record the FINAL response, after the
exception handlers ran: the 503 that embedder_503.install builds from an embedder outage and the
500 a route raises as an HTTPException are both responses by the time it looks, and an exception
nobody handles reaches it as an exception, which it records as a 500 and re-raises. A 2xx that a
route marks neutral (the add route's duplicate answer never reaches the embedder) is recorded as
nothing, so it can neither count as a success nor clear a failure; a 5xx is recorded whatever the
route marked.

`import app` cannot run headless (it builds the live Memory client), so this builds a minimal
FastAPI app that installs the SAME middleware (write_path.install) and the SAME embedder handler
(embedder_503.install), mounts fake write routes that fail in each of the ways the real ones do, and
asserts (a) what the tracker recorded and (b) that the response is byte-for-byte what the same app
answers without the middleware. Two source-level pins then check that app.py really installs it and
feeds the snapshot to /health/maintenance. Headless: no server, no network.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import embedder_503  # noqa: E402
import write_path as wp  # noqa: E402
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import Response, StreamingResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_tracker(monkeypatch):
    monkeypatch.setattr(wp, "TRACKER", wp.WritePathTracker())


class Body(BaseModel):
    text: str


def _stream(chunks, status):
    async def gen():
        for c in chunks:
            yield c
    return StreamingResponse(gen(), status_code=status, media_type="application/json")


def _bg_boom():
    raise RuntimeError("background task failed after the response")


def _act(case: str, background: BackgroundTasks | None = None, request: Request | None = None):
    """What a write route can do, named after the real failure it stands in for."""
    if case == "bg-fail":           # the NLI gate runs as a background task, after the response
        background.add_task(_bg_boom)
        return {"results": [{"id": "m1", "event": "ADD"}]}
    if case == "ok":
        return {"results": [{"id": "m1", "event": "ADD"}]}
    # A 2xx that never reached the embedder: the route marks it neutral, as app.add() does for a duplicate.
    if case == "neutral-dup":
        wp.mark_neutral(request)
        return {"results": [{"id": "m1", "memory": "hello", "event": "NOOP_DUPLICATE"}], "deduplicated": True}
    if case == "neutral-empty":     # infer=False, every message skipped: 200 {"results": []}
        wp.mark_neutral(request)
        return {"results": []}
    if case == "neutral-created":
        wp.mark_neutral(request)
        return Response(b'{"id": "m2"}', status_code=201, media_type="application/json")
    # A route that marked itself neutral and then failed: the failure still counts, whatever the flag says.
    if case == "neutral-then-503":
        wp.mark_neutral(request)
        raise HTTPException(503, "upstream unavailable, retry later", headers={"Retry-After": "10"})
    if case == "neutral-then-500":
        wp.mark_neutral(request)
        raise HTTPException(500, "boom")
    if case == "neutral-then-5xx-response":
        wp.mark_neutral(request)
        return Response(b'{"detail": "disk"}', status_code=507, media_type="application/json")
    if case == "neutral-then-crash":
        wp.mark_neutral(request)
        raise RuntimeError("bug")
    if case == "embedder":          # an embed call raised outside the route's own try: embedder_503 answers
        raise httpx.ConnectError("refused")
    if case == "upstream-500":      # what app._upstream_error(e) raises for an error that is not an outage
        raise HTTPException(500, "boom")
    if case == "upstream-503":      # ... and for an outage the route caught itself (no `reason` in its body)
        raise HTTPException(503, "upstream unavailable, retry later", headers={"Retry-After": "10"})
    if case == "crash":             # nobody handles this one
        raise RuntimeError("bug")
    if case in ("400", "401", "403", "413", "429"):
        raise HTTPException(int(case), "the caller's problem")
    if case == "reason-body":       # any 5xx that names its own reason
        return Response(b'{"detail": "disk", "reason": "disk-full"}', status_code=507, media_type="application/json")
    if case == "reason-not-a-string":
        return Response(b'{"reason": 5}', status_code=500, media_type="application/json")
    if case == "not-json":
        return Response(b"upstream said no", status_code=502, media_type="text/plain")
    if case == "json-list":
        return Response(b'["cold-embedder"]', status_code=500, media_type="application/json")
    if case == "empty-502":
        return Response(status_code=502)
    if case == "stream-reason":     # the reason sits in the first chunk of a chunked 5xx
        return _stream([b'{"reason": "cold-embedder"}', b"\n", b"\n"], 503)
    if case == "stream-split":      # the JSON is cut across chunks, so the first chunk alone is not JSON
        return _stream([b'{"reason": "cold-embed', b'der"}'], 503)
    if case == "huge-5xx":
        return Response(b'{"reason": "cold-embedder", "pad": "' + b"x" * 9000 + b'"}', status_code=500,
                        media_type="application/json")
    if case == "created":
        return Response(b'{"id": "m2"}', status_code=201, media_type="application/json")
    raise AssertionError(f"unknown case {case}")


def build_app(*, middleware: bool = True, install_middleware_first: bool = False, installs: int = 1) -> FastAPI:
    app = FastAPI()
    if middleware and install_middleware_first:
        wp.install(app)
    embedder_503.install(app)
    if middleware and not install_middleware_first:
        for _ in range(installs):
            wp.install(app)

    @app.post("/v1/memories")
    def add(b: Body, background: BackgroundTasks, request: Request, case: str = "ok"):
        return _act(case, background, request)

    @app.put("/v1/memories/{mid}")
    def update(mid: str, b: Body, background: BackgroundTasks, request: Request, case: str = "ok"):
        return _act(case, background, request)

    # The routes that share the prefix but are not the write path: they fail loudly and must not count.
    @app.post("/v1/memories/search")
    def search(case: str = "ok"):
        return _act(case)

    @app.post("/v1/memories/diagnose")
    def diagnose(case: str = "ok"):
        return _act(case)

    @app.get("/v1/memories")
    def list_all(case: str = "ok"):
        return _act(case)

    @app.patch("/v1/memories/{mid}/tier")
    def tier(mid: str, case: str = "ok"):
        return _act(case)

    @app.delete("/v1/memories/{mid}")
    def delete(mid: str, case: str = "ok"):
        return _act(case)

    return app


def _client(**kw) -> TestClient:
    return TestClient(build_app(**kw), raise_server_exceptions=False)


def _post(c: TestClient, case: str, text: str | None = "hello"):
    return c.post("/v1/memories", params={"case": case}, json={"text": text} if text is not None else {})


def _counts():
    snap = wp.snapshot()
    return snap["errors_1h"], snap["writes_1h"]


# --------------------------------------------------------------------------------- which requests count
@pytest.mark.parametrize("method,path,counts", [
    ("POST", "/v1/memories", True),
    ("PUT", "/v1/memories/abc", True),
    ("put", "/v1/memories/0f8e6b1c-uuid", True),
    ("PUT", "/v1/memories/search", True),           # an id like any other: PUT has no /search route of its own
    ("POST", "/v1/memories/", False),               # FastAPI answers this one with a 307 redirect
    ("POST", "/v1/memories/search", False),
    ("POST", "/v1/memories/diagnose", False),
    ("POST", "/v1/memories/abc", False),
    ("PUT", "/v1/memories", False),
    ("PUT", "/v1/memories/", False),
    ("PUT", "/v1/memories/abc/", False),
    ("PUT", "/v1/memories/abc/extra", False),
    ("PATCH", "/v1/memories/abc/tier", False),
    ("PATCH", "/v1/memories/abc/metadata", False),
    ("DELETE", "/v1/memories/abc", False),
    ("GET", "/v1/memories", False),
    ("GET", "/v1/memories/abc", False),
    ("POST", "/v1/episodes", False),
    ("POST", "/health/embedder", False),
    ("POST", "", False),
])
def test_only_post_memories_and_put_memory_by_id_are_write_requests(method, path, counts):
    assert wp.is_write_request(method, path) is counts


# ---------------------------------------------------------------------------------------- what it records
def test_an_embedder_outage_is_recorded_as_503_cold_embedder():
    c = _client()
    r = _post(c, "embedder")
    assert r.status_code == 503 and r.headers["Retry-After"] == "10"
    snap = wp.snapshot()
    assert snap["ok"] is False and snap["last_error"] == "503 cold-embedder"
    assert (snap["errors_1h"], snap["writes_1h"]) == (1, 1)


def test_a_write_that_succeeds_is_recorded_and_clears_the_failure():
    c = _client()
    assert _post(c, "embedder").status_code == 503
    assert wp.snapshot()["ok"] is False
    r = _post(c, "ok")
    assert r.status_code == 200
    snap = wp.snapshot()
    assert snap["ok"] is True and snap["last_ok_at"] is not None
    assert snap["last_error"] == "503 cold-embedder", "the last error stays on record after it clears"
    assert (snap["errors_1h"], snap["writes_1h"]) == (1, 2)


def test_a_201_is_a_success_too():
    c = _client()
    _post(c, "embedder")
    assert _post(c, "created").status_code == 201
    assert wp.snapshot()["ok"] is True


def test_an_http_exception_500_from_a_route_is_recorded_as_500_upstream():
    c = _client()
    r = _post(c, "upstream-500")
    assert r.status_code == 500 and r.json() == {"detail": "boom"}
    snap = wp.snapshot()
    assert snap["ok"] is False and snap["last_error"] == "500 upstream"
    assert (snap["errors_1h"], snap["writes_1h"]) == (1, 1)


def test_a_503_a_route_raised_itself_has_no_reason_and_reads_upstream():
    c = _client()
    r = _post(c, "upstream-503")
    assert r.status_code == 503 and r.headers["Retry-After"] == "10"
    assert wp.snapshot()["last_error"] == "503 upstream"


def test_a_validation_error_422_records_nothing():
    c = _client()
    r = _post(c, "ok", text=None)               # the body has no `text`
    assert r.status_code == 422
    assert wp.snapshot() == {"ok": True, "last_ok_at": None, "last_error_at": None, "last_error": None,
                             "errors_1h": 0, "writes_1h": 0}


@pytest.mark.parametrize("case", ["400", "401", "403", "413", "429"])
def test_the_callers_4xx_records_nothing_and_clears_nothing(case):
    c = _client()
    _post(c, "embedder")
    r = _post(c, case)
    assert r.status_code == int(case)
    snap = wp.snapshot()
    assert snap["ok"] is False and (snap["errors_1h"], snap["writes_1h"]) == (1, 1)


def test_put_by_id_is_recorded_like_post():
    c = _client()
    r = c.put("/v1/memories/abc", params={"case": "embedder"}, json={"text": "x"})
    assert r.status_code == 503
    assert wp.snapshot()["last_error"] == "503 cold-embedder"
    r = c.put("/v1/memories/abc", params={"case": "ok"}, json={"text": "x"})
    assert r.status_code == 200 and wp.snapshot()["ok"] is True


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/memories/search"), ("POST", "/v1/memories/diagnose"), ("GET", "/v1/memories"),
    ("PATCH", "/v1/memories/abc/tier"), ("DELETE", "/v1/memories/abc"),
])
@pytest.mark.parametrize("case", ["upstream-500", "embedder", "ok"])
def test_other_routes_under_the_prefix_are_not_the_write_path(method, path, case):
    c = _client()
    r = c.request(method, path, params={"case": case})
    assert r.status_code in (200, 500, 503)
    assert _counts() == (0, 0), f"{method} {path} must not be recorded"


# ------------------------------------------------------------ a 2xx that says nothing about the path is neutral
NEUTRAL_2XX = ["neutral-dup", "neutral-empty", "neutral-created"]
FRESH = {"ok": True, "last_ok_at": None, "last_error_at": None, "last_error": None, "errors_1h": 0, "writes_1h": 0}


@pytest.mark.parametrize("case", NEUTRAL_2XX)
def test_a_neutral_2xx_records_nothing(case):
    c = _client()
    assert _post(c, case).status_code in (200, 201)
    assert wp.snapshot() == FRESH


@pytest.mark.parametrize("case", NEUTRAL_2XX)
@pytest.mark.parametrize("method,path", [("POST", "/v1/memories"), ("PUT", "/v1/memories/abc")])
def test_a_neutral_2xx_during_a_failure_does_not_clear_it(method, path, case):
    """The outage this signal exists for. Automated writers re-post whole transcripts, so most of their
    writes are duplicates answered 200 without the embedder, interleaved with the 503s for the new facts:
    counting those 200s as successes made `ok` flap back to true between two failures."""
    c = _client()
    assert _post(c, "embedder").status_code == 503
    for _ in range(3):
        r = c.request(method, path, params={"case": case}, json={"text": "hello"})
        assert r.status_code in (200, 201)
    snap = wp.snapshot()
    assert snap["ok"] is False, "a duplicate answered 200 must not turn the path green"
    assert snap["last_error"] == "503 cold-embedder" and snap["last_ok_at"] is None
    assert (snap["errors_1h"], snap["writes_1h"]) == (1, 1), "a neutral answer is counted neither way"


def test_a_real_success_after_neutral_answers_still_clears_the_failure():
    c = _client()
    _post(c, "embedder")
    _post(c, "neutral-dup")
    assert wp.snapshot()["ok"] is False
    assert _post(c, "ok").status_code == 200
    snap = wp.snapshot()
    assert snap["ok"] is True and snap["last_ok_at"] is not None
    assert (snap["errors_1h"], snap["writes_1h"]) == (1, 2)


def test_the_neutral_mark_belongs_to_one_request_and_does_not_leak_to_the_next():
    c = _client()
    _post(c, "neutral-dup")
    assert _post(c, "ok").status_code == 200
    assert _counts() == (0, 1), "the second request is a real success and is recorded"


@pytest.mark.parametrize("case,status,label", [
    ("neutral-then-503", 503, "503 upstream"),
    ("neutral-then-500", 500, "500 upstream"),
    ("neutral-then-5xx-response", 507, "507 upstream"),
    ("neutral-then-crash", 500, "500 upstream"),
])
@pytest.mark.parametrize("method,path", [("POST", "/v1/memories"), ("PUT", "/v1/memories/abc")])
def test_a_neutral_mark_never_hides_a_failure(method, path, case, status, label):
    """Neutral suppresses a 2xx and only a 2xx: a 5xx, or a crash, is recorded whatever the flag says."""
    c = _client()
    r = c.request(method, path, params={"case": case}, json={"text": "hello"})
    assert r.status_code == status
    snap = wp.snapshot()
    assert snap["ok"] is False and snap["last_error"] == label
    assert (snap["errors_1h"], snap["writes_1h"]) == (1, 1)


def test_a_neutral_route_that_crashes_is_recorded_and_re_raised():
    c = TestClient(build_app(), raise_server_exceptions=True)
    with pytest.raises(RuntimeError, match="bug"):
        _post(c, "neutral-then-crash")
    assert wp.snapshot()["last_error"] == "500 upstream"


# ------------------------------------------------------------------------------ the reason comes from the response
@pytest.mark.parametrize("case,status,expected", [
    ("reason-body", 507, "507 disk-full"),
    ("reason-not-a-string", 500, "500 upstream"),
    ("not-json", 502, "502 upstream"),
    ("json-list", 500, "500 upstream"),
    ("empty-502", 502, "502 upstream"),
    ("stream-reason", 503, "503 cold-embedder"),
    ("stream-split", 503, "503 upstream"),
    ("huge-5xx", 500, "500 upstream"),
])
def test_the_reason_is_read_from_a_5xx_body_and_defaults_to_upstream(case, status, expected):
    c = _client()
    r = _post(c, case)
    assert r.status_code == status
    assert wp.snapshot()["last_error"] == expected


# ----------------------------------------------------------------- an exception nobody handles: recorded, re-raised
def test_an_unhandled_exception_is_recorded_as_500_and_re_raised_not_swallowed():
    c = TestClient(build_app(), raise_server_exceptions=True)
    with pytest.raises(RuntimeError, match="bug"):
        _post(c, "crash")
    snap = wp.snapshot()
    assert snap["ok"] is False and snap["last_error"] == "500 upstream"
    assert (snap["errors_1h"], snap["writes_1h"]) == (1, 1)


def test_an_unhandled_exception_still_answers_500_to_the_client():
    c = _client()
    r = _post(c, "crash")
    assert r.status_code == 500
    assert wp.snapshot()["last_error"] == "500 upstream"


def test_an_exception_on_a_route_that_is_not_recorded_is_still_re_raised():
    c = TestClient(build_app(), raise_server_exceptions=True)
    with pytest.raises(RuntimeError, match="bug"):
        c.post("/v1/memories/search", params={"case": "crash"})
    assert _counts() == (0, 0)


# ------------------------------------------------------- the middleware never changes what the client receives
def _wire(r: httpx.Response):
    return r.status_code, sorted(r.headers.multi_items()), r.content


@pytest.mark.parametrize("case", ["ok", "created", "embedder", "upstream-500", "upstream-503", "reason-body",
                                  "not-json", "empty-502", "stream-reason", "stream-split", "huge-5xx",
                                  "401", "crash", *NEUTRAL_2XX, "neutral-then-503", "neutral-then-500",
                                  "neutral-then-5xx-response", "neutral-then-crash"])
@pytest.mark.parametrize("method,path", [("POST", "/v1/memories"), ("PUT", "/v1/memories/abc")])
def test_the_response_is_identical_with_and_without_the_middleware(method, path, case):
    with_mw, without = _client(), _client(middleware=False)
    kw = dict(params={"case": case}, json={"text": "hello"})
    assert _wire(with_mw.request(method, path, **kw)) == _wire(without.request(method, path, **kw))


def test_a_chunked_5xx_reaches_the_client_chunk_for_chunk():
    """The reason is read from the first chunk; every chunk, including that one, still goes out."""
    c = _client()
    with c.stream("POST", "/v1/memories", params={"case": "stream-reason"}, json={"text": "x"}) as r:
        chunks = [ch for ch in r.iter_raw() if ch]
    assert b"".join(chunks) == b'{"reason": "cold-embedder"}\n\n'
    assert wp.snapshot()["last_error"] == "503 cold-embedder"


def test_a_background_task_that_fails_after_the_response_is_not_a_failed_write():
    """The NLI gate runs as a background task once the write has been answered 200; its crash is not the path's."""
    c = _client()
    r = _post(c, "bg-fail")
    assert r.status_code == 200
    snap = wp.snapshot()
    assert snap["ok"] is True and (snap["errors_1h"], snap["writes_1h"]) == (0, 1)


# --------------------------------------- the middleware's own bookkeeping can never raise into the request
def _boom(*_a, **_k):
    raise RuntimeError("the middleware's own bookkeeping broke")


async def _aboom(*_a, **_k):
    raise RuntimeError("the middleware's own bookkeeping broke")


@pytest.mark.parametrize("method,path", [("POST", "/v1/memories"), ("PUT", "/v1/memories/abc")])
def test_a_bug_reading_the_failure_reason_never_changes_the_response(monkeypatch, method, path):
    """_reason_of runs on the response the client is about to get. If it breaks, the client still gets that
    exact response, and the failure is still recorded (with the default reason)."""
    kw = dict(params={"case": "embedder"}, json={"text": "hello"})
    expected = _wire(_client(middleware=False).request(method, path, **kw))
    monkeypatch.setattr(wp, "_reason_of", _aboom)
    r = _client().request(method, path, **kw)
    assert r.status_code == 503 and _wire(r) == expected
    snap = wp.snapshot()
    assert snap["ok"] is False and snap["last_error"] == "503 upstream"
    assert (snap["errors_1h"], snap["writes_1h"]) == (1, 1)


def test_a_bug_parsing_the_reason_cannot_lose_the_chunk_that_was_peeked(monkeypatch):
    """The first chunk is taken off the stream to read the reason and put back. If the parser breaks after
    that, the chunk must already be back: the client still gets the whole body."""
    kw = dict(params={"case": "stream-reason"}, json={"text": "hello"})
    expected = _wire(_client(middleware=False).post("/v1/memories", **kw))
    monkeypatch.setattr(wp, "_reason_from", _boom)
    r = _client().post("/v1/memories", **kw)
    assert r.status_code == 503 and r.content == b'{"reason": "cold-embedder"}\n\n' and _wire(r) == expected
    assert wp.snapshot()["last_error"] == "503 upstream"


def test_an_unreadable_neutral_flag_records_nothing_and_changes_nothing(monkeypatch):
    """Failing to read the flag must never turn a 2xx into a success: nothing is recorded (an unknown path is
    not a healthy one) and the client gets exactly what the route gave. A 5xx never consults the flag."""
    c = _client()
    _post(c, "embedder")
    monkeypatch.setattr(wp, "is_neutral", _boom)
    r = _post(c, "ok")
    assert r.status_code == 200 and r.json() == {"results": [{"id": "m1", "event": "ADD"}]}
    snap = wp.snapshot()
    assert snap["ok"] is False and (snap["errors_1h"], snap["writes_1h"]) == (1, 1)
    assert _post(c, "upstream-500").status_code == 500
    assert _counts() == (2, 2)


@pytest.mark.parametrize("case,status", [("ok", 200), ("upstream-500", 500), ("embedder", 503)])
def test_a_tracker_that_cannot_record_never_changes_the_response(monkeypatch, case, status):
    kw = dict(params={"case": case}, json={"text": "hello"})
    expected = _wire(_client(middleware=False).post("/v1/memories", **kw))
    monkeypatch.setattr(wp, "TRACKER", types.SimpleNamespace(record=lambda *a, **k: 1 / 0))
    r = _client().post("/v1/memories", **kw)
    assert r.status_code == status and _wire(r) == expected


def test_a_tracker_that_cannot_record_does_not_replace_the_routes_exception(monkeypatch):
    monkeypatch.setattr(wp, "TRACKER", types.SimpleNamespace(record=lambda *a, **k: 1 / 0))
    c = TestClient(build_app(), raise_server_exceptions=True)
    with pytest.raises(RuntimeError, match="bug"):
        _post(c, "crash")


def _run_middleware(response):
    async def go():
        async def call_next(request):
            return response
        req = types.SimpleNamespace(method="POST", scope={"path": "/v1/memories"}, state=types.SimpleNamespace())
        return await wp.middleware(req, call_next)
    return asyncio.run(go())


def test_a_response_whose_status_cannot_be_read_is_handed_back_untouched():
    class Odd:
        @property
        def status_code(self):
            raise RuntimeError("no status on this response")

    odd = Odd()
    assert _run_middleware(odd) is odd
    assert _counts() == (0, 0)


# ------------------------------------------------- reading the reason leaves the response stream exactly as it was
class _Fake:
    """Just enough of a Starlette response for _reason_of: a status and a body_iterator."""

    def __init__(self, chunks, boom=None, boom_after=0):
        async def gen():
            for i, c in enumerate(chunks):
                if boom is not None and i == boom_after:
                    raise boom
                yield c
            if boom is not None and boom_after >= len(chunks):
                raise boom
        self.body_iterator = gen()


async def _drain(response):
    out, err = [], None
    try:
        async for chunk in response.body_iterator:
            out.append(chunk)
    except Exception as exc:  # noqa: BLE001 — the point is to see WHICH one arrives, and when
        err = exc
    return out, err


def _reason_and_stream(fake):
    async def go():
        reason = await wp._reason_of(fake)
        return reason, *(await _drain(fake))
    return asyncio.run(go())


def test_the_first_chunk_is_read_and_put_back_with_the_rest_of_the_stream():
    fake = _Fake([b'{"reason": "cold-embedder"}', b"tail-1", b"tail-2"])
    reason, chunks, err = _reason_and_stream(fake)
    assert reason == "cold-embedder" and err is None
    assert chunks == [b'{"reason": "cold-embedder"}', b"tail-1", b"tail-2"]


def test_an_empty_stream_stays_empty():
    reason, chunks, err = _reason_and_stream(_Fake([]))
    assert (reason, chunks, err) == (None, [], None)


def test_an_asgi_message_chunk_such_as_pathsend_passes_through_untouched():
    message = {"type": "http.response.pathsend", "path": "x"}
    reason, chunks, err = _reason_and_stream(_Fake([message, b"more"]))
    assert reason is None and chunks == [message, b"more"] and err is None


def test_a_stream_that_fails_at_once_fails_again_at_the_same_point_of_the_replay():
    boom = ConnectionResetError("client went away")
    reason, chunks, err = _reason_and_stream(_Fake([b"never"], boom=boom, boom_after=0))
    assert reason is None and chunks == [] and err is boom, "the failure is raised again, not swallowed"


def test_a_stream_that_fails_after_its_first_chunk_delivers_the_chunk_then_fails():
    boom = ConnectionResetError("cut off")
    reason, chunks, err = _reason_and_stream(_Fake([b'{"reason": "x"}', b"never"], boom=boom, boom_after=1))
    assert reason == "x" and chunks == [b'{"reason": "x"}'] and err is boom


def test_a_fully_rendered_response_is_read_from_its_body():
    class Rendered:
        body = b'{"reason": "cold-embedder"}'
    assert asyncio.run(wp._reason_of(Rendered())) == "cold-embedder"
    assert asyncio.run(wp._reason_of(object())) is None


# ------------------------------------------------------------------------------------------------ ordering
@pytest.mark.parametrize("first", [False, True])
def test_the_order_of_the_two_installs_does_not_matter(first):
    """Starlette wraps user middleware around the exception handlers whatever order they were added in."""
    c = _client(install_middleware_first=first)
    assert _post(c, "embedder").status_code == 503
    assert wp.snapshot()["last_error"] == "503 cold-embedder"
    assert _post(c, "upstream-500").status_code == 500
    assert wp.snapshot()["last_error"] == "500 upstream"
    assert _post(c, "ok").status_code == 200
    assert wp.snapshot()["ok"] is True


def test_without_the_install_nothing_is_recorded():
    c = _client(middleware=False)
    assert _post(c, "embedder").status_code == 503
    assert _counts() == (0, 0)


# ------------------------------------------------------------------------------- install is idempotent
def test_installing_the_middleware_twice_does_not_record_twice():
    c = _client(installs=3)
    assert _post(c, "embedder").status_code == 503
    assert _counts() == (1, 1)
    assert _post(c, "ok").status_code == 200
    assert _counts() == (1, 2)


def test_installing_again_after_the_first_request_is_a_no_op_not_an_error():
    """Once the app has served a request Starlette refuses new middleware. A second install has nothing to add,
    so it must not even try."""
    app = build_app()
    c = TestClient(app, raise_server_exceptions=False)
    assert _post(c, "ok").status_code == 200
    wp.install(app)
    assert _post(c, "embedder").status_code == 503
    assert _counts() == (1, 2)


def test_two_apps_each_get_their_own_middleware():
    """The marker lives on the app, not in the module: a second app is not mistaken for an installed one."""
    a, b = build_app(), build_app()
    ca, cb = (TestClient(x, raise_server_exceptions=False) for x in (a, b))
    assert _post(ca, "embedder").status_code == 503 and _post(cb, "embedder").status_code == 503
    assert _counts() == (2, 2)


# ---------------------------------------------------------------------------------------- app.py wiring pins
APP_PY = SERVER_DIR / "app.py"


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def test_app_py_installs_the_middleware_on_its_app():
    """`import app` cannot run headless, so pin the wiring on the syntax tree (a comment does not count)."""
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.Import)
               and any(a.name == "write_path" and a.asname == "_write_path" for a in n.names)]
    assert len(imports) == 1, "app.py must `import write_path as _write_path`"
    made = [n.lineno for n in tree.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "app" for t in n.targets)
            and isinstance(n.value, ast.Call) and _dotted(n.value.func) == "FastAPI"]
    installs = [n.lineno for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                and _dotted(n.value.func) == "_write_path.install"
                and [_dotted(a) for a in n.value.args] == ["app"] and not n.value.keywords]
    assert len(made) == 1 and len(installs) == 1, "app.py must call _write_path.install(app) once, at module level"
    assert installs[0] > made[0], "the middleware is installed on the app after it is created"


# ------------------------------------------- which app.py answers are neutral (pinned on the syntax tree)
# Every 2xx a write route can give WITHOUT the embedder must mark itself neutral, or it clears a failure.
# Enumerated against the routes and mem0 2.0.4 (Memory.add / Memory.update):
#   POST add(): the hash-dedup answer (NOOP_DUPLICATE) is a payload lookup; infer=False with every message
#     skipped (system role, malformed) returns {"results": []} without an embed call. infer=True embeds the
#     incoming text (existing-memory lookup) before it can answer at all, so its 200 is evidence, not neutral.
#   PUT update(): Memory.update embeds the new text as its first act; its only 2xx return is after it.
def _app_tree() -> ast.Module:
    return ast.parse(APP_PY.read_text(encoding="utf-8"))


def _route(tree: ast.Module, name: str) -> ast.FunctionDef:
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(fns) == 1, f"app.py must define exactly one module-level {name}()"
    return fns[0]


def _own_nodes(fn: ast.FunctionDef):
    """Every node of a function, not descending into functions or classes defined inside it."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _blocks(fn: ast.FunctionDef):
    """Every statement list of the function (bodies, else branches, handlers)."""
    for node in [fn, *_own_nodes(fn)]:
        if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(node, attr, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


def _is_mark_neutral(stmt: ast.stmt) -> bool:
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and _dotted(stmt.value.func) == "_write_path.mark_neutral"
            and [_dotted(a) for a in stmt.value.args] == ["request"] and not stmt.value.keywords)


def _line_of_call(fn: ast.FunctionDef, dotted: str) -> int:
    lines = [n.lineno for n in _own_nodes(fn) if isinstance(n, ast.Call) and _dotted(n.func) == dotted]
    assert len(lines) == 1, f"{fn.name}() must call {dotted}(...) exactly once"
    return lines[0]


def test_add_takes_the_request_it_marks():
    tree = _app_tree()
    params = {a.arg: _dotted(a.annotation) for a in _route(tree, "add").args.args if a.annotation is not None}
    assert params.get("request") == "Request", "add() needs `request: Request` to mark its response neutral"
    assert any(isinstance(n, ast.ImportFrom) and n.module == "fastapi"
               and any(a.name == "Request" and a.asname is None for a in n.names) for n in tree.body), \
        "app.py must import Request from fastapi"


def test_add_marks_every_answer_it_gives_before_mem_add_neutral():
    """The hash-dedup answer never reaches the embedder. An automated writer re-posts the whole transcript,
    so during an embedder outage most of its writes are exactly this 200, between the 503s."""
    add = _route(_app_tree(), "add")
    mem_add = _line_of_call(add, "mem.add")
    early = [(blk, i) for blk in _blocks(add) for i, s in enumerate(blk)
             if isinstance(s, ast.Return) and s.lineno < mem_add]
    assert early, "add() answers the duplicate before mem.add(...)"
    for blk, i in early:
        assert any(_is_mark_neutral(s) for s in blk[:i]), (
            f"the return at line {blk[i].lineno} answers before mem.add(...) without touching the embedder: "
            "call _write_path.mark_neutral(request) first in the same block")
    dup = [blk[i] for blk, i in early if any(isinstance(n, ast.Constant) and n.value == "NOOP_DUPLICATE"
                                             for n in ast.walk(blk[i]))]
    assert len(dup) == 1, "exactly one early answer is the NOOP_DUPLICATE one"


def test_add_marks_an_infer_false_add_that_stored_nothing_neutral():
    """infer=False skips system-role and malformed messages without an embed call and answers 200 with an
    empty `results`. infer=True is deliberately not covered: it embeds before it can answer."""
    add = _route(_app_tree(), "add")
    mem_add = _line_of_call(add, "mem.add")
    guards = [s for blk in _blocks(add) for s in blk if isinstance(s, ast.If)
              and any(isinstance(n, ast.Call) and _dotted(n.func) == "_write_path.stored_nothing"
                      for n in ast.walk(s.test))]
    assert len(guards) == 1, "add() must test _write_path.stored_nothing(result) once"
    guard = guards[0]
    assert guard.lineno > mem_add, "the check reads the result mem.add(...) returned"
    assert isinstance(guard.test, ast.BoolOp) and isinstance(guard.test.op, ast.And)
    assert [ast.unparse(v) for v in guard.test.values] == ["b.infer is False", "_write_path.stored_nothing(result)"]
    assert any(_is_mark_neutral(s) for s in guard.body)


def test_update_has_no_embedder_free_2xx_path():
    """Memory.update embeds the new text before it does anything else, so every 2xx that PUT gives has
    exercised the embedder and there is nothing to mark. A `return` ahead of the mem.update(...) call would be
    an embedder-free 2xx: it has to mark itself neutral (and get a test) before this pin is relaxed."""
    update = _route(_app_tree(), "update")
    mem_update = _line_of_call(update, "mem.update")
    returns = [n for n in _own_nodes(update) if isinstance(n, ast.Return)]
    assert returns, "update() answers with a return"
    assert all(r.lineno > mem_update for r in returns), "update() must not return before mem.update(...)"
