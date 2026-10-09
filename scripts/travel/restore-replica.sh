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
#   --collection      restore into this collection instead of the set's own (the manifest's
#                     collections.memories; a set from before embedding profiles is the default
#                     space's). The set's profile is checked either way.
#
# One-Brain Rule guard: refuses unless ~/.mem0/role is `replica` AND the authority is remote —
# on the Brain this would overwrite the live store with a day-old snapshot.
#
# Embedding-space guard: a snapshot's vectors only mean something to the model and prompt template
# that made them. The set's manifest names its profile (embed_profile); the restore refuses unless
# this replica is configured for the same profile (~/.mem0/stack.env, install/linux-replica.sh
# --embed-profile) AND the local llama-swap serves that profile's alias — otherwise the replica would
# restore healthy-looking vectors it embeds every query against in another space.
set -euo pipefail
LEAVE_RUNNING=0; DRY_RUN=0; COLLECTION=""
while [ $# -gt 0 ]; do
    case "$1" in
        --leave-running) LEAVE_RUNNING=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --collection) COLLECTION="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

# embedder_profile.py (mem0-server) is the one definition of the embedding space: found repo-relative
# first (the tests, a checkout), then where the replica installer deploys the server modules. This script
# is deployed alone, so it carries its own small resolver instead of sourcing a library.
ep_py() {  # <python> [args...]: runs with `ep` imported; rc 2 when the module cannot be found
    local d
    for d in "$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)/mem0-server" "$HOME/apps/mem0-server"; do
        [ -f "$d/embedder_profile.py" ] || continue
        python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import embedder_profile as ep; del sys.argv[1]
'"$1" "$d" "${@:2}"
        return $?
    done
    return 2
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
# Remote path: the tilde must reach the Brain unexpanded (a bare ~ in the default or in an
# unquoted env value expands to THIS box's home and the restore looks in the wrong place).
BRAIN_BACKUP_DIR="${BRAIN_BACKUP_DIR:-"~/.mem0/backups"}"
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
# media memories' files (1.35.0): optional, absent from older sets and from sets made while there was no media
MEDIA_FILE="$(printf '%s' "$MANIFEST_JSON" | python3 -c 'import json,sys; v=(json.load(sys.stdin).get("files") or {}).get("media"); print(v if isinstance(v,str) else "")')"

# ---------------------------------------------------------------- 1b. the set's embedding space
# "-" stands for an empty field (read would collapse a leading empty one): a set from before profiles
# names none, which means the default space.
read -r MP_PROFILE MP_COLLECTION < <(printf '%s' "$MANIFEST_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d.get("embed_profile") or "-", (d.get("collections") or {}).get("memories") or "-")')
[ "$MP_PROFILE" != "-" ] || MP_PROFILE=""
[ "$MP_COLLECTION" != "-" ] || MP_COLLECTION=""
if [ -z "$MP_PROFILE" ]; then
    MP_PROFILE="$(ep_py 'print(ep.LEGACY_PROFILE)')" || fail "embedder_profile.py not found beside the repo or in ~/apps/mem0-server: the replica cannot tell which embedding space set $TS is in"
    echo "    set $TS records no embedding profile (made before profiles): the legacy space, $MP_PROFILE"
fi
[ "$MP_PROFILE" != "unknown" ] || fail "set $TS was written without a resolvable embedding profile (embed_profile: unknown); fix the Brain's backup first (is embedder_profile.py deployed beside its scripts?)"
LOCAL_PROFILE="$(ep_py 'print(ep.active().name)')" || fail "this replica's embedding profile does not resolve (embedder_profile.py missing, or MEM0_EMBED_PROFILE names an unknown profile)"
if [ "$MP_PROFILE" != "$LOCAL_PROFILE" ]; then
    fail "set $TS is in embedding profile '$MP_PROFILE' but this replica is configured for '$LOCAL_PROFILE' (~/.mem0/stack.env MEM0_EMBED_PROFILE). Restoring would load vectors this replica embeds every query against in another space. Serve '$MP_PROFILE' on llama-swap :11436, then re-run: bash install/linux-replica.sh --embed-profile $MP_PROFILE"
fi
LOCAL_ALIAS="$(ep_py 'print(ep.embed_model(ep.get(sys.argv[1])))' "$MP_PROFILE")" || fail "cannot resolve the llama-swap alias of profile '$MP_PROFILE'"
EMBED_BASE="$(ep_py 'print(ep.base_url())')" || fail "cannot resolve the embedder base URL"
BOUND_COLLECTION="$(ep_py 'print(ep.collection("memories", ep.get(sys.argv[1])))' "$MP_PROFILE")" || fail "cannot resolve the memories collection of profile '$MP_PROFILE'"
# the local embedder must serve this profile's alias, not merely answer: any other model is another space
curl -sf -m 5 "$EMBED_BASE/models" | grep -q "\"$LOCAL_ALIAS\"" || fail "the local embedder ($EMBED_BASE) does not serve '$LOCAL_ALIAS', the alias of embedding profile '$MP_PROFILE' — the replica could answer nothing (EmbeddingGemma-2 needs llama.cpp b11452 or later; llama-swap entries: install/1-wsl-services.sh prints them)"
# the collection: --collection, else the set's own. The replica's server binds BOUND_COLLECTION, so a set
# that holds another one would restore fine and be invisible to the server.
if [ -z "$COLLECTION" ]; then
    COLLECTION="${MP_COLLECTION:-$BOUND_COLLECTION}"
    if [ "$COLLECTION" != "$BOUND_COLLECTION" ]; then
        fail "set $TS holds collection '$COLLECTION' but this replica's server binds '$BOUND_COLLECTION' for profile '$MP_PROFILE' (MEM0_QDRANT_COLLECTION in ~/.mem0/stack.env). Align the two, or pass --collection to restore under a name you will bind yourself"
    fi
fi
echo "    embedding profile $MP_PROFILE: alias '$LOCAL_ALIAS' served locally; restoring into collection '$COLLECTION'"
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
if [ -n "$MEDIA_FILE" ]; then
    want="$(remote "stat -c %s $BRAIN_BACKUP_DIR/$MEDIA_FILE" | tr -d '\r' | tail -1)"
    if [ "${want:-0}" -gt 0 ]; then
        if [ -f "$DEST/$MEDIA_FILE" ] && [ "$(stat -c %s "$DEST/$MEDIA_FILE")" = "$want" ]; then echo "    cached: $MEDIA_FILE ($want B)"
        else
            remote "cat $BRAIN_BACKUP_DIR/$MEDIA_FILE" > "$DEST/$MEDIA_FILE.part"
            got="$(stat -c %s "$DEST/$MEDIA_FILE.part")"
            [ "$got" = "$want" ] || { rm -f "$DEST/$MEDIA_FILE.part"; fail "$MEDIA_FILE: fetched $got B, expected $want B"; }
            mv "$DEST/$MEDIA_FILE.part" "$DEST/$MEDIA_FILE"; echo "    fetched: $MEDIA_FILE ($got B)"
        fi
    else
        echo "    WARN: the set lists $MEDIA_FILE but the Brain cannot size it; media memories will answer without their files"
        MEDIA_FILE=""
    fi
fi
# keep only the newest cached set
find "$REPLICA_CACHE" -mindepth 1 -maxdepth 1 -type d ! -name "$TS" -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------- 3. restore into the dormant local store
say "[3] restore: stop mem0, start qdrant, upload snapshot, copy ledgers"
curl -sf -m 5 "$EMBED_BASE/models" | grep -q "\"$LOCAL_ALIAS\"" || fail "the local embedder ($EMBED_BASE) is not serving '$LOCAL_ALIAS' — the replica could answer nothing"
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
if [ -n "$MEDIA_FILE" ] && [ -f "$DEST/$MEDIA_FILE" ]; then
    # content-addressed names: additive, and an existing name already holds the same bytes
    MEDIA_DST="${MEM0_MEDIA_DIR:-$MEM0_DIR/media}"
    mkdir -p "$MEDIA_DST" && tar -C "$MEDIA_DST" --skip-old-files --no-same-owner -xf "$DEST/$MEDIA_FILE" \
        || fail "media restore from $MEDIA_FILE into $MEDIA_DST failed"
    echo "    media: $(find "$MEDIA_DST" -type f ! -name '*.tmp' | wc -l) file(s) in $MEDIA_DST"
fi

# ---------------------------------------------------------------- 4. the replica must actually answer
say "[4] start mem0 and prove it answers (/health/deep embeds through the local embedder)"
systemctl --user start mem0.service
health=""
for i in $(seq 1 45); do health="$(curl -sf -m 10 http://127.0.0.1:18791/health 2>/dev/null || true)"; printf '%s' "$health" | grep -q '"ok"[[:space:]]*:[[:space:]]*true' && break; sleep 2; [ "$i" = 45 ] && fail "replica mem0 did not come up healthy: ${health:-<no answer>} (systemctl --user status mem0.service)"; done
deep="$(curl -sf -m 120 http://127.0.0.1:18791/health/deep 2>/dev/null || true)"
# the TOP-LEVEL ok: a grep for any "ok": true also matched every healthy sub-check, so a red /health/deep passed
printf '%s' "$deep" | jq -e '.ok == true' >/dev/null 2>&1 || fail "replica /health/deep not ok: ${deep:0:300}"
# ...and bound to the space and collection that were just restored (a server reports both; one that
# predates the profile report says nothing and is not second-guessed)
bound_profile="$(printf '%s' "$deep" | jq -r '.embed_profile.profile // empty' 2>/dev/null || true)"
bound_collection="$(printf '%s' "$deep" | jq -r '.collection // empty' 2>/dev/null || true)"
[ -z "$bound_profile" ] || [ "$bound_profile" = "$MP_PROFILE" ] || fail "replica mem0 is bound to embedding profile '$bound_profile', but the restored set is '$MP_PROFILE'"
[ -z "$bound_collection" ] || [ "$bound_collection" = "$COLLECTION" ] || fail "replica mem0 is bound to collection '$bound_collection', but the set was restored into '$COLLECTION'"
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
