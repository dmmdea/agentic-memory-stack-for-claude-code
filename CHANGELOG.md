# Changelog

This repo is the PRIMARY source for the agentic-memory-stack product; this file is the
product's version authority as of v1.17.0 (the earlier private-side history is summarized
in the first entries below — full pre-inversion history lives in the maintainer archive).

## 1.26.2 — the corpus partition never reached the applier, so every migration failed on its own (register P4-1b follow-up)

Found by a live run, not by a test. Against a purpose-made store and a hand-written plan, `judge-apply`
reported `dry-run: migrated 1` and then, applying for real:
`mem0 add: HTTP 500: {"detail":"Invalid user_id: cannot be empty or whitespace-only"}` — the fact was
kept, the receipt named the orphan, and the verb exited 0. The authority partitions the corpus by
`user_id`; the applier reads that partition from `MEM0_USER_ID`; **nothing in the deployed chain ever
set it.** `ams-step.sh` resolves the authority URL for every step (`ams_env.mem0_url`) but not the
partition, and the step unit carries the credential only. Every MIGRATE decision the nightly judge
made would have failed one request at a time, been receipted `line kept`, and the night would have
exited 0 — forever, with every test green.

Two halves, because the wiring and the refusal are different defects:

- **`ams-step.sh` exports `MEM0_USER_ID`** beside `MEM0_URL`, from `MEM0_DEFAULT_USER_ID` and then
  `MEM0_WSL_USER` in `~/.mem0/stack.env` — the precedence `ams_env.user_id()` already uses — with an
  explicit environment value still winning.
- **`judge-apply` refuses to build a corpus client without a partition**, exactly as it already refuses
  without an authority: one line on stderr, migrations reported as not performed, nothing posted. A
  client built with an empty partition turns one misconfiguration into one failure per fact, which is
  precisely how this stayed invisible.

Three tests, each seen red against the shipped code: a fake authority proves that nothing is attempted
when no partition is configured and that the fact survives; a second proves the configured partition
reaches the wire as `user_id` and the migration completes; the chain test proves the export and its
precedence.

## 1.26.1 — two reasons the nightly judge would never have written a plan (register P4-1b follow-up)

Both found by reading the DEPLOYED authority minutes after 1.26.0 installed, not by any test, and both
silent: the phase would have logged a skip and the applier would have run the deterministic path
forever, nightly, with every test green.

**1. The producer could not see the checkout.** The plan is written by `ams-step-dream.service`, whose
unit carries the credentials and the judge transport but **not** the store variables — those were added
to `ams-step-store-judge.service`, the applier. `systemctl --user show ams-step-dream.service -p
Environment | grep -i ams` returned nothing. `_ams_checkout_root()` and `_ams_store_bin()` now read the
environment first and then `~/.mem0/stack.env`, the precedence every other install value uses
(`ams_env.eval_root`), and the installer records `MEM0_AMS_STORE_BIN` beside `MEM0_AMS_CHECKOUT`.

**2. The schema it validates against was not deployed.** `validate_plan` refuses to write a plan it
cannot validate, and the schema lives under `docs/` — it is the published contract, generated from the
Go types — so the installer's `scripts/wsl/*` glob never carried it. On the authority:
`validate_plan says: 'the judge-plan schema is not deployed beside this script'`. `linux-authority.sh`
now copies `docs/schemas/judge-plan.schema.json` into the scripts directory beside the consolidator.

Each fix has a test that fails against the shipped code: one drives the phase with **nothing** in the
environment and only the installer's `stack.env` on disk; the other asserts the copy and that the
generated schema is checked in at all.

## 1.26.0 — the hub decides: the store-judge step, the hub checkout, and a generated plan contract (register P4-1b)

The nightly judge the fleet-store design promised is wired. `dream-consolidate.py` gains a **store
judge** phase between the autonomous promotion and the prune: for every store in the hub's checkout it
asks the binary for the offer set (`judge-apply --candidates --json`, which already excludes doctrine
and sealed lines), calls the judge once per store that has something to decide, and writes a plan.
A store with nothing to decide is `outcome: ok` with no decisions and no call - a judge that kept
everything is a successful plan, and the applier receipts it `no-op`. (`empty` is reserved for a call
that answered with whitespace, which on an over-trigger store the applier records as
`skipped-judge-unavailable`; using it for "nothing was offered" would have reported healthy stores as
failing judges to lint's `compactor-unproductive` watchdog - found by rehearsing the wrapper against
the real binary before shipping.) A failed call is `unavailable`; unparseable output is `parse_fail`. Decisions naming a slug that was never offered, repeating a slug,
or carrying the wrong fields for their verb are dropped by the producer, so one bad line cannot make
the applier refuse the whole file. The plan is validated against the schema **before** it is written
and a plan that does not validate is not written at all - a missing plan is a deterministic-only
night, which is strictly better than a malformed one.

**The contract is generated, not documented.** `docs/schemas/judge-plan.schema.json` is produced by
`go run ./scripts/planschema` from `internal/judge/plan.go` - verbs, outcomes, slug rule and version
read from the package, never retyped. Three tests hold it: the generator's file must be current, the
Go decoder's verdict on a shared 26-document corpus must match, and the schema's verdict on the same
corpus must match AND stay a subset of the decoder's (a schema stricter than the decoder makes the
producer refuse to write a plan the consumer would have applied - the nightly then stops deciding
with nothing failing anywhere).

**The chain step.** `ams-step-store-judge.service` runs after `ams-step-dream` and before
`ams-step-index-refresh`, through `ams-step.sh --guarded` like every step: it applies the plan store
by store with `ams-store judge-apply`, then syncs once. A missing plan skips the apply and still
syncs, so the deterministic floor lands on a night the judge never spoke.

**The authority installer** gains `--ams-checkout` and `--ams-hub` (both inherited from `stack.env`
on a re-run): it installs `/usr/local/bin/ams-store` from the linux/amd64 release asset of the tag in
`VERSION`, checksum-verified (`--ams-store-binary`/`--ams-store-sums` for an offline drop), and
prepares the checkout - `role` = `hub`, an ssh `Match` block, a seeded `known_hosts`, a history repo
on `main` with `hub` as its one remote, reached by the same `user@<magicdns>:repo.git` form every PC
uses. A box that configures neither flag gets no binary, no checkout, and the step is dropped from
the rendered unit set rather than enabled with nothing to judge.

Also: `main()` in the consolidator threaded every injected collaborator except `now`, so a test that
pinned the clock silently got the real one - the exit-code scenarios had been passing for a reason
unrelated to exit codes since their fixture aged out of the 36 h window. Fixed, and the dream suite
is now in CI (it never was, which is why that rotted unnoticed), along with the new schema suite and
`jsonschema` in the CI dependency line.

## 1.25.2 — a fresh checkout derives its index instead of failing (register P4-1b/P4-2 prerequisite)

`MEMORY.md` is derived and never tracked, so a checkout that has just materialized its stores from the hub holds
fact files and no index. `derive` read that index fail-closed, so the FIRST sync of any fresh checkout - a new PC,
or the hub's own checkout on the authority - materialized every store and then died with
`merge failed: read index ...: The system cannot find the file specified`, exit 5, leaving the stores on disk with
no index at all. Found by rehearsing the hub checkout against the live hub before wiring it (five stores, 350 fact
files materialized, zero indexes). A missing index is now an empty one: `derive` renders it from the fact files
(nothing to harvest, every file re-indexed from its frontmatter hook) and the compare-and-swap treats "still
absent" as unchanged while an index that appeared mid-run still aborts; `harvest` does the same. Any other read
error stays fail-closed. Two tests, both seen red against the old read: the derive engine renders an index for a
store that has none and the second pass is a no-op, and a CLI seam test drives a fresh checkout's first sync
against a bare hub end to end.

## 1.25.1 — the session-start line reports the G7 clock; the store lint runs at session start (register P4-1c)

The design's Phase 4 induced test asks for "the session-start line reporting the metric". `ams-store sync --once`
is silent on stdout by contract and the SessionStart hook runs it asynchronously, so the line is the SessionStart
banner's: `claude-config/storage-cap-check.sh` now prints, per store, the hours over trigger without an applied
decision (`stores[].over_trigger_hours` from `lint-summary.json`) - `auto-memory G7: over trigger <ws> <h>h` below
24 h, `AUTO-MEMORY G7 ALARM: …` at or above - and its stale-summary wording no longer names the PowerShell lint.
The maintenance spawner runs the binary's lint (`ams-store lint --summary-out <state>/lint-summary.json
--hub-host <hub>`) instead of `memory-lint.ps1`, which stays as the fallback only while the binary is absent:
both write the same summary, but only the binary's lint fills the G7 field (the PowerShell lint writes `null`).
Tests: a new banner suite runs the real script with a fixture summary (quiet line, alarm line listing the worst
store first, silent when nothing is over trigger, staleness instead of a stale clock, bash syntax);
InstallerParity and RegressionGuards pin the spawner's lint child and its fallback branch. The isolated induced
G2 test (a 32,646 B index written past the gate, floored to 19,896 B by the session-start pass, 130 hooks
harvested first) is recorded in the workspace receipt for P4-1c.

## 1.25.0 — ams-store into the Windows install (register P4-1a)

The Windows installer now installs the store binary and cuts the PC over to it. `2-windows-config.ps1`
downloads `ams-store-windows-amd64.exe` from the GitHub release of the tag `VERSION` names (the new
`release-assets` job in `ci.yml` cross-compiles windows/amd64, linux/amd64 and linux/arm64 on a pushed
`v*` tag, refuses a tag that disagrees with `VERSION`, and attaches the three binaries with `SHA256SUMS`;
every other CI job skips tags), verifies the asset against `SHA256SUMS`, installs it beside the hooks
with a `.sha256` sidecar, and aborts before the receipt and before hook registration when it cannot
(`-BinaryPath` + `-BinarySums` is the offline drop; an offline re-run keeps an installed binary that is
already the tag and matches its sidecar). It then writes the hub transport the seed did by hand: the
`Match host <hub> user ams-hub` ssh block (idempotent, between marker lines), the hub's host key into the
binary's own `<state>/known_hosts`, a history repo on `main` with `hub` as its one remote in the
user@MagicDNS form. It registers `ams-store gate` on PostToolUse over the two legacy markers (the
PowerShell gate is replaced, never duplicated), `ams-store sync --once` at SessionStart (async) and at
SessionEnd, and the maintenance spawner, which now launches the resident watcher (`sync --watch`, one
per PC) instead of the compactor catch-up; and it removes the 5am `ClaudeCode-MemoryCompactor-5am`
task. The sync hooks and the task removal happen only behind a proven hub path (`-HubHost`, inherited
from the receipt; the identity key present; the host key seeded) - a box that cannot prove it keeps
its legacy nightly and the installer says so in red. The receipt records `HubHost`, `AmsStoreTag`,
`AmsStoreSha256` and `AmsStoreSource`; `0-prereqs.ps1` requires git >= 2.38 (parsed, not merely
present); `3-verify.ps1` compares `--version` to the tag, the binary to its sidecar, the hook table to
the binary, runs `sync --once --json` and asserts the compactor task is gone. Pester: the installer
suites are extended (InstallerParity, RegressionGuards, AuthorityResolution) and a new
`AmsStoreInstall.Tests.ps1` runs the checksum parser, the ssh block writer, the known_hosts seeder and
the history-remote initialiser for real against a TestDrive, plus the Q-F scenario that the PowerShell
receipt reader tolerates the Go binary's rows. The PowerShell originals stay deployed until the Phase 5
gate deletes them.

### ams-store: the known_hosts path survives the shell git runs ssh through

The first live push to the hub (the P3-4 seed, minutes after 1.24.0 merged) failed with "No ED25519
host key is known" although the state-root known_hosts held the right key: `gitx.SSHCommand` quoted the
`UserKnownHostsFile` path only when it contained a space, git hands `GIT_SSH_COMMAND` to `sh -c`, and the
shell ate every backslash of the Windows path, so ssh read a file that does not exist. The path is now
always single-quoted (an embedded quote is closed, escaped and reopened), pinned by a test that runs the
emitted option through `sh -c` and asserts the shell hands ssh the exact path - with backslashes, spaces
and a quote. The fleet tests never saw it because their remotes are local paths and ssh never runs. No
runtime or version change; the binary is rebuilt from this commit.

## 1.24.0 — ams-store engines (System A store client, register P3-1/P3-2)

The Go rewrite of the auto-memory store library, write gate and nightly compactor begins
here. This change adds the `ams-store/` module: store enumeration (fail-closed, reparse-point
dedup, OS-gated case folding), the atomic writer, the `gitx` git wrapper (with the
`git >= 2.38` check for `merge-tree --write-tree`), frontmatter + hook harvest, the doctrine
rule, and the index parse/render core with the design's derived order (fixed heading, doctrine
first, commit-time descending, slug tiebreak, always LF). Every verb is a stub (`not
implemented`, exit 64); the engines land in the rows that follow. The 1:1 Pester-counterpart
table is seeded (94 of 95 scenarios named; the removed catch-up-spawn scenario is the one
exemption). Two Go CI jobs added (linux with `-race`, windows build+test). Measurements
(receipt in the workspace): the Go gate spawns ~26x faster than the PS 5.1 gate (~15 ms vs
~397 ms p50); the SessionStart hook order is not a fixed before/after. New system doc
`docs/systems/ams-store.md`. No runtime/version change to the mem0 stack — `VERSION` is
unchanged.

**The engines (this change).** No verb is a stub any more. `derive` and `harvest` (harvest,
the four hygiene passes, planned-ghost abort, blast cap, derived render with the injection
stop, convergence floor, compare-and-swap write); the merge engine (out-of-tree three-way
merge, deletion table, field-aware frontmatter merge, commit-time winner with the machine-id
tiebreak, CRLF normalization, materialize order, deferred queue, liveness); `sync` /
`sync --watch` / `lock` / `gate` / `lint`; and the hub-only `judge-apply` with every
apply-guard and the `Migrated:` trailer.

- **The seams are connected and tested as pairs.** Each engine was built against a one-method
  interface and a fake, which is what keeps the packages independent and also what makes a
  disconnected seam invisible: `gate.Options{Floor: nil}` compiles, ships and passes every
  unit test while the write gate silently becomes an advisory printer. `cli/seams.go` is the
  one place they meet and each adapter carries an end-to-end test driving the real pair.
- **Three duplications collapsed, each of which decided behaviour.** `internal/sync` and
  `internal/merge` both wrote the history repo's `info/exclude` and config, and disagreed
  about whether the shared over-trigger stamp was trackable — whichever ran last won.
  `internal/derive` carried a second copy of the anchor rule. `cli` had two global-flag
  structs, one of which left the state root empty when the home could not be resolved. One
  floor, one doctrine rule, one anchor rule, one repo shape.
- **The empty merge base must be a commit, not a tree.** The first sync between two PCs that
  each ran `git init` before either had pushed works on git 2.55 and fails outright on git
  2.43 (`object ... is a tree, not a commit`). The design's floor is 2.38, so the version
  that refuses is inside the supported range; found by the mandatory Linux `-race` run, which
  is the only place the other git version is exercised.
- **Staging is narrowed to fact files.** The forced pathspec that gets fact files past the
  blanket exclude also tracked whatever else was in the store directory, and the PowerShell
  compactor leaves `.bak-<date>-<kind>` files there; a live sync put several into history on
  their way to the hub and from there into every agent's glob. Nothing is untracked or
  deleted — that is a decision for a human.
- **A store whose whole workspace directory is gone now has its deletion staged**, which is
  the one way a store leaves the fleet; and the history repo pins a repo-local empty
  `core.hooksPath` so a global hooks path aimed at GitHub pushes does not refuse the hub push.
- **The G7 over-trigger clock is written.** `derive` and `sync` stamp
  `.ams/over-trigger.json`; nothing wrote it before, so `over_trigger_hours` was null on every
  PC and the 24 h alarm was inert.
- **The parity gate is a test, not a table in a plan.** It reads the four Pester files,
  extracts all 95 `It` blocks and asserts a Go test of the mapped name exists, with exactly
  one exemption carrying its reason inline. A companion check refuses any remaining
  placeholder skip, because a skipped test is still a test to `go test -list`. Two more repo
  gates: every package that can resolve the operator's home runs its tests with the home
  moved (two live incidents on 2026-09-15 came from tests that did not), and Go source stays
  ASCII.
- **The mutation gate is armed on every rule.** Ten rules carried a name but no mutation and
  were reported rather than checked. Arming them caught one mutation that did not compile (a
  build failure reads as a red test while proving nothing) and one that SURVIVED (it added a
  fetch before the local commit but ignored the error, so the rule was never actually broken).
  The measured figure is in the repair round below; the figure first written here (red 25)
  was never true at that commit.

**The repair round (this change).** A three-lens review of the engines found two severe
defects at the merge/sync seam, four moderate ones and a stale claim in these notes. Each fix
landed with a test seen RED against the unfixed code first.

- **The deferred queue is a pending merge result, not a note.** A queued path was re-staged by
  the next blanket add and re-committed, resurrecting a withheld deletion fleet-wide and
  re-committing a live session's older bytes over a merged blob. Staging now excludes every
  queued path, and materialize reconciles the repository index with `read-tree` first, because
  the merge is computed out of tree and `git commit` was committing the stale index entry for
  exactly the paths the add excluded.
- **The queue is drained.** `ApplyDeferred` had no production caller at all: the queue was
  written and never read. Every `sync --once` pass (and every watcher pass) drains it first,
  for every enumerated workspace, and a pass with queued changes and no drain wired refuses.
  The drain is a RE-CHECK against the ours-side blob recorded at defer time, not a blind
  write: an unchanged file takes the merged result, an edited one keeps the later edit (a
  replace is merged three-way with the disk side winning a real conflict, a deletion is
  abandoned and reported `resurrected`). A corrupt queue is a refusal, never an empty queue.
  The receipt names each withheld change's OP - every one of them used to be reported as
  `replace` - and lists what a drain applied.
- **Staging is two passes over one exclusion list.** The narrowed forced add still may add only
  `*.md`, and a second `git add -u` stages the removal of TRACKED paths of any extension, so a
  `.bak-<date>-<kind>` moved out of a store stops being tracked with stale bytes forever. A
  store whose memory directory is gone is now recognised as gone: the sweep stats the STORE
  directory, since a workspace whose store was deleted is never enumerated and the workspace
  stat kept succeeding.
- **A cleared over-trigger stamp stays cleared.** The G7 clock's reducer was a union over keys
  and a clear is an absence, so a converged store's cleared stamp came straight back from any
  PC that had not re-derived and the alarm could never reset. The file carries a `cleared_at`
  tombstone per workspace; the reducer maxes the tombstones, then mins only the stamps newer
  than their clear. A garbled time keeps the stamp ALIVE, so a bad tombstone cannot silence a
  starvation alarm. One renderer owns the file's bytes now - the producer indented and the
  reducer compacted the same tracked file, so every stamp change cost an extra commit.
- **A network git call that is not hardened does not dial.** The merge engine's `Fetch`/`Push`
  built their commands without the `GIT_SSH_COMMAND` hardening every other call site applies.
  One helper owns that environment and `gitx.Run` now REFUSES any network subcommand whose
  environment lacks it, before the process starts - the single source of truth is structural
  rather than a convention a new call site can forget.
- **`--engage-at <bytes>`** makes decision Q2's engage threshold a flag on `derive` and `gate`,
  defaulting to today's value, so the Phase 4 flip is a change of a default rather than an edit
  to the floor. The gate's own duplicate copy of the threshold reads it too, or a lowered flag
  would have been accepted and then silently skipped.
- **Four dark rows of the deletion table are executed and mutated.** Both-deleted,
  added-on-ours-only, added-on-theirs-only and the identical-bytes short circuit had zero
  executed statements; they now have end-to-end fleet fixtures and their own mutations.
- **Four tests that could not fail, fixed.** The gate's "never touches the network" claim was a
  wall-clock proxy that a real `git fetch` passed; it is a recording ssh stub with a control
  test now. The lock contender test never reached the file-lock branch on Windows (the named
  mutex short-circuited first), so its mutation SURVIVED. Every `cli` verb took the PRODUCTION
  Windows mutex names, so a real compaction - or a sibling test process - turned sixteen tests
  red and others vacuously green; the verbs take an isolatable lock. The placeholder-skip check
  scanned line by line and a gofmt-wrapped `t.Skip` evaded it; it parses the file now.
- **The mutation table is anchored by a test.** A hunk whose anchor text moved made the gate
  report the row broken and nothing automated noticed. A test asserts each hunk's `Old` occurs
  exactly once in its file and that every named test exists; it caught the re-anchoring this
  round itself needed.

Measured mutation gate at `29fbd5a` (windows/amd64, git 2.55.0, the whole table, exit 0):
**red 29, survived 0, broken 0, pending 0** - four rows more than the table had, since the
deletion-table repair added its own.

Two lead findings from the seed recon, after the repair round: history repos on Windows now pin
`core.longpaths=true` (a live projects root holds four workspace directories over MAX_PATH, and a
store under one would have been unstageable), and `derive --dry-run` no longer promises to
"write nothing" - it never touched the store or the dirty marker, and its receipt row, flagged
`dry_run`, is the compactor's contract (lint skips such rows); the cli test pins that shape.

`VERSION` moves to 1.24.0 for the engines. Phase 4 wires the binary into the installer.

## v1.23.5 (2026-09-15) — an explicit empty flag clears an inherited value; prerequisites read correctly over ssh

- **`linux-replica.sh` / `linux-authority.sh`: `--flag ""` clears.** Inherit-on-re-run (v1.23.2–v1.23.4)
  had no way to UNSET a value: re-pointing a replica from a WSL-hosted brain to a native one needed
  `BRAIN_WSL` emptied, and `--brain-wsl ""` inherited the old hop instead. An explicit empty value now
  clears the inherited value and says so; a flag not given at all still inherits.
- **`0-prereqs.ps1` under a non-console session.** `wsl.exe` prints UTF-16, which the default decoder
  renders as NUL-interleaved text that never matches "WSL" (the first remote install read "WSL2
  installed MISSING" on a box with WSL2); the check now decodes it and accepts a clean exit code. The
  Claude CLI check accepts the native installer's `~\.local\bin\claude.exe` and anything on PATH.

## v1.23.4 (2026-09-15) — the last members of both classes, found by an independent seat audit

A clean-context audit of the whole repo (the local 27B seat, two contracts) after v1.23.2/v1.23.3:

- **`linux-replica.sh` inherits `--brain-backup-dir` and `--brain-wsl` from `~/.mem0/replica.env`.**
  The backup dir had a non-empty default persisted into the receipt and never read back, so a
  re-run without the flag rewrote a custom remote backup dir to `~/.mem0/backups`; an omitted
  `--brain-wsl` blanked the WSL hop of a Windows-hosted brain.
- **`stamp-retired-at.py` resolves the authority through `ams_env`** (`MEM0_URL` >
  `~/.mem0/authority-url` > loopback; credential > key file) instead of a bare loopback literal.
- **`1-wsl-services.sh`'s post-install health probe follows `MEM0_BIND`**, and the Windows dream's
  brain-side `/health/deep` line goes through `Get-Mem0AuthorityUrl`.
- Static pins for all three in `test_loopback_probe_pins.py`; replica-installer test for the inherit.
  Left as-is by design: `3-verify.ps1`'s "brain, local authority" check and `restore-replica.*`,
  which probe a WSL brain's / a replica's own local store.

## v1.23.3 (2026-09-14) — the deploy path on a dormant replica; the authority re-run restarts its server

Both found by the v1.23.2 live deploy, both members of the same brain-assumption class.

- **`deploy.sh` on a dormant replica byte-compiles and stops before the import smoke.** `import app`
  opens the Qdrant connection at import time, so the smoke can never pass while a replica's stack
  is dormant; v1.23.2 placed the role gate after it and both replicas stopped there with their
  files already synced. The gate now runs first: a dormant replica gets `py_compile` of the synced
  modules and exits (its real smoke is the `/health/deep` gate `restore-replica` runs when the
  watcher brings it up); a live travel-mode replica still goes through the smoke and restart.
- **`linux-authority.sh` restarts `mem0.service` on every run.** `enable --now` leaves an
  already-running server on the old code: the v1.23.2 re-run stamped `VERSION` 1.23.2 and `/health`
  kept reporting 1.23.1. `restart` also starts an inactive unit, so a first install is unchanged.

## v1.23.2 (2026-09-14) — re-runs inherit every flag; no probe hard-codes loopback

The v1.23.1 tenant fix was one instance of two classes; this release closes both classes.

- **`linux-authority.sh` inherits every optional flag on a re-run**, not only `--user-id`: an
  omitted `--embed-model`, `--eval-root`, `--pcloud-dir` or `--zfs-dataset` keeps the value in
  `~/.mem0/stack.env` (the dataset is now recorded there too; a pre-v1.23.2 box inherits it from
  the installed drop-in). A re-run without `--embed-model` used to revert the embed model to the
  stock name — the exact wrong-conversion defect of Session 3 (searches score noise while
  `/health/deep` stays green) re-created by the installer itself; an omitted `--eval-root` silently
  dropped the drift canary, an omitted `--zfs-dataset` the pool-usage check.
- **`linux-replica.sh` and `linux-client.sh` inherit the tenant** (`stack.env`, else the client
  receipt); only a first install falls back to the login name, which differs from the tenant on
  every native box in this stack.
- **`memory-compact.ps1` posts, reads back and deletes through `Get-Mem0AuthorityUrl`.** Its three
  mem0 calls were the last hard-coded loopback probes under `scripts/windows`: on a replica they
  hit the dormant local store, and during an outage would have migrated facts INTO the disposable
  replica.
- **`deploy.sh` honours the role:** on a replica whose local mem0 is dormant it syncs the files and
  stops — the v1.23.1 deploy on the first demoted box restarted (started) that dormant mem0 and
  health-gated a store nobody reads; a live travel-mode replica is restarted on the new code and
  skips the retrieval-families gate, which judges the authority's store.
- **`deploy.sh`'s health gate and retrieval gate follow `MEM0_BIND`** (same rule as
  `stack-promote.sh` since v1.23.1); **`mem0-canonize.sh`** resolves `MEM0_URL` >
  `~/.mem0/authority-url` > loopback like every chain job, so a hand run on the native authority
  reaches the server.
- **`2-windows-config.ps1` removes a stale loopback user-scope `MEM0_URL` on a replica** (the
  residue of the pre-v1.23 offline watcher, and the second fallback of every hook resolver).
  A remote value is an operator's choice and stays.

## v1.23.1 (2026-09-14) — five defects found live during the first workstation cutover

- **`linux-authority.sh` inherits the tenant on a re-run.** An omitted `--user-id` now takes the
  tenant already in `~/.mem0/stack.env`; only a first install falls back to the login name. The
  cutover re-run without the flag rewrote the tenant to the Linux login and every search ran as
  the wrong user (canaries 0/7 against a healthy store).
- **`3-verify.ps1` timer checks are role-aware.** The WSL installer disables `decay-scan.timer` /
  `stack-backup.timer` on a replica by design; verify now expects that instead of reporting MISSING.
- **`restore-replica.ps1` fails loudly.** Every artifact must be readable from WSL (a streaming
  drive such as pCloud's `P:` passes `Test-Path` but is not mounted in WSL — the script had
  announced the old collection's count as "restored" with nothing restored), and the restored
  point count must equal the set's manifest.
- **The Windows receipt records `AuthoritySsh`** and an omitted flag inherits it, like `AuthorityUrl`.
- **`stack-promote.sh`'s post-promote health check follows `MEM0_BIND`** (the native authority does
  not listen on loopback; the check read "inconclusive" on every rehearsal).

## v1.23.0 (2026-09-14) — Phase 2 code: hooks resolve the per-host authority, queue to the Outbox, canonize on the authority

The workstation half of the System B cutover (register P2-3, P2-7, P2-8; spec §7). Nothing here
moves the authority by itself — the installer run that re-points a box does — but after this
every hook on every box follows that file.

- **Every Windows hook resolves its authority from `~\.mem0\authority-url`** (`Get-Mem0AuthorityUrl`
  in both libraries: file > `MEM0_URL` > loopback, whitelisted), and `2-windows-config.ps1` writes
  that file plus `~\.mem0\role` on the Windows side as mirrors of the WSL files. The SessionStart
  bundle and the session banner follow the same precedence — the banner probed loopback and read
  "still starting" forever on a replica, and the hooks read an env var nothing ever set.
- **A failed hook post is queued, never dead-lettered:** connection failures and retryable
  statuses append an `add` op to the WSL Outbox (the shim's record shape; `replay-ops.py` delivers
  it); deterministic 4xx go to `mem0-post-poison.jsonl`. `mem0-post-failures.jsonl` remains only
  as the fallback for the moment the Outbox itself is unreachable (WSL asleep) and is drained on the
  next run as before.
- **Replica reads fail over to the dormant local store and say so:** the `[MEMORY CONTEXT …]`
  header carries `source=authority:<host:port>` or `source=local-replica` (daemon and inline
  path alike); the SessionStart banner block names its source too.
- **`install.ps1` forwards `-AuthorityUrl` / `-AuthoritySsh` to phase 2 and an explicit `-Role` to the
  WSL phase** (as `MEM0_ROLE`; `wsl.exe -e` passes no environment), so one `install.ps1 -Role replica
  -AuthorityUrl … -AuthoritySsh …` demotes a box on both sides. Without `-Role` the WSL side keeps its
  inherit-never-revert rule.
- **`travel-mode.ps1` / `offline-watcher.ps1` no longer rewrite the user-scope `MEM0_URL`.** The
  hooks' authority file stays pointed at the authority in travel mode, so no hook can post into
  the disposable store.
- **Canonization runs only on the authority (Y7).** `mem0-canonize.sh` refuses to mint a token
  unless `role=brain`; from a replica it forwards its argv over SSH (`BRAIN_SSH` in
  `~/.mem0/replica.env`, written by `install.ps1 -AuthoritySsh` / `linux-replica.sh --brain-ssh`)
  to the new authority-side `ams-canonize.sh` (a transient unit loading both `systemd-creds`
  credentials on a native box); unreachable → a `canonize` Outbox op, executed over SSH at replay
  time with a token minted on the authority and confirmed per fact in
  `canonize-confirmations.jsonl`; the session banner reports queued/drained counts.

## v1.22.3 (2026-09-11) — deploy.sh keeps the native chain units off WSL hosts

`scripts/wsl/deploy.sh` copied every `systemd/*.service|*.timer` — including the `ams-*` units of the
native authority, whose `LoadCredentialEncrypted` lines carry a `__SECRETS_DIR__` sentinel only
`install/linux-authority.sh` resolves — onto whatever host ran it. They are now skipped unless
`MEM0_HOST_KIND=native`; the first workstation deploy of v1.22 would otherwise have left inert,
unresolved units on the WSL brain and the replica.

## v1.22.2 (2026-09-11) — the steps after the backup run unguarded

The first v1.22.1 chain run receipted `syncoid`, `pcloud-copy` and `morning-summary` as guard no-ops: they
carried `--guarded` and their predecessor, `stack-backup`, had just stamped the night. Every step after the
stamping step now runs unguarded (all three are idempotent).
The native Codex transport also parses the single-line `tokens used N` that codex 0.154 prints (the
first live native dream had recorded 0 tokens for three real calls).

**`MEM0_EMBED_MODEL` / `--embed-model`.** The staging authority's canaries scored noise (0.05, against
0.65–0.84 on the workstation) because its stock `embeddinggemma` GGUF is a different conversion of
the model than the one the store was embedded with; the two builds' vectors have a cross-box cosine of
0.01–0.06. The design's "reuse the offload stack's copy" assumption was wrong: the authority keeps the
exact GGUF in its dataset, llama-swap serves it under its own name, and mem0 asks for that name
(`config.EMBEDDER_CONFIG["model"]` from `MEM0_EMBED_MODEL`; `/health/embedder` and `/health/deep` follow it).
The installer refuses when llama-swap does not list the model.

## v1.22.1 (2026-09-11) — the first v1.22 deploy on the authority: l10-audit's key, the pool figure

Two findings from the live re-install. (1) `l10-audit.service` runs on its own timer outside the
chain and had no key credential of its own, so on a native box it exited 1 with "no mem0 API key";
the installer now renders `l10-audit.service.d/native.conf` (`LoadCredentialEncrypted` +
`MEM0_API_KEY_FILE`). (2) `/health/maintenance` read the pool as the root dataset's used/avail,
which subtracts slop space and reservations and reported 85.9 % (alarm) against a `zpool` capacity
of 76 %; the pool figure is now `zpool list -Hp -o allocated,size`, the number every receipt quotes,
and the dataset block keeps the `zfs` view.

## v1.22.0 (2026-09-11) — Phase 1 second half: the nightly jobs in Python on the authority, the whole chain, `cold-embedder`, and the first-nights fixes

The native authority now runs every nightly job itself. Nothing is removed: the PowerShell
originals and the workstation tasks stay until the Phase 5 gate; the staging copy is still discarded
at the end of Phase 1.

- **Python ports (register P1-3).** `scripts/wsl/dream-consolidate.py` (orient → gather →
  consolidate → autopromote → prune → drift canary), `autopromote_lib.py` (the 4C promotion gate and
  the nomination pipeline, with 1:1 twins of the three Pester files), `memory-index-refresh.py`
  (the decoupled index refresh) and `codex_usage.py` + `codex-usage-report.py` (usage report,
  plan-window probe, 25 % reserve gate). Every Codex call goes through the native transport and its
  single-flight lock. On the authority the dream's gather input is the store — the last 36 h of
  evidence plus the recent episodes — because workstation transcripts never reach it by design;
  transcripts that exist locally are appended as before. No catch-up script: the timer's
  `Persistent=` and the boot guard cover a missed night.
- **The whole chain (P1-4).** Fifteen `ams-step-*.service` units in spec order (dream → semantic
  dedup → index refresh → goal recurrence → the five Sunday jobs → stack backup → syncoid → pCloud
  copy → morning summary → health stamp → rtcwake). Each Python step loads the API key as its own
  systemd credential; the codex steps pin `CODEX_HOME` to the secrets dataset; the dream also loads
  the canonical credential so autopromotion signs natively. `ams-step.sh --weekly <Day>` gates the
  weekly jobs; `--guarded` (check-only) sits on every step between the first and the stamping
  stack-backup step, so a boot re-run of a completed night is a chain of receipted no-ops.
  `GET /health/morning-summary` serves the chain's summary to session starts.
- **`cold-embedder` on the workstation side (P1-6).** The bundle daemon names a 503 carrying
  `reason: cold-embedder`, waits the server's `Retry-After` (capped) and retries once; the
  SessionStart hook pre-warms the embedder through the new `GET /health/embedder`.
- **First-nights fixes.** Units render every home-relative path as `%h` (the Linux user and the
  tenant differ on a native box: `l10-audit.service` had failed 203/EXEC); the nftables bind belt
  persists through a root oneshot (`ams-nft.service`); `/health/maintenance` reports the POOL
  (the dataset's quota headroom read 2 % while the pool stood at 78 %) plus a `dataset` block and
  the Codex `usage` window; the boot guard is calendar-aware (an evening hand run no longer voids
  the 03:00 night); `mem0.service` gets `CODEX_HOME`; every chain job resolves the authority URL and
  the key through `ams_env.py` (no `~/.mem0/api-key` exists on the authority);
  `mem0-canonize.sh` signs with the systemd credential first. Installer flags `--eval-root`
  (drift canaries) and `--pcloud-dir`.

## v1.21.2 (2026-09-10) — first live chain run: steps enabled, receipt clock, restore WAL hygiene

Three findings from the first chain run on the native authority. (1) `systemctl start
ams-nightly.target` pulled in no step: `WantedBy=` binds a step only once it is enabled, and the
installer enabled only the timer; it now enables every `ams-step-*.service`. (2) Receipts carried
`duration_ms` in nanoseconds: the uutils `date` on Ubuntu 26.04 ignores `%3N`'s width; `ams-step.sh`
now uses bash's `$EPOCHREALTIME`. (3) `stack-restore.sh` restored `episodic.db` beside a foreign
`-wal`/`-shm` pair left by the already-started server, and SQLite reported the file malformed; the
stale pair is removed before the atomic rename.

## v1.21.1 (2026-09-10) — the key guard accepts a symlinked `~/.mem0`

The first native install refused its own `~/.mem0/canonical-key.dpapi`: the path-traversal guard
resolved the symlink into the data dataset and saw a path outside `$HOME`. The guard now also
judges the lexical (normalised, symlink-preserving) path, so the defaults placed under `$HOME`
by the owner pass while `..` traversals are still collapsed and refused. Test pins a symlinked
home directory. Stamps only otherwise.

## v1.21.0 (2026-09-10) — native Linux authority: installer, judge transport, health, nightly chain (Phase 1, staging)

The memory authority can now be installed natively on an always-on Linux box (no WSL anywhere),
ahead of the cutover described in the ADR `fleet-store-sync-and-linux-authority`. Nothing is
removed and no workstation changes role: this release is proven on a staging copy first.

- **`install/linux-authority.sh`** — mirrors the Linux replica installer (same module / pip /
  Qdrant lists read from the WSL installer, uv-managed Python 3.12), binds the server to the
  tailnet address only (`--bind-ip`, never `0.0.0.0`; `wait-for-bind.sh` as `ExecStartPre`),
  loads both secrets through `systemd-creds` (`LoadCredentialEncrypted` in a native drop-in
  `mem0.service.d/native.conf`, `MEM0_API_KEY_FILE=%d/ams-api-key`), accepts ZFS for Qdrant
  storage, enables only `l10-audit.timer` and `ams-nightly.timer`, and turns every per-job timer
  off. `--render-only <dir>` writes the resolved unit set for inspection; a test greps it for
  `/mnt/c`, `cmd.exe`, `powershell.exe` and the DPAPI fetch.
- **`canonical_key_provider`** gains the `credential` source (`$CREDENTIALS_DIRECTORY`, first in
  the chain) and `api_key_path()`; `app.py` reads the API key through it.
- **Native Codex judge transport** — `MEM0_CODEX_TRANSPORT = shim | native | auto`. The native
  path runs `codex exec` as a subprocess behind the shim client's fail-soft dict and retry loop,
  with a file lock as the single-flight mutex; `usage_limit`, `client_timeout`, `exit_nonzero`,
  `lock_contended` and `no_codex` are its error types. `/health/deep` reports
  `checks.judge_transport`. Every judge consumer inherits it unchanged.
- **One nightly chain** — `ams-nightly.timer` (03:00, `Persistent`, `OnBootSec=15min`) starts
  `ams-nightly.target`; steps attach with `WantedBy=`/`After=` (never `Requires=`) through
  `ams-step.sh`, which receipts every run (`{ts, step, ok, exit, duration_ms, receipt_id, note}`)
  and carries the 20 h boot guard. Steps in this release: stack backup, health stamp, RTC re-arm
  (`ams-rtcwake-arm.sh`; the dream, dedup and index steps follow with their Python ports).
- **`GET /health/maintenance`** — per-step last success / duration / receipt id, stale steps
  (48 h), `judge_transport`, pool usage (`zfs list` when `MEM0_ZFS_DATASET` is set) with the
  85 % alarm, boot ids of the last 7 days. Readers fail soft.
- **Embedder outages are 503 + `Retry-After: 10`** with `reason: cold-embedder` (`embedder_503`),
  so the shim queues the write instead of failing it; 4xx from the embedder is left alone.

Tests: installer (bash in a scratch HOME), key provider, native transport (injected runner), chain
step/guard/rtcwake, maintenance health, 503 classifier and handler — each red first, each with a
mutation proven red. Windows-side scripts are untouched.

## v1.20.21 (2026-09-10) — one compactor per PC, one judge attempt per store per night

The SessionStart catch-up added in v1.20.20 ran once per session start with no cross-instance
lock: four instances hit one store in the same second, `history.git/index.lock` failed, 243
receipts landed in nine hours, and the judge was called 32 times on one store with every result
rejected. Interim relief ahead of the AMS v2 design (ADR fleet-store-sync-and-linux-authority).

- **GUARD 0 — one compactor instance per PC.** A session-local named mutex
  (`Local\ams-memory-compact`) makes every concurrent instance exit at once with a log line and
  no receipt; the survivor does the whole run. The OS releases it if the holder dies.
- **One judge attempt per store per 20 h.** Every receipt now records `judge_called`;
  `Get-AmStoreRunHistory` exposes `LastJudgeUtc`. A store whose judge was called in the last
  20 h gets deterministic hygiene and the floors as before, but the judge call is withheld and a
  store with nothing else to do receipts `skipped-judge-attempted-today` (not productive, does
  not extend `skip_streak`). `-Force` bypasses the window for a hand run. The `-CatchUp` starved
  check applies the same window, so a session start no longer re-runs a store the judge already
  decided today — the sequential half of the storm. `memory-lint` treats the new status as neutral
  (excluded from the `compactor-unproductive` window, never counted as good), so a store that is
  waiting for tomorrow's attempt is not reported as stuck.
- **Receipt ages under pwsh 7 were skewed by the UTC offset.** `ConvertFrom-Json` in pwsh 7
  already yields a `[DateTime]` for `ts`; the `[string]` re-parse dropped the `Z` and read it as
  local time, so `LastProductiveUtc` was 5 h young on this fleet whenever the lib ran under pwsh 7
  (tests, installer). PS 5.1 — the scheduled task — was unaffected. `ConvertTo-AmUtc` handles both.

Tests: GUARD 0 held/free, judge withheld inside the window and called outside it, the catch-up
exclusion, `LastJudgeUtc`/`LastProductiveUtc` under pwsh 7 typing. The seal test ages its
first-run receipt past the window so the second run still calls the judge.

## v1.20.20 (2026-09-08) — a live session can no longer starve a store past the sync limit

Root cause of "MEMORY.md over its load limit" in a live session. The compactor gets one shot a
day at 05:00; its liveness guard skipped one store two nights running (legitimately — a session
wrote memories 33 minutes before the run); the store grew ~3,700 B/day against ~8,000 B of
headroom and crossed the 25,000 B sync limit, at which point the harness stopped syncing it and
every new session loaded a partial index. Three watchdogs stayed quiet, each for its own reason.

- **The throttle stamp is per run, so a skipped store was never retried.** A run that skipped
  this store still marked the stamp because other stores reached a decision, and the SessionStart
  catch-up — the only other chance in the day — exited at every session start. The catch-up is
  now **per store** (`Get-AmStoreRunHistory`): a fresh stamp no longer ends it when a store above
  trigger has reached no decision in 24 h, and the 12 h run throttle no longer re-silences that.
- **The liveness guard escalates at the sync limit.** A skip protects against a lost update,
  which is recoverable in one night; an index the harness refuses to load is broken for everyone.
  At/over the limit the quiet window drops from 30 to 5 minutes, and after two consecutive skips
  the run proceeds and says so (`liveness_override` in the receipt). Under the limit: unchanged.
- **Starvation is reported.** Every receipt carries `skip_streak`. `compactor-silent` keys on
  the receipts *file's* age — and a skip writes a receipt, so a store skipped nightly looked
  alive; `compactor-unproductive` needs three bad receipts in a row, and this one had `applied,
  skipped, skipped`. Lint now raises **`compactor-starved`** (actionable; the heartbeat renders it)
  on two consecutive skips above trigger or one skip at the limit.
- Found, documented, not changed: the write-time gate never fires on this index because its
  matcher is `Write|Edit` and the index is written through Bash/python. Probed directly it works
  (29,630 → 24,906 B, receipted). Widening it costs a `powershell.exe` spawn per shell call.
- Eight tests, boundaries included: under the limit a live session still skips; at the limit a
  1-minute-old write still skips while a 10-minute-old one proceeds; the catch-up runs a starved
  store on a fresh stamp and stays silent when the stamp is backed by a recent decision.

## v1.20.19 (2026-09-07) — the WSL installer now enforces the One-Brain rule too

- **`install/1-wsl-services.sh` no longer enables canonical-mutation units on a replica.** It
  enabled every unit unconditionally, so running it on a replica silently created a SECOND write
  authority: a local `mem0` + `qdrant`, plus the `l10-audit`, `decay-scan` (its `ExecStartPost`
  runs semantic-dedup), `stack-backup`, `goals-stale-sweep`, `contradiction-sweep`,
  `retrieval-pairs`, `episodic-reconcile` and `goal-recurrence-promote` timers — all mutating
  canonical state in a store that box does not own.
  `install/2-windows-config.ps1` has gated its half since v1.16, and the health check already
  reported WSL timers on a replica as brain-only machinery "by design", so the installer
  contradicted both. Every brain unit now goes through `enable_brain_unit`, which installs the
  unit either way (promoting a replica stays a one-liner), and on a replica enables nothing and
  disables whatever an earlier ungated run turned on — the same skip-and-remove the Windows
  installer performs.
- **The installer could not finish on a replica either.** Its service-status readout pipes
  `systemctl is-active` through `sed`, and `is-active` exits non-zero for an inactive unit —
  under the script's `set -eo pipefail` that aborts the run. On a replica every unit in that
  readout is inactive by design, so the gate above would have been followed immediately by a
  failed install: units written, health probes and completion message never reached. The readout
  is now non-fatal, and the probes report `dormant by design` on a replica instead of sending an
  operator to `systemctl status` for a unit that is off on purpose.
- Three guards, all mutation-proven: a static one asserting no brain unit is enabled by an ungated
  `systemctl` call; a **behavioural** one that extracts the helper and runs it against a stub
  `systemctl`, asserting a replica emits `disable --now` and never `enable --now` while a brain
  still enables; and one that runs the real status-readout line against a stub reporting an
  inactive unit, asserting the script survives it. Wording alone would not have caught a gate that
  never matches — that is one of the mutations proven red.

## v1.20.18 (2026-09-07) — a deployed runtime must be able to say which release it is

- **Every installer that deploys the server modules now stamps `VERSION` beside `app.py`.**
  `_resolve_stack_version()` reads that file at import and falls back to the string
  `"unknown"`, so an installer that copied the modules without the stamp produced a runtime
  whose `/health` could not answer the one question a deploy exists to settle. The Linux
  replica shipped exactly that way — both candidate paths absent, `stack: "unknown"` — and the
  WSL installer's fresh-install AND refresh paths had the same gap (only `deploy.sh`, the
  Brain's normal path, stamped it).
  This is not cosmetic. During the v1.20.17 deploy a STALE stamp was the only signal that a
  step had been missed: the server reported 1.20.16 while the repo said 1.20.17, which is what
  led to finding that the installer had never copied the file at all. A runtime that reports
  "unknown" cannot even lie usefully — it just removes the check.
- A guard test asserts the invariant per installer: every module-deploy site must be matched by
  a `VERSION` stamp, so a future deploy path cannot reintroduce an unstamped runtime.

## v1.20.17 (2026-09-07) — provenance in the store, and a cost meter for the decision

Close-out of the model-routing work.

- **`judge_model` on the tier ledger (schema v18).** `actor` is a role label
  ("dream-autopromote", "user-direct") and never said WHAT judged a promotion. Both the
  write-ahead intent row and the completion row now record the model. Additive: the field is
  OPTIONAL, so every pre-v18 row stays valid. It sits deliberately OUTSIDE the signed
  material — the canonical HMAC covers `<ts>|<nonce>|promote|<mid>|<reason>` — so it is an
  audit convenience, never an authorisation input. `mem0-canonize.sh` passes it via
  `JUDGE_MODEL`.
- **`codex exec -o <file>` at the three JSON-parsing call sites.** Scraping the answer out of
  stdout depends on a `codex` marker that is absent when a run produces no assistant message;
  the scrape then returned the metadata header and the caller parsed it as the answer (5 of
  330 live extractor calls). The `-o` file is Codex's own copy of the final message. Stdout
  scraping stays as the fallback and `$null` remains the honest outcome when both are absent.
- **`codex-usage-report.ps1`** — per-job calls, tokens, latency, failures and requested-vs-
  resolved model DRIFT, plus the plan's 7-day window. Built because the NLI write-gate is
  pinned but OFF and the decision to enable it needs measured cost, not a guess.
- **Silent-failure review fixes (folded in before merge).** The reviewer found that the new
  code repeated, in four new places, the very anti-pattern this release exists to remove.
  - The plan-window read cast an unvalidated field to `[int]`. This endpoint is UNOFFICIAL, so
    a renamed field lets the CALL succeed; `[int]$null` is `0`; the report would have stated
    "0% used" - maximum headroom - from a response that carried nothing, and the only
    downstream guard is a `$null` test that a genuine `0` passes. The shape check now lives in
    `Get-CodexPlanWindow`, where it is directly testable, and an unknown reads as unknown.
  - `unparsed` rows got their own column. They are excluded from DRIFT on purpose (an unknown
    is not a mismatch), but folding them into "not drift" meant a codex header-format change
    would turn every row unparsed while drift reported a clean `0` - invisible in the one
    report built to catch silent model change.
  - The compactor wrote NO ledger row when the judge succeeded and returned nothing: `''` is
    falsy, so `if ($raw)` skipped the write entirely. Fixed the same way `l1a-extract.ps1`
    already did it, with `outcome='empty'`.
  - The R-offload producer check could only see literal command/args text, so a wrapper script
    that reaches a producer - the real exposure - downgraded to a WARN. It now follows one hop
    into the script the hook names, says INFERRED rather than claiming proof, and names any
    matcher or file it could not evaluate.
  - A malformed `duration_ms` is now COUNTED (`bad_duration`) rather than dropped. The review
    said such a row would kill the whole report; measuring it showed otherwise — the cast is
    only statement-terminating, so the row is skipped and the run continues. The real defect was
    quieter and worse for being quiet: that row left the latency sample while still counting in
    `calls`, so p50/max described a smaller population than the column beside them claimed.
  - Also: `-o` temp files are cleared on every exit path and swept after 24h (a KILLED task can
    run no cleanup at all, so caller discipline alone cannot bound that directory);
    `New-CodexLastMessagePath` moved inside the compactor's try, so a failure there degrades to
    deterministic hygiene instead of failing the whole store; and the `-o` unreadable-file
    fallback now logs instead of silently reverting every call to the stdout scrape it was built
    to replace.
- **R-offload invariant narrowed to the real exposure.** It used to FAIL on ANY PreToolUse
  matcher that fires for the offload harness, which conflates "a hook fires" with "the harness
  receives the [MEMORY CONTEXT] block". Only a matcher bound to a memory-context PRODUCER can
  route that block; a third-party deny-only guard cannot. Held as a hard FAIL, the old rule
  reported the stack UNHEALTHY for days over another session's delegate guard — which is how a
  standing red light stops being read. A firing foreign matcher is now a WARN that names the
  offender; a producer on a firing matcher still FAILs (proven by mutation). The same change
  fixes a blind spot in the hook lookup: a hook is routinely
  `{command: "node.exe", args: ["…guard.js"]}`, and reading only `command` saw "node.exe".

## v1.20.16 (2026-09-07) — the judge model is pinned per job, and recorded

Every Codex call inherited whatever `~/.codex/config.toml` named. A config edit on 2026-09-07
moved the whole stack onto `gpt-6-astra` and nothing recorded it: no receipt, log line or ledger
row could say which model had judged a memory.

- **Per-job model routing.** `Invoke-CodexSubagent -Model` (and a `model` field on the shim's
  `/judge` request, allowlisted, shim `0.27.1` → `0.28.0`). Synthesis and consequence run on
  `gpt-6-astra` at **medium** effort (operator directive); bounded extraction, classification and
  routing run on `gpt-5.6-terra`. A guard test fails the build if a call site forgets `-Model`.
  Routing table and rationale: `docs/systems/codex-hooks.md`.
- **Provenance.** New `Parse-CodexHeader` reads the RESOLVED model and effort out of Codex's own
  stdout header (verified against codex-cli 0.153.4), and the usage ledger gained
  `model_requested`, `effort_requested`, `model_resolved`, `effort_resolved` and a closed
  `outcome` enum. `memory-compact.ps1` and `autopromote-lib.ps1` called the usage logger ZERO
  times and are now instrumented, as are the dream's abort paths and L1a's parse-failure exit.
- **Fixed: the promotion phase had been nominating nothing.** `Extract-JsonFromText` discarded a
  bare top-level array, and `'[]' | ConvertFrom-Json` yields nothing in PowerShell, so the empty
  list was invisible. `dream.log` recorded "autopromote: bad Codex JSON (promoting nothing): []"
  on 2026-09-03 and 09-07.
- **Fixed: header-only replies were parsed as answers.** `Get-CodexResponseText` returned the raw
  metadata header when Codex emitted no assistant message; it now returns `$null` so the caller
  records `outcome='parse_fail'` (5 of 330 live L1a calls).
- **Fixed: a live lock holder could be robbed.** `Acquire-CodexLock` reclaimed on age alone even
  with the holder alive; age now only applies when no PID can be read.
- Timeouts and ceilings sized to the work: L1a 60→90s (observed max 62.4s), Astra phases →240s,
  gate 90→180s, sweep 45→60s, dream lock 30→45 min, dream task limit 15→40 min, compactor task
  20→30 min (its own lock window was already 30).
- `CODEX_JUDGE_IDENTITY` bumped (`…effort-low:v1` → `codex-cli:terra:effort-low:v2`): the 30-day
  verdict cache was not bumped when the model changed, so stale verdicts would have survived.
- Stale `gpt-5.5` references replaced across docs and comments with the job's role.

## v1.20.15 (2026-09-06) — the compactor runs every night, and converges on lines

- Compactor throttle 23h → 12h: a daytime hand run marked the throttle and the next 05:00 run
  skipped itself (one silent night, 2026-09-04).
- `memory-compact.ps1 -CatchUp`, launched by the SessionStart spawner: runs the nightly only
  when the newest receipt is older than 24h. The box was off at 05:00 on 2026-09-05 and the
  scheduler's missed-start retry refused ("user not logged on"); nothing re-ran the job.
- Line floor: when an index is over its line trigger, the oldest pullable facts the judge did
  not migrate are migrated deterministically (write-then-verify, blast cap, doctrine excluded)
  until the store is back at its line target; receipts carry `line_floored`. A store had
  climbed to 174 lines while every nightly migrated 0.
- Orphan re-index synthesizes the hook from a frontmatter-less file's first line of prose
  instead of "recovered orphan; no description".
- Doctrine now includes attributed statements (`Owner: …`): the first live line-floor run
  migrated an operator rule typed `project` with no imperative verb. Migration failures log the
  server's answer instead of a bare "returned no id".

## v1.20.14 (2026-09-03) — Linux replica role

- New `install/linux-replica.sh`: a native-Linux box becomes a replica — thin client plus a
  dormant local Qdrant + mem0 (user units installed, disabled) and a 2-minute
  `offline-watcher.timer`. Server module list, Qdrant version and pip line are read from
  `install/1-wsl-services.sh` at run time (one owner). Ends with a first restore as the proof.
- Qdrant storage is backed by a loop-mounted ext4 image when the home filesystem is not
  ext4/xfs/btrfs/tmpfs: Qdrant 1.18.2's snapshot restore fails on f2fs (verified live; tmpfs
  and ext4 restore the same snapshot).
- New `scripts/travel/restore-replica.sh`: pulls the Brain's newest complete snapshot set over
  SSH (through `wsl.exe` for a Windows+WSL Brain), size-verified and cached, restores it via
  the Qdrant snapshot upload API, replaces the ledgers, requires `/health/deep` through the
  local embedder, stamps `~/.mem0/replica-restored`. Carries the One-Brain guard (role must be
  `replica`, authority must be remote).
- New `scripts/travel/offline-watcher.py`: the PowerShell watcher's state machine and
  transitions on Linux, plus one fix — the replica is refreshed while ONLINE (last restore
  >24 h), not at `go_offline` when the Brain is unreachable by definition.

## v1.20.13 (2026-09-03) — Linux thin client

- New `install/linux-client.sh`: a native-Linux box with no WSL and no local store can now use a
  remote Brain. It deploys the MCP shim and its sibling replay driver into a small venv, writes
  the per-host authority/role/key files (`role=client`), registers the `mem0` MCP server in
  Claude Code, appends the CLAUDE.md tier protocol, and proves the install with a real MCP
  session calling `memory_health` against the authority. A loopback authority is refused;
  the `__WSL_USER__` tenant sentinel is resolved to `--user-id` as on Windows.
- `replay-ops.py`'s One-Brain refusal (never replay an Outbox into loopback) now covers the
  `client` role as well as `replica`.

## v1.20.12 (2026-09-03) — hygiene on every store; the liveness row measures live

- Deterministic hygiene (orphan re-index, dangling/duplicate-slug removal) now runs nightly on
  EVERY populated store; only stores over the size trigger go on to the judge, migrations and the
  floor. A small store had carried 7 orphaned facts for days while the lint reported them every
  session and nothing ever fixed them. Clean below-trigger stores write no receipt.
- `Test-MemoryStack`'s maintenance-liveness row measures the live stores instead of the
  SessionStart lint snapshot, which kept a stale size for hours after a remediation.

## v1.20.11 (2026-09-03) — auto-memory: converge under the sync limit, or say so

Live failure: the AI-Ecosystem index reached 27.4 KB with 126 of 180 lines over the cap and the
harness loaded only part of it in another session. The compactor had run nightly and stamped
`applied` while leaving the store at 25,219 B (judge-driven shortening converges ~13 lines a
night, "KEEP is the safe default"), skipped the wake-up catch-up night entirely because dream and
dedup held the codex lock, and retried three 413-oversized migrations forever; the write-time
lint was advisory and ignored.

- Shared deterministic **convergence floor** (`Invoke-AmConvergenceFloor`): at/over the sync
  limit, the longest non-doctrine hooks are truncated to the line cap until the index is under the
  trigger. Doctrine is never touched.
- Compactor: floor runs after the judge (with or without it); a held codex lock skips only the
  judge; bodies over the server cap are never migration candidates; `applied-unconverged` /
  `unconverged` statuses with **exit 1**; receipt gains `floored`.
- Write-time gate: `memory-index-write-gate.ps1` (PS 5.1, Windows-native) replaces the bash
  advisory on PostToolUse — same advisory, plus in-place normalization at the sync limit behind a
  content-hash CAS, receipted.
- `Test-MemoryStack`: the maintenance-liveness row is red when any store is at/over the sync limit
  now or the latest receipt is unconverged; R9 tracks the gate.

## v1.20.10 (2026-09-02) — SessionStart banner fires on open, not on every resume

A context audit over 76 transcripts found the `[agentic-memory-stack]` / `[heartbeat]` /
`[storage-cap]` orientation banner re-emitted on every session *resume* (308 resume fires vs
225 startups, ~1.3 KB each) — the single largest routine SessionStart repetition, because the
installer registered the hook with no matcher. It now registers `startup|clear|compact`:
a fresh or cleared session gets its orientation, a compaction re-reads it into the rebuilt
context, a resume already has it. Pinned by a regression guard so a hand edit to
settings.json is never the only copy.

Same audit, second-largest class: the resident daemon's HK-5 dedupe re-injected unchanged open
goals/questions every 12th prompt; the cadence is now every 25th (the re-inject exists only
as a post-compaction guard, which 25 still serves). Pinned by a regression guard.

## v1.20.9 (2026-09-01) — installer: a WSL path that isn't one is refused, not recorded

The drift guard died silently for 4 nights: an operator ran the Windows config phase from
Git Bash, whose MSYS path conversion rewrote `-EvalRootWsl /mnt/...` into
`C:/Program Files/Git/mnt/...` before pwsh ever saw it. The installer recorded it unchecked;
the dream's drift snapshot became `python C:/Program Files/...` (bash split at the space,
exit 2 every night, "no false alarm" skip every night) while the liveness row kept reading
the stale state sidecar as "guard alive" — only the capability manifest's age check
eventually surfaced it. Two defenses, both keyed on the same form check:

- `2-windows-config.ps1` refuses a resolved `EvalRootWsl` (explicit or inherited) that is
  not an absolute POSIX path, with an error naming MSYS conversion and the
  `MSYS_NO_PATHCONV=1` fix — a poisoned receipt can neither be written nor survive a re-run.
- `Test-MemoryStack.ps1`'s drift-guard liveness row FAILs on a malformed receipt value
  before consulting the state sidecar, so an already-poisoned box alarms on the next health
  run instead of after four quiet nights.

## v1.20.8 (2026-09-01) — outbox: a stopped drain must resume, and never rewind a record

Found live: during an embedder-contention window (llama-swap 429 → server 503) a session's
writes queued to the outbox; the drain stopped on the retryable 503 — correctly keeping the
op — but kept it in `outbox.replaying.jsonl`, which the shim's session-start drain trigger
never looked at. The op sat stranded 15 hours across many session starts while
`outbox_depth` read None. Worse, the op was an update whose target the session had already
re-updated directly: a blind replay would have regressed the record to the older draft.

- shim `_drain_outbox_async`: triggers on a non-empty `outbox.replaying.jsonl` too
  (`replay-ops.py` has always resumed it; only the trigger was blind).
- `replay-ops.py`: superseded-update guard — before dispatching an update, compare the
  record's `updated_at` to the op's `queued_ts`; ops the world moved past go to
  `mutation-conflicts.jsonl` with reason `superseded-by-newer-write` (preserved, never
  dispatched, never dropped). Fail-open for legacy ops without `queued_ts` and on GET
  failures — fail-closed would recreate the stranded class.
- `job_liveness`: `outbox_depth` counts both queue files, so a stranded backlog is visible
  to the offline-outbox capability row instead of reading "unknown".

## v1.20.7 (2026-09-01) — server: no record is born without a tier

A record added without `metadata.tier` (a path the add endpoint's own 403 guidance
recommended) was stored tier-less — and `fetch_current_tier` fail-closes an absent tier to
`canonical` (the H1-race shield), so every mutation of that record demanded the user-direct
HMAC. An agent could create a memory it could never correct or delete; 127 such points
existed live, including a malformed add whose metadata parameter block leaked into the
memory text. `POST /v1/memories` now defaults the tier to `evidence` at birth (after the
canonical/insight gates, before hash-dedup), a live test pins born-tier + deletability, and
`scripts/wsl/tier-backfill.py` stamps the existing stock — skipping and reporting any id the
tier ledgers ever named with canonical/insight history rather than silently demoting it.

## v1.20.6 (2026-08-31) — installer: four fresh-install gaps, all silent

Four bugs filed against a fresh install, each invisible on a long-lived box because the
missing piece had been hand-installed or the failure exited 0:

- **fastmcp was never declared.** The MCP shim runs on the server venv's python and imports
  `fastmcp`, but neither `requirements.txt` nor either installer pip line carried it — a
  fresh install produced an MCP that only ever said "Failed to connect". Now in the floors
  file, both pip branches, and the installer's import-gating post-condition; a regression
  guard pins all three.
- **jq was required but not a prerequisite, and the backup swallowed its absence.** The
  nightly backup parses the Qdrant snapshot name with `jq`; without it the parse was empty
  and the block printed a WARN and exited 0 — the vector collection silently absent from
  every backup while the run reported success. jq is now a phase-0 prerequisite check, and
  every skip/failure path in the Qdrant block sets rc=1 like the local-file blocks always did.
- **3s health probes raced.** `3-verify.ps1`'s Qdrant/mem0/authority liveness probes used a
  single `-TimeoutSec 3` attempt and reported false MISSING right after wsl.exe activity
  while the round-trip check passed in the same run (the search leg was hardened 2026-07-25;
  these were the same defect one section up). Probes now retry once with a 10s timeout.
- **PowerShell platform truth.** `3-verify.ps1` was BOM-less UTF-8 with em-dashes and no
  `#Requires` while the docs promised "PowerShell 5.1+" — under 5.1 it parse-dies mid-file.
  Decision: the installer standardizes on pwsh 7. Phases 2–3 now carry a UTF-8 BOM (so 5.1
  parses them) plus `#Requires -Version 7` (so 5.1 refuses cleanly), phase 0 checks pwsh is
  present, and README/skill docs state the real contract: pwsh 7 for the installer, the
  built-in 5.1 for the deployed hooks.

## v1.20.5 (2026-08-28) — health: a replica is checked against the brain it uses

`Test-MemoryStack.ps1` probed loopback for every mem0/Qdrant row, so the first replica it ran
on reported 14 permanent FAILs for services a replica deliberately keeps dormant. It now
resolves the memory authority the way `3-verify.ps1` does (`~/.mem0/authority-url`, then the
receipt, then loopback) and every shared-store row targets it; on a replica the mutation
probes are skipped (server invariants the brain proves daily; they would also need a
canonical key the replica does not serve), brain-only machinery reports "by design", the
dream/dedup task rows flip polarity (present on a replica = FAIL), and a new `memory
authority (one-brain)` row FAILs a replica pointed at itself. The brain path is unchanged.
Regression guards pin the single loopback literal and the role gates. Proof: the Aorus
replica went 14 FAIL → 0 FAIL (46 PASS, 2 genuine WARNs); the Qube brain run is unchanged.

## v1.20.4 (2026-08-28) — installer: the replica fix, fixed for replicas

v1.20.3 defined the shared `$taskUserId` *inside* the brain-role branch. A replica skips that
branch, so the compactor registration (every role, after the gate) received a null `UserId`
and the replica deploy failed again — while the brain deploy passed, and the pre-merge live
probe had exercised the principal expression rather than the installer's control flow. The
definition now precedes the role gate, and the parity test asserts that ordering. The proof
this time is the installer itself completing on the replica.

## v1.20.3 (2026-08-28) — installer: task principals resolve on workgroup boxes

Deploying v1.20.2 to a replica box failed at the compactor task: `Register-ScheduledTask`
returned "No mapping between account names and security IDs". The installer built every
task principal as `$env:USERDOMAIN\$env:USERNAME`, and on a workgroup machine USERDOMAIN is
the literal `WORKGROUP`, which has no SID. The brain-only dream/dedup registrations carried
the same latent bug; the one brain box happened to have a matching USERDOMAIN. All three now
use `WindowsIdentity.GetCurrent().Name`, which resolves on domain, workgroup and
Microsoft-account boxes alike. A parity test fails if `USERDOMAIN` reappears in a principal.

## v1.20.2 (2026-08-26) — auto-memory: a migration is never a no-op

Found by a receipt-fidelity test written after the v1.20.1 live run showed a blank
"original line" for a re-indexed orphan. The test exposed something worse than a blank
field: an orphan that hygiene re-indexes and the judge then migrates leaves the index
text byte-identical to before, so the run reported `no-op` — while the migration write
had been made and verified, the orphan file stayed on disk, and no receipt row named the
corpus id (`migrated=1` beside `status=no-op`). The same unnamed-record class the v1.20.1
review closed on the abort paths, one exit path further along. A run with verified
migrations pending now always proceeds through write → verify → delete → receipt, and a
constructed index line carries itself into the receipt.

## v1.20.1 (2026-08-26) — auto-memory: the fix round reviewed

The operator asked for an adversarial review of the v1.20.0 fix round itself, and it found
what this stack's own notes predict: a fix applied literally recreated the bug class.

**Critical.** The "delete fact files only after the index write" fix placed the delete
between the write and the post-write invariant check. An invariant failure then restored
the pre-run index — which still listed the just-deleted facts — while the receipt reported
`lost=0`. The new reachability rule ("linked from ANY line") simultaneously created ghosts
that hygiene could not repair (a file name mentioned in a heading or a prose note), so the
invariant failed every night. Reproduced end-to-end: up to five files deleted per night,
index restored onto them, forever. Fixed by ordering — write, verify invariants, THEN
delete — and by deciding ghosts from entry links only, checked *before* the judge runs, so
an unrepairable store aborts with nothing written and nothing posted.

**High.** A compare-and-swap abort left verified migration records in the corpus that no
receipt named (now undone, unless the server reported the id as a pre-existing dedup hit —
those are never deleted, which closes the second finding: the shared write helper discarded
the `deduplicated` flag, so an unverifiable write could have deleted an L1a fact or an
earlier migration). The compactor now performs its own migration POST and reads the flag.
An enumeration failure *after* the write now reports `applied-unverified` and deletes
nothing instead of collapsing into a generic error.

**Medium.** The banner staleness guard was inert on Python 3.10 (seven fractional digits);
the unproductive-compactor finding was fleet-size gated by a 40-line tail; the widened
entry regex accepted a checkbox line as a pointer and fenced examples as entries (now:
fenced lines are text, and ambiguous lines are never removed and never ghosts); a
dead-extra-link repair rejected any line that also carried a live extra link; byte
truncation could split a surrogate pair; an empty seal file read as "no seals"; a locked
temp file was swallowed; `-Workspace` with a typo silently rehearsed nothing.

Tests: +7 library, +7 compactor scenarios, boundary assertion on the blast cap. Live
verification at the deployed config: real scheduled-task start (`LastTaskResult 0`, live
store correctly skipped), hook through its registered `wsl.exe` command with stdin, lint
through its real PS 5.1 spawn, banner rendered, exit codes propagate through `run-hidden.vbs`.

## v1.20.0 (2026-08-26) — auto-memory maintenance

The coding-agent harness keeps its own per-workspace file memory — an index of one-line
pointers, injected in full at every session start, plus one fact file per pointer. Nothing
in this stack maintained it. A live store was found at 96% of its hard per-file limit, with
an unindexed fact file no session had ever loaded and an index line pointing at a deleted
file; no job existed that would ever have noticed. This release makes those stores
self-maintaining, in three pillars.

**Lint** (`memory-lint.ps1`, spawned at session start, read-only, 6h throttle): enumerates
every populated store — deduplicating alias directories by canonical path — and recomputes
findings from disk: orphan, dangling link, duplicate slug, over-long line, oversized fact
file, missing frontmatter, near or over a budget. Stateless by design: the finding set is a
handful of items recomputable in milliseconds, and a monotone watermark would have silently
suppressed a defect that was fixed and later recurred. Two findings watch the maintainer
itself — a store above trigger with no run receipt in 48h, and a history repo that has
gained a remote.

**Write-time lint** (`memory-index-write-lint.sh`, PostToolUse on Write/Edit): the harness
warns on its *line* cap, but nothing checked *bytes per line* — which is what fills the byte
budget first (the store above was at 64% of the line cap and 96% of the byte cap). The hook
reports an over-long index line to the agent that just wrote it, in the same turn, so the
bloat is fixed at the source instead of being compacted forever. Advisory; always exits 0.

**Compaction** (`memory-compact.ps1`, new 5:00am task, every role — these stores are
machine-local, unlike the shared corpus): fires at 20,000 B or 160 lines, targets below
17,000 B and 140 lines. Deterministic hygiene first (dangling and duplicate lines removed,
orphans re-indexed from their own frontmatter), then one judge call over the *delta only* —
long lines and migration candidates, never the whole index.

Five guards, one behavioural test each, written so that removing the guard fails the test:

- **Liveness gate + compare-and-swap.** No process locks the index, and a box that sleeps
  runs its catch-up at the next logon — exactly when sessions start. Observed during the
  build: a store grew three entries mid-flight. The job skips a workspace with recent session
  activity, and re-reads the index hash and file set immediately before the swap, aborting on
  any drift. Abort, never roll back: a directory-level revert would clobber the live write.
- **Doctrine is untouchable.** `metadata.type: feedback` is *nested* — a top-level match finds
  nothing, which would have made the rule inert and every standing order eligible for deletion.
  Doctrine is classified deterministically and never even offered to the judge.
- **Strict decrease, seal, blast cap.** A judge edit applies only if it strictly shrinks the
  index past the hygiene baseline (hygiene is correctness and is exempt); each line may be
  rewritten by the judge at most once, ever; no run removes more than a fifth of the lines.
  A rewritten hook must retain an anchor token, so a line cannot be reduced to a label that
  no longer says when to open the file.
- **Write-then-verify migration.** A migrated fact is posted verbatim, tagged
  `source: automemory:<workspace>/<file>`, and read back **by id** with byte equality before
  its line and file are removed. A write returning no id counts as unverifiable and the line
  stays. Verification by semantic search was rejected: ranking top for its own text can be
  satisfied by a pre-existing near-duplicate.
- **Feasibility.** If doctrine alone exceeds the target budget, the job stops and reports
  rather than loosening the hard rule.

Supporting changes: `semantic-dedup.py` now protects auto-memory migrations — it deletes the
newer of a near-duplicate pair, and a migration is always the newer side, so an unguarded run
would have evicted a just-verified fact the next morning; two migrations delete neither, and
canonical still wins. History is a local git repository with its git-dir outside the tree and
no remote, replacing a hand-rolled archive: commits are the audit trail, per-file checkout is
the undo, and lint fails if a remote ever appears. All maintainer state lives outside the
store directory — an in-store archive would have resurfaced removed facts in every agent
search and re-exposed the credential-bearing file that started this work. Two health-check
rows added: store budgets and structural cleanliness (invariants), and maintainer liveness
(recovery) — a registered task proves nothing if it never fires.

## v1.19.0 (2026-08-08) — the hardening-program waves

Waves W1–W5 of the audit-driven hardening program (55 adjudicated findings; see the
audit register). W1: the verification spine — launch-path parity gates, deploy
pre-flight, behaviour-verified fixes on the LAUNCH PATH rather than the repo. W2:
PUT payload carry-over (atomic pre-merge, per-record locks), CP437 mojibake repair
across four stores, the BM25 sparse leg revived with a gating /health/deep canary.
W3: alarm delivery legs — capability manifest, job-liveness surface, drift-guard
cross-run legs, SessionStart heartbeat digest. W4: revive-or-bury — redaction rule
set fixed and widened under one three-runtime fixture, the Codex judgment leg
live-proven after two dead dependencies, the one-brain guard made real, DPAPI docs
truth-pass. W5: retrieval observability (`explain`, `POST /v1/memories/diagnose` +
`memory_diagnose`, `rerank_status`), gap annotations (withheld-family counters +
recall age summary + conditional staleness line), the keyword-recall union leg
(AMS-56: fail-closed on rerank, deliberate path only), per-pair judge cache +
retrieval-pair dry-run, real-query replay harness + deploy-gated retrieval
families, count-only entrance-redaction telemetry, and sparse-leg reboot survival
(durable fastembed cache + bounded sentinel self-heal + pre-reboot cache gate).
Note: the redaction rule set has a fourth copy in SkillOpt on the offline replica —
it adopts the shared fixture on that box's next return.

## v1.18.0 (2026-07-25) — the silent-failure week

Twenty PRs repairing a family of defects that shared one trait: **something stopped working and
nothing said so.** Every one was found by hand or by audit, never by an alarm, because each failed
into a shape indistinguishable from "nothing to do".

### Outages fixed

- **Memory injection was dead on every prompt** (~1000 recorded failures). Claude Code passes a
  hook command with no `args` array to Git Bash, where an unquoted backslash is an escape
  character, so the client's absolute path was shredded and the hook exited 127 — silently. Hooks
  registered *with* an `args` array are exec'd directly and kept working, so the event looked
  healthy throughout.
- **Episodic capture was dead for 9 days**, from two independent causes at once: the hook launcher
  was pinned to a version-stamped WindowsApps PowerShell path that Windows deletes on update, and
  the command strings carried the backslash bug above. Fixing either alone left it dead.
- **The weekly contradiction sweep had never judged anything** in the deployed layout — its
  `sys.path` resolved correctly in the repo but to a non-existent directory once deployed, so the
  Codex bridge import failed and the run exited 0 every week.
- **A replica silently queued every write to the Outbox.** The MCP shim read its authority from an
  environment variable, but `wsl.exe -e` execs directly (no login shell, no `WSLENV`
  pass-through), so the value never arrived and the shim fell back to a dead loopback.
- **The offsite backup was deleting archives.** `robocopy /MIR` mirrors, so every source-side
  retention prune destroyed the offsite copy too; 688 MB existed only offsite and was hours from
  being purged.

### Systemic fixes

- Authority resolution is a per-host file (`~/.mem0/authority-url`), read identically by the shim,
  `replay-ops`, the SessionStart bundle and the offline watcher. The Outbox drains at shim
  startup, and a replica refuses to replay into its own disposable store (One-Brain Rule).
- Throttle arithmetic uses a shell-independent epoch helper: PowerShell 5.1's
  `Get-Date -UFormat %s` is offset by the machine's UTC offset while pwsh 7 is correct, and both
  editions write the same state files.
- The nightly dream throttle is 23h, not 24h — the stamp is written at cycle completion, so a
  strict 24h window against a fixed 03:00 trigger made the dream run every *other* night.
- Installer values **inherit** rather than silently revert: omitting `-AuthorityUrl` or
  `-EvalRootWsl` on a re-run keeps what the box already had.
- Scheduled tasks run windowless through a `wscript` shim and register `Hidden`.
- Verifiers stopped crying wolf — role-aware checks (a replica's local store is *designed* to be
  down while online), a retry-hardened round-trip, and probe timeouts that report a slow CPU model
  as WARN rather than FAIL.

### Guards, so these classes cannot recur silently

- `3-verify` fails when any hook command carries an unquoted backslash path — the check that would
  have caught both hook outages on day one.
- `RegressionGuards.Tests.ps1` pins the throttle constant *and* its behaviour, the epoch helper,
  receipt inheritance, and the bash-safe hook builders. Mutation-tested: reverting each fix turns
  it red.
- The missing-bridge failure is receipt-gated — quiet on a fresh or partial deploy, loud on a box
  that completed an install.
- `check-docs.py` enumerates via `git ls-files`, so local scratch files no longer trip the gate
  while CI behaviour is unchanged.

### Restored from the carve

`_debris_patterns.py` + `conftest.py` (89 live-stack tests could not even be collected) and
`Run-PesterTests.ps1` (documented but never published; two defects fixed in the port — unquoted
`Start-Process -ArgumentList` elements broke on any path containing a space, and a locale-specific
module path).

## v1.17.0 (2026-07-18) — repo-local documentation system

A durable, repo-local documentation system for humans and AI agents, reviewed alongside code.

- **Taxonomy** under `docs/`: `systems/` (per-component deep-dives, renamed from `modular/`),
  `flows/` (cross-system pipeline walkthroughs), `architecture/` (long-lived constraints +
  `decisions/` ADRs), `glossary.md`, and `templates/`. `CLAUDE.md` gains a Documentation map
  and the agent workflow; `AGENTS.md` stays a one-line import shim so the guidance can't drift.
- **Six system docs** and **six flow docs** brought to a shared template with verified source
  maps; a **26-term glossary**; **nine seeded ADRs** recording the load-bearing decisions
  (one-brain rule, fail-open hooks, EmbeddingGemma on llama-swap, Codex as judge/extractor,
  the tier trust model, operator-agnostic sentinels, the offline-first supersession of travel
  mode, and public-repo-primary).
- **Docs gate** (`scripts/ci/check-docs.py`, a new 7th CI job): every relative doc link
  resolves to a real file, no operator-specific value leaks into docs, and every ADR carries
  valid frontmatter (`status`/`date`; `superseded_by` iff `Superseded`).
- The **docs-and-code-must-agree** rule is now explicit: every pull request that changes
  behavior, interfaces, security, data, or operational procedures updates the affected
  documentation in the same change.
- The `.claude-plugin/*` manifests are realigned to the release version (they had drifted
  to 1.15.0).

## v1.16.2 (2026-07-17) — operator-neutral test fixtures + suite repairs

- 25 test files neutralized for the public ship (fixtures self-referential; behavior
  preserved). The PII leak-guard tests now read operator-specific patterns from gitignored
  `scripts/windows/tests/pii-patterns.local.txt` (`.example` ships).
- 4 silently-broken tests repaired: Qdrant byte-body mock discriminators (broken since
  v1.12's UTF-8-bytes fix), the offload-invariant test brought to the 2026-07-14 audited
  semantics, and cwd/hostname-dependent fixtures made hermetic. Full Windows suite 459/0.
- Unit-drift commit-back: `decay-scan.service` ships with the destructive dedup
  `ExecStartPost` DISABLED (2026-07-14 audit), `stack-backup.timer` is DAILY (feeds the
  offline-first replica snapshot), and `mem0.service`'s bind address is operator config
  (`__MEM0_BIND__` ← `MEM0_BIND` in `~/.mem0/stack.env`, default loopback).

## v1.16.0/1 (2026-07-17) — deploy-layer-skew hardening

- **Fail-open PreCompact**: the capture hook command is `python3 … || true` — a missing or
  erroring capture script can never hard-block compaction (exit 2 deadlocked live sessions
  when a config-repo untrack+pull deleted a box's deployed script layer).
- **Distro-agnostic hook emission**: no `-d <distro>` when the stack's distro is the WSL
  default, so a machine-synced `settings.json` stays portable.
- **One-brain role gate**: `-Role brain|replica` (receipt-recorded); replicas never register
  the nightly dream/dedup canonical-mutation tasks and remove stale ones. Role-aware verify.
- **Skew guard**: `3-verify.ps1` asserts every hook-referenced deployed script exists.
- Installer is pwsh-only (loud pre-flight); brands.json privacy split
  (`brands.example.json` template + installer fallback).

## v1.15.0 (2026-07-16) — offline-first memory client

Offline behavior EMERGES from connectivity: reads fail over to a local read-only replica,
mutations queue to an operation-outbox replayed to the authority on reconnect. The replica
can never absorb a write; divergence is impossible by construction.

## Earlier

v0.12 → v1.14: the memory stack's build-out (mem0 + Qdrant + EmbeddingGemma on llama-swap,
hook pipeline, dream consolidator, tier governance, promotion gate, travel mode). See the
docs/ runbooks for the operational history.
