#!/bin/bash
# 1-wsl-services.sh - install WSL services for the agentic memory stack.
# Idempotent: safe to re-run for upgrades/repair.
#
# Args:
#   $1 = WSL username (e.g., output of `whoami` on the WSL side)
#   $2 = Windows username (e.g., $env:USERNAME on the Windows side)
#
# Installs:
#   - Qdrant 1.18.2 binary at ~/qdrant-server/ + systemd-user service
#   - mem0 v2.0.4 FastAPI wrapper at ~/apps/mem0-server/ + systemd-user service
#   - l10-audit Python timer (heuristic memory audit)
#   - EmbeddingGemma-300m embedder GGUF staged to ~/models/ (served on llama-swap
#     :11436; v0.22 migration — replaces the decommissioned Ollama+nomic embedder)
#   - llama-swap binary + bge-reranker-v2-m3 model (optional reranker)

set -eo pipefail
# v1.0 Phase 7B: verify-as-you-go — fail fast with an ACTIONABLE message naming the
# step + line, and remind that a re-run is safe (the installer is idempotent: every
# expensive/external step is guarded by an existence/post-state check — the Qdrant
# binary, the mem0 venv, the ~334MB EmbeddingGemma GGUF, the keys, the systemd units,
# and the hook registrations all skip when already done, so resume == re-run).
trap 'rc=$?; echo "FAILED at line $LINENO (exit $rc): $BASH_COMMAND" >&2; echo "  Fix the issue above and re-run install.ps1 — completed steps are skipped." >&2' ERR
WSL_USER="${1:-$(whoami)}"
WIN_USER="${2:-}"
# v1.0 Phase 7A: operator-supplied distro (arg $3, passed by install.ps1's
# detection). WSL_DISTRO_NAME is set inside the live WSL session; fall back to it,
# then to the os-release ID, then to the conventional default. Captured here
# BEFORE any sudo -i (login shells wipe WSL_DISTRO_NAME).
DISTRO="${3:-${WSL_DISTRO_NAME:-$(. /etc/os-release 2>/dev/null; echo "${ID:-Ubuntu}")}}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -z "$WIN_USER" ]; then
    # Try to derive from cmd.exe
    WIN_USER="$(cmd.exe /c 'echo %USERNAME%' 2>/dev/null | tr -d '\r\n ')"
fi
if [ -z "$WIN_USER" ]; then
    echo "FAIL: could not determine Windows username. Pass it as arg \$2."
    exit 1
fi

# Resolve actual home for the install user (in case script is run as someone else)
USER_HOME="/home/${WSL_USER}"
if [ ! -d "$USER_HOME" ]; then USER_HOME="$HOME"; fi

echo "==> WSL install starting"
echo "    WSL user:  $WSL_USER"
echo "    Win user:  $WIN_USER"
echo "    Home:      $USER_HOME"
echo "    Repo root: $REPO_ROOT"

# Enable linger so systemd-user services survive WSL session exits.
# v1.0 Phase 7A: non-interactive-safe. A bare `sudo loginctl enable-linger` HANGS
# forever when run non-interactively without passwordless sudo (sudo blocks on the
# password prompt with no tty). Check the current state first (no sudo needed) and
# skip when already enabled; otherwise use `sudo -n` (fail fast, never prompt).
echo "==> Enabling linger for $WSL_USER (so user services survive across sessions)"
if [ "$(loginctl show-user "$WSL_USER" --property=Linger 2>/dev/null)" = "Linger=yes" ]; then
    echo "  (linger already enabled)"
else
    sudo -n loginctl enable-linger "$WSL_USER" 2>/dev/null \
        && echo "  linger enabled" \
        || echo "  WARN: could not enable linger non-interactively — run once manually: sudo loginctl enable-linger $WSL_USER"
fi

# ----------------------------------------------------------------------
# 1. Qdrant
# ----------------------------------------------------------------------
QDRANT_VERSION=1.18.2
QDRANT_DIR="$USER_HOME/qdrant-server"
if [ ! -x "$QDRANT_DIR/qdrant" ]; then
    echo "==> Installing Qdrant $QDRANT_VERSION"
    mkdir -p "$QDRANT_DIR"
    cd /tmp
    curl -fsSL -o qdrant.tar.gz "https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VERSION}/qdrant-x86_64-unknown-linux-gnu.tar.gz"
    tar -xzf qdrant.tar.gz -C "$QDRANT_DIR"
    rm qdrant.tar.gz
    chmod +x "$QDRANT_DIR/qdrant"
    echo "  Qdrant binary installed at $QDRANT_DIR"
else
    echo "==> Qdrant binary already at $QDRANT_DIR (skipping download)"
fi

# ALWAYS write/refresh the config (idempotent, fixes the audit finding that
# config-on-first-install-only left existing installs without a config file and
# Qdrant defaulted to 0.0.0.0 - LAN-exposed - per audit 2026-06-08)
mkdir -p "$QDRANT_DIR/config" "$QDRANT_DIR/storage" "$QDRANT_DIR/snapshots"
if [ -f "$QDRANT_DIR/config/config.yaml" ]; then
    cp "$QDRANT_DIR/config/config.yaml" "$QDRANT_DIR/config/config.yaml.bak-$(date +%s)"
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
echo "  Qdrant config refreshed (loopback bind enforced)"

# ----------------------------------------------------------------------
# 2. mem0 server (FastAPI wrapper)
# ----------------------------------------------------------------------
MEM0_DIR="$USER_HOME/apps/mem0-server"
# Every module app.py imports must be deployed (fix-pass: the old app.py+config.py
# pair crash-looped fresh installs on ModuleNotFoundError for the newer modules).
MEM0_MODULES="app.py config.py reranker.py admission_gate.py episodic.py canonical_key_provider.py hook_contract.py security_invariants.py freshness.py codex_shim_client.py nli_write_gate.py episode_embeddings.py egemma_embedder.py imperative_canary.py"
if [ ! -d "$MEM0_DIR/.venv" ]; then
    echo "==> Setting up mem0 server at $MEM0_DIR"
    mkdir -p "$MEM0_DIR"
    for mod in $MEM0_MODULES; do
        cp "$REPO_ROOT/mem0-server/$mod" "$MEM0_DIR/$mod"
    done
    cd "$MEM0_DIR"
    python3 -m venv .venv
    ./.venv/bin/pip install --quiet --upgrade pip
    # v0.22: dropped the `ollama` python dep — mem0's embedder is EmbeddingGemma on
    # llama-swap :11436 (OpenAI-compatible transport via egemma_embedder.py); Ollama
    # is decommissioned from mem0's path.
    # v0.29.1: explicit security floors so fresh installs get the CVE-remediated
    # transitive deps — starlette>=1.3.1 (CVE-2026-54282/54283, FastAPI request path),
    # cryptography>=48.0.1,<49 (GHSA-537c-gmf6-5ccf; capped <49 so a fresh install
    # reproduces the as-tested 48.x major — bump the cap via UPGRADE.md for 49.x).
    # Both deps come in transitively otherwise.
    ./.venv/bin/pip install --quiet 'mem0ai[nlp]==2.0.4' fastapi uvicorn[standard] httpx pydantic 'starlette>=1.3.1' 'cryptography>=48.0.1,<49'
    echo "  mem0 venv ready"
else
    echo "==> mem0 venv exists at $MEM0_DIR/.venv (refreshing source files)"
    for mod in $MEM0_MODULES; do
        cp "$REPO_ROOT/mem0-server/$mod" "$MEM0_DIR/$mod"
    done
    # v0.29.1: enforce the security floors on existing installs too (idempotent —
    # a no-op when already satisfied). Without this, a re-run only refreshes code
    # and an existing venv stays on a CVE-vulnerable starlette/cryptography.
    # The pip step is fail-soft (a transient offline blip must not abort a code
    # refresh of an already-patched venv), but the post-condition assertion below
    # HARD-FAILS so the installer can never report success with a vulnerable venv.
    "$MEM0_DIR/.venv/bin/pip" install --quiet 'starlette>=1.3.1' 'cryptography>=48.0.1,<49' || \
        echo "  WARN: pip could not reach an index (offline?) — verifying existing versions…"
    "$MEM0_DIR/.venv/bin/python" - <<'PYEOF' || { echo "  FATAL: security floors not satisfied (need starlette>=1.3.1, cryptography>=48.0.1) — re-run with network access to remediate."; exit 1; }
import sys
from importlib.metadata import version
from packaging.version import Version as V
ok = V(version("starlette")) >= V("1.3.1") and V(version("cryptography")) >= V("48.0.1")
sys.exit(0 if ok else 1)
PYEOF
    echo "  security floors satisfied (starlette>=1.3.1, cryptography>=48.0.1,<49)"
fi

# v0.19 Phase H: deploy the DPAPI key-fetch script next to the app modules.
# mem0.service runs it via ExecStartPre=- (fail-soft). tr strips CRLF since the
# repo may live on a Windows drive (per docs/modular/dpapi-canonical-key.md).
tr -d "\r" < "$REPO_ROOT/scripts/wsl/dpapi-fetch-key.sh" > "$MEM0_DIR/dpapi-fetch-key.sh" && chmod +x "$MEM0_DIR/dpapi-fetch-key.sh"
echo "  dpapi-fetch-key.sh deployed (Phase H key chain)"

# Generate mem0 API key if not present
KEY_FILE="$USER_HOME/.mem0/api-key"
if [ ! -f "$KEY_FILE" ]; then
    echo "==> Generating mem0 API key"
    mkdir -p "$USER_HOME/.mem0"
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
    echo "  Key written to $KEY_FILE (mode 600)"
else
    echo "==> mem0 API key already exists (keeping)"
fi

# v1.0 Phase 7A: WSL-side operator receipt. WSL scripts source this to resolve
# the operator-chosen repo location + distro + users without hardcoding any
# developer path/handle (mirrors the Windows mem0-stack.config.psd1). LF, no BOM.
mkdir -p "$USER_HOME/.mem0"
REPO_ROOT_WSL="$REPO_ROOT"
cat > "$USER_HOME/.mem0/stack.env" <<ENV
MEM0_WSL_USER=$WSL_USER
MEM0_WIN_USER=$WIN_USER
MEM0_DISTRO=$DISTRO
MEM0_REPO_ROOT_WSL=$REPO_ROOT_WSL
ENV
echo "  stack.env written ($USER_HOME/.mem0/stack.env): user=$WSL_USER distro=$DISTRO"

# Generate canonical-key if not present (v0.14 B: HMAC auth for tier=canonical promotions).
# Fix-pass guard: a DPAPI-backed box has NO plaintext canonical-key — only the
# .dpapi blob (v0.19 Phase H cutover). Generating a fresh plaintext key there
# would silently rotate the key out from under the DPAPI blob AND regress the
# at-rest posture, so the generator runs only when NEITHER form exists.
CANON_KEY_FILE="$USER_HOME/.mem0/canonical-key"
if [ ! -f "$CANON_KEY_FILE" ] && [ ! -f "$CANON_KEY_FILE.dpapi" ]; then
    echo "==> Generating canonical-key (required for tier=canonical promotions)"
    bash "$REPO_ROOT/scripts/wsl/generate-canonical-key.sh"
    echo "  canonical-key written to $CANON_KEY_FILE (mode 600)"
else
    echo "==> canonical-key present (plaintext or DPAPI blob) — keeping"
fi

# ----------------------------------------------------------------------
# 3. Embedder model — EmbeddingGemma-300m on llama-swap :11436
# ----------------------------------------------------------------------
# v0.22 EmbeddingGemma migration (2026-06-13): mem0's embedder is multilingual
# EmbeddingGemma-300m served on llama.cpp/llama-swap :11436 (CPU, OpenAI-compatible),
# NOT Ollama+nomic (decommissioned — nomic is English-only, a defect for the EN/ES
# corpus). This stage stages the GGUF to a STABLE flat path; the llama-swap MODEL
# ENTRY itself lives in the out-of-repo llama-swap config (same as bge-reranker — see
# SKILL.md "llama-swap.yaml" manual step). We provision the model file + verify the
# endpoint; we do NOT silently rewrite the user's llama-swap yaml.
EGEMMA_GGUF="$USER_HOME/models/embeddinggemma-300M-Q8_0.gguf"
EGEMMA_HF_REPO="ggml-org/embeddinggemma-300M-GGUF"
EGEMMA_HF_FILE="embeddinggemma-300M-Q8_0.gguf"
mkdir -p "$USER_HOME/models"
if [ ! -f "$EGEMMA_GGUF" ]; then
    echo "==> Fetching EmbeddingGemma-300m GGUF (~334MB, one-time) to $EGEMMA_GGUF"
    FETCHED=""
    # Prefer huggingface-cli if present; else curl the resolve URL directly.
    if command -v huggingface-cli >/dev/null 2>&1; then
        if huggingface-cli download "$EGEMMA_HF_REPO" "$EGEMMA_HF_FILE" \
            --local-dir "$USER_HOME/models" >/dev/null 2>&1 \
            && [ -f "$USER_HOME/models/$EGEMMA_HF_FILE" ]; then
            # huggingface-cli may nest under the repo path; normalize to the flat path
            [ -f "$EGEMMA_GGUF" ] || mv "$USER_HOME/models/$EGEMMA_HF_FILE" "$EGEMMA_GGUF" 2>/dev/null || true
            FETCHED=1
        fi
    fi
    if [ -z "$FETCHED" ]; then
        curl -fsSL -o "$EGEMMA_GGUF" \
            "https://huggingface.co/$EGEMMA_HF_REPO/resolve/main/$EGEMMA_HF_FILE" \
            && FETCHED=1
    fi
    if [ -z "$FETCHED" ] || [ ! -s "$EGEMMA_GGUF" ]; then
        echo "  WARN: could not fetch the EmbeddingGemma GGUF automatically."
        echo "        Download $EGEMMA_HF_FILE from $EGEMMA_HF_REPO to $EGEMMA_GGUF manually."
        rm -f "$EGEMMA_GGUF" 2>/dev/null || true
    else
        echo "  EmbeddingGemma GGUF staged at $EGEMMA_GGUF"
    fi
else
    echo "==> EmbeddingGemma GGUF already present at $EGEMMA_GGUF (skipping download)"
fi

# Verify the embedder endpoint is actually serving egemma (the llama-swap model
# entry is the out-of-repo manual step in SKILL.md). A clean install that hasn't
# added the entry yet WARNs here with the exact stanza to add — it does NOT install
# or depend on Ollama.
if curl -sf -m 20 -X POST http://127.0.0.1:11436/v1/embeddings \
     -H 'Content-Type: application/json' \
     -d '{"model":"embeddinggemma","input":"title: none | text: ping"}' \
     | grep -q '"embedding"'; then
    echo "  EmbeddingGemma reachable on :11436 (768-dim embedder OK)"
else
    echo "  WARN: EmbeddingGemma not reachable on :11436. Add this model entry to your"
    echo "        llama-swap config (always_loaded group) and restart llama-swap —"
    echo "        see SKILL.md 'llama-swap.yaml' section:"
    echo "          embeddinggemma:"
    echo "            cmd: <llama.cpp>/llama-server --model $EGEMMA_GGUF \\"
    echo "                 --embeddings --pooling mean --n-gpu-layers 0 \\"
    echo "                 --ctx-size 2048 --batch-size 2048 --ubatch-size 2048 \\"
    echo "                 --port \${PORT} --host 127.0.0.1"
    echo "            ttl: 0"
    echo "            aliases: [\"embeddinggemma\", \"egemma\", \"embeddinggemma-300m\"]"
fi

# ----------------------------------------------------------------------
# 4. systemd-user units
# ----------------------------------------------------------------------
echo "==> Installing systemd-user units"
SYSTEMD_USER_DIR="$USER_HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

# v0.22 M5: egemma-rollback-prune.{service,timer} are now version-controlled and
# deployed here (previously hand-placed on the live box, outside R9/parity). They are
# DEPLOYED but the timer is NOT auto-enabled — it is a one-shot tied to the v0.22
# migration window (fires 2026-06-21) and a fresh install starts directly on the
# mem0_egemma_768 collection with no old `memories` rollback anchor to prune.
for unit in mem0.service qdrant.service l10-audit.service l10-audit.timer decay-scan.service decay-scan.timer stack-backup.service stack-backup.timer goals-stale-sweep.service goals-stale-sweep.timer contradiction-sweep.service contradiction-sweep.timer episodic-reconcile.service episodic-reconcile.timer egemma-rollback-prune.service egemma-rollback-prune.timer; do
    SRC="$REPO_ROOT/systemd/$unit"
    DST="$SYSTEMD_USER_DIR/$unit"
    if [ ! -f "$SRC" ]; then echo "  WARN: $SRC not found, skipping"; continue; fi
    # v1.0 Phase 7A/7B: resolve the operator sentinels (all units are sentinelized,
    # so this ships PII-free and resolves at install time). The legacy raw-handle
    # rules were removed in 7B once every unit used sentinels.
    sed \
        -e "s|__WSL_USER__|$WSL_USER|g" \
        -e "s|__WIN_USER__|$WIN_USER|g" \
        -e "s|__WSL_DISTRO__|$DISTRO|g" \
        -e "s|__REPO_ROOT_WSL__|$REPO_ROOT|g" \
        "$SRC" > "$DST"
    echo "  installed: $unit"
done

# Deploy the rollback-prune script the unit's ExecStart points at (~/.mem0/), CRLF-
# stripped (repo may live on a Windows drive). Deployed, not armed: the timer above
# is intentionally left disabled (one-shot migration cleanup; see SKILL.md rollback).
tr -d "\r" < "$REPO_ROOT/scripts/wsl/egemma-rollback-prune.sh" > "$USER_HOME/.mem0/egemma-rollback-prune.sh" \
    && chmod +x "$USER_HOME/.mem0/egemma-rollback-prune.sh" \
    && echo "  egemma-rollback-prune.sh deployed (timer NOT auto-enabled — one-shot rollback-window cleanup)"

# Place l10-audit.py where the systemd unit expects it (also at the WSL-script path
# referenced by the systemd ExecStart line: /mnt/c/Users/<winuser>/.claude/scripts/)
cp "$REPO_ROOT/scripts/wsl/l10-audit.py" "$MEM0_DIR/l10-audit.py" 2>/dev/null || true

# Reload + enable
systemctl --user daemon-reload
systemctl --user enable --now qdrant.service
systemctl --user enable --now mem0.service
systemctl --user enable --now l10-audit.timer
systemctl --user enable --now decay-scan.timer
systemctl --user enable --now stack-backup.timer

# v0.22 Phase G: auto-enable the weekly hygiene sweep timers (previously a
# manual step). enable --now is idempotent (safe to re-run). Fail-soft: if
# systemd-user is unavailable the warning is printed but the install does not
# abort. Only the TIMERS are enabled — they run the sweeps in their SAFE
# defaults: goals-stale-sweep stays REPORT-ONLY (its ExecStart passes no
# --auto-abandon, so no destructive goal-status flips on the unattended
# schedule); contradiction-sweep runs its normal judge-stamp mode.
systemctl --user enable --now goals-stale-sweep.timer contradiction-sweep.timer episodic-reconcile.timer \
    || echo "  WARN: could not enable sweep timers (systemd-user unavailable?) — enable manually: systemctl --user enable --now goals-stale-sweep.timer contradiction-sweep.timer episodic-reconcile.timer"

sleep 3
echo "==> Service status:"
systemctl --user is-active qdrant.service mem0.service l10-audit.timer decay-scan.timer stack-backup.timer 2>&1 | sed 's/^/  /'

# ----------------------------------------------------------------------
# 6. Health probes
# ----------------------------------------------------------------------
echo "==> Health probes"
for endpoint in "Qdrant http://127.0.0.1:6333/healthz" "mem0 http://127.0.0.1:18791/health"; do
    name="${endpoint%% *}"
    url="${endpoint#* }"
    if curl -fs -m 5 "$url" >/dev/null 2>&1; then
        echo "  $name: OK ($url)"
    else
        echo "  $name: NOT REACHABLE ($url) - check 'systemctl --user status $(echo $name | tr A-Z a-z).service'"
    fi
done

echo ""
echo "==> WSL services install complete."
echo "    Next: phase 2 (Windows config) registers Claude Code hooks + Task Scheduler."
