# mem0 REST API + MCP surface

## Purpose

The mem0-server is a FastAPI wrapper (`mem0-server/app.py`) around mem0 2.0.4, running on `127.0.0.1:18791` (loopback-only, never `0.0.0.0`). It owns all memory reads and writes. The MCP shim (`scripts/wsl/mem0-mcp-shim.py`) translates stdio MCP calls to HTTP against this server; Claude Code sees only the MCP tools and never calls the REST API directly.

## Questions this doc answers

- What is the REST surface, and how does authentication work?
- Which tiers can be set on `add` vs `PATCH /tier`, and what does the server enforce?
- What are the size caps, hard limits, and idempotency guarantees on writes?
- Which MCP tools map to which routes?
- What are the common failure codes and their causes?

## Scope

The HTTP REST surface (health, memories CRUD + search, tier/metadata mutation), the `X-API-Key` auth, the server-enforced tier admission on writes, and the MCP tool wrappers the shim exposes. Goals/open-questions/episodes/bundle routes exist on the same server but are documented in their own system docs.

## Non-scope

- **Tier semantics** (what each tier *means*, its lifecycle) → [`memory-model.md`](./memory-model.md) and [`tier-policy.md`](./tier-policy.md).
- **Read-side admission policy** (which query class admits which tier) → [`admission-gate.md`](./admission-gate.md).
- **The HMAC canonical key** (DPAPI storage, rotation, recovery) → [`dpapi-canonical-key.md`](./dpapi-canonical-key.md).
- **Reconciliation** (supersession/contradiction hiding) → [`reconciliation.md`](./reconciliation.md).

## Key concepts

- **`X-API-Key`** — the single shared auth header every REST call must carry.
- **Tier gate on write** — the server, not the caller, decides which tier a write may land in; `canonical` is never writable via `add`.
- **`infer`** — `false` stores the payload as-is (all automated paths); `true` runs mem0's LLM extraction.
- **Query class** — a search-time mode (`durable`/`operational`/`canonical`/`history`) that selects the admitted tiers and recency policy.

## How the system works

### Auth

Every REST request requires `X-API-Key: <key>` as a header. The key is stored in `~/.mem0/api-key` (WSL, mode 600) and compared with `hmac.compare_digest`. The MCP shim reads the same file at startup; callers outside the shim must supply it manually.

Missing or incorrect key → `401 {"detail": "missing or invalid X-API-Key"}`.

### The write path

`POST /v1/memories` runs a fixed, order-dependent pipeline before it stores anything: (1) size cap (`MAX_MEMORY_CHARS`, default 4000) → `413`; (2) empty-string guard → `400`; (3) the tier gate (below); (4) a strip of caller-supplied retrieval-gating metadata keys the caller must not be able to forge (`contradicts_canonical`, `superseded_by`, `retrievable`, …); (5) hash idempotency — on `infer=false`, an exact-hash duplicate in the same `(user_id, workspace, project)` scope returns the existing id and writes nothing. The tier gate and metadata strip run **before** dedup by design, so a rejected write is rejected whether or not its text already existed.

### The read path

`POST /v1/memories/search` (and the internal `/v1/context/bundle`) share `_search_core`: embed → Qdrant cosine ANN → optional rerank → query-class recency policy → the server-side admission gate. Retired (`retrievable=false`) and `_canonical_intent` records are filtered out unless explicitly opted in.

## Important flows

The end-to-end capture and retrieval paths that drive this API are documented as flows: [`../flows/memory-capture.md`](../flows/memory-capture.md) and [`../flows/memory-retrieval.md`](../flows/memory-retrieval.md).

## Data and state

- **Vector store:** Qdrant collection `mem0_egemma_768` (768-dim EmbeddingGemma vectors) on `:6333`; tier and metadata live in each point's payload.
- **History:** mem0's `~/.mem0/history.db` (SQLite).
- **Tier ledger:** append-only `~/.mem0/tier-ledger-YYYY-MM.jsonl` (monthly segments; the legacy `tier-ledger.jsonl` is a frozen archive) — every tier change, metadata merge, and decay-delete lands here.
- **API key:** `~/.mem0/api-key` (mode 600). **Canonical HMAC key:** resolved via the DPAPI provider (see [`dpapi-canonical-key.md`](./dpapi-canonical-key.md)).

## Interfaces and entry points

### `GET /health`

Shallow liveness probe. Returns within ~50ms.

```
Response 200: {"ok": true, "version": "2.0.4-v012", "stack": "<stack semver>", "store": "qdrant", "embedder": "embeddinggemma-300m"}
# NOTE: "version" is deliberately PINNED to the historical "2.0.4-v012" (dashboards pattern-match it);
# the release version of the stack is the separate "stack" key.
```

Use for liveness checks (hooks, Test-MemoryStack). Do **not** use for "write path working" — use `/health/deep` for that.

### `GET /health/deep`

Checks Qdrant collection status, EmbeddingGemma embedder dimension (via llama-swap), and mem0 collection point count. Also surfaces the canonical-key health, admission-rejection counters, and pending contradiction-review depth. Slow (~1-3s). Use for diagnostics, not polling.

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
  - `tier=canonical` → `403` always. Add as `evidence`, promote via the HMAC-signed `PATCH /tier` (`mem0-canonize.sh`).
  - `tier=insight` → `403` unless `metadata.source` is one of the exact consolidator allowlist actors (`c1-consolidator`, `dream-consolidator`, `c1-dream-consolidator`). The old substring check (`"c1" in source`) was trivially bypassable and was replaced by the exact allowlist `INSIGHT_ALLOWED_ACTORS`.
  - `tier=evidence` or `tier=temporal` → allowed.
  - No metadata.tier → defaults to no tier label (retrieved as untiered evidence).
- **Size limit:** `MAX_MEMORY_CHARS = 4000` (env-overridable via `MEM0_MAX_MEMORY_CHARS`). Payload above this → `413`. Break into atomic facts.
- **Idempotency:** on `infer=false`, a byte-identical memory already stored in the same scope returns the existing id (`"deduplicated": true`) and writes nothing.

```
Response 200: {"results": [{"id": "<uuid>", "memory": "...", ...}]}
Response 400: empty memory
Response 403: tier enforcement or insight-source missing
Response 413: payload exceeds MAX_MEMORY_CHARS
Response 500: Qdrant/llama-swap unreachable
```

### `GET /v1/memories`

List all memories for a user. Hard-capped server-side at 500 regardless of caller's `limit` (passed to mem0 as `top_k`).

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
  "rerank": false,
  "query_class": "durable"
}
```

- `rerank=true` triggers `bge-reranker-v2-m3` post-processing (`reranker.py`), applied only when there are ≥ 3 results **and** the top score is < 0.92 (`RERANK_MIN_N` / `RERANK_SKIP_IF_TOP_SCORE`). The reranker is a CPU cross-encoder served on llama-swap `:11436`; any reranker failure returns the dense-only order unchanged and logs a WARN (fail-soft).
- `query_class` (default `durable`) selects the admitted-tier set and recency policy: `operational` applies a 30-day Weibull recency weight; `canonical` filters to `{canonical, stable}`; `history` disables supersession/contradiction hiding (forensic).
- `limit` clamped at 500 server-side.

```
Response 200: {"results": [{"id": "...", "memory": "...", "score": 0.83, "metadata": {...}}, ...]}
```

### `PUT /v1/memories/{id}`

Update a memory's text content (does not change tier — the tier is restored on the Qdrant payload after the write). Canonical/insight records require a valid HMAC user-direct token (`mem0-canonize.sh --action put`); canonical text is additionally run through the imperative-canary and rejected `422` if it reads as a standing order.

```json
Request: {"text": "new content"}
Response 200: mem0 update result
Response 413: text exceeds MAX_MEMORY_CHARS
Response 500: Qdrant unreachable
```

### `PATCH /v1/memories/{id}/tier`

Promote or demote a memory's tier. Server-enforced actor requirements. Writes one ledger line to the current-month tier-ledger segment after the Qdrant payload update succeeds.

```json
Request: {"tier": "canonical", "actor": "user-direct", "reason": "the operator said to lock this in"}
```

- `actor` is required (a free-text label; the enforced rules are tier-specific below). `tier` must be in `PROMOTE_ALLOWED_TIERS` (`evidence`, `stable`, `canonical`, `insight`, `temporal`).
- `tier=canonical` requires `actor=user-direct` **or** `actor=dream-autopromote` (the nightly autopromotion), a non-empty `reason`, **and** a valid HMAC user-direct token — headers `X-User-Direct-Token` / `-Ts` / `-Nonce`, signing format-2 `<ts>|<nonce>|promote|<mid>|<reason>` (produced by `mem0-canonize.sh`; the nonce-less format-1 was removed in v0.20). A canonical promote from any other actor, or without the nonce, → `403`. The canonical text is run through the imperative-canary → `422` if it reads as a standing order rather than a declarative fact.
- `tier=insight` requires `actor` in the exact allowlist `{c1-consolidator, dream-consolidator, c1-dream-consolidator}`. Any other actor → `403`.
- `tier in {evidence, stable, temporal}` accepts `claude-autonomous` — autonomous Claude can only ever set these.

```
Response 200: {"ok": true, "memory_id": "...", "tier": "canonical", "actor": "user-direct", "ts": "2026-..."}
Response 400: missing actor, missing reason for canonical, invalid tier
Response 403: actor/tier enforcement rejected, or canonical promote without nonce
Response 422: imperative text rejected from canonical
```

### `PATCH /v1/memories/{id}/metadata`

Partial metadata update (shallow merge, not replace). Cannot change `tier` (use `PATCH /tier`). Used by re-extraction (marks originals `retrievable=false`), decay (sets `temporal.expires_at`), and the dream consolidator (stamps `touched_by_dream`). Lifecycle-critical keys that gate retrieval (`retrievable`, `superseded_by`, `contradicts_canonical`, …) are in `FORBIDDEN_KEYS`: only a trusted actor (per-actor `TRUSTED_PATCH_ACTORS` allowlist) or an HMAC user-direct token may write them. Every successful merge is appended to the tier ledger.

### `DELETE /v1/memories/{id}`

Delete a memory by ID. Canonical/insight deletes require an HMAC user-direct token (`mem0-canonize.sh --action delete`). The weekly decay-scan writes a ledger line with `event=decay-delete` when it removes an expired `temporal` record (`scripts/wsl/decay-scan.py`).

```
Response 200: mem0 delete result
```

### MCP tool wrappers

The shim (`scripts/wsl/mem0-mcp-shim.py`) exposes these tools to Claude Code:

- `memory_add(text, user_id, infer, metadata)` — POST /v1/memories
- `memory_search(query, user_id, limit, threshold)` — POST /v1/memories/search
- `memory_list(user_id, limit)` — GET /v1/memories (limit hard-clamped at 500 client-side too)
- `memory_update(memory_id, text)` — PUT /v1/memories/{id}
- `memory_promote(memory_id, tier, actor, reason)` — PATCH /v1/memories/{id}/tier
- `memory_demote(memory_id, tier, actor, reason)` — PATCH /v1/memories/{id}/tier (same endpoint, different direction)
- `memory_delete(memory_id)` — DELETE /v1/memories/{id}
- `memory_health()` — GET /health

## Dependencies

- **Qdrant** on `:6333` (collection `mem0_egemma_768`, loopback).
- **llama-swap** on `:11436` — the EmbeddingGemma-300m embedder and the bge-reranker-v2-m3 cross-encoder.
- **mem0 2.0.4** (`mem0ai`) library.
- **The Codex HTTP shim** on `:18792` — used by the optional NLI write-gate (`codex_shim_client.py`) to judge contradictions against canonical.

## Downstream effects

Every route change ripples to the MCP shim (`mem0-mcp-shim.py`), the Windows hook clients that POST to `/v1/context/bundle` and `/v1/memories/search`, the dream consolidator (which posts insights and calls the tier PATCH via `mem0-canonize.sh`), and the canonize CLI. The `hook_contract_version` field lets the server WARN on hook/server wire drift without rejecting.

## Invariants and assumptions

- The server binds loopback-only (`127.0.0.1`); it is never exposed on `0.0.0.0`.
- The tier gates are server-side; a caller cannot self-elevate to `canonical`/`insight` regardless of the metadata it sends.
- `infer=false` writes are hash-idempotent within a scope, so hooks re-firing on every Stop cannot re-insert duplicates.
- `limit` is clamped to 500 on both list and search.
- Callers cannot forge retrieval-gating metadata keys via `add` or the generic metadata PATCH.

## Error handling

| Code | Cause |
|---|---|
| `400` | empty memory; missing actor; missing reason for canonical; `tier` in metadata PATCH |
| `401` | missing/invalid `X-API-Key` |
| `403` | tier gate (canonical via add, insight source, canonical promote without user-direct/nonce) |
| `413` | payload exceeds `MAX_MEMORY_CHARS` |
| `422` | imperative text rejected from the canonical tier (imperative-canary) |
| `500` | Qdrant / llama-swap / mem0 backend error |
| `503` | canonical-canary could not verify the stored text (store unreachable) |

## Security and privacy notes

- **Auth:** single `X-API-Key` (mode-600 file), constant-time compared; loopback bind is the network boundary.
- **Canonical writes:** gated by an HMAC user-direct token (format-2, replay-protected via a burned nonce in `~/.mem0/canonical-replay.jsonl`); the signing key is DPAPI-held (see [`dpapi-canonical-key.md`](./dpapi-canonical-key.md)).
- **Metadata forgery:** retrieval-gating keys are stripped on `add` and forbidden on the generic metadata PATCH so an API-key holder cannot silently bury records.
- **Secret redaction:** stored prompt text is scrubbed server-side (`redact.py`).

## Observability and debugging

- `GET /health` for liveness; `GET /health/deep` for the real write-path diagnostics (Qdrant point count, embedder dim, canonical-key health, admission-rejection counters, contradiction-review queue depth).
- The tier ledger is the audit trail for every mutation.
- Retrieval decisions are logged for post-hoc inspection; `query_class="history"` surfaces hidden records.

## Testing notes

Server behavior is covered by the `mem0-server/tests` suite (tier enforcement, brand isolation, admission policy, hash idempotency, tier parity with `claude-config/model-tiers.json`). `Test-MemoryStack.ps1` (R9) is the live end-to-end probe. Validate an endpoint change against both.

## Common pitfalls

- **Forgetting `X-API-Key`** → `401`. The shim handles this; direct REST callers must set the header.
- **Passing `tier=canonical` to POST** → `403`. This is intentional — add as `evidence`, then promote via the HMAC-signed `PATCH /tier`.
- **Oversize payload** → `413`. The `MAX_MEMORY_CHARS` cap (default 4000, env-overridable via `MEM0_MAX_MEMORY_CHARS`) is per-memory, not per-request batch. Split into atomic facts.
- **`infer=true` for hook-extracted facts** → incorrect behavior: mem0's LLM extraction re-processes the already-extracted fact, possibly splitting or altering it. Always use `infer=false` from automated paths.
- **Calling `/health` to verify write path** → misleading green. Use `/health/deep` or run a test round-trip.
- **Expecting a substring match for the insight source** → the allowlist is exact (`c1-consolidator`, `dream-consolidator`, `c1-dream-consolidator`); `actor="not-c1"` no longer slips through.

## Source map

- [`../../mem0-server/app.py`](../../mem0-server/app.py) — the FastAPI app: all routes, auth, tier gates, hash idempotency, the ledger writer.
- [`../../mem0-server/config.py`](../../mem0-server/config.py) — mem0 config: embedder, Qdrant collection, ports.
- [`../../mem0-server/admission_gate.py`](../../mem0-server/admission_gate.py) — the read-side query-class admission policy.
- [`../../mem0-server/reranker.py`](../../mem0-server/reranker.py) — the bge-reranker cross-encoder client + skip thresholds.
- [`../../scripts/wsl/mem0-mcp-shim.py`](../../scripts/wsl/mem0-mcp-shim.py) — the stdio-MCP → HTTP shim (the MCP tool wrappers).
- [`../../scripts/wsl/mem0-canonize.sh`](../../scripts/wsl/mem0-canonize.sh) — the HMAC user-direct CLI for canonical promote / put / delete / metadata.

## Related docs

- [`memory-model.md`](./memory-model.md) — what the tiers and query classes *mean*.
- [`tier-policy.md`](./tier-policy.md) — the full tier rule table.
- [`admission-gate.md`](./admission-gate.md) — the read-side admission policy.
- [`dpapi-canonical-key.md`](./dpapi-canonical-key.md) — the HMAC canonical key lifecycle.
- [`reranker.md`](./reranker.md) — the reranker subsystem.
- [`../flows/memory-capture.md`](../flows/memory-capture.md) · [`../flows/memory-retrieval.md`](../flows/memory-retrieval.md) — the capture/retrieval flows.
- [`../glossary.md`](../glossary.md) · [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)
