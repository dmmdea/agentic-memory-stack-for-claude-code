# The capture pipeline — how conversations become memory

Deep-dive on layer 1 of [`ARCHITECTURE.md`](../../ARCHITECTURE.md): every mechanism that *writes* memory, why each exists, and how each fails safe. The design bet across the whole layer: **capture with skepticism** — it is cheaper to drop a mediocre fact than to pollute a store the agent will trust later.

## The four capture moments

| Moment | Trigger | What's captured | Code |
|---|---|---|---|
| Session end / compaction | `Stop` / `PreCompact` hooks | facts + the session episode + goals + open questions | `stop-extract.ps1` → `l1a-extract.ps1` |
| Every prompt | `UserPromptSubmit` | episode checkpoint + operator corrections | `mem0-hook-client.exe` → daemon → `user-prompt-lib.ps1` |
| Nightly 3am | Task Scheduler (WakeToRun) | insights + canonical promotions + hygiene | `dream-consolidate.ps1` |
| Compaction (query capture) | `PreCompact` (WSL side) | a redacted "what were we doing" query for the post-compact resume | `precompact_capture.py` |

## L1a — the session fact extractor

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

**Failure handling:** a failed mem0 POST dead-letters to `~/.claude/state/mem0-post-failures.jsonl` and retries on the next run — poison codes (413/401/422) quarantine immediately, everything else retries up to 5 attempts, ship-logs are dropped on drain by policy. Atomicity is enforced at write time in two rings: the extractor splits any fact over ~700 chars at sentence boundaries *before* POSTing (the MEM-10 guard), and the server rejects anything still oversize (`413` at `MAX_MEMORY_CHARS = 4000`); the prompt's atomicity rules exist so neither fires often.

## Per-prompt capture

The compiled `UserPromptSubmit` client (exec-form hook entry — a Windows concurrent-spawn race killed the shell form; see the v1.11.0 notes) checkpoints an **in-progress episode** on every prompt (a partial unique index guarantees at most one per session) and runs `Test-CorrectionLikePrompt`: prompts shaped like operator corrections append durably to `~/.mem0/learn-rules.jsonl` **the moment they happen**. Rationale: correction is the highest-value, lowest-frequency signal the operator emits; making it wait for the nightly cycle risked losing it to a crash or a missed dream.

## The dream — nightly consolidation

`dream-consolidate.ps1`, 3:00 am via Task Scheduler (`-WakeToRun`), 24 h-throttled, holding the shared Codex lock (extractor / dream / judgment shim never run Codex concurrently — one ChatGPT subscription). Four phases, each leaving an audit artifact:

1. **Orient** — load state, decide whether there is anything worth consolidating.
2. **Gather (surprise-weighted)** — read the last 36 h of transcripts (≤ 10 files) and weight signals by information gain: corrections, decisions, surprises, contradictions rank far above routine progress.
3. **Consolidate** — Codex (medium effort) synthesizes **1–3 lineage-tracked insights** (`tier=insight`, source-evidence links kept). Zero insights is a normal outcome on an uneventful day — re-running on unchanged evidence would only produce near-duplicates.
4. **Prune + index** — rebuild the semantic index; run the **retrieval-drift canary** (a zero-Codex before/after snapshot of canary-fact retrievability; a benign consolidation must not change what's findable) and brand-scope audits.

**Autonomous canonical promotion** (phase 3.5): Codex nominates evidence that reads as evergreen, declarative, ground-truth-grade, cross-session, high-confidence — precision over recall. The pipeline then applies *structural* controls the LLM can't override: nominees are **sorted by confidence, capped at 3/night** (over-cap deferred to the next night), deduped against the existing canonical set, and pushed through the **4C contradiction/corroboration gate** (shadow-calibration by default; in enforce mode a blocked nominee stays evidence). The surviving few are promoted via the same HMAC door the operator uses (`mem0-canonize.sh --actor dream-autopromote`), so even autonomous promotion is cryptographically uniform and ledgered.

**Robustness:** a SessionStart spawner (`dream-catchup.ps1`) re-runs a missed dream (fresh < 30 h → skip; pending learn-rules/promote-queue or > 48 h gap → run; its own 6 h throttle; the dream's internal 24 h throttle + Codex lock make double-runs impossible). The MEMORY.md index refresh is decoupled (`memory-index-refresh.ps1`, 6 h throttle) so a down dream cannot freeze the index. `dream-consolidate.ps1 -Force` bypasses the throttle for a manual run.

## PreCompact query capture

A tiny WSL sidecar (`precompact_capture.py`) with one job: when the context is about to compact, tail the transcript's last 256 KB, distill the last 6 turns into a **redacted ≤ 800-char query**, and atomically write a marker file. The very next SessionStart consumes it to fetch a *conversation-relevant* memory bundle (K = 2) — so the resume after compaction knows what you were doing, not just what you did recently. Fail-silent, dependency-free.

## Design principles of the layer (summary)

1. **Skeptical by default** — inferability gate, atomicity, ship-log split: the store's value is precision, not volume.
2. **Never block the operator** — every capture is detached/throttled/fail-open; hooks always exit 0.
3. **Secrets never enter** — redaction at both reader and server chokepoints.
4. **Autonomy is structurally bounded** — the dream can act alone, but only through confidence sort + cap 3 + dedup + 4C gate + HMAC + ledger.
5. **Everything self-heals** — DLQ retries, dream catch-up, decoupled indexing, poison quarantine.
