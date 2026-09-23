# Wiki Index

## Purpose

Gives sessions a semantic search over the operator's LLM Wiki — a curated Obsidian markdown
vault that is the canonical record of the operator's ecosystem — without ever making the search
index a source of truth. The vault lives on a cloud-synced folder that only the operator's PCs
mount; the index is a derived Qdrant collection on the brain box, rebuilt from the vault by two
paths that back each other up.

## Questions this doc answers

- Where does the wiki index live, and why not in every replica's Qdrant?
- What refreshes it, and what happens when a session forgets?
- What can the nightly pull key do on a PC, and why is that all it can do?
- Why is the index outside the backup set, and what is the restore?
- What does a green or red `wiki-index` receipt mean?

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
| `scripts/wsl/wiki-index.sh` | a replica's WSL | `snapshot` (tar of `wiki/` on stdin), `build`, `search` — the last two through an SSH control-socket tunnel to the brain's loopback Qdrant (first free local port from `WIKI_TUNNEL_PORT`, default 16333; closed on exit). The brain alias resolves from `WIKI_BRAIN_SSH`, then `MEM0_BRAIN_SSH` in `stack.env` (no installer flag sets it; the operator adds the line by hand, and since 1.31.3 every `stack.env` writer carries it over on a re-run), then the `~/.ssh/config` Host whose `HostName` is the authority-url's host |
| `scripts/wsl/wiki-index-nightly.sh` | brain box, chain step `wiki-index` | pulls `wiki/` from the first reachable PC in `MEM0_WIKI_SOURCES`, builds locally |
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

Both paths write the same points (ids are `uuid5("wiki:" + relative path)`), so they never
duplicate and either can restore what the other left.

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
| a source answered and the build ran | 0 | `ok:true`, empty note |
| no source reachable, last pull younger than `WIKI_MAX_STALE_H` (72 h) | 0 | `ok:true` — PCs are often off at 03:00; the index is kept as-is |
| no source reachable, last pull older than that | 1 | `ok:false`, the note names the age |
| `MEM0_WIKI_SOURCES` unset while the unit exists | 1 | a misinstall, loud on purpose |
| an empty tar | treated as unreachable | the previous snapshot is kept |

## Backup and restore

The index is deliberately outside the stack backup and the replica restore manifest (only the
mem0 collection is snapshotted). A lost or wrong index is rebuilt by either refresh path; the
vault is the record. Do not add it to the backup set — a stale restored index would shadow a
correct rebuild.

## Invariants

- The vault is never modified by anything in this repo: reads only, `wiki/` only.
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
  `scripts/wsl/wiki-index-nightly.sh`
- `systemd/ams-step-wiki-index.service`; `install/linux-authority.sh` (`--wiki-sources`,
  `MEM0_WIKI_SOURCES` / `MEM0_WIKI_PULL_KEY` in `stack.env`, the unit dropped when unconfigured).
  The flag takes a comma- or space-separated list. Since 1.31.1 it is stored comma-separated,
  because `stack.env` is sourced by bash, and the nightly step splits on commas and whitespace,
  so an older space-separated receipt still works until the next install rewrites it
- Tests: `scripts/wsl/test_wiki_index.py`, `scripts/wsl/test_wiki_index_nightly.py`,
  `mem0-server/tests/test_linux_authority_installer.py`
- Related: [installer-and-deploy.md](./installer-and-deploy.md) (the chain),
  [../operations.md](../operations.md) (schedules and receipts)
