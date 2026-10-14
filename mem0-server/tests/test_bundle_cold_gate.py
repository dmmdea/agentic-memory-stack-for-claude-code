"""The per-prompt bundle abstains instead of timing out while the embedder is unloaded.

llama-swap holds a request for an unloaded model until the model has loaded (it does not refuse it),
so a cold embedder made the bundle's dense search wait out the whole load: 5.75 s measured against a
0.38 s warm call, past the daemon's 3 s bundle request and the hook's 5 s timeout. In real use that cost the [MEMORY CONTEXT] block on
22-27 % of typed prompts. The bundle now reads llama-swap's model listing first; an embedder listed
as not loaded gets one background warm and that prompt's bundle skips the search.

The decision lives in ``embedder_503`` (headless: httpx + fastapi only) and is tested here directly.
The RUNTIME tests drive ``app.context_bundle`` with its collaborators patched; they need
``import app`` (the mem0 package), so they skip in the headless lane and gate in the live suite.
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import embedder_503 as e5  # noqa: E402

MODEL = "embeddinggemma2"


@pytest.fixture(autouse=True)
def _fresh_warm_state():
    e5._warm_state.update({"running": False, "started_at": None, "last_ok_at": None,
                           "last_error": None, "started": 0})
    yield


def _listing(*entries):
    return lambda: list(entries)


def _boom():
    raise RuntimeError("listing unreachable")


def _run_now(fn):
    fn()


# ---------------------------------------------------------------------------
# embedder_listed_ready
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry,expected", [
    ({"id": MODEL, "status": {"value": "loaded"}}, True),      # llama-swap >= v256
    ({"id": MODEL, "status": {"value": "unloaded"}}, False),
    ({"id": MODEL, "status": {"value": "starting"}}, False),   # loading counts as not ready
    ({"id": MODEL, "state": "ready"}, True),                    # the flat schema
    ({"id": MODEL, "state": "stopped"}, False),
    ({"id": MODEL}, None),                                      # a server that reports no state
    ({"id": MODEL, "status": {"value": "warm-ish"}}, None),     # a state word nobody taught us
    ({"id": MODEL, "state": "active"}, None),                   # unknown searches, never suppresses
])
def test_listed_ready_reads_both_listing_schemas(entry, expected):
    assert e5.embedder_listed_ready(_listing({"id": "other", "status": {"value": "loaded"}}, entry), MODEL) is expected


def test_listed_ready_is_unknown_when_the_model_is_absent_or_the_listing_fails():
    assert e5.embedder_listed_ready(_listing({"id": "other", "state": "ready"}), MODEL) is None
    assert e5.embedder_listed_ready(_boom, MODEL) is None
    assert e5.embedder_listed_ready(lambda: None, MODEL) is None


# ---------------------------------------------------------------------------
# bundle_embed_gate
# ---------------------------------------------------------------------------

def test_a_loaded_embedder_searches_and_starts_no_warm():
    warms = []
    got = e5.bundle_embed_gate(_listing({"id": MODEL, "status": {"value": "loaded"}}),
                               lambda: warms.append(1), MODEL, spawn=_run_now)
    assert got == {"embed": True, "state": "loaded"}
    assert warms == []


def test_an_unreadable_listing_keeps_the_old_behaviour():
    warms = []
    got = e5.bundle_embed_gate(_boom, lambda: warms.append(1), MODEL, spawn=_run_now)
    assert got == {"embed": True, "state": "unknown"}
    assert warms == []


def test_an_unloaded_embedder_skips_the_search_and_warms_it_once():
    warms = []
    got = e5.bundle_embed_gate(_listing({"id": MODEL, "status": {"value": "unloaded"}}),
                               lambda: warms.append(1), MODEL, spawn=_run_now)
    assert got == {"embed": False, "state": "warming", "warm_started": True}
    assert warms == [1]
    st = e5.warm_state()
    assert st["running"] is False and st["started"] == 1 and st["last_ok_at"] is not None
    assert st["last_error"] is None


def test_a_running_warm_is_not_started_twice():
    pending = []          # a spawn that has not run its thread yet: the warm is still in flight
    cold = _listing({"id": MODEL, "status": {"value": "unloaded"}})
    first = e5.bundle_embed_gate(cold, lambda: None, MODEL, spawn=pending.append)
    second = e5.bundle_embed_gate(cold, lambda: None, MODEL, spawn=pending.append)
    assert first["warm_started"] is True and second["warm_started"] is False
    assert second["embed"] is False
    assert len(pending) == 1
    pending[0]()          # the thread finishes; the next cold prompt may warm again
    third = e5.bundle_embed_gate(cold, lambda: None, MODEL, spawn=pending.append)
    assert third["warm_started"] is True


def test_a_failed_warm_is_recorded_logged_and_frees_the_slot(caplog):
    def _fail():
        raise ConnectionError("seat could not start")
    with caplog.at_level("WARNING", logger="mem0-server.embedder_503"):
        got = e5.bundle_embed_gate(_listing({"id": MODEL, "state": "stopped"}), _fail, MODEL, spawn=_run_now)
    assert got["embed"] is False and got["warm_started"] is True
    st = e5.warm_state()
    assert st["last_error"] == "ConnectionError: seat could not start" and st["running"] is False
    assert any("seat could not start" in r.getMessage() for r in caplog.records)


def test_a_spawn_failure_never_leaves_the_slot_held():
    def _no_threads(fn):
        raise RuntimeError("can't start new thread")
    got = e5.bundle_embed_gate(_listing({"id": MODEL, "state": "stopped"}), lambda: None, MODEL,
                               spawn=_no_threads)
    assert got == {"embed": False, "state": "warming", "warm_started": False}
    assert e5.warm_state()["running"] is False


# ---------------------------------------------------------------------------
# bundle_gate_for: the HTTP binding app._bundle_embed_gate delegates to (headless)
# ---------------------------------------------------------------------------

class _FakeHttp:
    """Records each call; `listing` is the /v1/models `data` array it serves."""

    def __init__(self, listing):
        self.listing = listing
        self.calls = []

    def _resp(self, payload):
        class _R:
            def raise_for_status(self_inner):
                return None

            def json(self_inner):
                return payload
        return _R()

    def get(self, url, timeout=None):
        self.calls.append(("GET", url, timeout))
        return self._resp({"data": self.listing})

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, timeout, json))
        return self._resp({"data": [{"embedding": [0.0]}]})


BASE = "http://127.0.0.1:11436/v1"


def test_gate_for_reads_the_listing_fast_and_warms_the_named_model():
    http = _FakeHttp([{"id": MODEL, "status": {"value": "unloaded"}}])
    got = e5.bundle_gate_for(BASE, MODEL, environ={}, http=http, spawn=_run_now)
    assert got == {"embed": False, "state": "warming", "warm_started": True}
    assert http.calls[0] == ("GET", f"{BASE}/models", 0.5)
    assert http.calls[1] == ("POST", f"{BASE}/embeddings", 60.0, {"model": MODEL, "input": "warm"})


def test_gate_for_searches_when_loaded():
    http = _FakeHttp([{"id": MODEL, "status": {"value": "loaded"}}])
    assert e5.bundle_gate_for(BASE, MODEL, environ={}, http=http, spawn=_run_now) == \
        {"embed": True, "state": "loaded"}
    assert [c[0] for c in http.calls] == ["GET"]


@pytest.mark.parametrize("value", ["0", " 0 "])
def test_gate_for_is_off_when_the_operator_says_so(value):
    http = _FakeHttp([{"id": MODEL, "status": {"value": "unloaded"}}])
    got = e5.bundle_gate_for(BASE, MODEL, environ={"MEM0_BUNDLE_COLD_DEGRADE": value}, http=http,
                             spawn=_run_now)
    assert got == {"embed": True, "state": "disabled"}
    assert http.calls == [], "a disabled gate must not even read the listing"


# ---------------------------------------------------------------------------
# WIRING pinned in source: app.py cannot be imported headless, so CI would skip the runtime tests
# below; these fail the headless lane when a guard is reverted. The runtime tests prove behaviour.
# ---------------------------------------------------------------------------

def _bundle_handler_src() -> str:
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    start = src.index("def context_bundle(")
    return src[start:src.index('@app.post("/v1/episodes")', start)]


def test_only_the_hook_path_probes_the_embedder():
    body = _bundle_handler_src()
    assert '_gate = _bundle_embed_gate() if b.checkpoint else {"embed": True, "state": "deliberate"}' in body


def test_a_failing_probe_searches_and_a_cold_one_skips_search_and_raw_fallback():
    body = _bundle_handler_src()
    probe = body[body.index("_gate = _bundle_embed_gate()"):]
    assert probe.index("except Exception:") < probe.index('_gate = {"embed": True, "state": "unknown"}')
    assert "    if not _cold:\n        try:\n            filters" in body, "the dense search must sit behind _cold"
    fallback = [ln for ln in body.splitlines() if "RAW_FALLBACK_ENABLED and" in ln]
    assert fallback and all("not _cold" in ln for ln in fallback), fallback


def test_the_app_gate_delegates_to_the_tested_binding():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    fn = src[src.index("def _bundle_embed_gate("):]
    fn = fn[:fn.index("\ndef ")]
    assert "_embedder_503.bundle_gate_for(_embed_base(), _embed_model())" in fn


# ---------------------------------------------------------------------------
# RUNTIME: app.context_bundle with its collaborators patched (live-stack venv only)
# ---------------------------------------------------------------------------

def _app_module():
    try:
        import app
    except Exception as e:  # noqa: BLE001 — any import failure is a skip
        pytest.skip(f"needs the live-stack venv (app import failed: "
                    f"{type(e).__name__}: {str(e)[:60]}) — runs in the live suite")
    return app


@pytest.fixture
def wired_app(monkeypatch):
    app = _app_module()
    calls = {"checkpoint": 0, "search": 0, "raw_fallback": 0, "goals": 0}

    def _checkpoint(b):
        calls["checkpoint"] += 1
        return {"ok": True, "episode_id": 7, "action": "updated", "state": "in_progress"}

    def _search(si, _route="search"):
        calls["search"] += 1
        return {"results": [{"id": "m1", "memory": "alpha fact", "metadata": {"tier": "evidence"}}]}

    def _goals(conn, **kw):
        calls["goals"] += 1
        return [{"id": 1, "title": "a goal", "priority": 2, "status": "open"}]

    def _raw(prompt, brand):
        calls["raw_fallback"] += 1
        return None

    monkeypatch.setattr(app, "auth", lambda *a, **k: None)
    monkeypatch.setattr(app, "_checkpoint_core", _checkpoint)
    monkeypatch.setattr(app, "_search_core", _search)
    monkeypatch.setattr(app, "_episodic_connect", lambda *a, **k: contextlib.nullcontext(None))
    monkeypatch.setattr(app, "_episodic_list_goals", _goals)
    monkeypatch.setattr(app, "_episodic_list_open_questions", lambda conn, **kw: [])
    monkeypatch.setattr(app, "_episode_raw_fallback", _raw)
    return app, calls


def _body(app):
    return app.ContextBundleIn(session_id="cold-gate-test", prompt="how does the outbox replay a queued write",
                               brand=None, workspace=None, project=None)


def test_cold_bundle_abstains_but_keeps_checkpoint_and_goals(wired_app, monkeypatch):
    app, calls = wired_app
    monkeypatch.setattr(app, "_bundle_embed_gate",
                        lambda: {"embed": False, "state": "warming", "warm_started": True})
    app._bundle_cold_bump()                      # roll the date first so the delta is today's
    before = app._bundle_cold_today["degraded"]
    out = app.context_bundle(_body(app), x_api_key="k")
    assert out["memories"] == [] and "raw_fallback" not in out
    assert out["embedder"] == {"state": "warming", "warm_started": True}
    assert calls == {"checkpoint": 1, "search": 0, "raw_fallback": 0, "goals": 1}
    assert app._bundle_cold_today["degraded"] == before + 1


def test_warm_bundle_searches_as_before(wired_app, monkeypatch):
    app, calls = wired_app
    monkeypatch.setattr(app, "_bundle_embed_gate", lambda: {"embed": True, "state": "loaded"})
    out = app.context_bundle(_body(app), x_api_key="k")
    assert [m["id"] for m in out["memories"]] == ["m1"]
    assert "embedder" not in out
    assert calls["search"] == 1


def test_a_failing_readiness_check_searches_as_before(wired_app, monkeypatch):
    app, calls = wired_app

    def _broken():
        raise RuntimeError("boom")
    monkeypatch.setattr(app, "_bundle_embed_gate", _broken)
    out = app.context_bundle(_body(app), x_api_key="k")
    assert calls["search"] == 1 and [m["id"] for m in out["memories"]] == ["m1"]


def test_a_deliberate_recall_never_degrades_on_a_cold_embedder(wired_app, monkeypatch):
    """memory_recall and the session-start precis send checkpoint=False and wait for the load
    (the shim reads for up to 30 s): they must search (and wait), never return an empty 'nothing stored'."""
    app, calls = wired_app
    probes = []

    def _cold_gate():
        probes.append(1)
        return {"embed": False, "state": "warming", "warm_started": True}
    monkeypatch.setattr(app, "_bundle_embed_gate", _cold_gate)
    body = app.ContextBundleIn(session_id="mcp-memory-recall", prompt="how does the outbox replay a queued write",
                               brand=None, workspace=None, project=None, checkpoint=False)
    out = app.context_bundle(body, x_api_key="k")
    assert [m["id"] for m in out["memories"]] == ["m1"]
    assert "embedder" not in out
    assert calls["search"] == 1 and probes == [], "the readiness probe is the hook path's only"
