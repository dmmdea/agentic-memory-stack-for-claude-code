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
import contextvars
import random
import time
import unicodedata
from typing import Literal, Optional

import httpx
from mem0.embeddings.openai import OpenAIEmbedding
from openai import RateLimitError

import embedder_profile
import media as _media

# A search that carries media (SearchIn.media): (query text, [media.Media]) for this request. The shim's
# embed(text, "search") returns the interleaved multimodal vector for exactly that text and nothing
# else, so mem0's other embeds in the same search (entity texts, a second query) stay text-only.
# The server sets it right before mem.search and resets it right after (app.py _search_core).
QUERY_MEDIA: contextvars.ContextVar = contextvars.ContextVar("ams_query_media", default=None)


class QueryMedia:
    """One media search's query: its text and its media, spent by the FIRST search embed of that text.
    mem0 embeds the query before anything else in its search; mem0 2.0.4 then embeds each extracted
    entity with embed(entity, "search") one at a time, and an entity that is the whole query must get
    its text vector there, not the media one (nor cost a second media request)."""
    __slots__ = ("text", "items", "used")

    def __init__(self, text: str, items):
        self.text, self.items, self.used = text, items, False
_MEDIA_TIMEOUT_S = 120.0     # a cold projector load plus a video can take tens of seconds
_MEDIA_MIN_TEXT_TOKENS = 64  # a caption keeps at least this much room next to its media

# Verbatim from the EmbeddingGemma model cards (both generations use the same strings; the
# active profile in embedder_profile is the source the embedder instance reads).
_QUERY_PREFIX = "task: search result | query: "
_DOC_PREFIX = "title: none | text: "

# MEM-12 (2026-07-03): llama-swap returns 429 bursts under queue saturation
# (25 RateLimitErrors/7d observed, incl. an 8-in-1s burst; every one killed a
# bundle raw-trace fallback). A bounded retry after a short jittered sleep
# absorbs the burst case. Anything still 429 after the last attempt re-raises so
# callers keep their existing fail-soft handling — the retry only ADDS attempts,
# it never swallows an error. Other errors are NEVER retried (a 500 from a
# ctx overflow must surface immediately, not get replayed).
#
# 2026-07-26: widened from a single retry to three total attempts with
# exponential backoff. One retry absorbed a two-deep burst but not the 8-in-1s
# one on record, and a burst that outlived it reached app.py's generic handler
# as a flat HTTP 500 — which the MCP shim treats as a real answer rather than a
# retryable condition, so the write was dropped instead of queued. app.py now
# also maps a surviving 429 to 503 (see _upstream_error there); widening the
# retry is the half that stops most bursts from getting that far.
_RETRY_429_ATTEMPTS = 3      # total attempts = 2 retries
_RETRY_429_BASE_SLEEP_S = 0.25
_RETRY_429_JITTER_S = 0.25   # uniform [0, 0.25) on top — de-syncs burst callers


def _retry_on_429(call):
    """Run ``call()``; on openai.RateLimitError (llama-swap 429) sleep with
    exponential backoff + jitter and retry, up to ``_RETRY_429_ATTEMPTS`` total
    attempts. The final 429 propagates unchanged; no other exception type is
    ever retried."""
    for attempt in range(_RETRY_429_ATTEMPTS):
        try:
            return call()
        except RateLimitError:
            if attempt == _RETRY_429_ATTEMPTS - 1:
                raise
            time.sleep(_RETRY_429_BASE_SLEEP_S * (2 ** attempt)
                       + random.random() * _RETRY_429_JITTER_S)

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


def budget_for(profile: "embedder_profile.EmbedProfile") -> int:
    """The embedding-input budget for a profile: its token budget minus the prefix reserve.
    EmbeddingGemma-300m keeps the measured 1900 - 16; EmbeddingGemma-2's 8,192-token window is
    what lets a long record or wiki page be embedded whole instead of as its first ~2,000 tokens."""
    return profile.token_budget - _PREFIX_TOKEN_RESERVE


class EmbeddingGemmaEmbedder(OpenAIEmbedding):
    """OpenAI-transport embedder that prepends EmbeddingGemma task prefixes.

    Reuses OpenAIEmbedding's HTTP client and batching unchanged; only the input
    text is rewritten with the action-appropriate prefix before it goes out.
    The prefixes and the token budget come from the active embedder_profile, read
    once when the instance is built (app.py builds it at server start).
    """

    # MEM-12: this class already does the bounded 429 retry internally.
    # episode_embeddings.py checks this marker so an injected shim instance is
    # not ALSO wrapped by the episode-path retry (attempts stay _RETRY_429_ATTEMPTS,
    # never a multiple of it).
    handles_429_retry = True

    def __init__(self, config=None, profile: "Optional[embedder_profile.EmbedProfile]" = None):
        super().__init__(config)
        self.profile = profile or embedder_profile.active()
        self._budget = budget_for(self.profile)

    def _prefix(self, memory_action) -> str:
        return self.profile.query_prefix if memory_action == "search" else self.profile.doc_prefix

    def embed_media(self, text: str, items, memory_action: Optional[str] = "add") -> list:
        """ONE embedding of `text` (task prefix, truncated to what the media leave of the budget) followed
        by the media items, as OpenAI-style content parts on llama-server's /v1/embeddings (the alias
        must be served with --mmproj). Raises on any failure; callers keep the text-only vector."""
        if not embedder_profile.media_enabled(self.profile):
            raise RuntimeError(f"embedding profile {self.profile.name}: no media embedder on this box "
                               "(the profile has none, or MEM0_MEDIA_EMBEDDER=off)")
        room = self._budget - _media.tokens(items)
        if room < _MEDIA_MIN_TEXT_TOKENS:
            raise ValueError(f"media take ~{_media.tokens(items)} of the {self._budget}-token embed window")
        content = []
        if text:
            content.append({"type": "text",
                            "text": self._prefix(memory_action) + _truncate_for_embedding(text, room)})
        content.extend(_media.content_part(m) for m in items)
        url = str(self.config.openai_base_url or embedder_profile.base_url()).rstrip("/") + "/embeddings"
        body = {"model": self.config.model, "input": [{"content": content}], "encoding_format": "float"}
        headers = {"Authorization": f"Bearer {self.config.api_key or 'noop'}"}
        for attempt in range(_RETRY_429_ATTEMPTS):
            r = httpx.post(url, json=body, headers=headers, timeout=_MEDIA_TIMEOUT_S)
            if r.status_code == 429 and attempt < _RETRY_429_ATTEMPTS - 1:
                time.sleep(_RETRY_429_BASE_SLEEP_S * (2 ** attempt) + random.random() * _RETRY_429_JITTER_S)
                continue
            r.raise_for_status()
            break
        vec = r.json()["data"][0]["embedding"]
        if len(vec) != self.profile.dims:
            raise ValueError(f"media embed returned {len(vec)} dims, the space has {self.profile.dims}")
        return vec

    def embed(
        self,
        text,
        memory_action: Optional[Literal["add", "search", "update"]] = None,
    ):
        q = QUERY_MEDIA.get()
        # mem0 2.1 strips the query before it embeds it (_validate_and_trim_search_query): compare stripped
        if (q is not None and not q.used and memory_action == "search" and isinstance(text, str)
                and text.strip() == q.text.strip()):
            q.used = True
            return self.embed_media(text, q.items, "search")
        # v0.22 M4: ctx-safe truncation of the embedding input only (storage keeps
        # the full text). Prevents a context overflow -> llama-server 500 ->
        # silent memory loss on token-dense records.
        prefixed = self._prefix(memory_action) + _truncate_for_embedding(text, self._budget)
        # MEM-12: one bounded retry on a llama-swap 429 burst; see module header.
        parent = super()
        return _retry_on_429(lambda: parent.embed(prefixed, memory_action))

    def embed_batch(self, texts, memory_action="add"):
        prefix = self._prefix(memory_action)
        prefixed = [prefix + _truncate_for_embedding(t, self._budget) for t in texts]
        parent = super()
        return _retry_on_429(lambda: parent.embed_batch(prefixed, memory_action))
