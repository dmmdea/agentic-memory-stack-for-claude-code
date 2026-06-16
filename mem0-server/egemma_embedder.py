"""EmbeddingGemma-300m prefix-shim embedder for mem0 (v0.22 migration, 2026-06-13).

WHY this exists
---------------
EmbeddingGemma requires *task prefixes* prepended to the raw text, and neither
llama.cpp nor mem0's stock OpenAI embedder applies them — mem0's OpenAIEmbedding
accepts `memory_action` but ignores it. Sending query and document text with the
same (or no) prefix degrades retrieval, especially cross-lingual EN/ES.

Verified (eval 2026-06-13, /tmp/run_eval.py, 200-mem pool, 30 EN/ES query pairs):
EmbeddingGemma with the ASYMMETRIC prefixes below scores recall@1 0.933 on BOTH
EN and ES, vs nomic's ES 0.333 — the bilingual fix this migration delivers.

PREFIXES (verbatim from Google's model card)
--------------------------------------------
- search (query)  -> "task: search result | query: {text}"
- add / update    -> "title: none | text: {text}"           (document side)

mem0 2.0.4 passes a distinguishable `memory_action` ("search" | "add" | "update")
to embed() / embed_batch() at every call site (verified in
mem0/memory/main.py), so asymmetric routing (Path A, Google's documented
optimum) is available — no symmetric fallback needed.

WIRING
------
config.py registers this class under the provider key "egemma" in mem0's
EmbedderFactory, then sets embedder.provider = "egemma". Transport is the stock
OpenAI-compatible path against llama-swap :11436/v1 (model "embeddinggemma").
"""
import unicodedata
from typing import Literal, Optional

from mem0.embeddings.openai import OpenAIEmbedding

# Verbatim from the EmbeddingGemma model card.
_QUERY_PREFIX = "task: search result | query: "
_DOC_PREFIX = "title: none | text: "

# v0.22 M4: EmbeddingGemma is served at --ctx-size 2048 (the MODEL's trained max —
# must NOT be raised). A record that passes the 4000-CHAR storage gate (app.py
# MAX_MEMORY_CHARS) can still exceed 2048 TOKENS when it is token-dense (CJK,
# accent-saturated, base64/hashes, minified code/paths): llama-server then returns
# HTTP 500 and the memory is SILENTLY LOST on add. Measured worst case against the
# live model: random CJK is ~2.1 tokens/char (1000 CJK chars -> 2109 tokens -> 500).
# A flat char cap therefore can't be both safe for CJK and non-destructive for
# normal prose. So we estimate tokens with a conservative per-char-class UPPER bound
# and truncate the EMBEDDING INPUT (only) to stay under a safe budget. The STORED
# memory text is untouched (full content kept in Qdrant payload + history.db);
# embeddings are a gist — the first ~2000 tokens is more than enough for retrieval.
_EMBED_TOKEN_BUDGET = 1900   # headroom under 2048; the prefix (~7 tok) is added on top
_PREFIX_TOKEN_RESERVE = 16   # generous reserve for the longest task prefix


def _prefix_for(memory_action: Optional[str]) -> str:
    """Return the correct task prefix for a given mem0 memory_action.

    "search" is the only query-side action; "add"/"update"/None all embed the
    document (stored memory) side.
    """
    return _QUERY_PREFIX if memory_action == "search" else _DOC_PREFIX


def _est_char_tokens(ch: str) -> float:
    """Conservative UPPER-bound token cost of a single char for EmbeddingGemma's
    tokenizer. Deliberately over-estimates dense scripts so the truncation never
    lets a 500-inducing input through (false truncation of borderline prose is an
    acceptable trade vs. a silently-dropped memory)."""
    o = ord(ch)
    if o < 0x80:
        # ASCII. Natural prose BPE-merges to ~0.25 tok/char, but HIGH-ENTROPY ASCII
        # barely merges: random base64 ~0.7, and the densest case — random hex
        # (0-9a-f) — measured ~0.9 tok/char against the live model. Upper-bound at
        # 0.9 so even a pure hash/hex blob can't slip past the budget and 500.
        # (Over-truncates natural prose's embedding input, but storage is unaffected
        # and the gist survives — never-500 is the invariant.)
        return 0.9
    if o < 0x400:
        # Latin-1 / Latin-extended (accented ES, etc.): random/dense sequences
        # measured >1 tok/char; upper-bound at 1.3.
        return 1.3
    # CJK, Hangul, Kana, symbols, emoji, etc.: measured up to ~2.2 tok/char.
    cat = unicodedata.category(ch)
    if cat.startswith(("L", "S", "P")):
        return 2.3
    return 1.6


def _truncate_for_embedding(text: str,
                            budget: int = _EMBED_TOKEN_BUDGET - _PREFIX_TOKEN_RESERVE) -> str:
    """Truncate `text` so its estimated token count (plus the task prefix reserve)
    stays under EmbeddingGemma's 2048-token context. Non-lossy w.r.t. STORAGE —
    only the embedding input is shortened. Fast single pass; cuts at the char where
    the running upper-bound estimate would exceed the budget."""
    if not text:
        return text
    total = 0.0
    for i, ch in enumerate(text):
        total += _est_char_tokens(ch)
        if total > budget:
            return text[:i]
    return text


class EmbeddingGemmaEmbedder(OpenAIEmbedding):
    """OpenAI-transport embedder that prepends EmbeddingGemma task prefixes.

    Reuses OpenAIEmbedding's HTTP client and batching unchanged; only the input
    text is rewritten with the action-appropriate prefix before it goes out.
    """

    def embed(
        self,
        text,
        memory_action: Optional[Literal["add", "search", "update"]] = None,
    ):
        # v0.22 M4: ctx-safe truncation of the embedding input only (storage keeps
        # the full text). Prevents a 2048-token overflow -> llama-server 500 ->
        # silent memory loss on token-dense records.
        prefixed = _prefix_for(memory_action) + _truncate_for_embedding(text)
        return super().embed(prefixed, memory_action)

    def embed_batch(self, texts, memory_action="add"):
        prefix = _prefix_for(memory_action)
        prefixed = [prefix + _truncate_for_embedding(t) for t in texts]
        return super().embed_batch(prefixed, memory_action)
