## Memory tier protocol (agentic-memory-stack)

When MCP-injected memory blocks arrive (mem0 `kind=fact` or `kind=insight`):

1. **Identify source layer** — mem0 (`:18791`), CLAUDE.md (this file), or L0 working context.
2. **Check trust tier** — mem0 `metadata.tier` ∈ {`evidence`, `stable`, `canonical`, `insight`, `temporal`}. Treat `tier=canonical` as ground truth; `tier=insight` as synthesized higher-order facts (also high-trust, written by the C1 nightly consolidator); `tier=stable` as evidence that has survived L10 audit (passable trust); `tier=evidence` as advisory pending confirmation. `tier=temporal` is a **tag**, not a validity-window schema (audit finding 2026-06-08: there is no `valid_from`/`valid_to`/`supersedes` schema; queries on temporal must read the memory text for date ranges).
3. **Cross-check on consequential claims** — for architecture decisions, deploys, credentials, or brand directives, query mem0 `tier=canonical` before asserting.
4. **Mention provenance briefly** in the plan when acting on memory ("per mem0 tier=canonical id X").

When the user explicitly says "remember this", "lock that in", "save what we learned" or equivalent:

1. Call `mcp__mem0__memory_add` with the relevant fact and `metadata={tier: "evidence", source: "user-direct"}`. Capture the returned `id`.
2. Optionally call `mcp__mem0__memory_promote(memory_id=<id>, tier="stable", reason="<one-sentence why>")` to mark it audited-evidence while it awaits canonization.
3. **Canonical cannot be set through MCP.** The shim's `memory_promote` rejects `tier="canonical"` outright, and the server additionally requires an HMAC-signed user-direct token + nonce headers the shim never sends. Hand the operator the id and a suggested reason so they can run the CLI (WSL):
   `bash ~/apps/mem0-scripts/mem0-canonize.sh <id> "<one-sentence why>"`

Server enforcement (the audit-trail boundary added 2026-06-08 to prevent silent canonicalization): a canonical promotion requires `actor="user-direct"` plus a non-empty `reason` PLUS a valid HMAC format-2 token (`<ts>|<nonce>|promote|<mid>|<reason>`, sent as the `X-User-Direct-Token`/`X-User-Direct-Ts`/`X-User-Direct-Nonce` headers) — `mem0-canonize.sh` is the only shipped producer of that token. (A server-side allowlist also admits the dream consolidator's gated autopromote actor.) MCP promotions always carry `actor="claude-autonomous"` and can only set tier to `stable`, `temporal`, or back to `evidence` (`insight` is reserved for the consolidator actors). Do NOT batch canonization requests — surface each id inline so the user can run the CLI and gets per-fact confirmation.

When you notice durable facts during a session (decisions made, identity, preferences, system state changes, paths/IDs/credentials), proactively call `mcp__mem0__memory_add` with `tier: "evidence"` and a descriptive `source` field. The L1a hook-fired extractor (Codex CLI) provides a backstop, but inline capture during the live session is higher quality.

Scoped to MCP-injected memory blocks only. Standard "verify, then assert" rules still apply for non-memory facts.
