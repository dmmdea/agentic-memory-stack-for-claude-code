#!/usr/bin/env bash
# stack-restore.sh — v0.17 Phase B (Codex HIGH-4 closure)
#
# Restores an agentic memory stack backup into ALTERNATE targets by default
# (memories-restore collection, episodic-restore.db) to avoid clobbering
# production.
#
# Usage:
#   bash stack-restore.sh --snapshot <TS> [--dry-run]
#                         [--target-collection <name>] [--target-episodic <path>]
#
#   <TS> format: YYYYmmdd-HHMMSS (matches backup filenames, e.g. 20260611-053000)
#
# If --snapshot is omitted, lists available snapshots and exits.
#
# Atomic mode: files are written to .tmp paths first; atomic rename only after
# each integrity check passes.

set -uo pipefail

BACKUP_DIR="$HOME/.mem0/backups"
QDRANT_BASE="http://127.0.0.1:6333"
MEM0_APP="$HOME/apps/mem0-server"
PYTHON="$HOME/apps/mem0-server/.venv/bin/python3"
# v0.20 Phase E (M10): env-overridable so the pytest suite can point its
# synthetic runs at a tmp log — suite-driven entries were exactly the
# "indefinite chain of dry-run drills" R4 used to count as proof, and the new
# failure logging below would otherwise leave a bogus outcome=failed tail
# entry after every test run (the suite probes a nonexistent snapshot).
DRILL_LOG="${DRILL_LOG:-$HOME/.mem0/restore-drill.jsonl}"

# v0.19 M9: every completed run (dry-run AND live) appends one JSONL line here.
# Test-MemoryStack R4 reads this log's freshness — drill proof comes from the
# drill itself, not from git commit-message archaeology (which the commit that
# fixed R4 could satisfy).
log_drill() {
    local mode="$1" outcome="$2"
    printf '{"ts":"%s","mode":"%s","snapshot":"%s","outcome":"%s"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$mode" "$SNAPSHOT_TS" "$outcome" \
        >> "$DRILL_LOG" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------

SNAPSHOT_TS=""
DRY_RUN=false
TARGET_COLLECTION="memories-restore"
TARGET_EPISODIC="$HOME/.mem0/episodic-restore.db"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --snapshot)
            SNAPSHOT_TS="$2"; shift 2 ;;
        --dry-run)
            DRY_RUN=true; shift ;;
        --target-collection)
            TARGET_COLLECTION="$2"; shift 2 ;;
        --target-episodic)
            TARGET_EPISODIC="$2"; shift 2 ;;
        *)
            echo "Unknown flag: $1" >&2
            echo "Usage: bash stack-restore.sh --snapshot <TS> [--dry-run] [--target-collection <name>] [--target-episodic <path>]" >&2
            exit 1 ;;
    esac
done

# v0.20 Phase E (M10): log FAILED runs too — the outcome field in
# restore-drill.jsonl is only meaningful if failures appear in it. Any nonzero
# exit after this point (with a snapshot selected) appends outcome=failed; the
# two success-path log_drill calls below are unaffected (rc=0 skips this).
MODE=dry-run
[ "$DRY_RUN" = false ] && MODE=live
trap 'rc=$?; [ $rc -ne 0 ] && [ -n "$SNAPSHOT_TS" ] && log_drill "$MODE" failed' EXIT

# ---------------------------------------------------------------------------
# If no snapshot given, list available and exit
# ---------------------------------------------------------------------------

if [ -z "$SNAPSHOT_TS" ]; then
    echo "Available snapshots (manifest files):"
    ls -1t "$BACKUP_DIR"/manifest-*.json 2>/dev/null \
        | while read -r mf; do
            ts=$(basename "$mf" .json | sed 's/manifest-//')
            pts=$(python3 -c "import json; d=json.load(open('$mf')); print(d['counts']['qdrant_points'])" 2>/dev/null || echo '?')
            echo "  $ts  (qdrant_points=$pts)"
          done
    if ! ls "$BACKUP_DIR"/manifest-*.json >/dev/null 2>&1; then
        echo "  (none found in $BACKUP_DIR)"
    fi
    echo ""
    echo "Re-run with --snapshot <TS> to restore."
    exit 0
fi

# ---------------------------------------------------------------------------
# 1. Read + validate manifest
# ---------------------------------------------------------------------------

MANIFEST="$BACKUP_DIR/manifest-$SNAPSHOT_TS.json"
if [ ! -f "$MANIFEST" ]; then
    echo "ERROR: manifest not found: $MANIFEST" >&2
    echo "Run without --snapshot to list available snapshots." >&2
    exit 1
fi

echo "=== stack-restore v0.17 Phase B ==="
echo "Snapshot   : $SNAPSHOT_TS"
echo "Manifest   : $MANIFEST"
echo "Dry run    : $DRY_RUN"
echo "Target Qdrant collection : $TARGET_COLLECTION"
echo "Target episodic.db       : $TARGET_EPISODIC"
echo ""

# Parse manifest fields with python3
MANIFEST_APP_VERSION=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['app_version'])"           2>/dev/null || echo "unknown")
MANIFEST_SCHEMA_VER=$(python3  -c "import json; print(json.load(open('$MANIFEST'))['schema_version'])"        2>/dev/null || echo "unknown")
MANIFEST_GIT_SHA=$(python3     -c "import json; print(json.load(open('$MANIFEST'))['git_sha'])"               2>/dev/null || echo "unknown")
MANIFEST_QDRANT_PTS=$(python3  -c "import json; print(json.load(open('$MANIFEST'))['counts']['qdrant_points'])" 2>/dev/null || echo 0)
MANIFEST_EP_SESSIONS=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['counts']['episodic_sessions'])" 2>/dev/null || echo 0)
MANIFEST_EP_EPISODES=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['counts']['episodic_episodes'])" 2>/dev/null || echo 0)
MANIFEST_EP_GOALS=$(python3    -c "import json; print(json.load(open('$MANIFEST'))['counts']['episodic_goals'])"    2>/dev/null || echo 0)

QDRANT_SNAP_FILE=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['files']['qdrant_snapshot'])"  2>/dev/null || echo "")
HISTORY_FILE=$(python3     -c "import json; print(json.load(open('$MANIFEST'))['files']['history_db'])"        2>/dev/null || echo "")
LEDGER_FILE=$(python3      -c "import json; print(json.load(open('$MANIFEST'))['files']['tier_ledger'])"       2>/dev/null || echo "")
MEMORY_FILE=$(python3      -c "import json; print(json.load(open('$MANIFEST'))['files']['memory_md'])"         2>/dev/null || echo "")
AUDIT_FILE=$(python3       -c "import json; print(json.load(open('$MANIFEST'))['files']['audit_baseline'])"   2>/dev/null || echo "")
EPISODIC_FILE=$(python3    -c "import json; print(json.load(open('$MANIFEST'))['files']['episodic_db'])"       2>/dev/null || echo "")

echo "--- Manifest contents ---"
echo "  app_version    : $MANIFEST_APP_VERSION"
echo "  schema_version : $MANIFEST_SCHEMA_VER"
echo "  git_sha        : $MANIFEST_GIT_SHA"
echo "  qdrant_points  : $MANIFEST_QDRANT_PTS"
echo "  episodic: sessions=$MANIFEST_EP_SESSIONS episodes=$MANIFEST_EP_EPISODES goals=$MANIFEST_EP_GOALS"
echo ""

# Verify all files exist
MISSING=0
for fname in "$QDRANT_SNAP_FILE" "$HISTORY_FILE" "$LEDGER_FILE" "$EPISODIC_FILE"; do
    if [ -n "$fname" ] && [ ! -f "$BACKUP_DIR/$fname" ]; then
        echo "WARN: backup file missing: $BACKUP_DIR/$fname"
        MISSING=$((MISSING+1))
    fi
done
# MEMORY and audit are optional (may not exist in older backups)
for fname in "$MEMORY_FILE" "$AUDIT_FILE"; do
    if [ -n "$fname" ] && [ ! -f "$BACKUP_DIR/$fname" ]; then
        echo "INFO: optional file absent: $BACKUP_DIR/$fname (non-fatal)"
    fi
done

if [ "$MISSING" -gt 0 ]; then
    echo "ERROR: $MISSING required backup file(s) missing — aborting." >&2
    exit 1
fi

echo "--- Intended actions ---"
echo "  1. Qdrant restore  : upload $QDRANT_SNAP_FILE to collection '$TARGET_COLLECTION'"
echo "  2. history.db      : $BACKUP_DIR/$HISTORY_FILE -> $HOME/.mem0/history-restore.db"
echo "  3. tier-ledger     : $BACKUP_DIR/$LEDGER_FILE -> $HOME/.mem0/tier-ledger-restore.jsonl"
if [ -n "$MEMORY_FILE" ] && [ -f "$BACKUP_DIR/$MEMORY_FILE" ]; then
echo "  4. MEMORY.md       : $BACKUP_DIR/$MEMORY_FILE -> $HOME/.mem0/MEMORY-restore.md"
fi
if [ -n "$AUDIT_FILE" ] && [ -f "$BACKUP_DIR/$AUDIT_FILE" ]; then
echo "  5. audit-baseline  : $BACKUP_DIR/$AUDIT_FILE -> $HOME/.mem0/audit-flags-restore.baseline"
fi
echo "  6. episodic.db     : $BACKUP_DIR/$EPISODIC_FILE -> $TARGET_EPISODIC"
echo "     (integrity_check + schema migration to current version)"
echo "  7. post-restore health: count comparison against manifest"
echo ""

if [ "$DRY_RUN" = true ]; then
    log_drill dry-run ok   # v0.19 M9: dry-run drill completed (manifest validated, files verified)
    echo "DRY RUN complete — no files written."
    echo "Drill logged to: $DRILL_LOG"
    exit 0
fi

echo "=== Starting live restore ==="

# ---------------------------------------------------------------------------
# 2. Qdrant restore
# ---------------------------------------------------------------------------

echo ""
echo "--- Step 1: Qdrant snapshot upload ---"

# Check if target collection already exists
existing=$(curl -fsS "$QDRANT_BASE/collections/$TARGET_COLLECTION" 2>/dev/null || true)
if echo "$existing" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('status')=='ok' else 1)" 2>/dev/null; then
    echo "ERROR: target collection '$TARGET_COLLECTION' already exists." >&2
    echo "Pick a different name with --target-collection or delete it first:" >&2
    echo "  curl -X DELETE $QDRANT_BASE/collections/$TARGET_COLLECTION" >&2
    exit 1
fi

SNAP_PATH="$BACKUP_DIR/$QDRANT_SNAP_FILE"

if [ -f "$SNAP_PATH" ] && [ -s "$SNAP_PATH" ]; then
    echo "Uploading snapshot $SNAP_PATH to collection $TARGET_COLLECTION ..."
    upload_resp=$(curl -sS \
        -X POST \
        "$QDRANT_BASE/collections/$TARGET_COLLECTION/snapshots/upload?priority=snapshot" \
        -H 'Content-Type: multipart/form-data' \
        -F "snapshot=@$SNAP_PATH" \
        2>&1)

    upload_status=$(echo "$upload_resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "error")
    if [ "$upload_status" = "ok" ]; then
        echo "Qdrant snapshot upload: OK"
    else
        echo "WARN: Qdrant snapshot upload response: $upload_resp" >&2
        echo "WARN: Continuing (restore may be partial)." >&2
    fi

    # Verify restored point count (allow +-10 tolerance for in-flight writes)
    sleep 1
    restored_pts=$(curl -fsS "$QDRANT_BASE/collections/$TARGET_COLLECTION" 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('points_count',0))" \
        2>/dev/null || echo 0)
    delta=$(( restored_pts - MANIFEST_QDRANT_PTS ))
    abs_delta=${delta#-}
    if [ "$abs_delta" -le 10 ]; then
        echo "Qdrant point count: restored=$restored_pts manifest=$MANIFEST_QDRANT_PTS delta=$delta (within tolerance)"
    else
        echo "WARN: Qdrant point count mismatch: restored=$restored_pts manifest=$MANIFEST_QDRANT_PTS delta=$delta (>10 tolerance)" >&2
    fi
else
    echo "WARN: Qdrant snapshot file not found or empty: $SNAP_PATH (skipping)" >&2
    restored_pts=0
fi

# ---------------------------------------------------------------------------
# 3. history.db restore
# ---------------------------------------------------------------------------

echo ""
echo "--- Step 2: history.db restore ---"
HIST_DST="$HOME/.mem0/history-restore.db"
cp "$BACKUP_DIR/$HISTORY_FILE" "$HIST_DST.tmp" && mv "$HIST_DST.tmp" "$HIST_DST"
echo "history.db restored to: $HIST_DST"

# ---------------------------------------------------------------------------
# 4. tier-ledger restore
# ---------------------------------------------------------------------------

echo ""
echo "--- Step 3: tier-ledger restore ---"
LEDGER_DST="$HOME/.mem0/tier-ledger-restore.jsonl"
cp "$BACKUP_DIR/$LEDGER_FILE" "$LEDGER_DST.tmp" && mv "$LEDGER_DST.tmp" "$LEDGER_DST"
LEDGER_LINES=$(wc -l < "$LEDGER_DST" 2>/dev/null || echo 0)
echo "tier-ledger restored to: $LEDGER_DST ($LEDGER_LINES lines)"

# ---------------------------------------------------------------------------
# 5. MEMORY.md restore (optional)
# ---------------------------------------------------------------------------

if [ -n "$MEMORY_FILE" ] && [ -f "$BACKUP_DIR/$MEMORY_FILE" ]; then
    echo ""
    echo "--- Step 4: MEMORY.md restore ---"
    MEMORY_DST="$HOME/.mem0/MEMORY-restore.md"
    cp "$BACKUP_DIR/$MEMORY_FILE" "$MEMORY_DST.tmp" && mv "$MEMORY_DST.tmp" "$MEMORY_DST"
    echo "MEMORY.md restored to: $MEMORY_DST"
fi

# ---------------------------------------------------------------------------
# 6. audit-baseline restore (optional)
# ---------------------------------------------------------------------------

if [ -n "$AUDIT_FILE" ] && [ -f "$BACKUP_DIR/$AUDIT_FILE" ]; then
    echo ""
    echo "--- Step 5: audit-baseline restore ---"
    AUDIT_DST="$HOME/.mem0/audit-flags-restore.baseline"
    cp "$BACKUP_DIR/$AUDIT_FILE" "$AUDIT_DST.tmp" && mv "$AUDIT_DST.tmp" "$AUDIT_DST"
    echo "audit-baseline restored to: $AUDIT_DST"
fi

# ---------------------------------------------------------------------------
# 7. episodic.db restore
# ---------------------------------------------------------------------------

echo ""
echo "--- Step 6: episodic.db restore ---"

# Atomic copy to .tmp first
cp "$BACKUP_DIR/$EPISODIC_FILE" "$TARGET_EPISODIC.tmp"

# Integrity check before rename (prefer sqlite3 CLI, fall back to python3)
integrity="ok"
if command -v sqlite3 >/dev/null 2>&1; then
    integrity=$(sqlite3 "$TARGET_EPISODIC.tmp" 'PRAGMA integrity_check' 2>&1)
else
    integrity=$(python3 -c "
import sqlite3, sys
conn = sqlite3.connect('$TARGET_EPISODIC.tmp')
rows = conn.execute('PRAGMA integrity_check').fetchall()
conn.close()
result = rows[0][0] if rows else 'ok'
print(result)
" 2>&1 || echo "check_failed")
fi
if [ "$integrity" != "ok" ]; then
    rm -f "$TARGET_EPISODIC.tmp"
    echo "ERROR: episodic.db integrity_check FAILED: $integrity" >&2
    echo "Aborting restore — backup copy may be corrupt." >&2
    exit 1
fi
echo "episodic.db integrity_check: ok"

mv "$TARGET_EPISODIC.tmp" "$TARGET_EPISODIC"
echo "episodic.db restored to: $TARGET_EPISODIC"

# Run schema migration (idempotent — brings 16.0 -> 17.0 if needed)
if [ -f "$PYTHON" ] && [ -f "$MEM0_APP/episodic.py" ]; then
    echo "Running schema migration on restored episodic.db ..."
    "$PYTHON" -c "
import sys
sys.path.insert(0, '$MEM0_APP')
import sqlite3
from episodic import init_schema

conn = sqlite3.connect('$TARGET_EPISODIC')
conn.row_factory = sqlite3.Row
init_schema(conn)
conn.close()
print('schema migration: ok')
" 2>&1 || echo "WARN: schema migration failed (non-fatal if schema already current)"
fi

# Verify restored episodic counts (prefer sqlite3 CLI, fall back to python3)
_query_episodic() {
    local db="$1" sql="$2" default="${3:-0}"
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$db" "$sql" 2>/dev/null || echo "$default"
    else
        python3 -c "
import sqlite3
conn = sqlite3.connect('$db')
try:
    row = conn.execute(\"$sql\").fetchone()
    print(row[0] if row is not None else '$default')
except Exception:
    print('$default')
conn.close()
" 2>/dev/null || echo "$default"
    fi
}

r_sessions=$(_query_episodic "$TARGET_EPISODIC" "SELECT COUNT(*) FROM sessions")
r_episodes=$(_query_episodic "$TARGET_EPISODIC" "SELECT COUNT(*) FROM episodes")
r_goals=$(_query_episodic    "$TARGET_EPISODIC" "SELECT COUNT(*) FROM goals")
r_schema=$(_query_episodic   "$TARGET_EPISODIC" "SELECT value FROM schema_meta WHERE key='schema_version'" "unknown")

echo "Restored episodic counts:"
echo "  schema_version : $r_schema"
echo "  sessions       : $r_sessions (manifest: $MANIFEST_EP_SESSIONS)"
echo "  episodes       : $r_episodes (manifest: $MANIFEST_EP_EPISODES)"
echo "  goals          : $r_goals (manifest: $MANIFEST_EP_GOALS)"

# ---------------------------------------------------------------------------
# 8. Post-restore health summary
# ---------------------------------------------------------------------------

echo ""
echo "=== Post-restore health summary ==="
echo ""
echo "Qdrant ($TARGET_COLLECTION):"
echo "  restored_points : $restored_pts"
echo "  manifest_points : $MANIFEST_QDRANT_PTS"
if [ "$restored_pts" -gt 0 ]; then
    echo "  status          : OK"
else
    echo "  status          : WARN (0 points — verify Qdrant upload)"
fi
echo ""
echo "episodic.db ($TARGET_EPISODIC):"
echo "  schema_version  : ${r_schema:-?}"
echo "  sessions        : ${r_sessions:-?} / manifest: $MANIFEST_EP_SESSIONS"
echo "  episodes        : ${r_episodes:-?} / manifest: $MANIFEST_EP_EPISODES"
echo "  goals           : ${r_goals:-?} / manifest: $MANIFEST_EP_GOALS"
echo ""
echo "Restored files:"
echo "  $HOME/.mem0/history-restore.db"
echo "  $HOME/.mem0/tier-ledger-restore.jsonl"
[ -f "$HOME/.mem0/MEMORY-restore.md" ]              && echo "  $HOME/.mem0/MEMORY-restore.md"
[ -f "$HOME/.mem0/audit-flags-restore.baseline" ]   && echo "  $HOME/.mem0/audit-flags-restore.baseline"
echo "  $TARGET_EPISODIC"
echo ""
echo "Next steps:"
echo "  - Validate manually before promoting to production paths."
echo ""
echo "  H10: To promote episodic.db to production use stack-promote.sh (NOT cp):"
echo "    bash scripts/wsl/stack-promote.sh --snapshot $(basename $TARGET_EPISODIC .db | sed 's/episodic-restore-//')   # from your stack repo"
echo ""
echo "  WARNING: Do NOT manually 'cp $TARGET_EPISODIC $HOME/.mem0/episodic.db' —"
echo "  use stack-promote.sh which stops services, backs up the live DB, runs integrity_check,"
echo "  promotes atomically, restarts services, and logs to the ledger."
echo ""
echo "  - To promote Qdrant: data is already in collection '$TARGET_COLLECTION'."
echo "    Swap alias or rename collection after validation."
echo "  - Cleanup drill artifacts:"
echo "    curl -X DELETE $QDRANT_BASE/collections/$TARGET_COLLECTION"
echo "    rm $TARGET_EPISODIC $HOME/.mem0/history-restore.db $HOME/.mem0/tier-ledger-restore.jsonl"
echo "    rm -f $HOME/.mem0/MEMORY-restore.md $HOME/.mem0/audit-flags-restore.baseline"
echo ""
log_drill live ok   # v0.19 M9: live restore completed
trap - EXIT         # v0.21 Phase A (L4): success logged — disarm the failure trap so a trailing stdout write error (or a signal during the two echos below) cannot append a bogus outcome=failed tail entry
echo "Drill logged to: $DRILL_LOG"
echo "=== Restore complete ==="
