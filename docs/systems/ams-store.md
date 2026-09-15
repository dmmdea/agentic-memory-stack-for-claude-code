# ams-store (System A store client)

## Purpose

`ams-store` is one Go binary that maintains Claude Code's native per-workspace
auto-memory stores — a `MEMORY.md` index plus one fact file per index line under
`~/.claude/projects/<workspace>/memory/`. It is the System A counterpart to the
mem0 stack (System B): where mem0 is a replicated server, System A is a set of
git-backed per-machine stores that sync through one hub, with the index *derived*
from the fact set rather than hand-edited.

It replaces three PowerShell surfaces that ship today — the store library
(`scripts/windows/memory-store-lib.ps1`), the write gate
(`scripts/windows/memory-index-write-gate.ps1`) and the nightly compactor
(`scripts/windows/memory-compact.ps1`) — with one implementation for every OS,
and it is the tool the Linux authority runs on the hub. The design is
`plans/2026-09-10-ams-v2-lenovo-authority-design.md` §5, §6; the phased build is
register rows P3-* / P4-* / P5-*.

## Status

**Engines implemented; every verb is real.** There are no stubs left: `derive`,
`harvest`, `lint`, `gate`, `sync`, `sync --watch`, `lock` and the hub-only
`judge-apply` all do their work. Phase 3 is code complete and waits on the hub
(P3-3) and the seed (P3-4) before it runs a fleet.

What that covers: store enumeration (fail-closed, reparse-point/junction dedup,
OS-gated case folding); the atomic writer; `gitx` as the single git surface, with
a `git >= 2.38` check and a kill-tree timeout on every call; the derive engine
(harvest, four hygiene passes, planned-ghost abort, blast cap, derived render
with its injection stop, convergence floor, compare-and-swap write); the merge
engine (out-of-tree three-way merge, deletion table, field-aware frontmatter,
commit-time winner, deferred materialization); sync with the bounded push loop
and the remote policy; the write gate; lint; and the judge's apply-guards.

The engines were built in parallel against one-method interfaces, each unit-tested
against a fake. `cli/seams.go` is the one place they meet, and every adapter there
carries an end-to-end test that drives the real pair rather than the fake - a
disconnected seam compiles, ships and passes every unit test while the verb
silently degrades (`gate.Options{Floor: nil}` prints the same advisory block and
never rewrites a thing).

Open for Phase 4, deliberately: the floor's engage threshold stays on the legacy
hysteresis, `<STATE_ROOT>/role` is not seeded on any machine, `dream-consolidate.py`
does not emit a plan file yet, and `VERSION` is unchanged - the installer wires the
binary in and bumps it.

## Why Go, not the PowerShell library

The design left this as a measurement, not an assertion (§5.4): the write gate
runs on every `Write`/`Edit`, and a PowerShell 5.1 hook spawn is hundreds of
milliseconds. Measured on the reference workstation (20 runs each, same
stopwatch harness for all candidates, payload chosen to hit the gate's cheapest
early-exit path):

| candidate | p50 | p95 |
|---|---|---|
| PowerShell 5.1 gate | ~397 ms | ~471 ms |
| PowerShell 7 gate | ~464 ms | ~514 ms |
| Go stdin-unmarshal harness | ~15 ms | ~17 ms |

The Go path is ~26× faster than the deployed PS 5.1 gate, clearing the design's
5× bar with room to spare, so the pwsh-7-on-Linux fallback (§5.4) stays a
fallback and is not needed. Full receipt with commands and machine facts:
`plans/receipts/2026-09-15-session6-P3-5-measurements.md` (workspace copy).

The same session measured the SessionStart hook order (§6 hook-order note): it is
**not** a fixed before/after — some sessions see their SessionStart hook's write
to the index, others reply and exit before the hook finishes. The derive at
SessionEnd and after every merge is therefore the load-bearing defense that keeps
the on-disk index under cap before the next read; the SessionStart derive is
belt-and-braces, exactly as the design assumed.

## Layout

```
ams-store/
  cmd/ams-store/     thin entry point
  cli/               verb routing + --help + exit-code contract
  internal/
    store/           enumerate, constants, paths, reparse dedup
    index/           parse, render (derived order), line, round-trip, reach, ghosts
    frontmatter/     parse, harvest, doctrine rule
    atomic/          temp + rename + hash read-back writer
    gitx/            every git exec (argv, env, timeout, exit classifier, version check)
    derive/          harvest, hygiene, the floor, truncation
    merge/           merge-tree, deletion table, body merge, winner, materialize, deferred
    sync/            the history repo, the once pass, the watcher, the remote policy
    lock/            the per-PC file lock (pid + start time) and the named mutexes
    gate/            the PostToolUse payload, the advisory block, the receipt
    lint/            the rules, the summary artifact, the over-trigger clock
    live/            the liveness probe
    judge/           the hub-only apply path: plan schema, apply-guards, migration
    porting/         the counterpart table, the mutation table, and the repo-wide gates
    testutil/        sandbox (temp projects root + state root + optional history repo)
  scripts/           the mutation gate (local only, never CI)
```

`cmd/` holds no logic. Every git invocation goes through `gitx`, every file write
through `atomic`, and there is exactly ONE floor, ONE doctrine rule, ONE anchor
rule and ONE history-repo shape in the binary - each of those had two
implementations at some point during the parallel build, and each duplication
decided behaviour rather than merely repeating it. The sync and merge packages
both initialize the history repo; they disagreed about whether the shared
over-trigger stamp was trackable, and whichever ran last won.

## Verbs

```
ams-store derive   [--store <dir>|--all] [--workspace <slug>] [--dry-run] [--json]
                   [--no-harvest] [--stop-below <bytes>] [--projects-root <dir>]
ams-store lint     [--all] [--workspace <slug>] [--json] [--quiet] [--summary-out <path>]
ams-store gate     [--stdin-payload]
ams-store sync     [--once] [--watch] [--timeout <dur>] [--hub-host <name>]
                   [--workspace <slug>] [--allow-local-path] [--json]
ams-store lock     status | acquire --for <dur> --reason <s> | release | break
ams-store harvest  --store <dir> | --all [--workspace <slug>] [--json]
ams-store judge-apply --plan <file> --store <dir> [--workspace <slug>] [--dry-run]
                      [--max-migrations 5] [--force] [--hub] [--candidates]
                      [--mem0-url <url>] [--mem0-user <id>] [--json]   # hub-only
```

## What `derive` does

`derive` is the single deterministic writer. Per store, under the per-PC lock
(a contender skips; it never waits):

1. read the index verbatim and enumerate the fact files **fail-closed** - "could
   not read the directory" must never be spellable as "nothing is there", or a
   caller concludes every line is dangling and the whole index is wiped with a
   receipt reporting success;
2. abort when the index has entries and the store enumerates no fact files;
3. sweep leftover `*.am-tmp` from a previously failed write;
4. **harvest**: copy each entry's hook text into its fact file as `hook:`, and
   stamp `migrated: <id>` on a re-created slug the judge had migrated. Idempotent,
   body-preserving, and it never adds a frontmatter block to a file that has none;
5. **hygiene**, four passes, not three: duplicate slug, dangling removal (with the
   bracketed-title exemption for checkbox lines), dead extra-link repair, orphan
   re-index. The repair pass is the one the design's table omits and the shipped
   compactor performs; without it the post-write ghost check fails forever;
6. abort on a planned entry ghost, before anything is written;
7. abort when hygiene's removals exceed the blast cap, `max(1, floor(entries * 0.2))`;
8. render: fixed heading, doctrine first, then by the commit time of each file's
   last change (one `git log --format=%ct --name-only` pass, never one exec per
   file), slug as tiebreak, always LF;
9. floor, then compare-and-swap against the hash taken at entry and the fact-file
   set enumerated at entry - on drift it **aborts, never rolls back**, because a
   directory-level revert clobbers what the live session just wrote;
10. write atomically, verify the post-write invariants, touch the dirty marker,
    commit locally and append a receipt row to `compact-receipts.jsonl`.

### The floor and the injection cap

The floor truncates non-doctrine hooks to the 130 B line cap in **descending**
rendered length until the projected index is under the stop threshold. Doctrine
is never truncated; a doctrine-only overflow is reported, never "fixed", and
leaves the run unconverged (exit 1).

Phase 3 ships the **legacy hysteresis as the default**: the floor engages at or
above the 25,000 B sync limit and stops below the 20,000 B trigger, so a store
between the two is left alone. `--stop-below <bytes>` moves the stop. The
unconditional-to-trigger floor the design calls for is a threshold change held
for Phase 4, after the zero-hooks-lost check, so it lands as a decided change
rather than a silent one.

The render stops at the 200-line injection cap and reports the omitted entries as
`over-inject-limit N`; they stay on disk and are the judge's first candidates.
**Doctrine is never dropped** - if doctrine alone exceeds the cap the render goes
past it and reports `protected-set-overflow`, because a standing order that
disappears from the index is a standing order nobody obeys.

### Scope is required, never defaulted

`ams-store derive` and `ams-store harvest` refuse to run without `--store`,
`--all` or `--workspace`. Both write - harvest into fact files, derive into
`MEMORY.md` - and an implicit "every populated store on this PC" turned one stray
bare invocation into a fleet-wide write. `TestCLI_WritingVerbsRefuseAnImplicitScope`
is the guard.

## sync, watch, gate, lint

**`sync --once`** is one pass, and its ORDER is the design: derive every store,
commit LOCALLY, clear the dirty marker, and only then fetch, merge and push. The
local commit happens before the fetch so a PC that worked all day offline keeps
its history whatever the network did; every other ordering makes offline work
invisible until connectivity returns, and the box that has been offline for a week
is exactly the one whose history matters most when it comes back. Having no hub
configured is a fully successful pass, not a failure.

The push loop is bounded at three attempts. A non-fast-forward rejection is the
loop's signal that another PC pushed first, so it re-merges and retries; anything
else is a real error and is never retried. The merge itself never runs `git merge`,
`git checkout` or `git stash` - it computes the merge out of tree, resolves every
path in memory, commits it, and only then materializes file by file, fact files
first and the derived index last, so no index ever points at a file that has not
landed. A file a live session has touched since that session started is DEFERRED
rather than replaced, and the queue is applied at the next session boundary.

`MEMORY.md` is never tracked and never merged. Two guards hold that: the
`info/exclude` entry and the `:(exclude)` pathspec on the forced add, because
either one alone would still keep it out and a mutation that removes only one
would look tested. Staging is narrowed to `*.md` for the same reason the exclude
is a deny-list: the force that gets fact files past the exclude would otherwise
track whatever else is in the directory, and the PowerShell compactor leaves
`.bak-<date>-<kind>` files beside the index.

A store whose whole workspace directory is gone has its deletion staged, which is
the one way a store leaves the fleet. The guard is the WORKSPACE directory rather
than the memory folder: a store folder that vanished while its workspace is still
there is far more likely a bad stat than a decision, and propagating that would
carry a fleet-wide removal off one bad read.

**`sync --watch`** is one singleton watcher per PC, not one per session. It holds
`watch.lock`, wakes on the dirty marker through a filesystem watch, and holds no
timer and does no network while idle. A second instance exits 0 silently, because
a session starting while the watcher already runs is the normal case.

**`gate`** is the PostToolUse hook and it returns 0, always - a hook that can fail
a `Write` is a hook that can stop the operator working, so every error path is
swallowed and the worst case is silence. It advises under the caps, normalizes an
index that has crossed the sync limit through the same floor `derive` uses, commits
locally and marks the tree dirty. It never touches the network: the watcher is what
carries the change to the hub.

**`lint`** is read-only by contract and never writes inside a store. It carries the
shipped rules (orphan, dangling, dup-slug, long-line, oversized-file, budget
findings, `compactor-starved`, `compactor-unproductive`) and adds `resurrected` and
`conflict-in-history`, which it reads from the sync receipts and reports with the
losing commit id so the loser is recoverable. `compactor-silent` is parameterised:
on a PC the finding does not exist, because there is no local nightly to be silent;
on the hub `--nightly-unit` names the systemd timer. The G7 clock - hours over
trigger without an applied decision - comes from `.ams/over-trigger.json`, which
rides in the synced tree outside every store and merges with a `min` reducer, so
the earliest crossing any PC saw is the truth. `derive` and `sync` write it; lint
only reads it.

## judge-apply

The nightly judge is split in two on purpose (design Q1): the model call stays in
the Python consolidation chain, which writes a **plan file**; this verb decides
what of that plan may be applied, and applies it. Nothing in `ams-store` calls a
model.

**Hub-only.** The verb refuses to run unless `<state-root>/role` reads `hub`, or
`--hub` is passed (for the hub's own first run, before the role file is seeded).
The refusal is exit 3 and it happens before the plan file is even read. The design
gives exactly one judge, on the Linux authority, against its own checkout of the
hub: two PCs applying the same plan to their own copies would each migrate the
same fact, each delete its own copy of the file, and push two different histories.

**Plan schema** (version 1, strict — an unknown field or verb is refused, never
ignored):

```json
{
  "version": 1,
  "generated_at": "2026-09-15T05:00:00Z",
  "stores": [
    {
      "workspace": "<harness slug>",
      "outcome": "ok | unavailable | empty | parse_fail",
      "note": "free text, carried into the receipt",
      "decisions": [
        { "slug": "some-fact.md", "verb": "SHORTEN", "new_hook": "shorter hook text" },
        { "slug": "other-fact.md", "verb": "MIGRATE" },
        { "slug": "third-fact.md", "verb": "KEEP" }
      ]
    }
  ]
}
```

`SHORTEN` requires `new_hook`; `MIGRATE` may carry `mem0_text` and `metadata`;
`KEEP` carries neither. An outcome other than `ok` must carry no decisions — a
call that did not answer has none. `--candidates` prints the offer set the plan's
producer should build its prompt from, which is the same filter the apply path
uses, so what may be judged and what may be applied are one implementation.

**The apply-guards**, each with a named test and a mutation that turns it red:

| guard | rule |
|---|---|
| doctrine untouchable | never offered, and re-checked at apply time; a plan that names doctrine is refused |
| strict decrease | per line, the rewrite must be shorter; per run, the projected index must shrink or the whole run is discarded |
| anchors | a rewrite must keep at least one anchor token (number, path, URL, backticked identifier, ALL-CAPS term) of the line it replaces |
| round-trip | the rewritten line must re-parse to the same slug set; a markdown link in a hook injects a phantom slug hygiene can never remove |
| the seal | `sealed-lines.json` — one judge rewrite per line, ever |
| write-then-verify | a fact file is deleted only after a byte-equal read-back **by id**; an unverifiable write is undone, and a record the server reports as deduplicated is never deleted |
| blast cap | at most 20 % of the entries may be removed in one run; `--max-migrations` (default 5) additionally bounds the judge's own migrations |
| protected-set overflow | when doctrine alone exceeds the budget the run reports `protected-set-overflow` and stops rather than loosen the hard rule |
| the 20 h window | one judge attempt per store, computed from `judge_called` in the receipts ledger, never from a timer or a stamp file; `--force` bypasses that and nothing else |

The corpus API key is read from the environment only (`MEM0_API_KEY`,
`AMS_MEM0_KEY`): a key on a command line reaches the process list and the shell
history.

**`Migrated:` trailer.** A migrated fact's id has nowhere to live in the file it
came from, because that file is the one being deleted. The deletion commit carries
`Migrated: <slug> <mem0-id>` instead — the one artifact that outlives the file and
is already synced to every PC. When a slug re-appears later, derive's harvest step
looks it up (`git log --grep`, fails closed) and writes `migrated: <id>` into the
new file, so the judge updates the existing record by id instead of adding a
near-duplicate every night.

## Build and test

From `ams-store/`: `go build ./...`, `go vet ./...`, `go test ./...` are the
gates (the same three the sibling Go project uses). CI runs the Go suite on
`ubuntu-latest` with `-race` and builds + tests on `windows-latest` (no `-race`,
which needs a C toolchain) so the Windows reparse/path code is exercised. The
binary cross-compiles for `windows/amd64` and `linux/amd64`.

Running BOTH jobs is not redundancy. The Linux job runs a different git, and that
is what caught a merge that worked on git 2.55 and failed outright on 2.43: the
unrelated-histories path - the first sync between two PCs that each ran `git init`
before either had pushed - handed `merge-tree` the empty TREE as its merge base,
which 2.55 accepts and 2.43 refuses. The design's floor is git 2.38, so the version
that refuses is inside the supported range and the version that accepts is the one
the engine was written on.

### The gates that check the tests

Four checks in `internal/porting` guard the suite itself, because each of them
covers a failure that a green suite cannot see:

- **the counterpart gate** reads the four Pester files, extracts all 95 `It`
  blocks, maps each through `counterparts.go` and asserts a Go test of that name
  exists (`go test -list`). Exactly one scenario is exempt and carries its reason
  inline: `MemoryCompactRobustness.Tests.ps1:420`, the `-CatchUp` fresh-then-stale
  run, whose spawn the design removes from the PCs. A second exemption fails the
  gate, as does a table row whose `It` moved, or an `It` with no row.
- **the placeholder check** refuses any remaining `ported in task N` skip. A
  skipped test is still a test to `go test -list`, so the counterpart gate alone
  would pass over a suite that asserts nothing.
- **the home-sandbox check** fails when a package whose source calls
  `store.DefaultRoots` has tests without a `TestMain` that moves `HOME` and
  `USERPROFILE` to a temp directory. Two live incidents hours apart on 2026-09-15
  came from a test that did not inject its roots: one harvested a `hook:` line into
  248 live fact files, the other committed five live stores into the real history
  repo. In both the code was correct and the TEST reached production.
- **the ASCII check** keeps non-ASCII out of Go source. Inherited from the
  PowerShell library it replaces: a BOM-less UTF-8 file read as ANSI by PowerShell
  5.1 tokenises an em-dash as a smart quote, so the separator is built from its code
  point (`"\u2014"`) and never typed. Goldens under `testdata/` are exempt - they are
  the product, and an ASCII golden could not prove the em-dash survives.

### The mutation gate

`ams-store/scripts/mutation-gate.sh` is LOCAL only, never a CI job: it is N+1
times the suite. It exists because a passing suite proves the tests run, not that
they would NOTICE if a rule were lost.

For each of the 25 rules in blueprint 10.2 it copies the target file aside, applies
one minimal change that breaks that rule, **builds**, runs the named test alone
expecting it to FAIL, and restores the file with a byte compare against the
pre-image. The build step is not ceremony: a mutation that does not compile reads
as a red test while proving nothing. Neither is the byte-verified restore - the
gate's first ever run used WSL git against a Windows worktree checkout, every
`git checkout --` failed silently, fifteen mutations stacked on each other, and it
reported four rules SURVIVED that had never been tested in isolation.

Current state: **red 25, survived 0, broken 0, pending 0**. A SURVIVED rule means
nothing tests it; a NOCOMPILE entry means the mutation is broken. Both were hit
while arming the last ten and both are reported separately for that reason.
