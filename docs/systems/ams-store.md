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

**Scaffold + the derive engine (this change).** The scaffold carries store
enumeration (fail-closed, reparse-point/junction dedup, OS-gated case folding),
the atomic writer (temp + rename + hash read-back), the git wrapper (`gitx`, with
a `git >= 2.38` check for `merge-tree --write-tree` and a kill-tree timeout on
every call), frontmatter parsing, the doctrine rule and the index parse/render
core.

`derive` and `harvest` are now real: harvest, the four hygiene passes, the
planned-ghost abort, the blast cap, the derived render with its injection stop,
the convergence floor, the compare-and-swap write and the post-write invariants.
`lint`, `gate`, `sync`, `lock` and `judge-apply` still print `not implemented`
and exit 64; their engines land in the register rows that follow. The 1:1
Pester-counterpart table is seeded so the parity gate is enumerable from day one,
and the scenarios derive owns have moved out of the placeholder package into
`internal/derive`.

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
    judge/           the hub-only apply path: plan schema, apply-guards, migration
    porting/         the 1:1 Pester-counterpart table
    testutil/        sandbox (temp projects root + state root + optional history repo)
```

## Verbs

```
ams-store derive   [--store <dir>|--all] [--workspace <slug>] [--dry-run] [--json]
                   [--no-harvest] [--stop-below <bytes>] [--projects-root <dir>]
ams-store lint     [--all] [--workspace <slug>] [--json] [--quiet] [--summary-out <path>]
ams-store gate     [--stdin-payload]
ams-store sync     [--once] [--watch] [--timeout <dur>] [--remote <name>]
ams-store lock     status | acquire --for <dur> --reason <s> | release | break
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
binary cross-compiles for `windows/amd64` and `linux/amd64`; mutation runs (each
merge rule flipped to turn a named test red) are local, never a CI job.
