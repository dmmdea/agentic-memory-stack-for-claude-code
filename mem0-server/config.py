"""mem0 v2.0.4 config - v0.12 stack
WSL-native, no Docker. Backends:
- LLM (fallback extractor, fires only when infer=True): local llama-swap. NOT used in
  the hot path - L1a Stop hook and C1 nightly consolidation both use Codex CLI
  (gpt-5.5, ChatGPT subscription OAuth) instead. See ARCHITECTURE.md for the
  history of why this is NOT 'claude --print' (Anthropic Max OAuth concurrency
  block; verified failure documented in CHANGELOG.md).
- Embedder: EmbeddingGemma-300m (multilingual EN/ES) via llama.cpp/llama-swap :11436
  (CPU). Migrated from English-only nomic-embed-text in v0.22 (2026-06-13); full
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

# Embedder transport config, shared by build_config() (for schema validation) and
# build_embedder() (the actual prefix-shim instance app.py installs on the Memory).
EMBEDDER_CONFIG = {
    "model": "embeddinggemma",
    "openai_base_url": "http://localhost:11436/v1",
    "api_key": "sk-noop",
    "embedding_dims": 768,
}


def build_embedder():
    """Return the EmbeddingGemma prefix-shim embedder instance.

    app.py calls this and assigns the result to mem.embedding_model right after
    Memory.from_config(), so every add/search/update goes through the asymmetric
    prefix shim. We can't set provider="egemma" in build_config() because mem0
    2.0.4's EmbedderConfig pydantic validator rejects provider names outside its
    hardcoded allowlist — so build_config() declares the stock "openai" provider
    (same transport) purely to pass validation, and this swap supplies the shim.
    """
    from mem0.configs.embeddings.base import BaseEmbedderConfig
    from egemma_embedder import EmbeddingGemmaEmbedder
    return EmbeddingGemmaEmbedder(BaseEmbedderConfig(**EMBEDDER_CONFIG))


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
                # v0.22: re-embedded EmbeddingGemma vectors live in a NEW collection;
                # the old nomic "memories" collection is retained untouched for rollback.
                "collection_name": "mem0_egemma_768",
                "host": "localhost",
                "port": 6333,
                "embedding_model_dims": 768,
                "on_disk": True,
            },
        },
        "custom_instructions": EXTRACTION_PROMPT,
        "history_db_path": HISTORY_DB_PATH,
    }
