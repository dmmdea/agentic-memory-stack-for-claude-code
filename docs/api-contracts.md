# API Contracts

The mem0 stack exposes two surfaces: a **REST API** (HTTP, used internally by L1a/C1/L10 and the MCP shim) and an **MCP tool surface** (stdio JSON-RPC, used by Claude Code / Codex CLI). This doc is the contract — upgrades must preserve it, breakages here are user-visible regressions.

**Coverage:** the memories core (`/health*` + `/v1/memories*` and their MCP tools) is specified in full below. The goals / open-questions / episodes / context-bundle surfaces are enumerated as one-line contracts (existence, signature, purpose); their authoritative body schemas are the Pydantic models in `mem0-server/app.py` and the tool docstrings in `scripts/wsl/mem0-mcp-shim.py`, exercised by the pytest suite.

---

## REST API — `mem0-server/app.py`

Base URL: `http://127.0.0.1:18791` (loopback only, accessible from Windows via WSL mirrored networking).

Auth: every endpoint except `GET /health` and `GET /health/deep` requires the `X-API-Key` header. Key is at `~/.mem0/api-key` (WSL, mode 0600). Comparison uses `hmac.compare_digest` (constant-time).

All endpoints return JSON. On error, HTTP 401 (auth), 400 (validation), 503 (upstream rate-limited — **retryable**, carries `Retry-After`), or 500 (server). Error body is `{"detail": "..."}`.

**503 is the one status a client must not treat as a real answer.** It means the embedder upstream (llama-swap) answered 429 and outlived the embedder's bounded retry — the server can serve this request later, it just cannot now. A client that treats it as a failed operation drops the work: the MCP shim's failover fires on connect-level errors only, so before 503 existed this arrived as 500, was read as a real answer, and the write was discarded instead of queued. The shim now routes 503 the way it routes a connect failure (reads → local replica, writes → outbox). Every other status remains a real answer and must never be masked with a stale read.

### `GET /health`

Liveness + version probe. No auth.

**Response 200:**
```json
{"ok": true, "version": "2.0.4-v012", "stack": "<stack release>", "store": "qdrant", "embedder": "embeddinggemma-300m"}
```

`version` is the historical mem0-lib+phase tag (kept stable — dashboards may pattern-match it). `stack` is the actual stack release (the repo `VERSION` file) — the field the operations runbook tells you to read.

### `GET /health/deep`

Deep diagnostic probe. No auth. Exercises Qdrant + the embedder + the live collection binding (reported as `collection`), plus job liveness, canonical-key source, and drift canaries. Slow — use `/health` for liveness, `/health/deep` for diagnostics.

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
    "tier": "evidence",              // on add: evidence | temporal only (see below)
    "event": "Stop",                 // any string
    "...": "..."
  },
  "infer": false                      // false = store messages verbatim; true = mem0 LLM extracts facts
}
```

**Response 200:** `mem0.Memory.add()` return value. Typically `{"results":[{"id":"...","memory":"...","event":"ADD",...}]}` or `{"results":[]}` if `infer=true` and nothing extractable.

**Convention for `metadata.tier`** (server-enforced at add: only `evidence` and `temporal` are accepted; `insight` additionally with a consolidator `source`; anything else → 400):
- `evidence` — default. Hook-extracted or programmatic writes. Subject to L10 audit + auto-promote.
- `stable` — promotion-only (audited evidence); cannot be set on add.
- `canonical` — explicit human-blessed truth. Promotion-only, and **not reachable via MCP**: it requires the user-direct HMAC token that only the operator CLI `scripts/wsl/mem0-canonize.sh` produces (see `PATCH /tier` below).
- `insight` — synthesized higher-order facts; actor/source-allowlisted to the C1/dream consolidator.
- `temporal` — time-scoped facts; consumers read the validity window from the memory text.

### `GET /v1/memories` — list

**Query params:**
- `user_id` (required)
- `limit` (default 100, silently clamped server-side at 500)

**Response 200:** `{"results":[{"id":..., "memory":..., "metadata":..., "user_id":..., "created_at":..., "updated_at":..., "hash":...}, ...]}`

> **Known quirk (mem0 v2.0.4):** `Memory.get_all`'s size param is named `top_k` (default 20) — `app.py` passes `top_k` explicitly, so `limit` is honored up to the 500 cap. Beyond that, use the Qdrant scroll API directly (`POST :6333/collections/<collection>/points/scroll`) against the collection mem0 is bound to — the `collection_name` in `mem0-server/config.py` (`mem0_egemma_768` as shipped; `/health/deep` reports the live binding as `collection`).

### `GET /v1/memories/{mid}` — exact read

Returns one record by id: text, metadata (incl. tier), timestamps. Use before update/delete/promote — search and list are not substitutes for an exact read. 404 when the id does not exist.

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

> **Scope:** `PUT` updates `text` only. Tier changes go through `PATCH /v1/memories/{mid}/tier`; other metadata through `PATCH /v1/memories/{mid}/metadata` (shallow merge). On a `canonical`/`insight` target, PUT additionally requires the user-direct HMAC headers (`assert_writable` gate) and `actor`/`reason` query params — `mem0-canonize.sh --action put` produces a valid call. The full existing payload is carried over into the update (AMS-01 fix), and canonical targets reject imperative-phrased text (422 — declarative facts only).

### `PATCH /v1/memories/{mid}/tier` — update tier metadata (custom endpoint, our addition)

**Request body (`TierIn`):**
```json
{
  "tier": "stable",                   // evidence | stable | canonical | insight | temporal
  "actor": "claude-autonomous",       // REQUIRED — 400 if missing or empty
  "reason": "audited: survived L10"   // optional (REQUIRED for canonical), written to ledger
}
```

**Server-enforced tier rules:**
- `canonical` — requires `actor="user-direct"` (or the server-side `dream-autopromote` allowlist), a **non-empty `reason`**, AND a valid HMAC format-2 user-direct token: headers `X-User-Direct-Token` / `X-User-Direct-Ts` / `X-User-Direct-Nonce` signing `<ts>|<nonce>|promote|<mid>|<reason>` (403 without them; the nonce is burned server-side for replay protection). `scripts/wsl/mem0-canonize.sh` is the only shipped producer of that token. Imperative-phrased text is rejected 422 (canonical is declarative facts only).
- `insight` — requires an actor from the consolidator allowlist (403 otherwise).
- `evidence` / `stable` / `temporal` — any non-empty actor.

**Response 200:**
```json
{"ok": true, "memory_id": "abc...", "tier": "stable", "actor": "claude-autonomous", "ts": "2026-06-08T..."}
```

**Side effect:** appends an entry to the monthly ledger segment `~/.mem0/tier-ledger-YYYY-MM.jsonl`:
```json
{"ts":"...", "event":"tier-change", "memory_id":"...", "tier":"...", "actor":"...", "reason":"...", "transport":"cli-user-direct | autonomous | rest-api"}
```

### `PATCH /v1/memories/{mid}/metadata` — shallow-merge metadata (custom endpoint, our addition)

**Request body (`MetadataIn`):** `{"metadata": {...non-empty...}, "actor": "...", "reason": "..."}` — shallow-merges into the existing payload. Cannot change `tier` (400 — use `PATCH /tier`); lifecycle-gating keys (`retrievable`, `expires_at`, `created_at`, `tier_actor`, `superseded_by`, `contradicts_canonical`, …) are forbidden to generic callers (per-actor server-side allowlists only). `canonical`/`insight` targets require the user-direct HMAC headers (`mem0-canonize.sh --action patch_metadata`). Every successful merge is ledgered.

### `DELETE /v1/memories/{mid}`

**Query params:** `actor`, `reason` (optional, ledgered), `cascade` (default false — `true` also deletes records superseded by the target). `canonical`/`insight` targets require the user-direct HMAC headers (`mem0-canonize.sh --action delete`).

**Response 200:** mem0 `delete()` return value. Always appends a `delete` ledger entry (with the prior payload's source) so destructive ops are audit-covered.

### Goals / open-questions / episodes / context-bundle routes (one-line contracts)

| Route | Purpose |
|---|---|
| `POST /v1/goals` | create a goal (title, priority 1–5, brand, optional parent_goal_id) |
| `GET /v1/goals` | list goals; `status`/`brand`/`limit` filters |
| `GET /v1/goals/tree` | recursive goal hierarchy (optional `root_id`) |
| `GET /v1/goals/{goal_id}` | full goal detail incl. linked-episode count |
| `PATCH /v1/goals/{goal_id}/status` | set goal status |
| `PATCH /v1/goals/{goal_id}/abandon` | mark abandoned (actor + reason) |
| `PATCH /v1/goals/{goal_id}/complete` | mark completed (actor + reason) |
| `PATCH /v1/goals/{goal_id}/priority` | set priority |
| `POST /v1/goals/{goal_id}/link_episode` | link an episode (link_type) |
| `POST /v1/goals/{source_goal_id}/merge` | merge a duplicate goal into a target |
| `POST /v1/open_questions` | record a frontier question |
| `GET /v1/open_questions` | list open questions (`status`/`brand`/`limit`) |
| `POST /v1/open_questions/search` | FTS5 search across questions |
| `GET /v1/open_questions/{oq_id}` | question detail incl. related goal |
| `PATCH /v1/open_questions/{oq_id}/resolve` | resolve with resolution text + session id |
| `PATCH /v1/open_questions/{oq_id}/status` | set question status |
| `POST /v1/episodes` | finalize/write an episode (session summary) |
| `POST /v1/episodes/checkpoint` | fast in-progress episode upsert (per-prompt hook) |
| `POST /v1/episodes/search` | FTS5 episode search (since/until/brand) |
| `GET /v1/episodes` | recent episodes (`recent`, `brand`) |
| `GET /v1/episodes/count` | episode count |
| `GET /v1/episodes/{episode_id}` | episode detail + linked mem0 memory ids |
| `POST /v1/context/bundle` | one-round-trip bundle: episode checkpoint + gated memories + goals + open questions (the per-prompt hook / `memory_recall` wire) |

---

## MCP tool surface — `scripts/wsl/mem0-mcp-shim.py`

Stdio JSON-RPC, spawned by Claude Code / Codex CLI as a child process. The shim wraps the REST API; tool names map to functions in the shim file.

### `memory_health()` → dict
Returns `GET /health/deep` — the probe that actually exercises the store and the embedder, not the static `/health`. A health tool that cannot observe a broken write path does not merely fail to help, it certifies the outage: shallow `/health` reported `ok=True` throughout a live embedder rate-limit burst that had `POST /v1/memories` returning 500. Liveness callers on a hot path (the prompt-time storage-cap check, the installer's port probe) stay on shallow `/health` deliberately.

### `memory_add(text, user_id="youruser", infer=False, metadata=None)` → dict
Wraps `POST /v1/memories`. Use `metadata={"source":"...", "tier":"evidence"}` minimum. The result may carry a `note` string (absent on a plain success): a tier auto-downgrade explanation (`canonical`/`insight` requested over MCP), and/or — since 2026-08-24 — an **oversize advisory** when an `infer=False` text exceeds 1200 chars: the record is stored **intact** (the server accepts up to 4000), the note asks the writer to split into atomic facts only if the record bundles several claims. Never a rejection, never a split. Multiple notes are joined with ` | `.

### `memory_search(query, user_id="youruser", limit=5, threshold=0.1, rerank=None, query_class="durable", brand=None, allow_cross_brand=False)` → dict
Wraps `POST /v1/memories/search` with filter `{"user_id": user_id}` (+ brand scoping). Auto-reranks when `limit>=5` (`rerank=True/False` overrides). `query_class`: `durable` (default) | `operational` (recency-decayed) | `canonical` (**required** to retrieve `tier=canonical` records — the default class excludes them) | `history` (forensic — hide checks disabled). `brand` scopes to one brand; a brandless search fail-closes to brand-neutral records only, with `allow_cross_brand=True` as the explicit audited opt-in. W5 surfacing: adds `rerank_note` when the reranker fell back dense-only, and `withheld_note` when superseded/contradicted records were withheld by admission.

### `memory_diagnose(query, target_id, user_id="youruser", brand=None, query_class="durable", threshold=0.1, limit=20, rerank=False, allow_cross_brand=False)` → dict
Wraps `POST /v1/memories/diagnose` (W5 ADOPT-2). Names the first retrieval stage that eats the target; read-only. Set the parameters to the FAILING search's values.

### `memory_list(user_id="youruser", limit=50)` → dict
Wraps `GET /v1/memories`. Client-clamps `limit` at 500 (the server clamps too). Prefer `memory_search` for content discovery; this is for inventory.

### `memory_get_by_id(memory_id)` → dict
Wraps `GET /v1/memories/{id}` — exact read (text, metadata, tier, timestamps). Call it before update/delete/promote to confirm the right record.

### `memory_recall(query, brand=None, initiative=None, project=None, user_id="youruser")` → dict
The proactive start-of-task pull: wraps `POST /v1/context/bundle` (checkpoint suppressed) plus a `query_class="canonical"` search. Returns `{ok, canonical, memories, goals, open_questions}` — a branded recall returns that brand's facts plus the brand-neutral set; brandless returns neutral only.

### `memory_update(memory_id, text)` → dict
Wraps `PUT /v1/memories/{id}`. Text only. Queues to the offline outbox when the authority is unreachable.

### `memory_promote(memory_id, tier="stable", reason=None)` → dict
Wraps `PATCH /v1/memories/{id}/tier` with `actor="claude-autonomous"` (always — the shim never sends another actor). Tiers reachable via MCP: `evidence` | `stable` | `temporal` (`insight` 403s — consolidator-only). **`tier="canonical"` is rejected inside the shim itself**: canonical promotion cannot execute over MCP because the server demands the user-direct HMAC token + nonce headers the shim never sends. The "lock that in" flow is therefore: `memory_add` (evidence) → optionally `memory_promote(tier="stable")` → the operator runs `bash mem0-canonize.sh <id> "<reason>"` (deployed to `~/apps/mem0-scripts/`).

### `memory_demote(memory_id, tier="evidence", reason=None)` → dict
Same wire as promote, opposite direction (also `actor="claude-autonomous"`). Use to walk back wrong tier assignments.

### `memory_delete(memory_id)` → dict
Wraps `DELETE /v1/memories/{id}`. Queues to the offline outbox when the authority is unreachable.

### Episodic tools (one-line contracts)

- `episodic_search(query, since=None, until=None, brand=None, limit=10)` — FTS5 search over past sessions; ISO-8601 date bounds.
- `episodic_recent(limit=7, brand=None)` — last N episodes by `ended_at`.
- `episodic_get(episode_id)` — full episode detail + linked mem0 memory ids.

### Goal tools (one-line contracts)

- `goals_list(status=None, brand=None, limit=20)` — list goals; status ∈ {open, blocked, advanced, completed, abandoned}.
- `goals_tree(root_id=None)` — recursive hierarchy with depth.
- `goals_open(brand=None, limit=10)` / `goals_blocked(brand=None, limit=10)` — convenience filters.
- `goal_details(goal_id)` — full detail incl. linked_episode_count.
- `goal_create_manual(title, description=None, brand=None, priority=3, parent_goal_id=None)` — operator-direct goal creation.
- `goal_abandon(goal_id, reason, actor="claude-autonomous")` / `goal_complete(goal_id, reason, actor="claude-autonomous")` — lifecycle closes; non-empty reason required.
- `goal_set_priority(goal_id, priority, reason=None, actor="claude-autonomous")` — 1=highest, 5=lowest.
- `goal_link_episode(goal_id, episode_id, link_type="advanced_goal", delta_text=None, actor="claude-autonomous")` — link_type ∈ {advanced_goal, blocked_goal, completed_goal, cited_goal}.
- `goal_merge(source_goal_id, target_goal_id, reason, actor="claude-autonomous")` — merge duplicates; source kept for audit, excluded from listings.

### Open-question tools (one-line contracts)

- `open_questions_open(brand=None, limit=7)` — current open frontier questions.
- `open_question_search(query, brand=None, status="open", limit=10)` — FTS5 search; `status="all"` includes resolved.
- `open_question_resolve(open_question_id, resolution_text, resolved_in_session_id, actor="claude-autonomous")` — mark resolved.
- `open_question_details(open_question_id)` — full detail incl. related goal.

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

After upgrade, run the real gates: the deployed **`Test-MemoryStack.ps1`** (`~/.claude/scripts/Test-MemoryStack.ps1` — liveness + invariants, pass/fail per row) and the server **pytest suite** (`mem0-server/tests/`, on the server venv). If either fails, the contract is broken; roll back or fix `mem0-server/app.py` / `mem0-mcp-shim.py` to restore the surface. (An earlier revision of this doc pointed at an `audit/upgrade-smoke.ps1` script that never shipped.)
