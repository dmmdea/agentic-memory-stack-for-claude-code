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
hysteresis as the DEFAULT of `--engage-at` (the flip is a change of that default,
not an edit to the floor), `<STATE_ROOT>/role` is not seeded on any machine, and
`dream-consolidate.py` does not emit a plan file yet. **Since 1.25.0 the Windows
installer wires the binary in** (register P4-1a; see *Release assets and install*
below): it installs the release asset, writes the hub transport, registers the
gate and sync hooks, spawns the watcher and retires the PowerShell nightly. The
Linux installers follow (P4-1b for the hub, P4-3 for the thin client).

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
over-trigger stamp was trackable, and whichever ran last won. That one shape is
`merge.RepoConfig`: an `automemory` identity with signing off, `core.autocrlf`,
`core.safecrlf` and `core.quotepath` off so git never rewrites a byte, an empty
`core.hooksPath` under the git dir so the operator's global hooks never see a hub
push, renames off, and on Windows `core.longpaths` on - git refuses a work-tree
directory over MAX_PATH otherwise, and a live projects root already holds four
workspace directories past that length.

## Verbs

```
ams-store derive   [--store <dir>|--all] [--workspace <slug>] [--dry-run] [--json]
                   [--no-harvest] [--engage-at <bytes>] [--stop-below <bytes>]
                   [--projects-root <dir>]
ams-store lint     [--all] [--workspace <slug>] [--json] [--quiet] [--summary-out <path>]
ams-store gate     [--stdin-payload] [--engage-at <bytes>]
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

`--dry-run` leaves the store and the dirty marker untouched and still appends its
receipt row, flagged `dry_run` with status `dry-run`: the ledger is the record of
every run, rehearsals included (the compactor's own contract, carried 1:1), and
lint skips `dry_run` rows when it judges whether the maintainer is silent or
starved, so a rehearsal can never pass for a run.

### The floor and the injection cap

The floor truncates non-doctrine hooks to the 130 B line cap in **descending**
rendered length until the projected index is under the stop threshold. Doctrine
is never truncated; a doctrine-only overflow is reported, never "fixed", and
leaves the run unconverged (exit 1).

Phase 3 ships the **legacy hysteresis as the default**: the floor engages at or
above the 25,000 B sync limit and stops below the 20,000 B trigger, so a store
between the two is left alone. Both thresholds are flags on the two verbs that run
the floor: `--engage-at <bytes>` moves the engage point and `--stop-below <bytes>`
moves the stop, on `derive` and on `gate`. The unconditional-to-trigger floor the
design calls for is therefore a change of `--engage-at`'s DEFAULT rather than an
edit to the floor, held for Phase 4 after the zero-hooks-lost check so it lands as
a decided change rather than a silent one - and it can be rehearsed on one PC
first. The gate carried its own copy of the same threshold in its silent-exit and
advise-only rules; both read the engage value now, or a lowered `--engage-at`
would have been accepted on the command line and then silently skipped before the
floor was ever called.

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

### The deferred queue is a pending merge result, not a note

A queued path is already resolved in HISTORY and only withheld from the work tree,
and both halves of that need holding.

**Nothing re-stages a queued path while it waits.** The staging pass excludes every
queued path by literal `:(exclude,literal)` pathspec, because a blanket add would
otherwise re-add the file whose deletion was withheld (resurrecting a judge
migration fleet-wide) and re-commit the session's older bytes over the merged blob
(reverting another PC's edit) - the same defect in both directions. The exclusion
alone is not enough: the merge is computed OUT of tree, so after `update-ref` the
repository index still describes the pre-merge state and `git commit` would commit
that stale entry verbatim for exactly the paths the add excludes. Materialize
therefore runs `git read-tree <merged-tree>` first - plumbing, no `-u`, no file
touched - which is the only spelling that reconciles the index while leaving the
work tree alone, `reset` and `checkout` being forbidden here.

**The queue is drained at the top of every `sync --once` pass**, for every
enumerated workspace, before derive/stage/commit; the watcher's passes drain the
same way, and a pass that finds queued changes with no drain wired REFUSES (exit 3)
rather than staging as if nothing were pending. The drain is a RE-CHECK, not a
replay: each entry records the ours-side blob (the on-disk bytes at defer time) as
well as the merged blob. If the file still holds those bytes, the merged result
lands or the file is removed. If it does not, the session edited it AFTER the merge
and the later edit wins: a replace is merged three-way (base = the queued bytes,
ours = disk, theirs = the merged blob) with the disk side taking a real body
conflict, and a deletion is abandoned and reported `resurrected` - deletion-table
row 3, one pass late, with the deleted side still reachable in history. An entry a
live session still blocks stays queued. A queue that cannot be read is an error and
never an empty queue: the drain refuses and the workspace is not staged.

The receipt carries both ends. `deferred` names each withheld change with its OP
(`[{"path":...,"op":"delete"}]`, where every entry used to be reported as
`replace`), and `deferred_applied` names what a drain landed, so the audit trail
does not show changes going into a queue and never coming out.

`MEMORY.md` is never tracked and never merged. Two guards hold that: the
`info/exclude` entry and the `:(exclude)` pathspec that both staging passes share,
because either one alone would still keep it out and a mutation that removes only
one would look tested.

**Staging is two passes over one exclusion list.** The first is the forced add,
narrowed to `*.md`, and it is the only thing that may ADD a path: the force that
gets fact files past the blanket exclude would otherwise track whatever else is in
the directory, and the PowerShell compactor leaves `.bak-<date>-<kind>` files
beside the index. The second is `git add -u` over the store, TRACKED paths only and
any extension, because narrowing the add also stopped git ever noticing that the
artifacts ALREADY tracked are gone - a `.bak-<date>-<kind>` moved out of a store
stayed tracked with its stale bytes forever and would materialize onto every other
PC. `-u` never adds a file, so what may be added stays exactly as narrow as it was,
and the deferred queue is excluded from both passes. An unmatched pathspec is the
ordinary answer for an empty store in either pass, and the two spell it
differently, so the shared prefix is what is matched.

A store that is gone has its deletion staged, which is the one way a store leaves
the fleet. The stat is the STORE directory (`<ws>/memory`), not the workspace: a
store is recognised by its `MEMORY.md`, so a workspace whose memory directory was
deleted is never enumerated, was therefore never in the live set, and a stat of the
workspace kept succeeding and skipping it - its fact files stayed tracked with
stale bytes forever. Only `IsNotExist` counts as a removal; any other stat error
leaves the store tracked, which keeps the original caution against propagating a
fleet-wide removal off one bad read and makes it exact.

**`sync --watch`** is one singleton watcher per PC, not one per session. It holds
`watch.lock`, wakes on the dirty marker through a filesystem watch, and holds no
timer and does no network while idle. A second instance exits 0 silently, because
a session starting while the watcher already runs is the normal case.

**`gate`** is the PostToolUse hook and it returns 0, always - a hook that can fail
a `Write` is a hook that can stop the operator working, so every error path is
swallowed and the worst case is silence. It advises under the caps, normalizes an
index that has crossed the sync limit through the same floor `derive` uses, commits
locally and marks the tree dirty. It never touches the network: the watcher is what
carries the change to the hub. That claim is proved structurally rather than by a
stopwatch - the test points `GIT_SSH_COMMAND` at a recording stub, gives the
fixture hub an ssh URL and fails if the marker file exists, with a companion test
that dials on purpose and fails if the marker is ABSENT, so "no marker" can never
quietly mean "the stub was never reached".

### Network calls are hardened by refusal, not by convention

Every reachable network git call already carried the hardening environment
(`BatchMode=yes`, `ConnectTimeout=2`, `StrictHostKeyChecking=yes`, a pinned
`known_hosts`) - except the merge engine's own `Fetch`/`Push`, which built their
commands without it. Rather than add a fourth copy of the same literal, one helper
in `gitx` owns that environment and `gitx.Run` REFUSES, before the process starts,
any network subcommand (`fetch`, `push`, `ls-remote`, `clone`, `pull`,
`remote update`, including behind global options like `-c` and `--git-dir`) whose
assembled environment carries no non-empty `GIT_SSH_COMMAND`. A local plumbing call
is untouched by the guard. The single source of truth is now structural: a new
network call site cannot forget the hardening, because forgetting it does not dial
- it fails. (`GIT_TERMINAL_PROMPT=0` stays set universally.)

The known_hosts path in that command is always single-quoted. git runs `GIT_SSH_COMMAND` through
`sh -c`, and an unquoted Windows path loses every backslash on the way - the first live hub push
read a known_hosts that did not exist and strict checking refused the hub. A test runs the emitted
option through `sh -c` and asserts ssh receives the exact path.

**`lint`** is read-only by contract and never writes inside a store. It carries the
shipped rules (orphan, dangling, dup-slug, long-line, oversized-file, budget
findings, `compactor-starved`, `compactor-unproductive`) and adds `resurrected` and
`conflict-in-history`, which it reads from the sync receipts and reports with the
losing commit id so the loser is recoverable. `compactor-silent` is parameterised:
on a PC the finding does not exist, because there is no local nightly to be silent;
on the hub `--nightly-unit` names the systemd timer. The G7 clock - hours over
trigger without an applied decision - comes from `.ams/over-trigger.json`, which
rides in the synced tree outside every store. `derive` and `sync` write it; lint
only reads it.

**A cleared clock stays cleared.** The reducer takes the earliest crossing any PC
saw, but a clear is an ABSENCE, and a union-of-keys minimum could never represent
one: the store converged, the local PC deleted its key, and the next merge with any
PC that had not re-derived brought the old clock straight back, so the G7 alarm
could never reset. The file therefore carries a second object, `cleared_at`, one
tombstone per workspace. The reducer takes the MAX of the tombstones first (a
monotone step, so either reduction order yields the same bytes), then the MIN over
only the stamps strictly NEWER than their workspace's clear; a dead stamp is
dropped, the tombstone is kept even when nothing survives it, and a PC that
re-crosses the trigger after a clear stamps a fresh time that outlives it. An
unparseable time on either side leaves the stamp ALIVE, so a garbled tombstone
cannot silence a starvation alarm. A file written before the tombstone existed
reads correctly (a missing `cleared_at` is empty) and is rewritten into the new
shape on the first stamp change or merge.

**One renderer owns those bytes.** The producer wrote the file with indented JSON
while the merge reducer rewrote the same tracked file compactly, so every stamp
change cost an extra commit and no two PCs agreed on the bytes until a merge had
run. `lint` now projects its typed stamps onto the merge type and writes through
the reducer's own renderer at RFC3339 second precision, so one crossing renders
identically on two machines, and it prunes any stamp the tombstone already
resolved so it cannot emit bytes the reducer would rewrite.

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

### Release assets and install (P4-1a)

The binary is never built on a PC and never committed. A pushed tag `v<VERSION>` runs
the `release-assets` job in `ci.yml` (every other job skips tags): it refuses a tag
that does not equal `v` + the `VERSION` file at that commit, cross-compiles
`ams-store-windows-amd64.exe`, `ams-store-linux-amd64` and `ams-store-linux-arm64`
with `CGO_ENABLED=0` and the ldflags above (the version stamp IS the tag, so
`--version` on a PC says exactly what the installer expected), writes `SHA256SUMS`
over the three, and attaches all four files to the GitHub release of that tag.

| OS | Path | Installed by |
|---|---|---|
| Windows | `%USERPROFILE%\.claude\scripts\ams-store.exe` (beside the hooks, with a `.sha256` sidecar) | `install/2-windows-config.ps1` (1.25.0) |
| Linux client / replica | `~/.local/bin/ams-store` | `install/linux-client.sh`, `install/linux-replica.sh` (P4-3) |
| Linux authority / hub | `/usr/local/bin/ams-store` | `install/linux-authority.sh` (P4-1b) |

What the Windows installer does, in order, and why the order matters:

1. **Binary first, before the receipt.** It downloads `SHA256SUMS` and the Windows
   asset for the tag `VERSION` names, verifies the digest, and swaps the file in by
   rename (a resident watcher may hold the old image open; Windows refuses to
   overwrite a running image but allows a rename). A deployed binary whose digest
   already equals the release's is left alone. `-BinaryPath` + `-BinarySums` is the
   offline drop; an offline re-run keeps an installed binary that is already the tag
   and matches its own sidecar. Anything else aborts the run before the receipt is
   rewritten and before a hook is registered: `settings.json` must never point at a
   missing exe, and the nightly must never be removed under a missing gate.
2. **The hub transport (`-HubHost`, inherited from the receipt).** The installer
   writes the `Match host <hub> user ams-hub` block into `~/.ssh/config` between
   marker lines (idempotent; the remote policy pins the user@MagicDNS URL form and
   ssh has no other way to bind that user to the hub's identity file), seeds the hub's
   host-key lines from the user's `known_hosts` into `<STATE_ROOT>/known_hosts` (the
   hardened `GIT_SSH_COMMAND` pins that file with strict checking), creates the
   history repo on `main` if it is missing, renames a pre-binary `master` to `main`,
   and makes `hub` its one remote. The sync hooks are registered ONLY when the
   identity key exists and at least one host-key line was seeded: a strict-checking
   failure at SessionStart is silent by contract, so the refusal is loud here instead.
3. **The nightly goes.** Behind that proven path, and only there, the installer
   unregisters `ClaudeCode-MemoryCompactor-5am`; the spawner script no longer launches
   the `-CatchUp` child. A box that cannot prove its hub path keeps the legacy
   nightly, because a store with no judge at all only ever shrinks by truncation.
   `ams-store` takes the legacy mutex beside its own, so the two never race while
   both exist.
4. **Hooks.** PostToolUse `Write|Edit` runs `ams-store gate` (registered over the two
   legacy markers, so the bash lint and the PowerShell gate are replaced in place);
   SessionStart runs `ams-store sync --once --hub-host <hub>` asynchronously (the
   network is never on a hook's critical path) and the maintenance spawner, which
   launches `ams-store sync --watch --hub-host <hub>` hidden and detached (the
   singleton exits at once when one is already running); SessionEnd runs
   `sync --once` too. The receipt records `HubHost`, `AmsStoreTag`, `AmsStoreSha256`
   and `AmsStoreSource`.

`0-prereqs.ps1` parses `git --version` and requires 2.38 or newer. `3-verify.ps1`
compares `--version` to `v<VERSION>`, the binary's digest to its sidecar, asserts the
PostToolUse entry is the binary (exactly one stack entry), the SessionStart and
SessionEnd sync entries and the watcher line in the spawner, runs
`sync --once --hub-host <hub> --json` and expects exit 0, and asserts the compactor
task is gone. Every installer run passes every flag its receipt recorded or relies on
an inherit rule a Pester scenario pins.

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

For each rule in the table it copies the target file aside, applies one minimal
change that breaks that rule, **builds**, runs the named test alone expecting it to
FAIL, and restores the file with a byte compare against the pre-image. The build
step is not ceremony: a mutation that does not compile reads as a red test while
proving nothing. Neither is the byte-verified restore - the gate's first ever run
used WSL git against a Windows worktree checkout, every `git checkout --` failed
silently, fifteen mutations stacked on each other, and it reported four rules
SURVIVED that had never been tested in isolation.

**The table itself is now anchored by a test.** `TestPorting_EveryMutationAnchorStillExists`
asserts that each hunk's `Old` text occurs EXACTLY once in its file, byte for byte
and by the same comparison the applier makes, and
`TestPorting_EveryMutationNamesATestThatExists` asserts every named test is in the
`go test -list` set. The table had rotted silently once already: a refactor moved a
pathspec into another package, the gate refused that row as STALE, and nothing
automated noticed - so the rule "MEMORY.md is untracked everywhere" was documented
as defended while no mutation could reach it. A `go test ./internal/porting` now
fails the moment an anchor moves, which is also what caught the re-anchoring this
round needed.

Measured state, and the figure is stamped with the commit it was measured at
because this gate is local-only and drifts between runs: **red 29, survived 0, broken 0, pending 0 at `29fbd5a`** (windows/amd64, git 2.55.0, whole table, exit
0), which is the last commit of that round to touch Go source. A SURVIVED rule means nothing tests it; a NOCOMPILE or STALE entry means the
mutation is broken. Both were hit while arming the last ten and both are reported
separately for that reason, and the figure that stood here before (red 25) was
never true at the commit it was written at - the gate reported red 23, survived 1,
broken 1 there. The four deletion-table rows added since take the table to 29.
