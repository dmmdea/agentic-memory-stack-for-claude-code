# Reranker — bge-reranker base (R2)

> **Status:** DESIGN. Implementation lands in Task C.1. This document describes what C.1 will wire, written ahead. In v0.12 baseline, `SearchIn.rerank` is accepted but ignored.

## What it is

`bge-reranker-base` is a cross-encoder reranking model from BAAI. It scores query-document pairs by joint attention (vs. the embedder's independent encoding), which catches semantic relevance that cosine similarity misses — especially for short, ambiguous queries.

The model is already loaded in llama-swap's `always_loaded` persistent group on `127.0.0.1:11436`, consuming ~250MB VRAM at all times. No cold-start latency for reranking calls.

Context window: 512 tokens. For memories well under the 1500-char limit, this is sufficient for full-text cross-encoding.

## HTTP Endpoint

```
POST http://127.0.0.1:11436/v1/rerank
Content-Type: application/json

{
  "model": "bge-reranker-base",
  "query": "...",
  "documents": ["memory text 1", "memory text 2", ...],
  "top_n": 5
}

Response 200:
{
  "results": [
    {"index": 2, "relevance_score": 0.94},
    {"index": 0, "relevance_score": 0.87},
    ...
  ]
}
```

The `index` field references the original `documents` array position. The reranker returns up to `top_n` results in descending relevance order.

## Wire-Up in mem0-server

`mem0-server/reranker.py` (Task C.1) wraps the HTTP call. `app.py POST /v1/memories/search` calls `reranker.rerank(query, results)` when both conditions hold:

- `SearchIn.rerank == True` in the request body
- Number of results ≥ 3 AND top dense score < 0.92

When only one or two results come back from Qdrant, reranking is skipped (no meaningful reorder signal). When the top dense score is already ≥ 0.92, the dense retrieval is already high-confidence and reranking adds latency without benefit.

## Fail Policy

Any error from the reranker (timeout, 5xx, connection refused) is handled fail-open:
1. Log a WARNING at `WARN` level (first occurrence + every 10th thereafter, to suppress log spam)
2. Return results in dense-only order
3. Never propagate the reranker error to the search caller

The search response is identical in shape whether reranking succeeded or failed; callers cannot distinguish the two paths from the response body.

## Model Trade-Off: base vs v2-m3

| | bge-reranker-base | bge-reranker-v2-m3 |
|---|---|---|
| Context | 512 tokens | 8192 tokens |
| VRAM (Q4_K_M GGUF) | ~250MB | ~419MB |
| BEIR benchmark | Strong | Best-in-class small model |
| Current status | `always_loaded` persistent | On disk; TTL-load only |

Decision for v0.13: keep base in `always_loaded`. The 250MB persistent cost is already paid; adding v2-m3 at ~419MB would push the always-loaded group close to the 700MB practical limit on the Mobile RTX 3070 (8GB VRAM shared with other always-loaded models). Re-evaluate after Phase C re-extraction lands and retrieval quality can be measured empirically with a blind eval set.
