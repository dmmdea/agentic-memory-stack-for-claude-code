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
QDRANT_URL="${MEM0_QDRANT_URL:-http://127.0.0.1:6333}"

# stack.env is read BY KEY, never sourced: it is operator-edited, and an unquoted value with a
# space made bash execute the second word (set -e then killed this writer on the 09-21 and
# 09-22 nights, leaving those sets without a manifest).
stack_env_get() {
    [ -f "$HOME/.mem0/stack.env" ] || return 0
    { grep -m1 "^$1=" "$HOME/.mem0/stack.env" || true; } | cut -d= -f2- | tr -d '\r' | sed -e "s/^[\"']//" -e "s/[\"']\$//"
}

# ---------------------------------------------------------------------------
# 0. The embedding space the set was made in (mem0-server/embedder_profile.py, through embed-profile.sh)
# ---------------------------------------------------------------------------
# A vector snapshot is only meaningful with the model and prompt template that made it, so the manifest
# says which: embed_profile, embed_model, template_version and the collection each Qdrant file holds.
# stack-backup.sh resolves the same profile in the same run, so the names agree with the files it wrote.
# DR fix (2026-06-20): count the LIVE collection, not a frozen older one - the name is the active
# space's memories collection, never a literal.
EP_LIB="$(dirname "$0")/embed-profile.sh"
EP_READY=0
if [ -f "$EP_LIB" ]; then
    # shellcheck disable=SC1090
    . "$EP_LIB"
    if ep_load; then EP_READY=1; else echo "WARN: the embedding profile did not resolve (a configuration error: see above); the manifest will say embed_profile unknown" >&2; fi
else
    echo "WARN: $EP_LIB is missing; the manifest will say embed_profile unknown (redeploy scripts/wsl/)" >&2
fi
if [ "$EP_READY" != 1 ]; then
    EP_PROFILE=unknown; EP_MODEL=""; EP_TEMPLATE=""
    EP_MEM="${MEM0_QDRANT_COLLECTION:-}"; EP_ENT=""; EP_EPI=""; EP_WIKI=""
fi
QDRANT_COLLECTION="$EP_MEM"

# ---------------------------------------------------------------------------
# 1. Qdrant points count from live state at backup time
# ---------------------------------------------------------------------------

QDRANT_POINTS=0
qdrant_raw=""
[ -z "$QDRANT_COLLECTION" ] || qdrant_raw=$(curl -fsS "$QDRANT_URL/collections/$QDRANT_COLLECTION" 2>/dev/null || true)
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
# The deployed tree has no .git, so the release is what was stamped beside the server modules:
# VERSION, and DEPLOYED_SHA (one line, a 40-hex sha or the word `unknown`) written by the installers
# through install/deploy-stamp.sh and by deploy.sh. (A hard-coded "v0.17" and an "unknown" sha sat
# in every manifest for months.) The stamp is AUTHORITATIVE: `unknown`, an empty line or anything
# that is not a 40-hex sha reads unknown, because a checkout that happens to be reachable names the
# commit it is on, not the one that was deployed. Only a tree with no stamp at all (this script
# running straight from a checkout no installer deployed) asks that checkout.

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
if [ -e "$APP_DIR/DEPLOYED_SHA" ]; then
    sha=$(head -n1 "$APP_DIR/DEPLOYED_SHA" | tr -d '[:space:]')
    if [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then GIT_SHA="$sha"; fi
else
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
# Further collections of a secondary kind (qcol-<kind>+<collection>-<TS>.snapshot, see stack-backup.sh):
# every one that is in THIS set is listed, so it is checksummed and a restore can find it. Names hold
# only [A-Za-z0-9._+-] (stack-backup.sh validates the collection name), so they are JSON-safe.
EXTRA_COLLECTIONS_JSON="["; _sep=""
for _f in "$BACKUP_DIR"/qcol-*+*-"$TS".snapshot; do
    [ -f "$_f" ] || continue
    EXTRA_COLLECTIONS_JSON="$EXTRA_COLLECTIONS_JSON$_sep\"${_f##*/}\""; _sep=", "
done
EXTRA_COLLECTIONS_JSON="$EXTRA_COLLECTIONS_JSON]"
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
    "qdrant_wiki": $(mf "qcol-wiki-$TS.snapshot"),
    "media": $(mf "media-$TS.tar")
  },
  "qdrant_extra_collections": $EXTRA_COLLECTIONS_JSON,
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
python3 - "$BACKUP_DIR" "$MANIFEST.tmp" "$TS" "$EP_PROFILE" "$EP_MODEL" "$EP_TEMPLATE" "$EP_MEM" "$EP_ENT" "$EP_EPI" "$EP_WIKI" <<'PYSUMS'
import hashlib, json, os, sys
backup_dir, path, ts, ep_profile, ep_model, ep_template, c_mem, c_ent, c_epi, c_wiki = sys.argv[1:11]
with open(path) as fh:
    m = json.load(fh)
# The embedding space, right after the identity fields. "collections" are the ACTIVE space's names (the
# ones the fixed file keys hold); "qdrant_collections" maps EVERY Qdrant snapshot in this set to the
# collection it holds, extras included (qcol-<kind>+<collection>-<TS>.snapshot carries its own name).
# A restore reads the profile and the collection from here instead of assuming the default space.
active = {"memories": c_mem, "entities": c_ent, "episodes": c_epi, "wiki": c_wiki}
files = m["files"]
held = {}
for key, kind in (("qdrant_snapshot", "memories"), ("qdrant_episodes", "episodes"),
                  ("qdrant_entities", "entities"), ("qdrant_wiki", "wiki")):
    if isinstance(files.get(key), str) and active.get(kind):
        held[files[key]] = active[kind]
for name in m.get("qdrant_extra_collections", []):
    if isinstance(name, str) and "+" in name and name.endswith("-" + ts + ".snapshot"):
        held[name] = name[name.index("+") + 1:-len("-" + ts + ".snapshot")]
head_keys = ("ts", "backup_ts_raw", "app_version", "schema_version", "git_sha")
ordered = {k: m.pop(k) for k in head_keys if k in m}
ordered["embed_profile"] = ep_profile
ordered["embed_model"] = ep_model
ordered["template_version"] = ep_template
ordered["collections"] = {k: v for k, v in active.items() if v}
ordered["qdrant_collections"] = held
ordered.update(m)
m = ordered
sums = {}
names = [v for v in m["files"].values() if isinstance(v, str)]
names += [n for n in m.get("qdrant_extra_collections", []) if isinstance(n, str)]
for name in names:
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
