---
status: Accepted
date: "2026-07-16"
---

# Offline behavior emerges from connectivity — no mode switch

## Context

Memory must stay usable when the machine loses its path to the write authority (the **brain box**).
The earlier design made offline use a deliberate operator action — a switch flipped before a trip and
back on return (see [`switch-based-travel-mode.md`](./switch-based-travel-mode.md)). Depending on a
human to flip a mode is a correctness hazard: forget it in one direction and writes fail or route to
a store that could diverge from the authority.

## Decision

Offline behavior **emerges from connectivity**; there is no mode to turn on. The mem0 MCP shim
decides per call:

- **Reads** try the authority with a short **1.5s connect timeout**; on a *connect-level* failure they
  fail over to a read-only local replica, tagged `local-replica` with a `stale_note`.
- **Mutations** go to the authority *only*; on a connect-level failure they queue to an on-disk
  **Outbox** (`~/.mem0/outbox.jsonl`) — never to the replica.

On reconnect, the offline-watcher's `go_online` runs the replay driver, which drains the Outbox to the
authority **adds-first**, **idempotent** (a `uuid4`-key ledger), and **never dropping a record**. Only
a connect-level failure triggers failover; a read timeout or an HTTP error status is a real answer and
propagates.

## Consequences

- The replica can **never absorb a write**, so divergence is impossible *by construction* — the
  offline-side statement of the One-Brain Rule.
- No operator action is required, and a session degrades (empty-but-valid `offline` result) rather
  than crashing.
- The Outbox writer and the replay `dispatch` map are one contract: a mutating tool added without its
  op sends offline mutations to the conflict log.

## Alternatives considered

- **The switch-based travel mode** (an explicit `travel-mode.ps1 on/off`) — superseded by this design;
  it depended on the operator flipping a mode at both ends. See [`switch-based-travel-mode.md`](./switch-based-travel-mode.md).

## Related code

- [`scripts/wsl/mem0-mcp-shim.py`](../../../scripts/wsl/mem0-mcp-shim.py) — per-call read failover and the write-to-Outbox queue.
- [`scripts/wsl/replay-ops.py`](../../../scripts/wsl/replay-ops.py) — the adds-first, idempotent reconnect drain.
- [`scripts/travel/offline-watcher.ps1`](../../../scripts/travel/offline-watcher.ps1) — the background offline/online transitions.

## Related docs

- [`offline-travel.md`](../../systems/offline-travel.md) — the full offline subsystem.
- [`offline-outbox-replay.md`](../../flows/offline-outbox-replay.md) — the write→replay flow.
- [`switch-based-travel-mode.md`](./switch-based-travel-mode.md) — the superseded manual-switch approach.
