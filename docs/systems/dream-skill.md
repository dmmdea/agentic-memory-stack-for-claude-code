# Dream Consolidator — nightly memory consolidation

## Purpose

The dream consolidator is the nightly job that turns the day's raw `evidence` into durable knowledge: it synthesizes cross-cutting `insight`-tier memories, autonomously promotes a tightly-gated few evidence facts to `canonical`, and rebuilds the lean `MEMORY.md` index. It is the live script `scripts/windows/dream-consolidate.ps1`, run by the Windows Task Scheduler entry `ClaudeCode-DreamConsolidator-3am`. It superseded the earlier single-pass `c1-consolidate.ps1` (a lone evidence→insight pass).

The 4-phase consolidation pattern (orient → gather → consolidate → prune) and the "dream" framing are ported from [`grandamenium/dream-skill`](https://github.com/grandamenium/dream-skill) (MIT License). This stack adapts the concept to its own architecture — a Windows Task Scheduler trigger, a PowerShell orchestrator, a Codex CLI subagent, and a mem0 REST backend — but the conceptual model is MIT-licensed upstream work.

## Questions this doc answers

- What runs nightly, when, and how is it triggered and throttled?
- What are the phases (1–4, plus 3.5 autopromote and 5 drift), and what does each do?
- How does autonomous canonical promotion stay safe (the 4C gate, the cap, the HMAC)?
- What is the retrieval-drift canary, and what is the `EvalRootWsl` fallback?
- How does it avoid colliding with the real-time L1a extractor?

## Scope

The nightly cycle: orientation, evidence/transcript gathering, Codex-driven insight synthesis, autonomous canonical promotion, `MEMORY.md` rebuild, and the retrieval-drift canary — plus the trigger, the 24h throttle, the shared Codex mutex, and the missed-run catch-up.

## Non-scope

- **Real-time L1a extraction** (the per-session Stop-hook facts) → [`codex-hooks.md`](./codex-hooks.md).
- **What the tiers mean** and the promotion lifecycle → [`memory-model.md`](./memory-model.md).
- **The HMAC canonize CLI internals** and the tier PATCH gate → [`mem0-api.md`](./mem0-api.md) and [`dpapi-canonical-key.md`](./dpapi-canonical-key.md).
- **The exact reconciliation matrix** → [`reconciliation.md`](./reconciliation.md).

## Key concepts

- **The five phases** — orient, gather, consolidate, autopromote (3.5), prune, plus a zero-Codex drift canary (5).
- **24h throttle** — one consolidation per day, independent of the L1a 10-min throttle.
- **Shared Codex mutex** — the dream and L1a contend for one Codex lock so they never invoke Codex concurrently.
- **4C promotion gate** — the contradiction + source-weighted-corroboration gate that decides whether an autopromotion nominee reaches canonical.
- **Retrieval-drift canary** — a before/after snapshot of whether a fixed canary set stays retrievable across a consolidation, run only when an eval checkout is present (`EvalRootWsl`).

## How the system works

The consolidator fires nightly at 03:00 (Task Scheduler, `-WakeToRun`). It first checks the 24h throttle and exits immediately if the last successful run was < 24h ago. It then acquires the shared Codex mutex (blocking L1a extractions for its duration) and runs the phases in order, marking the throttle only after a phase completes successfully. Every Codex call goes through the ChatGPT-subscription Codex CLI, never Claude — see [`codex-hooks.md`](./codex-hooks.md) for why.

If the machine is off/asleep at 03:00 the scheduled run is simply missed. `dream-catchup.ps1` — spawned detached from a SessionStart hook — covers that: it does cheap in-process debt checks (a pending learn-rule, a queued promotion, or a last run > 48h ago) and nudges `dream-consolidate.ps1` only when there is real work, fail-open throughout. The dream's own 24h throttle + Codex lock prevent a double-run.

## Important flows

### Phase 1 — Orient

Load current context before deciding what to gather. Reads:
1. `~/.mem0/MEMORY.md` — the lean index from the previous dream run (if it exists)
2. The current tier distribution: count of evidence / insight / canonical / stable / temporal in mem0
3. Time since last dream run (`~/.claude/state/dream-last-run`)
4. Any open audit flags in `~/.mem0/audit-flags.jsonl` since baseline

Output: a "context snapshot" struct passed to Phase 2 — what the consolidator already knows, what tiers are crowded, whether there's enough new evidence to justify consolidation.

**Skip condition:** the 24h throttle is enforced at the top level — if `dream-last-run` is < 24h ago (`86400s`), the script exits before Phase 1.

### Phase 2 — Gather

Pull the evidence window and transcript signals that will feed consolidation.

**From mem0:**
- All `tier=evidence` memories created in the last 36 hours, capped at 30 most-recent
- Sort by `created_at` descending

**From transcripts:**
- Last 36h of `~/.claude/projects/*/*.jsonl` — scans the JSONL transcripts for surprise/correction/contradiction signals
- These are not re-extracted into facts; they inform the consolidation prompt's context budget

**Information Gain principle (bridge to AGI paper):** the gather phase preferentially surfaces surprising, contradicting, or correction signals — facts that update a prior belief rather than reinforce it. Implementation: the Codex prompt for Phase 3 receives evidence sorted by "novelty" — items that appear to contradict earlier canonical/insight memories are listed first. This is a heuristic implementation of the Information Gain principle from [Agent Exploration Toward AGI (SSRN-6748619)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6748619).

### Phase 3 — Consolidate

The Codex call (gpt-5.5, reasoning=medium, timeout=180s):

```
prompt: "You are a memory consolidator running once per 24h. Below are recent evidence-tier 
facts from the past 36h. Produce 1-3 CONSOLIDATED INSIGHTS — cross-cutting patterns, themes, 
or synthesized higher-order facts that emerge from looking ACROSS them.

Rules:
- Each insight ≤ 40 words, self-contained, declarative, durable.
- Maximum 3 insights. Skip the obvious; surface what is not visible from any single fact.
- Prefer surprising / contradicting / updating signals over reinforcing ones.
- If nothing meaningful emerges, output {"insights":[]}.
- Output: {"insights":["insight 1","insight 2"]}

Evidence:
<sorted bullet list with IDs>
"
```

Output: `{"insights":["...", "..."]}`. Each insight posted to mem0 as:
```json
{
  "tier": "insight",
  "source": "dream-consolidator",
  "source_memory_ids": ["<id1>", "<id2>", ...],
  "window_evidence_count": N,
  "consolidated_at": "<ISO timestamp>"
}
```

### Phase 3.5 — Autonomous canonical promotion

After consolidation, the dream may autonomously promote a few `evidence` facts to `canonical` under a strict, precision-first bar. A second Codex call nominates canonical-worthy evidence (evergreen, declarative, ground-truth, cross-session, high-confidence); `Invoke-AutopromoteDecision` (in `autopromote-lib.ps1`) then runs the pure pipeline: parse → structural filter (rejects task/imperative text) → sort by confidence → **cap at 3** → dedup against the existing canonical set.

Each surviving nominee passes through the **4C promotion gate** (`Invoke-PromotionGate`):
1. **Contradiction gate (all sources):** any contradiction against an existing canonical fact — judged by an *independent, adversarial* Codex pass, never the proposing pass — → BLOCK.
2. **Source-weighted corroboration:** a `trusted` source (operator-asserted) fast-tracks on the contradiction gate alone; every other source class needs `≥ MinCorroboration` (default **2**) independent observations. Fail-safe: anything not on the trusted allowlist is treated as untrusted.

The gate is **shadow-first**: `MEM0_PROMOTION_GATE_MODE` ∈ `{off, shadow, enforce}` (default `shadow`; also settable persistently via the receipt's `PromotionGateMode`). `shadow` computes and logs the verdict but does not change the promotion decision (calibration); `enforce` makes a BLOCK verdict — or a gate *error* (fail-safe) — skip the promotion; `off` is the kill switch. The single decision point is the unit-tested `Resolve-GateBlocked`.

Promotion itself calls `mem0-canonize.sh --actor dream-autopromote` — so a dream promotion is still HMAC-signed with the canonical key (the actor label only distinguishes it from `user-direct` in the ledger; `transport=autonomous`). A `422` from the server means the imperative-canary rejected the text (expected for edge cases, non-fatal). Nominees and outcomes are appended to a morning summary.

### Phase 4 — Prune

After promotion, rebuild `~/.mem0/MEMORY.md` as a lean ≤200-line index.

**Structure of `~/.mem0/MEMORY.md`:**
```markdown
# Memory Index (auto-generated by dream-consolidator, <timestamp>)
Total: N memories | evidence: N | insight: N | canonical: N | stable: N | temporal: N

## Top 7 — highest canonical/insight density
1. <memory text> [canonical, id=...]
2. ...

## Recent evidence (last 24h)
- <text> [evidence, id=...]
...

## Canonical facts
- <text> [canonical, id=..., reason=...]
...
```

Rules for MEMORY.md generation:
- Hard cap: 200 lines. If the index would exceed this, truncate evidence section first (keep canonical + insight + Top-7 always).
- Top-7 section: the 7 memories with highest trust × recency score. Canonical > insight > stable > evidence weighting.
- Regenerated at the end of every dream run, not on every L1a extraction.

### Phase 5 — Retrieval-drift canary (zero Codex)

To catch a consolidation that silently degrades retrieval, the dream takes a **before** and **after** snapshot of whether a fixed canary set stays retrievable against live mem0 (local EmbeddingGemma + Qdrant — **no Codex**), then compares them. The BEFORE snapshot is taken only on nights that actually consolidate (after the "no signals" and dedup-lock early-returns); the AFTER snapshot runs only if a BEFORE was taken.

The canary harness lives in the eval checkout: `<EvalRootWsl>/eval/retrieval-drift/retrieval_drift.py`. **`EvalRootWsl` fallback:** the eval tree is optional (it lives with the private moat checkout after the repo split). The consolidator resolves `EvalRootWsl` from the operator receipt; if absent, it falls back to `RepoRootWsl`, and if that is also absent, to an empty string — in which case the drift snapshot **no-ops and the compare is skipped, never raising a false alarm**. A snapshot that fails for any reason also skips the compare for that cycle.

## Data and state

| File | Role |
|---|---|
| `~/.mem0/MEMORY.md` | The lean ≤200-line index, rebuilt every run (Phase 4). |
| `~/.claude/state/dream-last-run` | Unix timestamp of the last successful run; the 24h throttle store. |
| `~/.mem0/audit-flags.jsonl` | Open audit flags read in Phase 1. |
| morning summary | Human-readable log of the night's autopromotions and gate verdicts. |
| `/tmp/dream-drift-before.json` / after | The retrieval-drift snapshots compared in Phase 5. |
| `~/.claude/logs/codex-usage.jsonl` | Per-call Codex token/duration/status records. |

## Interfaces and entry points

- **Trigger:** Windows Task Scheduler entry `ClaudeCode-DreamConsolidator-3am`, daily at 03:00, `-WakeToRun`, action = `dream-consolidate.ps1`.
- **Catch-up:** `dream-catchup.ps1`, spawned detached from a SessionStart hook, nudges the consolidator when debt has accumulated.
- **Flags:** `-DryRun` makes zero promotions and zero file writes (nominees logged only); `-Force` bypasses **only** the 24h throttle.
- **Backend calls:** mem0 REST (`GET`/search for evidence and canonical, `POST` insights) and `mem0-canonize.sh --actor dream-autopromote` for canonical promotion.

## Dependencies

- **Codex CLI** (gpt-5.5, ChatGPT-subscription OAuth) for the consolidation and autopromote-nomination calls.
- **The shared Codex mutex** (`memory-common.ps1`) — see [`codex-hooks.md`](./codex-hooks.md).
- **mem0 REST** on `:18791` and **`mem0-canonize.sh`** for the HMAC-signed promotion.
- **EmbeddingGemma + Qdrant** (via mem0) for the Phase 5 drift canary; the optional **`eval/` harness** for the drift script.

## Downstream effects

Consolidation writes new `insight`-tier records and (rarely, ≤3/night) new `canonical` facts — both of which change what future retrievals and the per-prompt bundle surface, and canonical additions become part of the anchor set the reconciliation sweeps judge new facts against. The rebuilt `MEMORY.md` is what the next run's Phase 1 and the session-start précis read.

## Invariants and assumptions

- At most one consolidation per 24h (the throttle is marked only after a phase succeeds).
- The dream never invokes Codex concurrently with L1a — the shared mutex guarantees it.
- At most **3** canonical autopromotions per night, each HMAC-signed and gate-checked.
- `-DryRun` writes nothing (no promotions, no file writes).
- A missing or failed drift snapshot never produces a false alarm — the compare is simply skipped.

## Error handling

Every phase is best-effort and fail-open: the 4C gate is wrapped so it can never crash the consolidator (a gate error becomes a fail-safe BLOCK in `enforce` only); a malformed Codex JSON skips the throttle mark so the next run retries; a `422` canary rejection on a promotion is non-fatal; the Codex mutex is released in a `finally` block; a canonical fetch or canonize failure is logged non-fatally. The 24h throttle is written only when a phase completes without error.

## Security and privacy notes

Autonomous canonical promotion does **not** bypass the canonical write gate: it still signs the same format-2 HMAC token via `mem0-canonize.sh` (actor `dream-autopromote`), so an attacker who could only run the consolidator still cannot forge canonical without the DPAPI-held key. The imperative-canary independently blocks standing-order text from the canonical tier. Logs carry counts, ids, and reasons — not raw memory text where avoidable.

## Observability and debugging

Per-run logging goes to the dream component log; the GATE log records each nominee's verdict, gate class, source class, corroboration count, and contradiction flag; the morning summary lists what was promoted, gate-blocked, deduped, or over-cap; `codex-usage.jsonl` tracks token spend. The Phase 5 drift compare is the alarm for a consolidation that degraded retrieval.

## Testing notes

The pure decision logic in `autopromote-lib.ps1` (`Invoke-PromotionGate`, `Resolve-GateBlocked`, `Get-SourceClass`, `Get-CorroborationCount`, `Test-CanonicalDuplicate`, `Test-ImperativeOrTask`) is unit-tested without the live stack (`DreamAutopromote.Tests.ps1`, `DreamGateVerdict.Tests.ps1`). Validate an end-to-end change with `-DryRun`, which exercises the full pipeline (including shadow-mode gate verdicts) while writing nothing.

## Common pitfalls

- **Expecting the 4C gate to block by default** — it ships in `shadow` mode (log-only). It only blocks promotions when `MEM0_PROMOTION_GATE_MODE` (or the receipt's `PromotionGateMode`) is `enforce`.
- **Assuming autopromotion skips the HMAC** — it does not; it signs via `mem0-canonize.sh` exactly like an operator promotion.
- **Confusing `-Force` with skipping the gate** — `-Force` bypasses only the 24h throttle, nothing else.
- **Expecting drift alarms without the eval checkout** — with no `EvalRootWsl`, Phase 5 no-ops silently.

## Source map

- [`../../scripts/windows/dream-consolidate.ps1`](../../scripts/windows/dream-consolidate.ps1) — the nightly orchestrator (all phases, drift canary, EvalRootWsl resolution).
- [`../../scripts/windows/autopromote-lib.ps1`](../../scripts/windows/autopromote-lib.ps1) — the pure 4C-gate + nomination decision logic.
- [`../../scripts/windows/dream-catchup.ps1`](../../scripts/windows/dream-catchup.ps1) — the debt-based missed-run catch-up.
- [`../../scripts/windows/memory-common.ps1`](../../scripts/windows/memory-common.ps1) — the shared Codex lock and throttle helpers.
- [`../../scripts/wsl/mem0-canonize.sh`](../../scripts/wsl/mem0-canonize.sh) — the HMAC-signed canonical promotion the dream calls.

## Related docs

- [`codex-hooks.md`](./codex-hooks.md) — the shared Codex mutex, and the real-time L1a extractor the dream complements.
- [`memory-model.md`](./memory-model.md) — the tiers, the 4C gate, and the canonical anchor set.
- [`mem0-api.md`](./mem0-api.md) — the `PATCH /tier` gate the promotion goes through.
- [`reconciliation.md`](./reconciliation.md) — how canonical facts anchor the contradiction sweeps.
- [`../flows/memory-capture.md`](../flows/memory-capture.md) — the capture pipeline that feeds nightly consolidation.
- [`../glossary.md`](../glossary.md) · [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)
