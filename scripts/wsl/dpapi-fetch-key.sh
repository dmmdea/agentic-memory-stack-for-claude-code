#!/usr/bin/env bash
# v0.19 Phase H: provision the canonical key into tmpfs at service start.
#
# DPAPI-decrypts ~/.mem0/canonical-key.dpapi via WSL->Windows PowerShell interop
# and writes the key to the systemd RuntimeDirectory (tmpfs, mode 600). Persistent
# disk holds ONLY the DPAPI blob; the plaintext key exists only in RAM-backed
# /run/user/<uid>/mem0/ while the service runs (systemd removes it on stop).
#
# Wired as `ExecStartPre=-` on mem0.service (non-blocking: if interop is down the
# server still starts; canonical mutations 503 loudly until key restored + restart,
# matching the v0.18 provider diagnostics). Bounded retry below.
#
# Interop note (PoC-verified 2026-06-12, WSL2 + systemd 255): systemd user units
# do NOT carry WSL_INTEROP, but the binfmt interpreter falls back to the boot
# socket /run/WSL/1_interop, so .exe launch works anyway. ensure_interop() also
# rescues the case where that fallback breaks by probing live /run/WSL sockets.
set -uo pipefail

BLOB="${MEM0_DPAPI_BLOB:-$HOME/.mem0/canonical-key.dpapi}"
OUT_DIR="${RUNTIME_DIRECTORY:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/mem0}"
OUT="$OUT_DIR/canonical-key"
# DPAPI_FETCH_PS: test seam (v0.20 Phase D L8) — the automated tests point this
# at a stubbed powershell.exe; production never sets it.
PS="${DPAPI_FETCH_PS:-/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe}"
RETRIES="${DPAPI_FETCH_RETRIES:-5}"
SLEEP_SECS="${DPAPI_FETCH_SLEEP:-3}"

log() { echo "dpapi-fetch-key: $*" >&2; }

if [ ! -f "$BLOB" ]; then
  log "FATAL: DPAPI blob not found at $BLOB - canonical key cannot be provisioned"
  exit 1
fi

# v0.20 Phase D (L7): no set -e in this script (the retry loop needs failing
# commands to be non-fatal) — so every write step is explicitly guarded.
mkdir -p "$OUT_DIR" && chmod 700 "$OUT_DIR" || { log "FATAL: cannot create $OUT_DIR - canonical key NOT provisioned"; exit 1; }

ensure_interop() {
  # Probe default interop path first (binfmt falls back to /run/WSL/1_interop
  # when WSL_INTEROP is unset). If dead, point WSL_INTEROP at each live socket.
  if "$PS" -NoProfile -NonInteractive -Command 'exit 0' >/dev/null 2>&1; then
    return 0
  fi
  local s
  for s in /run/WSL/*_interop; do
    [ -S "$s" ] || continue
    if WSL_INTEROP="$s" "$PS" -NoProfile -NonInteractive -Command 'exit 0' >/dev/null 2>&1; then
      export WSL_INTEROP="$s"
      log "interop recovered via $s"
      return 0
    fi
  done
  return 1
}

attempt=1
while :; do
  if ensure_interop; then
    # Key bytes travel blob-b64 -> powershell stdin -> decrypted-b64 stdout -> bash
    # variable -> tmpfs file. Never written to persistent disk.
    KEY_B64=$(base64 -w0 "$BLOB" | "$PS" -NoProfile -NonInteractive -Command \
      "Add-Type -AssemblyName System.Security; \$b=[Convert]::FromBase64String([Console]::In.ReadToEnd().Trim()); [Convert]::ToBase64String([System.Security.Cryptography.ProtectedData]::Unprotect(\$b, \$null, 'CurrentUser'))" \
      2>/dev/null) || KEY_B64=""
    KEY_B64=$(printf '%s' "$KEY_B64" | tr -d '\r\n')
    if [ -n "$KEY_B64" ]; then
      # v0.20 Phase D (L7+L8): checked decode + non-empty sanity gate. The old
      # unchecked `base64 -d > TMP` could install a truncated/empty key and
      # still log 'provisioned' + exit 0 (403 storm instead of the documented
      # 503-degraded). A failed decode now falls into the retry loop; a failed
      # tmpfs write (mktemp/chmod/mv: dir missing/full/perms) is FATAL — no
      # success log, no exit 0, no half-installed key, no tmp debris.
      umask 077
      TMP=$(mktemp "$OUT_DIR/.canonical-key.XXXXXX") \
        || { log "FATAL: mktemp failed in $OUT_DIR - canonical key NOT provisioned"; exit 1; }
      if printf '%s' "$KEY_B64" | base64 -d > "$TMP" && [ -s "$TMP" ]; then
        chmod 600 "$TMP" && mv -f "$TMP" "$OUT" \
          || { log "FATAL: tmpfs write to $OUT failed (dir missing/full/perms) - canonical key NOT provisioned"; rm -f "$TMP" 2>/dev/null; exit 1; }
        log "canonical key provisioned to $OUT (tmpfs, mode 600)"
        exit 0
      else
        rm -f "$TMP"
        log "attempt $attempt/$RETRIES: interop output was not valid base64 (decode failed or empty) - not installing"
      fi
    else
      log "attempt $attempt/$RETRIES: interop OK but DPAPI decrypt failed or empty"
    fi
  else
    log "attempt $attempt/$RETRIES: no working WSL interop socket"
  fi
  if [ "$attempt" -ge "$RETRIES" ]; then
    log "FATAL: canonical key NOT provisioned after $RETRIES attempts - mem0 will run degraded (canonical/insight mutations 503). Recover: from Windows run [System.Security.Cryptography.ProtectedData]::Unprotect on canonical-key.dpapi, restore plaintext ~/.mem0/canonical-key (mode 600), restart mem0."
    exit 1
  fi
  attempt=$((attempt+1))
  sleep "$SLEEP_SECS"
done
