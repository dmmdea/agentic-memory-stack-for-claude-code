---
status: Accepted
date: "2026-06-15"
---

# Codex CLI as the extraction, consolidation, and judgment LLM

## Context

Several unattended jobs need an LLM: the session fact extractor, the nightly consolidator, and the
reconciliation sweeps / NLI write-gate (contradiction and supersession judgment). Running these
through the in-session Claude is unreliable — Claude Max OAuth enforces a **single concurrent session
per account**, so subprocess `claude --print` calls from hooks fail intermittently with "Not logged
in" while an interactive session holds the slot (verified extensively: detached PowerShell,
WSL-bridged, `WSLENV` forwarding — all unreliable). Local models were disqualified separately on
judgment quality (a measured 78% false-positive rate).

## Decision

Route all extraction, consolidation, and judgment to the **Codex CLI** (authenticated via a ChatGPT
subscription — a separate OAuth surface with no concurrency block), used strictly as the *LLM* and
**never as a data store**. Codex runs Windows-side; the WSL-side sweeps reach it over a loopback
**HTTP shim on `:18792`** that moves only clean JSON across the boundary, API-key-authed. A single
shared Codex lock serializes every use — the extractor, the dream consolidator, and the shim never
invoke Codex concurrently.

## Consequences

- Unattended jobs run reliably and headless at zero marginal cost (covered by the existing ChatGPT
  subscription), and never touch the Claude credential.
- Local models are confined to embedding and reranking — the "local models never judge" invariant.
- The shim adds a Windows↔WSL hop for judgment, and the shared lock means a long Codex call in one job
  briefly blocks the others.

## Alternatives considered

- **In-session Claude via `claude --print`** — rejected: the Claude Max single-concurrent-session
  block makes hook subprocess calls fail "Not logged in" (WSL-bridged and `WSLENV`-forwarded variants
  failed too).
- **Local llama-swap models as the judge/extractor** — rejected on measured quality (78% false
  positives on contradiction judgment).
- **A paid Anthropic API key** — out of scope by design (no paid APIs beyond existing subscriptions).

## Related code

- [`scripts/windows/l1a-extract.ps1`](../../../scripts/windows/l1a-extract.ps1) — the Codex-backed session extractor.
- [`scripts/windows/dream-consolidate.ps1`](../../../scripts/windows/dream-consolidate.ps1) — the nightly Codex consolidator.
- [`mem0-server/codex_shim_client.py`](../../../mem0-server/codex_shim_client.py) — the WSL client for the `:18792` judgment shim.

## Related docs

- [`codex-hooks.md`](../../systems/codex-hooks.md) — the extractor and the shared Codex mutex.
- [`dream-skill.md`](../../systems/dream-skill.md) — the nightly consolidator.
- [`reconciliation.md`](../../systems/reconciliation.md) — Codex as the sweep and write-gate judge.
