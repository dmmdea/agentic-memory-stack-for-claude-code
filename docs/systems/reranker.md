# Reranker — bge-reranker-v2-m3

The cross-encoder that re-orders deliberate search results. It is served by llama-swap on the
authority's loopback, on demand, like every other model in the stack: it is **not** resident,
and the first rerank after an idle spell pays a cold load.

## Purpose

A cross-encoder scores a query and a candidate together (joint attention), which catches
relevance that the embedder's independent vectors miss, especially for short or ambiguous
queries. `mem0-server/reranker.py` is the thin client that calls it and re-orders the search
pool. It is an optional quality stage: search never depends on it.

## Questions this doc answers

- Which model, where does it run, and is it always loaded?
- When does a search rerank, and what does the caller see when the reranker is slow or down?
- What does `rerank_status` mean, including `ok-after-cold-retry`?
- Why did a search after a quiet spell come back in dense order, and what warms the reranker?
- What does the SessionStart pre-warm do for it?

## Scope

`reranker.py` (`rerank`, `warm`, `rerank_health`, the skip rules, the cold-start retry, the
passive counters) and the places `app.py` calls it: `_search_core`, the diagnose probe and
`GET /health/embedder?warm=rerank`.

## Non-scope

Retrieval, admission and recency policy around the rerank ([memory-retrieval flow](../flows/memory-retrieval.md),
[admission gate](./admission-gate.md)); the embedder's own outage handling (`embedder_503.py`,
[mem0 API](./mem0-api.md)); the llama-swap install ([`install/llama-swap-setup.md`](../../install/llama-swap-setup.md)).

## Key concepts

- **Model:** `bge-reranker-v2-m3` (Q4_K_M GGUF, about 0.4 GiB on the card, 8192-token context;
  it replaced the 512-token `bge-reranker-base` design). Endpoint `POST /v1/rerank`
  (llama-server's `--reranking` mode), reached at `127.0.0.1:11436`.
- **Serving:** GPU (`--n-gpu-layers 99`) in llama-swap's `support` group, which is
  `swap: false, exclusive: false` so the support pair co-resides with a chat seat instead of
  queueing behind it. The group has **no residency**: every member carries `ttl: 300`, so the
  reranker unloads after five idle minutes.
- **Cold start:** the first request after the unload waits for the model to load inside the
  caller's timeout. Measured cold requests take about 1 to 6 s; on the reference authority
  about 2 of 21 cold loads outlived the old fixed 8 s timeout and degraded that search.
- **`rerank_status`:** the per-search stamp (see below).
- **Passive counters:** `rerank_stats`, bumped by real search reranks only (W4).

## How the system works

`_search_core` calls `reranker.rerank(query, results, text_key, force=..., status_out=...)`
when the request asked for a rerank and there are results. Skip rules (`skip_reason`): fewer
than `RERANK_MIN_N = 3` results (`small_n`) or a head whose fused score is at or above
`RERANK_SKIP_IF_TOP_SCORE` (`confident`; the profile's `rerank_skip`, 1.0 in every space: the head is
ranked first by every leg of the [hybrid fusion](fusion.md)). `force=True` bypasses both (see W5). Each
document is cut to `RERANK_DOC_MAX_CHARS = 6000` characters before it is sent.

**Timeouts and the cold-start retry.** The first attempt uses `RERANK_TIMEOUT_S = 8.0`. If it
raises `httpx.ReadTimeout` (the model is loading), `rerank` retries **once** with
up to `RERANK_COLD_RETRY_TIMEOUT_S = 20.0`, capped by what is left of the whole-stage ceiling
`RERANK_TOTAL_BUDGET_S = 20.0` (so the shipped retry waits 12 s, and a stage with under
`RERANK_RETRY_MIN_S = 1.0` left does not retry at all). Only a read timeout is retried: a start
failure (an HTTP 5xx from llama-swap) or a refused connection is not a slow load and is not
repeated. The worst case for the rerank stage is therefore 20 s, paid only on a cold or failing
reranker. The ceiling exists because callers wait a fixed time: the MCP shim reads for 30 s and a
read timeout is not a failover there, and `Test-MemoryStack`'s rerank search waits 30 s. A cold
embed (about 3.4 s measured) plus the ceiling stays inside both, so a failing reranker degrades
to dense order instead of surfacing as a caller-side timeout. A test pins the relation to both
callers, so raising either side fails it. `warm()` is not on a search path and keeps the plain 20 s.

**Fail policy (fail-open).** Any error that survives the retry (timeout, 5xx, connection
refused) logs a WARNING (first occurrence and every tenth after), returns the results in dense
order and never reaches the search caller as an error. The response shape is the same either
way; `rerank_status` is what tells the two apart.

**Co-residency failure.** When a large seat holds the card, the reranker (or embedder) may fail
to load at all: llama-swap answers `500 upstream command exited prematurely`. For the reranker
that is the fail-open path above (dense order). For the embedder it is the 503 + `Retry-After`
path, so hook writes queue to the outbox and retry. Neither depends on pinning a model
resident; the models stay on the five-minute TTL.

### `rerank_status`

| Value | Meaning |
|---|---|
| `ran` | The cross-encoder scored this search on the first attempt. |
| `ok-after-cold-retry` | The first attempt timed out while the model cold-loaded and the single budget-bounded retry succeeded. The search is scored exactly like `ran`. |
| `skipped_small_n` / `skipped_confident` | A skip rule fired; the transport was not touched. |
| `failed_fallback_dense` | The transport failed (after the retry, for timeouts); results are in dense order and any lexical-only rescue candidates were dropped fail-closed. |

Code that asks "did the reranker score this?" tests membership in `reranker.RAN_STATUSES`
(`ran`, `ok-after-cold-retry`), never equality with `ran`.

### Warm-up

`reranker.warm()` issues a one-document rerank, waits up to the 20 s cold-start allowance and
never raises (`{"ok": true, "warm_ms": N}` or `{"ok": false, "error": "..."}`). It touches none
of the passive counters: a warm-up is not search traffic and must not read as liveness.
`GET /health/embedder?warm=rerank` embeds one token (the embedder pre-warm) and then calls it,
adding `rerank: {ok, warm_ms | error}` to the response. A reranker that cannot load is reported
there and never fails the endpoint, because search works without it. The SessionStart hook calls
this URL detached (see [codex hooks](./codex-hooks.md)), so the first deliberate search of a
session finds both support models warm. Without `?warm=` the endpoint behaves as before.

## W5 (ADOPT-2/AMS-56): rerank_status, force, and the union-leg contract

- `reranker.rerank(query, results, text_key, *, force=False, status_out=None)` — the list-return signature is pinned; the two additions are keyword-only. `status_out["status"]` reports the values in the table above (the skip reasons were previously collapsed inside `should_rerank`).
- `force=True` bypasses BOTH skip heuristics. The search path passes it whenever keyword-union candidates joined the pool: a silent skip would delete every lexical rescue via the fail-closed drop (`lexical_only` items without a `rerank_score` never reach the caller).
- The confidence skip was measured-inert on mem0's additive score scale (0.92 cut, max observed 0.737 pre-W5). Since the rank fusion (1.34.0) the cut is 1.0, a unanimous head, which the 2026-10-08 lab hit on 0.0-0.1% of searches: still effectively inert. `rerank_status` measures `skipped_confident` occurrences so the constant's behavior is observable rather than assumed (AMS-43).
- Passive liveness counters (W4) are unchanged: skips still bump nothing; `failed_fallback_dense` corresponds to a real transport failure.

## Data and state

`rerank_stats` is in-process and zeroed by a restart: `last_rerank_ok_ts`,
`consecutive_rerank_failures` (reset on success), `ok_total`, `fail_total`, `last_error`, and
`cold_retry_total` (first attempts that timed out and were retried). A search that succeeds on
the retry counts as one success and no failure; a search whose retry also fails counts as one
failure, not two. `/health/deep` reports these passively under `checks.reranker` and never
probes actively (see [mem0 API](./mem0-api.md)); the capability manifest folds them into the
`reranker` row ([capability manifest](../capability-manifest.md)).

## Interfaces and entry points

- `reranker.rerank`, `reranker.warm`, `reranker.rerank_health`, `reranker.RAN_STATUSES`.
- `POST /v1/memories/search` with `rerank: true` (response carries `rerank_status`).
- `GET /health/embedder?warm=rerank` (pre-warm target).

## Failure modes

| Symptom | Cause | What to do |
|---|---|---|
| Search after a quiet spell returned `failed_fallback_dense` | Cold load slower than both attempts, or a start failure | Check the llama-swap log for `upstream command exited prematurely`; the pre-warm normally prevents the cold case. |
| `ok-after-cold-retry` appears often | The reranker is unloading between searches | Expected after idle; the retry absorbed it. Rising `cold_retry_total` with SessionStart pre-warm active means sessions run longer than the TTL between searches. |
| Repeated 500 `exited prematurely` | Not enough VRAM headroom beside a resident seat | Capacity, not client behavior: give the support pair room on the card. The client stays fail-open. |

## Tests

- [`test_support_tier.py`](../../mem0-server/tests/test_support_tier.py) — cold-start retry, `ok-after-cold-retry`, warm-up, against a fake llama-swap.
- [`test_reranker.py`](../../mem0-server/tests/test_reranker.py) — skip rules, `status_out`, `force`, fail-open.
- [`test_embedder_503.py`](../../mem0-server/tests/test_embedder_503.py) — `/health/embedder` including `?warm=rerank` (needs the live-stack imports).

## Related

- [mem0 API](./mem0-api.md) · [memory retrieval flow](../flows/memory-retrieval.md) · [capability manifest](../capability-manifest.md) · [codex hooks](./codex-hooks.md)
- [`../../mem0-server/reranker.py`](../../mem0-server/reranker.py)
