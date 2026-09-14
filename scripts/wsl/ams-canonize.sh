#!/usr/bin/env bash
# ams-canonize.sh — the AUTHORITY-side executor for mem0-canonize.sh (v1.23 P2-8, spec §7 Y7).
#
# A PC's mem0-canonize.sh forwards its argv here over SSH (`bash ~/apps/mem0-scripts/ams-canonize.sh
# <argv…>`). This box must be the brain: the canonical HMAC key lives only here, so the token is
# minted here, at execution time — never on a replica, never ahead of time.
#   - role != brain          -> exit 3 "refusing" (the One-Brain Rule; a queued request that
#                               reaches a demoted box is never executed against it)
#   - native authority       -> run the real script inside a transient user unit that loads BOTH
#                               systemd credentials (the same LoadCredentialEncrypted lines the
#                               dream step uses) and points MEM0_URL at the tailnet bind
#   - WSL brain              -> the script finds its keys the usual way; run it directly
# MEM0_CANONIZE_NO_FORWARD=1 tells mem0-canonize.sh not to forward again (it is already here).
set -euo pipefail
ROLE="$(tr -d '[:space:]' < "$HOME/.mem0/role" 2>/dev/null || true)"
if [[ "${ROLE:-brain}" != "brain" ]]; then
  echo "refusing: role=${ROLE:-?} is not the authority (One-Brain Rule) — canonization runs only where the canonical key lives" >&2
  exit 3
fi
ENVF="$HOME/.mem0/stack.env"
kind="$(sed -n 's/^MEM0_HOST_KIND=//p' "$ENVF" 2>/dev/null | head -n1)"
if [[ "$kind" == "native" ]]; then
  sec="$(sed -n 's/^MEM0_SECRETS_DIR=//p' "$ENVF" | head -n1)"
  bind="$(sed -n 's/^MEM0_BIND=//p' "$ENVF" | head -n1)"
  [[ -n "$sec" && -n "$bind" ]] || { echo "stack.env lacks MEM0_SECRETS_DIR / MEM0_BIND — re-run install/linux-authority.sh" >&2; exit 2; }
  exec systemd-run --user --pipe --wait --quiet --collect \
    -p "LoadCredentialEncrypted=ams-api-key:$sec/ams-api-key.cred" \
    -p "LoadCredentialEncrypted=ams-canonical-key:$sec/ams-canonical-key.cred" \
    -E "MEM0_URL=http://$bind:18791" -E "MEM0_CANONIZE_NO_FORWARD=1" \
    /bin/bash -c 'export MEM0_API_KEY_FILE="$CREDENTIALS_DIRECTORY/ams-api-key"; exec bash "$HOME/apps/mem0-scripts/mem0-canonize.sh" "$@"' _ "$@"
fi
MEM0_CANONIZE_NO_FORWARD=1 exec bash "$HOME/apps/mem0-scripts/mem0-canonize.sh" "$@"
