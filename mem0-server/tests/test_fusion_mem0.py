"""fusion.install() against the INSTALLED mem0 (local only: it skips where mem0 is absent, as in CI's
headless lane). Memory and AsyncMemory are built without their constructors over a stub vector store
and embedder, so mem0's real _search_vector_store runs: its results must be the fusion's, sync and
async, and the mem0 mode must reproduce stock mem0 exactly. This is the proof the static self-check in
install() stands in for: mem0 looks the global up at call time."""
import asyncio
import pathlib
import sys

import pytest

mm = pytest.importorskip("mem0.memory.main")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import fusion  # noqa: E402

COS = [0.80, 0.79, 0.78, 0.77, 0.70, 0.60]
FILTERS = {"user_id": "u"}


class _Hit:
    def __init__(self, id, score, payload):
        self.id, self.score, self.payload = id, score, payload


def _payload(i):
    return {"data": f"text {i}", "hash": f"h{i}", "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z", "user_id": "u"}


class _Store:
    def search(self, query, vectors, top_k, filters):
        return [_Hit(f"m{i}", c, _payload(i)) for i, c in enumerate(COS)]

    def keyword_search(self, query, top_k, filters):
        # raw BM25 values; mem0 normalises them monotonically, and the fusion reads only their rank
        return [_Hit("m3", 14.0, _payload(3)), _Hit("m5", 6.0, _payload(5))]


class _Embedder:
    def embed(self, text, action):
        return [0.0]

    def embed_batch(self, texts, action):
        return [[0.0] for _ in texts]


def _memory(cls):
    m = cls.__new__(cls)
    m.embedding_model = _Embedder()
    m.vector_store = _Store()
    return m


def _scored(res):
    return [(r["id"], round(r["score"], 12)) for r in res]


@pytest.fixture
def stock(monkeypatch):
    """mem0 as shipped, with spaCy kept out (both helpers are module globals read at call time)."""
    # whatever install() does is undone; and when an earlier test in the session imported app (which
    # installs the fusion), mem0's real function is the one the fusion kept, not the module global
    real = fusion._MEM0_ORIGINAL if mm.score_and_rank is fusion.ams_score_and_rank else mm.score_and_rank
    monkeypatch.setattr(mm, "score_and_rank", real)
    monkeypatch.setattr(mm, "lemmatize_for_bm25", lambda q: q)
    monkeypatch.setattr(mm, "extract_entities", lambda q: [])
    monkeypatch.setattr(fusion, "_MEM0_ORIGINAL", None)
    monkeypatch.setattr(fusion, "SEARCHES", {"reached": 0, "bypassed": 0})
    monkeypatch.delenv("MEM0_FUSION", raising=False)


def test_the_installed_mem0_binds_and_its_sync_and_async_search_run_the_fusion(stock):
    st = fusion.install(mm)
    assert st["bound"] is True and st["callers_bound"] == [True, True], st
    expected = fusion.rrf([{"id": f"m{i}", "score": c, "payload": _payload(i)} for i, c in enumerate(COS)],
                          {"m3": 0.9, "m5": 0.5}, {}, 0.1, 5)
    fusion.begin_search()
    res = _memory(mm.Memory)._search_vector_store("q", FILTERS, 5, 0.1)
    assert _scored(res) == _scored(expected)
    fusion.end_search({"results": res})
    assert [r["cosine"] for r in res] == [COS[int(r["id"][1:])] for r in res]
    assert fusion.SEARCHES == {"reached": 1, "bypassed": 0}
    res_async = asyncio.run(_memory(mm.AsyncMemory)._search_vector_store("q", FILTERS, 5, 0.1))
    assert _scored(res_async) == _scored(expected)


def test_the_mem0_mode_reproduces_stock_mem0(stock, monkeypatch):
    unpatched = _memory(mm.Memory)._search_vector_store("q", FILTERS, 5, 0.1)
    fusion.install(mm)
    monkeypatch.setenv("MEM0_FUSION", "mem0")
    fusion.begin_search()
    res = _memory(mm.Memory)._search_vector_store("q", FILTERS, 5, 0.1)
    assert _scored(res) == _scored(unpatched)
    assert fusion.last_legs()["m0"] == {"cosine": 0.80}
    monkeypatch.setenv("MEM0_FUSION", "rrf")
    assert _scored(_memory(mm.Memory)._search_vector_store("q", FILTERS, 5, 0.1)) != _scored(unpatched)
