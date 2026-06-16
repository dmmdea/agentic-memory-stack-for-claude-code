#!/usr/bin/env bash
# Generate ~/.mem0/canonical-key if absent. Mode 600.
# Idempotent: exits 0 if key already exists.
set -euo pipefail
KEY_FILE="$HOME/.mem0/canonical-key"
if [ -f "$KEY_FILE" ]; then
  echo "canonical-key exists at $KEY_FILE (mode $(stat -c %a "$KEY_FILE"))"
  exit 0
fi
# v0.20 Phase D (M9): blob guard — on a DPAPI box (v0.19 Phase H) generating a
# fresh plaintext key would silently diverge from the canonical-key.dpapi blob
# (key split-brain: new HMAC key here, old key inside the blob the server fetches).
if [ -f "$HOME/.mem0/canonical-key.dpapi" ] && [ "${1:-}" != "--force" ]; then
  echo "REFUSING: canonical-key.dpapi exists - generating a new key would diverge from the blob." >&2
  echo "Recover the existing key instead: docs/modular/dpapi-canonical-key.md section Recovery" >&2
  echo "(ProtectedData::Unprotect restore), or restart mem0 to re-run dpapi-fetch-key.sh." >&2
  echo "Use --force only for deliberate rotation (then re-run dpapi-store-canonical-key.ps1)." >&2
  exit 1
fi
mkdir -p "$HOME/.mem0"
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$KEY_FILE"
chmod 600 "$KEY_FILE"
echo "generated $KEY_FILE (mode 600)"
