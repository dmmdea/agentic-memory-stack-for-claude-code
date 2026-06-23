#!/usr/bin/env bash
# stack-backup-manifest.sh — v0.17 Phase B
# Writes manifest-$TS.json into the backup dir documenting what each snapshot contains.
# Invoked by stack-backup.sh after all files are written.
#
# Usage: bash stack-backup-manifest.sh <TS>
# TS format: YYYYmmdd-HHMMSS (e.g. 20260611-053000)

set -euo pipefail

TS="${1:?ts arg required}"
BACKUP_DIR="$HOME/.mem0/backups"
MANIFEST="$BACKUP_DIR/manifest-$TS.json"
# DR fix (2026-06-20): count the LIVE collection, not the frozen pre-egemma "memories".
QDRANT_COLLECTION="${MEM0_QDRANT_COLLECTION:-mem0_egemma_768}"

# ---------------------------------------------------------------------------
# 1. Qdrant points count from live state at backup time
# ---------------------------------------------------------------------------

QDRANT_POINTS=0
qdrant_raw=$(curl -fsS "http://127.0.0.1:6333/collections/$QDRANT_COLLECTION" 2>/dev/null || true)
if [ -n "$qdrant_raw" ]; then
    QDRANT_POINTS=$(echo "$qdrant_raw" \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('points_count',0))" \
        2>/dev/null || echo 0)
fi

# ---------------------------------------------------------------------------
# 2. Episodic counts from the backup copy (not live DB — consistent with snapshot)
# ---------------------------------------------------------------------------

EPISODIC_SESSIONS=0
EPISODIC_EPISODES=0
EPISODIC_GOALS=0
EPISODIC_OQ=0
SCHEMA_VERSION="unknown"

EPISODIC_BACKUP="$BACKUP_DIR/episodic-$TS.db"
if [ -f "$EPISODIC_BACKUP" ]; then
    # Prefer sqlite3 CLI; fall back to python3's built-in sqlite3 module
    if command -v sqlite3 >/dev/null 2>&1; then
        EPISODIC_SESSIONS=$(sqlite3 "$EPISODIC_BACKUP" "SELECT COUNT(*) FROM sessions" 2>/dev/null || echo 0)
        EPISODIC_EPISODES=$(sqlite3 "$EPISODIC_BACKUP" "SELECT COUNT(*) FROM episodes" 2>/dev/null || echo 0)
        EPISODIC_GOALS=$(sqlite3    "$EPISODIC_BACKUP" "SELECT COUNT(*) FROM goals"    2>/dev/null || echo 0)
        EPISODIC_OQ=$(sqlite3       "$EPISODIC_BACKUP" "SELECT COUNT(*) FROM open_questions" 2>/dev/null || echo 0)
        SCHEMA_VERSION=$(sqlite3    "$EPISODIC_BACKUP" "SELECT value FROM schema_meta WHERE key='schema_version'" 2>/dev/null || echo "unknown")
    else
        # python3's sqlite3 module is always available in the venv environment
        read -r EPISODIC_SESSIONS EPISODIC_EPISODES EPISODIC_GOALS EPISODIC_OQ SCHEMA_VERSION <<< "$(python3 - "$EPISODIC_BACKUP" <<'PYEOF'
import sys, sqlite3 as sq
db = sys.argv[1]
conn = sq.connect(db)
def qone(sql, default=0):
    try: return conn.execute(sql).fetchone()[0]
    except: return default
s = qone("SELECT COUNT(*) FROM sessions")
e = qone("SELECT COUNT(*) FROM episodes")
g = qone("SELECT COUNT(*) FROM goals")
oq = qone("SELECT COUNT(*) FROM open_questions")
ver_row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
ver = ver_row[0] if ver_row else "unknown"
print(s, e, g, oq, ver)
PYEOF
)" 2>/dev/null || true
        # Defaults if python3 also failed
        EPISODIC_SESSIONS="${EPISODIC_SESSIONS:-0}"
        EPISODIC_EPISODES="${EPISODIC_EPISODES:-0}"
        EPISODIC_GOALS="${EPISODIC_GOALS:-0}"
        EPISODIC_OQ="${EPISODIC_OQ:-0}"
        SCHEMA_VERSION="${SCHEMA_VERSION:-unknown}"
    fi
fi

# ---------------------------------------------------------------------------
# 3. Git SHA from the agentic-memory-stack repo
# ---------------------------------------------------------------------------

GIT_SHA="unknown"
# v1.0 Phase 7A: resolve the repo from the operator receipt (~/.mem0/stack.env),
# falling back to this script's own location — never hardcode a developer repo path.
[ -f "$HOME/.mem0/stack.env" ] && . "$HOME/.mem0/stack.env"
REPO="${MEM0_REPO_ROOT_WSL:-$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)}"
if [ -n "$REPO" ] && [ -d "$REPO/.git" ]; then
    GIT_SHA=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)
fi

# ---------------------------------------------------------------------------
# 4. Parse TS into ISO 8601 timestamp (YYYYmmdd-HHMMSS -> YYYY-MM-DDTHH:MM:SSZ)
# ---------------------------------------------------------------------------

# TS example: 20260611-053012
TS_DATE="${TS:0:8}"   # 20260611
TS_TIME="${TS:9:6}"   # 053012
TS_ISO="${TS_DATE:0:4}-${TS_DATE:4:2}-${TS_DATE:6:2}T${TS_TIME:0:2}:${TS_TIME:2:2}:${TS_TIME:4:2}Z"

# ---------------------------------------------------------------------------
# 5. Write manifest (atomic tmp-then-rename)
# ---------------------------------------------------------------------------

cat > "$MANIFEST.tmp" <<EOF
{
  "ts": "$TS_ISO",
  "backup_ts_raw": "$TS",
  "app_version": "v0.17",
  "schema_version": "$SCHEMA_VERSION",
  "git_sha": "$GIT_SHA",
  "files": {
    "qdrant_snapshot": "qdrant-$TS.snapshot",
    "history_db": "history-$TS.db",
    "tier_ledger": "tier-ledger-$TS.jsonl",
    "memory_md": "MEMORY-$TS.md",
    "audit_baseline": "audit-flags-$TS.baseline",
    "episodic_db": "episodic-$TS.db"
  },
  "counts": {
    "qdrant_points": $QDRANT_POINTS,
    "episodic_sessions": $EPISODIC_SESSIONS,
    "episodic_episodes": $EPISODIC_EPISODES,
    "episodic_goals": $EPISODIC_GOALS,
    "episodic_open_questions": $EPISODIC_OQ
  }
}
EOF

mv "$MANIFEST.tmp" "$MANIFEST"
echo "manifest written: $MANIFEST"
