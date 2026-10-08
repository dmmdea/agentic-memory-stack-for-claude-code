# Hybrid Fusion

## Purpose

Ranks the candidates of every memory search by what the query means, with keyword and entity
evidence as support, in a way that holds in every embedding space and survives the server's durable
freshness. It replaces mem0's own ranking at the single point mem0 calls it, without forking mem0.

## Questions this doc answers

- How does a search turn a cosine, a BM25 score and an entity boost into one order?
- Why did mem0's own formula rank badly, and why worse on EmbeddingGemma-2?
- What does a search result's `score` mean now, and who may threshold on it?
- How is the binding checked, and how do I roll back?
- What was measured, and how do I re-measure after a change?

## Scope

`mem0-server/fusion.py` (the ranking and its binding), the server start (`app.py` binds it right after
the embedder swap), `/health/deep` (`checks.fusion`), the consumers of the fused score (the reranker's
skip cut, durable freshness, the diagnose verdict) and the lab evaluation in the private operator repo
(`eval/embedder-ab/`).

## Non-scope

What becomes a candidate: mem0 still builds the pool from the dense search (`max(limit * 4, 60)`
nearest neighbours under the request's filters), the keyword leg (BM25 over the same filters) and the
entity leg (entity-store matches). The raw-cosine gates (the context-bundle relevance gate, the NLI
pre-filter) stay where they were. The reranker ([reranker](reranker.md)) reorders the fused list on
deliberate searches and is unchanged.

## Key concepts

- **Dense pool** — mem0's nearest-neighbour candidates for the query, each with its raw cosine.
- **Legs** — the dense rank (by cosine), the keyword rank (by mem0's sigmoid-normalised BM25, among the
  candidates that have a keyword match) and the entity rank (by mem0's entity boost, among the
  candidates that have one).
- **Fused score** — `score` on every search result:
  `[1/(k + r_dense) + w_bm25/(k + r_bm25) + w_entity/(k + r_entity)] / [(1 + w_bm25 + w_entity)/(k + 1)]`
  with k = 2, w_bm25 = 0.4, w_entity = 0.25. It lies in (0, 1], is 1.0 only for a candidate ranked first
  by every leg, and orders the results. It is not a cosine.
- **Raw cosine** — still what every relevance threshold compares (the gate runs before fusion).

## How the system works

mem0 2.0.4 and 2.1.0 compute the three legs, then call the module global
`mem0.memory.main.score_and_rank(semantic_results, bm25_scores, entity_boosts, threshold, top_k)`,
looked up by name in both the sync and the async search. At start the server assigns
`fusion.ams_score_and_rank` to that global (`fusion.install`). The replacement drops candidates whose
raw cosine is below the caller's threshold, ranks the rest by weighted reciprocal rank fusion over the
legs, and returns the top `top_k` as `{id, score, payload}`; mem0 formats them as before. Each call also
records every candidate's legs (raw cosine, ranks, BM25 value, entity boost) for that request.

### Why mem0's own formula ranked badly

mem0 adds the legs: `min((cosine + sigmoid(bm25) + entity_boost) / max_possible, 1.0)`. The keyword term
spans most of 0..1 and the entity term up to 0.5, while the cosines of one query's candidates sit within
about 0.2 of each other on EmbeddingGemma-300m and about 0.1 on EmbeddingGemma-2. The keyword and entity
terms therefore outvote the meaning of the query. The server's durable freshness then multiplies the
scores of evidence-tier memories by 0.77-0.90 (their age) and re-sorts: on closely spaced scores that
lets age outvote relevance as well, the more so the more compressed the model's cosine scale is.

Rank fusion fixes both. Ranks ignore how compressed a cosine scale is, and the fused scores keep the
top ranks well apart (with the dense leg alone, rank 1, 2, 3 score 0.61, 0.45, 0.36), so a freshness
weight moves a memory a rank or two instead of to the bottom.

### The measurement (2026-10-08 lab)

A restored copy of the 2026-10-07 store (16,945 memories) in a lab Qdrant; the queries ran through the
server's own `Memory` object on mem0 2.0.4 (the authority's version, with its fastembed and spaCy), and a
recorder captured exactly what the fusion receives. Two sets: 240 memories with a natural English and
Spanish question each that avoids exact identifiers (scored on the 160 that a brandless durable search
can return), and 200 memories with a rare identifier (a version, file, host, port or model id) in an
English, a Spanish and a terse keyword prompt, where every memory holding the identifier counts. Every
stage after the fusion (retired and intent filters, durable freshness, admission, trim) was replayed with
the server's own modules; a lab server built from this change then answered all 1,080 queries and
returned exactly the replayed top 10 for every one, in both fusion modes.

MRR@10 on EmbeddingGemma-300m (the memories' space), freshness on as in production:

| query | mem0 additive | dense only | rank fusion |
|---|---|---|---|
| paraphrase, EN / ES | 0.758 / 0.735 | 0.970 / 0.956 | 0.972 / 0.959 |
| identifier, EN / ES / terse | 0.490 / 0.598 / 0.708 | 0.592 / 0.587 / 0.834 | 0.652 / 0.666 / 0.904 |
| injected per prompt (top 2 kept): paraphrase EN / ES | 0.622 / 0.656 | 0.838 / 0.825 | 0.838 / 0.828 |
| injected per prompt: identifier terse | 0.685 | 0.812 | 0.877 |

Rank fusion against mem0's formula: +0.21 to +0.22 on paraphrases (47 queries better, 2 worse in
English), +0.07 to +0.20 on identifiers, +0.11 to +0.22 on what the per-prompt hook injects; every 95%
paired-bootstrap interval excludes zero, and the even and odd halves of the item list agree. Against
dense only: a tie on paraphrases (+0.003) and +0.04 to +0.08 on identifiers.

The same matrix with freshness off shows the second defect: mem0's formula recovers 0.06 (paraphrase)
and 0.11 (identifier) MRR on EmbeddingGemma-300m without freshness, rank fusion moves by less than 0.01.
On EmbeddingGemma-2 the effect is extreme (identifier 0.364 with freshness, 0.695 without, under mem0's
formula).

How the constants were chosen: a grid over k (1-60) and the keyword weight (0.1-0.8) traces a frontier
from paraphrase recall to identifier recall. The shipped point is the one that loses nothing to dense
search on paraphrases and then gains most on identifiers; k = 2 is also Qdrant's own RRF default, and the
surface is flat between k = 1 and 3. The entity weight made no measurable difference on either set
and stays at 0.25, as mem0 designed the leg.

### The embedding-model question

Under rank fusion EmbeddingGemma-2 recovers most of what mem0's formula cost it (paraphrase 0.944 EN
against 0.542) but still trails EmbeddingGemma-300m in every cell (by 0.01-0.04, significantly in four
of nine). The memories stay on EmbeddingGemma-300m ([embedder profiles](embedder-profiles.md)).

## Important flows

- **Rollback**: `MEM0_FUSION=mem0` in the server's environment (unit drop-in or stack.env) and a restart:
  the binding stays, the ranking is mem0's own formula again (measured identical to stock mem0).
- **After a mem0 upgrade**: `/health/deep` `checks.fusion.bound` must be true. It is false when mem0
  stops calling the module global (`Memory._search_vector_store` no longer names `score_and_rank`), and
  that flips `ok`, so the installers' post-condition fails instead of the stack silently ranking the old
  way.
- **Re-measuring**: the private operator repo's `eval/embedder-ab/scripts` (`capture_mem0.py`,
  `offline_fusion.py`, `analyze_fusion.py`) re-run the whole matrix in minutes from one capture pass.

## Interfaces and entry points

`fusion.install()` (server start), `fusion.ams_score_and_rank` (mem0's hook), `fusion.last_legs()` (the
current request's legs), `GET /health/deep` `checks.fusion` (`{ok, mode, bound, callers_bound}`),
`MEM0_FUSION` (`rrf` default, `mem0`). `POST /v1/memories/search` with `explain` passes `score_details`
(ranks and raw values per leg) through mem0 2.1.0.

## Invariants and assumptions

- The caller's threshold is compared to the raw cosine, before fusion, exactly as mem0 does.
- Candidates are the dense pool only; a keyword-only memory is not a candidate here (the deliberate
  search's union leg in `app.py` is the one place that adds those, and only the reranker can keep them).
- `score` is in (0, 1] and monotone with the order; equal scores keep the dense order.
- Nothing may threshold on `score` as if it were a cosine.

## Error handling

`fusion.install` never raises: an unexpected mem0 shape is reported in `checks.fusion.error` and
`bound` reads false. An unknown `MEM0_FUSION` value falls back to `rrf`.

## Observability and debugging

`/health/deep` `checks.fusion`; the `explain` trace on a search; `POST /v1/memories/diagnose` reports
the target's fused `score` and its raw `cosine`, and its threshold verdict compares the cosine (the live
gate's footing).

## Testing notes

`mem0-server/tests/test_fusion.py` (pure, CI-gated): the raw-cosine gate, the formula, rank-only
behaviour (a compressed space gives identical scores), the spacing against a 0.8 freshness weight, a
keyword match lifting a near-top candidate without burying the dense leader, the rollback mode, the
binding self-check (bound and unbound) and the server wiring.

## Common pitfalls

- The fused score is not a cosine: the reranker's skip cut is 1.0 (every leg ranks the head first) in
  every space, and the admission gate's optional brand-coherence floor (off everywhere) would need a value
  on this scale if it is ever enabled.
- A change to freshness, the pool size or the legs moves the ranking: re-run the lab matrix.

## Source map

`mem0-server/fusion.py`, `mem0-server/app.py` (binding, `/health/deep`, diagnose),
`mem0-server/reranker.py` (skip cut), `mem0-server/embedder_profile.py` (`rerank_skip`),
`mem0-server/freshness.py` (durable freshness).

## Related docs

[embedder profiles](embedder-profiles.md), [reranker](reranker.md), [mem0 API](mem0-api.md),
[ADR: hybrid fusion by rank](../architecture/decisions/hybrid-fusion-rank.md).
