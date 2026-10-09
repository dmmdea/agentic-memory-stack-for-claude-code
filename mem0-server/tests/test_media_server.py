"""1.35.0 media memories, the parts that need the heavy stack: the embedder (the `mem0` package), mem0's real
search path, and the server's helpers and endpoints (`import app` builds the live Memory client, so a Qdrant
on 127.0.0.1:6333 is needed). Local only, like test_egemma_embedder.py; the headless parts of the feature
are in test_media.py (which CI runs).

The load-bearing claims:
  * the embedder sends ONE request whose input is the prefixed caption followed by the media parts, in
    the shape llama-server's /v1/embeddings takes, and refuses rather than overflow the ubatch;
  * a media search swaps the query vector for exactly the query mem0 embeds (mem0 strips it first),
    and for nothing else: not a stored text, not the entity leg;
  * a failed media embed keeps the caption vector and says so; the dense vector is replaced through
    update_vectors on the unnamed vector only, so the BM25 sparse vector stays;
  * an add the server cannot embed is refused before anything is written, and a stored file is served back.
"""
import uuid

import httpx
import pytest

pytest.importorskip("mem0")
import embedder_profile as ep  # noqa: E402
import media  # noqa: E402
from test_media import MP4, PNG, _b64, _wav  # noqa: E402

EG2 = ep.get("egemma2")
LEGACY = ep.get(ep.LEGACY_PROFILE)


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    d = tmp_path / "media"
    monkeypatch.setenv("MEM0_MEDIA_DIR", str(d))
    return d

# ---------------------------------------------------------------- the embedder

def _embedder(profile=EG2):
    from mem0.configs.embeddings.base import BaseEmbedderConfig
    from egemma_embedder import EmbeddingGemmaEmbedder
    cfg = BaseEmbedderConfig(model=profile.model, openai_base_url="http://127.0.0.1:11436/v1", api_key="noop",
                             embedding_dims=profile.dims)
    return EmbeddingGemmaEmbedder(cfg, profile=profile)


def _ok(vec, url="http://127.0.0.1:11436/v1/embeddings"):
    return httpx.Response(200, json={"data": [{"embedding": vec}]}, request=httpx.Request("POST", url))


def test_embed_media_sends_one_input_of_the_prefixed_caption_then_the_media(monkeypatch):
    seen = {}

    def post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, timeout=timeout)
        return _ok([0.5] * EG2.dims)
    monkeypatch.setattr(httpx, "post", post)
    img, wav = media.decode([{"type": "image", "data": _b64(PNG)}, {"type": "audio", "data": _b64(_wav(1.0))}])
    vec = _embedder().embed_media("the whiteboard after planning", [img, wav], "add")
    assert vec == [0.5] * EG2.dims
    assert seen["url"] == "http://127.0.0.1:11436/v1/embeddings"
    body = seen["body"]
    assert body["model"] == EG2.model and body["encoding_format"] == "float"
    assert len(body["input"]) == 1, "caption and media are ONE embedding"
    content = body["input"][0]["content"]
    assert content[0] == {"type": "text", "text": EG2.doc_prefix + "the whiteboard after planning"}
    assert content[1] == media.content_part(img) and content[2] == media.content_part(wav)
    assert seen["timeout"] >= 60, "a cold projector load is slow"


def test_a_media_search_takes_the_query_prefix(monkeypatch):
    seen = {}
    monkeypatch.setattr(httpx, "post", lambda url, json=None, **kw: (seen.update(body=json), _ok([0.1] * EG2.dims))[1])
    img = media.decode([{"type": "image", "data": _b64(PNG)}])[0]
    _embedder().embed_media("koalas", [img], "search")
    assert seen["body"]["input"][0]["content"][0]["text"] == EG2.query_prefix + "koalas"


def test_embed_media_refuses_a_space_without_a_media_embedder_and_an_overfull_window(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no request may be sent"))
    img = media.decode([{"type": "image", "data": _b64(PNG)}])[0]
    with pytest.raises(RuntimeError, match="no media embedder"):
        _embedder(LEGACY).embed_media("x", [img])
    vid = media.decode([{"type": "video", "data": _b64(MP4)}])[0]
    with pytest.raises(ValueError, match="embed window"):
        _embedder().embed_media("x", [vid] * 4)


def test_embed_media_refuses_on_a_box_that_serves_its_alias_text_only(monkeypatch):
    """1.35.1: the profile has a media embedder, but MEM0_MEDIA_EMBEDDER=off says this box serves the alias without
    the projector. embed_media must refuse before any request reaches a llama-server that cannot read the media."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no request may be sent"))
    monkeypatch.setenv("MEM0_MEDIA_EMBEDDER", "off")
    img = media.decode([{"type": "image", "data": _b64(PNG)}])[0]
    assert EG2.media, "the space itself has a media embedder; it is the box that has none"
    with pytest.raises(RuntimeError, match="MEM0_MEDIA_EMBEDDER=off"):
        _embedder().embed_media("x", [img])


def test_embed_media_retries_a_429_and_checks_the_dims(monkeypatch):
    import egemma_embedder
    monkeypatch.setattr(egemma_embedder.time, "sleep", lambda s: None)
    answers = [httpx.Response(429, request=httpx.Request("POST", "http://x")), _ok([0.2] * EG2.dims)]
    monkeypatch.setattr(httpx, "post", lambda *a, **k: answers.pop(0))
    img = media.decode([{"type": "image", "data": _b64(PNG)}])[0]
    assert _embedder().embed_media("x", [img]) == [0.2] * EG2.dims
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok([0.2] * 512))
    with pytest.raises(ValueError, match="512 dims"):
        _embedder().embed_media("x", [img])


def test_query_media_swaps_only_the_search_embed_of_that_query(monkeypatch):
    import egemma_embedder
    emb = _embedder()
    calls = []
    monkeypatch.setattr(emb, "embed_media", lambda text, items, action="add": calls.append((text, action)) or ["media"])
    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed", lambda self, text, action=None: ["text"])
    img = media.decode([{"type": "image", "data": _b64(PNG)}])[0]
    tok = egemma_embedder.QUERY_MEDIA.set(egemma_embedder.QueryMedia("  what is on the board \n", [img]))
    try:
        assert emb.embed("another query", "search") == ["text"]
        assert emb.embed("what is on the board", "add") == ["text"], "a stored text never takes the media"
        assert emb.embed("what is on the board", "search") == ["media"], "mem0 embeds the STRIPPED query"
        # spent: mem0 2.0.4 embeds an entity that is the whole query with "search" right after
        assert emb.embed("what is on the board", "search") == ["text"], "the media vector is used once"
    finally:
        egemma_embedder.QUERY_MEDIA.reset(tok)
    assert emb.embed("what is on the board", "search") == ["text"], "unset outside the search"
    assert calls == [("what is on the board", "search")]


def test_the_installed_mem0_hands_the_shim_the_stripped_query(monkeypatch):
    """Through mem0's real search path: the query vector the vector store receives is the media one, and
    the entity leg (embed_batch) is never swapped."""
    mm = pytest.importorskip("mem0.memory.main")
    import egemma_embedder
    trim = getattr(mm, "_validate_and_trim_search_query", None)   # mem0 2.1 strips the query; 2.0.4 does not
    if trim is not None:
        assert trim("  q \n") == "q", "the swap compares stripped text because of this"
    monkeypatch.setattr(mm, "lemmatize_for_bm25", lambda q: q)
    monkeypatch.setattr(mm, "extract_entities", lambda q: [])
    emb = _embedder()
    monkeypatch.setattr(emb, "embed_media", lambda text, items, action="add": [7.0])
    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed", lambda self, text, action=None: [1.0])
    got = []

    class _Store:
        def search(self, query, vectors, top_k, filters):
            got.append(vectors)
            return []

        def keyword_search(self, query, top_k, filters):
            return []
    m = mm.Memory.__new__(mm.Memory)
    m.embedding_model, m.vector_store = emb, _Store()
    img = media.decode([{"type": "image", "data": _b64(PNG)}])[0]
    tok = egemma_embedder.QUERY_MEDIA.set(egemma_embedder.QueryMedia(" what is on the board ", [img]))
    try:
        m._search_vector_store((trim or str.strip)(" what is on the board "), {"user_id": "u"}, 5, 0.1)
    finally:
        egemma_embedder.QUERY_MEDIA.reset(tok)
    m._search_vector_store("what is on the board", {"user_id": "u"}, 5, 0.1)
    assert got == [[7.0], [1.0]]


# ---------------------------------------------------------------- the server (app.py)

@pytest.fixture(scope="module")
def appmod():
    import app as appmod  # heavy import; mem0 init runs once
    return appmod


@pytest.fixture
def client(appmod):
    from fastapi.testclient import TestClient
    return TestClient(appmod.app, raise_server_exceptions=False)


class _Client:
    def __init__(self, records=None, fail=None):
        self.records, self.fail, self.calls = records or [], fail, []

    def update_vectors(self, collection_name, points):
        if self.fail:
            raise self.fail
        self.calls.append(("update_vectors", collection_name, points))

    def set_payload(self, collection_name, payload, points):
        self.calls.append(("set_payload", collection_name, payload, points))

    def retrieve(self, collection_name, ids, with_payload, with_vectors):
        return self.records


class _Mem:
    def __init__(self, client, vec=None, embed_error=None):
        class _VS:
            pass
        self.vector_store = _VS()
        self.vector_store.client, self.vector_store.collection_name = client, "mem0_eg2_768"

        class _E:
            def embed_media(_s, caption, items, action="add"):
                if embed_error:
                    raise embed_error
                return vec
        self.embedding_model = _E()


def test_set_media_vector_replaces_only_the_dense_vector_and_records_it(appmod, monkeypatch):
    c = _Client()
    monkeypatch.setattr(appmod, "mem", _Mem(c, vec=[0.3] * 768))
    monkeypatch.setattr(appmod, "_media_stats", {"embeds_ok": 0, "embeds_failed": 0, "last_ok_ts": None, "last_error": None})
    img = media.decode([{"type": "image", "data": _b64(PNG)}])[0]
    assert appmod._set_media_vector("11111111-1111-1111-1111-111111111111", "caption", [img]) is True
    kind, coll, points = c.calls[0]
    assert kind == "update_vectors" and coll == "mem0_eg2_768"
    assert points[0].vector == {"": [0.3] * 768}, "the unnamed dense vector only: the bm25 sparse vector stays"
    assert c.calls[1] == ("set_payload", "mem0_eg2_768", {"media_embedded": True}, ["11111111-1111-1111-1111-111111111111"])
    assert appmod._media_stats["embeds_ok"] == 1


@pytest.mark.parametrize("embed_error,store_error", [(httpx.ConnectError("refused"), None),
                                                     (None, RuntimeError("qdrant down"))])
def test_a_failed_media_embed_keeps_the_caption_vector(appmod, monkeypatch, embed_error, store_error):
    c = _Client(fail=store_error)
    monkeypatch.setattr(appmod, "mem", _Mem(c, vec=[0.3] * 768, embed_error=embed_error))
    monkeypatch.setattr(appmod, "_media_stats", {"embeds_ok": 0, "embeds_failed": 0, "last_ok_ts": None, "last_error": None})
    img = media.decode([{"type": "image", "data": _b64(PNG)}])[0]
    assert appmod._set_media_vector("11111111-1111-1111-1111-111111111111", "caption", [img]) is False
    assert not [x for x in c.calls if x[0] == "set_payload"], "media_embedded is never claimed after a failure"
    assert appmod._media_stats["embeds_failed"] == 1 and appmod._media_stats["last_error"]


def test_query_media_validates_the_search(appmod, monkeypatch):
    from fastapi import HTTPException

    class B:
        def __init__(self, query, media_items):
            self.query, self.media = query, media_items
    assert appmod._query_media(B("q", None)) == []
    item = [{"type": "image", "data": _b64(PNG)}]
    monkeypatch.setattr(appmod, "EMBED_PROFILE", LEGACY)
    with pytest.raises(HTTPException, match="no media embedder"):
        appmod._query_media(B("q", item))
    monkeypatch.setattr(appmod, "EMBED_PROFILE", EG2)
    with pytest.raises(HTTPException, match="needs a query text"):
        appmod._query_media(B("   ", item))
    with pytest.raises(HTTPException) as e:
        appmod._query_media(B("q", [{"type": "audio", "data": _b64(PNG)}]))
    assert e.value.status_code == 400
    assert [m.type for m in appmod._query_media(B("q", item))] == ["image"]


def test_add_refuses_media_it_cannot_embed_before_writing_anything(appmod, client, monkeypatch, media_dir):
    monkeypatch.setattr(appmod, "mem", None)  # any write attempt would crash: these must all be 400s first
    h = {"X-API-Key": appmod.API_KEY}
    body = {"messages": "the whiteboard", "user_id": "test-media", "infer": False,
            "media": [{"type": "image", "data": _b64(PNG)}]}
    monkeypatch.setattr(appmod, "EMBED_PROFILE", LEGACY)
    r = client.post("/v1/memories", json=body, headers=h)
    assert r.status_code == 400 and "no media embedder" in r.text
    monkeypatch.setattr(appmod, "EMBED_PROFILE", EG2)
    r = client.post("/v1/memories", json={**body, "infer": True}, headers=h)
    assert r.status_code == 400 and "infer=false" in r.text
    r = client.post("/v1/memories", json={**body, "media": [{"type": "video", "data": _b64(PNG)}]}, headers=h)
    assert r.status_code == 400 and "declared video" in r.text
    assert not media_dir.exists(), "nothing stored for a refused add"


def test_get_media_returns_the_stored_file(appmod, client, monkeypatch, media_dir):
    m = media.decode([{"type": "image", "data": _b64(PNG), "filename": "board.png"}])[0]
    media.store(m)
    mid = str(uuid.uuid4())

    class R:
        payload = {"data": "the whiteboard", "media": [media.payload_meta(m)], "media_embedded": True}
    monkeypatch.setattr(appmod, "mem", _Mem(_Client(records=[R()])))
    h = {"X-API-Key": appmod.API_KEY}
    r = client.get(f"/v1/memories/{mid}/media/0", headers=h)
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"].startswith("image/png") and "board.png" in r.headers["content-disposition"]
    assert client.get(f"/v1/memories/{mid}/media/1", headers=h).status_code == 404
    assert client.get(f"/v1/memories/{mid}/media/0").status_code == 401
    assert client.get("/v1/memories/not-a-uuid/media/0", headers=h).status_code == 404
    media.path_for(media.payload_meta(m)).unlink()
    r = client.get(f"/v1/memories/{mid}/media/0", headers=h)
    assert r.status_code == 404 and "missing" in r.text




def test_a_media_search_refused_is_a_400_through_the_endpoint(appmod, client, monkeypatch):
    """search() maps unexpected errors to 5xx; a media search's caller errors must stay 400s."""
    h = {"X-API-Key": appmod.API_KEY}
    item = [{"type": "image", "data": _b64(PNG)}]
    monkeypatch.setattr(appmod, "EMBED_PROFILE", LEGACY)
    r = client.post("/v1/memories/search", json={"query": "board", "filters": {"user_id": "test-media"}, "media": item}, headers=h)
    assert r.status_code == 400 and "no media embedder" in r.text, r.text
    monkeypatch.setattr(appmod, "EMBED_PROFILE", EG2)
    r = client.post("/v1/memories/search", json={"query": "  ", "filters": {"user_id": "test-media"}, "media": item}, headers=h)
    assert r.status_code == 400 and "query text" in r.text, r.text
    r = client.post("/v1/memories/search", json={"query": "board", "filters": {"user_id": "test-media"},
                                                 "media": [{"type": "video", "data": _b64(MP4)}] * 4}, headers=h)
    assert r.status_code == 400 and "embed window" in r.text, r.text


def test_a_media_search_is_not_reranked(appmod, client, monkeypatch):
    """The cross-encoder reads only text (the query against captions): it would sink what the media found."""
    import egemma_embedder
    seen = {}

    class _M:
        def search(self, query, filters, top_k, threshold):
            seen["query_media"] = egemma_embedder.QUERY_MEDIA.get()
            return {"results": []}
    monkeypatch.setattr(appmod, "mem", _M())
    monkeypatch.setattr(appmod, "EMBED_PROFILE", EG2)
    monkeypatch.setattr(appmod, "bge_rerank", lambda *a, **k: pytest.fail("a media search was reranked"))
    r = client.post("/v1/memories/search", headers={"X-API-Key": appmod.API_KEY},
                    json={"query": "board", "filters": {"user_id": "test-media"}, "rerank": True, "limit": 5,
                          "explain": True, "media": [{"type": "image", "data": _b64(PNG)}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "rerank_status" not in body, "rerank was requested and must have been turned off"
    lex = [s for s in body.get("_explain", {}).get("stages", []) if s.get("stage") == "union_lexical"]
    assert lex and lex[0]["detail"]["active"] is False, body.get("_explain")
    q = seen["query_media"]
    assert q is not None and q.text == "board" and [m.type for m in q.items] == ["image"]
    assert egemma_embedder.QUERY_MEDIA.get() is None, "scoped to that one search"


def test_a_text_only_box_refuses_media_with_a_400_that_says_why(appmod, client, monkeypatch, media_dir):
    """1.35.1: MEM0_MEDIA_EMBEDDER=off (the alias is served without the projector): media adds and media searches
    are refused before anything is written or sent to llama-server, and /health/deep says media is off."""
    monkeypatch.setattr(appmod, "mem", None)
    monkeypatch.setattr(appmod, "EMBED_PROFILE", EG2)
    monkeypatch.setenv("MEM0_MEDIA_EMBEDDER", "off")
    h = {"X-API-Key": appmod.API_KEY}
    item = [{"type": "image", "data": _b64(PNG)}]
    r = client.post("/v1/memories", json={"messages": "the whiteboard", "user_id": "test-media", "infer": False,
                                          "media": item}, headers=h)
    assert r.status_code == 400 and "MEM0_MEDIA_EMBEDDER=off" in r.text, r.text
    r = client.post("/v1/memories/search", json={"query": "board", "filters": {"user_id": "test-media"}, "media": item},
                    headers=h)
    assert r.status_code == 400 and "MEM0_MEDIA_EMBEDDER=off" in r.text, r.text
    assert appmod._media_on() is False
    assert not media_dir.exists()
    monkeypatch.setenv("MEM0_MEDIA_EMBEDDER", "on")
    assert appmod._media_on() is True
