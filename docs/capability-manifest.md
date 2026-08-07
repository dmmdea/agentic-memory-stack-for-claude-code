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
  shipping its row with a real probe. `probe: none — W4` rows are named admissions of blindness —
  they always evaluate `unknown` and form the W4 revive-or-bury worklist; a silent omission is the
  defect this manifest exists to prevent.

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
| `put-carryover` | PUT payload carry-over (metadata survives text rewrites) | checks.put_carryover_today (daily activity counters) | both | F17 |
| `mojibake-tripwire` | CP437 corpus-encoding tripwire | checks.mojibake (payload scan) | both | F17 |
| `contradiction-review-queue` | human review queue for contradiction verdicts is watched | checks.pending_contradiction_reviews (queue depth) | brain | F17 |
| `dream-cycle` | nightly dream consolidation runs | job_liveness.last_dream_age_h (throttle marker content) | brain | F17 |
| `drift-guard` | retrieval-drift guard compares around each consolidation | retrieval_drift (~/.mem0/retrieval-drift-state.json) | brain | F17 |
| `backup-pipeline` | daily stack snapshot + manifest writer | job_liveness.backup_manifest_age_h (newest manifest age) | brain | F17 |
| `dedup-job` | daily semantic dedup sweep | job_liveness.dedup_report_age_h (report rewritten every run) | brain | F17 |
| `memory-index` | dream gather step (memory index refresh) | job_liveness.gather_age_h (gather.json receipt age) | brain | F17 |
| `sweep-job` | dream prune step (consolidation-completed receipt) | job_liveness.prune_age_h (prune.json receipt age) | brain | F17 |
| `codex-auth` | Codex CLI auth serving dream judgment | derived: a fresh dream ran, therefore its judge authenticated | brain | F17 |
| `reranker` | cross-encoder rerank leg on search | none — W4 | optional | — |
| `l1a-extraction` | hook-fired L1a memory extractor | none — W4 | brain | — |
| `sessionstart-banner` | SessionStart resume/context banner | none — W4 | both | — |
| `mcp-shim` | MCP shim bridging the CLI to the memory API | none — W4 | both | — |
| `admission-gate` | server-side retrieval admission gate | none — W4 | both | — |
| `tier-policy` | tier enforcement (canonical HMAC, insight allowlist) | none — W4 | both | — |
| `brand-isolation` | brand-scoped retrieval isolation | none — W4 | both | — |
| `offline-outbox` | shim outbox queue/replay when the authority is unreachable | none — W4 | replica | — |

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
