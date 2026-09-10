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

  **Both installers gate, not just the Windows one (2026-09-07).** The rule above describes the
  Windows scheduled tasks. `install/1-wsl-services.sh` enabled its units unconditionally, so
  running it on a replica stood up a SECOND write authority: a local `mem0` + `qdrant` plus the
  `l10-audit`, `decay-scan` (whose `ExecStartPost` runs semantic-dedup), `stack-backup`,
  `goals-stale-sweep`, `contradiction-sweep`, `retrieval-pairs`, `episodic-reconcile` and
  `goal-recurrence-promote` timers — canonical-mutation jobs against a store that box does not
  own. The health check already expected the opposite ("brain-only machinery reports by design"
  on a replica), so the installer and the verifier disagreed. It now routes every brain unit
  through a role-gated helper that installs the unit either way — promoting a replica to brain
  stays a one-liner — but on a replica never enables it and disables anything an earlier
  ungated run switched on, mirroring the Windows installer's skip-and-remove exactly.
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

**Amendment 2026-09-10** (ADR [`fleet-store-sync-and-linux-authority.md`](./fleet-store-sync-and-linux-authority.md)):
the brain box may be a native-Linux always-on server rather than a Windows+WSL workstation, and
the rule extends to the harness's own per-workspace stores: their history repositories may carry
exactly one remote, the hub on the brain box, and no other. The single-writer invariant for the
memory corpus is unchanged.

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
