#!/usr/bin/env bash
# install/linux-replica.sh — the Linux REPLICA install (native Linux, no WSL).
#
# A replica is a thin client (install/linux-client.sh: shim + outbox replay, Brain as the
# authority) PLUS a dormant local copy of the Brain it can read from when the Brain is
# unreachable. This installs, on top of the client:
#   - Qdrant + the mem0 server in their own venv, as systemd USER units that are installed but
#     NOT enabled (the watcher starts them on go_offline, stops them on go_online);
#   - restore-replica.sh (fetch the Brain's newest snapshot set over SSH, restore it read-only)
#     and offline-watcher.py (2-minute tick: probe, fail over, recover, refresh while online),
#     deployed beside the shim; the watcher runs from a systemd user timer (linger enabled);
#   - ~/.mem0/role = replica, ~/.mem0/replica.env (how to reach the Brain's snapshots),
#     ~/.mem0/stack.env (role + loopback bind the server reads).
# It ends with a FIRST RESTORE: the newest set is pulled and restored, /health/deep must answer
# through the local embedder, then the services are stopped again (dormant while online).
#
# Requires a local EmbeddingGemma@768 on :11436 (llama-swap), the same model the Brain
# embeds with — a replica with a different embedder answers nonsense.
#
# Usage:
#   bash install/linux-replica.sh --authority http://<brain-host>:18791 --brain-ssh <ssh-alias> \
#        [--brain-wsl <distro>:<user>] [--brain-backup-dir <remote dir>] \
#        [--api-key-file <file>] [--user-id <tenant>] [--qdrant-storage-gb <n>] [--dry-run]
#   --brain-wsl: the Brain keeps its stack inside WSL on a Windows host; snapshot commands run
#                through wsl.exe on that host (the usual Windows+WSL install).
#   --qdrant-storage-gb: size of the ext4 image that backs Qdrant's storage when the home
#                filesystem cannot host it (default 8). Qdrant 1.18.2's snapshot restore fails
#                on f2fs ("Failed to load ID tracker mappings"; verified: tmpfs and ext4 restore
#                the same snapshot fine, f2fs fails with or without compression), so on anything
#                but ext4/xfs/btrfs/tmpfs the storage directory is a loop-mounted ext4 image.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AUTHORITY=""; BRAIN_SSH=""; BRAIN_WSL=""; BRAIN_BACKUP_DIR='~/.mem0/backups'
API_KEY_FILE=""; USER_ID="${USER:-$(id -un)}"; DRY_RUN=0; QDRANT_STORAGE_GB=8
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
CLIENT_DIR="${MEM0_CLIENT_DIR:-$HOME/apps/mem0-client}"
MEM0_DIR="$HOME/.mem0"
MEM0_APP="$HOME/apps/mem0-server"
QDRANT_DIR="$HOME/qdrant-server"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
WSL_INSTALLER="$REPO_ROOT/install/1-wsl-services.sh"

usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --authority) AUTHORITY="${2:-}"; shift 2 ;;
        --brain-ssh) BRAIN_SSH="${2:-}"; shift 2 ;;
        --brain-wsl) BRAIN_WSL="${2:-}"; shift 2 ;;
        --brain-backup-dir) BRAIN_BACKUP_DIR="${2:-}"; shift 2 ;;
        --api-key-file) API_KEY_FILE="${2:-}"; shift 2 ;;
        --user-id) USER_ID="${2:-}"; shift 2 ;;
        --qdrant-storage-gb) QDRANT_STORAGE_GB="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 2 ;;
    esac
done
fail() { echo "FAIL: $*" >&2; exit 1; }
say()  { echo "==> $*"; }
plan() { if [ "$DRY_RUN" = 1 ]; then echo "    [dry-run] $*"; return 0; fi; return 1; }
is_local_url() {
    local url="$1" host
    host="$(printf '%s' "$url" | sed -nE 's#^[a-zA-Z][a-zA-Z0-9+.-]*://\[?([^]/:]+)\]?(:[0-9]+)?(/.*)?$#\1#p')"
    [ -z "$host" ] && return 0
    case "$host" in 127.*|localhost|*.localhost|0.0.0.0|::1|::) return 0 ;; esac
    return 1
}

# ---------------------------------------------------------------- 0. prerequisites
say "[0] prerequisites"
[ "$(id -u)" != 0 ] || fail "run as the user who runs Claude Code, not root"
[ -n "$AUTHORITY" ] || fail "--authority http://<brain-host>:18791 is required"
[ -n "$BRAIN_SSH" ] || fail "--brain-ssh <ssh alias of the Brain> is required (the replica pulls snapshots over SSH)"
AUTHORITY="${AUTHORITY%/}"
is_local_url "$AUTHORITY" && fail "a replica's authority must be a REMOTE Brain; '$AUTHORITY' is loopback/unspecified/malformed"
[[ "$USER_ID" =~ ^[A-Za-z0-9._-]+$ ]] || fail "--user-id must be a plain tenant name (letters, digits, . _ -), got '$USER_ID'"
[ -z "$BRAIN_WSL" ] || [[ "$BRAIN_WSL" == *:* ]] || fail "--brain-wsl must be <distro>:<user>"
[[ "$QDRANT_STORAGE_GB" =~ ^[0-9]+$ ]] && [ "$QDRANT_STORAGE_GB" -ge 1 ] || fail "--qdrant-storage-gb must be a whole number of GiB"
for t in python3 curl jq ssh systemctl; do command -v "$t" >/dev/null || fail "$t is required"; done
[ -f "$WSL_INSTALLER" ] || fail "missing $WSL_INSTALLER (run from a repo checkout)"
for f in scripts/travel/restore-replica.sh scripts/travel/offline-watcher.py systemd/mem0.service systemd/qdrant.service systemd/offline-watcher.service systemd/offline-watcher.timer scripts/wsl/generate-canonical-key.sh scripts/wsl/dpapi-fetch-key.sh; do
    [ -f "$REPO_ROOT/$f" ] || fail "missing $REPO_ROOT/$f"
done
# Single source of truth for what the server needs: the WSL-side installer's own lists.
MEM0_MODULES="$(grep -E '^MEM0_MODULES=' "$WSL_INSTALLER" | head -1 | sed -E 's/^MEM0_MODULES="(.*)"$/\1/')"
QDRANT_VERSION="$(grep -E '^QDRANT_VERSION=' "$WSL_INSTALLER" | head -1 | cut -d= -f2 | tr -d '[:space:]')"
PIP_SPECS="$(grep -E "pip install --quiet 'mem0ai" "$WSL_INSTALLER" | head -1 | sed -E "s/^.*pip install --quiet //")"
[ -n "$MEM0_MODULES" ] && [ -n "$QDRANT_VERSION" ] && [ -n "$PIP_SPECS" ] || fail "could not read MEM0_MODULES / QDRANT_VERSION / the mem0 pip line from $WSL_INSTALLER"
STACK_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"
echo "    stack $STACK_VERSION; authority $AUTHORITY; brain via ssh '$BRAIN_SSH'${BRAIN_WSL:+ (WSL $BRAIN_WSL)}; tenant $USER_ID"
echo "    server: qdrant $QDRANT_VERSION, $(echo "$MEM0_MODULES" | wc -w) modules, specs: $PIP_SPECS"
if [ "$DRY_RUN" = 0 ]; then
    curl -sf -m 5 http://127.0.0.1:11436/v1/models >/dev/null || fail "no local embedder on :11436 — serve EmbeddingGemma@768 through llama-swap first (see install/llama-swap-setup.md)"
    ssh -o BatchMode=yes -o ConnectTimeout=15 "$BRAIN_SSH" exit 0 >/dev/null 2>&1 || fail "ssh '$BRAIN_SSH' does not accept key auth from this box"
fi

# ---------------------------------------------------------------- 1. thin client first
say "[1] thin client (install/linux-client.sh)"
CLIENT_ARGS=(--authority "$AUTHORITY" --user-id "$USER_ID")
[ -n "$API_KEY_FILE" ] && CLIENT_ARGS+=(--api-key-file "$API_KEY_FILE")
[ "$DRY_RUN" = 1 ] && CLIENT_ARGS+=(--dry-run)
bash "$SCRIPT_DIR/linux-client.sh" "${CLIENT_ARGS[@]}"

# ---------------------------------------------------------------- 2. role + receipts
say "[2] role=replica, replica.env, stack.env"
if plan "write $MEM0_DIR/role=replica, replica.env (BRAIN_SSH/BRAIN_BACKUP_DIR/BRAIN_WSL), stack.env (MEM0_ROLE=replica, MEM0_BIND=127.0.0.1)"; then :; else
    umask 077
    printf 'replica\n' > "$MEM0_DIR/role"
    cat > "$MEM0_DIR/replica.env" <<ENV
# written by install/linux-replica.sh — how restore-replica.sh reaches the Brain's snapshots.
# BRAIN_BACKUP_DIR is a REMOTE path: single-quoted so a leading ~ survives being sourced here
# and expands on the Brain (unquoted, bash expanded it to this box's own home).
BRAIN_SSH='$BRAIN_SSH'
BRAIN_BACKUP_DIR='$BRAIN_BACKUP_DIR'
BRAIN_WSL='$BRAIN_WSL'
REPLICA_CACHE='$MEM0_DIR/replica-snapshots'
ENV
    cat > "$MEM0_DIR/stack.env" <<ENV
MEM0_WSL_USER=$USER_ID
MEM0_WIN_USER=
MEM0_DISTRO=native
MEM0_REPO_ROOT_WSL=$REPO_ROOT
MEM0_BIND=127.0.0.1
MEM0_ROLE=replica
ENV
    umask 022
    echo "    role=replica; replica.env + stack.env written"
fi

# ---------------------------------------------------------------- 3. python for the server
say "[3] python for the mem0 server"
SERVER_PY=""
for c in python3.13 python3.12; do command -v "$c" >/dev/null && { SERVER_PY="$(command -v "$c")"; break; }; done
if [ -z "$SERVER_PY" ]; then
    if [ -x "$CLIENT_DIR/.venv/bin/uv" ]; then SERVER_PY="$("$CLIENT_DIR/.venv/bin/uv" python find 3.12 2>/dev/null || true)"; fi
fi
if [ -z "$SERVER_PY" ]; then
    if plan "no python3.12/3.13 on PATH: bootstrap uv into the client venv and install a managed 3.12"; then SERVER_PY="<uv-managed python3.12>"; else
        "$CLIENT_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check uv
        "$CLIENT_DIR/.venv/bin/uv" python install 3.12 >/dev/null
        SERVER_PY="$("$CLIENT_DIR/.venv/bin/uv" python find 3.12)"
    fi
fi
echo "    server python: $SERVER_PY"

# ---------------------------------------------------------------- 4. qdrant (dormant)
say "[4] qdrant $QDRANT_VERSION at $QDRANT_DIR (loopback, dormant)"
qdrant_fs_ok() { case "$(stat -f -c %T "$1" 2>/dev/null)" in ext2/ext3|ext4|xfs|btrfs|tmpfs) return 0 ;; esac; return 1; }
if plan "download qdrant, write config.yaml (127.0.0.1:6333); back storage with a ${QDRANT_STORAGE_GB} GiB ext4 image unless the filesystem is ext4/xfs/btrfs/tmpfs"; then :; else
    mkdir -p "$QDRANT_DIR/config" "$QDRANT_DIR/storage" "$QDRANT_DIR/snapshots"
    # Qdrant's snapshot restore fails on f2fs (see the header); give it ext4 via a loop image.
    if mountpoint -q "$QDRANT_DIR/storage"; then
        echo "    storage already a mountpoint ($(stat -f -c %T "$QDRANT_DIR/storage"))"
    elif qdrant_fs_ok "$QDRANT_DIR/storage"; then
        echo "    storage on $(stat -f -c %T "$QDRANT_DIR/storage") — usable as is"
    else
        IMG="$QDRANT_DIR/storage.img"
        echo "    storage is on $(stat -f -c %T "$QDRANT_DIR/storage"): backing it with an ext4 image ($IMG, ${QDRANT_STORAGE_GB} GiB sparse)"
        command -v mkfs.ext4 >/dev/null || fail "mkfs.ext4 is required to back Qdrant storage with an ext4 image"
        if [ ! -s "$IMG" ]; then
            truncate -s "${QDRANT_STORAGE_GB}G" "$IMG" && mkfs.ext4 -q -F "$IMG" || fail "could not create $IMG"
        fi
        FSTAB_LINE="$IMG $QDRANT_DIR/storage ext4 loop,noatime,nofail 0 0"
        grep -qF "$IMG " /etc/fstab || printf '%s\n' "$FSTAB_LINE" | sudo -n tee -a /etc/fstab >/dev/null || fail "could not add the storage image to /etc/fstab (needs passwordless sudo): $FSTAB_LINE"
        sudo -n systemctl daemon-reload 2>/dev/null || true
        sudo -n mount "$QDRANT_DIR/storage" || fail "could not mount $IMG on $QDRANT_DIR/storage"
        sudo -n chown "$USER:$USER" "$QDRANT_DIR/storage" || fail "could not chown the mounted storage"
        echo "    mounted: $(df -hT "$QDRANT_DIR/storage" | tail -1)"
    fi
    if [ ! -x "$QDRANT_DIR/qdrant" ]; then
        curl -fsSL -o /tmp/qdrant.tar.gz "https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VERSION}/qdrant-x86_64-unknown-linux-gnu.tar.gz"
        tar -xzf /tmp/qdrant.tar.gz -C "$QDRANT_DIR"; rm -f /tmp/qdrant.tar.gz; chmod +x "$QDRANT_DIR/qdrant"
    fi
    cat > "$QDRANT_DIR/config/config.yaml" <<YAML
storage:
  storage_path: $QDRANT_DIR/storage
  snapshots_path: $QDRANT_DIR/snapshots
service:
  host: 127.0.0.1
  http_port: 6333
log_level: INFO
YAML
    echo "    $("$QDRANT_DIR/qdrant" --version 2>/dev/null | head -1)"
fi

# ---------------------------------------------------------------- 5. mem0 server venv (dormant)
say "[5] mem0 server at $MEM0_APP"
if plan "venv with $SERVER_PY; pip install $PIP_SPECS; deploy $(echo "$MEM0_MODULES" | wc -w) modules + dpapi-fetch-key.sh; warm the BM25 cache"; then :; else
    mkdir -p "$MEM0_APP"
    for mod in $MEM0_MODULES; do cp "$REPO_ROOT/mem0-server/$mod" "$MEM0_APP/$mod"; done
    tr -d "\r" < "$REPO_ROOT/scripts/wsl/dpapi-fetch-key.sh" > "$MEM0_APP/dpapi-fetch-key.sh"; chmod +x "$MEM0_APP/dpapi-fetch-key.sh"
    [ -x "$MEM0_APP/.venv/bin/python" ] || "$SERVER_PY" -m venv "$MEM0_APP/.venv"
    "$MEM0_APP/.venv/bin/pip" install --quiet --disable-pip-version-check --upgrade pip
    eval "\"$MEM0_APP/.venv/bin/pip\" install --quiet --disable-pip-version-check $PIP_SPECS"
    export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$HOME/.cache/fastembed}"; mkdir -p "$FASTEMBED_CACHE_PATH"
    "$MEM0_APP/.venv/bin/python" - <<'PYEOF' || fail "server post-conditions not satisfied (fastmcp importable, fastembed BM25 encoder loadable, app importable)"
import sys
from fastmcp import FastMCP  # noqa: F401
from fastembed import SparseTextEmbedding
SparseTextEmbedding(model_name="Qdrant/bm25")
sys.path.insert(0, ".")
print("    server deps OK; BM25 cache warm")
PYEOF
    # canonical key: a replica signs nothing, but the server refuses to start without one.
    if [ ! -f "$MEM0_DIR/canonical-key" ] && [ ! -f "$MEM0_DIR/canonical-key.dpapi" ]; then
        bash "$REPO_ROOT/scripts/wsl/generate-canonical-key.sh" >/dev/null && echo "    canonical-key generated (replica-local)"
    fi
fi

# ---------------------------------------------------------------- 6. units + scripts
say "[6] systemd user units (mem0/qdrant installed but DISABLED; offline-watcher timer enabled) + scripts"
if plan "install mem0.service qdrant.service offline-watcher.{service,timer}; deploy restore-replica.sh + offline-watcher.py to $SCRIPTS_DIR; enable linger"; then :; else
    mkdir -p "$SYSTEMD_USER_DIR" "$SCRIPTS_DIR"
    for unit in mem0.service qdrant.service offline-watcher.service offline-watcher.timer; do
        sed -e "s|__WSL_USER__|$USER_ID|g" -e "s|__WIN_USER__||g" -e "s|__WSL_DISTRO__|native|g" \
            -e "s|__MEM0_BIND__|127.0.0.1|g" -e "s|__REPO_ROOT_WSL__|$REPO_ROOT|g" "$REPO_ROOT/systemd/$unit" > "$SYSTEMD_USER_DIR/$unit"
        grep -q "__[A-Z_]*__" "$SYSTEMD_USER_DIR/$unit" && fail "unresolved sentinel in $unit"
        echo "    installed: $unit"
    done
    for f in restore-replica.sh offline-watcher.py; do
        tr -d "\r" < "$REPO_ROOT/scripts/travel/$f" > "$SCRIPTS_DIR/$f"; chmod +x "$SCRIPTS_DIR/$f"; echo "    deployed: $f"
    done
    systemctl --user daemon-reload
    systemctl --user disable mem0.service qdrant.service >/dev/null 2>&1 || true
    systemctl --user stop mem0.service qdrant.service >/dev/null 2>&1 || true
    systemctl --user enable --now offline-watcher.timer
    if [ "$(loginctl show-user "$USER" --property=Linger 2>/dev/null)" != "Linger=yes" ]; then
        sudo -n loginctl enable-linger "$USER" 2>/dev/null && echo "    linger enabled" || echo "    WARN: could not enable linger (run once: sudo loginctl enable-linger $USER) — without it the watcher only ticks while you are logged in"
    fi
    python3 - "$MEM0_DIR/client-receipt.json" "$BRAIN_SSH" "$STACK_VERSION" <<'PYEOF'
import json, sys
p, brain, ver = sys.argv[1:4]
try:
    d = json.load(open(p, encoding="utf-8"))
except Exception:
    d = {}
d.update({"role": "replica", "brain_ssh": brain, "stack_version": ver})
json.dump(d, open(p, "w", encoding="utf-8"))
PYEOF
    echo "    receipt updated (role=replica)"
fi

# ---------------------------------------------------------------- 7. first restore = the proof
say "[7] first restore: pull the Brain's newest set, restore it, prove /health/deep, stop again"
if plan "bash $SCRIPTS_DIR/restore-replica.sh (then a watcher dry-run tick)"; then exit 0; fi
bash "$SCRIPTS_DIR/restore-replica.sh"
"$CLIENT_DIR/.venv/bin/python" "$SCRIPTS_DIR/offline-watcher.py" --dry-run
echo
echo "Linux replica installed. The watcher ticks every 2 min (systemctl --user list-timers offline-watcher.timer);"
echo "state: $MEM0_DIR/offline-mode.json; last restore: $MEM0_DIR/replica-restored; logs: $MEM0_DIR/offline-watcher.log, $MEM0_DIR/replica-restore.log"
