---
status: Proposed
date: "2026-10-08"
---

# Hybrid fusion by rank, not by adding scores

## Context

mem0 ranks every search's dense candidates by adding the raw cosine, a sigmoid-normalised BM25 score and
an entity boost. The terms live on different scales: the keyword and entity terms span most of 0..1, the
candidates' cosines sit within about 0.1-0.2 of each other. The server's durable freshness then multiplies
these closely spaced scores by 0.77-0.90 and re-sorts. On a restored copy of the store the per-prompt path
ranked the right memory first for 70% of paraphrased questions where dense search alone managed 94%, and
EmbeddingGemma-2, whose cosine scale is more compressed, lost even more (audit EVAL-01, the
2026-10-08 lab; [hybrid fusion](../../systems/fusion.md)).

## Decision

1. The server replaces mem0's `score_and_rank` (the module global mem0's search calls by name) with
   weighted reciprocal rank fusion over the dense pool: k = 2, keyword weight 0.4, entity weight 0.25,
   the score normalised to (0, 1]. The raw-cosine gate before fusion and the candidate pool are unchanged.
2. The binding is verified at start and reported on `/health/deep`; an unbound fusion fails the deploy
   gate. `MEM0_FUSION=mem0` restores mem0's own formula.
3. Consumers of the fused score move with it: the reranker's skip cut becomes 1.0 (every leg ranks the
   head first) in every space; the diagnose verdict compares the raw cosine.

## Consequences

- The per-prompt injection gains 0.11-0.22 MRR on paraphrases and identifiers on EmbeddingGemma-300m,
  and freshness becomes the mild recency preference it was meant to be.
- The fusion is scale-free: a future embedding space needs no new fusion constants (its cosine
  thresholds still need calibrating).
- The server depends on a private mem0 binding; the health check (the code mem0 runs, its signature,
  and a count of searches that bypassed the fusion) turns a mem0 refactor into a failed deploy instead
  of a silent regression, and `MEM0_FUSION=mem0` runs such a mem0 on purpose.
- Search results' `score` changes meaning (rank fusion, not a cosine-like sum). Each result now also
  carries its raw `cosine`, and the one reader that compared `score` with a cosine-scale value (the
  admission gate's optional brand-coherence floor, off everywhere) reads `cosine` instead.

## Alternatives considered

- Keep mem0's formula with smaller keyword and entity weights: better than today, still exposed to
  freshness on a compressed scale (lab: below rank fusion on identifiers in both spaces).
- A cosine-anchored sum (cosine plus keyword evidence scaled by the pool's spread): scale-free, but on a
  cosine scale freshness still reorders strongly (lab: identifier 0.43 on EmbeddingGemma-2).
- Fork mem0 or move fusion into Qdrant's query API: more code to own, and the raw-cosine gate would need
  a second query.
