#!/usr/bin/env bash
# scripts/travel/restore-replica.sh — native-Linux port of restore-replica.ps1.
#
# Pulls the newest COMPLETE snapshot set from the Brain over SSH and restores it into this box's
# dormant local mem0 + Qdrant as a READ-ONLY replica. Qdrant is restored through the snapshot
# UPLOAD API (version-safe), never by copying its storage directory. Idempotent: re-running
# refreshes the replica to a newer set; an already-cached set is not fetched twice.
#
# Config: ~/.mem0/replica.env (written by install/linux-replica.sh)
#   BRAIN_SSH=<ssh host alias>            the Brain, as ~/.ssh/config knows it
#   BRAIN_BACKUP_DIR=~/.mem0/backups      the Brain's snapshot directory (remote path)
#   BRAIN_WSL=<distro>:<user>             optional: the Brain keeps its stack inside WSL on a
#                                         Windows host; remote commands run through wsl.exe
#   REPLICA_CACHE=~/.mem0/replica-snapshots   local cache of fetched sets (newest kept)
#
# Usage: restore-replica.sh [--leave-running] [--dry-run] [--collection <name>]
#   --leave-running   keep qdrant + mem0 up after the restore (the watcher's go_offline path);
#                     default stops them again so the replica stays dormant while online.
#   --dry-run         resolve config, list the newest remote set, touch nothing.
#
# One-Brain Rule guard: refuses unless ~/.mem0/role is `replica` AND the authority is remote —
# on the Brain this would overwrite the live store with a day-old snapshot.
set -euo pipefail
LEAVE_RUNNING=0; DRY_RUN=0; COLLECTION="mem0_egemma_768"
while [ $# -gt 0 ]; do
    case "$1" in
        --leave-running) LEAVE_RUNNING=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --collection) COLLECTION="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
MEM0_DIR="$HOME/.mem0"
LOG="$MEM0_DIR/replica-restore.log"
STAMP_FILE="$MEM0_DIR/replica-restored"
fail() { echo "FAIL: $*" >&2; log_line failed "$*"; exit 1; }
say()  { echo "==> $*"; }
log_line() { # outcome, note
    printf '{"ts":"%s","event":"replica-restore","outcome":"%s","snapshot":"%s","note":%s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "${TS:-}" "$(printf '%s' "$2" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" >> "$LOG" 2>/dev/null || true
}
is_local_url() {
    local url="$1" host
    host="$(printf '%s' "$url" | sed -nE 's#^[a-zA-Z][a-zA-Z0-9+.-]*://\[?([^]/:]+)\]?(:[0-9]+)?(/.*)?$#\1#p')"
    [ -z "$host" ] && return 0
    case "$host" in 127.*|localhost|*.localhost|0.0.0.0|::1|::) return 0 ;; esac
    return 1
}

# ---------------------------------------------------------------- guards
ROLE="$(tr -d '[:space:]' < "$MEM0_DIR/role" 2>/dev/null || true)"
[ "$ROLE" = "replica" ] || fail "this box's role is '${ROLE:-unset}', not replica — restoring a snapshot here would overwrite a live store (One-Brain Rule). Refusing."
AUTH="$(grep -v '^\s*#' "$MEM0_DIR/authority-url" 2>/dev/null | sed -n '1p' | tr -d '[:space:]' || true)"
[ -n "$AUTH" ] && ! is_local_url "$AUTH" || fail "authority-url '${AUTH:-unset}' is missing or loopback — a replica's authority must be a REMOTE Brain. Refusing."
[ -f "$MEM0_DIR/replica.env" ] || fail "missing $MEM0_DIR/replica.env (run install/linux-replica.sh)"
# shellcheck disable=SC1091
. "$MEM0_DIR/replica.env"
: "${BRAIN_SSH:?BRAIN_SSH unset in replica.env}"
BRAIN_BACKUP_DIR="${BRAIN_BACKUP_DIR:-~/.mem0/backups}"
REPLICA_CACHE="${REPLICA_CACHE:-$MEM0_DIR/replica-snapshots}"
for t in curl jq ssh python3; do command -v "$t" >/dev/null || fail "$t is required"; done
# Dormancy on EVERY exit path: a failed upload or health wait must not leave qdrant/mem0 running
# while the Brain is reachable. --leave-running only survives a successful restore (the success
# branch flips RESTORED=1); anything else stops both units.
RESTORED=0
trap '[ "$LEAVE_RUNNING" = 1 ] && [ "$RESTORED" = 1 ] || systemctl --user stop mem0.service qdrant.service 2>/dev/null || true' EXIT

# remote command runner: plain Linux brain, or a WSL brain behind a Windows sshd
remote() { # $1 = a simple bash command line (no single quotes)
    if [ -n "${BRAIN_WSL:-}" ]; then
        local distro="${BRAIN_WSL%%:*}" user="${BRAIN_WSL#*:}"
        ssh -o BatchMode=yes -o ConnectTimeout=15 "$BRAIN_SSH" "wsl.exe -d $distro -u $user -e bash -lc \"$1\"" 2>/dev/null
    else
        ssh -o BatchMode=yes -o ConnectTimeout=15 "$BRAIN_SSH" "bash -lc '$1'" 2>/dev/null
    fi
}

# ---------------------------------------------------------------- 1. newest complete set on the brain
say "[1] newest snapshot set on $BRAIN_SSH:$BRAIN_BACKUP_DIR"
NEWEST="$(remote "ls -t $BRAIN_BACKUP_DIR/manifest-*.json 2>/dev/null | head -1" | tr -d '\r' | tail -1 || true)"
[ -n "$NEWEST" ] || fail "no manifest found on the Brain (is $BRAIN_SSH reachable and $BRAIN_BACKUP_DIR right?)"
TS="$(basename "$NEWEST" .json)"; TS="${TS#manifest-}"
MANIFEST_JSON="$(remote "cat $BRAIN_BACKUP_DIR/manifest-$TS.json" | tr -d '\r')"
printf '%s' "$MANIFEST_JSON" | python3 -c 'import json,sys; json.load(sys.stdin)' || fail "manifest $TS unreadable"
read -r QDRANT_FILE EPI_FILE HIST_FILE PTS < <(printf '%s' "$MANIFEST_JSON" | python3 -c '
import json,sys; d=json.load(sys.stdin); f=d["files"]
print(f.get("qdrant_snapshot") or "", f.get("episodic_db") or "", f.get("history_db") or "", d.get("counts",{}).get("qdrant_points",0))')
[ -n "$QDRANT_FILE" ] && [ -n "$EPI_FILE" ] && [ -n "$HIST_FILE" ] || fail "manifest $TS is not a complete set (qdrant/episodic/history all required)"
[ "${PTS:-0}" -gt 0 ] || fail "manifest $TS reports 0 Qdrant points — refusing to restore an empty brain"
echo "    set $TS: $PTS points ($QDRANT_FILE, $EPI_FILE, $HIST_FILE)"
if [ "$DRY_RUN" = 1 ]; then echo "    [dry-run] would fetch into $REPLICA_CACHE/$TS, restore into collection '$COLLECTION', then $([ $LEAVE_RUNNING = 1 ] && echo 'leave services running' || echo 'stop services'); nothing touched"; exit 0; fi

# ---------------------------------------------------------------- 2. fetch (size-verified, cached)
say "[2] fetch into $REPLICA_CACHE/$TS"
DEST="$REPLICA_CACHE/$TS"; mkdir -p "$DEST"; chmod 700 "$REPLICA_CACHE"
printf '%s\n' "$MANIFEST_JSON" > "$DEST/manifest-$TS.json"
for f in "$QDRANT_FILE" "$EPI_FILE" "$HIST_FILE"; do
    want="$(remote "stat -c %s $BRAIN_BACKUP_DIR/$f" | tr -d '\r' | tail -1)"
    [ "${want:-0}" -gt 0 ] || fail "cannot size $f on the Brain"
    if [ -f "$DEST/$f" ] && [ "$(stat -c %s "$DEST/$f")" = "$want" ]; then echo "    cached: $f ($want B)"; continue; fi
    remote "cat $BRAIN_BACKUP_DIR/$f" > "$DEST/$f.part"
    got="$(stat -c %s "$DEST/$f.part")"
    [ "$got" = "$want" ] || { rm -f "$DEST/$f.part"; fail "$f: fetched $got B, expected $want B"; }
    mv "$DEST/$f.part" "$DEST/$f"; echo "    fetched: $f ($got B)"
done
# keep only the newest cached set
find "$REPLICA_CACHE" -mindepth 1 -maxdepth 1 -type d ! -name "$TS" -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------- 3. restore into the dormant local store
say "[3] restore: stop mem0, start qdrant, upload snapshot, copy ledgers"
curl -sf -m 5 http://127.0.0.1:11436/v1/models >/dev/null || fail "the local embedder (llama-swap :11436) is not serving — the replica could answer nothing"
systemctl --user stop mem0.service 2>/dev/null || true
systemctl --user start qdrant.service
for i in $(seq 1 60); do curl -sf -m 3 http://127.0.0.1:6333/healthz >/dev/null && break; sleep 2; [ "$i" = 60 ] && fail "local qdrant did not come up within 2 minutes (systemctl --user status qdrant.service)"; done
# the replica store is disposable: drop the old collection so the upload is the whole truth
curl -s -m 60 -X DELETE "http://127.0.0.1:6333/collections/$COLLECTION" >/dev/null || true
out="$(curl -s -m 900 -X POST "http://127.0.0.1:6333/collections/$COLLECTION/snapshots/upload?priority=snapshot" -H 'Content-Type: multipart/form-data' -F "snapshot=@$DEST/$QDRANT_FILE")"
printf '%s' "$out" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' || fail "Qdrant snapshot upload failed: $out"
restored="$(curl -sf -m 10 "http://127.0.0.1:6333/collections/$COLLECTION" | jq -r '.result.points_count')"
[ "${restored:-0}" -gt 0 ] || fail "collection '$COLLECTION' is empty after the upload"
echo "    qdrant: $restored points restored (manifest said $PTS)"
rm -f "$MEM0_DIR"/episodic.db-shm "$MEM0_DIR"/episodic.db-wal "$MEM0_DIR"/history.db-shm "$MEM0_DIR"/history.db-wal
cp "$DEST/$EPI_FILE" "$MEM0_DIR/episodic.db.tmp" && mv "$MEM0_DIR/episodic.db.tmp" "$MEM0_DIR/episodic.db"
cp "$DEST/$HIST_FILE" "$MEM0_DIR/history.db.tmp" && mv "$MEM0_DIR/history.db.tmp" "$MEM0_DIR/history.db"
echo "    ledgers: episodic.db + history.db replaced"

# ---------------------------------------------------------------- 4. the replica must actually answer
say "[4] start mem0 and prove it answers (/health/deep embeds through the local embedder)"
systemctl --user start mem0.service
health=""
for i in $(seq 1 45); do health="$(curl -sf -m 10 http://127.0.0.1:18791/health 2>/dev/null || true)"; printf '%s' "$health" | grep -q '"ok"[[:space:]]*:[[:space:]]*true' && break; sleep 2; [ "$i" = 45 ] && fail "replica mem0 did not come up healthy: ${health:-<no answer>} (systemctl --user status mem0.service)"; done
deep="$(curl -sf -m 120 http://127.0.0.1:18791/health/deep 2>/dev/null || true)"
printf '%s' "$deep" | grep -q '"ok"[[:space:]]*:[[:space:]]*true' || fail "replica /health/deep not ok: ${deep:0:300}"
echo "    replica live: $restored memories, /health/deep ok"
printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$TS" "$restored" > "$STAMP_FILE"
log_line ok "restored $restored points from $TS"
RESTORED=1
if [ "$LEAVE_RUNNING" = 1 ]; then
    echo "    services left running (offline mode)"
else
    systemctl --user stop mem0.service qdrant.service 2>/dev/null || true
    echo "    services stopped again (dormant while online)"
fi
