# The capture pipeline — how conversations become memory

## Purpose

Deep-dive on layer 1 of [`ARCHITECTURE.md`](../../ARCHITECTURE.md): every mechanism that *writes* memory, why each exists, and how each fails safe. The design bet across the whole layer: **capture with skepticism** — it is cheaper to drop a mediocre fact than to pollute a store the agent will trust later.

## Trigger

There is no single trigger — capture happens at four distinct moments, each hung off a different Claude Code hook or schedule:

| Moment | Trigger | What's captured | Code |
|---|---|---|---|
| Session end / compaction | `Stop` / `PreCompact` hooks | facts + the session episode + goals + open questions | `stop-extract.ps1` → `l1a-extract.ps1` |
| Every prompt | `UserPromptSubmit` | episode checkpoint + operator corrections | `mem0-hook-client.exe` → daemon → `user-prompt-lib.ps1` |
| Nightly 3am | Task Scheduler (WakeToRun) | insights + canonical promotions + hygiene | `dream-consolidate.ps1` |
| Compaction (query capture) | `PreCompact` (WSL side) | a redacted "what were we doing" query for the post-compact resume | `precompact_capture.py` |

## Participants

- **Claude Code hooks** — `Stop`, `PreCompact`, and `UserPromptSubmit` are the entry points that fire each capture.
- **The L1a extractor chain** — `stop-extract.ps1` (dispatcher) → `l1a-extract.ps1` (the detached session fact extractor), with shared helpers in `memory-common.ps1`.
- **The per-prompt hook client + resident daemon** — the compiled `mem0-hook-client` (`mem0-hook-client.cs`) → `mem0-hook-daemon.ps1` → `user-prompt-lib.ps1` / `user-prompt-extract.ps1`.
- **The dream consolidator** — `dream-consolidate.ps1`, run nightly by Windows Task Scheduler, with `dream-catchup.ps1` and `memory-index-refresh.ps1` as its resilience spawners.
- **The PreCompact WSL sidecar** — `precompact_capture.py`.
- **Codex CLI** (ChatGPT-subscription) — the LLM that performs extraction, consolidation, and nomination; one shared lock across all three.
- **The mem0 server** (`:18791`) — the REST authority every write targets; `redact.py` scrubs at its checkpoint chokepoint; `mem0-canonize.sh` is the HMAC door for canonical promotions.

## Step-by-step flow

### L1a — the session fact extractor

**Chain:** the hook dispatcher (`stop-extract.ps1`) reads the hook JSON from stdin, snapshots the transcript (PreCompact mutates it), and spawns `l1a-extract.ps1` **detached** — session close is never blocked. The extractor throttles to **one successful extraction per 10 min** (marked only on success, so a transient failure doesn't burn the window), drains the dead-letter queue, health-checks mem0, then reads the last **24 turns / 12 000 chars** of the transcript (last 512 KB only; any single record > 256 KB skipped — a pathological-giant-line guard).

**Redaction before the LLM.** `Redact-Secrets` strips credential shapes (keys, tokens, bearer headers, PEM blocks) *before* the text reaches Codex — and `redact.py` applies the same class of scrubbing server-side at the checkpoint chokepoint. Secrets are excluded at every entrance, not filtered after the fact.

**The inferability gate** is the extraction prompt's rule #1 and the reason the store stays high-signal. Verbatim core: *"before keeping a fact, ask: could a competent engineer who knows general software/tools but has NEVER worked on THIS project infer or guess this? If YES, DROP it."* Only genuinely project-specific facts survive — ports, paths, collection names, config values, decisions, IDs, flags, versions, locked-in choices. Generic best practices are noise by definition.

The other shape rules (all enforced in-prompt, all consequential downstream):

- **Fewer over quota** — max 5 facts, `[]` is a valid output.
- **Atomic** — one fact = one claim about one topic; multi-topic dumps must be split *before* output, because retrieval returns records individually.
- **Specific beats short** — ≤ 30 words preferred, 60 hard max, but never at the cost of the distinguishing value/name/path/number.
- **Verbatim tokens** — proper nouns, dates, numbers, paths, IDs, flags, versions unchanged.
- **Procedures as rules** — `IF <situation> THEN <action>`, which is the retrievable form of a lesson.
- **Durability filter** — drop pleasantries, hypotheticals, code blocks, one-off transient specifics.

**The ship-log split.** Facts are then partitioned (`Split-FactsByShipLog`): evergreen atomics POST to mem0 as `tier=evidence`; ship-log narratives ("shipped X, fixed Y, merged Z") fold into the **episode summary** instead. This single rule keeps release-note noise — the largest class of junk a coding agent generates — out of semantic memory while preserving it as history.

**Beyond facts**, the same Codex call extracts the episode (goal: 1–2 sentences; summary: 2–4), **0–3 advanced goals** (with a one-sentence delta), **0–2 blocked goals** (with the blocker), and **0–5 open questions** — declarative uncertainties raised but unanswered, "NOT idle wondering". These feed the prospective-memory surfaces (session banners, the bundle).

### Per-prompt capture

The compiled `UserPromptSubmit` client (exec-form hook entry — a Windows concurrent-spawn race killed the shell form; see the v1.11.0 notes) checkpoints an **in-progress episode** on every prompt (a partial unique index guarantees at most one per session) and runs `Test-CorrectionLikePrompt`: prompts shaped like operator corrections append durably to `~/.mem0/learn-rules.jsonl` **the moment they happen**. Rationale: correction is the highest-value, lowest-frequency signal the operator emits; making it wait for the nightly cycle risked losing it to a crash or a missed dream.

### The dream — nightly consolidation

`dream-consolidate.ps1`, 3:00 am via Task Scheduler (`-WakeToRun`), 24 h-throttled, holding the shared Codex lock (extractor / dream / judgment shim never run Codex concurrently — one ChatGPT subscription). Four phases, each leaving an audit artifact:

1. **Orient** — load state, decide whether there is anything worth consolidating.
2. **Gather (surprise-weighted)** — read the last 36 h of transcripts (≤ 10 files) and weight signals by information gain: corrections, decisions, surprises, contradictions rank far above routine progress.
3. **Consolidate** — Codex (medium effort) synthesizes **1–3 lineage-tracked insights** (`tier=insight`, source-evidence links kept). Zero insights is a normal outcome on an uneventful day — re-running on unchanged evidence would only produce near-duplicates.
4. **Prune + index** — rebuild the semantic index; run the **retrieval-drift canary** (a zero-Codex before/after snapshot of canary-fact retrievability; a benign consolidation must not change what's findable) and brand-scope audits.

**Autonomous canonical promotion** (phase 3.5): Codex nominates evidence that reads as evergreen, declarative, ground-truth-grade, cross-session, high-confidence — precision over recall. The pipeline then applies *structural* controls the LLM can't override: nominees are **sorted by confidence, capped at 3/night** (over-cap deferred to the next night), deduped against the existing canonical set, and pushed through the **4C contradiction/corroboration gate** (shadow-calibration by default; in enforce mode a blocked nominee stays evidence). The surviving few are promoted via the same HMAC door the operator uses (`mem0-canonize.sh --actor dream-autopromote`), so even autonomous promotion is cryptographically uniform and ledgered.

### PreCompact query capture

A tiny WSL sidecar (`precompact_capture.py`) with one job: when the context is about to compact, tail the transcript's last 256 KB, distill the last 6 turns into a **redacted ≤ 800-char query**, and atomically write a marker file. The very next SessionStart consumes it to fetch a *conversation-relevant* memory bundle (K = 2) — so the resume after compaction knows what you were doing, not just what you did recently. Fail-silent, dependency-free.

## Data and state changes

| Write / derived state | When | Where |
|---|---|---|
| Evergreen atomic facts | L1a extraction | mem0 `tier=evidence` (POST) |
| Episode summary + advanced/blocked goals + open questions | L1a extraction | mem0 episodic / goals / open-questions collections |
| In-progress episode checkpoint | every prompt | mem0 (partial-unique-indexed, at most one per session) |
| Operator corrections | correction-like prompt | `~/.mem0/learn-rules.jsonl` (append-only) |
| 1–3 insights + ≤ 3 canonical promotions/night | nightly dream | mem0 `tier=insight`; canonical via the HMAC-signed ledger |
| Rebuilt memory index | nightly, plus a decoupled 6 h refresh | `~/.mem0/MEMORY.md` |
| PreCompact resume query | PreCompact | `precompact-query.json` marker, consumed at the next SessionStart |
| Failed POSTs | on write failure | DLQ `~/.claude/state/mem0-post-failures.jsonl`; poison → `mem0-post-poison.jsonl` |
| Throttle markers | after each successful run | `~/.claude/state/` (`l1a`, `dream`, `dream-catchup`, `index-refresh`) |

## Success behavior

When the pipeline succeeds: only genuinely project-specific facts reach mem0 as `tier=evidence` (max 5 per extraction, atomic, secret-free); the session episode, advanced/blocked goals, and open questions are recorded for the prospective-memory surfaces; ship-log narratives are folded into history rather than semantic memory; operator corrections are captured the moment they happen; and — on an eventful night — 1–3 lineage-tracked insights and at most 3 gate-checked canonical promotions are added. Every hook exits 0 regardless, so the operator is never blocked and session close is never delayed.

## Failure behavior

**L1a writes** — a failed mem0 POST dead-letters to `~/.claude/state/mem0-post-failures.jsonl` and retries on the next run — poison codes (413/401/422) quarantine immediately, everything else retries up to 5 attempts, ship-logs are dropped on drain by policy. Atomicity is enforced at write time in two rings: the extractor splits any fact over ~700 chars at sentence boundaries *before* POSTing (the MEM-10 guard), and the server rejects anything still oversize (`413` at `MAX_MEMORY_CHARS = 4000`); the prompt's atomicity rules exist so neither fires often. A connection-level failure (`status_code == 0`) deliberately does **not** count toward the 5-attempt quarantine cap, so a multi-day offline stretch never quarantines good writes.

**Dream (missed-run recovery)** — a SessionStart spawner (`dream-catchup.ps1`) re-runs a missed dream (fresh < 30 h → skip; pending learn-rules/promote-queue or > 48 h gap → run; its own 6 h throttle; the dream's internal 24 h throttle + Codex lock make double-runs impossible). The MEMORY.md index refresh is decoupled (`memory-index-refresh.ps1`, 6 h throttle) so a down dream cannot freeze the index. `dream-consolidate.ps1 -Force` bypasses the throttle for a manual run.

**Per-prompt and PreCompact** — both are fail-open by contract: the per-prompt hook always exits 0 and never blocks the prompt, and `precompact_capture.py` is fail-silent and dependency-free, so a capture miss degrades the next resume rather than breaking the session.

## External dependencies

- **Codex CLI** (gpt-5.5, ChatGPT-subscription OAuth) — the extraction / consolidation / nomination LLM; one shared Codex lock serializes all three.
- **mem0 REST server** on `:18791` — the write authority; `mem0-canonize.sh` for the HMAC-signed canonical promotion.
- **Windows Task Scheduler** — hosts the nightly dream (`-WakeToRun`).
- **WSL2** — hosts `precompact_capture.py`, the mem0 server, and the canonize CLI.
- **EmbeddingGemma + Qdrant** (via mem0) — index the written records so they are retrievable.

## Invariants and assumptions

1. **Skeptical by default** — inferability gate, atomicity, ship-log split: the store's value is precision, not volume.
2. **Never block the operator** — every capture is detached/throttled/fail-open; hooks always exit 0.
3. **Secrets never enter** — redaction at both reader and server chokepoints.
4. **Autonomy is structurally bounded** — the dream can act alone, but only through confidence sort + cap 3 + dedup + 4C gate + HMAC + ledger.
5. **Everything self-heals** — DLQ retries, dream catch-up, decoupled indexing, poison quarantine.

## Security and privacy notes

Secrets are excluded at every entrance, not filtered after the fact: `Redact-Secrets` scrubs credential shapes before the transcript reaches Codex, and `redact.py` re-scrubs server-side at the checkpoint chokepoint (see the L1a step above). Autonomous canonical promotion does **not** bypass the canonical write gate — it signs the same HMAC token via `mem0-canonize.sh --actor dream-autopromote`, so an attacker who could only run the consolidator still cannot forge canonical without the DPAPI-held key. All capture state lives under `~/.mem0/` (WSL) and `~/.claude/state/` (Windows); nothing here opens a LAN listener, and the shipped scripts carry no real host or operator value.

## Observability and debugging

- The component logs (via `Write-MemoryLog`, e.g. `l1a.log`) record extraction decisions, throttle skips, and DLQ quarantine events at WARN.
- The DLQ (`mem0-post-failures.jsonl`) and poison quarantine (`mem0-post-poison.jsonl`) are the durable record of failed writes and why they were dropped.
- The dream's morning summary lists what was promoted, gate-blocked, deduped, or over-cap; `~/.claude/logs/codex-usage.jsonl` tracks Codex token/duration/status per call.
- Throttle markers under `~/.claude/state/` (`dream-last-run`, etc.) show when each stage last ran successfully.

## Testing notes

- [`../../scripts/windows/tests/DrainDeadLetter.Tests.ps1`](../../scripts/windows/tests/DrainDeadLetter.Tests.ps1) — DLQ drain, poison-code quarantine, and the attempt cap.
- [`../../scripts/windows/tests/MemoryCommon.Tests.ps1`](../../scripts/windows/tests/MemoryCommon.Tests.ps1) — the ship-log split, redaction, and transcript-windowing helpers.
- [`../../scripts/windows/tests/UserPromptExtract.Tests.ps1`](../../scripts/windows/tests/UserPromptExtract.Tests.ps1) — correction detection and per-prompt admission.
- [`../../scripts/windows/tests/DreamAutopromote.Tests.ps1`](../../scripts/windows/tests/DreamAutopromote.Tests.ps1), [`../../scripts/windows/tests/DreamGateVerdict.Tests.ps1`](../../scripts/windows/tests/DreamGateVerdict.Tests.ps1), [`../../scripts/windows/tests/DreamCatchup.Tests.ps1`](../../scripts/windows/tests/DreamCatchup.Tests.ps1) — the nomination pipeline, the 4C gate verdict, and the catch-up debt logic.
- [`../../claude-config/tests/test_precompact_capture.py`](../../claude-config/tests/test_precompact_capture.py) — the PreCompact query capture.
- [`../../mem0-server/tests/test_redact.py`](../../mem0-server/tests/test_redact.py) — server-side redaction.

## Source map

- [`../../scripts/windows/stop-extract.ps1`](../../scripts/windows/stop-extract.ps1) — the Stop/PreCompact dispatcher that snapshots the transcript and spawns the extractor detached.
- [`../../scripts/windows/l1a-extract.ps1`](../../scripts/windows/l1a-extract.ps1) — the session fact extractor (throttle, DLQ drain, transcript window, inferability-gated Codex extraction, MEM-10 split).
- [`../../scripts/windows/memory-common.ps1`](../../scripts/windows/memory-common.ps1) — shared helpers: `Split-FactsByShipLog`, `Redact-Secrets`, transcript windowing, the DLQ drain, the Codex lock/throttle.
- [`../../scripts/windows/mem0-hook-client.cs`](../../scripts/windows/mem0-hook-client.cs) — the compiled `UserPromptSubmit` client (exec-form entry).
- [`../../scripts/windows/mem0-hook-daemon.ps1`](../../scripts/windows/mem0-hook-daemon.ps1) — the resident daemon behind the client.
- [`../../scripts/windows/user-prompt-lib.ps1`](../../scripts/windows/user-prompt-lib.ps1) — `Test-CorrectionLikePrompt` and the learn-rules append; also the per-prompt admission library.
- [`../../scripts/windows/user-prompt-extract.ps1`](../../scripts/windows/user-prompt-extract.ps1) — the per-prompt checkpoint + correction path.
- [`../../scripts/windows/dream-consolidate.ps1`](../../scripts/windows/dream-consolidate.ps1) — the nightly consolidator (all phases + autopromote + drift canary).
- [`../../scripts/windows/autopromote-lib.ps1`](../../scripts/windows/autopromote-lib.ps1) — the 4C promotion gate + nomination decision logic.
- [`../../scripts/windows/dream-catchup.ps1`](../../scripts/windows/dream-catchup.ps1) — the missed-run catch-up spawner.
- [`../../scripts/windows/memory-index-refresh.ps1`](../../scripts/windows/memory-index-refresh.ps1) — the decoupled `MEMORY.md` refresh.
- [`../../claude-config/precompact_capture.py`](../../claude-config/precompact_capture.py) — the PreCompact query-capture sidecar.
- [`../../scripts/wsl/mem0-canonize.sh`](../../scripts/wsl/mem0-canonize.sh) — the HMAC-signed canonical door the dream autopromotion uses.
- [`../../mem0-server/redact.py`](../../mem0-server/redact.py) — server-side secret scrubbing at the checkpoint chokepoint.

## Related docs

- [`../systems/codex-hooks.md`](../systems/codex-hooks.md) — the shared Codex mutex and the hook wiring behind extraction.
- [`../systems/dream-skill.md`](../systems/dream-skill.md) — the nightly consolidation in depth (phases, 4C gate, autopromotion).
- [`../systems/memory-model.md`](../systems/memory-model.md) — the tiers and the promotion lifecycle these writes feed.
- [`../systems/mem0-api.md`](../systems/mem0-api.md) — the REST write surface and the tier PATCH gate.
- [`../systems/episodic.md`](../systems/episodic.md) — the episode summaries captured here.
- [`../systems/admission-gate.md`](../systems/admission-gate.md) — the server-side write admission.
- [`../systems/offline-travel.md`](../systems/offline-travel.md) — how these writes queue when the brain is unreachable.
- [`./memory-retrieval.md`](./memory-retrieval.md) — the read side that consumes what this captures.
- [`../glossary.md`](../glossary.md) · [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)
