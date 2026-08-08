# Capability manifest

The stack accumulated capabilities faster than it accumulated mouths: a capability could die (a
scheduled task unregistered, a queue unwatched, an auth token expired) and nothing structural would
ever say so. The manifest is the single answer sheet — every named capability, what proves it
alive, and one honest per-capability verdict on `/health/deep`.

## Mechanics

- The `CAPABILITIES` literal in `mem0-server/capabilities.py` is the **single source of truth**.
  This table is its human mirror; `mem0-server/tests/test_capabilities.py` pins the two together
  (the ids below must equal the literal's ids, in order), so the doc cannot drift silently.
- `GET /health/deep` carries the verdicts as `checks.capabilities` — `evaluate()` folds the
  endpoint's own checks dict into `{role, states, dead_required, unknown, evaluated_at}`, where
  each state is `alive`, `degraded`, `dead`, `unknown`, or `retired`. Shapes: see
  [systems/mem0-api.md](systems/mem0-api.md).
- The verifier (`Test-MemoryStack`, check L10) **FAILs** when `dead_required` is non-empty — a dead
  required capability for this box's role is an operator-actionable outage. `unknown` rows surface
  as WARN-class visibility, never FAIL.
- **Detection latency is one session/night by design — do not add a watchdog.** The manifest is
  evaluated when `/health/deep` is read (a session banner, the nightly dream heartbeat, an operator
  run of the verifier); no unattended scheduler polls it.
- **§3 rule: a capability with no probe row cannot be marked done.** Shipping a capability means
  shipping its row with a real probe. A `probe: none — ...` row is a named admission of blindness —
  it always evaluates `unknown`; a silent omission is the defect this manifest exists to prevent.
  **As of W4 there are none left**, and `test_capabilities.py` fails if one reappears.

Two honesty rules govern the verdicts (bound review contract):

- **F9 — zero-signal is never `alive`.** Activity counters (e.g. `put_carryover_today` with
  `puts: 0`) prove liveness only when they counted something; a zero on a quiet day is silence,
  not health, and maps to `unknown`. Queue *depths* are different: an empty review queue is itself
  the good outcome.
- **F8 — an unknown role cannot convict.** `brain`/`replica` rows enter `dead_required` only when
  the box's role is known and matches; `both` rows count on every box; `optional` rows never enter
  `dead_required`.

## Rows

The **escalation** column marks the F17 rows: their source check is *informational* on
`/health/deep` (it never flips the endpoint's `ok`), so deadness escalates through
`dead_required` here — the verifier's L10 FAIL — not through the deploy gate.

| id | capability | probe | required | escalation |
|---|---|---|---|---|
| `mem0-api` | REST memory API (the /v1 surface the MCP shim and hooks call) | /health/deep responded (this evaluation ran inside it) | both | — |
| `qdrant-store` | vector store holding the memory corpus | checks.qdrant (collection status + point count) | both | — |
| `embedder` | dense embedder (768-dim round-trip) | checks.embedder | both | — |
| `bm25-sparse-leg` | lexical BM25 leg of hybrid retrieval | checks.sparse_leg (deterministic oldest-point canary) | both | — |
| `canonical-key` | HMAC canonical-key chain (runtime/DPAPI/plaintext provider) | checks.canonical_key | brain | — |
| `put-carryover` | PUT payload carry-over (metadata survives text rewrites) | checks.put_carryover_today (daily activity counters); unknown-on-idle by design (F9) — exerciser: Test-MemoryStack I13 PUT canary | both | F17 |
| `mojibake-tripwire` | CP437 corpus-encoding tripwire | checks.mojibake (payload scan) | both | F17 |
| `contradiction-review-queue` | human review queue for contradiction verdicts is watched | checks.pending_contradiction_reviews (queue depth) | brain | F17 |
| `dream-cycle` | nightly dream consolidation runs | job_liveness.last_dream_age_h (throttle marker content) | brain | F17 |
| `drift-guard` | retrieval-drift guard compares around each consolidation | retrieval_drift (~/.mem0/retrieval-drift-state.json) | brain | F17 |
| `backup-pipeline` | daily stack snapshot + manifest writer | job_liveness.backup_manifest_age_h (newest manifest age) | brain | F17 |
| `dedup-job` | daily semantic dedup sweep | job_liveness.dedup_report_age_h (report rewritten every run) | brain | F17 |
| `memory-index` | dream gather step (memory index refresh) | job_liveness.gather_age_h (gather.json receipt age) | brain | F17 |
| `sweep-job` | dream prune step (consolidation-completed receipt) | job_liveness.prune_age_h (prune.json receipt age) | brain | F17 |
| `codex-auth` | Codex CLI auth serving dream judgment | derived: a fresh dream ran, therefore its judge authenticated | brain | F17 |
| `reranker` | cross-encoder rerank leg on search | checks.reranker (PASSIVE counters bumped by real search reranks; no active probe here by F11 — active 3-doc probe is Test-MemoryStack L5) | optional | F17 |
| `l1a-extraction` | hook-fired L1a memory extractor | job_liveness.l1a_attempt_age_h vs l1a_success_age_h (two-signal, F12: convicts only attempt-fresh + success-stale >96h; capped at degraded) | brain | F17 |
| `sessionstart-banner` | SessionStart resume/context banner | job_liveness.sessionstart_banner_age_h (unconditional stamp at the top of storage-cap-check.sh) cross-checked against l1a_attempt_age_h | both | F17 |
| `mcp-shim` | MCP shim bridging the CLI to the memory API | job_liveness.mcp_shim_receipt_age_h + mcp_shim_host_match + mcp_shim_stack_version (host-keyed start receipt; version skew = stale launch path) | brain | F17 |
| `admission-gate` | server-side retrieval admission gate | checks.admission_probe (in-process AdmissionPolicy.evaluate() self-probe, zero I/O) | both | F17 |
| `tier-policy` | tier enforcement (canonical HMAC, insight allowlist) | checks.admission_probe.tier_rejected — read-half server-probed; write-half proven by Test-MemoryStack I-rows on demand | both | F17 |
| `brand-isolation` | brand-scoped retrieval isolation | checks.admission_probe.brand_rejected (read half) + job_liveness.brand_scope_misscoped/brand_scope_age_h (nightly write-side audit) | both | F17 |
| `offline-outbox` | shim outbox queue/replay when the authority is unreachable | job_liveness.outbox_depth + outbox_replayed_age_h + outbox_drain_log_age_h — evaluated on the REPLICA only (F8 keeps it non-convicting on a brain box) | replica | F17 |
| `job-queue` | durable two-phase job queue (jobs.py): claim/receipt/reap for adopted scheduled jobs | job_liveness.jobs_heartbeat_age_h (age-gates the mirror) + jobs_failed_24h + jobs_oldest_running_age_h + jobs_oldest_queued_age_h | optional | W6 |

Verdict rules per probe family (all thresholds live in `capabilities.py`):

- **Nightly receipts** (`dream-cycle`, `backup-pipeline`, `dedup-job`, `memory-index`,
  `sweep-job`): age ≤ 48h → `alive` (one missed night tolerated); ≤ 96h → `degraded`; older →
  `dead`; no signal → `unknown`.
- **`bm25-sparse-leg`**: `sparse_leg.ok: false` → `dead`; alive but coverage < 0.95 → `degraded`
  (a half-backfilled corpus is degraded, not dead).
- **`drift-guard`**: `state_present: false` → `unknown` (the guard may simply not be deployed);
  ≥ 2 consecutive snapshot failures or a > 96h-stale compare → `dead`; a standing alarm or
  compat-fallback → `degraded`.
- **`codex-auth`**: derived only — a fresh dream proves its judge authenticated (`alive`);
  anything else is `unknown`, because a stale dream proves nothing about the token either way.
- **`put-carryover`**: unknown-on-idle is the DESIGNED F9 semantics, not a gap — a day with no PUTs
  proves nothing. The named exerciser is `Test-MemoryStack`'s **I13 PUT-survival canary**: run the
  verifier and the counters move, then the row reports `alive`.

### W4 rows (the eight that were `probe: none` until 2026-08-07)

- **`reranker`** — **passive only, and that is a hard rule (review F11).** The reranker is a CPU
  cross-encoder that the verifier budgets 90s, and `scripts/wsl/deploy.sh` gates on `/health/deep`
  seconds after a restart. An *active* rerank probe on that endpoint would hang deploys on a cold
  model, so `reranker.py` instead exposes counters bumped by **real** search traffic
  (`last_rerank_ok_ts`, `consecutive_rerank_failures`, in-process, zero I/O). Verdict: ≥ 3
  consecutive failures → `dead`; any failures, or failures with no success since boot →
  `degraded`; a success inside 48h with no failures → `alive`; **no traffic at all, or a success
  older than 48h → `unknown`** (the counters reset on restart, so silence is silence). The row is
  `optional`, so it never enters `dead_required`. The active 3-doc probe stays at verifier **L5**,
  which echoes the passive counters beside its own result.
- **`l1a-extraction`** — **two-signal (review F12).** `~/.claude/state/last-l1a` stamps only on a
  *successful* extraction, and `last-sessionstart-capture` is not written on five early-exit paths,
  so the obvious rule ("success stamp stale ⇒ dead") convicted the commonest healthy state:
  sessions ran and had nothing durable to extract. `l1a-extract.ps1` now also writes
  `last-l1a-attempt` **unconditionally at entry**, before every early return. Verdict: success
  inside 48h → `alive`; attempts fresh **and** success stale by more than **96h** → `degraded`
  (**never `dead`** — the cap is deliberate); anything else, including "never succeeded here" and
  "attempts stopped too", → `unknown`. An idle box can therefore never be convicted.
- **`sessionstart-banner`** — same two-signal shape. `claude-config/storage-cap-check.sh` writes
  `~/.mem0/last-sessionstart-banner` on its **first line**, above the cold-server gate, so the
  stamp proves the hook fired even on a morning when every server-dependent section was skipped.
  Verdict: stamp inside 48h → `alive`; stamp missing/stale **while `l1a_attempt_age_h` shows
  sessions are still happening** → `degraded`; no corroborating session signal → `unknown`.
- **`mcp-shim`** — **host-keyed, `brain`-scoped (review F13).** The shim writes
  `~/.mem0/mcp-shim-receipt.json` in its `__main__`, before `mcp.run` — to a *file*, never to
  stdout, which is the MCP JSON-RPC channel. The receipt carries `host` (because `~/.mem0` travels
  in stack backups: a restored receipt from another box is treated as absent) and
  `stack_version` (a pinned literal in the shim). The row is scoped to `brain` because a replica's
  shim writes to the *replica's* `~/.mem0`, whose server may not be running to read it. Verdict:
  fresh own-host receipt whose version matches the running server → `alive`; **version skew →
  `degraded`** (the AMS-02 class: the Windows `.claude\scripts` launch path is refreshed only by
  `install/2-windows-config.ps1`, never by `deploy.sh`); absent/stale/foreign-host receipt while
  sessions are running → `degraded`; otherwise `unknown`. Never `dead` — an operator who has not
  opened a session is idle, not broken. *A version bump reads `degraded` until the next session,
  because the receipt was written by the shim that was launched before the deploy — which is the
  literal truth about what is running.*
- **`admission-gate`, `tier-policy`, `brand-isolation`** — one **pure in-process self-probe**
  (`capabilities.admission_selfprobe`) runs three synthetic records through
  `AdmissionPolicy.evaluate()`: a `tier=canonical` record must be rejected by the durable class, a
  *branded* record on a *brandless* scope must be rejected (the v0.19 M4 fail-closed rule), and a
  brand-neutral `evidence` record must be admitted (the negative control — a gate that rejected
  everything would otherwise pass the first two). **It must never call `apply_admission`**: that
  bumps the MEM-8 daily rejection counters this same endpoint reports and appends to
  `~/.mem0/admission-rejected.jsonl`, so probing through it would corrupt the starvation metrics
  and write audit lines on every health read — including the deploy gate's and the session
  banner's. Verdict: all three assertions hold → `alive`; the relevant assertion fails → `dead`
  (these rows are `both`-required, so that is a verifier FAIL, correctly); the probe could not run
  → `unknown`. `brand-isolation` additionally reads `~/.mem0/brand-scope-status.json` as a
  *write-side* corroborator that can only **degrade**: `n_misscoped > 0` → `degraded` (canonical
  facts invisible to their own brand), audit older than 96h → `degraded`, audit absent → no effect
  (the nightly job may not be deployed on this box). `tier-policy` covers the **read half only** —
  the write half (403 on `POST` with `tier=canonical`, HMAC-signed `PATCH /tier`) has no cheap
  in-process probe and is exercised by the verifier's I-rows on demand; the probe string says so
  rather than implying full coverage.
- **`offline-outbox`** — **evaluated on the replica only.** The code ships now; the live verdict
  needs the replica box. Verdict from `outbox_depth` + the replay ledger + the drain log: queued
  writes with a drain inside 48h → `degraded` (working through it); queued writes with **nothing
  draining** → `dead`; empty queue **and** a replay inside 30 days → `alive` (only a real replay
  proves the mechanism); empty queue with no replay ever → `unknown` (F9 — an empty outbox on a box
  that never went offline is silence, not health). `required: replica`, so on a brain box F8 keeps
  it out of `dead_required` whatever it says.

## job-queue

**What:** the W6 durable two-phase job queue (`scripts/wsl/jobs.py`): adopted scheduled jobs run
through claim → execute → keyed-receipt observation → done/failed, with per-name stale-claim reap
(same-host reap requires the recorded pid DEAD and the claim past its stale_after; foreign-host
rows reap on age alone — the owner is host-keyed so a restored `jobs.db` can never let this box
requeue another box's live work).

**Probe:** `job_liveness.jobs_heartbeat_age_h` + `jobs_failed_24h` + `jobs_oldest_running_age_h`
+ `jobs_oldest_queued_age_h`, all read from the file mirror `~/.mem0/jobs-heartbeat.json` that
every `jobs.py` invocation refreshes — the sqlite db itself is never opened on the health path
(the deploy-gate budget rule), and the SessionStart banner reads the same mirror as a plain file.

**Verdict:** no mirror at all → `unknown` (no adopter has run on this box; an idle box must never
read dead); mirror older than 8 days (past the weekly cadence) → `degraded` — a dead queue must
not present a stale mirror as current (the designed-but-dead class); `jobs_failed_24h > 0` →
`degraded`; otherwise `alive`. `required: optional` — adoption is per-job and staged.
