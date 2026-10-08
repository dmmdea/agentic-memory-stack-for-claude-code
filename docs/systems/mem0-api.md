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
- **`X-AMS-Service-Key`** (1.32.5) — an optional second header on the write routes, carrying the authority-only service key. It is what makes a server-side job label (`actor` / `metadata.source` strings such as `contradiction-sweep-v019` or `dream-consolidator`) count: those labels are free text, and the shared API key is held by every PC and MCP session, so a label alone grants nothing.
- **Tier gate on write** — the server, not the caller, decides which tier a write may land in; `canonical` is never writable via `add`.
- **`infer`** — `false` stores the payload as-is (all automated paths); `true` runs mem0's LLM extraction.
- **Query class** — a search-time mode (`durable`/`operational`/`canonical`/`history`) that selects the admitted tiers and recency policy.

## How the system works

### Auth

Every REST request requires `X-API-Key: <key>` as a header. The key is stored in `~/.mem0/api-key` (WSL, mode 600) and compared with `hmac.compare_digest`. The MCP shim reads the same file at startup; callers outside the shim must supply it manually.

Missing or incorrect key → `401 {"detail": "missing or invalid X-API-Key"}`.

#### The service key (1.32.5)

The shared API key proves "some stack caller", never "the authority's own job". Three families of free-text label used to act as credentials on top of it, so any API-key holder could send one:

- the stamp jobs `stamp-retired-v013` (`retired_at`) and `contradiction-sweep-v019` (`contradicts_canonical`, `contradiction_checked_at`, `contradicts_canonical_pending`), which skip the canonical/insight HMAC gate on `PATCH /metadata`;
- the legacy lifecycle writers `backfill-apply-v013` (`retrievable`), `decay-scan` (`expires_at`) and `system` (`expires_at`, `tier_actor`);
- the insight consolidators `c1-consolidator`, `dream-consolidator`, `c1-dream-consolidator`, which wrote insight with no HMAC through `POST`, `PUT`, `DELETE` and the tier `PATCH`.

The result was that any holder could hide non-canonical records (`contradicts_canonical`, `retrievable=false`), schedule deletion (`expires_at`), stamp `retired_at` on a canonical, and mint, rewrite or delete insight records. Now each write route (`POST /v1/memories` on an insight add, `PUT`, `DELETE`, `PATCH /tier`, `PATCH /metadata`) calls `security_invariants.require_service_credential` first. For a label in any of the three tables it compares `X-AMS-Service-Key` with the loaded key in constant time and answers `403 service-credential-required: <field>=<label> is a server-side job label and ...` when the header is missing or wrong, **whatever the tier of the target record**. The tail of the message tells the two causes apart: "this server holds no service key" (a replica or PC, or an authority whose key is missing: re-run the installer) or "the request did not carry the authority's service key". A request with no job label is untouched and needs no header. The policy functions (`assert_writable`, `validate_insight_actor`, `authorize_metadata_patch`) take `service_verified`, default `False`, and read an unproven label as plain text, so a handler that forgot the gate denies rather than allows.

Where the key lives: the native authority loads it as the systemd credential `ams-service-key` (`LoadCredentialEncrypted=` from `<secrets-dir>/ams-service-key.cred`) into `mem0.service`, the dream and contradiction-sweep step units, and the transient units of `ams-dream-now.sh` and `ams-service-run.sh`; a WSL authority keeps it in `~/.mem0/service-key` (mode 600). Replicas and PCs never hold it, so a replica's dormant server refuses every job label. It is a separate secret from the canonical HMAC key (a job holding it cannot mint a canonical token) and is regenerable: nothing outside the authority needs a copy. Once `mem0.service` carries the credential line, a missing or undecryptable `.cred` stops the service from starting, and re-running the installer regenerates it. Callers: the dream and the sweep send the header from `ams_env.mem0_headers()` when they hold the key; an operator's hand run on the native authority goes through `bash ~/apps/mem0-scripts/ams-service-run.sh <script> [args]` (`contradiction-sweep.py`, `stamp-retired-at.py`, `ship_log_reclassify.py`); the MCP shim and the offline outbox replay never send it.

### The write path

`POST /v1/memories` runs a fixed, order-dependent pipeline before it stores anything: (1) size cap (`MAX_MEMORY_CHARS`, default 4000) → `413`; (2) empty-string guard → `400`; (3) the tier gate (below; an insight add starts with the service-credential check on `metadata.source`); (4) a strip of caller-supplied retrieval-gating metadata keys the caller must not be able to forge (`contradicts_canonical`, `superseded_by`, `retrievable`, …); (5) hash idempotency — on `infer=false`, an exact-hash duplicate in the same `(user_id, workspace, project)` scope returns the existing id and writes nothing. The tier gate and metadata strip run **before** dedup by design, so a rejected write is rejected whether or not its text already existed.

### The read path

`POST /v1/memories/search` (and the internal `/v1/context/bundle`) share `_search_core`: embed → Qdrant cosine ANN → optional rerank → query-class recency policy → the server-side admission gate. Retired (`retrievable=false`) and `_canonical_intent` records are filtered out unless explicitly opted in.

## Important flows

The end-to-end capture and retrieval paths that drive this API are documented as flows: [`../flows/memory-capture.md`](../flows/memory-capture.md) and [`../flows/memory-retrieval.md`](../flows/memory-retrieval.md).

## Data and state

- **Vector store:** Qdrant collection `mem0_egemma_768` (768-dim EmbeddingGemma vectors) on `:6333`; tier and metadata live in each point's payload.
- **History:** mem0's `~/.mem0/history.db` (SQLite).
- **Tier ledger:** append-only `~/.mem0/tier-ledger-YYYY-MM.jsonl` (monthly segments; the legacy `tier-ledger.jsonl` is a frozen archive) — every tier change, metadata merge, and decay-delete lands here.
- **API key:** `~/.mem0/api-key` (mode 600). **Canonical HMAC key:** resolved via the DPAPI provider (see [`dpapi-canonical-key.md`](./dpapi-canonical-key.md)). **Service key** (1.32.5): the systemd credential `ams-service-key` on the native authority, `~/.mem0/service-key` (mode 600) on a WSL authority, absent everywhere else; it is read by the same provider class as the canonical key (`service_key_provider()`), under its own file names, so neither key can fall back to the other's file. Nothing to back up: the installer regenerates it.

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

Checks Qdrant collection status, EmbeddingGemma embedder dimension (via llama-swap), and mem0 collection point count. Also surfaces the canonical-key health, the service-key presence, admission-rejection counters, pending contradiction-review depth, nightly-job + session receipt ages, drift-guard state, the passive reranker counters, the admission self-probe, and the capability manifest. Slow (~1-3s). Use for diagnostics, not polling. **No check here may perform a slow ACTIVE model call** — `scripts/wsl/deploy.sh` gates on this endpoint seconds after a restart (with `--max-time 60`), so a cold model behind a probe here blocks deploys regardless of device; that is why the reranker is surfaced passively.

```
Response 200: {"ok": true, "checks": {"qdrant": {"ok": true, "points": N, "status": "green"}, "embedder": {"ok": true, "dim": 768}}}
Response 200 (degraded): {"ok": false, "checks": {"qdrant": {"ok": false, "error": "..."}}}
```

Three checks added by the 2026-08 audit:

- **`sparse_leg` — GATING** (flips `ok` to `false`): BM25 lexical-leg liveness. mem0 fail-softs to dense-only search silently when the fastembed sparse encoder is missing (the installer now declares fastembed explicitly, with a loadability post-condition); this check makes that fallback loud with a **deterministic canary** — it derives a token from the *oldest* BM25-bearing point (longest token, lexicographic tiebreak, from its lemmatized text) and requires that point back in the top-5 keyword results, so a tokenizer/hash change that strands the legacy corpus also fails. Shape: `{"ok": bool, "fastembed": bool, "bm25_slot": bool, "points": N, "with_bm25": N, "coverage": 0.0-1.0, "canary": {"ran": bool, "hit": bool, "token": "..."}, "error"?: "..."}`. `coverage` (`with_bm25 / points`) is reported but does not gate — a half-backfilled corpus with a live encoder is degraded, not dead (`Test-MemoryStack` WARNs below 0.95).
- **`mojibake` — informational** (never flips `ok`): CP437 mojibake corpus tripwire — scans point text payloads for the glyph-pair signature of UTF-8 bytes decoded through the OEM console codepage. Shape: `{"ok": true, "scanned": N, "hits": N, "sample_ids": [...], "allowlisted": N, "elapsed_ms": N}`. A point whose payload carries `mojibake_ok: true` (the literal boolean, set per point by the operator) is skipped and counted in `allowlisted`: a legitimate note that quotes mojibake as an example must not hold the tripwire at `degraded`. The scroll is page-capped so the walk stays bounded as the corpus grows; `scanned` stays honest about a capped scan. `Test-MemoryStack` WARNs on `hits > 0`.
- **`promotion_gate` / top-level `promotion_gate_mode` — informational**: the effective 4C promotion-gate mode, resolved like the dream does (`MEM0_PROMOTION_GATE_MODE` env, else `stack.env`, else `shadow`). Shape: `{"role": "brain"|..., "mode": "enforce"|"shadow"|"off", "source": "env"|"stack.env"|"default"}`. The `promotion-gate` capability reads `degraded` on a brain that is not enforcing.
- **`admission_rejections_today.stamps` — informational**: `{"stamp_target_unresolved": N, "stamp_ignored_not_canonical": N}` - daily counters for the contradiction-stamp tier resolution ([admission-gate.md](./admission-gate.md)): a lookup that failed (stamp ignored, fail-open) and a stamp ignored because its target is no longer canonical.
- **`put_carryover_today` — informational** (never flips `ok`): daily counters proving the PUT payload carry-over (below) is active. Shape: `{"date": "YYYY-MM-DD", "puts": N, "keys_restored": N, "keys_lost": N}` — `keys_restored`/`keys_lost` count post-verify repair activity and should stay 0; nonzero values indicate mem0-contract drift.

Five more added by W3 and W4 (the alarm-mouth + revive-or-bury tracks — all informational, never flip `ok`; the fixed key names below are a cross-track contract consumed by the verifier, the dream heartbeat, and the session-banner digest):

- **`job_liveness`**: age of every receipt the stack leaves behind. Shape: `{"role": "brain"|"replica"|null, "last_dream_age_h": h|null, "prune_age_h": h|null, "gather_age_h": h|null, "backup_manifest_age_h": h|null, "dedup_report_age_h": h|null, "morning_summary_age_h": h|null, "morning_summary_sections_48h": N|null, "l1a_attempt_age_h": h|null, "l1a_success_age_h": h|null, "sessionstart_banner_age_h": h|null, "mcp_shim_receipt_age_h": h|null, "mcp_shim_host_match": bool|null, "mcp_shim_stack_version": "..."|null, "brand_scope_age_h": h|null, "brand_scope_misscoped": N|null, "outbox_depth": N|null, "outbox_replayed_age_h": h|null, "outbox_drain_log_age_h": h|null, "error"?: "..."}`. On a native Linux brain (`MEM0_HOST_KIND=native`, 1.28.5) `last_dream_age_h`, `prune_age_h`, `gather_age_h` and the two `morning_summary_*` fields are read from `~/.mem0/maintenance/` (the chain's own state dir: `last-dream` epoch content, `dream/prune.json`, `dream/gather.json`, `morning-summary.md`) whenever the Windows profile did not fill them; a Windows profile's markers, when present, take precedence. Every field is independently fail-soft — a missing file/env yields `null` for that field plus a note in `error` while the rest still populate. `last_dream_age_h`, the two `l1a_*` fields and `sessionstart_banner_age_h` derive from their stamp files' epoch **content**, never the mtime (a copy/restore changes mtime); `role` comes from env `MEM0_ROLE`, else the WSL `stack.env` receipt, else the `~/.mem0/role` installer receipt. **Key names are a cross-track contract — W4 ADDED keys and additions are safe, but nothing here may be renamed** (the verifier, the dream heartbeat and the session-banner digest all read by name). Two W4 notes: the epoch parser strips a leading **U+FEFF**, because the Windows PowerShell 5.1 hooks write these stamps with `Set-Content -Encoding UTF8`, which emits a BOM that `strip()` does not remove; and `mcp_shim_host_match` exists because `~/.mem0` travels in stack backups, so a receipt restored from another machine must not read as local liveness.
- **`reranker` — informational** (never flips `ok`): **passive** cross-encoder counters, bumped by real search reranks. Shape: `{"last_rerank_ok_ts": epoch|null, "consecutive_rerank_failures": N, "ok_total": N, "fail_total": N, "last_error": "..."|null, "cold_retry_total": N}`. There is deliberately **no active rerank probe on this endpoint** — the reranker is GPU-served but not resident (it unloads after five idle minutes, so a cold load can take seconds; the verifier budgets 90s) and `deploy.sh` gates on `/health/deep` seconds after a restart, so an active probe would hang deploys on a cold model. `cold_retry_total` counts first attempts that timed out and were retried once with the cold-start allowance (bounded by the 20 s whole-rerank ceiling) (see [reranker.md](./reranker.md)). The active 3-doc probe lives in `Test-MemoryStack` check L5.
- **`admission_probe` — informational** (never flips `ok`): the admission gate's in-process self-probe. Shape: `{"ok": bool, "tier_rejected": bool|null, "brand_rejected": bool|null, "neutral_admitted": bool|null, "query_class": "durable", "error"?: "..."}`. Three synthetic records go through `AdmissionPolicy.evaluate()` **directly** — never `apply_admission`, which would bump the daily rejection counters this same response reports and append to `~/.mem0/admission-rejected.jsonl` on every health read. Zero I/O, zero side effects; it also runs on an empty store.
- **`retrieval_drift`**: passthrough of the retrieval-drift guard's state sidecar (`~/.mem0/retrieval-drift-state.json`; the guard itself lives in a private evaluation repo). Shape: `{"state_present": bool, "last_compare_ts": ts|null, "age_hours": h|null, "before_retrievable": N|null, "n_total": N|null, "hwm": N|null, "hwm_seeded": bool|null, "consecutive_below_hwm": N|null, "consecutive_snapshot_failures": N|null, "alarm": bool|null, "missing": [...]|null, "compat_fallback": bool|null, "error"?: "..."}`. An absent file is `state_present: false` with everything null and **no** error (a not-yet-deployed guard is a fact, not a fault); a present-but-malformed file is `state_present: true` plus an `error` note.
- **`capabilities`**: the capability-manifest verdict — a pure fold of the checks above against the `CAPABILITIES` literal. Shape: `{"role": "brain"|"replica"|null, "states": {"<id>": "alive"|"degraded"|"dead"|"unknown"|"retired"}, "dead_required": ["<id>", ...], "unknown": ["<id>", ...], "evaluated_at": "<iso>"}`. `dead_required` lists dead rows required for this box's role (unknown role never convicts role-scoped rows; zero-signal activity counters are `unknown`, never `alive`). Row table and verdict rules: [../capability-manifest.md](../capability-manifest.md). The verifier FAILs on a non-empty `dead_required`.

One more, added in 1.32.5:

- **`service_key` — informational** (never flips `ok`): whether this server loaded the service key that proves a server-side job label (see Auth). Shape: `{"present": bool, "source": "credential"|"runtime"|"plaintext"|"none"}`. A box without it is healthy and refuses every job label, which is correct on a replica or PC. On the brain the capability row `service-key` (required for role `brain`) reads `dead` when `present` is false, because the dream's insight writes and the sweep's stamps would all get `403 service-credential-required`; the installer (`install/linux-authority.sh`) fails the install unless `/health/deep` reports `checks.service_key.present: true`, and `scripts/wsl/deploy.sh` asserts it after a WSL restart.

### `GET /health/maintenance`

The authority's nightly chain, folded from `~/.mem0/maintenance/receipts.jsonl` (v1.21; v1.22 adds `dataset` and `usage`): `steps.<name>` = `{last_success, last_run, duration_ms, receipt_id, ok, status}` (`status` = `ok|degraded|failed`, the receipt's outcome); `stale_steps` = daily steps without a success in 48 h, and `--weekly` steps (recognised by their `weekly:` off-day no-op receipts, or by every recent receipt falling on a Sunday) without a real run in 8 days; `failed_steps` / `degraded_steps` = `[{step, ts, note}]` for each step whose LATEST real run failed / degraded (a later ok run clears it; the `weekly:` and `guard:` no-op receipts are not runs, so a Sunday failure stays listed all week; `health-stamp` is excluded because it exits non-zero on the verdict it prints); `judge_transport` = `native|shim|none`; `pool` = `{used_pct, alarm, threshold_pct, health, health_alarm[, health_ack]}` of the **pool** as `zpool list` reports capacity (alarm at 85 %) and `zpool list -H -o health` reports health (`health_alarm` on anything but `ONLINE`; `health` is `"unknown"`, with no alarm, when the box has no ZFS dataset configured or the reader fails: fail-open on the reader, loud in the value). **Pool-health acknowledgment.** A known, dated non-`ONLINE` pool (a planned disk swap) is acknowledged by the operator with `MEM0_POOL_HEALTH_ACK=<STATE>:<YYYY-MM-DD>` (e.g. `DEGRADED:2026-10-06`), read on every call: the process environment first, then `~/.mem0/stack.env` (the server unit does not load that file into its environment, so the endpoint opens it itself). The ack is active iff it parses, today's UTC date is on or before its date, and the live `health` equals its `STATE` (case-insensitive). Active: `health` stays the live value, `health_alarm` is `false`, `health_ack` = `{state, until, active: true}`, and `ok` no longer counts the pool health (capacity and every step still count). Not active: `health_alarm` stays as the live value dictates and `health_ack` = `{state, until, active: false, reason}` with `reason` = `expired` (the date has passed), `mismatch` (the pool is in a different state than the ack names, e.g. an ack for `DEGRADED` while the pool is `FAULTED`) or `malformed` (`state` and `until` null, plus the raw `value`). No key: no `health_ack` field. An ack never makes an `ONLINE` pool alarm, and it hides a `FAULTED` or `UNAVAIL` pool only when it names exactly that state. To set and clear it, see [../operations.md](../operations.md), "Pool-health acknowledgment". `dataset` = `{used_bytes, avail_bytes, used_pct}` of the AMS dataset (quota headroom, informational; present only with `MEM0_ZFS_DATASET`); `usage` = the newest Codex plan-window probe `{used_percent, resets_in_days, probed_at, note}`; `drift` = `{alarm, before, n_total}` from the retrieval-drift state file and `wiki` = `{last_pull_age_h, last_build_age_h, fresh_age_h}` from the `~/wiki-index/last-pull` and `last-build` epoch stamps (`fresh_age_h` is the newer of the two; each null when unreadable) — both reported, neither folded into `ok`; `boots_7d` = boot ids of the last seven days. `ok` is `not pool.alarm and not pool.health_alarm and not stale_steps and not failed_steps and not degraded_steps and write_path.ok is not false`: a step that ran but did nothing (`degraded`) turns it false just as a failed one does, and so does a failing write path (below). Previously `ok` looked at the pool alarm and staleness only, so a step that failed last night after succeeding the night before, and a DEGRADED mirror, both read green. Never raises on a reader: an unreadable pool/journal/ledger reads as unknown.

**`critical_failed_steps`: the failed steps that can be acted on at night.** `failed_steps` minus the steps whose failure cannot be fixed at night because they depend on a PC being switched on (`PC_DEPENDENT_STEPS` in [`maintenance_health.py`](../../mem0-server/maintenance_health.py); today only `wiki-index`, whose nightly pull needs a PC that mounts the vault and fails by design once every PC has been off for more than 72 h, see [wiki-index.md](./wiki-index.md)). It has the same `[{step, ts, note}]` entries in the same order as `failed_steps`, it is always present (`[]` when nothing failed), and it is derived from `failed_steps`: `ok`, `failed_steps` and every other field are computed exactly as before. Its purpose is paging: an external monitor can page **urgently** (through night quiet hours) on `critical_failed_steps` while `failed_steps` keeps paging at the normal level, so a failed `wiki-index` still pages normally and any other failed step pages on both. The unit is the step: a `wiki-index` run whose pull succeeded but whose build failed is held back from the urgent list too, and still shows in `failed_steps`.

**`write_path` (1.32.1): the write path, learned from real writes.** `{ok, last_ok_at, last_error_at, last_error, errors_1h, writes_1h}`, published by [`write_path.py`](../../mem0-server/write_path.py). One HTTP middleware feeds an in-process tracker with the FINAL status of every `POST /v1/memories` and `PUT /v1/memories/{id}` (and of no other route: `/v1/memories/search`, `/diagnose`, the tier and metadata `PATCH`es, `DELETE` and the reads share the prefix but are not the write path). It sees the response after the exception handlers ran, so the `503` that `embedder_503` builds and the `503` or `500` a route raises as an `HTTPException` (`_upstream_error`) are both recorded; an exception nobody handles is recorded as a `500` and raised again, and the middleware never changes what the client receives. Only two kinds of outcome count: a `2xx` is a success and a `5xx` a failure. A `4xx` (admission reject, validation, auth) is the caller's problem and is not recorded, so it neither fails the path nor clears a failure. A `5xx` counts **whatever its cause**: the write routes answer every unexpected error that is neither one of their own explicit `4xx` nor a recognised outage (`503`) with a `500`, so a server-side bug on the route is a broken write path too, and so is a caller-caused error that reaches that catch-all (a `PUT` of an id that does not exist, for one: mem0 raises its not-found as a plain error, which the route answers `500`). A `2xx` that never reached the embedder is the exception, and is neutral (below).

- `ok` is `false` iff the most recent recorded outcome is a failure. It goes false on the first failed write and **clears only on the next successful, non-neutral one**. There is **no time decay**: an hour without writes after a failure still reads `false`, because an unknown path is not a healthy one. A server that has recorded nothing (just restarted) reads `ok: true` with nulls: no evidence either way.
- `last_ok_at` / `last_error_at` are UTC ISO-8601 stamps of the latest success and the latest failure since the server started (`null` before the first of each). `last_error` is `"<status> <reason>"`; it stays on record after `ok` clears. The reason is the `reason` the failing response's JSON body names, and `upstream` when the response names none. Only the `embedder_503` handler names one (`cold-embedder`), and it answers only for an embedder-shaped exception that escapes a route. The write routes catch their own embed errors and answer through `_upstream_error`, an `HTTPException` whose body is `{"detail": ...}`, so **an embedder that cannot start usually reads `503 upstream` on a write** (`500 upstream` for an error the server does not recognise as an outage, a server-side bug included); expect that, not `503 cold-embedder`, in the live outage case.
- `errors_1h` / `writes_1h` count the failures and all recorded outcomes of the last 3600 s. The tracker keeps those timestamps and nothing older, so its memory follows the last hour's write rate. Unlike `ok`, these do age out.
- **Neutral answers.** A `2xx` that never reached the embedder says nothing about the path, so it is recorded as nothing: it is not counted in `writes_1h`, it is not a success, and it does not clear a failure. The add route marks its answer neutral (through `request.state`; the middleware reads the mark) in two cases: an idempotent duplicate (`deduplicated: true`, answered from a payload lookup before any embed call) and an `infer: false` add that stored nothing (`results: []`, every message skipped: system-role or malformed messages never reach the embedder). Automated writers re-post whole transcripts, so during an embedder outage most of their writes are exactly such duplicates, interleaved with the `5xx` for the new facts; counted as successes they made `ok` flap back to `true` between two failures. Neutral suppresses a `2xx` only: a `5xx`, and an exception nobody handles, is recorded whatever the route marked. Not neutral, because they do exercise the embedder: an `infer: true` add (mem0 embeds the incoming text to look up existing memories before it can answer at all, as of mem0 2.0.4) and any `PUT` (`Memory.update` embeds the new text first, and the route has no other `2xx` exit). The price is that after the embedder recovers, `ok` stays `false` until a write that stores something arrives; an hour of re-posted duplicates does not clear it. A hand check of the embedder does not wait for one ([../operations.md](../operations.md), "The banner says the write path is failing").

**Why it is passive.** The endpoints that do touch the embedder (`/health/embedder`, `/health/deep`) load the model, so an uptime checker polling them every few minutes would keep it resident, and every idle model must unload after five minutes. The tracker costs a lock and two counters per write and nothing per read, so `/health/maintenance` stays safe to poll as often as anything likes. The price is that it cannot see a dead embedder until a write hits it, nor a recovered one until a write that stores something does. The state lives in the server process (a single uvicorn worker) and nowhere else: nothing is written to disk, and a restart resets it. For a hand check of the embedder itself, call `/health/embedder` once, never on a schedule ([../operations.md](../operations.md), "The banner says the write path is failing").

The payload has no `write_path` key when no tracker is wired in (an older build), and `ok` is then what it was before; a tracker that cannot be read reads `{"ok": null, "note": "write-path reader failed"}` and does not change `ok`. The session banner names a failing write path on its `NOT OK` line ([sessionstart-banner.md](./sessionstart-banner.md)). The nightly chain's last reading step, `ams-health-stamp.sh`, exits `2` on it (a broken write path is a red night, like a failed step) and, with `ams-morning-summary.sh`, appends ` write-path <last_error>` to its health line; a healthy or unreadable write path adds nothing to either line.

**`capture` (informational): is the PC-side L1a extractor still finishing runs?** `{state, stalled, success_at, success_age_h, activity_at, activity_age_h, quiet_after_h, stalled_after_h[, note]}`, read per request from `episodic.db` over a read-only connection that gives up on a lock after 1 s (index reads, about a millisecond; no Qdrant, no model, no cache). It exists because the authority had no view of a dead extractor: `job_liveness` reads the L1a stamps from a Windows profile only, so on a native Linux authority (`MEM0_HOST_KIND=native`, no Windows profile) the `l1a-extraction` capability row has no evidence and reads `unknown`. Two facts carry the block. `activity_at` is the newest `episodes.ended_at` in any state (every prompt of a PC session moves it, whether or not L1a ever runs); `success_at` is the newest `ended_at` of a `complete` episode (L1a's `POST /v1/episodes` writes one; so does the operator's `ship_log_reclassify.py --apply --live`, which can therefore mask a stall for up to 96 h). The `*_at` fields are UTC ISO-8601 stamps and the `*_age_h` fields the hours since, to one decimal and never negative (a PC clock that runs ahead can only make the data look fresher); each is `null` when there is no such episode, and a stamp more than an hour ahead of the server is ignored (a PC clock error) and named in `note`. `state` is `ok` when a run finished within 48 h (`quiet_after_h`); `stalled` when none has for more than 96 h (`stalled_after_h`) while a session was active within 48 h and the sessions since the last success have been going for at least an hour (L1a's first chance; so the first prompt after a long trip reads `quiet`); `quiet` for any other silence (the PCs are off, or the silence is still inside the grace window); and `unknown` when no run is on record (a broken extractor cannot be told from a new install) or the store could not be read (then `note` is `"capture reader failed"`: fail-open on the reader, loud in the value). The two thresholds are the capability manifest's own (`FRESH_H` and `L1A_CONVICT_H`, see [../capability-manifest.md](../capability-manifest.md)). `stalled` is a plain boolean (`state == "stalled"`), so a monitor condition can read it. **It never folds into `ok`, `failed_steps`, `stale_steps` or `degraded_steps`**: PCs that are switched off are not a chain fault, so a stalled capture is a heads-up of its own. The nightly chain's reading steps name it without going red: `ams-health-stamp.sh` and `ams-morning-summary.sh` append ` capture stalled <success_age_h>h` to their health line when `stalled` is true, and any other state adds nothing to either. The payload has no `capture` key when no reader is wired in (an older build). What to do about a `stalled` reading: [../operations.md](../operations.md), "L1a fires but no facts get extracted".

### `GET /health/morning-summary`

`{path, mtime, sections: [...]}` — the last three `## ` sections of the chain's morning summary (`~/.mem0/maintenance/morning-summary.md`), for the session-start line; `404 {"detail": "no summary yet"}` before the first chain night.

### `GET /health/embedder`

Warms and reports the embedder: `{"ok": true, "loaded": bool|null, "warm_ms": int}` after one embedding through llama-swap (`loaded` from `/v1/models`, read from the flat `state`/`status` string or from llama-swap v256 and later's `status: {"value": ...}` object; `null` when the listing does not say). `?warm=rerank` then issues a one-document rerank and adds `"rerank": {"ok": bool, "warm_ms": int | "error": str}`; a reranker that cannot load is reported there and never fails the endpoint. A cold or down embedder answers `503` + `Retry-After: 10` with `reason: cold-embedder`, the same body every embedder outage gets (`embedder_503.py`); the SessionStart hook calls `/health/embedder?warm=rerank` detached so the first prompt finds the embedder warm and the first deliberate search finds the reranker warm.

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
  - `tier=insight` → `403` unless `metadata.source` is one of the exact consolidator allowlist actors (`c1-consolidator`, `dream-consolidator`, `c1-dream-consolidator`) **and** the request carries the service key in `X-AMS-Service-Key` (1.32.5). A job-label `source` without the key is `403 service-credential-required: metadata.source=<label> is a server-side job label and ...`; a `source` that is neither on the allowlist nor a job label is the plain "reserved for the C1/dream consolidator" `403`. The header is read only on an insight add. The old substring check (`"c1" in source`) was trivially bypassable and was replaced by the exact allowlist `INSIGHT_ALLOWED_ACTORS` (one copy, in `security_invariants`); the allowlist alone then proved nothing, since the label was free text any API-key holder could type, which is what the service key closes. Over MCP an insight add never gets here: the shim downgrades it to `evidence`.
  - `tier=evidence` or `tier=temporal` → allowed.
  - No metadata.tier → defaults to no tier label (retrieved as untiered evidence).
- **Size limit:** `MAX_MEMORY_CHARS = 4000` (env-overridable via `MEM0_MAX_MEMORY_CHARS`). Payload above this → `413`. Break into atomic facts.
- **Idempotency:** on `infer=false`, a byte-identical memory already stored in the same scope returns the existing id (`"deduplicated": true`) and writes nothing.

```
Response 200: {"results": [{"id": "<uuid>", "memory": "...", ...}]}
Response 400: empty memory
Response 403: tier enforcement, insight-source missing, or an insight source that is a job label sent without the service key (service-credential-required)
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

- `score` is the [hybrid fusion](fusion.md) score: reciprocal rank fusion of the dense, keyword and entity legs, in (0, 1] and monotone with the order; it is not a cosine, so never threshold on it as one (`threshold` in the request is compared to the raw cosine, before fusion).
- `rerank=true` triggers `bge-reranker-v2-m3` post-processing (`reranker.py`), applied only when there are ≥ 3 results **and** the head is not unanimous (fused score < `RERANK_SKIP_IF_TOP_SCORE`, 1.0; `RERANK_MIN_N`). The reranker is a cross-encoder served on llama-swap `:11436` (GPU since 2026-08-13; raw-logit score scale is device-independent); any reranker failure returns the dense-only order unchanged and logs a WARN (fail-soft).
- `query_class` (default `durable`) selects the admitted-tier set and recency policy: `operational` applies a 30-day Weibull recency weight; `canonical` filters to `{canonical, stable}`; `history` disables supersession/contradiction hiding (forensic).
- `limit` clamped at 500 server-side.

```
Response 200: {"results": [{"id": "...", "memory": "...", "score": 0.83, "metadata": {...}}, ...]}
```

### `PUT /v1/memories/{id}`

Update a memory's text content. The **full existing payload is carried over** atomically into the rewrite (tier, source, brand, project, provenance stamps — every custom key): mem0 rebuilds the Qdrant payload from scratch on update, so before this carry-over a PUT silently destroyed all custom metadata (only tier was restored). The pre-update payload read is **fail-closed**: if it errors, the PUT is refused with `503` rather than performing a blind update that would wipe metadata. Because a PUT changes the text, the record **re-enters the NLI write-gate**: the carry-over deliberately drops the NLI check-markers (a judgment of the old text must not vouch for the new one) and re-judgment of the new text is queued asynchronously, exactly like an add. Canonical/insight records require a valid HMAC user-direct token (`mem0-canonize.sh --action put`), with one exemption: the consolidator's `actor` label (query param) passes an **insight** record without it, and since 1.32.5 only when the request carries `X-AMS-Service-Key`. A job label in `actor` without that header is `403 service-credential-required` on a record of any tier, before the tier gate, the size cap or the payload read; canonical text is additionally run through the imperative-canary and rejected `422` if it reads as a standing order. `PUT` never retires a fact: when the new text carries a hand-written `SUPERSEDED <date> by mem0 <id>` marker and the record is not already superseded, the answer gains `supersede_note` and `supersede_marker` (`{kind, winner_id}`) and the server does nothing else, because the gate reads the `superseded_by` field, never the text.

```json
Request: {"text": "new content"}
Response 200: mem0 update result
Response 400: pre-update read rejected by the store (malformed memory id — permanent fault, never queued)
Response 403: canonical/insight target without a valid HMAC token (an insight target also passes with a consolidator actor plus the service key), or a job-label actor sent without the service key (service-credential-required)
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
- The route reads `X-AMS-Service-Key` first (1.32.5): an `actor` that is a job label (the three consolidators, the stamp labels and the legacy labels listed under Auth) without a matching key is `403 service-credential-required: actor=<label> is a server-side job label and ...`, whatever the tier asked for. An ordinary actor (`claude-autonomous`, `user-direct`) ignores the header.
- `tier=insight` requires `actor` in the exact allowlist `{c1-consolidator, dream-consolidator, c1-dream-consolidator}` **and** the service key (the label is checked first, so an allowlisted label without the key is the service-credential `403`). Any other actor → `403`.
- `tier in {evidence, stable, temporal}` accepts `claude-autonomous` — autonomous Claude can only ever set these, and only on a record that is neither canonical nor insight.
- **Moving a record out of `canonical`, or since 1.32.5 out of `insight`** (to any other tier) needs the same kind of token signed for the action word `demote` (`<ts>|<nonce>|demote|<mid>|<reason>`, produced by `mem0-canonize.sh --action demote [--tier evidence|stable|temporal]`) and a non-empty `reason`. Without it → `403`; a promote token cannot be replayed as a demotion. **No job label exempts an insight demotion, the consolidator with the service key included**: nothing in the stack demotes an insight, so it leaves the tier only through the operator's signed path. Before 1.32.5 `insight → evidence` needed no token, after which `PUT` and `DELETE` were ungated (their gate reads the tier the record has at that moment): the insight gate fell to two plain requests, the same two-step hole the canonical demote token already closed. A record with no `tier` field reads as canonical (fail-closed), so an unsigned change to it is also `403`. The tier is re-read under the record's write lock: if the record became canonical **or insight** while an unsigned change was in flight → `409` (`the record became <tier> while this tier change was in flight; retry. Moving it out of <tier> needs the signed 'demote' token`, naming whichever tier it is; retry, or sign it). A move to a non-canonical tier on a record that does not exist (or was deleted mid-flight) → `404`. The per-record write lock keys on the canonical UUID spelling, so every spelling of one id serializes on the same lock.

```
Response 200: {"ok": true, "memory_id": "...", "tier": "canonical", "actor": "user-direct", "ts": "2026-..."}
Response 400: missing actor, missing reason for a canonical promotion or a demotion (out of canonical or insight), invalid tier
Response 403: actor/tier enforcement rejected, a job-label actor without the service key (service-credential-required), canonical promote without nonce, or a move out of canonical or insight without the signed demote token
Response 404: a move to a non-canonical tier on a record that does not exist (or was deleted mid-flight)
Response 409: the record became canonical or insight while an unsigned tier change was in flight
Response 422: imperative text rejected from canonical
Response 503: the tier could not be read (store unreachable) or the audit intent line could not be written
```

### `PATCH /v1/memories/{id}/metadata`

Partial metadata update (shallow merge, not replace). Cannot change `tier` (use `PATCH /tier`). Used by re-extraction (marks originals `retrievable=false`), decay (sets `temporal.expires_at`), and the dream consolidator (stamps `touched_by_dream`). Lifecycle-critical keys that gate retrieval (`retrievable`, `contradicts_canonical`, …) are in `security_invariants.METADATA_FORBIDDEN_KEYS`, and `authorize_metadata_patch` (a pure function, 1.32.4) decides each write: only a trusted actor (per-actor `TRUSTED_PATCH_ACTORS` allowlist, plus the legacy lifecycle labels in `LEGACY_PATCH_ACTOR_KEYS`) may write them, and a trusted actor writes only its own keys. Since 1.32.5 an actor counts as trusted only when the request carries `X-AMS-Service-Key`: the handler checks it before anything else (`403 service-credential-required` for a job label without it, whatever the target's tier) and hands the policy functions `service_verified`, so an unproven label gets no key allowance and no HMAC bypass. The stamp jobs' bypass of the canonical/insight HMAC gate (`stamp-retired-v013` for `retired_at`, `contradiction-sweep-v019` for its three stamp keys) is the same: it holds only with the key. `superseded_by` and the supersede door's other keys (`superseded_at`, `superseded_via`, `partially_superseded_by`) are in the list with **no** actor allowed: their only writer is `POST /v1/memories/{id}/supersede` (below). A hide key (`superseded_by`, `contradicts_canonical`) is refused on a `canonical` record for every actor, because the trusted-actor path skips the HMAC check and an actor string alone must never hide a canonical. Every successful merge is appended to the tier ledger.

### `POST /v1/memories/{id}/supersede` and `DELETE /v1/memories/{id}/supersede`

The supersede door (1.32.4): the **only writer of `superseded_by`**, and the way a session retires a stale fact. `POST` takes `{winner_id, scope, detail, reason, source}`. `scope: "full"` sets `superseded_by` / `superseded_at` / `superseded_via`, and the admission gate then hides the record outside the `history` class; `scope: "partial"` appends `{winner_id, detail, at}` to `partially_superseded_by` and **never hides** (the gate does not read that key). The server enforces the refusal matrix of [`supersession.py`](../../mem0-server/supersession.py) whoever calls, with the codes listed in [api-contracts.md](../api-contracts.md): a canonical, insight or tier-less record, a retired record or winner, a superseded winner, another user's or another brand's winner and a different existing winner are all refused. Both records are read fail-closed and locked in sorted key order, an intent ledger line (`supersede-intent`) is written before the mutation and a `supersede` line after, the server stamps the actor itself (`supersede-endpoint`), and a repeated call is a no-op. `DELETE ?scope=full|partial|all&reason=` undoes it under the same tier rules (`unsupersede-intent` / `unsupersede` ledger lines).

```
Response 200 (POST): {"ok": true, "memory_id", "winner_id", "scope", "noop", "hidden"}
Response 200 (DELETE): {"ok": true, "memory_id", "scope", "noop"}
Response 400/403/404/409: "<code>: <message>" (bad-id, loser-canonical, winner-superseded, ...), see api-contracts.md
Response 503: the store could not be read or the intent ledger line could not be written; nothing was changed
```

The MCP tools are `memory_supersede` / `memory_unsupersede`; the operator modes are `contradiction-sweep.py --resolve-supersede`, `--unsupersede` and `--supersede-markers` ([reconciliation.md](./reconciliation.md)).

### `DELETE /v1/memories/{id}`

Delete a memory by ID. Canonical/insight deletes require an HMAC user-direct token (`mem0-canonize.sh --action delete`); the consolidator's `actor` label (query param) passes an insight record without one only with `X-AMS-Service-Key` (1.32.5), and a job label in `actor` without the header is `403 service-credential-required` on a record of any tier. The weekly decay-scan writes a ledger line with `event=decay-delete` when it removes an expired `temporal` record (`scripts/wsl/decay-scan.py`).

```
Response 200: mem0 delete result
```

### MCP tool wrappers

The shim (`scripts/wsl/mem0-mcp-shim.py`) exposes these tools to Claude Code:

- `memory_add(text, user_id, infer, metadata)` — POST /v1/memories (an `insight` tier request is always downgraded to `evidence` with a note since 1.32.5: no MCP session holds the service key)
- `memory_search(query, user_id, limit, threshold)` — POST /v1/memories/search
- `memory_list(user_id, limit)` — GET /v1/memories (limit hard-clamped at 500 client-side too)
- `memory_update(memory_id, text)` — PUT /v1/memories/{id} (text only; never append a `SUPERSEDED` marker, use `memory_supersede`)
- `memory_supersede(memory_id, superseded_by, scope, detail, reason)` — POST /v1/memories/{id}/supersede (1.32.4)
- `memory_unsupersede(memory_id, scope, reason)` — DELETE /v1/memories/{id}/supersede (1.32.4)
- `memory_promote(memory_id, tier, actor, reason)` — PATCH /v1/memories/{id}/tier (the shim always sends `actor="claude-autonomous"`: it cannot move a record into `canonical` or `insight`, nor out of either)
- `memory_demote(memory_id, tier, actor, reason)` — PATCH /v1/memories/{id}/tier (same endpoint, different direction; refused `400` without a reason, otherwise `403`, on a `canonical` or, since 1.32.5, an `insight` record: those leave their tier only through the signed `mem0-canonize.sh --action demote`, and a wrong insight is demoted that way or deleted with a signed `--action delete`, never with this tool)
- `memory_delete(memory_id)` — DELETE /v1/memories/{id}
- `memory_health()` — GET /health/deep (switched 2026-07-26: shallow /health green-lit broken write paths)

## Dependencies

- **Qdrant** on `:6333` (collection `mem0_egemma_768`, loopback).
- **llama-swap** on `:11436` — the EmbeddingGemma-300m embedder and the bge-reranker-v2-m3 cross-encoder.
- **mem0** (`mem0ai[nlp]`, floor 2.0.4; see `mem0-server/requirements.txt`) library.
- **The Codex HTTP shim** on `:18792` — used by the optional NLI write-gate (`codex_shim_client.py`) to judge contradictions against canonical.

## Downstream effects

Every route change ripples to the MCP shim (`mem0-mcp-shim.py`), the Windows hook clients that POST to `/v1/context/bundle` and `/v1/memories/search`, the dream consolidator (which posts insights, sending the service key, and calls the tier PATCH via `mem0-canonize.sh`), the nightly sweep and stamp jobs (which send it on their job-label writes), and the canonize CLI (`--action demote` now also moves a record out of insight). The `hook_contract_version` field lets the server WARN on hook/server wire drift without rejecting.

## Invariants and assumptions

- The server binds loopback-only (`127.0.0.1`); it is never exposed on `0.0.0.0`.
- The tier gates are server-side; a caller cannot self-elevate to `canonical`/`insight` regardless of the metadata it sends.
- An `actor` or `metadata.source` label is never a credential: the labels of the server's own jobs count only on a request that carries `X-AMS-Service-Key`, and a server without the key (every replica and PC) refuses all of them.
- A record leaves `canonical` or `insight` only through the operator's signed `demote` token; no job label, and no key the stack's jobs hold, exempts it.
- `infer=false` writes are hash-idempotent within a scope, so hooks re-firing on every Stop cannot re-insert duplicates.
- `limit` is clamped to 500 on both list and search.
- Callers cannot forge retrieval-gating metadata keys via `add` or the generic metadata PATCH.

## Error handling

| Code | Cause |
|---|---|
| `400` | empty memory; missing actor; missing reason for canonical; `tier` in metadata PATCH |
| `401` | missing/invalid `X-API-Key` |
| `403` | tier gate (canonical via add, insight source, canonical promote without user-direct/nonce, a move out of canonical or insight without the signed `demote` token); `service-credential-required` (a server-side job label in `actor` / `metadata.source` without `X-AMS-Service-Key`, on any write route and any tier) |
| `409` | `PATCH /tier`: the record became `canonical` or `insight` while an unsigned tier change was in flight (retry, or sign it); the supersede door has its own 409 codes (see [api-contracts.md](../api-contracts.md)) |
| `413` | payload exceeds `MAX_MEMORY_CHARS` |
| `422` | imperative text rejected from the canonical tier (imperative-canary) |
| `500` | Qdrant / llama-swap / mem0 backend error |
| `503` | canonical-canary could not verify the stored text (store unreachable); PUT pre-update payload read failed (fail-closed carry-over) |

## Security and privacy notes

- **Auth:** single `X-API-Key` (mode-600 file), constant-time compared; loopback bind is the network boundary.
- **Service credential (1.32.5):** the shared API key is held by every PC and MCP session, so a free-text label can no longer vouch for a server-side job; the `ams-service-key` header does (see Auth). What it separates: the authority's own jobs from every caller holding only the shared key (MCP shim sessions, hooks, PCs, replicas). What it does **not** resist: a shell on the authority as the service user, an ssh session to the brain, or (a WSL brain) Windows-side processes of the same user, any of which can read the key. It is one key for every job, not per-job least privilege; its separation from the canonical key means a job holding it cannot mint canonical tokens, and no replica or PC holds it. The codex judge child (`codex_shim_client.judge_env()`) has `CREDENTIALS_DIRECTORY` and the API-key pointers removed from its environment because it reads attacker-writable memory text; that drops the pointer, not the files, and the sandbox is the real boundary.
- **Known gaps (not closed by the service key, stated so nobody assumes they are):** writes that need no forged label and are no worse than the `DELETE` every key holder already has on `evidence`/`stable`/`temporal` records: `PATCH /tier` to `temporal` (hides a record from every query class), `_canonical_intent`, the supersede door on unprotected tiers, `retired_at` on non-canonical records. Caller-chosen `source` labels read by server-side jobs: autopromote's `user-decision` / `operator-decision` corroboration fast-track (its legitimate sender is an unprivileged PC hook, so no server credential can tell it from a forger) and semantic-dedup's `automemory:` protection; these need a different design. The ledger `transport` field is self-declared from which headers were present.
- **Canonical writes:** gated by an HMAC user-direct token (format-2, replay-protected via a burned nonce in `~/.mem0/canonical-replay.jsonl`); the signing key rests as a DPAPI blob where the per-box cutover has been run, else mode-600 plaintext — /health/deep reports which (see [`dpapi-canonical-key.md`](./dpapi-canonical-key.md)).
- **Metadata forgery:** retrieval-gating keys are stripped on `add` and forbidden on the generic metadata PATCH so an API-key holder cannot silently bury records; since 1.32.5 the per-actor exceptions (the sweep's and the lifecycle writers' keys) hold only for a request that proves the label with the service key.
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
- **Calling `/health` to verify write path** → misleading green. `/health/maintenance` `write_path` reports the outcome of the last real writes (passive, safe to poll); `/health/deep` or a test round-trip checks the path on demand, and loads the embedder to do it.
- **Expecting a substring match for the insight source** → the allowlist is exact (`c1-consolidator`, `dream-consolidator`, `c1-dream-consolidator`); `actor="not-c1"` no longer slips through.
- **Typing a job label to get a privilege** (`actor="contradiction-sweep-v019"`, `source="dream-consolidator"`, `actor="system"`) → `403 service-credential-required` since 1.32.5. The label is not the credential; the service key is, and only the authority's jobs hold it. Use your own label (`claude-autonomous`); a hand run of a sweep or stamp script on the native authority goes through `bash ~/apps/mem0-scripts/ams-service-run.sh <script> [args]`, not a hand-built request.
- **Walking a bad insight back with `memory_demote`** → `403` (`400` with no reason) since 1.32.5; an insight leaves its tier only with the operator's signed token: `bash mem0-canonize.sh --action demote <id> "<reason>" [--tier evidence|stable|temporal]`, or a signed `--action delete`.

## Source map

- [`../../mem0-server/app.py`](../../mem0-server/app.py) — the FastAPI app: all routes, auth, tier gates, hash idempotency, the ledger writer.
- [`../../mem0-server/config.py`](../../mem0-server/config.py) — mem0 config: embedder, Qdrant collection, ports.
- [`../../mem0-server/admission_gate.py`](../../mem0-server/admission_gate.py) — the read-side query-class admission policy.
- [`../../mem0-server/supersession.py`](../../mem0-server/supersession.py) — the supersede door's pure rules: the refusal matrix, the payload builders and the hand-written-marker parser (the routes are in `app.py`; the metadata key policy is `security_invariants.authorize_metadata_patch`).
- [`../../mem0-server/reranker.py`](../../mem0-server/reranker.py) — the bge-reranker cross-encoder client + skip thresholds.
- [`../../mem0-server/write_path.py`](../../mem0-server/write_path.py) — the passive write-path tracker and the middleware that feeds it (`/health/maintenance` `write_path`).
- [`../../mem0-server/security_invariants.py`](../../mem0-server/security_invariants.py) — the write-gate policy: the job-label tables, `require_service_credential` (the `X-AMS-Service-Key` check), `assert_writable`, `validate_insight_actor`, `authorize_metadata_patch`, `tier_change_hmac_action`.
- [`../../mem0-server/canonical_key_provider.py`](../../mem0-server/canonical_key_provider.py) — the key provider for the canonical key and (`service_key_provider()`, `service_key_health()`) the service key.
- [`../../scripts/wsl/ams-service-run.sh`](../../scripts/wsl/ams-service-run.sh) — the operator's wrapper for hand runs of the sweep / stamp / reclassify scripts with the service key on the authority.
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

## W5 additions (ADOPT-2/3, AMS-56)

- `POST /v1/memories/search`: `explain` flag (per-stage trace as `_explain.stages`), `rerank_status` (when rerank requested), withheld-family counters `rejected_superseded` / `rejected_contradicted` beside `rejected_brand_scoped`, and the keyword-union `lexical_only` result marker (see docs/flows/memory-retrieval.md).
- `POST /v1/memories/diagnose`: per-layer replay for one target; verdict names the first eating stage; read-only (pure admission evaluate).
- `POST /v1/context/bundle`: forwards the three withheld-family counters.
- Shim: `memory_diagnose` tool; `memory_search` adds `rerank_note` / `withheld_note`; `memory_recall` adds `withheld_note` + `age_summary` (bundle memories only).

## C10 addition (machine turns)

- `POST /v1/context/bundle`: a `prompt` that starts with `<task-notification>` (after leading whitespace; `hook_contract.is_machine_turn_prompt`) is a background task notification, not a human prompt. The episode checkpoint still runs, no search runs, and `memories` / `goals` / `open_questions` come back empty with `machine_turn: true`. The field is additive and absent on every other response.

## 1.32.4 additions (the supersede door)

- `POST` / `DELETE /v1/memories/{id}/supersede`: the only writer of `superseded_by` (and of `superseded_at`, `superseded_via`, `partially_superseded_by`); server-enforced refusal matrix, intent ledger line first, actor stamped by the server, repeat call a no-op.
- `PATCH /v1/memories/{id}/metadata`: no actor may write `superseded_by` any more (the `supersession-resolve-v030` entry is gone), and no actor may put a hide key on a canonical record; the key policy lives in `security_invariants.authorize_metadata_patch`. `add()` strips the three new keys too.
- `PUT /v1/memories/{id}`: `supersede_note` / `supersede_marker` when the new text holds a hand-written `SUPERSEDED ... by mem0 <id>` marker.
- Shim: `memory_supersede` and `memory_unsupersede` (offline-queued like the other writes; a 4xx refusal is never queued), `partial_supersession_note` on `memory_search` and `memory_recall`, `'history'` in the `memory_search` docstring. `replay-ops.py` replays both ops.
- Ledger events `supersede`, `supersede-intent`, `unsupersede`, `unsupersede-intent` (`scripts/wsl/ledger-audit.py`).

## 1.32.5 additions (the service credential)

- New request header `X-AMS-Service-Key` on `POST /v1/memories` (insight adds), `PUT`, `DELETE`, `PATCH /tier` and `PATCH /metadata`. A server-side job label (the stamp, legacy-lifecycle and consolidator labels) counts only with it; otherwise `403 service-credential-required: <field>=<label> is a server-side job label and ...`, before the tier gates and the signed-token check, and on a target of any tier.
- `PATCH /tier`: moving a record out of `insight` now needs the signed `demote` token, like canonical, with no job-label exemption (`mem0-canonize.sh --action demote`). The 409 for a change that raced a protected tier names `canonical` or `insight`.
- `/health/deep`: `checks.service_key` (`{present, source}`, informational) and the capability row `service-key` (required on the brain).
- Shim: `memory_add` always downgrades `tier=insight` to `evidence`; `memory_promote` / `memory_demote` can no longer move an insight in either direction.
