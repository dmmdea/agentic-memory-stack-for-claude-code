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

Checks Qdrant collection status, EmbeddingGemma embedder dimension (via llama-swap), and mem0 collection point count. Also surfaces the canonical-key health, admission-rejection counters, pending contradiction-review depth, nightly-job + session receipt ages, drift-guard state, the passive reranker counters, the admission self-probe, and the capability manifest. Slow (~1-3s). Use for diagnostics, not polling. **No check here may perform a slow ACTIVE model call** — `scripts/wsl/deploy.sh` gates on this endpoint seconds after a restart (with `--max-time 60`), so a cold CPU model behind a probe here blocks deploys; that is why the reranker is surfaced passively.

```
Response 200: {"ok": true, "checks": {"qdrant": {"ok": true, "points": N, "status": "green"}, "embedder": {"ok": true, "dim": 768}}}
Response 200 (degraded): {"ok": false, "checks": {"qdrant": {"ok": false, "error": "..."}}}
```

Three checks added by the 2026-08 audit:

- **`sparse_leg` — GATING** (flips `ok` to `false`): BM25 lexical-leg liveness. mem0 fail-softs to dense-only search silently when the fastembed sparse encoder is missing (the installer now declares fastembed explicitly, with a loadability post-condition); this check makes that fallback loud with a **deterministic canary** — it derives a token from the *oldest* BM25-bearing point (longest token, lexicographic tiebreak, from its lemmatized text) and requires that point back in the top-5 keyword results, so a tokenizer/hash change that strands the legacy corpus also fails. Shape: `{"ok": bool, "fastembed": bool, "bm25_slot": bool, "points": N, "with_bm25": N, "coverage": 0.0-1.0, "canary": {"ran": bool, "hit": bool, "token": "..."}, "error"?: "..."}`. `coverage` (`with_bm25 / points`) is reported but does not gate — a half-backfilled corpus with a live encoder is degraded, not dead (`Test-MemoryStack` WARNs below 0.95).
- **`mojibake` — informational** (never flips `ok`): CP437 mojibake corpus tripwire — scans point text payloads for the glyph-pair signature of UTF-8 bytes decoded through the OEM console codepage. Shape: `{"ok": true, "scanned": N, "hits": N, "sample_ids": [...], "elapsed_ms": N}`. The scroll is page-capped so the walk stays bounded as the corpus grows; `scanned` stays honest about a capped scan. `Test-MemoryStack` WARNs on `hits > 0`.
- **`put_carryover_today` — informational** (never flips `ok`): daily counters proving the PUT payload carry-over (below) is active. Shape: `{"date": "YYYY-MM-DD", "puts": N, "keys_restored": N, "keys_lost": N}` — `keys_restored`/`keys_lost` count post-verify repair activity and should stay 0; nonzero values indicate mem0-contract drift.

Five more added by W3 and W4 (the alarm-mouth + revive-or-bury tracks — all informational, never flip `ok`; the fixed key names below are a cross-track contract consumed by the verifier, the dream heartbeat, and the session-banner digest):

- **`job_liveness`**: age of every receipt the stack leaves behind. Shape: `{"role": "brain"|"replica"|null, "last_dream_age_h": h|null, "prune_age_h": h|null, "gather_age_h": h|null, "backup_manifest_age_h": h|null, "dedup_report_age_h": h|null, "morning_summary_age_h": h|null, "morning_summary_sections_48h": N|null, "l1a_attempt_age_h": h|null, "l1a_success_age_h": h|null, "sessionstart_banner_age_h": h|null, "mcp_shim_receipt_age_h": h|null, "mcp_shim_host_match": bool|null, "mcp_shim_stack_version": "..."|null, "brand_scope_age_h": h|null, "brand_scope_misscoped": N|null, "outbox_depth": N|null, "outbox_replayed_age_h": h|null, "outbox_drain_log_age_h": h|null, "error"?: "..."}`. Every field is independently fail-soft — a missing file/env yields `null` for that field plus a note in `error` while the rest still populate. `last_dream_age_h`, the two `l1a_*` fields and `sessionstart_banner_age_h` derive from their stamp files' epoch **content**, never the mtime (a copy/restore changes mtime); `role` comes from env `MEM0_ROLE`, else the WSL `stack.env` receipt, else the `~/.mem0/role` installer receipt. **Key names are a cross-track contract — W4 ADDED keys and additions are safe, but nothing here may be renamed** (the verifier, the dream heartbeat and the session-banner digest all read by name). Two W4 notes: the epoch parser strips a leading **U+FEFF**, because the Windows PowerShell 5.1 hooks write these stamps with `Set-Content -Encoding UTF8`, which emits a BOM that `strip()` does not remove; and `mcp_shim_host_match` exists because `~/.mem0` travels in stack backups, so a receipt restored from another machine must not read as local liveness.
- **`reranker` — informational** (never flips `ok`): **passive** cross-encoder counters, bumped by real search reranks. Shape: `{"last_rerank_ok_ts": epoch|null, "consecutive_rerank_failures": N, "ok_total": N, "fail_total": N, "last_error": "..."|null}`. There is deliberately **no active rerank probe on this endpoint** — the reranker is CPU-only (the verifier budgets it 90s) and `deploy.sh` gates on `/health/deep` seconds after a restart, so an active probe would hang deploys on a cold model. The active 3-doc probe lives in `Test-MemoryStack` check L5.
- **`admission_probe` — informational** (never flips `ok`): the admission gate's in-process self-probe. Shape: `{"ok": bool, "tier_rejected": bool|null, "brand_rejected": bool|null, "neutral_admitted": bool|null, "query_class": "durable", "error"?: "..."}`. Three synthetic records go through `AdmissionPolicy.evaluate()` **directly** — never `apply_admission`, which would bump the daily rejection counters this same response reports and append to `~/.mem0/admission-rejected.jsonl` on every health read. Zero I/O, zero side effects; it also runs on an empty store.
- **`retrieval_drift`**: passthrough of the retrieval-drift guard's state sidecar (`~/.mem0/retrieval-drift-state.json`; the guard itself lives in a private evaluation repo). Shape: `{"state_present": bool, "last_compare_ts": ts|null, "age_hours": h|null, "before_retrievable": N|null, "n_total": N|null, "hwm": N|null, "hwm_seeded": bool|null, "consecutive_below_hwm": N|null, "consecutive_snapshot_failures": N|null, "alarm": bool|null, "missing": [...]|null, "compat_fallback": bool|null, "error"?: "..."}`. An absent file is `state_present: false` with everything null and **no** error (a not-yet-deployed guard is a fact, not a fault); a present-but-malformed file is `state_present: true` plus an `error` note.
- **`capabilities`**: the capability-manifest verdict — a pure fold of the checks above against the `CAPABILITIES` literal. Shape: `{"role": "brain"|"replica"|null, "states": {"<id>": "alive"|"degraded"|"dead"|"unknown"|"retired"}, "dead_required": ["<id>", ...], "unknown": ["<id>", ...], "evaluated_at": "<iso>"}`. `dead_required` lists dead rows required for this box's role (unknown role never convicts role-scoped rows; zero-signal activity counters are `unknown`, never `alive`). Row table and verdict rules: [../capability-manifest.md](../capability-manifest.md). The verifier FAILs on a non-empty `dead_required`.

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

Update a memory's text content. The **full existing payload is carried over** atomically into the rewrite (tier, source, brand, project, provenance stamps — every custom key): mem0 rebuilds the Qdrant payload from scratch on update, so before this carry-over a PUT silently destroyed all custom metadata (only tier was restored). The pre-update payload read is **fail-closed**: if it errors, the PUT is refused with `503` rather than performing a blind update that would wipe metadata. Because a PUT changes the text, the record **re-enters the NLI write-gate**: the carry-over deliberately drops the NLI check-markers (a judgment of the old text must not vouch for the new one) and re-judgment of the new text is queued asynchronously, exactly like an add. Canonical/insight records require a valid HMAC user-direct token (`mem0-canonize.sh --action put`); canonical text is additionally run through the imperative-canary and rejected `422` if it reads as a standing order.

```json
Request: {"text": "new content"}
Response 200: mem0 update result
Response 400: pre-update read rejected by the store (malformed memory id — permanent fault, never queued)
Response 413: text exceeds MAX_MEMORY_CHARS
Response 500: carry-over restore exhausted for a canonical/insight record (inconsistent state — manual verification)
Response 503: pre-update payload read failed (refused fail-closed rather than wiping custom metadata; MCP shim queues 503s to the outbox and replays)
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
- `memory_health()` — GET /health/deep (switched 2026-07-26: shallow /health green-lit broken write paths)

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
| `503` | canonical-canary could not verify the stored text (store unreachable); PUT pre-update payload read failed (fail-closed carry-over) |

## Security and privacy notes

- **Auth:** single `X-API-Key` (mode-600 file), constant-time compared; loopback bind is the network boundary.
- **Canonical writes:** gated by an HMAC user-direct token (format-2, replay-protected via a burned nonce in `~/.mem0/canonical-replay.jsonl`); the signing key rests as a DPAPI blob where the per-box cutover has been run, else mode-600 plaintext — /health/deep reports which (see [`dpapi-canonical-key.md`](./dpapi-canonical-key.md)).
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
