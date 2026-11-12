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
#   ~/.mem0/canonical-key   HMAC key that signs the short-lived user-direct tokens authorising
#                           canonical/insight writes (no stored record is signed under it, so a
#                           fresh key loses no data; but until one is in place those writes are
#                           refused, and a backup saves minting and re-wiring a new one)
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
# DESTINATIONS (both, always - one disk is not durability; no default, you name them)
#   KEY_BACKUP_LOCAL    a directory on a different disk from ~/.mem0, same machine, fast
#                       local recovery
#   KEY_BACKUP_OFFSITE  a directory that survives the machine entirely (a synced cloud
#                       folder, a mounted remote share)
#   OFFSITE_WIN_DEST    optional: the Windows-side path of the offsite folder, used when
#                       that volume is not mounted inside WSL
#
# REFUSES A BUNDLE WITHOUT THE KEYS
#   The run fails (exit 4) and names what is missing when the canonical key or the API key was
#   not collected: a bundle of only authority-url used to pass as "1 artifact, verified in 2
#   destinations". --allow-partial backs up what exists anyway (the run says it is partial).
#   On a native Linux authority (stack.env MEM0_HOST_KIND=native) the keys are systemd credentials,
#   sealed to that host and not files, so this script cannot back them up at all: it says so and
#   points to docs/systems/key-custody.md instead of reporting a green run holding no key.
#
# Usage:  KEY_BACKUP_LOCAL=<dir> KEY_BACKUP_OFFSITE=<dir> key-backup.sh [--allow-partial]
set -euo pipefail

ALLOW_PARTIAL=0
for arg in "$@"; do
    case "$arg" in
        --allow-partial) ALLOW_PARTIAL=1 ;;
        *) echo "ERROR: unknown argument '$arg' (only --allow-partial is accepted)" >&2; exit 64 ;;
    esac
done

# MEM0_DIR lets this run from the Windows-side shell, which can see Windows drives that WSL
# may not mount. Point it at the WSL share: //wsl.localhost/<distro>/home/<user>/.mem0
MEM0="${MEM0_DIR:-$HOME/.mem0}"
STAMP="$(date +%Y-%m-%d-%H%M)"
LOCAL_DEST="${KEY_BACKUP_LOCAL:-}"
OFFSITE_DEST="${KEY_BACKUP_OFFSITE:-}"
# Windows-side equivalent of the offsite folder, used only when that volume is not mounted in WSL.
OFFSITE_WIN_DEST="${OFFSITE_WIN_DEST:-}"
if [ -z "$LOCAL_DEST" ] || [ -z "$OFFSITE_DEST" ]; then
    echo "ERROR: set KEY_BACKUP_LOCAL (a directory on another disk of this machine) and KEY_BACKUP_OFFSITE (a directory that survives the machine) before running; the backup has no default destinations." >&2
    exit 64
fi
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

# The two keys are what a bundle is for. Without --allow-partial a bundle missing either is a failure.
missing=""
[ -s "$bundle/canonical-key" ] || missing="canonical-key"
[ -s "$bundle/api-key" ] || missing="${missing:+$missing, }api-key"
PARTIAL_NOTE=""
if [ -n "$missing" ]; then
    host_kind="${MEM0_HOST_KIND:-}"
    [ -n "$host_kind" ] || host_kind="$(sed -n 's/^MEM0_HOST_KIND=//p' "$MEM0/stack.env" 2>/dev/null | head -n1 | tr -d "\r\"' " || true)"
    if [ "$host_kind" = "native" ]; then
        log "ERROR: not collected: $missing. This box is a native Linux authority (MEM0_HOST_KIND=native): its keys are systemd credentials (<secrets-dir>/ams-*.cred), not files in $MEM0. A sealed credential is bound to the host that made it, so this script cannot back it up and a copy of the .cred file is useless on another machine. See 'Native authority' in docs/systems/key-custody.md for the procedure."
    else
        log "ERROR: not collected: $missing (absent or empty under $MEM0). A bundle without the keys is not a key backup. Fix the path (MEM0_DIR), or pass --allow-partial to back up what exists anyway."
    fi
    if [ "$ALLOW_PARTIAL" -ne 1 ]; then
        log "FAIL: nothing was written to either destination."
        exit 4
    fi
    PARTIAL_NOTE=" PARTIAL (--allow-partial): missing $missing."
    log "  WARN:$PARTIAL_NOTE"
fi

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
# The offsite volume may be a Windows drive (a synced cloud folder). Those are often NOT
# mounted inside WSL, so a naive `cp` silently loses the offsite half and leaves exactly one
# copy. With OFFSITE_WIN_DEST set, fall back to a Windows-side copy instead of accepting the gap.
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
log "key-backup: $n artifact(s), verified in $verified destinations.$PARTIAL_NOTE"
exit $rc
