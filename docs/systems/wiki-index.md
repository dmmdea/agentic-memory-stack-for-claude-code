# Wiki Index

## Purpose

Gives sessions a semantic search over the operator's LLM Wiki — a curated Obsidian markdown
vault that is the canonical record of the operator's ecosystem — without ever making the search
index a source of truth. The vault lives on a cloud-synced folder that only the operator's PCs
mount; the index is a derived Qdrant collection on the brain box, rebuilt from the vault by two
paths that back each other up (and a PC-side catch-up that runs the fast one when it is due).

## Questions this doc answers

- Where does the wiki index live, and why not in every replica's Qdrant?
- What refreshes it, and what happens when a session forgets?
- What can the nightly pull key do on a PC, and why is that all it can do?
- Why is the index outside the backup set, and what is the restore?
- What does a green, degraded or red `wiki-index` receipt mean, and what is "fresh"?
- What happens when every PC was off at 03:00?

## Scope

One collection, `wiki_pages_egemma_768`, in the brain box's Qdrant: one point per page under
the vault's `wiki/` tree (`entities/`, `concepts/`, `sources/`, `syntheses/`), embedded with the
same EmbeddingGemma prefix shim as `mem0` (`MEM0_EMBED_MODEL`, the store's exact GGUF), payload
`{path, title, type, tags, updated, summary, hash}`. Builds are idempotent and incremental on
the content hash; deleted pages are removed.

## Non-scope

The vault itself (its schema, ingest workflow and ledger are the operator's, outside this
repo), the raw sources folder (never walked — it is large and cloud-hydrated on demand), and
the mem0 corpus (a different collection with a different life cycle).

## Components

| piece | where it runs | what it does |
| --- | --- | --- |
| `scripts/wsl/wiki-index-build.py` | brain box or a replica's WSL | embeds a `wiki/` snapshot (`WIKI_ROOT`, default `~/wiki-index/wiki`) into `WIKI_QDRANT_HOST:PORT` (default the local loopback) |
| `scripts/wsl/wiki-search.py` | same | query embedding + top-K JSON lines |
| `scripts/wsl/wiki-index.sh` | a replica's WSL | `snapshot` (tar of `wiki/` on stdin), `build` (then stamps `~/wiki-index/last-build` on the brain through the same tunnel; fail-soft), `search` — the last two through an SSH control-socket tunnel to the brain's loopback Qdrant (first free local port from `WIKI_TUNNEL_PORT`, default 16333; closed on exit). The brain alias resolves from `WIKI_BRAIN_SSH`, then `MEM0_BRAIN_SSH` in `stack.env` (no installer flag sets it; the operator adds the line by hand, and since 1.31.3 every `stack.env` writer carries it over on a re-run), then the `~/.ssh/config` Host whose `HostName` is the authority-url's host |
| `scripts/wsl/wiki-index-nightly.sh` | brain box, chain step `wiki-index` | pulls `wiki/` from the first reachable PC in `MEM0_WIKI_SOURCES`, builds locally, stamps `last-pull` and `last-build`, and receipts the night (below) |
| `claude-config/wiki-index-refresh.sh` | a PC that mounts the vault (Git Bash), deployed to `~/.claude/scripts/` | the session refresh driver: tars the vault's `wiki/` into WSL, runs `wiki-index.sh snapshot` + `build`, and on success writes the local stamp `~/.claude/state/last-wiki-refresh`. The vault directory is operator configuration: the argument, `WIKI_VAULT`, or the first line of `~/.mem0/wiki-vault` |
| `scripts/windows/wiki-index-catchup.ps1` | a replica PC, detached child of `memory-maintenance-spawn.ps1` at SessionStart | runs the driver when the index is due (below); no scheduled task |
| `systemd/ams-step-wiki-index.service` | brain box | `--guarded`, after `index-refresh`, before the stamping `stack-backup`; rendered only when `--wiki-sources` is configured |

## Two refresh paths

**Session refresh (fast).** After a session ingests a source or edits pages it runs the
operator's refresh driver on the PC that mounts the vault: it tars `wiki/` into WSL
(`wiki-index.sh snapshot`) and builds through the tunnel (`wiki-index.sh build`). Seconds;
`unchanged=N` is the normal readback. The vault's own operating rules name this as the last
step of an ingest.

**Nightly backstop.** The brain's chain step pulls `wiki/` from the first reachable PC and
builds into its own Qdrant, so pages edited without a refresh are caught the next night. Its
first live run upserted three pages other sessions had changed without refreshing.

**PC catch-up (when due).** A replica PC is off at 03:00 as often as not, so at SessionStart
`wiki-index-catchup.ps1` (a detached child, so its HTTP call never delays the session) reads
`wiki.fresh_age_h` from the authority's `/health/maintenance` and runs the session refresh in the
background when the index is older than 20 h, or when any `wiki/*.md` page in the vault is newer
than the PC's own refresh stamp. Attempts are throttled to one per 6 h; an unreachable authority
or an authority that does not report `wiki` yet falls through to the page-time rule; the brain
role, and a box with no vault configured, never run it. The vault directory is a hand-set,
per-box operator value (`WIKI_VAULT`, or one line in `~/.mem0/wiki-vault`; no installer flag sets
it). The drive letter of a cloud-synced folder moves, so a configured path that is gone is
retried on the other letters.

Both paths write the same points (ids are `uuid5("wiki:" + relative path)`), so they never
duplicate and either can restore what the other left.

## Freshness

The index is as fresh as the newer of two stamps on the brain: `~/wiki-index/last-pull` (the
nightly's own pull) and `~/wiki-index/last-build` (the last successful build by either path: the
nightly, or a session's `wiki-index.sh build` from any PC). `fresh_age_h` is the age of that
newer stamp. The nightly's 72 h failure limit measures `fresh_age_h`, not the pull, so a fresh
session-side refresh keeps a night with every PC off from reading red, and
`/health/maintenance` reports the value as `wiki.fresh_age_h`.

## The pull key

The brain pulls with a dedicated key (`MEM0_WIKI_PULL_KEY`, default `~/.ssh/id_ed25519_wiki_pull`).
On each PC the operator pins that key in the SSH server's authorized keys to a forced command
(`command="<wrapper that streams the wiki tar>",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding`),
so the key can do exactly one thing: stream `wiki/`. The nightly script sends a placeholder
remote word (`wiki-tar`) that the forced command ignores; a key that returned a shell instead of
a tar would fail the extraction and be reported as "unreachable or empty".

## Exit policy of the nightly step

| situation | exit | receipt |
| --- | --- | --- |
| a source answered and the build ran | 0 | `ok`; `last-pull` and `last-build` stamped |
| no source reachable, `fresh_age_h` <= 24 | 0 | `ok`: the index is kept as-is; the outcome JSON carries each source's ssh exit and stderr |
| no source reachable, 24 h < `fresh_age_h` <= 72 h (`WIKI_MAX_STALE_H`) | 0 | `degraded:no-source-fresh-<h>h`: a skipped night is visible the first night the index ages past a day, not only at the limit |
| no source reachable, `fresh_age_h` > 72 h, or no stamp ever recorded | 1 | `failed`; the note names the age |
| the build failed after a pull | the builder's | `failed`; `last-build` is not stamped |
| `MEM0_WIKI_SOURCES` unset while the unit exists | 1 | a misinstall, loud on purpose |
| an empty tar | treated as unreachable | the previous snapshot is kept |

The thresholds are compared in seconds; the reason names whole hours (the floor). Each
unreachable source is also written to the step's stderr as `<source> unreachable or empty (ssh
exit N): <ssh stderr>`, so "host down", "tailnet offline", "sshd refusing" and a forced command's
"vault not found" read differently. The outcome line follows the chain's step contract
(one line written to `AMS_OUTCOME_FILE`, `<status>[:<reason>] <json>`).

## Backup and restore

The index is deliberately outside the stack backup and the replica restore manifest (only the
mem0 collection is snapshotted). A lost or wrong index is rebuilt by either refresh path; the
vault is the record. Do not add it to the backup set — a stale restored index would shadow a
correct rebuild.

## Invariants

- The vault is never modified by anything in this repo: reads only, `wiki/` only.
- The nightly never retries and never reaches a PC that is off; the PC catch-up is the retry,
  and it adds no scheduled task.
- A replica never builds into its own Qdrant; the brain never tunnels.
- The pull key has no shell (forced command) and no forwarding.
- Nothing in the repo names an operator machine: hosts come from `stack.env`, aliases from the
  operator's SSH config.

## Pitfalls

- A Windows PC's cloud-drive letter can move; the operator's tar wrapper resolves it at run
  time. A `wiki-index-build.py` that finds no `WIKI_ROOT` reports the path it looked for.
- `tr`/`sed` with backslashes sent through SSH into a Windows shell lose the backslash; deploy
  the scripts by copying files, then compare hashes.
- Two sessions may hold tunnels at once; the port range handles it, `ExitOnForwardFailure`
  makes a busy port fail fast rather than serve nothing.

## Source map

- `scripts/wsl/wiki-index-build.py`, `scripts/wsl/wiki-search.py`, `scripts/wsl/wiki-index.sh`,
  `scripts/wsl/wiki-index-nightly.sh`, `claude-config/wiki-index-refresh.sh`,
  `scripts/windows/wiki-index-catchup.ps1` (spawned from `scripts/windows/memory-maintenance-spawn.ps1`,
  deployed by `install/2-windows-config.ps1`)
- `systemd/ams-step-wiki-index.service`; `install/linux-authority.sh` (`--wiki-sources`,
  `MEM0_WIKI_SOURCES` / `MEM0_WIKI_PULL_KEY` in `stack.env`, the unit dropped when unconfigured).
  The flag takes a comma- or space-separated list. Since 1.31.1 it is stored comma-separated,
  because `stack.env` is sourced by bash, and the nightly step splits on commas and whitespace,
  so an older space-separated receipt still works until the next install rewrites it
- Tests: `scripts/wsl/test_wiki_index.py`, `scripts/wsl/test_wiki_index_nightly.py`,
  `scripts/wsl/test_wiki_index_wrapper.py`, `claude-config/tests/test_wiki_index_refresh.py`,
  `scripts/windows/tests/WikiIndexCatchup.Tests.ps1`, `mem0-server/tests/test_linux_authority_installer.py`
- Related: [installer-and-deploy.md](./installer-and-deploy.md) (the chain),
  [../operations.md](../operations.md) (schedules and receipts)
