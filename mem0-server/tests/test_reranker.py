"""Unit + live integration tests for reranker. Live test skips if llama-swap is down."""
import httpx, pytest
from reranker import should_rerank, skip_reason, rerank

def test_should_rerank_skips_small():
    assert should_rerank([{"score": 0.5}, {"score": 0.4}]) is False  # N=2 < 3

def test_should_rerank_skips_high_confidence():
    assert should_rerank([{"score": 0.95}, {"score": 0.5}, {"score": 0.4}]) is False

def test_should_rerank_runs_when_unsure():
    assert should_rerank([{"score": 0.5}, {"score": 0.45}, {"score": 0.4}, {"score": 0.35}]) is True

def _reranker_up() -> bool:
    # Probe the model endpoint directly; if llama-swap has the model loaded, /v1/models lists it
    try:
        r = httpx.get("http://127.0.0.1:11436/v1/models", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False

@pytest.mark.skipif(not _reranker_up(), reason="bge-reranker llama-swap not reachable")
def test_rerank_live_reorders_or_passes_through():
    docs = [
        {"memory": "The operator runs Python 3.13 globally and a 3.12 venv for mem0."},
        {"memory": "The box A laptop has 64GB RAM and an RTX 3070 8GB."},
        {"memory": "mem0 listens on 127.0.0.1:18791 with X-API-Key auth."},
        {"memory": "Chickens lay eggs."},  # irrelevant
    ]
    out = rerank("What port does mem0 listen on?", docs)
    assert len(out) == len(docs)
    titles = [r["memory"][:30] for r in out]
    assert any("mem0 listens" in t for t in titles[:2])

def test_rerank_passes_through_on_failure(monkeypatch):
    import reranker as rr
    def boom(*a, **k): raise httpx.ConnectError("nope")
    monkeypatch.setattr(rr.httpx, "post", boom)
    docs = [{"memory": "a"}, {"memory": "b"}, {"memory": "c"}, {"memory": "d"}]
    out = rr.rerank("q", docs)
    assert [d["memory"] for d in out] == ["a", "b", "c", "d"]


# ---------------------------------------------------------------------------
# W5 T1.2/T5.3: skip_reason + status_out + force
# ---------------------------------------------------------------------------

def test_skip_reason_distinguishes_the_two_skips():
    assert skip_reason([{"score": 0.5}, {"score": 0.4}]) == "small_n"
    assert skip_reason([{"score": 0.95}, {"score": 0.5}, {"score": 0.4}]) == "confident"
    assert skip_reason([{"score": 0.5}, {"score": 0.45}, {"score": 0.4}]) is None


def test_status_out_reports_skips_without_transport(monkeypatch):
    import reranker as rr
    def must_not_call(*a, **k):
        raise AssertionError("skip paths must never touch the transport")
    monkeypatch.setattr(rr.httpx, "post", must_not_call)
    st: dict = {}
    rr.rerank("q", [{"memory": "a"}, {"memory": "b"}], status_out=st)
    assert st["status"] == "skipped_small_n"
    st = {}
    rr.rerank("q", [{"score": 0.99, "memory": "a"}, {"memory": "b"},
                    {"memory": "c"}], status_out=st)
    assert st["status"] == "skipped_confident"


def test_status_out_failed_fallback_dense(monkeypatch):
    import reranker as rr
    def boom(*a, **k): raise httpx.ConnectError("nope")
    monkeypatch.setattr(rr.httpx, "post", boom)
    st: dict = {}
    out = rr.rerank("q", [{"memory": "a"}, {"memory": "b"}, {"memory": "c"},
                          {"memory": "d"}], status_out=st)
    assert st["status"] == "failed_fallback_dense"
    assert [d["memory"] for d in out] == ["a", "b", "c", "d"]


def test_force_bypasses_both_skips(monkeypatch):
    """T5.3: with lexical candidates present the union leg FORCES the rerank —
    a silent skip would delete every lexical rescue via the fail-closed drop."""
    import reranker as rr

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [{"index": 1, "relevance_score": 0.9},
                                {"index": 0, "relevance_score": 0.2}]}

    calls = {"n": 0}
    def fake_post(*a, **k):
        calls["n"] += 1
        return _Resp()
    monkeypatch.setattr(rr.httpx, "post", fake_post)
    # small_n pool (2 docs) — force must still call the transport and score
    st: dict = {}
    out = rr.rerank("q", [{"memory": "a"}, {"memory": "b"}],
                    force=True, status_out=st)
    assert calls["n"] == 1 and st["status"] == "ran"
    assert out[0]["memory"] == "b" and out[0]["rerank_score"] == 0.9
    # confident head — force must still rerank
    st = {}
    rr.rerank("q", [{"score": 0.99, "memory": "a"}, {"memory": "b"}],
              force=True, status_out=st)
    assert calls["n"] == 2 and st["status"] == "ran"
