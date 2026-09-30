# Data Backup (separate from code backup)

This repo backs up the **code, configs, and installer** for the agentic memory stack. It does **not** back up your actual memory data — that's a separate concern with different trade-offs (size, sensitivity, sync frequency).

## What "data" means

| Path (default install) | Contains | Typical size |
|---|---|---|
| `~/.mem0/history.db` (WSL SQLite) | mem0 fact history | 1-50 MB |
| `~/.mem0/api-key` (WSL) | mem0 API key (regenerated on install) | <1 KB |
| `~/.mem0/audit-flags.jsonl` (WSL) | L10 audit history | 1-10 MB |
| `~/.mem0/tier-ledger*.jsonl` (WSL) | Promotion/demotion history (monthly segments + frozen legacy file) | <1 MB |
| `~/qdrant-server/storage/` (WSL) | Qdrant collection: vectors + payload | 100 MB - several GB |

The largest store is Qdrant storage (vectors). The rest is small.

## What the daily `stack-backup.sh` snapshot covers

The installed timer (03:30 daily) writes dated artifacts into `~/.mem0/backups/` (last 8
kept per kind) plus a `manifest-<TS>.json` that lists **exactly what that snapshot
contains** (absent artifacts are an explicit `null`, never a hoped-for name):

| artifact | source | note |
|---|---|---|
| `qdrant-<TS>.snapshot` | live collection snapshot API | vectors + payloads |
| `history-<TS>.db` / `episodic-<TS>.db` | SQLite online-backup | integrity-checked |
| `tier-ledger-<TS>.jsonl` | legacy + monthly segments, concatenated | |
| `MEMORY-<TS>.md` | auto-memory index | |
| `audit-flags-<TS>.baseline` | L10 baseline | only if the file exists |
| `claude-settings-<TS>.json` | Windows `~/.claude/settings.json` | hook registrations |
| `l10-flags-<TS>.jsonl` | `~/.mem0/audit-flags.jsonl` | the L10 flag log itself |
| `l10-state-<TS>.json` | `~/.mem0/l10-state.json` | operator `reviewed_keys`; copy is parse-validated (a torn copy is refused, because restoring it would resurrect every reviewed flag) |
| `promote-review-<TS>.jsonl` | contradiction promote-review queue | queued human decisions |
| `stale-worksheet-<TS>.jsonl` | stale-paths hand-label worksheet | operator labels — not regenerable |
| `qcol-episodes-<TS>.snapshot` | the `episodes_*` collection | small; rebuildable with `episode-embed-backfill.py` |
| `qcol-entities-<TS>.snapshot` | the `*_entities` collection | written by the mem0 library; the snapshot is its only copy |
| `qcol-wiki-<TS>.snapshot` | the `wiki_pages_*` collection | small; rebuildable with `wiki-index-build.py` |

Every collection that matches a kind is snapshotted, so a second `wiki_pages_*` (or `episodes_*`,
`*_entities`) collection is backed up, not skipped, and is not a degradation. Within a kind the first
collection in name order keeps the plain `qcol-<kind>-<TS>.snapshot` name (the manifest's fixed
`qdrant_*` keys name it); each further one is `qcol-<kind>+<collection>-<TS>.snapshot`, listed and
checksummed under the manifest's top-level `qdrant_extra_collections` array, and trimmed to its own
newest 8. A collection that stops existing leaves its last (up to 8) snapshots behind; they are
plain files an operator can delete.

A failed secondary snapshot warns and leaves its manifest entry `null`, but does not fail the
night: those collections are small, episodes and wiki are rebuildable (the entities snapshot is the
only copy of that collection, which is why a miss reads `degraded` and not clean), and a red night
stops the off-box copy. The collection list itself is read strictly: a `GET /collections` that fails, or answers with a body that
is not a collection list, reads `degraded` (`secondary-snapshot-failed`), never a clean night with no
secondary snapshotted.

### Retention

Each kind keeps its newest 8 entries. An entry is a **real artifact**: `<kind>-<timestamp>.<ext>`
for one explicit extension per kind, ordered by the timestamp in the name. SQLite `-wal` / `-shm`
sidecars (a read-only opener of a WAL-mode backup leaves them newer than every real `.db`) and
`.tmp` partials are never counted, so they cannot push a real backup out of the window; orphan
sidecars of an empty WAL are swept. Read a backup database with
`file:<path>?mode=ro&immutable=1` (a plain `mode=ro` open creates the sidecars). Manifests age
out with their sets.

### Qdrant server-side snapshots

The snapshot API leaves its own copy under `~/qdrant-server/snapshots/<collection>/`. After the
copy into `backups/` is verified (byte size equal, and the sha256 in Qdrant's `.checksum` file
equal to the copy's), the newest 2 snapshots stay server-side and older ones are deleted through
the API; a copy that fails verification is removed and nothing server-side is deleted. Hand-made
`qdrant-*.snapshot` one-offs older than 14 days are swept. Before this the store kept every
nightly snapshot, roughly a second full copy of the vectors per day.

A failed server-side list, DELETE or sweep, or a secondary collection whose snapshot could not be
taken, does not turn the night red (a red night makes the off-box copy refuse the primary set)
but it does not read as a clean success either: the step writes one `degraded` outcome line to
`AMS_OUTCOME_FILE` (the chain's step outcome contract, described in
[`systems/installer-and-deploy.md`](./systems/installer-and-deploy.md)):
`degraded:secondary-snapshot-failed,server-prune-failed {"secondary_snapshot_failed":N,"server_prune_failed":M}`,
with only the reason that applies. The receipt then reads `status: degraded` (still `ok: true`, so
the off-box copy still runs) with the counts in `work`, and `/health/maintenance` lists the step
under `degraded_steps`. The entities collection has no rebuild path, so a missing
`qcol-entities-*` snapshot is a real gap, not noise.

### Manifest fields

`app_version` and `git_sha` come from the `VERSION` and `DEPLOYED_SHA` stamps written beside the
server modules (the deployed tree has no `.git`): by `deploy.sh` on a WSL host, and by the
installers (`install/linux-authority.sh`, `install/linux-replica.sh`, `install/1-wsl-services.sh`)
on every install and refresh, because `deploy.sh` refuses a native host. The installers and
`deploy.sh` write it only through the shared `install/deploy-stamp.sh` contract: one line, a 40-hex
sha or the word `unknown` (a tree with no readable `.git` and no stamp of its own), so a stale sha
is never kept and a checkout git cannot read never leaves an empty file. `deploy.sh` reads the
stamp back as the rollback ref it prints after a red retrieval gate, and takes only a sha for it. The stamp is authoritative for the manifest: `git_sha` is its 40-hex sha, and `unknown` when
the stamp reads `unknown` or is empty or malformed, because a checkout that happens to be reachable
names its own commit, not the deployed one. Only a tree with no stamp file at all (the writer
running straight from a checkout no installer deployed) asks that checkout; with none, it says
`unknown`. `checksums` maps every file in the set to its `size` and `sha256`. `stack.env` is read by
key (`grep '^KEY='`), never sourced, so a malformed line cannot stop the writer.

**Deliberately excluded** (so nobody re-litigates; the secondary Qdrant collections are no longer
in this list, they ride in the set): `pair-verdict-cache.db` (TTL'd,
rebuilt by the next sweep), `jobs.db` (transient queue state), `canonical-replay.jsonl`
(anti-replay nonces; signed tokens carry a 300 s skew gate and the ledger GCs at 600 s,
so losing it reopens at most a 10-minute-old-token window), telemetry/receipt ledgers
(`retrieval-log.jsonl`, `admission-rejected.jsonl`, per-job receipt files — operational
history, regenerated by operation), live-DB internals (`*-wal`, `*-shm`), ad-hoc `*.bak`
files, and scripts that live in this repo. Keys (`api-key`, `canonical-key.dpapi`,
`authority-url`) are `key-backup.sh`'s job, documented in
[`systems/key-custody.md`](./systems/key-custody.md).

## Off-box copies

**Cloud mirror (`ams-pcloud-copy.sh`).** Mirrors the newest set to the synced cloud folder. It
copies files as they are: the mirror is **not client-side encrypted**, so treat the cloud account as
holding the raw corpus (encrypting it is an operator decision, not something the script does). The
copy refuses, with exit 5 and the outcome `failed:stale-set` (or `failed:stack-backup-not-ok`), when
the newest local manifest is older than 26 h or the latest `stack-backup` receipt is not ok, so a
failed night can no longer read green by re-copying yesterday's set. A `degraded` night is ok, so
its whole primary set still travels. It copies only real artifacts (never sidecars or `.tmp`),
data files first and the manifest last, and compares sizes before the manifest travels (exit 6 and
`failed:copy-size-mismatch` on a mismatch). A refusal leaves a `failed` receipt: the exit code, the
message as the note, and the outcome line's counts in `work` (`{"age_h": N}`, or the file name), and
`/health/maintenance` lists the step under `failed_steps`. After a verified copy it keeps the
newest 7 **complete** sets (a set is complete when its manifest exists), deletes older sets and
dead partials, and never deletes the newest complete set. `AMS_PCLOUD_MAX_AGE_H` and
`AMS_PCLOUD_KEEP_SETS` override the two limits.

**ZFS replica (recovery point).** The replication step is an operator-side script on the brain
(it is not in this repo). It sends with `--no-sync-snap`, so it ships only snapshots that already
exist, and the newest one at chain time is the hourly snapshot taken before that night's
`stack-backup` wrote its set. The replica therefore lags one backup set: after a total loss of the
brain, the replica holds the previous night's set and the live store as of that earlier snapshot
(RPO about 24 h), while the cloud mirror holds the latest set. Operator-side remedy: drop
`--no-sync-snap` so syncoid takes its own snapshot after the backup step, or take a named snapshot
at the end of the chain and send that. The replica target may also be a single disk; two copies on
single disks are not a redundant pair.

## Recommended approaches (pick one)

### Option A — daily compressed snapshot to a local backup dir

Simple. No external service. Survives a reinstall on the same machine but **does not survive a disk wipe**.

Add to a cron or systemd-user timer that runs daily:

```bash
#!/bin/bash
BACKUP_DIR="$HOME/backups/memory-stack"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y-%m-%d)

# Stop services briefly to get a consistent snapshot
systemctl --user stop mem0.service qdrant.service

tar -czf "$BACKUP_DIR/memory-data-$DATE.tar.gz" \
    -C "$HOME" \
    .mem0/ \
    qdrant-server/storage

systemctl --user start qdrant.service mem0.service

# Keep last 7 days
ls -1t "$BACKUP_DIR"/memory-data-*.tar.gz | tail -n +8 | xargs -r rm
```

### Option B — daily snapshot to OneDrive / Google Drive / iCloud

Same as A but with the backup dir pointed at a synced folder. Survives a disk wipe. Sensitive data lives in your cloud — assess privacy.

### Option C — git-lfs to a separate private repo

For users who want full versioning of memory data. Heavier. The Qdrant collection can grow into GB territory.

### Option D — accept the loss

**Moving to a new machine? Use the full runbook: [`MIGRATION.md`](./MIGRATION.md)** — snapshot → fresh install → restore into production targets → fresh key → verify. The rest of this section is the older, minimal alternative: if your daily L1a extraction is reliable and you'd accept re-accumulating on a new machine, you *can* do nothing. The high-value facts are in `tier=canonical` and `tier=insight` and they'll regenerate from new sessions. The runbook v0.12 documents an `mem0-backfill.py` script for migrating from an older mem0 install.

## Restoring from a backup

After re-installing the stack on a new machine:

```bash
# 1. Stop services (just installed by .\install.ps1)
systemctl --user stop mem0.service qdrant.service

# 2. Extract backup (overwrites the empty default install state)
cd $HOME
tar -xzf ~/backups/memory-stack/memory-data-YYYY-MM-DD.tar.gz

# 3. Restart
systemctl --user start qdrant.service mem0.service

# 4. Smoke test
curl -s -H "X-API-Key: $(cat ~/.mem0/api-key)" http://127.0.0.1:18791/v1/memories?user_id=youruser&limit=3
```

## What's NOT in scope

- **Sensitive data in memories**: mem0 entries can contain anything you've discussed with Claude. Treat the SQLite/Qdrant as sensitive — don't commit them to a public repo, don't share without redaction.
- **OAuth tokens**: Claude Max credentials (`~/.claude/.credentials.json`) and Codex/ChatGPT credentials (`~/.codex/auth.json`) are device-bound OAuth tokens. They cannot meaningfully be backed up — on a new machine, re-run `claude /login` and `codex login`.
