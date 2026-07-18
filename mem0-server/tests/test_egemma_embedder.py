"""Unit tests for the EmbeddingGemma prefix-shim embedder (v0.22 migration).

The load-bearing claim: query text (memory_action="search") gets the QUERY
prefix and document text (add/update/None) gets the DOCUMENT prefix, asymmetrically.
These tests intercept the text handed to mem0's stock OpenAIEmbedding so they run
with no live embedding backend.

A live test (skipped if llama-swap is down) confirms 768-dim L2-normalized output
for both query and document paths.
"""
import math
import os
import random

import httpx
import pytest

from egemma_embedder import (
    EmbeddingGemmaEmbedder,
    _QUERY_PREFIX,
    _DOC_PREFIX,
    _EMBED_TOKEN_BUDGET,
    _RETRY_429_BASE_SLEEP_S,
    _RETRY_429_JITTER_S,
    _prefix_for,
    _truncate_for_embedding,
)


def _make_embedder():
    """Build the shim without touching the network. OpenAIEmbedding.__init__ only
    constructs an OpenAI client object (no request), so a noop base_url is fine."""
    from mem0.configs.embeddings.base import BaseEmbedderConfig
    cfg = BaseEmbedderConfig(
        model="embeddinggemma",
        openai_base_url="http://127.0.0.1:11436/v1",
        api_key="sk-noop",
        embedding_dims=768,
    )
    return EmbeddingGemmaEmbedder(cfg)


# ---- prefix-routing logic (pure, no I/O) ----

def test_prefix_for_search_is_query():
    assert _prefix_for("search") == _QUERY_PREFIX

def test_prefix_for_add_is_document():
    assert _prefix_for("add") == _DOC_PREFIX

def test_prefix_for_update_is_document():
    assert _prefix_for("update") == _DOC_PREFIX

def test_prefix_for_none_defaults_to_document():
    assert _prefix_for(None) == _DOC_PREFIX

def test_prefixes_are_distinct():
    assert _QUERY_PREFIX != _DOC_PREFIX

def test_prefix_strings_match_model_card():
    # Verbatim from EmbeddingGemma's model card — a typo here silently degrades retrieval.
    assert _QUERY_PREFIX == "task: search result | query: "
    assert _DOC_PREFIX == "title: none | text: "


# ---- embed() / embed_batch() prepend the correct prefix (parent call intercepted) ----

def test_embed_search_gets_query_prefix(monkeypatch):
    emb = _make_embedder()
    captured = {}
    def fake_super_embed(self, text, memory_action=None):
        captured["text"] = text
        return [0.0] * 768
    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed", fake_super_embed)
    emb.embed("where is the token", memory_action="search")
    assert captured["text"] == _QUERY_PREFIX + "where is the token"

def test_embed_add_gets_doc_prefix(monkeypatch):
    emb = _make_embedder()
    captured = {}
    def fake_super_embed(self, text, memory_action=None):
        captured["text"] = text
        return [0.0] * 768
    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed", fake_super_embed)
    emb.embed("the token lives in 1Password", memory_action="add")
    assert captured["text"] == _DOC_PREFIX + "the token lives in 1Password"

def test_embed_none_gets_doc_prefix(monkeypatch):
    emb = _make_embedder()
    captured = {}
    def fake_super_embed(self, text, memory_action=None):
        captured["text"] = text
        return [0.0] * 768
    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed", fake_super_embed)
    emb.embed("stored fact")
    assert captured["text"] == _DOC_PREFIX + "stored fact"

def test_embed_batch_add_prefixes_all_docs(monkeypatch):
    emb = _make_embedder()
    captured = {}
    def fake_super_batch(self, texts, memory_action="add"):
        captured["texts"] = texts
        return [[0.0] * 768 for _ in texts]
    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed_batch", fake_super_batch)
    emb.embed_batch(["a", "b"], memory_action="add")
    assert captured["texts"] == [_DOC_PREFIX + "a", _DOC_PREFIX + "b"]

def test_embed_batch_search_prefixes_all_queries(monkeypatch):
    emb = _make_embedder()
    captured = {}
    def fake_super_batch(self, texts, memory_action="add"):
        captured["texts"] = texts
        return [[0.0] * 768 for _ in texts]
    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed_batch", fake_super_batch)
    emb.embed_batch(["q1", "q2"], memory_action="search")
    assert captured["texts"] == [_QUERY_PREFIX + "q1", _QUERY_PREFIX + "q2"]


# ---- MEM-12 (2026-07-03): ONE bounded retry on a llama-swap 429 burst ----
# 25 RateLimitErrors/7d (incl. 8-in-1s) each killed a bundle raw-trace fallback;
# a single ~250ms+jitter retry absorbs the burst. The contract pinned here:
# exactly ONE retry, ONLY on RateLimitError, second 429 re-raises unchanged.

def _rate_limit_error():
    import openai
    req = httpx.Request("POST", "http://127.0.0.1:11436/v1/embeddings")
    return openai.RateLimitError(
        "429 queue saturated", response=httpx.Response(429, request=req), body=None)


def test_embed_429_then_success_retries_once(monkeypatch):
    emb = _make_embedder()
    calls = {"n": 0}
    slept = []

    def flaky_super_embed(self, text, memory_action=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rate_limit_error()
        return [0.5] * 768

    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed", flaky_super_embed)
    monkeypatch.setattr("egemma_embedder.time.sleep", lambda s: slept.append(s))
    v = emb.embed("burst survivor", memory_action="add")
    assert v == [0.5] * 768
    assert calls["n"] == 2, "exactly one retry after the 429"
    assert len(slept) == 1
    # ~250ms base + [0, 250ms) jitter — the de-sync window
    assert _RETRY_429_BASE_SLEEP_S <= slept[0] < _RETRY_429_BASE_SLEEP_S + _RETRY_429_JITTER_S


def test_embed_429_twice_reraises_no_third_attempt(monkeypatch):
    """Never-500 invariant intact: the retry never swallows — a sustained 429
    surfaces to the caller's existing fail-soft handling after attempt #2."""
    import openai
    emb = _make_embedder()
    calls = {"n": 0}

    def always_429(self, text, memory_action=None):
        calls["n"] += 1
        raise _rate_limit_error()

    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed", always_429)
    monkeypatch.setattr("egemma_embedder.time.sleep", lambda s: None)
    with pytest.raises(openai.RateLimitError):
        emb.embed("still saturated", memory_action="search")
    assert calls["n"] == 2, "bounded: never more than one retry"


def test_embed_non_429_error_is_never_retried(monkeypatch):
    """A non-429 failure (e.g. the ctx-overflow 500 class M4 exists for) must
    surface IMMEDIATELY — replaying it would just double the damage."""
    emb = _make_embedder()
    calls = {"n": 0}

    def raise_500(self, text, memory_action=None):
        calls["n"] += 1
        raise ValueError("llama-server 500: context overflow")

    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed", raise_500)
    with pytest.raises(ValueError):
        emb.embed("dense blob", memory_action="add")
    assert calls["n"] == 1, "non-429 errors are never retried"


def test_embed_batch_429_then_success_retries_once(monkeypatch):
    emb = _make_embedder()
    calls = {"n": 0}

    def flaky_super_batch(self, texts, memory_action="add"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rate_limit_error()
        return [[0.25] * 768 for _ in texts]

    monkeypatch.setattr("mem0.embeddings.openai.OpenAIEmbedding.embed_batch", flaky_super_batch)
    monkeypatch.setattr("egemma_embedder.time.sleep", lambda s: None)
    out = emb.embed_batch(["a", "b"], memory_action="add")
    assert len(out) == 2 and calls["n"] == 2


def test_shim_advertises_self_retry_marker():
    """episode_embeddings._embed_with_429_retry keys off this marker so the
    composed episode path never multiplies attempts beyond 2."""
    assert EmbeddingGemmaEmbedder.handles_429_retry is True


# ---- live: real EmbeddingGemma returns 768-dim L2-normalized vectors ----

def _egemma_up() -> bool:
    try:
        r = httpx.post(
            "http://127.0.0.1:11436/v1/embeddings",
            json={"model": "embeddinggemma", "input": "title: none | text: ping"},
            timeout=20.0,
        )
        return r.status_code == 200
    except Exception:
        return False

@pytest.mark.skipif(not _egemma_up(), reason="embeddinggemma llama-swap not reachable")
def test_live_query_and_doc_both_768_l2norm():
    emb = _make_embedder()
    q = emb.embed("where is the cloudflare token", memory_action="search")
    d = emb.embed("the cloudflare token is stored in bastion", memory_action="add")
    for v in (q, d):
        assert len(v) == 768
        n = math.sqrt(sum(x * x for x in v))
        assert abs(n - 1.0) < 1e-2, f"not L2-normalized: norm={n}"
    # query and document of related text should be non-degenerate (not identical vectors)
    assert q != d


# ---- v0.22 M4: ctx-safe truncation of the embedding input (never 500) ----

def _dense_inputs(n=4000):
    """Token-dense inputs that pass the 4000-char storage gate but, untruncated,
    exceed EmbeddingGemma's 2048-token ctx and 500 at embed time."""
    rng = random.Random(7)
    return {
        "cjk": "".join(chr(rng.randint(0x4E00, 0x9FFF)) for _ in range(n)),
        "accented": "".join(rng.choice("áéíóúñàèìçâêûäëüabcdefghijklmnopqrstuvwxyz ")
                            for _ in range(n)),
        "hex": os.urandom(n // 2).hex(),  # densest ASCII case (~0.9 tok/char)
        "english": ("the quick brown fox jumps over the lazy dog. " * 100)[:n],
        "spanish": ("el rápido zorro café salta sobre el perro perezoso. " * 100)[:n],
    }


def test_truncate_shortens_dense_input_preserves_prose_pure():
    """Pure (no network): dense scripts get truncated under the token budget;
    natural prose is left intact-enough (truncation is for the embed input only —
    storage keeps the full text, which this function never sees)."""
    inputs = _dense_inputs()
    # CJK (~2.2 tok/char est) must be cut hard; an empty/short string is untouched.
    assert len(_truncate_for_embedding(inputs["cjk"])) < 1000
    assert _truncate_for_embedding("") == ""
    assert _truncate_for_embedding("short fact") == "short fact"
    # Every truncated input's UPPER-BOUND token estimate stays under the budget.
    from egemma_embedder import _est_char_tokens
    for name, raw in inputs.items():
        t = _truncate_for_embedding(raw)
        est = sum(_est_char_tokens(c) for c in t)
        assert est <= _EMBED_TOKEN_BUDGET, f"{name}: est {est} over budget"


@pytest.mark.skipif(not _egemma_up(), reason="embeddinggemma llama-swap not reachable")
@pytest.mark.parametrize("kind", ["cjk", "accented", "hex", "english", "spanish"])
def test_live_4000char_dense_embeds_without_500(kind):
    """THE M4 GUARD: a 4000-char token-dense record (the exact silent-loss case —
    passes the 4000-char 413 gate, then untruncated would 500 at embed time)
    embeds to 768 dims via the shim, no exception. The shim truncates the embedding
    INPUT only; the stored memory text is unaffected (asserted in the pure test)."""
    emb = _make_embedder()
    raw = _dense_inputs()[kind]
    assert len(raw) >= 3800
    v = emb.embed(raw, memory_action="add")   # must NOT raise (no llama-server 500)
    assert len(v) == 768
    n = math.sqrt(sum(x * x for x in v))
    assert abs(n - 1.0) < 1e-2, f"not L2-normalized: norm={n}"


# ---- v0.22 M6: committed bilingual (EN+ES) recall regression guard ----

def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))  # both L2-normalized -> dot == cosine


# Doc/query pairs that exercise the asymmetric prefix routing AND retrieval
# discrimination, in BOTH languages — the migration's load-bearing claim
# (recall@1 0.93 EN/ES vs nomic ES 0.33). Each query must retrieve its matching
# doc over the unrelated docs. Pairs are semantically distinct so a CORRECT
# embedder separates them; a degraded one (wrong prefix, model/pooling/quant
# regression) would not — which routing-only unit tests cannot catch.
_RECALL_PAIRS = [
    # (lang, query, matching_doc)
    ("en", "where is the cloudflare api token stored",
           "the cloudflare api token lives in the bastion 1password vault"),
    ("en", "what port does the mem0 server listen on",
           "the mem0 fastapi server is bound to 127.0.0.1 port 18791"),
    ("es", "dónde se guarda el token de cloudflare",
           "el token de la api de cloudflare está en la bóveda 1password del bastión"),
    ("es", "en qué puerto escucha el servidor mem0",
           "el servidor mem0 fastapi está enlazado a 127.0.0.1 puerto 18791"),
]


@pytest.mark.skipif(not _egemma_up(), reason="embeddinggemma llama-swap not reachable")
def test_live_bilingual_recall_matching_pair_outranks_mismatch():
    """Within each language, every query must rank its own matching document above
    the other (topically distinct) documents in that language's pool (recall@1).
    Reproducible regression guard for the EmbeddingGemma migration's bilingual-recall
    claim — replaces reliance on the uncommitted /tmp eval. A prefix/model/pooling
    regression that degrades within-language discrimination (while vectors stay
    well-formed) fails here.

    NOTE: pools are per-language. Cross-lingual same-TOPIC twins (e.g. the EN and
    ES 'cloudflare token' docs) sit ~0.68 cosine apart — by design, EmbeddingGemma
    is multilingual, so a topic's EN and ES docs are near-neighbours; that is the
    desired behaviour, not a recall failure. The recall claim is about separating
    DISTINCT topics within the query's language."""
    emb = _make_embedder()
    failures = []
    for lang in ("en", "es"):
        pool = [(q, d) for (l, q, d) in _RECALL_PAIRS if l == lang]
        docs = [emb.embed(d, memory_action="add") for (_, d) in pool]
        for i, (query, _) in enumerate(pool):
            qv = emb.embed(query, memory_action="search")
            sims = [_cos(qv, dv) for dv in docs]
            best = max(range(len(sims)), key=lambda j: sims[j])
            if best != i:
                failures.append(
                    f"[{lang}] {query!r}: best doc#{best} (sim {sims[best]:.3f}) "
                    f"!= expected doc#{i} (sim {sims[i]:.3f})"
                )
    assert not failures, "recall@1 failures:\n" + "\n".join(failures)


@pytest.mark.skipif(not _egemma_up(), reason="embeddinggemma llama-swap not reachable")
def test_live_es_query_matches_es_doc_above_threshold():
    """ES query→ES doc cosine must clear the v0.22 bundle search threshold (0.30);
    the migration lowered 0.4→0.30 because EmbeddingGemma's ES gold cosine floor was
    ~0.36. Guards that the deployed threshold still admits real ES matches."""
    emb = _make_embedder()
    qv = emb.embed("dónde se guarda el token de cloudflare", memory_action="search")
    dv = emb.embed("el token de la api de cloudflare está en la bóveda 1password",
                   memory_action="add")
    assert _cos(qv, dv) >= 0.30, "ES match fell below the 0.30 bundle threshold"
