#!/usr/bin/env bash
# ensure-codex-shim.sh — bring the Windows codex-shim (:18792) up from WSL, fail-soft.
#
# The shim is spawned by a Claude SessionStart hook with a 240-min idle shutdown, so
# any scheduled or detached consumer (the Sunday sweep legs, the SessionStart-detached
# rejudge) can find :18792 dark. The spawn script itself is idempotent (TCP probe +
# named singleton mutex + opt-out flag), so calling it when the shim is already up is
# a no-op. This wrapper exists so every consumer shares ONE correct invocation:
#   * absolute /mnt/c powershell path — systemd user units have no Windows PATH
#     entries (verified live 2026-08-24: bare `powershell.exe` resolves to nothing
#     inside a transient user unit while the absolute path executes fine);
#   * Windows user resolved AT RUNTIME from the stack.env receipt — scripts/wsl/ is
#     rsynced verbatim by deploy.sh (never sentinel-templated), so a baked-in
#     __WIN_USER__ placeholder would ship literally and fail invisibly, the exact
#     AMS-07 failure class;
#   * bounded health poll so callers get a truthful exit code.
#
# Exit 0: shim answers /health. Exit 1: could not bring it up (callers fail soft —
# a missing shim degrades their run exactly as before, it never crashes them).
set -u

PORT="${MEM0_CODEX_SHIM_PORT:-18792}"
POLL_SECONDS="${1:-90}"
PSEXE="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# Already up? (cheap, no interop needed)
if curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "codex-shim already listening on :${PORT}"
    exit 0
fi

# Resolve the Windows user from the installer's receipt (same pattern as
# stack-backup.sh). No receipt or no interop -> fail soft, never guess a user.
WIN_USER=""
if [ -f "$HOME/.mem0/stack.env" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.mem0/stack.env" 2>/dev/null || true
    WIN_USER="${MEM0_WIN_USER:-}"
fi
if [ -z "$WIN_USER" ]; then
    echo "ensure-codex-shim: MEM0_WIN_USER not in ~/.mem0/stack.env - cannot locate spawn script" >&2
    exit 1
fi
SPAWN="C:\\Users\\${WIN_USER}\\.claude\\scripts\\codex-shim-spawn.ps1"
if [ ! -x "$PSEXE" ]; then
    echo "ensure-codex-shim: Windows interop unavailable (${PSEXE} not executable)" >&2
    exit 1
fi

# Spawn (idempotent on the Windows side); the spawn script reads stdin, feed it EOF.
# Capture output — when the later health poll fails, the spawn's own words are the
# only clue WHY (missing venv, port conflict, wedged mutex holder).
spawn_out="$("$PSEXE" -NoProfile -ExecutionPolicy Bypass -File "$SPAWN" </dev/null 2>&1)" || {
    echo "ensure-codex-shim: spawn invocation failed (interop dead or script missing at ${SPAWN}): $(echo "$spawn_out" | head -1)" >&2
    exit 1
}

i=0
while [ "$i" -lt "$POLL_SECONDS" ]; do
    if curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo "codex-shim up on :${PORT} after ${i}s"
        exit 0
    fi
    sleep 1
    i=$((i + 1))
done
echo "ensure-codex-shim: shim did not answer /health within ${POLL_SECONDS}s (spawn said: $(echo "$spawn_out" | head -1))" >&2
exit 1
