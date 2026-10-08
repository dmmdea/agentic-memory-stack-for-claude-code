---
status: Proposed
date: "2026-10-08"
---

# Embedder profiles: one definition per embedding space, the wiki in its own

## Context

The embedder was an implicit constant spread over about 70 files: a model alias, the 768-d width, the
task prefixes, a 2,048-token budget, three collection names under three different environment names,
and seven cosine thresholds fitted to EmbeddingGemma-300m's score scale. Two defects followed from it:
chain steps outside the server unit fell back to the stock `embeddinggemma` alias, a different
conversion than the store's file (audit F-01, 2026-10-08), and every health check verified only
`dim == 768`, which cannot tell two 768-d models apart (F-02).

EmbeddingGemma-2 (2026-10-06; 270M text parameters, 768-d, 8,192-token window, same task prefixes)
raised the question of replacing the embedder. A lab A/B on a restored copy of the store measured it
(see `docs/systems/embedder-profiles.md`): on short memory facts it is slightly worse dense-only
(MRR −0.02) and much worse through mem0's additive hybrid fusion on the per-prompt path (−0.13 EN,
−0.11 ES), because its cosine scale is compressed and high; on whole wiki pages it is better than
EmbeddingGemma-300m's best recipe on detail questions (+0.10, CI [+0.016, +0.194]).

## Decision

1. An **embedder profile** (`mem0-server/embedder_profile.py`) defines each embedding space: model alias,
   served context and token budget, task prefixes, template version, collection names (named by model,
   never reused across spaces) and calibrated thresholds. Every component resolves these through it.
2. **The memories' space** is `MEM0_EMBED_PROFILE`, default `egemma-300m`; behaviour with nothing set is
   unchanged. The memories, entities and episodes stay on EmbeddingGemma-300m.
3. **The wiki index has its own space**, `MEM0_WIKI_EMBED_PROFILE`; the operator's authority sets it to
   `egemma2` and embeds whole pages through the 4K alias.
4. Moving the memories to another space is done only with `scripts/wsl/embedder-migrate.py` (new
   collections beside the old ones, verify, then a catch-up with writes stopped and the switch through the
   installer), after the house evals show the new space is at least as good through the per-prompt path.

## Consequences

- F-01 and F-02 close: every embedder resolves the same alias, and `/health` and `/health/deep` report
  the profile, model and template version.
- A future embedder is one profile entry plus a measured migration; the thresholds for it must be
  calibrated (the egemma2 set shows how: probe sets, a nearest-neighbour quantile map, the dedup tail).
- The wiki needs an EmbeddingGemma-2-capable llama.cpp (b11452+) and the `embeddinggemma2` alias on the
  authority; a PC that refreshes the wiki without serving it sends its builds and searches to the brain.
- Two embedders can be resident on the authority's card during a wiki build (about 0.7 GiB each).

## Alternatives considered

- **Replace the embedder everywhere with EmbeddingGemma-2** — rejected on the measurement: the
  per-prompt path loses about a quarter of its MRR.
- **Keep everything on EmbeddingGemma-300m** — leaves the wiki's measured gain on the table and keeps
  the literals that caused F-01.
- **Rescale EmbeddingGemma-2's cosine before fusion** — possible, but the dense-only result is already
  slightly worse, so the best case is parity; not worth the complexity now.
- **An 8K wiki alias** — measured +0.023 MRR (not significant) for about 600 MiB more VRAM; available per
  box, not the default.

## Related code

- [`mem0-server/embedder_profile.py`](../../../mem0-server/embedder_profile.py)
- [`scripts/wsl/embedder-migrate.py`](../../../scripts/wsl/embedder-migrate.py)

## Related docs

- [Embedder profiles](../../systems/embedder-profiles.md)
- [EmbeddingGemma on llama-swap](embeddinggemma-on-llama-swap.md) — the memories' embedder, unchanged.
