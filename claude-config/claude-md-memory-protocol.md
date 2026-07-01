## Memory tier protocol (agentic-memory-stack)

When MCP-injected memory blocks arrive (mem0 `kind=fact` or `kind=insight`):

1. **Identify source layer** — mem0 (`:18791`), CLAUDE.md (this file), or L0 working context.
2. **Check trust tier** — mem0 `metadata.tier` ∈ {`evidence`, `stable`, `canonical`, `insight`, `temporal`}. Treat `tier=canonical` as ground truth; `tier=insight` as synthesized higher-order facts (also high-trust, written by the C1 nightly consolidator); `tier=stable` as evidence that has survived L10 audit (passable trust); `tier=evidence` as advisory pending confirmation. `tier=temporal` is a **tag**, not a validity-window schema (audit finding 2026-06-08: there is no `valid_from`/`valid_to`/`supersedes` schema; queries on temporal must read the memory text for date ranges).
3. **Cross-check on consequential claims** — for architecture decisions, deploys, credentials, or brand directives, query mem0 `tier=canonical` before asserting.
4. **Mention provenance briefly** in the plan when acting on memory ("per mem0 tier=canonical id X").

When the user explicitly says "remember this", "lock that in", "save what we learned" or equivalent:

1. Call `mcp__mem0__memory_add` with the relevant fact and `metadata={tier: "evidence", source: "user-direct"}`. Capture the returned `id`.
2. **Immediately** call `mcp__mem0__memory_promote(memory_id=<id>, tier="canonical", actor="user-direct", reason="<one-sentence why>")`.

The server **enforces** that `tier="canonical"` requires `actor="user-direct"` plus a non-empty `reason`. Autonomous Claude promotions (default actor `"claude-autonomous"`) are rejected for canonical — they can only set tier to `stable` or `temporal`. This is the audit-trail boundary added 2026-06-08 to prevent silent canonicalization. Do NOT batch these — record them inline so the user gets confirmation.

When you notice durable facts during a session (decisions made, identity, preferences, system state changes, paths/IDs/credentials), proactively call `mcp__mem0__memory_add` with `tier: "evidence"` and a descriptive `source` field. The L1a hook-fired extractor (Codex CLI) provides a backstop, but inline capture during the live session is higher quality.

Scoped to MCP-injected memory blocks only. Standard "verify, then assert" rules still apply for non-memory facts.
