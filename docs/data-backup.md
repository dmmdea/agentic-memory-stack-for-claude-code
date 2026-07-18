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
