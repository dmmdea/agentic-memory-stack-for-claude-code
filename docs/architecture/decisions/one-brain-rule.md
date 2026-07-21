---
status: Accepted
date: "2026-07-13"
---

# Single write authority — the One-Brain Rule

## Context

The store is self-writing and can be reached from more than one machine (a brain box, plus a replica
box used while away from it). If two machines could both accept writes, their copies would diverge
with no authority to reconcile them — and the nightly consolidation and dedup mutate the one shared
brain, with no cross-machine lock to coordinate a second writer.

## Decision

Exactly one machine — the **brain box** — holds write authority. Every other machine is a **replica
box**: read-only, and it can *never* absorb a write. Two independent mechanisms enforce this:

- **Install-time role gate.** `install.ps1 -Role brain|replica` (default `brain`) records the role
  in the install receipt. A `brain` install registers the two nightly canonical-mutation scheduled
  tasks (the dream consolidator and the semantic dedup); a `replica` install registers *neither* and
  removes any it finds. Verify asserts the tasks are present on a brain and absent on a replica.
- **Offline write path.** When the authority is unreachable, mutations queue to an on-disk Outbox
  and replay to the authority on reconnect; they are never redirected to the local replica, which is
  restored read-only and torn down on reconnect.

Because a write only ever lands on the authority or in the Outbox that drains to it, divergence is
impossible **by construction**, not by policy.

## Consequences

- The store cannot silently fork; there is always exactly one truth to reconcile against.
- A replica box must carry the brain's address in `~/.mem0/authority-url` (installer `-AuthorityUrl`), or its writes would queue and replay into its own
  disposable store — a One-Brain violation the offline-watcher's authority guard also prevents.
- The offline-watcher must never run on the brain box (its reconnect transition stops the live
  services); its installer refuses registration there.

## Alternatives considered

Not recorded. The record states the single-writer invariant and its two enforcement points, not a
weighed-and-rejected multi-writer design.

## Related code

- [`install/2-windows-config.ps1`](../../../install/2-windows-config.ps1) — the `brain`/`replica` role gate.
- [`scripts/wsl/mem0-mcp-shim.py`](../../../scripts/wsl/mem0-mcp-shim.py) — writes go to the authority or the Outbox, never the replica.
- [`scripts/wsl/replay-ops.py`](../../../scripts/wsl/replay-ops.py) — the reconnect replay driver.

## Related docs

- [`installer-and-deploy.md`](../../systems/installer-and-deploy.md) — the install-time role gate in full.
- [`offline-travel.md`](../../systems/offline-travel.md) — the offline-side statement of the rule.
- [`offline-outbox-replay.md`](../../flows/offline-outbox-replay.md) — the offline write→replay flow.
