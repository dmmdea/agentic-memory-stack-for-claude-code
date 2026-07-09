# mem0 REST API + MCP surface

The mem0-server is a FastAPI wrapper (`mem0-server/app.py`) around mem0 v2.0.4, running on `127.0.0.1:18791` (loopback-only, never `0.0.0.0`). It owns all memory reads and writes. The MCP shim (`scripts/wsl/mem0-mcp-shim.py`) translates stdio MCP calls to HTTP against this server; Claude Code sees only the MCP tools and never calls the REST API directly.

## Auth

Every REST request requires `X-API-Key: <key>` as a header. The key is stored in `~/.mem0/api-key` (WSL, mode 600). The MCP shim reads the same file at startup; callers outside the shim must supply it manually.

Missing or incorrect key → `401 {"detail": "missing or invalid X-API-Key"}`.

## REST Endpoints

### `GET /health`

Shallow liveness probe. Returns within ~50ms.

```
Response 200: {"ok": true, "version": "2.0.4-v012", "stack": "<stack semver>", "store": "qdrant", "embedder": "embeddinggemma-300m"}
# NOTE: "version" is deliberately PINNED to the historical "2.0.4-v012" (dashboards pattern-match it);
# the release version of the stack is the separate "stack" key.
```

Use for liveness checks (hooks, Test-MemoryStack). Do **not** use for "write path working" — use `/health/deep` for that.

### `GET /health/deep`

Checks Qdrant collection status, EmbeddingGemma embedder dimension (via llama-swap), and mem0 collection point count. Slow (~1-3s). Use for diagnostics, not polling.

```
Response 200: {"ok": true, "checks": {"qdrant": {"ok": true, "points": N, "status": "green"}, "embedder": {"ok": true, "dim": 768}}}
Response 200 (degraded): {"ok": false, "checks": {"qdrant": {"ok": false, "error": "..."}}}
```

### `POST /v1/memories`

Add one or more memories.

```json
Request: {
  "messages": "<string> | [{"role":"user","content":"..."},...] | {"content":"..."}",
  "user_id": "youruser",
  "infer": false,
  "metadata": {"tier": "evidence", "source": "l1a-extractor", ...}
}
```

- `infer=false` stores as-is (used by all automated paths). `infer=true` runs mem0's LLM extraction pipeline.
- **Tier restrictions on add (server-enforced):**
  - `tier=canonical` → `403` always. Add as `evidence`, promote via PATCH.
  - `tier=insight` → `403` unless `metadata.source` contains `c1` or `consolidator`.
  - `tier=evidence` or `tier=temporal` → allowed.
  - No metadata.tier → defaults to no tier label (retrieved as untiered evidence).
- **Size limit:** `MAX_MEMORY_CHARS = 4000` (env-overridable via `MEM0_MAX_MEMORY_CHARS`). Payload above this → `413`. Break into atomic facts.

```
Response 200: {"results": [{"id": "<uuid>", "memory": "...", ...}]}
Response 403: tier enforcement or insight-source missing
Response 413: payload exceeds MAX_MEMORY_CHARS
Response 500: Qdrant/llama-swap unreachable
```

### `GET /v1/memories`

List all memories for a user. Hard-capped server-side at 500 regardless of caller's `limit`.

```
GET /v1/memories?user_id=youruser&limit=100
Response 200: {"results": [...]}
```

Prefer `POST /v1/memories/search` for content discovery. Use list for inventory/audit only.

### `POST /v1/memories/search`

Semantic search via embedder → Qdrant cosine ANN → optional bge-reranker cross-encoder reorder.

```json
Request: {
  "query": "...",
  "filters": {"user_id": "youruser"},
  "limit": 5,
  "threshold": 0.1,
  "rerank": false
}
```

- `rerank=true` triggers bge-reranker (R2) post-processing when ≥ 3 results and top score < 0.92. Implementation lands in Task C.1 — currently accepted but ignored in v0.12 baseline.
- `limit` clamped at 500 server-side.

```
Response 200: {"results": [{"id": "...", "memory": "...", "score": 0.83, "metadata": {...}}, ...]}
```

### `PUT /v1/memories/{id}`

Update a memory's text content. Does not change tier.

```json
Request: {"text": "new content"}
Response 200: mem0 update result
Response 500: Qdrant unreachable
```

### `PATCH /v1/memories/{id}/tier`

Promote or demote a memory's tier. Server-enforced actor requirements. Writes one ledger line to `~/.mem0/tier-ledger.jsonl` after the Qdrant payload update succeeds.

```json
Request: {"tier": "canonical", "actor": "user-direct", "reason": "the operator said to lock this in"}
```

- `actor` is required. Allowed values: `user-direct`, `c1-consolidator`, `dream-consolidator`, `claude-autonomous`.
- `tier=canonical` requires `actor=user-direct` and non-empty `reason`. Any other actor → `403`.
- `tier=insight` requires actor containing `c1` or `consolidator`. Any other actor → `403`.
- `tier in {evidence, stable, temporal}` accepts `claude-autonomous`.

```
Response 200: {"ok": true, "memory_id": "...", "tier": "canonical", "actor": "user-direct", "ts": "2026-..."}
Response 400: missing actor, missing reason for canonical, invalid tier
Response 403: actor/tier enforcement rejected
```

### `PATCH /v1/memories/{id}/metadata`

Partial metadata update (merge, not replace). Added in Task C.1 for re-extraction and decay scanner use. Not present in v0.12 baseline — note "lands in Phase C."

### `DELETE /v1/memories/{id}`

Delete a memory by ID. Writes a ledger line with `event=decay-delete` when called by the decay scanner (Task D.2 adds this convention).

```
Response 200: mem0 delete result
```

## MCP Tool Wrappers

The shim (`scripts/wsl/mem0-mcp-shim.py`) exposes these tools to Claude Code:

- `memory_add(text, user_id, infer, metadata)` — POST /v1/memories
- `memory_search(query, user_id, limit, threshold)` — POST /v1/memories/search
- `memory_list(user_id, limit)` — GET /v1/memories (limit hard-clamped at 500 client-side too)
- `memory_update(memory_id, text)` — PUT /v1/memories/{id}
- `memory_promote(memory_id, tier, actor, reason)` — PATCH /v1/memories/{id}/tier
- `memory_demote(memory_id, tier, actor, reason)` — PATCH /v1/memories/{id}/tier (same endpoint, different direction)
- `memory_delete(memory_id)` — DELETE /v1/memories/{id}
- `memory_health()` — GET /health

## Common Pitfalls

- **Forgetting `X-API-Key`** → `401`. The shim handles this; direct REST callers must set the header.
- **Passing `tier=canonical` to POST** → `403`. This is intentional — add as `evidence`, then promote via PATCH.
- **Oversize payload** → `413` with message "Break into atomic facts." The 1500-char cap is per-memory, not per-request batch.
- **`infer=true` for hook-extracted facts** → incorrect behavior: mem0's LLM extraction re-processes the already-extracted fact, possibly splitting or altering it. Always use `infer=false` from automated paths.
- **Calling `/health` to verify write path** → misleading green. Use `/health/deep` or run a test round-trip.

See `docs/modular/tier-policy.md` for the full tier rule table.
