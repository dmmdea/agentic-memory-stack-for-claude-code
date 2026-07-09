#!/usr/bin/env bash
# stack-promote.sh — H10 fix: safe episodic.db promotion to production.
#
# Usage:
#   bash stack-promote.sh --snapshot <TS>
#
# where <TS> is the timestamp suffix of the restore snapshot, e.g.:
#   bash stack-promote.sh --snapshot 20260611-043000
# (the restored episodic DB is expected at $HOME/.mem0/episodic-restore-<TS>.db
#  or $HOME/.mem0/episodic-restore.db if no timestamp is given).
#
# What this script does:
#   1. Stops mem0.service (and qdrant.service if managed by systemd-user).
#   2. Runs 'PRAGMA integrity_check' on the restore copy — aborts if not OK.
#   3. Backs up the live episodic.db to episodic.db.pre-promote-<TS>.
#   4. Copies the restore copy over the live DB.
#   5. Restarts mem0.service (and qdrant.service).
#   6. Health-checks the mem0 endpoint.
#   7. Appends a production-restore event to the tier-ledger.
#
# H10 rationale: the previous instruction was "cp episodic-restore.db episodic.db"
# with no backup, no service stop, no integrity check, no ledger entry. This script
# replaces that with a safe, audited, reversible promotion flow.
#
# Requires: sqlite3 on PATH, systemctl --user, curl.

set -euo pipefail

MEM0_DIR="$HOME/.mem0"
MEM0_DB="$MEM0_DIR/episodic.db"
# MEM-16 (2026-07-03): ledger writes go to the CURRENT-MONTH segment
# (tier-ledger-YYYY-MM.jsonl, same naming as app.py _append_ledger); the legacy
# tier-ledger.jsonl is a frozen historical archive.
LEDGER="$MEM0_DIR/tier-ledger-$(date -u +%Y-%m).jsonl"
MEM0_URL="http://127.0.0.1:18791"
SNAPSHOT_TS=""

# --- arg parsing ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --snapshot) SNAPSHOT_TS="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

PROMOTE_TS=$(date -u +%Y%m%d-%H%M%S)

if [[ -n "$SNAPSHOT_TS" ]]; then
    RESTORE_DB="$MEM0_DIR/episodic-restore-${SNAPSHOT_TS}.db"
    if [[ ! -f "$RESTORE_DB" ]]; then
        # Also try the generic name (stack-restore.sh uses episodic-restore.db)
        RESTORE_DB="$MEM0_DIR/episodic-restore.db"
    fi
else
    RESTORE_DB="$MEM0_DIR/episodic-restore.db"
fi

echo "=== stack-promote.sh ==="
echo "  promote_ts : $PROMOTE_TS"
echo "  restore_db : $RESTORE_DB"
echo "  live_db    : $MEM0_DB"
echo ""

if [[ ! -f "$RESTORE_DB" ]]; then
    echo "ERROR: restore DB not found at $RESTORE_DB"
    echo "  Run stack-restore.sh first, then re-run stack-promote.sh."
    exit 1
fi

# --- 1. Stop services ---
echo "[1/6] Stopping mem0.service..."
systemctl --user stop mem0.service 2>/dev/null || true
# Stop qdrant only if it is managed as a user service (may not be)
if systemctl --user is-active qdrant.service &>/dev/null; then
    echo "[1/6] Stopping qdrant.service..."
    systemctl --user stop qdrant.service 2>/dev/null || true
    QDRANT_WAS_ACTIVE=1
else
    QDRANT_WAS_ACTIVE=0
fi
sleep 1

# --- 2. Integrity check on restore copy ---
echo "[2/6] Running integrity_check on $RESTORE_DB..."
IC=$(sqlite3 "$RESTORE_DB" "PRAGMA integrity_check;" 2>&1)
if [[ "$IC" != "ok" ]]; then
    echo "ERROR: integrity_check FAILED on restore DB:"
    echo "$IC"
    echo "Aborting promote. Restarting services..."
    systemctl --user start mem0.service 2>/dev/null || true
    [[ $QDRANT_WAS_ACTIVE -eq 1 ]] && systemctl --user start qdrant.service 2>/dev/null || true
    exit 1
fi
echo "  integrity_check: OK"

# --- 3. Backup live DB ---
BACKUP_PATH="$MEM0_DB.pre-promote-$PROMOTE_TS"
if [[ -f "$MEM0_DB" ]]; then
    echo "[3/6] Backing up live DB to $(basename $BACKUP_PATH)..."
    cp "$MEM0_DB" "$BACKUP_PATH"
    echo "  backup: $BACKUP_PATH"
else
    echo "[3/6] No existing live DB to back up (first-time promote)."
fi

# --- 4. Promote ---
echo "[4/6] Promoting restore DB to production..."
cp "$RESTORE_DB" "$MEM0_DB"
echo "  $RESTORE_DB -> $MEM0_DB"

# --- 5. Restart services ---
echo "[5/6] Restarting services..."
[[ $QDRANT_WAS_ACTIVE -eq 1 ]] && systemctl --user start qdrant.service 2>/dev/null || true
systemctl --user start mem0.service 2>/dev/null || true
sleep 3

# --- 6. Health check ---
echo "[6/6] Health check..."
HC=$(curl -sf "$MEM0_URL/health" -H "X-API-Key: $(cat $MEM0_DIR/api-key 2>/dev/null)" 2>&1) || true
if echo "$HC" | grep -q '"status"'; then
    echo "  health: OK ($HC)"
else
    echo "  WARNING: health check inconclusive: $HC"
    echo "  Check 'systemctl --user status mem0.service' manually."
fi

# --- 7. Ledger entry ---
echo "[7/6] Writing production-restore ledger entry..."
# MEM-16: append unconditionally — a fresh month's segment won't exist yet and
# the old "-f or skip" guard would silently drop the restore audit event.
if [[ -d "$MEM0_DIR" ]]; then
    LEDGER_ENTRY=$(python3 -c "
import json, datetime
entry = {
    'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'event': 'production-restore',
    'actor': 'stack-promote.sh',
    'restore_db': '$(basename $RESTORE_DB)',
    'backup_path': '$(basename $BACKUP_PATH 2>/dev/null || echo none)',
    'promote_ts': '$PROMOTE_TS',
    'snapshot_ts': '${SNAPSHOT_TS:-unknown}',
    'reason': 'H10: safe episodic.db promotion via stack-promote.sh',
}
print(json.dumps(entry))
" 2>/dev/null || echo '{}')
    echo "$LEDGER_ENTRY" >> "$LEDGER"
    echo "  ledger entry appended"
else
    echo "  WARNING: ledger not found at $LEDGER; skipping entry"
fi

echo ""
echo "=== Promote complete ==="
echo "  Live DB backed up to: $BACKUP_PATH"
echo "  Production DB promoted from: $RESTORE_DB"
echo "  To undo: cp '$BACKUP_PATH' '$MEM0_DB' (and restart mem0.service)"
