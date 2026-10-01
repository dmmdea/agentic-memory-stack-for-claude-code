"""1.32.4 WP-2: a cold-start embed failure is retried, never silently dropped.

`embed_with_cold_retry` is the one retry both callers share: the finalize-time background retry in
app.py (the hook that posts the episode times out at 5 s, so nothing may wait in the request) and the
episode-embed-backfill script (its first run after an embedder restart left four vectors missing).
It retries ONLY what `embedder_503.retry_later` calls "not right now": a refused or timed-out
connection, a gateway status and llama-swap's 500 'upstream command exited prematurely'. A
context-overflow 500 is a real error and surfaces at once.

Every cold-shaped exception here is a REAL type (httpx.ConnectError, httpx.HTTPStatusError carrying a
real httpx.Response, openai's APIStatusError family when openai is installed), because the classifier
reads `status_code` and `response.text` and a hand-rolled stand-in would only prove the stand-in.
Headless: no Qdrant, no mem0, no live embedder; the sleeps go through an injected clock.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import episode_embeddings as ee  # noqa: E402

START_FAILURE = {"error": {"message": "unspecific error: upstream command exited prematurely",
                           "type": "server_error"}}
CTX_OVERFLOW = {"error": {"message": "input is too large to process; increase the physical batch size",
                          "type": "server_error"}}
_REQ = httpx.Request("POST", "http://embedder.invalid/v1/embeddings")


def _http_status_error(status: int, body: dict) -> httpx.HTTPStatusError:
    resp = httpx.Response(status, json=body, request=_REQ)
    return httpx.HTTPStatusError(f"HTTP {status}", request=_REQ, response=resp)


def _openai_status_error(status: int, body: dict):
    openai = pytest.importorskip("openai")
    resp = httpx.Response(status, json=body, request=_REQ)
    cls = openai.InternalServerError if status >= 500 else openai.APIStatusError
    return cls(f"Error code: {status} - {body}", response=resp, body=body)


class _Clock:
    """A clock that only moves when the code under test sleeps: no test waits for real."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Embedder:
    """embed() raises each queued exception in turn, then answers with a vector."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.calls = 0

    def embed(self, text, memory_action=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return [0.1, 0.2, 0.3]


TEXT = "a finalized episode summary " * 4


def _retry(embedder, clock, **kw):
    return ee.embed_with_cold_retry(embedder, TEXT, sleep=clock.sleep, clock=clock.monotonic, **kw)


# --- what is retried -------------------------------------------------------------------------

def test_a_cold_500_start_failure_is_retried_once_and_then_succeeds():
    clock, emb = _Clock(), _Embedder(_http_status_error(500, START_FAILURE))
    assert _retry(emb, clock) == [0.1, 0.2, 0.3]
    assert emb.calls == 2
    assert len(clock.sleeps) == 1 and clock.sleeps[0] >= 10


def test_a_503_is_retried_once_and_then_succeeds():
    clock, emb = _Clock(), _Embedder(_http_status_error(503, {"error": "loading"}))
    assert _retry(emb, clock) == [0.1, 0.2, 0.3]
    assert emb.calls == 2 and len(clock.sleeps) == 1 and clock.sleeps[0] >= 10


def test_a_refused_connection_is_retried_once_and_then_succeeds():
    clock, emb = _Clock(), _Embedder(httpx.ConnectError("refused"))
    assert _retry(emb, clock) == [0.1, 0.2, 0.3]
    assert emb.calls == 2 and len(clock.sleeps) == 1 and clock.sleeps[0] >= 10


def test_the_openai_client_error_types_classify_the_same_way():
    """The real write path goes through the openai SDK, whose errors are not httpx's."""
    openai = pytest.importorskip("openai")
    for exc in (_openai_status_error(500, START_FAILURE), _openai_status_error(503, {"error": "x"}),
                openai.APIConnectionError(request=_REQ), openai.APITimeoutError(request=_REQ)):
        clock, emb = _Clock(), _Embedder(exc)
        assert _retry(emb, clock) == [0.1, 0.2, 0.3], type(exc).__name__
        assert emb.calls == 2 and len(clock.sleeps) == 1


# --- what is NOT retried ---------------------------------------------------------------------

def test_a_context_overflow_500_is_not_retried():
    """Replaying a request that can never fit only doubles the damage (retry_later's own rule)."""
    clock, exc = _Clock(), _http_status_error(500, CTX_OVERFLOW)
    emb = _Embedder(exc)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _retry(emb, clock)
    assert ei.value is exc and emb.calls == 1 and clock.sleeps == []


def test_the_openai_context_overflow_500_is_not_retried_either():
    clock, emb = _Clock(), _Embedder(_openai_status_error(500, CTX_OVERFLOW))
    with pytest.raises(Exception) as ei:
        _retry(emb, clock)
    assert type(ei.value).__name__ == "InternalServerError" and emb.calls == 1 and clock.sleeps == []


def test_a_plain_error_is_raised_at_once():
    clock, emb = _Clock(), _Embedder(RuntimeError("embedder exploded"))
    with pytest.raises(RuntimeError):
        _retry(emb, clock)
    assert emb.calls == 1 and clock.sleeps == []


def test_a_4xx_is_not_an_outage():
    clock, emb = _Clock(), _Embedder(_http_status_error(400, {"error": "bad request"}))
    with pytest.raises(httpx.HTTPStatusError):
        _retry(emb, clock)
    assert emb.calls == 1 and clock.sleeps == []


# --- the bounds ------------------------------------------------------------------------------

def test_a_seat_that_never_comes_up_is_given_up_on_after_the_delays_and_the_cold_error_surfaces():
    clock = _Clock()
    emb = _Embedder(*[httpx.ConnectError("refused")] * 10)
    with pytest.raises(httpx.ConnectError) as ei:
        _retry(emb, clock)
    assert emb.calls == 3, "one try plus one per delay (10 s, 20 s), never an open-ended loop"
    assert clock.sleeps == [10, 20]
    assert ee.is_cold_embed_error(ei.value), "the caller tells 'still cold' from 'broken' by the exception it gets"


def test_the_budget_caps_the_sleeping_it_never_sleeps_past_it():
    clock = _Clock()
    emb = _Embedder(*[httpx.ConnectError("refused")] * 10)
    with pytest.raises(httpx.ConnectError):
        _retry(emb, clock, budget_s=15)
    assert clock.sleeps == [10], "the 20 s wait would end at 30 s, past the 15 s budget: give up instead"
    assert emb.calls == 2
    assert clock.now <= 15


def test_a_zero_budget_makes_the_first_failure_final():
    clock, emb = _Clock(), _Embedder(*[httpx.ConnectError("refused")] * 3)
    with pytest.raises(httpx.ConnectError):
        _retry(emb, clock, budget_s=0)
    assert emb.calls == 1 and clock.sleeps == []


def test_the_wait_is_the_longer_of_retry_after_and_the_configured_delay():
    from embedder_503 import RETRY_AFTER_S
    clock = _Clock()
    emb = _Embedder(httpx.ConnectError("x"), httpx.ConnectError("x"))
    _retry(emb, clock, delays=(1, RETRY_AFTER_S + 25))
    assert clock.sleeps == [RETRY_AFTER_S, RETRY_AFTER_S + 25]


def test_a_non_cold_error_after_a_cold_one_stops_the_retrying():
    clock = _Clock()
    emb = _Embedder(httpx.ConnectError("refused"), RuntimeError("now it is broken"))
    with pytest.raises(RuntimeError):
        _retry(emb, clock)
    assert emb.calls == 2 and clock.sleeps == [10]


def test_empty_text_makes_no_embedder_call():
    clock, emb = _Clock(), _Embedder(httpx.ConnectError("never reached"))
    assert ee.embed_with_cold_retry(emb, "   ", sleep=clock.sleep, clock=clock.monotonic) is None
    assert emb.calls == 0


def test_the_module_stays_import_light():
    """embedder_503 pulls fastapi: it must be imported inside the function, not at module scope."""
    import ast
    src = (Path(__file__).resolve().parent.parent / "episode_embeddings.py").read_text(encoding="utf-8")
    top = [n for n in ast.parse(src).body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in top if isinstance(n, ast.ImportFrom)}
    assert "embedder_503" not in names and not any((m or "").startswith(("fastapi", "httpx")) for m in names)


def test_the_egemma_429_retry_contract_is_not_widened():
    """The cold retry sits ABOVE the embedder: the shim's own retry still retries 429 only."""
    src = (Path(__file__).resolve().parent.parent / "egemma_embedder.py").read_text(encoding="utf-8")
    assert "embedder_503" not in src and "retry_later" not in src


# --- the finalize-time background retry (create_episode) -------------------------------------
#
# POST /v1/episodes commits the episode, then embeds its summary. The hook that posts it gives up
# after 5 s, so a cold embedder is never waited on in the request: the response is unchanged and the
# retry is scheduled as a FastAPI background task, capped so a burst of finalizes during an outage
# cannot pile threads up (one in flight per episode id, a small global cap). Over the cap the daily
# upkeep step (episodic-reconcile --upkeep) picks the vector up.

import logging  # noqa: E402

PAYLOAD = {"brand": None, "goal": "g", "summary": "s"}


def _gate_logger_records(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "mem0-server"]


def test_the_gate_allows_one_retry_per_episode_and_a_small_global_number():
    gate = ee.DeferredEmbedGate(cap=4)
    assert gate.acquire(1) is True
    assert gate.acquire(1) is False, "a second retry for the same episode must not stack"
    assert [gate.acquire(i) for i in (2, 3, 4)] == [True, True, True]
    assert gate.in_flight == 4
    assert gate.acquire(5) is False, "over the cap: leave it to the daily step"
    gate.release(2)
    assert gate.acquire(5) is True
    gate.release(999)  # releasing what was never held is harmless
    assert gate.in_flight == 4


def test_the_default_cap_is_four():
    gate = ee.DeferredEmbedGate()
    assert [gate.acquire(i) for i in range(6)] == [True] * 4 + [False] * 2


def test_the_deferred_embed_recovers_after_a_cold_start_and_upserts_the_same_payload(caplog):
    caplog.set_level(logging.INFO, logger="mem0-server")
    gate, clock = ee.DeferredEmbedGate(), _Clock()
    assert gate.acquire(7)
    emb, written = _Embedder(_http_status_error(500, START_FAILURE)), []
    ee.run_deferred_embed(emb, lambda ep, vec, payload: written.append((ep, vec, payload)), gate, 7, TEXT,
                          PAYLOAD, sleep=clock.sleep, clock=clock.monotonic)
    assert written == [(7, [0.1, 0.2, 0.3], PAYLOAD)]
    assert "episode embed recovered ep=7" in _gate_logger_records(caplog)
    assert gate.in_flight == 0, "the slot is released once the task is done"


def test_the_deferred_embed_gives_up_loudly_when_the_seat_never_comes_up(caplog):
    caplog.set_level(logging.INFO, logger="mem0-server")
    gate, clock = ee.DeferredEmbedGate(), _Clock()
    gate.acquire(8)
    written = []
    ee.run_deferred_embed(_Embedder(*[httpx.ConnectError("refused")] * 9), lambda *a: written.append(a), gate, 8,
                          TEXT, PAYLOAD, sleep=clock.sleep, clock=clock.monotonic)
    assert written == [] and clock.sleeps == [10, 20]
    msgs = _gate_logger_records(caplog)
    assert any(m.startswith("episode embed gave up ep=8") for m in msgs), msgs
    assert gate.in_flight == 0


def test_the_deferred_embed_releases_its_slot_when_the_upsert_itself_fails(caplog):
    caplog.set_level(logging.INFO, logger="mem0-server")
    gate, clock = ee.DeferredEmbedGate(), _Clock()
    gate.acquire(9)

    def boom(*a):
        raise RuntimeError("qdrant is down")

    ee.run_deferred_embed(_Embedder(), boom, gate, 9, TEXT, PAYLOAD, sleep=clock.sleep, clock=clock.monotonic)
    assert any(m.startswith("episode embed gave up ep=9") for m in _gate_logger_records(caplog))
    assert gate.in_flight == 0


def test_a_non_cold_error_is_not_retried_in_the_background_either(caplog):
    caplog.set_level(logging.INFO, logger="mem0-server")
    gate, clock = ee.DeferredEmbedGate(), _Clock()
    gate.acquire(10)
    emb = _Embedder(_http_status_error(500, CTX_OVERFLOW))
    ee.run_deferred_embed(emb, lambda *a: None, gate, 10, TEXT, PAYLOAD, sleep=clock.sleep, clock=clock.monotonic)
    assert emb.calls == 1 and clock.sleeps == []
    assert gate.in_flight == 0


APP = Path(__file__).resolve().parent.parent / "app.py"


def _create_episode_source() -> str:
    src = APP.read_text(encoding="utf-8")
    start = src.index("def create_episode(")
    return src[start:src.index('@app.post("/v1/episodes/search")', start)]


def test_create_episode_schedules_the_retry_in_the_background_and_leaves_the_response_alone():
    """app.py cannot be imported headless (it builds the live Memory client): pin the wiring in its
    source, in the style of test_bundle_self_echo's handler pin."""
    src = APP.read_text(encoding="utf-8")
    sig = src[src.index("def create_episode("):]
    sig = sig[:sig.index(":\n")]
    assert "background_tasks: BackgroundTasks" in sig, sig
    body = _create_episode_source()
    assert 'return {"ok": True, "session_id": b.session_id, "episode_id": episode_id}' in body, \
        "the response is unchanged: the hook that posts it times out at 5 s"
    embed_at = body.index("embed_episode_summary(")
    handler = body[body.index("except Exception as e:", embed_at):]      # the embed's own except branch
    assert "_embedder_503.retry_later(e)" in handler, "only a cold-shaped failure is deferred"
    assert "_episode_embed_gate.acquire(episode_id)" in handler, "capped: one per episode, a small global number"
    assert "background_tasks.add_task(" in handler and "run_deferred_embed" in handler
    assert "episode embed deferred ep=%s" in handler
    assert handler.count("\"episode embed deferred ep=%s") == 1, "an over-cap refusal must not read as a scheduled deferral"
    assert "episode embed not deferred ep=%s (retry cap)" in handler
    assert "episode embed deferral failed" in handler, "scheduling is itself fail-soft: it may never fail the write"
    assert "embed_with_cold_retry(" not in body and "time.sleep" not in body, "nothing waits inside the request"


def test_app_builds_one_module_level_gate_with_the_helpers_it_uses():
    src = APP.read_text(encoding="utf-8")
    assert "_episode_embed_gate = DeferredEmbedGate()" in src
    imp = src[src.index("from episode_embeddings import ("):]
    imp = imp[:imp.index(")\n")]
    assert "DeferredEmbedGate" in imp and "run_deferred_embed" in imp
