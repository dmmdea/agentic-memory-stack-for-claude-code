"""Support-tier resilience (embedder + reranker behind llama-swap): the headless half.

Covers the pure/HTTP-level behavior with a FAKE upstream (a loopback http.server), so it runs on
a clean CI runner with no live stack:

  * an embedder that cannot start is "retry later" (503 + Retry-After), not a failed write;
  * a cold reranker gets one longer retry before the dense-only fallback;
  * llama-swap's model listing is read in both the flat and the v256 (`status.value`) shapes;
  * the one-document rerank warm-up used by the SessionStart pre-warm.

The route wiring in app.py (`import app` builds the live Memory client) is pinned by the
local-only suites test_embedder_503.py and test_upstream_rate_limit_status.py, plus the
source-level guard at the bottom of this file, which does run headless.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embedder_503 as e5  # noqa: E402
import reranker as rr  # noqa: E402

START_FAILURE = {"error": {"message": "unspecific error: upstream command exited prematurely",
                           "type": "server_error"}}


class _Fake:
    """A loopback stand-in for llama-swap. `plan` is consumed one entry per POST /v1/rerank;
    an entry is (delay_seconds, status, body). /v1/embeddings answers `embed` = (status, body)."""

    def __init__(self):
        self.embed = (200, {"data": [{"embedding": [0.0] * 4}]})
        self.plan: list[tuple[float, int, object]] = []
        self.rerank_calls: list[dict] = []
        self.models = {"data": []}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep pytest output pristine
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up on a slow answer: exactly the case under test

            def do_GET(self):
                self._send(200, fake.models)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/v1/embeddings":
                    self._send(*fake.embed)
                    return
                fake.rerank_calls.append(body)
                delay, status, out = fake.plan.pop(0) if fake.plan else (0.0, 200, None)
                if delay:
                    time.sleep(delay)
                if out is None:
                    out = {"results": [{"index": i, "relevance_score": float(i)}
                                       for i in range(len(body.get("documents", [])))]}
                self._send(status, out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fake():
    f = _Fake()
    yield f
    f.close()


@pytest.fixture()
def clean_stats():
    saved = dict(rr.rerank_stats)
    rr.rerank_stats.update(last_rerank_ok_ts=None, consecutive_rerank_failures=0,
                           ok_total=0, fail_total=0, last_error=None, cold_retry_total=0)
    yield rr.rerank_stats
    rr.rerank_stats.clear()
    rr.rerank_stats.update(saved)


DOCS = [{"memory": "a"}, {"memory": "b"}, {"memory": "c"}, {"memory": "d"}]


# --------------------------------------------------------------------------- 7.1
def _embed_error(fake, status, body):
    """The exception mem0's write path meets: the embedder call against the fake upstream."""
    fake.embed = (status, body)
    r = httpx.post(fake.base + "/v1/embeddings", json={"model": "m", "input": "x"})
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("fake upstream did not fail")


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_statuses_are_retry_later(fake, status):
    exc = _embed_error(fake, status, {"error": {"message": "bad gateway"}})
    assert e5.retry_later(exc) == e5.RETRY_AFTER_S


def test_start_failure_500_is_retry_later(fake):
    """llama-swap answers 500 'upstream command exited prematurely' when llama-server dies at
    load (no VRAM headroom beside a resident seat). That is 'not now', not a failed write."""
    exc = _embed_error(fake, 500, START_FAILURE)
    assert e5.retry_later(exc) == e5.RETRY_AFTER_S


def test_start_failure_through_the_openai_client_is_retry_later(fake):
    """The real write path goes through the openai SDK; its InternalServerError must classify too."""
    openai = pytest.importorskip("openai")
    fake.embed = (500, START_FAILURE)
    client = openai.OpenAI(base_url=fake.base + "/v1", api_key="x", max_retries=0, timeout=5)
    with pytest.raises(openai.InternalServerError) as ei:
        client.embeddings.create(model="m", input="x")
    assert e5.retry_later(ei.value) == e5.RETRY_AFTER_S


def test_other_500s_stay_loud(fake):
    """A ctx-overflow or a coding error must never be advertised as retryable: replaying it
    only doubles the damage (see test_upstream_rate_limit_status.py)."""
    exc = _embed_error(fake, 500, {"error": {"message": "input is too large: 2300 tokens > 2048"}})
    assert e5.retry_later(exc) is None
    for other in (ValueError("context overflow"), RuntimeError("qdrant unreachable"), KeyError("k")):
        assert e5.retry_later(other) is None


@pytest.mark.parametrize("status", [400, 401, 404])
def test_client_errors_are_not_outages(fake, status):
    assert e5.retry_later(_embed_error(fake, status, {"error": {"message": "nope"}})) is None


def test_unreachable_upstream_is_retry_later():
    assert e5.retry_later(httpx.ConnectError("refused")) == e5.RETRY_AFTER_S
    assert e5.retry_later(httpx.ReadTimeout("slow")) == e5.RETRY_AFTER_S


# --------------------------------------------------------------------------- 7.2
@pytest.fixture()
def rerank_env(fake, clean_stats, monkeypatch):
    monkeypatch.setattr(rr, "RERANK_URL", fake.base + "/v1/rerank")
    monkeypatch.setattr(rr, "RERANK_TIMEOUT_S", 0.4)
    monkeypatch.setattr(rr, "RERANK_COLD_RETRY_TIMEOUT_S", 3.0)
    monkeypatch.setattr(rr, "RERANK_RETRY_MIN_S", 0.1)   # scaled with the sub-second timeouts above
    return fake


def test_shipped_timeouts():
    assert rr.RERANK_TIMEOUT_S == 8.0
    assert rr.RERANK_COLD_RETRY_TIMEOUT_S == 20.0
    assert rr.RERANK_TOTAL_BUDGET_S == 20.0
    # the retry needs real room after the first attempt, or the cold-start allowance is a fiction
    assert rr.RERANK_TOTAL_BUDGET_S - rr.RERANK_TIMEOUT_S >= 5.0


def _caller_read_budgets() -> dict[str, float]:
    """The wall-clock budgets of the callers that search WITH rerank, read from their source."""
    import re
    root = Path(__file__).resolve().parents[2]
    shim = (root / "scripts/wsl/mem0-mcp-shim.py").read_text(encoding="utf-8")
    tms = (root / "scripts/windows/Test-MemoryStack.ps1").read_text(encoding="utf-8")
    m_shim = re.search(r"^_READ_TIMEOUT\s*=\s*([0-9.]+)", shim, re.M)
    m_tms = re.search(r"rerank=\$true\}.*?-TimeoutSec\s+(\d+)", tms, re.S)
    assert m_shim and m_tms, "caller timeout lines moved: update this pin, do not drop it"
    return {"mcp shim read timeout": float(m_shim.group(1)),
            "Test-MemoryStack rerank=True search": float(m_tms.group(1))}


def test_rerank_budget_fits_every_caller_timeout():
    """A read timeout is not a failover in the shim: a search that outlives the caller turns the
    graceful dense-order fallback into a caller-side error. Worst case = a cold embed (measured
    ~3.4 s) + the vector/lexical legs + the whole rerank budget; keep 8 s of room for the non-rerank
    part so the pin fails when either side moves."""
    non_rerank_allowance_s = 8.0
    for name, budget in _caller_read_budgets().items():
        assert rr.RERANK_TOTAL_BUDGET_S + non_rerank_allowance_s <= budget, name


def test_total_rerank_time_is_bounded_by_the_budget(rerank_env, clean_stats, monkeypatch):
    monkeypatch.setattr(rr, "RERANK_TOTAL_BUDGET_S", 0.9)   # first attempt 0.4 s -> retry gets ~0.5 s, not 3.0 s
    rerank_env.plan = [(3.0, 200, None), (3.0, 200, None)]
    st: dict = {}
    t0 = time.perf_counter()
    out = rr.rerank("q", DOCS, status_out=st)
    took = time.perf_counter() - t0
    assert st["status"] == "failed_fallback_dense"
    assert [d["memory"] for d in out] == ["a", "b", "c", "d"]
    assert len(rerank_env.rerank_calls) == 2
    assert took < 1.4, f"rerank stage took {took:.2f}s against a 0.9 s budget"


def test_no_retry_when_the_budget_is_already_spent(rerank_env, clean_stats, monkeypatch):
    monkeypatch.setattr(rr, "RERANK_TOTAL_BUDGET_S", 0.4)   # nothing left after the first 0.4 s attempt
    rerank_env.plan = [(1.0, 200, None), (0.0, 200, None)]
    st: dict = {}
    rr.rerank("q", DOCS, status_out=st)
    assert st["status"] == "failed_fallback_dense"
    assert len(rerank_env.rerank_calls) == 1, "no budget left: fall back at once, do not start a doomed retry"
    assert clean_stats["cold_retry_total"] == 0 and clean_stats["fail_total"] == 1


def test_warm_reranker_first_attempt_is_plain_ran(rerank_env):
    st: dict = {}
    out = rr.rerank("q", DOCS, status_out=st)
    assert st["status"] == "ran"
    assert len(rerank_env.rerank_calls) == 1
    assert [d["memory"] for d in out] == ["d", "c", "b", "a"]


def test_cold_reranker_gets_one_longer_retry(rerank_env, clean_stats):
    rerank_env.plan = [(1.0, 200, None), (0.0, 200, None)]  # first answer is later than the 0.4 s timeout
    st: dict = {}
    out = rr.rerank("q", DOCS, status_out=st)
    assert st["status"] == "ok-after-cold-retry"
    assert len(rerank_env.rerank_calls) == 2
    assert [d["memory"] for d in out] == ["d", "c", "b", "a"], "the retry's scores must be applied"
    assert clean_stats["ok_total"] == 1 and clean_stats["fail_total"] == 0
    assert clean_stats["consecutive_rerank_failures"] == 0
    assert clean_stats["cold_retry_total"] == 1


def test_retry_that_also_times_out_falls_back_to_dense_order(rerank_env, clean_stats, monkeypatch):
    monkeypatch.setattr(rr, "RERANK_COLD_RETRY_TIMEOUT_S", 0.6)
    rerank_env.plan = [(1.5, 200, None), (1.5, 200, None)]
    st: dict = {}
    out = rr.rerank("q", DOCS, status_out=st)
    assert st["status"] == "failed_fallback_dense"
    assert [d["memory"] for d in out] == ["a", "b", "c", "d"]
    assert len(rerank_env.rerank_calls) == 2, "exactly one retry, never a loop"
    assert clean_stats["fail_total"] == 1, "one search is one failure, not two"
    assert clean_stats["consecutive_rerank_failures"] == 1


def test_only_a_read_timeout_is_retried(rerank_env):
    rerank_env.plan = [(0.0, 500, START_FAILURE), (0.0, 200, None)]
    st: dict = {}
    rr.rerank("q", DOCS, status_out=st)
    assert st["status"] == "failed_fallback_dense"
    assert len(rerank_env.rerank_calls) == 1, "a start failure is not a cold load: no retry"


def test_connect_error_is_not_retried(monkeypatch, clean_stats):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(rr.httpx, "post", boom)
    st: dict = {}
    rr.rerank("q", DOCS, status_out=st)
    assert st["status"] == "failed_fallback_dense" and len(calls) == 1


def test_cold_retry_status_counts_as_ran():
    assert "ran" in rr.RAN_STATUSES and "ok-after-cold-retry" in rr.RAN_STATUSES
    assert "failed_fallback_dense" not in rr.RAN_STATUSES


# --------------------------------------------------------------------------- 7.3 (warm)
def test_warm_issues_a_one_document_rerank(rerank_env, clean_stats):
    res = rr.warm()
    assert res["ok"] is True and isinstance(res["warm_ms"], int)
    assert len(rerank_env.rerank_calls) == 1
    assert len(rerank_env.rerank_calls[0]["documents"]) == 1
    assert rerank_env.rerank_calls[0]["model"] == rr.RERANK_MODEL
    assert clean_stats["ok_total"] == 0, "a warm-up is not search traffic: the passive counters stay put"


def test_warm_waits_for_a_cold_load_with_the_long_timeout(rerank_env):
    rerank_env.plan = [(0.9, 200, None)]  # slower than the 0.4 s first-attempt timeout
    assert rr.warm()["ok"] is True


def test_warm_never_raises(rerank_env, clean_stats):
    rerank_env.plan = [(0.0, 500, START_FAILURE)]
    res = rr.warm()
    assert res["ok"] is False and "error" in res
    assert clean_stats["fail_total"] == 0


# --------------------------------------------------------------------------- 7.3 (listing)
@pytest.mark.parametrize("entry,expect", [
    ({"id": "m", "state": "loaded"}, True),                     # flat, old schema
    ({"id": "m", "state": "ready"}, True),
    ({"id": "m", "status": "running"}, True),                   # flat `status` string
    ({"id": "m", "state": "stopped"}, False),
    ({"id": "m", "status": {"value": "loaded"}}, True),         # llama-swap >= v256
    ({"id": "m", "status": {"value": "ready"}}, True),
    ({"id": "m", "status": {"value": "unloaded"}}, False),
    ({"id": "m", "status": {"value": "stopped"}}, False),
    ({"id": "m", "status": {}}, None),                          # dict with no value: unknown
    ({"id": "m"}, None),
])
def test_listing_state_reads_both_schemas(entry, expect):
    assert e5.listing_loaded(entry) is expect


# --------------------------------------------------------------------------- app wiring
def test_app_routes_unclassified_errors_through_the_start_failure_mapper():
    """`import app` cannot run headless; pin the wiring at the source level."""
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert "_embedder_503.retry_later(e)" in src
    assert "listing_loaded" in src and "_rerank_warm" in src
    assert '_st.get("status") in _RERANK_RAN_STATUSES' in src, "the diagnose probe must count a cold-retry rerank as ran"
    assert '== "ran"' not in src
