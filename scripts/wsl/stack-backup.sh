#!/usr/bin/env bash
# Weekly snapshot of the agentic memory stack. Idempotent - keeps last 8 weeks.
#
# v0.14 C hardening:
#   - Qdrant block moved AFTER local-file backups (Qdrant outage can't kill local backups)
#   - SQLite online-backup API (sqlite3 .backup) instead of bare cp (safe for live DB)
#   - Atomic tmp-then-rename for all copies (no partial writes on crash)
#   - Path validation for Qdrant snapshot name (no path traversal)
#   - Post-backup integrity checks: sqlite3 pragma integrity_check + test -s for others
#   - set +e around Qdrant block so local backups always complete first
# Note: NOT set -euo pipefail globally; individual errors handled per-block.

TS=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="$HOME/.mem0/backups"
mkdir -p "$BACKUP_DIR"
rc=0

# DR fix (2026-06-20): the LIVE mem0 vector collection is mem0_egemma_768 (config.py).
# It was "memories" before the EmbeddingGemma migration; the old collection still exists
# frozen (~2165 pts) while the live store grew in mem0_egemma_768 (3028+). Snapshotting the
# stale name silently backed up the WRONG vectors. Single source of truth here so it can't
# drift again. Override via env if the collection is ever renamed.
QDRANT_COLLECTION="${MEM0_QDRANT_COLLECTION:-mem0_egemma_768}"

echo "stack-backup: starting TS=$TS BACKUP_DIR=$BACKUP_DIR"

# ── 1. Local file backups (always run; Qdrant outage must not skip these) ─────

# 1a. history.db — use SQLite online backup API (safe for live DB)
HIST_SRC="$HOME/.mem0/history.db"
HIST_DST="$BACKUP_DIR/history-$TS.db"
if [ -f "$HIST_SRC" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$HIST_SRC" ".backup '$HIST_DST.tmp'" 2>/dev/null \
      && mv "$HIST_DST.tmp" "$HIST_DST" \
      && echo "history.db backed up" \
      || { echo "WARN: history.db backup failed" >&2; rc=1; }
    if [ -f "$HIST_DST" ]; then
      result=$(sqlite3 "$HIST_DST" 'pragma integrity_check' 2>&1)
      if [ "$result" != "ok" ]; then
        echo "WARN: history.db integrity_check: $result" >&2; rc=1
      else
        echo "history.db integrity OK"
      fi
    fi
  else
    # sqlite3 not installed — fall back to cp (no online-backup guarantee; acceptable for
    # low-write-rate history.db while sqlite3 is absent)
    cp "$HIST_SRC" "$HIST_DST.tmp" 2>/dev/null && mv "$HIST_DST.tmp" "$HIST_DST" \
      && echo "history.db backed up (cp fallback; install sqlite3 for online-backup)" \
      || { echo "WARN: history.db backup failed (cp fallback)" >&2; rc=1; }
  fi
fi

# 1b. tier-ledger.jsonl
LEDGER_SRC="$HOME/.mem0/tier-ledger.jsonl"
LEDGER_DST="$BACKUP_DIR/tier-ledger-$TS.jsonl"
if [ -f "$LEDGER_SRC" ]; then
  cp "$LEDGER_SRC" "$LEDGER_DST.tmp" && mv "$LEDGER_DST.tmp" "$LEDGER_DST" \
    || { echo "WARN: tier-ledger.jsonl backup failed" >&2; rc=1; }
  test -s "$LEDGER_DST" || { echo "WARN: tier-ledger backup empty" >&2; rc=1; }
fi

# 1c. MEMORY.md
MEMORY_SRC="$HOME/.mem0/MEMORY.md"
MEMORY_DST="$BACKUP_DIR/MEMORY-$TS.md"
if [ -f "$MEMORY_SRC" ]; then
  cp "$MEMORY_SRC" "$MEMORY_DST.tmp" && mv "$MEMORY_DST.tmp" "$MEMORY_DST" \
    || { echo "WARN: MEMORY.md backup failed" >&2; rc=1; }
  test -s "$MEMORY_DST" || { echo "WARN: MEMORY.md backup empty" >&2; rc=1; }
fi

# 1d. audit-flags.baseline
BASELINE_SRC="$HOME/.mem0/audit-flags.baseline"
BASELINE_DST="$BACKUP_DIR/audit-flags-$TS.baseline"
if [ -f "$BASELINE_SRC" ]; then
  cp "$BASELINE_SRC" "$BASELINE_DST.tmp" && mv "$BASELINE_DST.tmp" "$BASELINE_DST" \
    || { echo "WARN: audit-flags.baseline backup failed" >&2; rc=1; }
  test -s "$BASELINE_DST" || { echo "WARN: audit-flags.baseline backup empty" >&2; rc=1; }
fi

# 1e. episodic.db — v0.15: SQLite + FTS5 episodic sidecar (session goals + summaries).
# Use SQLite online-backup API (same pattern as history.db) — safe for live DB with WAL mode.
EPISODIC_SRC="$HOME/.mem0/episodic.db"
EPISODIC_DST="$BACKUP_DIR/episodic-$TS.db"
if [ -f "$EPISODIC_SRC" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$EPISODIC_SRC" ".backup '$EPISODIC_DST.tmp'" 2>/dev/null \
      && mv "$EPISODIC_DST.tmp" "$EPISODIC_DST" \
      && echo "episodic.db backed up" \
      || { echo "WARN: episodic.db backup failed" >&2; rc=1; }
    if [ -f "$EPISODIC_DST" ]; then
      result=$(sqlite3 "$EPISODIC_DST" 'pragma integrity_check' 2>&1)
      if [ "$result" != "ok" ]; then
        echo "WARN: episodic.db integrity_check: $result" >&2; rc=1
      else
        echo "episodic.db integrity OK"
      fi
    fi
  else
    # sqlite3 not installed — fall back to cp
    cp "$EPISODIC_SRC" "$EPISODIC_DST.tmp" 2>/dev/null && mv "$EPISODIC_DST.tmp" "$EPISODIC_DST" \
      && echo "episodic.db backed up (cp fallback; install sqlite3 for online-backup)" \
      || { echo "WARN: episodic.db backup failed (cp fallback)" >&2; rc=1; }
  fi
fi

# 1f. ~/.claude/settings.json (Windows side) — v0.20 Final (adversarial-review
# HIGH): the UserPromptSubmit/SessionStart hook registrations the whole prompt
# pipeline depends on lived ONLY outside every backup. Capture them so DR
# restores the registration alongside the WSL DBs.
# v1.0 Phase 7A: resolve the Windows user from the operator receipt (~/.mem0/stack.env),
# falling back to cmd.exe — never hardcode the developer handle.
WIN_USER_BK="${MEM0_WIN_USER:-}"
[ -z "$WIN_USER_BK" ] && [ -f "$HOME/.mem0/stack.env" ] && WIN_USER_BK="$(. "$HOME/.mem0/stack.env" 2>/dev/null; echo "${MEM0_WIN_USER:-}")"
[ -z "$WIN_USER_BK" ] && WIN_USER_BK="$(cmd.exe /c 'echo %USERNAME%' 2>/dev/null | tr -d '\r\n ')"
SETTINGS_SRC="/mnt/c/Users/$WIN_USER_BK/.claude/settings.json"
SETTINGS_DST="$BACKUP_DIR/claude-settings-$TS.json"
if [ -f "$SETTINGS_SRC" ]; then
  cp "$SETTINGS_SRC" "$SETTINGS_DST.tmp" && mv "$SETTINGS_DST.tmp" "$SETTINGS_DST" \
    || { echo "WARN: claude settings.json backup failed" >&2; rc=1; }
  test -s "$SETTINGS_DST" || { echo "WARN: claude settings.json backup empty" >&2; rc=1; }
fi

echo "stack-backup: local files done (rc=$rc so far)"

# ── 2. Qdrant snapshot (isolated — failure here does NOT affect above) ─────────
(
  set +e
  SNAP=$(curl -sf -X POST "http://127.0.0.1:6333/collections/$QDRANT_COLLECTION/snapshots" | jq -r '.result.name // empty')
  if [ -z "$SNAP" ]; then
    echo "WARN: Qdrant snapshot request failed or returned empty name — skipping" >&2
    exit 0
  fi

  # Validate snapshot name: no empty, no path separators, no dot-prefix (traversal guard)
  case "$SNAP" in
    ""|*/*|.*)
      echo "WARN: bad Qdrant snapshot name '$SNAP' — refusing to copy" >&2
      exit 0
      ;;
  esac

  SNAP_SRC="$HOME/qdrant-server/snapshots/$QDRANT_COLLECTION/$SNAP"
  SNAP_DST="$BACKUP_DIR/qdrant-$TS.snapshot"
  if [ -f "$SNAP_SRC" ]; then
    cp "$SNAP_SRC" "$SNAP_DST.tmp" && mv "$SNAP_DST.tmp" "$SNAP_DST" \
      && echo "qdrant snapshot $SNAP -> $SNAP_DST" \
      || echo "WARN: failed to copy Qdrant snapshot" >&2
    test -s "$SNAP_DST" || echo "WARN: qdrant snapshot backup empty" >&2
  else
    echo "WARN: Qdrant snapshot file not found at $SNAP_SRC" >&2
  fi
) || true

echo "stack-backup: Qdrant block done"

# ── 3. Prune: keep last 8 snapshots of each kind ──────────────────────────────
for kind in qdrant history tier-ledger MEMORY audit-flags episodic claude-settings; do
  ls -1t "$BACKUP_DIR/$kind"-*.* 2>/dev/null | tail -n +9 | xargs -r rm -f
done

du -sh "$BACKUP_DIR"

# ── 4. Backup manifest (v0.17 Phase B) ────────────────────────────────────────
# Write manifest-$TS.json documenting counts + file list for this snapshot.
# Run after all backup files (including Qdrant) are written. Fail open.
MANIFEST_SCRIPT="$(dirname "$0")/stack-backup-manifest.sh"
if [ -f "$MANIFEST_SCRIPT" ]; then
    bash "$MANIFEST_SCRIPT" "$TS" || echo "WARN: manifest writer failed (non-fatal)" >&2
else
    echo "WARN: stack-backup-manifest.sh not found at $MANIFEST_SCRIPT" >&2
fi

echo "stack-backup: complete (rc=$rc)"
exit $rc
