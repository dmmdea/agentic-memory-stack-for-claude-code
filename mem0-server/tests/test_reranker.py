"""Unit + live integration tests for reranker. Live test skips if llama-swap is down."""
import httpx, pytest
from reranker import should_rerank, rerank, RERANK_URL

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
