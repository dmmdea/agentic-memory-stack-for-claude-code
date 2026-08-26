# Auto-Memory Maintenance

## Purpose

Keeps the coding agent's own per-workspace memory stores healthy without human effort. The
agent harness writes a file-backed memory store per workspace; nothing in the harness maintains
it, so an index grows until it silently breaches a hard limit and structural defects (a fact
file nothing links to, an index line pointing at a deleted file) accumulate unseen. This system
lints those stores on every session start, and compacts them nightly under a set of safety
guards, so the store the agent reads at the start of every session stays small, correct and
loaded in full.

## Questions this doc answers

- What are the two memory systems in this stack, and which one does this system own?
- What are the hard limits, and what actually happens when a store breaches one?
- What may the nightly job change, and what is it forbidden to touch?
- How is a memory kept safe when the job removes its index line?
- What happens when a session is writing memories while the job runs?
- How do I see what changed last night, and how do I undo it?

## Scope

The harness-native per-workspace stores at `~/.claude/projects/<workspace>/memory/`: the
`MEMORY.md` Index and its Fact Files. Three pillars: **lint** (read-only health scan),
**write-time lint** (a hook that reports an over-long index line to the agent that just wrote
it), and **compaction** (the nightly maintainer).

## Non-scope

- The mem0 corpus and its own generated index. That is a different store with a different
  lifecycle; see [memory-model.md](memory-model.md). The two are **harmonized, not merged**
  (ADR [auto-memory-system-a-vs-b.md](../architecture/decisions/auto-memory-system-a-vs-b.md)).
- **Fact File bodies.** The compactor rewrites the Index only. An oversized fact file is
  reported and left alone; splitting one is a semantic judgment left to a human.
- Anything the agent writes during a session. Maintenance never runs against a live workspace.

## Key concepts

**Index** — `MEMORY.md`: one pointer line per memory, written as a Markdown list item whose
link text is the title, whose link target is the fact file, and whose trailing dash introduces
the Hook. The whole
Index is injected into every session in that workspace; the Fact Files are not.

**Fact File** — one memory, with frontmatter (`name`, `description`, `metadata.type`,
`metadata.modified`) and a body. Loaded only when the agent follows its pointer.

**Hook** — the text after the link. Its job is to make the agent open the file at the right
moment, so it must keep the distinguishing detail, not merely name the topic.

**Doctrine Entry** — a memory carrying `metadata.type: feedback` (nested, never top-level) or
imperative standing-order phrasing. Untouchable by every job here.

**The two budgets** — a store breaches at 25,000 bytes per file (the store stops syncing, which
is loud) or at 200 Index lines (entries past the cap are not injected, which is silent). They
nearly coincide for a dense index, so both are enforced.

**Trigger and target** — compaction fires at 20,000 bytes *or* 160 lines and aims below 17,000
bytes *and* 140 lines. The gap is hysteresis: a just-compacted store is not re-processed the
following night.

## How the system works

### Pillar 1 — lint (read-only)

`memory-lint.ps1` is spawned detached at session start on its own 6-hour throttle. It
enumerates every populated store (deduplicating alias directories by canonical path so one
store is never processed twice), and recomputes findings from disk: orphan, dangling, duplicate
slug, over-long line, oversized fact file, missing frontmatter, near or over a budget. It writes
one summary file and **never writes inside a store**.

There is deliberately no flag ledger or reviewed-keys watermark. The finding set is under a
dozen items across the whole fleet and is recomputable in milliseconds; a monotone watermark
would silently suppress an issue that was fixed and later recurred.

Two findings watch the maintainer itself: a store above trigger with no run receipt in 48 hours
(the nightly job is not actually running), and a history repository that has gained a remote
(these stores hold private material and must never be pushed).

### Pillar 2 — write-time lint (the source fix)

The harness warns when an Index passes its line cap, but nothing checks bytes per line — and
that is what fills the byte budget first. A `PostToolUse` hook on Write/Edit returns instantly
unless the path is a workspace Index, then reports any line over the hook budget so the agent
fixes it in the same turn, while the material is still in context. It is advisory and always
exits 0: a hook must never be able to block a memory write.

### Pillar 3 — compaction (nightly)

`memory-compact.ps1` runs from a scheduled task at 05:00, offset from the other nightly jobs so
the shared judge mutex is free. Per store, in order:

1. **Liveness gate.** Skip if a session in that workspace has written recently.
2. **Snapshot.** Commit the store to the history repository.
3. **Deterministic hygiene.** Remove dangling and duplicate lines; re-index orphans from their
   own frontmatter. No model involved — this is correctness, and it applies unconditionally.
4. **Feasibility.** If Doctrine Entries alone exceed the target budget, stop and report. The
   hard rule is never loosened to make room.
5. **Judgment on the delta only.** One model call per store, over long lines and migration
   candidates — never the whole Index, and never a Doctrine Entry.
6. **Apply**, under the guards below.
7. **Compare-and-swap**, then write, then verify invariants, then commit and write a receipt
   with an annotated diff.

Decisions available to the judge are `SHORTEN` (rewrite the hook shorter), `MIGRATE` (move a
pullable lookup fact into the mem0 corpus and drop its line), and `KEEP` — the safe default
whenever the call is unclear, because a wrongly-kept line costs bytes while a wrongly-migrated
one removes steering the agent needed before it knew to ask.

## Important flows

**A memory leaving the Index (migration).** The fact is posted verbatim (never paraphrased, so
a retry deduplicates instead of creating a variant) with `source: automemory:<workspace>/<file>`,
then read back **by id** and compared byte-for-byte. Only then are the line and file removed.
If the write returns no id, that is unverifiable and the line stays. The source tag also exempts
the record from semantic dedup, which deletes the newer of a near-duplicate pair — without the
exemption a fact verified at 05:02 could be evicted the next morning, since a migration is
always the newer side.

**A night's changes, reviewed in thirty seconds.** Each run appends a receipt (counts, byte
delta, mem0 ids with the original line verbatim) and writes an annotated diff of the Index. The
session banner surfaces a receipt while it is fresh.

**Undo.** Every state the store passed through is a commit in the history repository. Restore
is always per-file, never a directory-level revert.

## Data and state

All maintainer state lives **outside** the stores, under `~/.claude/state/automemory/`: the
history repository (its git directory out of tree, work tree pointed at the projects root, no
remote), the lint summary, run receipts, per-workspace diffs and seal records. Nothing but
`MEMORY.md` and Fact Files may exist inside a store — the harness syncs that directory and
agents glob it, so a maintenance folder there would resurface removed facts in every search.

## Interfaces and entry points

| Entry point | Trigger | Writes |
|---|---|---|
| `scripts/windows/memory-lint.ps1` | session start, 6h throttle | lint summary only |
| `claude-config/memory-index-write-lint.sh` | `PostToolUse` Write/Edit | nothing (stdout) |
| `scripts/windows/memory-compact.ps1` | scheduled task, 05:00, 23h throttle | Index, history, receipts |
| `scripts/windows/memory-store-lib.ps1` | dot-sourced library | nothing |

The compactor accepts `-DryRun` (decide and report, write nothing), `-Force` (bypass throttle,
trigger and liveness gate — for rehearsal) and `-Workspace <name>`.

## Dependencies

The shared helper library (throttle, judge mutex, judge invocation, memory write), the judge CLI,
the memory server for migrations, and `git` for history. Every one of them is optional at
runtime: a missing judge or server degrades the run to deterministic hygiene, and the job skips
rather than guessing.

## Downstream effects

Compaction changes what every future session in that workspace reads at startup. Migration moves
a fact from always-injected to retrieved-on-demand. The dedup job's keep/delete decision now
depends on the migration source tag.

## Invariants and assumptions

- Every populated Index stays under both hard caps, and **maintenance never grows a store** past
  one — re-indexing an orphan is skipped rather than crossing the sync limit.
- No orphans, no dangling links, no duplicate slugs after a run.
- Reachability is decided by "is this file linked from any line", never by whether a line
  matched the entry pattern — otherwise an unparseable pointer makes its file look like an
  orphan and earns it a second, duplicate pointer.
- A Doctrine Entry is never shortened by a model, merged, migrated or dropped.
- No file is ever deleted without its content existing either in the memory corpus (verified by
  id) or in the history repository.
- The job writes exactly one file per store: the Index. Fact files are deleted only *after*
  that write and its invariant check succeed, so an abort leaves the store untouched.
- A model-driven edit must strictly shrink the Index; hygiene is exempt from that rule, because
  correctness must never be gated on saving bytes.
- Every line the job constructs must parse back to the same single pointer.
- Each line may be rewritten by the model at most once, ever.
- No run removes more than a fifth of the lines — hygiene removals included.

## Error handling

| Failure | Behaviour |
|---|---|
| A session is live in the workspace (or cannot be probed) | skip that store; receipt records why |
| The store changed during the run | abort before anything is written **or deleted** |
| The fact directory cannot be read, or enumerates empty under a non-empty Index | abort; never treat every line as dangling |
| Hygiene wants to remove more than a fifth of the lines | abort and report; never silently gut an Index |
| Judge unreachable or unparseable | deterministic hygiene only; status says so; throttle **not** marked |
| Memory server returns no id, or the read-back does not match | migration not performed, the line stays, and the unverified record is deleted |
| Post-write invariants fail | restore the Index alone; if that restore fails, say so loudly with the recovery command |
| Doctrine set alone exceeds the budget | stop and report; never loosen the rule |
| No verified pre-run snapshot | skip the store; never mutate without a restore point |
| One store throws | that store is recorded as an error; the others continue |

There is no local fallback for judgment work. A run that only skipped does not mark the
throttle, so it is retried rather than silently counted as done. Each store's receipt is written
the moment that store finishes, never buffered to the end of the run — a job killed by its
execution time limit must not lose the record of what it already did.

## Security and privacy notes

These stores contain credentials, endpoints and private brand facts. Consequences: the history
repository must never gain a remote (asserted by lint), maintainer state stays out of the synced
store directory, and migrated records carry their workspace tag so they cannot be read as
belonging to another brand.

## Observability and debugging

The component log records every decision and skip with its reason. Receipts are the audit trail;
the diff beside them shows exactly what changed. Two health-check rows cover this system: an
invariants row asserting the budgets and structural cleanliness of every store, and a recovery
row asserting both that the task is registered against the deployed script and that a store
above trigger has produced a receipt within 48 hours.

Freshness is not progress, and both surfaces say so: a compactor that aborts every night still
writes a receipt every night, so the lint additionally reports a store above trigger whose last
three runs all ended in a non-productive status. The banner reports the lint summary's own age
rather than presenting stale counts as current.

To rehearse safely: run with `-DryRun` (decides, writes nothing), or with `-Workspace` against a
throwaway store.

## Testing notes

`MemoryStoreLib.Tests.ps1` covers the primitives: byte-exact round-trip of a real index
including multi-byte characters, index parse/regenerate fidelity, nested-frontmatter reading,
doctrine classification (including the case a human review caught during design, where a
doctrine entry was misfiled as a location fact), alias deduplication, stateless lint findings,
the liveness probe, and history snapshot/restore including that a per-file restore leaves a
concurrently-created file alone.

`MemoryCompact.Tests.ps1` runs the shipped compactor against synthetic stores with a scripted
judge and memory server — one scenario per guard, each written so that removing the guard makes
it fail: liveness skip, concurrent-write abort, doctrine untouched (and never even offered to
the judge), a hook that does not shrink or that drops every anchor token rejected, a sealed line
never re-offered, migration kept on unverifiable or mismatched read-back, hygiene still applied
when the judge is down, feasibility failing loud, idempotence, and dry-run purity.

## Common pitfalls

- **`type` is nested.** A top-level `^type:` match finds nothing; the doctrine rule would be
  inert and every standing order eligible for deletion.
- **Two different `MEMORY.md` files.** One belongs to the harness (this system), one is
  generated from the memory corpus. Editing the wrong one is a silent no-op.
- **A store is not quiescent at night.** A box that sleeps runs its catch-up at the next logon,
  which is exactly when sessions start.
- **Encoding.** Store files are UTF-8 without a BOM. Writing them through a shell default that
  adds a BOM or rewrites line endings corrupts every line and defeats change detection.
- **Verification by search is not verification.** Ranking a record top for its own text can be
  satisfied by a pre-existing near-duplicate; only read-back by id proves the write landed.

## Source map

- [`scripts/windows/memory-store-lib.ps1`](../../scripts/windows/memory-store-lib.ps1) — primitives
- [`scripts/windows/memory-lint.ps1`](../../scripts/windows/memory-lint.ps1) — pillar 1
- [`claude-config/memory-index-write-lint.sh`](../../claude-config/memory-index-write-lint.sh) — pillar 2
- [`scripts/windows/memory-compact.ps1`](../../scripts/windows/memory-compact.ps1) — pillar 3
- [`scripts/wsl/semantic-dedup.py`](../../scripts/wsl/semantic-dedup.py) — migration exemption

## Related docs

- [memory-model.md](memory-model.md) — the memory corpus this system migrates into
- [auto-memory-system-a-vs-b.md](../architecture/decisions/auto-memory-system-a-vs-b.md) — why harmonize rather than merge
- [installer-and-deploy.md](installer-and-deploy.md) — how these scripts and the task are deployed
- [tier-policy.md](tier-policy.md) — the tier migrated records are written at
- [glossary.md](../glossary.md)
