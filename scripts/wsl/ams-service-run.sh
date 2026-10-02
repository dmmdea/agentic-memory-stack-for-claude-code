#!/usr/bin/env bash
# ams-service-run.sh — run one operator command on the AUTHORITY with the service key (1.32.5).
#
# A server-side job label (contradiction-sweep-v019, stamp-retired-v013, backfill-apply-v013, the
# insight consolidators) is accepted by the server only when the request carries the authority's
# service key (security_invariants.require_service_credential). The nightly units load it as a
# systemd credential; this wrapper gives the operator's HAND RUNS the same credentials, so these
# modes keep working:
#   bash ~/apps/mem0-scripts/ams-service-run.sh contradiction-sweep.py --unstamp <id>
#   bash ~/apps/mem0-scripts/ams-service-run.sh contradiction-sweep.py --promote <id>
#   bash ~/apps/mem0-scripts/ams-service-run.sh stamp-retired-at.py --dry-run
#   bash ~/apps/mem0-scripts/ams-service-run.sh ship_log_reclassify.py --live
# Only the scripts in ALLOWED below run (each one sends a server-side job label); the rest of the
# arguments pass through verbatim.
#   - role != brain     -> exit 3 (no replica or PC holds the service key, by design)
#   - native authority  -> a transient user unit loading ams-api-key + ams-service-key, MEM0_URL at
#                          the tailnet bind (the shape of ams-canonize.sh). Its output is piped to
#                          this terminal: if the ssh session drops, the unit is stopped at its next
#                          write, so run a long scroll (stamp-retired-at.py, ship_log_reclassify.py)
#                          inside tmux or screen.
#   - WSL brain         -> run directly: ams_env.service_key() reads ~/.mem0/service-key
set -euo pipefail
ALLOWED="contradiction-sweep.py stamp-retired-at.py ship_log_reclassify.py"
[[ $# -ge 1 ]] || { echo "usage: ams-service-run.sh <$(echo "$ALLOWED" | tr ' ' '|')> [args...]" >&2; exit 2; }
ENVF="$HOME/.mem0/stack.env"
env_val() { sed -n "s/^$1=//p" "$ENVF" 2>/dev/null | head -n1; }
ROLE="$(env_val MEM0_ROLE)"
[[ -n "$ROLE" ]] || ROLE="$(tr -d '[:space:]' < "$HOME/.mem0/role" 2>/dev/null || true)"
if [[ "$ROLE" != "brain" ]]; then
  echo "refusing: role=${ROLE:-unset} is not the authority; the service key exists only there" >&2
  exit 3
fi
name="$1"; shift
case " $ALLOWED " in
  *" $name "*) ;;
  *) echo "not a service-key script: $name (allowed: $ALLOWED)" >&2; exit 2 ;;
esac
SCRIPTS="$HOME/apps/mem0-scripts"
PY="$HOME/apps/mem0-server/.venv/bin/python"
target="$SCRIPTS/$name"
[[ -f "$target" ]] || { echo "missing $target (re-run the installer, which deploys the scripts)" >&2; exit 2; }
[[ -x "$PY" ]] || { echo "missing $PY (the authority's server venv)" >&2; exit 2; }
if [[ "$(env_val MEM0_HOST_KIND)" == "native" ]]; then
  sec="$(env_val MEM0_SECRETS_DIR)"
  bind="$(env_val MEM0_BIND)"
  [[ -n "$sec" && -n "$bind" ]] || { echo "stack.env lacks MEM0_SECRETS_DIR / MEM0_BIND — re-run install/linux-authority.sh" >&2; exit 2; }
  for c in ams-api-key ams-service-key; do
    [[ -r "$sec/$c.cred" ]] || { echo "missing $sec/$c.cred (re-run install/linux-authority.sh, which makes the service key)" >&2; exit 2; }
  done
  # a plain `ssh host 'cmd'` may reach a shell without the user runtime dir, and systemd-run --user needs it
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  unit="ams-service-run-$(date -u +%Y%m%dT%H%M%SZ)"
  echo "ams-service-run: $name in $unit.service (journalctl --user -u $unit)" >&2
  exec systemd-run --user --pipe --wait --collect --unit="$unit" \
    -p "LoadCredentialEncrypted=ams-api-key:$sec/ams-api-key.cred" \
    -p "LoadCredentialEncrypted=ams-service-key:$sec/ams-service-key.cred" \
    -p "Environment=MEM0_HOST_KIND=native" \
    -p "Environment=MEM0_CODEX_TRANSPORT=native" \
    -p "Environment=CODEX_HOME=$sec/codex" \
    -E "MEM0_URL=http://$bind:18791" \
    /bin/bash -c 'export MEM0_API_KEY_FILE="$CREDENTIALS_DIRECTORY/ams-api-key"; exec "$@"' _ "$PY" "$target" "$@"
fi
exec "$PY" "$target" "$@"
