---
status: Superseded
date: "2026-07-14"
superseded_by: ./offline-first-emergent.md
---

# Explicit operator switch for travel mode

> This ADR records a historical decision. It was **superseded** by
> [`offline-first-emergent.md`](./offline-first-emergent.md); do not treat it as current guidance.

## Context

Before offline behavior emerged from connectivity, using memory away from the write authority (the
**brain box**) was a deliberate operator action, bracketed by two manual steps around a trip.

## Decision

Offline use was handled by an explicit switch, `travel-mode.ps1 on/off`:

- **`on`** (before a trip) restored a **read-only replica** from the newest snapshot set
  (`restore-replica.ps1`) and redirected the session's writes by pointing `MEM0_URL` at the replica
  context, writing a `~/.mem0/travel.json` flag the shim read to know it was offline.
- **`off`** (on return) replayed the queued operations to the authority and tore the replica down.

The operator, not connectivity, decided when the box was "offline".

## Consequences

- Correctness depended on the operator remembering to flip the switch at **both** ends: forgetting
  `on` left writes failing or misrouted, and forgetting `off` left the box pointed at the disposable
  replica.
- That fragility is why it was replaced in v1.15 by connectivity-emergent offline behavior
  ([`offline-first-emergent.md`](./offline-first-emergent.md)), where the shim fails over per call and
  a background watcher restores and replays automatically with no flag.
- The machinery is not dead: the watcher's `go_offline` still **reuses** `travel-mode.ps1 on`'s
  restore path, and `travel.json` is now vestigial — it only feeds the `status` read-out; the shim no
  longer reads it.

## Alternatives considered

Not recorded. This ADR captures the historical manual-switch approach itself; the emergent model that
replaced it is [`offline-first-emergent.md`](./offline-first-emergent.md).

## Related code

- [`scripts/travel/travel-mode.ps1`](../../../scripts/travel/travel-mode.ps1) — the legacy `status`/`on`/`off` switch.
- [`scripts/travel/restore-replica.ps1`](../../../scripts/travel/restore-replica.ps1) — the read-only replica restore its `on` path invoked.

## Related docs

- [`offline-travel.md`](../../systems/offline-travel.md) — the current offline subsystem, with the legacy switch marked legacy.
- [`offline-first-emergent.md`](./offline-first-emergent.md) — the ADR that supersedes this one.
