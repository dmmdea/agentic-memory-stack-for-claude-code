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
# stack.env (written by install/1-wsl-services.sh) defines MEM0_DISTRO; an earlier
# version read only MEM0_WSL_DISTRO and therefore always fell back to the literal
# 'Ubuntu', mis-resolving __WSL_DISTRO__ in unit sentinels on any other distro name.
# Read both, preferring the receipt's own spelling.
DISTRO="${MEM0_DISTRO:-${MEM0_WSL_DISTRO:-Ubuntu}}"

echo "==> deploy: $REPO_ROOT -> live runtime ${DRY:+(DRY RUN)}"
if [ -n "$(git -C "$REPO_ROOT" status --porcelain -- mem0-server scripts/wsl systemd 2>/dev/null)" ]; then
    echo "  WARN: deploying a DIRTY working tree (uncommitted changes in the deployed surface)"
fi

# --- 0. PRE-FLIGHT: launch-path parity (audit 2026-08-07: AMS-02/AMS-03) ---
# This script brands itself THE single deploy path, but it writes only the WSL copies.
# The MCP tool surface Claude Code actually launches is the WINDOWS copy under the
# operator's .claude/scripts, refreshed exclusively by install/2-windows-config.ps1.
# Deploying the WSL side and printing "deploy complete" is how two shipped fixes (503
# write-queueing, deep memory_health) stayed dead in production for 12 days while the
# parity check agreed everything matched.
#
# PRE-FLIGHT on purpose: an earlier revision ran this AFTER step 2, so a stale launch
# path aborted mid-deploy -- new server modules already rsynced onto disk, but the
# import smoke, restart and health gate skipped. The next reboot would then activate
# ungated bytes: the exact divergence class this script exists to prevent. Nothing is
# written before this point, so aborting here is clean.
#
# This block deliberately does NOT deploy the Windows-side files (that is the
# installer's job, on the Windows side); it refuses to let a half-shipped fix wave
# look like a completed one, and names the command that fixes it.
LAUNCH_SCRIPTS="mem0-mcp-shim.py l10-audit.py replay-ops.py"
launch_stale=""
launch_checked=""
launch_dir=""

# `set -e` + a failing command substitution aborts the script, and `2>/dev/null` would
# eat the only diagnostic: on a headless / interop-less WSL session (no WSL_INTEROP, an
# sshd-spawned shell) or a plain Linux host, cmd.exe is not invocable and this deploy
# previously died silently right here. `|| true` keeps the skip branch reachable.
win_home="$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r' || true)"
if [ -n "$win_home" ]; then
    launch_dir="$(wslpath "$win_home" 2>/dev/null || true)/.claude/scripts"
fi

if [ -z "$win_home" ] || [ ! -d "$launch_dir" ]; then
    echo "    launch-path: SKIPPED (no Windows side reachable from this shell)"
else
    for f in $LAUNCH_SCRIPTS; do
        src="$REPO_ROOT/scripts/wsl/$f"
        dst="$launch_dir/$f"
        if [ ! -f "$src" ]; then
            # A trio file absent from the repo is itself a defect: claiming parity over a
            # file that was never compared is how this class hides.
            launch_stale="$launch_stale $f(NO-REPO-COPY)"
            continue
        fi
        launch_checked="$launch_checked $f"
        if [ ! -f "$dst" ]; then
            launch_stale="$launch_stale $f(MISSING)"
        elif ! diff -q <(sed -e "s|__WSL_USER__|$WSL_USER|g" \
                             -e "s|__WIN_USER__|$WIN_USER|g" \
                             -e "s|__WSL_DISTRO__|$DISTRO|g" "$src") "$dst" >/dev/null 2>&1; then
            launch_stale="$launch_stale $f(STALE)"
        fi
    done

    if [ -n "$launch_stale" ]; then
        echo "==> LAUNCH-PATH STALE:$launch_stale" >&2
        echo "    The MCP tool surface launches the WINDOWS copies, not the WSL ones this" >&2
        echo "    script writes. Fix from Windows PowerShell, then restart the session:" >&2
        echo "      pwsh -NoProfile -File install/2-windows-config.ps1" >&2
        if [ -n "$DRY" ]; then
            # Dry-run's contract is report-only: warn, then continue so the operator still
            # sees everything else that would change.
            echo "    (dry run: continuing so the rest of the report is still produced)" >&2
        else
            echo "==> DEPLOY ABORTED before writing anything." >&2
            exit 3
        fi
    else
        # Name only what was actually compared -- never claim verification not performed.
        echo "    launch-path parity OK ($launch_checked )"
    fi
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

# --- 4b. AMS-09b (W5 review F2): idempotent durable fastembed-cache seed ---
# The unit pins FASTEMBED_CACHE_PATH to a reboot-surviving dir and runs
# HF_HUB_OFFLINE=1, so the SERVER can never download the BM25 model itself —
# this seed is the only thing standing between the next reboot and the leg's
# third death. No-op (~1s) when the dir is already populated. Runs BEFORE the
# restart so the fresh process loads from the durable dir on first encode.
FASTEMBED_CACHE="$HOME/.cache/fastembed"
mkdir -p "$FASTEMBED_CACHE"
if ! FASTEMBED_CACHE_PATH="$FASTEMBED_CACHE" "$APP_DIR/.venv/bin/python" -c \
    "from fastembed import SparseTextEmbedding; SparseTextEmbedding(model_name='Qdrant/bm25')" >/dev/null 2>&1; then
    echo "==> FASTEMBED SEED FAILED — durable cache at $FASTEMBED_CACHE could not be populated."
    echo "    The next reboot would kill the BM25 leg (the offline server cannot re-download)."
    echo "    Check network reachability, then re-run. Deploy ABORTED before restart."
    exit 1
fi
echo "    fastembed durable cache seeded OK ($FASTEMBED_CACHE)"

# --- 5. restart + health gate ---
systemctl --user restart mem0.service
for i in $(seq 1 30); do
    curl -sf http://127.0.0.1:18791/health >/dev/null 2>&1 && break
    sleep 1
    [ "$i" = 30 ] && { echo "==> HEALTH GATE FAILED after 30s — check: journalctl --user -u mem0.service -n 50"; exit 1; }
done
# W4 (review F11): --max-time 60. This gate runs SECONDS after a restart, when the
# CPU embedder/BM25 encoder may still be cold, and /health/deep has no server-side
# deadline of its own — an unbounded curl here means one slow check hangs the deploy
# indefinitely with no output. 60s is ~2x the verifier's own 30s budget for the same
# endpoint, so a merely-cold box still passes; a wedged one now ABORTS the deploy
# (curl exits non-zero and `set -o pipefail` propagates it) instead of hanging forever.
# (It is also why no ACTIVE reranker probe may ever be added to /health/deep.)
curl -sf --max-time 60 http://127.0.0.1:18791/health/deep | python3 -c "
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
