#!/usr/bin/env bash
# deploy.sh — THE single deploy path from this repo to the live runtime (v1.12, MEM-7).
#
# Before this existed, production spanned three roots (hand-copied app dir, systemd
# timers exec'ing the live dev working tree, a third copy under the Windows .claude
# dir) and the v1.11.0 P0 shipped exactly through that gap: a module hand-copied to
# the runtime but never added to the installer. One deploy path, gated, kills the class.
#
# What it does (in order):
#   1. rsync server modules + tests + requirements.txt  -> ~/apps/mem0-server/
#   2. rsync maintenance scripts                        -> ~/apps/mem0-scripts/
#   3. install sentinel-resolved systemd units          -> ~/.config/systemd/user/
#   4. import-smoke the server IN ITS VENV — refuses to restart on failure
#   5. restart mem0.service, wait for /health, assert /health/deep ok:true
#
# Usage:  bash deploy.sh [--dry-run]
# Rollback: previous module bytes are in the stack backup (~/.mem0/backups/) and git;
#           `git checkout <last-good> && bash deploy.sh` is the restore path.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="$HOME/apps/mem0-server"
SCRIPTS_DIR="$HOME/apps/mem0-scripts"
SYSTEMD_DIR="$HOME/.config/systemd/user"
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

# Operator receipt (WIN_USER / DISTRO for unit sentinels); WSL user is always $USER.
[ -f "$HOME/.mem0/stack.env" ] && . "$HOME/.mem0/stack.env"
WSL_USER="$USER"
WIN_USER="${MEM0_WIN_USER:-$USER}"
DISTRO="${MEM0_WSL_DISTRO:-Ubuntu}"

echo "==> deploy: $REPO_ROOT -> live runtime ${DRY:+(DRY RUN)}"
if [ -n "$(git -C "$REPO_ROOT" status --porcelain -- mem0-server scripts/wsl systemd 2>/dev/null)" ]; then
    echo "  WARN: deploying a DIRTY working tree (uncommitted changes in the deployed surface)"
fi

# --- 1. server: top-level modules + requirements (never deletes runtime extras) ---
rsync -rc $DRY -v --include='*.py' --include='requirements.txt' --exclude='*' \
    "$REPO_ROOT/mem0-server/" "$APP_DIR/" | grep -v '^$' | sed 's/^/    server: /'
# NOTE: tests/ deliberately NOT deployed — the suite's home is the repo (it resolves
# scripts/wsl/ relative to the repo layout). The full gate runs: cd REPO_ROOT/mem0-server && pytest.
rsync -c $DRY -v "$REPO_ROOT/scripts/wsl/dpapi-fetch-key.sh" "$APP_DIR/dpapi-fetch-key.sh" | sed 's/^/    dpapi: /'
# MEM-17 (2026-07-03): stamp the app dir with the repo VERSION so /health can
# report the ACTUAL stack release (app.py reads ./VERSION beside app.py at
# startup; without this stamp a deployed runtime falls back to "unknown").
rsync -c $DRY -v "$REPO_ROOT/VERSION" "$APP_DIR/VERSION" | sed 's/^/    version: /'

# --- 2. maintenance scripts runtime root ---
mkdir -p "$SCRIPTS_DIR"
rsync -rc $DRY -v --include='*.py' --include='*.sh' --exclude='*' \
    "$REPO_ROOT/scripts/wsl/" "$SCRIPTS_DIR/" | grep -v '^$' | sed 's/^/    scripts: /'

# --- 3. systemd units (same sentinel resolution as the installer) ---
for src in "$REPO_ROOT"/systemd/*.service "$REPO_ROOT"/systemd/*.timer; do
    [ -f "$src" ] || continue
    unit="$(basename "$src")"
    resolved="$(sed -e "s|__WSL_USER__|$WSL_USER|g" \
                    -e "s|__WIN_USER__|$WIN_USER|g" \
                    -e "s|__WSL_DISTRO__|$DISTRO|g" \
                    -e "s|__MEM0_BIND__|${MEM0_BIND:-127.0.0.1}|g" \
                    -e "s|__REPO_ROOT_WSL__|$REPO_ROOT|g" "$src")"
    if [ -n "$DRY" ]; then
        if ! diff -q <(printf '%s\n' "$resolved") "$SYSTEMD_DIR/$unit" >/dev/null 2>&1; then
            echo "    unit CHANGED: $unit"
        fi
    else
        printf '%s\n' "$resolved" > "$SYSTEMD_DIR/$unit"
    fi
done

if [ -n "$DRY" ]; then
    echo "==> dry-run complete (nothing written, no restart)"
    exit 0
fi

chmod +x "$SCRIPTS_DIR"/*.sh "$APP_DIR/dpapi-fetch-key.sh" 2>/dev/null || true
systemctl --user daemon-reload

# --- 4. import smoke in the LIVE venv — a bad module never reaches a restart ---
if ! (cd "$APP_DIR" && ./.venv/bin/python -c "import app" >/dev/null 2>&1); then
    echo "==> IMPORT SMOKE FAILED — mem0.service NOT restarted. Runtime still running the previous code."
    (cd "$APP_DIR" && ./.venv/bin/python -c "import app") || true
    exit 1
fi
echo "    import smoke OK"

# --- 5. restart + health gate ---
systemctl --user restart mem0.service
for i in $(seq 1 30); do
    curl -sf http://127.0.0.1:18791/health >/dev/null 2>&1 && break
    sleep 1
    [ "$i" = 30 ] && { echo "==> HEALTH GATE FAILED after 30s — check: journalctl --user -u mem0.service -n 50"; exit 1; }
done
curl -sf http://127.0.0.1:18791/health/deep | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d.get('ok'), f'/health/deep not ok: {d}'
# checks values are HETEROGENEOUS: sub-dicts with an 'ok' key AND bare scalars
# (e.g. pending_contradiction_reviews: 0) — .get on an int crashed this print
# (2026-07-17 cutover) and failed an otherwise-green deploy.
print('    /health/deep OK — ' + ', '.join(
    k + ':' + str(v.get('ok', v) if isinstance(v, dict) else v)
    for k, v in d.get('checks', {}).items()))
"
echo "==> deploy complete. Full gate: cd $APP_DIR && MEM0_KEY=\$(cat ~/.mem0/api-key) MEM0_URL=http://127.0.0.1:18791 ./.venv/bin/pytest -q"
