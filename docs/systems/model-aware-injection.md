# Model-Aware Memory Injection (v0.22)

Memory injection is **available, not shoved**: the auto-surfaced `[MEMORY CONTEXT]` block
is scaled to the *consuming model's* tier — full for frontier Opus/Fable, lighter for
small Haiku-class models, and **nothing at all** for an offload tool (a cheap local model reached over MCP). The
guiding principle is that memory leverage should live with the capable agent that can
actually use it, and the cheaper a model is, the less context it should have to wade
through.

This is purely additive for the dominant path: **the frontier tier reproduced v0.21
behavior byte-for-byte at v0.22** (regression-guarded in
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

The canonical source is `claude-config/model-tiers.json` (its server mirror is `TIER_BUNDLE_POLICY` in
`mem0-server/app.py`). Current values: v0.23 folded the old `mid` tier into `frontier`, 1.35.0 moved the
relevance gate to the active embedder profile's calibration (0.70 on EmbeddingGemma-2), memory caps were cut
to 2 and 1, and 1.37.0 set both the goal cap and the open-question cap to 0, so the per-prompt block holds
memories only (goals and open questions return only behind a per-item relevance gate; the session-start
banner still lists them):

| Tier | Matches (substring, case-insensitive) | mem cap | goal cap | OQ cap | relevance threshold | format | legend | block |
|---|---|---|---|---|---|---|---|---|
| **frontier** | `opus`, `fable`, `sonnet` | 2 | 0 | 0 | 0.70 | full | no | yes — full `[tier\|brand] text` bullets |
| **small** | `haiku` | 1 | 0 | 0 | 0.70 | flat | yes | yes — flat, highest-tier-first, 1-line legend, `[brand]` tag dropped when brand known |
| **offload** | a local-model MCP tool (`mcp__<server>__*`) | — | — | — | — | — | — | **none — by construction** (see below) |

Notes:

- **Thresholds** are read from `model-tiers.json`, not hardcoded. They were lowered from
  0.4 (nomic era) to 0.30/0.33 in v0.22 (0.70 now, the active profile's calibrated gate) when mem0's embedder migrated from English-only
  `nomic-embed-text` to multilingual **EmbeddingGemma-300m** (768-d, via llama-swap
  on `:11436`); the reranker is **bge-reranker-v2-m3** (it runs on the GPU; the score scale is
  device-independent, so threshold calibration does not depend on the device). Ollama was
  decommissioned in the same migration.
- **Caps** are applied server-side in `context_bundle` (per `ContextBundleIn.tier`). The
  client `Select-AdmittedMemoryResults` independently hard-caps surfaced memories at
  **top-3** (defense in depth, every tier) and truncates each memory to 200 chars.
- **Frontier format**: full `[tier|brand] text` bullets, no legend. At v0.22 it matched v0.21 output
  byte for byte; its caps have moved since (K = 2, and goals and open questions off in 1.37.0).
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

An offload tool is an MCP server that fronts a cheap local model (classify, extract,
summarize). The stack does not ship one; if you register one, it gets **no memory
block at all — and this is true by construction, not by a runtime tier check.** Three
independent facts make it impossible for the tool to receive the `[MEMORY CONTEXT]`
block:

1. **`UserPromptSubmit` is never raised for a tool call.** It fires for human prompts and
   also for background task notifications, which get no block since C10 (see
   [`../flows/memory-retrieval.md`](../flows/memory-retrieval.md), Channel 1). It is *never*
   raised for a tool call or an MCP invocation, and it does not fire inside subagents. The
   offload tool is reached only via `mcp__<server>__*` tool calls from the
   orchestrating agent. Those calls do not raise `UserPromptSubmit`, so the only producer of
   the block is never invoked for them.
2. **The block producer is bound only to the human-prompt client.** `UserPromptSubmit` is
   registered to a single command — the compiled `mem0-hook-client.exe` (PowerShell fallback
   `user-prompt-extract.ps1`) and the daemon it spawns. There is no matcher and no `mcp__`
   anything on that registration.
3. **The stack registers no `PreToolUse` hook.** Its only matcher-bearing hooks are
   `PostToolUse` (`Write|Edit`) and `SessionStart` (`startup|clear|compact`); none names
   `mcp__`. So no stack hook fires for an `mcp__<server>__*` call. The invariant below
   guards against a third-party `PreToolUse` matcher that would fire for one.

Therefore the offload tool receives **only** what the orchestrating agent passes it as
tool arguments — never the memory block, never CLAUDE.md, never the tier protocol (it runs
in a separate MCP process that does not load any of those).

### Verification (CI-style gate)

`Test-MemoryStack.ps1` asserts this invariant every run, so a future misconfiguration
that would expose the offload tool is caught:

- **R-offload** (INVARIANTS I13): parses the deployed `~/.claude/settings.json` hooks and
  asserts (a) `UserPromptSubmit` binds only to the human-prompt client with no `mcp__`
  matcher/command, and (b) no `PreToolUse` hook (a third-party one, since the stack registers none) that
  fires for an offload call can route the memory block to it.

  (b) is evaluated the way Claude Code evaluates it: each matcher is run as a **regex** against
  a real offload tool name, not substring-searched for the server name — `mcp__.*`, `mcp__`
  and `.*` all fire at runtime while containing no such substring, so a substring test would
  miss precisely the holes that matter. A firing matcher is an **exposure** only when the
  command behind it is a memory-context **producer** (the stack's own hook client, daemon or
  extractor, listed authoritatively in `$script:AmMemoryContextProducers`); a third-party guard
  that fires on an offload call and merely denies or logs cannot route a block it never had.
  The check reads the hook's `command` **and** its `args` — a hook is routinely
  `{command: "node.exe", args: ["…guard.js"]}`, where reading `command` alone sees only
  "node.exe" — and follows **one hop** into the script the hook names, because a wrapper hides
  the producer from the literal text.

  Past that one hop it says so rather than implying proof: a firing foreign hook is a WARN whose
  wording is explicitly *inferred*, and anything that could not be evaluated — an unparseable
  matcher, an unreadable file — is reported as `NOT VERIFIED` instead of passing quietly.
  Fail-**open** WARN if the config/helper is absent; **FAIL** only on a real violation. Held as
  a hard FAIL, the older and broader "any matcher that fires" rule reported the stack UNHEALTHY
  for days over an unrelated deny-only delegate guard — which is how a standing red light stops
  being read at all. Logic lives in `Test-OffloadNoBlockInvariant` (`user-prompt-lib.ps1`),
  Pester-covered including the wrapper-indirection case.
- **R-budget** (INVARIANTS I14): renders a worst-case (cap-filling) block per tier and
  asserts each is within a char-proxy ceiling derived from that tier's caps (small is
  tighter by caps). Optional precise leg: with `$env:ANTHROPIC_API_KEY` set it calls the
  Anthropic **count_tokens** API (never tiktoken — it undercounts Claude 15–20%) and asserts
  the small-tier block is under its token target; skipped cleanly with no key. Non-fatal /
  fail-open. Logic lives in `Measure-MemoryContextBudget` (`user-prompt-lib.ps1`),
  Pester-covered.

## The "orchestrating agent inlines one fact" pattern

Because the offload tool gets no memory, the **memory leverage stays with the capable
orchestrating agent** (Opus/Fable). When an offload task genuinely needs a stored fact,
the orchestrator retrieves it (via `mcp__mem0__memory_search` or from the already-surfaced
block) and **inlines that one fact into the tool arguments** — it does not try to give the
small model the whole context.

Concretely, the offload model gets: **task + input + (optionally) one fact-as-argument.**
For example, if a classification depends on a brand-specific label set, the orchestrator
passes the label set in the `labels` argument rather than expecting the small model to
recall it. This keeps the offload path fast, free, and stateless, and keeps the
high-judgment "which fact matters" decision where the judgment is — on the frontier model.

## See also

- `docs/systems/tier-policy.md` — the trust tiers themselves (who can write/promote each).
- `docs/systems/admission-gate.md` — the server + client admission layers the block passes
  through before rendering.
- `ARCHITECTURE.md` (P1 Tier Protocol) — the always-loaded CLAUDE.md tier-protocol prose
  that frontier sessions read; the small-tier legend is the per-session, in-block flat
  echo of it.
