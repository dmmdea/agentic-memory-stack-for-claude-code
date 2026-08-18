#!/usr/bin/env bash
# Key-material backup — the half memory-backup.sh does NOT cover.
#
# WHY THIS EXISTS
#   memory-backup.sh copies every collection and SQLite db OFF the source disk, so the
#   CORPUS survives a disk death. The keys did not. Restoring that backup onto fresh
#   hardware would have given you every memory and no ability to write a canonical fact,
#   because the HMAC signing key that authorises canonical promotion existed in exactly
#   one place: ~/.mem0/canonical-key on the disk that just died.
#
#   The DPAPI blob (canonical-key.dpapi) is NOT a backup. It is encrypted against the
#   Windows user profile on this machine, so it is worthless on new hardware, after a
#   profile rebuild, or after an account change - precisely the cases a backup is for.
#
# OPERATOR RULE THIS IMPLEMENTS
#   Keys and secrets get saved durably so that no single mistake - by the operator or by
#   an agent - can make them unrecoverable. Nothing here is ever the only copy.
#
# WHAT IT BACKS UP
#   ~/.mem0/canonical-key   HMAC signing key for canonical promotion (irreplaceable:
#                           losing it does not just block writes, it invalidates the
#                           audit chain that existing canonical records were signed under)
#   ~/.mem0/api-key         mem0 server API key (re-issuable, but everything is wired to it)
#   ~/.mem0/authority-url   which memory server this box talks to
#   ~/.claude.json          NVIDIA_API_KEY lives here in plaintext (extracted, not the
#                           whole file - that file is huge and mostly not secret)
#
# WHAT IT DELIBERATELY DOES NOT
#   GitHub tokens (OS keyring, re-issuable via `gh auth login`) and the Codex OAuth blob
#   (re-issuable by re-authenticating). Backing up re-issuable credentials widens the
#   blast radius for no recovery benefit.
#
# DESTINATIONS (both, always - one disk is not durability)
#   1. V:/mem0-backups/keys   off the source disk, same machine, fast local recovery
#   2. the Drive keys folder  offsite, survives the machine entirely
#
# Usage:  key-backup.sh [--verify-only]
set -euo pipefail

# MEM0_DIR lets this run from the Windows-side shell, which is the only runtime that
# sees BOTH destinations (WSL has no /mnt/g on this estate). Point it at the WSL
# share: //wsl.localhost/<distro>/home/<user>/.mem0
MEM0="${MEM0_DIR:-$HOME/.mem0}"
STAMP="$(date +%Y-%m-%d-%H%M)"
LOCAL_DEST="${KEY_BACKUP_LOCAL:-/v/mem0-backups/keys}"
OFFSITE_DEST="${KEY_BACKUP_OFFSITE:-/g/My Drive/AI Ecosystem/Ecosystem/Keys/ams}"
# Windows-side equivalents, used only when the offsite volume is not mounted in WSL.
OFFSITE_WIN_DEST="${OFFSITE_WIN_DEST:-G:\My Drive\AI Ecosystem\Ecosystem\Keys\ams}"
OFFSITE_WIN_VERIFY="${OFFSITE_WIN_VERIFY:-}"
WIN_CLAUDE_JSON="${WIN_CLAUDE_JSON:-/c/Users/$(. "$MEM0/stack.env" 2>/dev/null; echo "${MEM0_WIN_USER:-$USER}")/.claude.json}"

log() { printf '%s\n' "$*"; }

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
bundle="$stage/ams-keys-$STAMP"
mkdir -p "$bundle"

# --- collect ------------------------------------------------------------------
n=0
for f in canonical-key api-key authority-url; do
    if [ -f "$MEM0/$f" ]; then
        cp -p "$MEM0/$f" "$bundle/$f"
        n=$((n+1))
    else
        log "  WARN: $MEM0/$f absent - not backed up"
    fi
done

# NVIDIA key out of the Claude registry (the file itself is 60KB of mostly non-secret).
if [ -f "$WIN_CLAUDE_JSON" ]; then
    # python3 under WSL, python under Git Bash - pick whichever exists, or the key is
    # silently skipped and the bundle looks complete while missing an artifact.
    # Do not trust `command -v`: on Windows it resolves the Microsoft Store alias stub
    # for python3, which exits with an install advert instead of running anything. Probe
    # each candidate by actually executing it.
    PY_BIN=""
    for c in python3 python py; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c "pass" >/dev/null 2>&1; then
            PY_BIN="$c"; break
        fi
    done
    if [ -z "$PY_BIN" ]; then
        log "  WARN: no python on PATH - NVIDIA key not extracted"
    else
    "$PY_BIN" - "$WIN_CLAUDE_JSON" "$bundle/nvidia-api-key" <<'PY' || log "  WARN: NVIDIA key not extracted"
import json, sys
src, dst = sys.argv[1], sys.argv[2]
d = json.load(open(src, encoding="utf-8"))
v = ((d.get("mcpServers") or {}).get("local-offload") or {}).get("env", {}).get("NVIDIA_API_KEY")
if not v:
    raise SystemExit(1)
open(dst, "w", encoding="utf-8").write(v)
PY
    fi
    [ -f "$bundle/nvidia-api-key" ] && n=$((n+1))
fi

[ "$n" -gt 0 ] || { log "ERROR: nothing collected - refusing to write an empty bundle"; exit 2; }

# --- manifest (a restore must be verifiable, not hopeful) ----------------------
( cd "$bundle" && sha256sum * > MANIFEST.sha256 )
cat > "$bundle/README.txt" <<'TXT'
AMS key material. Restore by copying these files back to ~/.mem0/ inside the WSL distro
and `chmod 600` them, then restart mem0.service. The canonical-key MUST be restored
before any canonical promotion will work; the .dpapi blob is machine-bound and is NOT a
substitute for this bundle on new hardware.
Verify first:  sha256sum -c MANIFEST.sha256
TXT
chmod 700 "$bundle"; chmod 600 "$bundle"/*

# --- publish to both destinations ---------------------------------------------
rc=0
# The offsite volume is typically a Windows drive (Drive/OneDrive). Those are often NOT
# mounted inside WSL - G: and P: are not, on this estate - so a naive `cp` silently loses
# the offsite half and leaves exactly one copy. Fall back to a Windows-side copy instead
# of accepting the gap.
win_copy() {   # win_copy <linux-src-dir> <win-dest-dir>
    local src_win dst_win
    src_win="$(wslpath -w "$1" 2>/dev/null)" || return 1
    dst_win="$2"
    powershell.exe -NoProfile -Command         "New-Item -ItemType Directory -Force -Path '$dst_win' | Out-Null;          Copy-Item -Recurse -Force -Path '$src_win' -Destination '$dst_win'" >/dev/null 2>&1
}

for dest in "$LOCAL_DEST" "$OFFSITE_DEST"; do
    if mkdir -p "$dest" 2>/dev/null && cp -r "$bundle" "$dest/" 2>/dev/null; then
        log "  wrote $dest/$(basename "$bundle")"
    elif [ -n "${OFFSITE_WIN_DEST:-}" ] && [ "$dest" = "$OFFSITE_DEST" ]          && win_copy "$bundle" "$OFFSITE_WIN_DEST"; then
        log "  wrote (via Windows) $OFFSITE_WIN_DEST\$(basename "$bundle")"
    else
        log "  ERROR: $dest unreachable - NOT backed up there"; rc=1
    fi
done

# --- verify what actually landed, by re-reading it -----------------------------
verified=0
for dest in "$LOCAL_DEST" "$OFFSITE_DEST"; do
    d="$dest/$(basename "$bundle")"
    if [ ! -d "$d" ] && [ "$dest" = "$OFFSITE_DEST" ] && [ -n "${OFFSITE_WIN_VERIFY:-}" ]; then
        d="$OFFSITE_WIN_VERIFY/$(basename "$bundle")"
    fi
    [ -d "$d" ] || continue
    if ( cd "$d" && sha256sum -c MANIFEST.sha256 >/dev/null 2>&1 ); then
        log "  VERIFIED $d"; verified=$((verified+1))
    else
        log "  ERROR: checksum mismatch at $d"; rc=1
    fi
done

if [ "$verified" -lt 2 ]; then
    log "FAIL: key material is durable only when it exists in BOTH destinations ($verified/2)."
    exit 3
fi
log "key-backup: $n artifact(s), verified in $verified destinations."
exit $rc
