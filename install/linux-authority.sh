#!/usr/bin/env bash
# install/linux-authority.sh — the native Linux AUTHORITY install (spec §4; no WSL anywhere).
#
# What it does, in order:
#   [0] prerequisites: not root; tools; the two systemd-creds files; bind address is a real
#       non-wildcard IPv4 (the tailscale0 address); the Phase 0 symlink set resolves off the
#       root disk; the local embedder answers on :11436
#   [1] ~/.mem0/role=brain, stack.env (MEM0_HOST_KIND=native, MEM0_BIND, MEM0_ROLE=brain,
#       MEM0_SECRETS_DIR), authority-url
#   [2] Python 3.12 via uv (the system Python is untested with the pinned wheel set)
#   [3] Qdrant (loopback 6333; ZFS is an accepted storage filesystem — the Phase 1 restore
#       drill is its proof)
#   [4] mem0 server venv + modules + VERSION stamp (same lists as the WSL installer)
#   [5] units: mem0/qdrant/l10-audit + the ams-nightly chain + the native drop-in;
#       NO per-job timers (one chain, spec §4); scripts deployed to ~/apps/mem0-scripts
#   [6] enable qdrant, mem0, l10-audit.timer, ams-nightly.timer; health probes on the bind ip
#
# Usage:
#   bash install/linux-authority.sh --bind-ip <tailscale0 ipv4> --secrets-dir <dir with *.cred> \
#        [--zfs-dataset <pool/dataset>] [--user-id <tenant>] [--eval-root <dir>] [--pcloud-dir <dir>]
#        [--dry-run] [--render-only <dir>]
#   --zfs-dataset: the dataset /health/maintenance reads pool usage from (omit on a non-ZFS box).
#   --eval-root:   checkout holding eval/retrieval-drift/retrieval_drift.py (the dream's drift
#                  canary); written to stack.env as MEM0_EVAL_ROOT. Omit -> the canary no-ops.
#   --pcloud-dir:  where the chain's pcloud-copy step mirrors the newest backup set
#                  (stack.env MEM0_PCLOUD_DIR; default ~/pCloudDrive/memory-backups/<hostname>).
#   --render-only: write the resolved unit set (units + drop-in) into <dir> and exit; touches
#                  nothing else (the test harness uses it).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BIND_IP=""; SECRETS_DIR=""; USER_ID="${USER:-$(id -un)}"; DRY_RUN=0; RENDER_ONLY=""; ZFS_DATASET=""; EVAL_ROOT=""; PCLOUD_DIR=""
MEM0_DIR="$HOME/.mem0"; MEM0_APP="$HOME/apps/mem0-server"; SCRIPTS_DIR="$HOME/apps/mem0-scripts"
QDRANT_DIR="$HOME/qdrant-server"; SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
WSL_INSTALLER="$REPO_ROOT/install/1-wsl-services.sh"
UV="${UV:-$HOME/.local/bin/uv}"

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --bind-ip) BIND_IP="${2:-}"; shift 2 ;;
        --secrets-dir) SECRETS_DIR="${2:-}"; shift 2 ;;
        --zfs-dataset) ZFS_DATASET="${2:-}"; shift 2 ;;
        --user-id) USER_ID="${2:-}"; shift 2 ;;
        --eval-root) EVAL_ROOT="${2:-}"; shift 2 ;;
        --pcloud-dir) PCLOUD_DIR="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --render-only) RENDER_ONLY="${2:-}"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 2 ;;
    esac
done
fail() { echo "FAIL: $*" >&2; exit 1; }
say()  { echo "==> $*"; }
plan() { if [ "$DRY_RUN" = 1 ]; then echo "    [dry-run] $*"; return 0; fi; return 1; }

# ---------------------------------------------------------------- 0. prerequisites
say "[0] prerequisites"
[ "$(id -u)" != 0 ] || fail "run as the service user, not root"
[ -n "$BIND_IP" ] || fail "--bind-ip <tailscale0 ipv4> is required (never 0.0.0.0; spec §4 bind rule)"
[[ "$BIND_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || fail "--bind-ip must be a plain IPv4 (the tailscale0 address), got '$BIND_IP' — a wildcard, loopback or name is not a bind"
case "$BIND_IP" in 0.0.0.0|127.*) fail "--bind-ip '$BIND_IP' is a wildcard/loopback bind; the authority binds the tailscale0 address only" ;; esac
[ -n "$SECRETS_DIR" ] || fail "--secrets-dir <dir> is required (holds ams-api-key.cred and ams-canonical-key.cred from systemd-creds)"
for c in ams-api-key.cred ams-canonical-key.cred; do [ -s "$SECRETS_DIR/$c" ] || fail "missing $SECRETS_DIR/$c (Phase 0 P0-3: systemd-creds --user encrypt --with-key=host+tpm2)"; done
[[ "$USER_ID" =~ ^[A-Za-z0-9._-]+$ ]] || fail "--user-id must be a plain tenant name (letters, digits, . _ -), got '$USER_ID'"
[ -z "$EVAL_ROOT" ] || [ -f "$EVAL_ROOT/eval/retrieval-drift/retrieval_drift.py" ] || fail "--eval-root $EVAL_ROOT has no eval/retrieval-drift/retrieval_drift.py"
[ -f "$WSL_INSTALLER" ] || fail "missing $WSL_INSTALLER (run from a repo checkout)"
MEM0_MODULES="$(grep -E '^MEM0_MODULES=' "$WSL_INSTALLER" | head -1 | sed -E 's/^MEM0_MODULES="(.*)"$/\1/')"
QDRANT_VERSION="$(grep -E '^QDRANT_VERSION=' "$WSL_INSTALLER" | head -1 | cut -d= -f2 | tr -d '[:space:]')"
PIP_SPECS="$(grep -E "pip install --quiet 'mem0ai" "$WSL_INSTALLER" | head -1 | sed -E "s/^.*pip install --quiet //")"
[ -n "$MEM0_MODULES" ] && [ -n "$QDRANT_VERSION" ] && [ -n "$PIP_SPECS" ] || fail "could not read MEM0_MODULES / QDRANT_VERSION / the mem0 pip line from $WSL_INSTALLER"
STACK_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"
UNITS="mem0.service qdrant.service l10-audit.service l10-audit.timer"
for u in "$REPO_ROOT"/systemd/ams-*; do [ -f "$u" ] && UNITS="$UNITS $(basename "$u")"; done
echo "    stack $STACK_VERSION; bind $BIND_IP; secrets $SECRETS_DIR; tenant $USER_ID"
echo "    units: $UNITS"

render_units() {  # $1 = destination dir
    local dst="$1"; mkdir -p "$dst/mem0.service.d"
    for unit in $UNITS; do
        # /home/__WSL_USER__ is the WSL layout, where the Linux user IS the tenant. On a native box
        # they differ (the first live l10-audit run failed 203/EXEC on /home/<tenant>/...), so every
        # home-relative path renders as %h FIRST; the bare sentinel then becomes the tenant id.
        sed -e "s|/home/__WSL_USER__|%h|g" -e "s|__WSL_USER__|$USER_ID|g" -e "s|__WIN_USER__||g" -e "s|__WSL_DISTRO__|native|g" \
            -e "s|__MEM0_BIND__|$BIND_IP|g" -e "s|__REPO_ROOT_WSL__|$REPO_ROOT|g" -e "s|__SECRETS_DIR__|$SECRETS_DIR|g" "$REPO_ROOT/systemd/$unit" > "$dst/$unit"
        # The shared mem0.service keeps its WSL ExecStartPre (the DPAPI fetch) so the WSL install is
        # untouched; a native box must not carry it. The drop-in below supplies the native pre-start.
        sed -i '/dpapi-fetch-key\.sh/d' "$dst/$unit"
        grep -q "__[A-Z_]*__" "$dst/$unit" && fail "unresolved sentinel in $unit"
    done
    sed -e "s|__MEM0_BIND__|$BIND_IP|g" -e "s|__SECRETS_DIR__|$SECRETS_DIR|g" -e "s|__ZFS_DATASET__|$ZFS_DATASET|g" \
        "$REPO_ROOT/systemd/mem0-native.conf" > "$dst/mem0.service.d/native.conf"
    # no dataset given: the endpoint falls back to the disk usage of the home filesystem
    [ -n "$ZFS_DATASET" ] || sed -i '/^Environment=MEM0_ZFS_DATASET=$/d' "$dst/mem0.service.d/native.conf"
    grep -q "__[A-Z_]*__" "$dst/mem0.service.d/native.conf" && fail "unresolved sentinel in native.conf"
    local bad
    bad="$(grep -lE '/mnt/c|cmd\.exe|powershell\.exe|dpapi-fetch-key\.sh|/run/WSL' "$dst"/* "$dst"/mem0.service.d/native.conf 2>/dev/null || true)"
    [ -z "$bad" ] || fail "a rendered unit still carries a WSL-only line: $bad"
    return 0
}
if [ -n "$RENDER_ONLY" ]; then
    render_units "$RENDER_ONLY"
    echo "    rendered $(find "$RENDER_ONLY" -type f | wc -l) file(s) into $RENDER_ONLY"
    exit 0
fi

if [ "$DRY_RUN" = 0 ]; then
    for t in python3 curl jq systemctl ip systemd-creds; do command -v "$t" >/dev/null || fail "$t is required"; done
    for p in "$HOME/apps/mem0-server" "$HOME/apps/mem0-scripts" "$HOME/qdrant-server" "$HOME/.mem0"; do
        [ -L "$p" ] || fail "$p is not a symlink into the AMS dataset (Phase 0 P0-2 symlink set; spec §4: nothing on the root disk)"
    done
    ip -4 -o addr show | grep -q " inet ${BIND_IP}/" || fail "$BIND_IP is not present on any interface (is tailscaled up?)"
    curl -sf -m 5 http://127.0.0.1:11436/v1/models >/dev/null || fail "no local embedder on :11436 (llama-swap)"
fi

# ---------------------------------------------------------------- 1. role + receipts
say "[1] role=brain, stack.env (MEM0_HOST_KIND=native), authority-url"
if plan "write $MEM0_DIR/role=brain, stack.env, authority-url=http://$BIND_IP:18791"; then :; else
    mkdir -p "$MEM0_DIR"; umask 077
    printf 'brain\n' > "$MEM0_DIR/role"
    cat > "$MEM0_DIR/stack.env" <<ENV
MEM0_WSL_USER=$USER_ID
MEM0_WIN_USER=
MEM0_DISTRO=native
MEM0_HOST_KIND=native
MEM0_REPO_ROOT_WSL=$REPO_ROOT
MEM0_BIND=$BIND_IP
MEM0_ROLE=brain
MEM0_SECRETS_DIR=$SECRETS_DIR
ENV
    [ -z "$EVAL_ROOT" ] || printf 'MEM0_EVAL_ROOT=%s\n' "$EVAL_ROOT" >> "$MEM0_DIR/stack.env"
    [ -z "$PCLOUD_DIR" ] || printf 'MEM0_PCLOUD_DIR=%s\n' "$PCLOUD_DIR" >> "$MEM0_DIR/stack.env"
    printf 'http://%s:18791\n' "$BIND_IP" > "$MEM0_DIR/authority-url"
    umask 022; echo "    written"
fi

# ---------------------------------------------------------------- 2. python 3.12 via uv
say "[2] python 3.12 (uv-managed)"
SERVER_PY=""
if plan "uv python install 3.12; uv python find 3.12"; then SERVER_PY="<uv python3.12>"; else
    if [ ! -x "$UV" ]; then
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
        UV="$HOME/.local/bin/uv"
    fi
    "$UV" python install 3.12 >/dev/null
    SERVER_PY="$("$UV" python find 3.12)"
fi
echo "    server python: $SERVER_PY"

# ---------------------------------------------------------------- 3. qdrant
say "[3] qdrant $QDRANT_VERSION at $QDRANT_DIR (127.0.0.1:6333)"
qdrant_fs_ok() { case "$(stat -f -c %T "$1" 2>/dev/null)" in ext2/ext3|ext4|xfs|btrfs|tmpfs|zfs) return 0 ;; esac; return 1; }
if plan "download qdrant, write config.yaml (127.0.0.1:6333)"; then :; else
    mkdir -p "$QDRANT_DIR/config" "$QDRANT_DIR/storage" "$QDRANT_DIR/snapshots"
    qdrant_fs_ok "$QDRANT_DIR/storage" || fail "qdrant storage is on $(stat -f -c %T "$QDRANT_DIR/storage"); accepted: ext4/xfs/btrfs/tmpfs/zfs"
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

# ---------------------------------------------------------------- 4. mem0 server
say "[4] mem0 server at $MEM0_APP"
if plan "venv with $SERVER_PY; pip install $PIP_SPECS; deploy $(echo "$MEM0_MODULES" | wc -w) modules; warm BM25"; then :; else
    mkdir -p "$MEM0_APP"
    for mod in $MEM0_MODULES; do cp "$REPO_ROOT/mem0-server/$mod" "$MEM0_APP/$mod"; done
    # Stamp the release beside app.py (see linux-replica.sh: a runtime that cannot say which
    # release it runs is how a missed deploy step hides).
    cp "$REPO_ROOT/VERSION" "$MEM0_APP/VERSION"
    [ -x "$MEM0_APP/.venv/bin/python" ] || "$SERVER_PY" -m venv "$MEM0_APP/.venv"
    "$MEM0_APP/.venv/bin/pip" install --quiet --disable-pip-version-check --upgrade pip
    eval "\"$MEM0_APP/.venv/bin/pip\" install --quiet --disable-pip-version-check $PIP_SPECS"
    export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$HOME/.cache/fastembed}"; mkdir -p "$FASTEMBED_CACHE_PATH"
    "$MEM0_APP/.venv/bin/python" - <<'PYEOF' || fail "server post-conditions not satisfied (fastmcp importable, fastembed BM25 encoder loadable)"
from fastmcp import FastMCP  # noqa: F401
from fastembed import SparseTextEmbedding
SparseTextEmbedding(model_name="Qdrant/bm25")
print("    server deps OK; BM25 cache warm")
PYEOF
fi

# ---------------------------------------------------------------- 5. units + scripts
say "[5] units (native drop-in; no per-job timers) + maintenance scripts"
if plan "render $UNITS + mem0.service.d/native.conf into $SYSTEMD_USER_DIR; deploy scripts/wsl/*.{py,sh} to $SCRIPTS_DIR"; then :; else
    mkdir -p "$SYSTEMD_USER_DIR" "$SCRIPTS_DIR"
    render_units "$SYSTEMD_USER_DIR"
    for f in "$REPO_ROOT"/scripts/wsl/*.py "$REPO_ROOT"/scripts/wsl/*.sh; do
        [ -f "$f" ] || continue
        tr -d "\r" < "$f" > "$SCRIPTS_DIR/$(basename "$f")"
    done
    chmod +x "$SCRIPTS_DIR"/*.sh
    cp "$REPO_ROOT/scripts/wsl/l10-audit.py" "$MEM0_DIR/l10-audit.py"
    systemctl --user daemon-reload
    # One chain (spec §4): any per-job timer a previous install enabled is turned off, never deleted.
    for t in decay-scan stack-backup goals-stale-sweep contradiction-sweep retrieval-pairs episodic-reconcile goal-recurrence-promote egemma-rollback-prune offline-watcher; do
        systemctl --user disable --now "$t.timer" >/dev/null 2>&1 || true
    done
    echo "    units rendered; per-job timers off"
    # Bind belt persistence (P0-5 design note): a ROOT oneshot loading the AMS table from its own
    # file. Never nftables.service (its conf flushes the iptables-nft tables tailscale/docker own).
    if [ -s /etc/nftables.d/ams.nft ] && sudo -n true 2>/dev/null; then
        sudo -n install -m 0644 "$REPO_ROOT/systemd/ams-nft.service" /etc/systemd/system/ams-nft.service \
            && sudo -n systemctl daemon-reload && sudo -n systemctl enable --now ams-nft.service >/dev/null 2>&1 \
            && echo "    nft belt: $(sudo -n nft list table inet ams 2>/dev/null | grep -c dport) rule(s) live, ams-nft.service enabled" \
            || echo "    WARN: ams-nft.service install failed; run: sudo install -m 0644 systemd/ams-nft.service /etc/systemd/system/ && sudo systemctl enable --now ams-nft.service"
    else
        echo "    WARN: nft belt not persisted (no /etc/nftables.d/ams.nft or no passwordless sudo); run: sudo install -m 0644 systemd/ams-nft.service /etc/systemd/system/ && sudo systemctl enable --now ams-nft.service"
    fi
fi

# ---------------------------------------------------------------- 6. enable + probes
say "[6] enable qdrant, mem0, l10-audit.timer, ams-nightly.timer"
if plan "systemctl --user enable --now qdrant mem0 l10-audit.timer ams-nightly.timer; probe http://$BIND_IP:18791/health and /health/deep"; then exit 0; fi
if [ "$(loginctl show-user "$USER" --property=Linger 2>/dev/null)" != "Linger=yes" ]; then
    sudo -n loginctl enable-linger "$USER" 2>/dev/null || echo "    WARN: linger not enabled (run once: sudo loginctl enable-linger $USER)"
fi
systemctl --user enable --now qdrant.service mem0.service l10-audit.timer
systemctl --user enable --now ams-nightly.timer 2>/dev/null || echo "    (ams-nightly.timer not present in this checkout)"
# The step services attach to the target through their [Install] WantedBy=; that symlink only
# exists once each step is ENABLED (not started). The first live chain run started the target
# and pulled in nothing because only the timer had been enabled.
for u in "$SYSTEMD_USER_DIR"/ams-step-*.service; do [ -f "$u" ] && systemctl --user enable "$(basename "$u")" >/dev/null 2>&1; done
echo "    chain steps enabled: $(ls "$SYSTEMD_USER_DIR"/ams-nightly.target.wants/ 2>/dev/null | tr "\n" " ")"
for i in $(seq 1 60); do curl -sf -m 2 "http://$BIND_IP:18791/health" >/dev/null && break; sleep 2; done
curl -sf -m 5 "http://$BIND_IP:18791/health" | jq -c . || fail "mem0 did not answer on http://$BIND_IP:18791/health (journalctl --user -u mem0)"
curl -sf -m 60 "http://$BIND_IP:18791/health/deep" | jq -c '{ok, stack, canonical_key: .checks.canonical_key, embedder: .checks.embedder, judge_transport: .checks.judge_transport}' \
    || echo "    WARN: /health/deep not ok yet (an empty store is expected before the staging restore)"
echo
echo "Native authority installed. Bind $BIND_IP:18791; secrets via systemd-creds; timers: systemctl --user list-timers ams-nightly.timer l10-audit.timer"
