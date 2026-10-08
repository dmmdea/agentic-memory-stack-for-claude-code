"""mem0 v2.0.4 config - v0.12 stack
WSL-native, no Docker. Backends:
- LLM (fallback extractor, fires only when infer=True): local llama-swap. NOT used in
  the hot path - L1a Stop hook and C1 nightly consolidation both use Codex CLI
  (per-job model, ChatGPT subscription OAuth) instead. See ARCHITECTURE.md for the
  history of why this is NOT 'claude --print' (Anthropic Max OAuth concurrency
  block; verified failure documented in CHANGELOG.md).
- Embedder: EmbeddingGemma-300m (multilingual EN/ES) via llama.cpp/llama-swap :11436
  (a GPU model, unloaded after 300 s idle). Migrated from English-only nomic-embed-text in v0.22 (2026-06-13); full
  corpus re-embedded into Qdrant collection mem0_egemma_768. Ollama fully
  decommissioned by this change. The model needs asymmetric task prefixes that
  mem0's stock embedder won't apply, so a custom prefix-shim embedder
  (egemma_embedder.EmbeddingGemmaEmbedder) is installed onto the Memory instance via
  build_embedder() — app.py swaps mem.embedding_model after Memory.from_config.
  (The config below declares provider=openai so mem0's pydantic schema validates;
  mem0 2.0.4 hardcodes an embedder-provider allowlist that excludes custom names.)
- Vector store: Qdrant 1.18.2 systemd-user on :6333 (loopback-bound per audit fix
  2026-06-08; was previously on 0.0.0.0 by default).
"""
from pathlib import Path

import embedder_profile

# The embedding space this server is bound to (embedder_profile: model alias, prefixes, token
# budget and collections, resolved once at import from MEM0_EMBED_PROFILE / stack.env).
EMBED_PROFILE = embedder_profile.active()

# Embedder transport config, shared by build_config() (for schema validation) and
# build_embedder() (the actual prefix-shim instance app.py installs on the Memory).
EMBEDDER_CONFIG = {
    # The llama-swap model name. The store is bound to the exact GGUF it was embedded with; a box
    # whose stock alias is a different conversion serves the matching file under another name (the
    # native authority: embeddinggemma-ams). embedder_profile.embed_model() scopes that override to
    # its profile.
    "model": embedder_profile.embed_model(EMBED_PROFILE),
    "openai_base_url": embedder_profile.base_url(),
    "api_key": "sk-noop",
    "embedding_dims": EMBED_PROFILE.dims,
}
MEMORIES_COLLECTION = embedder_profile.collection("memories", EMBED_PROFILE)


def build_embedder(long: bool = False, profile=None):
    """Return the EmbeddingGemma prefix-shim embedder instance.

    profile: the embedding space to embed in (default: the server's, EMBED_PROFILE). The wiki
    indexer passes embedder_profile.wiki_profile(), which may be another space than the memories'.
    long=True returns that space served for long documents (wiki pages): the profile's long alias
    and long token budget (embedder_profile.long_model), or the hot alias with the hot budget when
    the profile or the box has none, so a caller never needs to care.

    app.py calls this and assigns the result to mem.embedding_model right after
    Memory.from_config(), so every add/search/update goes through the asymmetric
    prefix shim. We can't set provider="egemma" in build_config() because mem0
    2.0.4's EmbedderConfig pydantic validator rejects provider names outside its
    hardcoded allowlist — so build_config() declares the stock "openai" provider
    (same transport) purely to pass validation, and this swap supplies the shim.
    """
    from mem0.configs.embeddings.base import BaseEmbedderConfig
    from egemma_embedder import EmbeddingGemmaEmbedder, _PREFIX_TOKEN_RESERVE
    p = profile or EMBED_PROFILE
    cfg = dict(EMBEDDER_CONFIG) if p.name == EMBED_PROFILE.name else {
        **EMBEDDER_CONFIG, "model": embedder_profile.embed_model(p), "embedding_dims": p.dims}
    if not long:
        return EmbeddingGemmaEmbedder(BaseEmbedderConfig(**cfg), profile=p)
    alias, budget = embedder_profile.long_model(p)
    emb = EmbeddingGemmaEmbedder(BaseEmbedderConfig(**{**cfg, "model": alias}), profile=p)
    emb._budget = budget - _PREFIX_TOKEN_RESERVE
    return emb


EXTRACTION_PROMPT = """You extract memorable facts from conversation chunks.
Output STRICT JSON only — no prose, no markdown fences, no commentary, no preamble:
{"facts": ["...", "..."]}

Rules:
- Maximum 10 facts per call.
- Each fact <= 25 words, self-contained, declarative.
- Keep proper nouns, dates, numbers, paths, IDs verbatim.
- Drop pleasantries, meta-commentary, hypotheticals, questions, transient state.
- Prefer durable facts: user preferences, decisions, identity, relationships.
- If nothing memorable, return {"facts": []}.
"""

HISTORY_DB_PATH = str(Path.home() / ".mem0" / "history.db")

def build_config() -> dict:
    return {
        "version": "v1.1",
        "llm": {
            "provider": "openai",
            "config": {
                # FALLBACK extractor (fires only when caller passes infer=True). The L1a Stop
                # hook and C1 consolidator both call POST /v1/memories with infer=False, so
                # this model is essentially never invoked in normal operation. Kept for
                # completeness in case mem0 ever wants to extract from raw message dicts.
                "model": "llama-3.2-3b",   # llama-swap alias; cheap any-tier dense model in the catalog
                "openai_base_url": "http://localhost:11436/v1",
                "api_key": "sk-noop",
                "temperature": 0.1,
                "max_tokens": 1024,
            },
        },
        "embedder": {
            # v0.22 EmbeddingGemma migration (2026-06-13): multilingual EN/ES embedder
            # served on llama.cpp/llama-swap :11436, via the OpenAI-compatible transport.
            # Declared as "openai" only to satisfy mem0 2.0.4's EmbedderConfig provider
            # allowlist; app.py immediately swaps mem.embedding_model for the prefix-shim
            # (config.build_embedder / egemma_embedder.py) that prepends the asymmetric
            # task prefixes EmbeddingGemma requires (query vs document). mem0's stock
            # embedder ignores memory_action and would degrade ES retrieval.
            # Replaces nomic-embed-text via Ollama :11435; Ollama is decommissioned.
            "provider": "openai",
            "config": dict(EMBEDDER_CONFIG),
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                # One collection per embedding space (embedder_profile): mem0_egemma_768 for
                # EmbeddingGemma-300m, mem0_eg2_768 for EmbeddingGemma-2. A space change builds
                # the new collection beside the old one (scripts/wsl/embedder-migrate.py).
                "collection_name": MEMORIES_COLLECTION,
                "host": "localhost",
                "port": int(__import__("os").environ.get("MEM0_QDRANT_PORT", "6333")),
                "embedding_model_dims": EMBED_PROFILE.dims,
                "on_disk": True,
            },
        },
        "custom_instructions": EXTRACTION_PROMPT,
        "history_db_path": HISTORY_DB_PATH,
    }
