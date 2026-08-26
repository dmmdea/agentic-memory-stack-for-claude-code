---
status: "Accepted"
date: "2026-08-26"
---

# Harness auto-memory and the memory corpus are harmonized, not merged

## Context

Two files named `MEMORY.md` exist in this stack, and they are routinely confused.

**System A** is the coding-agent harness's own per-workspace file memory: a `MEMORY.md` index of
one-line pointers plus one fact file per pointer, under the workspace's own directory. The
harness injects the whole index into every session in that workspace and loads a fact file only
when the agent follows its pointer. It is written by the agent through the harness's memory
tool, is machine-local, and until this change **nothing maintained it**.

**System B** is the memory corpus served by the memory server: an embedded, tiered, searchable
store with its own generated index, maintained by a family of scheduled jobs (extraction,
consolidation, dedup, decay, audit). It is shared across machines and retrieved on demand.

The question forced by a real incident: one workspace store reached 96% of its hard per-file
limit with an unindexed fact file that no session had ever loaded, and no job existed that would
ever have noticed. Should the two systems be merged — one store, one maintainer — or kept
separate?

## Decision

Keep them separate and connect them deliberately.

1. **System A is the push channel.** It holds what must shape behaviour *before* the agent knows
   to ask: standing orders, corrections, traps, preferences. Its budget is enforced by
   compaction, not by moving its content elsewhere.
2. **System B is the pull channel.** It holds what is needed *once a topic is already in hand*:
   endpoints, identifiers, paths, versions, finished statuses.
3. **The bridge is one-directional and explicit.** Compaction may migrate a fact from A into B
   at the evidence tier, verbatim, tagged with its origin (`source: automemory:<workspace>/<file>`),
   verified by read-back, and only then may its index line be removed. Nothing flows B → A.
4. **Doctrine never migrates.** A memory typed as feedback, or phrased as a standing order,
   stays in A regardless of any other consideration.

## Consequences

**Benefits.** Each store keeps the retrieval model it is good at, and neither maintainer has to
understand the other's lifecycle. The push channel stays small enough to be injected in full.
The origin tag makes migrated records traceable back to the file they replaced, and gives the
dedup job the signal it needs to protect them.

**Costs and risks.** A migrated fact is no longer guaranteed to be in front of the agent; if the
judge misclassifies push as pull, steering is lost quietly. This is mitigated by making KEEP the
default under uncertainty, by the doctrine rule, by a bounded number of migrations per run, and
by a receipt that names every migrated fact with its original line so a human can reverse it.
The dedup exemption is a second, load-bearing consequence: without it the corpus would evict a
just-migrated record as the newer half of a near-duplicate pair, because the extractor has often
already captured the same fact from the session transcript.

**What this rules out.** No job may write into System A on the basis of System B content, and no
"pointer record" summarizing a workspace's memory may be added to B — the corpus's own capture
rules classify a pointer as ephemera and drop it, and the index is already injected where it
matters.

## Alternatives considered

**Merge A into B** (delete the workspace store; let retrieval serve everything). Rejected: the
harness injects A unconditionally at session start, which is precisely the property standing
orders depend on. Retrieval only fires when a query resembles the memory, and the memories that
matter most are the ones the agent does not know to search for.

**Merge B into A** (write everything to workspace files). Rejected: A is machine-local, has a
hard size limit, and has no search, tiering or contradiction handling.

**Generate A from B nightly.** Rejected: it makes the agent's own writes second-class — the
harness writes A during a session, and a nightly regeneration would overwrite them — and it
couples the injected context to the health of the whole corpus pipeline.

**Leave A unmaintained and simply raise the caps.** Not available: the caps are the harness's,
not ours.

## Related code

- [`scripts/windows/memory-compact.ps1`](../../../scripts/windows/memory-compact.ps1)
- [`scripts/windows/memory-store-lib.ps1`](../../../scripts/windows/memory-store-lib.ps1)
- [`scripts/wsl/semantic-dedup.py`](../../../scripts/wsl/semantic-dedup.py)

## Related docs

- [auto-memory-maintenance.md](../../systems/auto-memory-maintenance.md)
- [memory-model.md](../../systems/memory-model.md)
- [tier-policy.md](../../systems/tier-policy.md)
