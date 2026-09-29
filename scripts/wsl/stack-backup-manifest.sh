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
QDRANT_URL="${MEM0_QDRANT_URL:-http://127.0.0.1:6333}"

# stack.env is read BY KEY, never sourced: it is operator-edited, and an unquoted value with a
# space made bash execute the second word (set -e then killed this writer on the 09-21 and
# 09-22 nights, leaving those sets without a manifest).
stack_env_get() {
    [ -f "$HOME/.mem0/stack.env" ] || return 0
    { grep -m1 "^$1=" "$HOME/.mem0/stack.env" || true; } | cut -d= -f2- | tr -d '\r' | sed -e "s/^[\"']//" -e "s/[\"']\$//"
}

# ---------------------------------------------------------------------------
# 1. Qdrant points count from live state at backup time
# ---------------------------------------------------------------------------

QDRANT_POINTS=0
qdrant_raw=$(curl -fsS "$QDRANT_URL/collections/$QDRANT_COLLECTION" 2>/dev/null || true)
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
# Read the backup immutably: a plain read-only open of a WAL-mode database leaves -wal/-shm
# sidecars beside it, and those are what once made the prune delete real backups.
EPISODIC_URI="file:$EPISODIC_BACKUP?mode=ro&immutable=1"
if [ -f "$EPISODIC_BACKUP" ]; then
    # Prefer sqlite3 CLI; fall back to python3's built-in sqlite3 module
    if command -v sqlite3 >/dev/null 2>&1; then
        EPISODIC_SESSIONS=$(sqlite3 "$EPISODIC_URI" "SELECT COUNT(*) FROM sessions" 2>/dev/null || echo 0)
        EPISODIC_EPISODES=$(sqlite3 "$EPISODIC_URI" "SELECT COUNT(*) FROM episodes" 2>/dev/null || echo 0)
        EPISODIC_GOALS=$(sqlite3    "$EPISODIC_URI" "SELECT COUNT(*) FROM goals"    2>/dev/null || echo 0)
        EPISODIC_OQ=$(sqlite3       "$EPISODIC_URI" "SELECT COUNT(*) FROM open_questions" 2>/dev/null || echo 0)
        SCHEMA_VERSION=$(sqlite3    "$EPISODIC_URI" "SELECT value FROM schema_meta WHERE key='schema_version'" 2>/dev/null || echo "unknown")
    else
        # python3's sqlite3 module is always available in the venv environment
        read -r EPISODIC_SESSIONS EPISODIC_EPISODES EPISODIC_GOALS EPISODIC_OQ SCHEMA_VERSION <<< "$(python3 - "$EPISODIC_URI" <<'PYEOF'
import sys, sqlite3 as sq
db = sys.argv[1]
conn = sq.connect(db, uri=True)
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
# 3. Release stamp of the DEPLOYED tree: VERSION and DEPLOYED_SHA beside the server modules
# ---------------------------------------------------------------------------
# The brain has no .git: deploy.sh stamps VERSION and DEPLOYED_SHA into the app dir, so those
# two files ARE the deployed release. (A hard-coded "v0.17" and an "unknown" sha sat in every
# manifest for months.) A checkout with a .git is only the fallback for the sha.

APP_DIR="${MEM0_APP_DIR:-$HOME/apps/mem0-server}"
APP_VERSION="unknown"
if [ -s "$APP_DIR/VERSION" ]; then
    v=$(head -n1 "$APP_DIR/VERSION" | tr -d '[:space:]')
    case "$v" in
        ""|*[!0-9A-Za-z._+-]*) ;;
        *) APP_VERSION="v${v#v}" ;;
    esac
fi
GIT_SHA="unknown"
if [ -s "$APP_DIR/DEPLOYED_SHA" ]; then
    sha=$(head -n1 "$APP_DIR/DEPLOYED_SHA" | tr -d '[:space:]')
    case "$sha" in
        ""|*[!0-9a-f]*) ;;
        *) GIT_SHA="$sha" ;;
    esac
fi
if [ "$GIT_SHA" = "unknown" ]; then
    REPO="${MEM0_REPO_ROOT_WSL:-$(stack_env_get MEM0_REPO_ROOT_WSL)}"
    [ -n "$REPO" ] || REPO="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)"
    if [ -n "$REPO" ] && [ -d "$REPO/.git" ]; then
        GIT_SHA=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)
    fi
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

# 2026-08-24: the manifest must describe what the snapshot CONTAINS, not what was
# hoped — the audit_baseline entry named a file that never existed on disk, so a
# restore chased a phantom. Every entry is now conditional on the artifact being
# present in THIS snapshot; absent artifacts are an explicit JSON null.
mf() { if [ -f "$BACKUP_DIR/$1" ]; then echo "\"$1\""; else echo "null"; fi; }
# required artifacts: a 0-byte file is NOT a backup (review R3) - null it so the restore
# gate and the TMS FAIL row both see it the same night instead of at disaster time
mfreq() { if [ -s "$BACKUP_DIR/$1" ]; then echo "\"$1\""; else echo "null"; fi; }

cat > "$MANIFEST.tmp" <<EOF
{
  "ts": "$TS_ISO",
  "backup_ts_raw": "$TS",
  "app_version": "$APP_VERSION",
  "schema_version": "$SCHEMA_VERSION",
  "git_sha": "$GIT_SHA",
  "files": {
    "qdrant_snapshot": $(mfreq "qdrant-$TS.snapshot"),
    "history_db": $(mfreq "history-$TS.db"),
    "tier_ledger": $(mfreq "tier-ledger-$TS.jsonl"),
    "memory_md": $(mf "MEMORY-$TS.md"),
    "audit_baseline": $(mf "audit-flags-$TS.baseline"),
    "episodic_db": $(mfreq "episodic-$TS.db"),
    "claude_settings": $(mf "claude-settings-$TS.json"),
    "l10_flags": $(mf "l10-flags-$TS.jsonl"),
    "l10_state": $(mf "l10-state-$TS.json"),
    "promote_review": $(mf "promote-review-$TS.jsonl"),
    "stale_worksheet": $(mf "stale-worksheet-$TS.jsonl"),
    "qdrant_episodes": $(mf "qcol-episodes-$TS.snapshot"),
    "qdrant_entities": $(mf "qcol-entities-$TS.snapshot"),
    "qdrant_wiki": $(mf "qcol-wiki-$TS.snapshot")
  },
  "deliberately_excluded": "pair-verdict-cache.db (TTL'd rebuildable cache), jobs.db (transient queue), canonical-replay.jsonl (anti-replay nonce ledger; signed tokens carry a 300s skew gate and the ledger GCs at 600s, so a lost ledger reopens at most a 10-minute window), telemetry ledgers (retrieval-log, admission-rejected, receipts). The three secondary Qdrant collections (episodes, entities, wiki) are snapshotted into the set when present; if one is missing, rebuild episodes with episode-embed-backfill.py (from episodic.db) and wiki with wiki-index-build.py - entities is written by the mem0 library and has no rebuild path, its snapshot is the only copy. See docs/data-backup.md",
  "counts": {
    "qdrant_points": $QDRANT_POINTS,
    "episodic_sessions": $EPISODIC_SESSIONS,
    "episodic_episodes": $EPISODIC_EPISODES,
    "episodic_goals": $EPISODIC_GOALS,
    "episodic_open_questions": $EPISODIC_OQ
  }
}
EOF
# Per-file size + sha256 (bit-rot is otherwise detectable only by sqlite/tar checks). Kept in a
# separate `checksums` map keyed by file name so `files` stays name-only for stack-restore.
python3 - "$BACKUP_DIR" "$MANIFEST.tmp" <<'PYSUMS'
import hashlib, json, os, sys
backup_dir, path = sys.argv[1], sys.argv[2]
with open(path) as fh:
    m = json.load(fh)
sums = {}
for name in (v for v in m["files"].values() if isinstance(v, str)):
    full = os.path.join(backup_dir, name)
    h = hashlib.sha256()
    with open(full, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    sums[name] = {"size": os.path.getsize(full), "sha256": h.hexdigest()}
m["checksums"] = sums
with open(path, "w") as fh:
    json.dump(m, fh, indent=2)
    fh.write("\n")
PYSUMS
# Refuse to publish a manifest that does not parse (a malformed manifest is worse
# than none — stack-restore trusts it).
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$MANIFEST.tmp" || {
    echo "ERROR: generated manifest does not parse — not publishing" >&2
    rm -f "$MANIFEST.tmp"
    exit 1
}

mv "$MANIFEST.tmp" "$MANIFEST"
echo "manifest written: $MANIFEST"
