# Model-Aware Memory Injection (v0.22)

Memory injection is **available, not shoved**: the auto-surfaced `[MEMORY CONTEXT]` block
is scaled to the *consuming model's* tier — full for frontier Opus/Fable, lighter for
small Haiku-class models, and **nothing at all** for the local offload harness. The
guiding principle is that memory leverage should live with the capable agent that can
actually use it, and the cheaper a model is, the less context it should have to wade
through.

This is purely additive for the dominant path: **the frontier tier reproduces v0.21
behavior byte-for-byte** (regression-guarded in
`scripts/windows/tests/UserPromptExtract.Tests.ps1`). Only the small tier renders a
different (flatter, shorter) block, and the offload tier renders none.

## Where this lives

| Piece | File |
|---|---|
| Tier policy (caps, thresholds, format, legend) | `claude-config/model-tiers.json` (deployed beside the hook lib as `~/.claude/scripts/model-tiers.json`) |
| Model→tier resolution + tier-aware render | `scripts/windows/user-prompt-lib.ps1` (`Resolve-ModelTier`, `Get-SessionTier`, `Format-MemoryContextBlock -Tier`) |
| SessionStart tier sidecar writer | `scripts/windows/mem0-hook-daemon-spawn.ps1` |
| Server-side tier-scaled bundle | `mem0-server/app.py` (`context_bundle`, `ContextBundleIn.tier`) |
| Verification (R-offload + R-budget) | `scripts/windows/Test-MemoryStack.ps1` (INVARIANTS I13/I14) |

## Tier matrix

The canonical source is `claude-config/model-tiers.json`. As of v0.22 (post-EmbeddingGemma
migration, with thresholds recalibrated for the 768-d EmbeddingGemma-300m embedder):

| Tier | Matches (substring, case-insensitive) | mem cap | goal cap | OQ cap | relevance threshold | format | legend | block |
|---|---|---|---|---|---|---|---|---|
| **frontier** | `opus`, `fable` | 5 | 5 | 3 | 0.30 | full | no | yes — full `[tier\|brand] text` bullets |
| **mid** | `sonnet` | 5 | 4 | 3 | 0.30 | full | no | yes — same full format as frontier |
| **small** | `haiku` | 3 | 3 | 2 | 0.33 | flat | yes | yes — flat, highest-tier-first, 1-line legend, `[brand]` tag dropped when brand known |
| **offload** | Gemma cascade (`mcp__local-offload__*`) | — | — | — | — | — | — | **none — by construction** (see below) |

Notes:

- **Thresholds** are read from `model-tiers.json`, not hardcoded. They were lowered from
  0.4 (nomic era) to 0.30/0.33 in v0.22 when mem0's embedder migrated from English-only
  `nomic-embed-text` to multilingual **EmbeddingGemma-300m** (768-d, CPU via llama-swap
  on `:11436`); the reranker is **bge-reranker-v2-m3** (CPU). Ollama was decommissioned in
  the same migration.
- **Caps** are applied server-side in `context_bundle` (per `ContextBundleIn.tier`). The
  client `Select-AdmittedMemoryResults` independently hard-caps surfaced memories at
  **top-3** (defense in depth, every tier) and truncates each memory to 200 chars.
- **Frontier == v0.21**: 5/5/3 caps, full format, no legend — byte-identical output, so
  the main Opus/Fable driver sees no change.
- **Small format**: prepends a one-line trust legend, orders memories highest-tier-first
  (canonical/insight/stable before evidence/temporal), and drops the redundant `|brand`
  suffix when the session brand is known (every surfaced row is same-brand or
  brand-neutral by admission anyway).

  > Memory tiers: [canonical]=locked truth · [insight]/[stable]=trusted · [evidence]/[temporal]=advisory (verify before risky actions) · prefer higher-tier on conflict.

### How a session's tier is resolved

`UserPromptSubmit` has **no** `model` field in its payload, so detection is staged:

1. **SessionStart** reads `model` from its payload, runs `Resolve-ModelTier`, and writes a
   per-session sidecar `~/.mem0/session-tier/<session_id>.json` (`{model, tier, initiative, ts}`).
2. **UserPromptSubmit** reads the sidecar (fast path, no transcript scan).
3. **Fallback** (resume/compact sessions started before the sidecar existed): tail the
   transcript and resolve `Resolve-ModelTier` against the **last** assistant line's
   `.message.model`, then cache the result back to the sidecar.
4. **Default**: `frontier`. Over-injecting a rare unknown model is the safe default — the
   main driver is Opus/Fable, and frontier == today's behavior.

Resolution and rendering are fail-open everywhere: any error → `frontier` → the v0.21
block. A tier-resolution slip can never *degrade* injection, only fail to lighten it.

## The offload-no-block principle

The local offload harness (`mcp__local-offload__offload_classify | offload_extract |
offload_summarize | offload_triage`, a free Gemma-4 cascade on `:11436`) gets **no memory
block at all — and this is true by construction, not by a runtime tier check.** Three
independent facts make it impossible for the harness to receive the `[MEMORY CONTEXT]`
block:

1. **`UserPromptSubmit` fires on human prompts only.** It is *never* raised for a tool call
   or an MCP invocation, and it does not fire inside subagents. The offload harness is
   reached only via `mcp__local-offload__*` tool calls from the orchestrating agent — those
   calls do not raise `UserPromptSubmit`, so the only producer of the block is never invoked
   for them.
2. **The block producer is bound only to the human-prompt client.** `UserPromptSubmit` is
   registered to a single command — the compiled `mem0-hook-client.exe` (PowerShell fallback
   `user-prompt-extract.ps1`) and the daemon it spawns. There is no matcher and no `mcp__`
   anything on that registration.
3. **`PreToolUse` gates strictly on the editing/exec tools and excludes MCP.** The stack's
   `PreToolUse` hook matcher is `Bash|Edit|MultiEdit|Write`; no `PreToolUse` matcher names
   `mcp__`. So no hook of any kind fires for an `mcp__local-offload__*` call, and that hook
   path does not render the block anyway.

Therefore the offload model receives **only** what the orchestrating agent passes it as
tool arguments — never the memory block, never CLAUDE.md, never the tier protocol (it runs
in a separate MCP process that does not load any of those).

### Verification (CI-style gate)

`Test-MemoryStack.ps1` asserts this invariant every run, so a future misconfiguration
that would expose the harness is caught:

- **R-offload** (INVARIANTS I13): parses the deployed `~/.claude/settings.json` hooks and
  asserts (a) `UserPromptSubmit` binds only to the human-prompt client with no `mcp__`
  matcher/command, and (b) no `PreToolUse` matcher names `mcp__`. Fail-**open** WARN if the
  config/helper is absent; **FAIL** only on a real violation (an `mcp__` matcher or command
  on either path). Logic lives in `Test-OffloadNoBlockInvariant` (`user-prompt-lib.ps1`),
  Pester-covered.
- **R-budget** (INVARIANTS I14): renders a worst-case (cap-filling) block per tier and
  asserts each is within a char-proxy ceiling derived from that tier's caps (small is
  tighter by caps). Optional precise leg: with `$env:ANTHROPIC_API_KEY` set it calls the
  Anthropic **count_tokens** API (never tiktoken — it undercounts Claude 15–20%) and asserts
  the small-tier block is under its token target; skipped cleanly with no key. Non-fatal /
  fail-open. Logic lives in `Measure-MemoryContextBudget` (`user-prompt-lib.ps1`),
  Pester-covered.

## The "orchestrating agent inlines one fact" pattern

Because the offload harness gets no memory, the **memory leverage stays with the capable
orchestrating agent** (Opus/Fable). When an offload task genuinely needs a stored fact,
the orchestrator retrieves it (via `mcp__mem0__memory_search` or from the already-surfaced
block) and **inlines that one fact into the tool arguments** — it does not try to give the
small model the whole context.

Concretely, the offload model gets: **task + input + (optionally) one fact-as-argument.**
For example, if a classification depends on a brand-specific label set, the orchestrator
passes the label set in the `labels` argument rather than expecting the Gemma model to
recall it. This keeps the offload path fast, free, and stateless, and keeps the
high-judgment "which fact matters" decision where the judgment is — on the frontier model.

## See also

- `docs/modular/tier-policy.md` — the trust tiers themselves (who can write/promote each).
- `docs/modular/admission-gate.md` — the server + client admission layers the block passes
  through before rendering.
- `ARCHITECTURE.md` (P1 Tier Protocol) — the always-loaded CLAUDE.md tier-protocol prose
  that frontier sessions read; the small-tier legend is the per-session, in-block flat
  echo of it.
