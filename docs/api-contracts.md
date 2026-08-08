# API Contracts

The mem0 stack exposes two surfaces: a **REST API** (HTTP, used internally by L1a/C1/L10 and the MCP shim) and an **MCP tool surface** (stdio JSON-RPC, used by Claude Code / Codex CLI). This doc is the contract — upgrades must preserve it, breakages here are user-visible regressions.

---

## REST API — `mem0-server/app.py`

Base URL: `http://127.0.0.1:18791` (loopback only, accessible from Windows via WSL mirrored networking).

Auth: every endpoint except `/health` requires the `X-API-Key` header. Key is at `~/.mem0/api-key` (WSL, mode 0600). Comparison uses `hmac.compare_digest` (constant-time).

All endpoints return JSON. On error, HTTP 401 (auth), 400 (validation), 503 (upstream rate-limited — **retryable**, carries `Retry-After`), or 500 (server). Error body is `{"detail": "..."}`.

**503 is the one status a client must not treat as a real answer.** It means the embedder upstream (llama-swap) answered 429 and outlived the embedder's bounded retry — the server can serve this request later, it just cannot now. A client that treats it as a failed operation drops the work: the MCP shim's failover fires on connect-level errors only, so before 503 existed this arrived as 500, was read as a real answer, and the write was discarded instead of queued. The shim now routes 503 the way it routes a connect failure (reads → local replica, writes → outbox). Every other status remains a real answer and must never be masked with a stale read.

### `GET /health`

Liveness + version probe. No auth.

**Response 200:**
```json
{"ok": true, "version": "2.0.4-v012", "store": "qdrant", "embedder": "embeddinggemma-300m"}
```

### `POST /v1/memories` — add memory

**Request body (`AddIn`):**
```json
{
  "messages": "<string OR list of {role,content} dicts OR single dict>",
  "user_id": "youruser",
  "agent_id": null,                  // optional
  "run_id": null,                    // optional
  "metadata": {                      // optional — anything; convention below
    "source": "l1a-extractor",
    "tier": "evidence",              // evidence | canonical | insight | temporal
    "event": "Stop",                 // any string
    "...": "..."
  },
  "infer": false                      // false = store messages verbatim; true = mem0 LLM extracts facts
}
```

**Response 200:** `mem0.Memory.add()` return value. Typically `{"results":[{"id":"...","memory":"...","event":"ADD",...}]}` or `{"results":[]}` if `infer=true` and nothing extractable.

**Convention for `metadata.tier`:**
- `evidence` — default. Hook-extracted or programmatic writes. Subject to L10 audit + auto-promote.
- `canonical` — explicit human-blessed truth. Lock-in via `memory_promote` MCP tool.
- `insight` — synthesized higher-order facts from C1 consolidator.
- `temporal` — time-scoped facts; consumer must check validity window.

### `GET /v1/memories` — list

**Query params:**
- `user_id` (required)
- `limit` (default 100)

**Response 200:** `{"results":[{"id":..., "memory":..., "metadata":..., "user_id":..., "created_at":..., "updated_at":..., "hash":...}, ...]}`

> **Known quirk (mem0 v2.0.4):** `get_all` returns a hardcoded ~20 items regardless of `limit`. Use the Qdrant scroll API directly (`POST :6333/collections/memories/points/scroll`) if you need more.

### `POST /v1/memories/search` — semantic search

**Request body (`SearchIn`):**
```json
{
  "query": "what we know about X",
  "filters": {"user_id": "youruser"},
  "limit": 20,
  "threshold": 0.1,
  "rerank": false,
  "query_class": "durable",
  "explain": false
}
```

(The field is `limit` — an earlier revision of this doc showed a `top_k` field that never existed on this endpoint.)

**Response 200:** `{"results":[...], "rejected_brand_scoped": 0, "rejected_superseded": 0, "rejected_contradicted": 0, ...}` — `results` ordered by descending score; the `rejected_*` counters report per-call admission withholding by family (W5 ADOPT-3). With `rerank: true` and a non-empty result set the response also carries `rerank_status` (`ran | skipped_small_n | skipped_confident | failed_fallback_dense`), and the keyword-recall union leg activates (W5 AMS-56): exact-token hits outside the dense window are unioned into the pool marked `lexical_only: true`, forced through the cross-encoder, and DROPPED fail-closed if the rerank did not actually run. With `explain: true` the response adds `_explain.stages` — a per-stage `{stage, in, out, detail}` trace (counts and scalars only).

### `POST /v1/memories/diagnose` — why does memory X not surface for query Q (W5 ADOPT-2)

**Request body (`DiagnoseIn`):** `{"query", "target_id", "user_id?", "brand?", "allow_cross_brand?", "query_class?": "durable", "threshold?": 0.1, "limit?": 20, "rerank?": false}` — set threshold/limit/rerank to the FAILING search's values (defaults mirror `SearchIn`).

**Response 200:** `{"target_id", "verdict", "dense": {rank_at_500, score, overfetch_limit, within_overfetch}, "flags": {retired, canonical_intent}, "admission": {admit, reason, query_class}, "rerank": {...}, "freshness", "caveats"}` — `verdict` names the FIRST eating stage (`dense_retrieval:below_500_horizon`, `threshold:…`, `overfetch_pool:…`, `retired_filter`, `canonical_intent_filter`, `admission:<reason>`, `trim…`, or `returned`). Read-only: the admission verdict uses the pure evaluate path and never mutates the rejection counters or audit log. 404 when the target does not exist.

### `PUT /v1/memories/{mid}` — update text only

**Request body (`UpdateIn`):**
```json
{"text": "new memory content"}
```

**Response 200:** mem0 `update()` return value.

> **Limitation:** mem0 v2.0.4's `Memory.update(memory_id, data=text)` only updates `text`, not metadata. Use `PATCH /v1/memories/{mid}/tier` for metadata.tier; for other metadata changes, currently no endpoint — add one if needed.

### `PATCH /v1/memories/{mid}/tier` — update tier metadata (custom endpoint, our addition)

**Request body (`TierIn`):**
```json
{
  "tier": "canonical",               // evidence | canonical | insight | temporal
  "reason": "user said lock it in",  // optional, written to ledger
  "actor": "claude"                   // optional, default "claude"
}
```

**Response 200:**
```json
{"ok": true, "memory_id": "abc...", "tier": "canonical", "actor": "claude", "ts": "2026-06-08T..."}
```

**Side effect:** appends an entry to the monthly ledger segment `~/.mem0/tier-ledger-YYYY-MM.jsonl`:
```json
{"ts":"...", "memory_id":"...", "tier":"canonical", "actor":"claude", "reason":"..."}
```

### `DELETE /v1/memories/{mid}`

**Response 200:** mem0 `delete()` return value.

---

## MCP tool surface — `scripts/wsl/mem0-mcp-shim.py`

Stdio JSON-RPC, spawned by Claude Code / Codex CLI as a child process. The shim wraps the REST API; tool names map to functions in the shim file.

### `memory_health()` → dict
Returns `GET /health/deep` — the probe that actually exercises the store and the embedder, not the static `/health`. A health tool that cannot observe a broken write path does not merely fail to help, it certifies the outage: shallow `/health` reported `ok=True` throughout a live embedder rate-limit burst that had `POST /v1/memories` returning 500. Liveness callers on a hot path (the prompt-time storage-cap check, the installer's port probe) stay on shallow `/health` deliberately.

### `memory_add(text, user_id="youruser", infer=False, metadata=None)` → dict
Wraps `POST /v1/memories`. Use `metadata={"source":"...", "tier":"evidence"}` minimum.

### `memory_search(query, user_id="youruser", limit=5, threshold=0.1, rerank=None, query_class="durable", brand=None, allow_cross_brand=False)` → dict
Wraps `POST /v1/memories/search` with filter `{"user_id": user_id}` (+ brand scoping). Auto-reranks when `limit>=5`. W5 surfacing: adds `rerank_note` when the reranker fell back dense-only, and `withheld_note` when superseded/contradicted records were withheld by admission.

### `memory_diagnose(query, target_id, user_id="youruser", brand=None, query_class="durable", threshold=0.1, limit=20, rerank=False, allow_cross_brand=False)` → dict
Wraps `POST /v1/memories/diagnose` (W5 ADOPT-2). Names the first retrieval stage that eats the target; read-only. Set the parameters to the FAILING search's values.

### `memory_list(user_id="youruser", limit=100)` → dict
Wraps `GET /v1/memories`.

### `memory_update(memory_id, text)` → dict
Wraps `PUT /v1/memories/{id}`. Text only.

### `memory_promote(memory_id, tier="canonical", reason=None)` → dict
Wraps `PATCH /v1/memories/{id}/tier` with `actor="claude"`. Use when user says "lock that in" / "save as canon".

### `memory_demote(memory_id, tier="evidence", reason=None)` → dict
Same as promote, opposite direction. Use to walk back wrong canonicalizations.

### `memory_delete(memory_id)` → dict
Wraps `DELETE /v1/memories/{id}`.

---

## Stability guarantees

- **REST API** is stable. New endpoints can be added (additive); existing ones won't change shape without a major version bump. Removing `messages`/`user_id`/`infer` from `POST /v1/memories` would be a breaking change.
- **MCP tool surface** is stable. Tool names + param signatures won't change without a bump. Adding new tools is additive.
- **Internals (mem0ai package itself)** are NOT covered by this contract — they can change between mem0 releases. The `mem0-server/app.py` wrapper is the abstraction boundary; if mem0 v3 ships with breaking `Memory.add` signature changes, only `app.py` needs to adapt — the REST + MCP surfaces stay the same.

## Things this contract does NOT cover

- Qdrant's HTTP API on `:6333`. We use a stable subset (`/collections/mem0_egemma_768/points/{search,scroll}`, `/collections/mem0_egemma_768`). Upstream Qdrant guarantees its own API stability per their semver.
- llama-swap's OpenAI-compatible API on `:11436` (serves EmbeddingGemma-300m embeddings + the bge reranker). We use `/v1/embeddings`. **(v0.22: replaced Ollama/`:11434` + nomic-embed-text.)**

> **Note (v0.13):** agentmemory's MCP surface (50+ `mcp__agentmemory__*` tools) has been **REMOVED** from the stack. Those tools no longer appear in Claude Code sessions. Episodic memory is a deliberate v0.14 gap.

## When upgrading mem0 or fastmcp

After upgrade, run `audit/upgrade-smoke.ps1` — it exercises **every contracted endpoint and tool above**. If any phase fails, the contract is broken; rollback or fix `mem0-server/app.py` / `mem0-mcp-shim.py` to restore the surface.
