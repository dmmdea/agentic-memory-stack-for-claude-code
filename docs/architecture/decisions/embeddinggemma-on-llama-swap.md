---
status: Accepted
date: "2026-06-13"
---

# EmbeddingGemma-300m on llama-swap as the embedder

## Context

The memory corpus is bilingual (English + Spanish). The previous embedder, `nomic-embed-text` served
via Ollama, is structurally English-only — a measured defect: on 30 real query pairs its Spanish
recall@1 was **0.33** versus **0.93** for EmbeddingGemma-300m (English was a tie, ≈0.9). Poor
embeddings silently cap retrieval quality no matter how good the tiers and gate layered above them.

## Decision

Serve **EmbeddingGemma-300m on llama-swap (`:11436`, loopback)** as the embedder, and decommission
Ollama entirely. EmbeddingGemma needs *asymmetric task prefixes* (a different prefix for a query than
for a stored document) that neither llama.cpp nor stock mem0 applies; a server-side shim,
`egemma_embedder.py`, installs that prefixing onto the mem0 embedding model at server start. The
migration re-embedded the full corpus into a new 768-d collection (`mem0_egemma_768`) because the two
models' vector spaces are not comparable, and recalibrated the semantic search gate from **0.4 →
0.30** to match EmbeddingGemma's more compressed cosine scale.

## Consequences

- Spanish recall rises to parity with English; retrieval is bilingual by default.
- A one-time full re-embed was required, and the search gate had to be re-tuned (0.30 is calibrated,
  not arbitrary — raising it to 0.35 was measured to crater recall).
- The embedder runs locally at zero marginal cost, consistent with the "local models embed and rerank
  only" boundary.

## Alternatives considered

- **`nomic-embed-text` (via Ollama)** — rejected on a measured Spanish-recall defect (0.33 vs 0.93);
  it is structurally English-only.
- **Ollama as the serving runtime** — fully decommissioned; llama-swap already fronts the embedder and
  reranker on loopback `:11436`.
- **An earlier EmbeddingGemma trial** had been wrongly rejected — that test predated the prefix shim,
  so it measured EmbeddingGemma without its required asymmetric prefixes.

## Related code

- [`mem0-server/egemma_embedder.py`](../../../mem0-server/egemma_embedder.py) — the asymmetric task-prefix shim.

## Related docs

- [`llama-swap-binding.md`](../../systems/llama-swap-binding.md) — the `:11436` loopback binding that serves the embedder.
- [`mem0-api.md`](../../systems/mem0-api.md) — the server that installs the prefix shim and owns the search gate.
