---
status: Accepted
date: "2026-06-08"
---

# Five-tier trust model with cryptographically-gated canonical

## Context

A self-writing memory store needs a trust axis, or an autonomously-captured guess ranks alongside a
fact the operator explicitly locked in. A 2026-06-08 audit found the concrete failure: the tier
protocol was bypassable — a direct `memory_add` with `metadata.tier=canonical` set it, and the
`memory_promote` tool hardcoded `actor="claude"`, so autonomous Claude could silently elevate any
evidence to canonical. That erased the "the operator explicitly decided" guarantee.

## Decision

Every record carries one of five trust tiers — **canonical > stable > insight > evidence >
temporal** — and tier transitions are enforced **server-side** (no metadata trick bypasses them; the
server returns 403). The load-bearing rules:

- **`canonical` is never reachable by autonomous agents.** POST can never set it; `PATCH /tier` to
  canonical requires `actor="user-direct"` plus a non-empty reason **and** an HMAC-SHA256 token
  signed with the canonical key, produced only by the `mem0-canonize.sh` CLI. The MCP shim's
  `memory_promote(tier='canonical')` raises before it ever reaches the server.
- **Autonomous actors are capped.** `claude-autonomous` may write only `evidence`/`stable`/`temporal`;
  `insight` is consolidator-only. The nightly dream may autopromote to canonical but is bounded to
  **≤3/night**, each still HMAC-signed and passed through the 4C contradiction gate.

## Consequences

- Ground truth is unforgeable: canonical cannot be created or mutated without the DPAPI-held key, so a
  compromised or prompt-injected agent cannot elevate its own guesses.
- Every tier change is auditable — each PATCH and insight add appends to the append-only tier ledger.
- Canonical promotion is deliberately a human (or tightly-gated nightly) act, not an in-session one.

## Alternatives considered

- **`actor="claude"` self-promotion** (the pre-audit behavior) — rejected: it let autonomous Claude
  silently canonicalize, removing the user-intent guarantee.
- **In-conversation Claude writing `insight`** — rejected: it sees only a slice, producing noisier
  synthesis; `insight` is restricted to the consolidator that sees the full evidence window.

## Related code

- [`mem0-server/app.py`](../../../mem0-server/app.py) — the tier-policy constants and the `PATCH /tier` actor rule.
- [`mem0-server/security_invariants.py`](../../../mem0-server/security_invariants.py) — the format-2 HMAC user-direct validation.
- [`scripts/wsl/mem0-canonize.sh`](../../../scripts/wsl/mem0-canonize.sh) — the sole canonical-signing surface.

## Related docs

- [`tier-policy.md`](../../systems/tier-policy.md) — the tier matrix and server-enforced constants.
- [`memory-model.md`](../../systems/memory-model.md) — tiers, lifecycles, and the canonical anchor set.
- [`canonical-promotion.md`](../../flows/canonical-promotion.md) — the HMAC promotion flow.
- [`glossary.md`](../../glossary.md) — tier definitions.
