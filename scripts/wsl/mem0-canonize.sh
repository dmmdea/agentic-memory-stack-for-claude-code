#!/usr/bin/env bash
# mem0-canonize.sh — sign and dispatch user-direct operations against mem0 memories.
#
# Usage (tier promotion — v0.19 Phase G: signs format-2, action="promote"):
#   bash mem0-canonize.sh <memory_id> "<reason>"
#   bash mem0-canonize.sh --actor dream-autopromote <memory_id> "<reason>"
#
# Usage (mutation actions — v0.17 Phase A, new --action flag):
#   bash mem0-canonize.sh --action put            <memory_id> "<reason>" --text "<new text>"
#   bash mem0-canonize.sh --action delete         <memory_id> "<reason>"
#   bash mem0-canonize.sh --action delete         <memory_id> "<reason>" --cascade
#   bash mem0-canonize.sh --action patch_metadata <memory_id> "<reason>" --metadata-json '<json>'
#
# Requires:
#   ~/.mem0/api-key — regular mem0 API key
#   HMAC signing key from ONE of (v0.19 Phase H resolution order):
#     1. $XDG_RUNTIME_DIR/mem0/canonical-key (tmpfs — present while mem0.service
#        runs; injected by dpapi-fetch-key.sh)
#     2. ~/.mem0/canonical-key (plaintext — dev/recovery only)
#     3. ~/.mem0/canonical-key.dpapi (DPAPI blob, decrypted inline via interop)
#
# ─── Signed-payload format (v0.19 Phase G: format-2 everywhere) ─────────────
#
# ALL operations — including tier promotion — now sign format 2:
#     <ts>|<nonce>|<action>|<memory_id>|<reason>
#   The nonce (uuid4 via uuidgen) is always generated and sent as:
#     X-User-Direct-Nonce header
#   This enables server-side replay protection (~/.mem0/canonical-replay.jsonl).
#   where action ∈ {promote, put, delete, patch_metadata}
#   Tier promotion (no --action flag) signs action="promote" (v0.19 Phase G).
#   Server validates via security_invariants.validate_hmac_user_direct().
#
# Format 1 (tier promotion, v0.14–v0.18: <ts>|<memory_id>|<reason>, no nonce)
# is DEPRECATED: the server still accepts it through v0.19 (logging a WARN per
# use) and rejects it in v0.20. This CLI no longer produces it.
#
# IMPORTANT: the action word is inside the signed payload, so a promote token
# cannot be replayed as a put/delete/patch_metadata token (and vice versa) —
# the server rejects the wrong action outright (HMAC mismatch).
#
# ─── delete_linked / --cascade (v0.17 F.1.4) ─────────────────────────────────
# Default (no --cascade): server uses delete_linked=False — preserves the
#   supersession chain for ledger replay and stack-restore.
# With --cascade: server uses delete_linked=True — also removes memories that
#   were superseded by the target (useful when cleaning up an entire chain).
#   Only valid with --action delete. Irreversible.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MEM0="${MEM0_URL:-http://127.0.0.1:18791}"

# ─── Argument parsing ────────────────────────────────────────────────────────

ACTION=""        # empty → tier-promotion (v0.14 compat), or one of: put, delete, patch_metadata
ACTOR=""         # Phase 2 autonomous: --actor flag for tier promotion body (default: user-direct)
TEXT=""          # required for --action put
METADATA_JSON="" # required for --action patch_metadata
CASCADE=0        # v0.17 F.1.4: pass ?cascade=true to DELETE when set
POSITIONAL=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --action)
      ACTION="$2"
      shift 2
      ;;
    --actor)
      ACTOR="$2"
      shift 2
      ;;
    --text)
      TEXT="$2"
      shift 2
      ;;
    --metadata-json)
      METADATA_JSON="$2"
      shift 2
      ;;
    --cascade)
      CASCADE=1
      shift
      ;;
    --help|-h)
      grep '^#' "$0" | head -50 | sed 's/^# \?//'
      exit 0
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

# Validate positional args
if [[ ${#POSITIONAL[@]} -lt 2 ]]; then
  echo "Error: requires <memory_id> and \"<reason>\" positional arguments." >&2
  echo "Run '$0 --help' for usage." >&2
  exit 2
fi

MID="${POSITIONAL[0]}"
REASON="${POSITIONAL[1]}"

# Validate --action value if supplied
if [[ -n "$ACTION" ]]; then
  case "$ACTION" in
    put|delete|patch_metadata) ;;
    *)
      echo "Error: --action must be one of: put, delete, patch_metadata" >&2
      echo "  (For tier promotion, omit --action entirely.)" >&2
      exit 2
      ;;
  esac
fi

# Validate --text is supplied for put
if [[ "$ACTION" == "put" && -z "$TEXT" ]]; then
  echo "Error: --action put requires --text \"<new memory text>\"" >&2
  exit 2
fi

# Validate --metadata-json is supplied for patch_metadata
if [[ "$ACTION" == "patch_metadata" && -z "$METADATA_JSON" ]]; then
  echo "Error: --action patch_metadata requires --metadata-json '<json object>'" >&2
  exit 2
fi

# Validate --cascade only applies to delete
if [[ $CASCADE -eq 1 && "$ACTION" != "delete" ]]; then
  echo "Error: --cascade is only valid with --action delete" >&2
  exit 2
fi

# ─── Read keys ───────────────────────────────────────────────────────────────

API_KEY="$(cat "$HOME/.mem0/api-key")"

# v0.19 Phase H: canonical key resolution (mirrors canonical_key_provider.py):
#   1. runtime tmpfs key — $XDG_RUNTIME_DIR/mem0/canonical-key, injected by
#      dpapi-fetch-key.sh (ExecStartPre on mem0.service) while mem0 runs
#   2. plaintext ~/.mem0/canonical-key — dev/recovery fallback (removed from
#      the production box at v0.19 Phase H cutover)
#   3. inline DPAPI decrypt of ~/.mem0/canonical-key.dpapi via WSL interop
#      (last resort — key bytes stay in memory, never written to disk)
resolve_canon_key() {
  local runtime="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/mem0/canonical-key"
  if [[ -r "$runtime" ]]; then cat "$runtime"; return 0; fi
  if [[ -r "$HOME/.mem0/canonical-key" ]]; then cat "$HOME/.mem0/canonical-key"; return 0; fi
  local blob="$HOME/.mem0/canonical-key.dpapi"
  local ps=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
  if [[ -r "$blob" && -x "$ps" ]]; then
    base64 -w0 "$blob" | "$ps" -NoProfile -NonInteractive -Command \
      "Add-Type -AssemblyName System.Security; \$b=[Convert]::FromBase64String([Console]::In.ReadToEnd().Trim()); [Console]::Out.Write([Convert]::ToBase64String([System.Security.Cryptography.ProtectedData]::Unprotect(\$b, \$null, 'CurrentUser')))" \
      2>/dev/null | tr -d '\r\n' | base64 -d
    return 0
  fi
  return 1
}

CANON_KEY="$(resolve_canon_key)" && [[ -n "$CANON_KEY" ]] || {
  echo "Error: canonical key unavailable — no runtime key (is mem0.service running?), no plaintext ~/.mem0/canonical-key, and DPAPI decrypt failed/unavailable" >&2
  exit 1
}
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ─── Nonce (v0.17 Phase F.1; v0.19 Phase G: ALL operations) ──────────────────
# A uuid4 nonce is generated for every operation, including tier promotion
# (v0.19 Phase G switched promotion to format-2 — the nonce-less format-1
# payload is deprecated and v0.20 rejects it).
# uuidgen is available in WSL Ubuntu (uuid-runtime package). Fallback: python3 uuid.
NONCE="$(uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())")"

# ─── Sign ────────────────────────────────────────────────────────────────────
# Format 2 (v0.17 F.1 + v0.19 G):  <ts>|<nonce>|<action>|<mid>|<reason>
# Tier promotion (no --action flag) signs the "promote" action word.

ACTION_WORD="${ACTION:-promote}"
MSG="${TS}|${NONCE}|${ACTION_WORD}|${MID}|${REASON}"

TOKEN="$(printf '%s' "$MSG" | openssl dgst -sha256 -hmac "$CANON_KEY" -binary | openssl base64 | tr -d '\n')"

HMAC_HEADERS=(-H "X-User-Direct-Token: $TOKEN" -H "X-User-Direct-Ts: $TS" -H "X-User-Direct-Nonce: $NONCE")

# ─── Dispatch ────────────────────────────────────────────────────────────────

if [[ -z "$ACTION" ]]; then
  # ── Tier promotion (v0.19 Phase G: format-2, action="promote") ──────────
  # Phase 2: --actor flag selects the promotion actor label (default: user-direct).
  PROMOTE_ACTOR="${ACTOR:-user-direct}"
  BODY="$(python3 -c "
import json, sys
actor = sys.argv[1]
reason = sys.argv[2]
print(json.dumps({'tier': 'canonical', 'actor': actor, 'reason': reason}))
" "$PROMOTE_ACTOR" "$REASON")"

  echo "Promoting memory $MID to canonical (action=promote)..."
  echo "  ts=$TS"
  echo "  nonce=${NONCE}"
  echo "  token=${TOKEN:0:20}..."

  curl -fsS -X PATCH "$MEM0/v1/memories/$MID/tier" \
    -H "X-API-Key: $API_KEY" \
    "${HMAC_HEADERS[@]}" \
    -H "Content-Type: application/json" \
    -d "$BODY" | python3 -m json.tool

elif [[ "$ACTION" == "put" ]]; then
  # ── PUT: update text on a canonical/insight memory ──────────────────────
  BODY="$(python3 -c "
import json, sys
print(json.dumps({'text': sys.argv[1]}))
" "$TEXT")"

  REASON_ENC="$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" "$REASON")"
  ACTOR_PARAM="actor=user-direct"

  echo "Updating text of memory $MID (action=put)..."
  echo "  ts=$TS"
  echo "  nonce=${NONCE}"
  echo "  token=${TOKEN:0:20}..."

  curl -fsS -X PUT "$MEM0/v1/memories/$MID?${ACTOR_PARAM}&reason=${REASON_ENC}" \
    -H "X-API-Key: $API_KEY" \
    "${HMAC_HEADERS[@]}" \
    -H "Content-Type: application/json" \
    -d "$BODY" | python3 -m json.tool

elif [[ "$ACTION" == "delete" ]]; then
  # ── DELETE: remove a canonical/insight memory ───────────────────────────
  REASON_ENC="$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" "$REASON")"
  ACTOR_PARAM="actor=user-direct"
  # v0.17 F.1.4: cascade flag — preserves chain by default; --cascade opts into delete_linked=True
  CASCADE_PARAM=""
  if [[ $CASCADE -eq 1 ]]; then
    CASCADE_PARAM="&cascade=true"
    echo "WARNING: --cascade passed — delete_linked=True will remove superseded memories too." >&2
  fi

  echo "Deleting memory $MID (action=delete, cascade=$CASCADE)..."
  echo "  ts=$TS"
  echo "  nonce=${NONCE}"
  echo "  token=${TOKEN:0:20}..."

  curl -fsS -X DELETE "$MEM0/v1/memories/$MID?${ACTOR_PARAM}&reason=${REASON_ENC}${CASCADE_PARAM}" \
    -H "X-API-Key: $API_KEY" \
    "${HMAC_HEADERS[@]}" | python3 -m json.tool

elif [[ "$ACTION" == "patch_metadata" ]]; then
  # ── PATCH /metadata: merge metadata on a canonical/insight memory ───────
  # Build merged body: {"metadata": <user-supplied json>, "actor": "user-direct", "reason": "<reason>"}
  BODY="$(python3 -c "
import json, sys
metadata = json.loads(sys.argv[1])
reason   = sys.argv[2]
print(json.dumps({'metadata': metadata, 'actor': 'user-direct', 'reason': reason}))
" "$METADATA_JSON" "$REASON")"

  echo "Patching metadata of memory $MID (action=patch_metadata)..."
  echo "  ts=$TS"
  echo "  nonce=${NONCE}"
  echo "  token=${TOKEN:0:20}..."

  curl -fsS -X PATCH "$MEM0/v1/memories/$MID/metadata" \
    -H "X-API-Key: $API_KEY" \
    "${HMAC_HEADERS[@]}" \
    -H "Content-Type: application/json" \
    -d "$BODY" | python3 -m json.tool
fi
