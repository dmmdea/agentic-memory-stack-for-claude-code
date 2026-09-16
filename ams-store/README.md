# ams-store

One binary that maintains Claude Code's native per-workspace auto-memory stores
(`~/.claude/projects/<workspace>/memory/MEMORY.md` plus one fact file per index line).
It replaces the PowerShell store library, the write gate and the nightly compactor on
every PC, and it is the tool the Linux authority runs on the hub.

**Status: scaffold.** The parse/render core, store enumeration, frontmatter, the atomic
writer and the git wrapper are implemented and tested. Every verb is still a stub that
prints `not implemented` and exits 64; the engines land in the tasks that follow.

## Verbs

```
ams-store derive   [--store <dir>|--all] [--workspace <slug>] [--dry-run] [--json]
                   [--no-harvest] [--stop-below <bytes>] [--projects-root <dir>]
ams-store lint     [--all] [--workspace <slug>] [--json] [--quiet] [--summary-out <path>]
ams-store gate     [--stdin-payload]
ams-store sync     [--once] [--watch] [--timeout <dur>] [--remote <name>] [--json]
ams-store lock     status | acquire --for <dur> --reason <s> | release | break
ams-store judge-apply --plan <file> --store <dir> [--dry-run] [--max-migrations 5]
ams-store harvest  --store <dir>
```

`judge-apply` is hub-only. `harvest` is a step of `derive` exposed on its own because it
must run on every PC before that PC's first push, so `hook:`-only differences never
reach the merge.

Global flags on every verb: `--json`, `--verbose`, `--state-root`, `--projects-root`,
`--now`, `--machine-id`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Normal. Per-store skips, deferrals and "nothing to do" are all 0. |
| 1 | Unconverged: an index is still at or above the sync limit after the floor. |
| 2 | Bad invocation. |
| 3 | Refused to run: history unavailable, or a remote that is not the hub. |
| 4 | Lock held by another process; the contender skipped. |
| 5 | Network unreachable or the hub refused. `sync` only. |
| 6 | A merge conflict was recorded in history. Advisory-loud; the work tree is correct. |
| 64 | Not implemented yet in this build (scaffold only; no verb will return this once its engine lands). |

`gate` returns 0 and nothing else, ever. It is fail-open by contract: a gate that can
fail a tool call is a gate that can stop the operator working.

`stdout` is the product - the gate's advisory block, or one JSON document under
`--json`, and nothing else. `stderr` is the log.

## Layout

```
ams-store/
  cmd/ams-store/       thin main: the ldflags version stamp, then cli.Run
  cli/                 the whole command surface, one file per verb
  internal/
    store/             constants, paths, fail-closed enumeration, alias dedup, temp sweep
    index/             parse, render (verbatim and derived), line, round-trip, reach, ghosts
    frontmatter/       the leading --- block, the doctrine rule, the hook harvest
    atomic/            every file write: .am-tmp + rename + SHA-256 readback, JSON state
    gitx/              every git invocation: argv, env, timeout, exit classifier, >= 2.38
    testutil/          sandboxes and fixtures (temp PROJECTS_ROOT + STATE_ROOT + history.git)
    porting/           the 1:1 Pester counterpart table, skipped until its engine lands
```

Rules the layout enforces, each of which has cost something before:

- `cmd/` holds no logic.
- Every `git` invocation goes through `gitx`, so argv, environment, timeouts and the
  version floor are pinned in one place.
- Every file write goes through `atomic`, so nothing can leave a full copy of an index
  behind in a directory the harness syncs and agents glob.
- `derive` and `gate` share ONE floor implementation and ONE doctrine rule. Two
  implementations of a rule are two rules.
- Source files are ASCII-only. Non-ASCII runtime characters are built from code points
  (`store.EmDash`), never typed, so the file survives every editor and every encoding
  guess the PowerShell siblings have to make.
- Nothing but `MEMORY.md` and fact files may ever be written inside a store. Maintainer
  state lives under the state root.

## Gates

Run from `ams-store/`:

```
go build ./...
go vet ./...
go test ./... -count=1
```

There is no Makefile and no build script; those three are the gates. CI adds `-race` on
Linux and a test+build job on Windows.

A pushed tag `v<VERSION>` runs the `release-assets` job in `.github/workflows/ci.yml`: it
refuses a tag that disagrees with the `VERSION` file, cross-compiles the three targets
below with the version stamp set to the tag, writes `SHA256SUMS` and attaches everything
to the GitHub release of that tag. The installers download and verify from there; nothing
builds the binary on a PC and nothing commits one.

Cross-compile targets (`CGO_ENABLED=0`, static):

```
GOOS=windows GOARCH=amd64   ->  ams-store.exe
GOOS=linux   GOARCH=amd64   ->  ams-store
GOOS=linux   GOARCH=arm64   ->  ams-store
```

Version stamp:

```
go build -ldflags "-s -w \
  -X main.version=$(git describe --tags --always --dirty) \
  -X main.commit=$(git rev-parse --short HEAD) \
  -X main.built=$(date -u +%Y-%m-%dT%H:%M:%SZ)" ./cmd/ams-store
```

## The Pester counterpart table

Every scenario in the four PowerShell suites under `scripts/windows/tests/` has a Go
test of a mapped name from the first commit. The ones this scaffold can assert live in
the package that owns the behaviour; the rest are skipped placeholders in
`internal/porting` whose skip reason names the task that will port them. A task that
implements a scenario moves the test into the owning package and deletes the
placeholder - see `internal/porting/doc.go`.

## Dependencies

None yet. The module will take `github.com/fsnotify/fsnotify` (the state-dir watch) and
`golang.org/x/sys` (process start time, flock) when the code that needs them is written,
and nothing else.
