#!/usr/bin/env bash
# ams-dream-now.sh - start ONE dream consolidation by hand, on the authority, now.
#
# Runs ON the authority. From a replica PC: ssh <brain-alias> 'bash ~/apps/mem0-scripts/ams-dream-now.sh'
# (a replica's own dream-consolidate.ps1 exits on its role, -Force included, so it never consolidates).
#
# The nightly dream is ams-step-dream.service, one step of the chain. Started from a shell it would
# fail to authenticate: the unit supplies three systemd credentials and three Environment= lines. And
# its `--guarded` turns it into a receipted no-op once tonight's chain has succeeded. So this starts
# a transient user unit that carries the same LoadCredentialEncrypted= / Environment= / ExecStartPre=
# and runs the unit's own command without --guarded. It goes through ams-step.sh, so the run is
# receipted like a chain step (step `dream` in ~/.mem0/maintenance/receipts.jsonl); it never stamps
# last-chain-success (only the --guard step does), so it cannot void or complete a night.
#
# --force is passed to the dream on purpose: dream-consolidate.py skips ("nightly throttle (23h) not
# yet elapsed") when it ran less than 23 h ago, which after the nightly run is always. --force
# bypasses that throttle ONLY: the judge lock (a run overlapping the nightly is a quiet, receipted
# skip) and the Codex quota reserve still apply. It takes no arguments, because a --dry-run would
# still be receipted as a `dream` step and refresh the dream's freshness in /health/maintenance.
#
# One thing differs from the unit file, because systemd-run is not the unit loader: `-p Environment=`
# does not expand specifiers (measured on systemd 255: %d and %h reach the process literally), so
# MEM0_API_KEY_FILE=%d/ams-api-key is exported inside the command from $CREDENTIALS_DIRECTORY
# (scripts/wsl/ams-canonize.sh does the same), and %h becomes $HOME here.
# scripts/wsl/tests/test_ams_dream_now.py compares this call with systemd/ams-step-dream.service, so
# a credential or Environment= line added to the unit fails that test until it is added here.
#
# There is deliberately NO --pipe (nor --pty/--shell). With --pipe the unit's stdout is this session's
# pipe, and the dream prints as it goes, so a dropped ssh session, a closed laptop or a Ctrl-C kills it
# at its next print: a partial cycle (insights posted, throttle unmarked) and a `failed` dream receipt
# that turns /health/maintenance red (measured on systemd 255: exit 120). Without it the output goes to
# the journal exactly as the chain's does, `systemd-run --wait` still returns the unit's exit status,
# and the unit keeps running if this session goes away. So the helper prints the journalctl command
# that follows the unit, waits, and on completion prints the unit's result and where the dream's own
# verdict is: the exit status says the run ended, not what the dream decided (a skip exits 0 too).
#
# A forced dream is a real dream. When it completes it marks the same 23 h throttle as the nightly one
# (dream-consolidate.py marks `dream` and `index-refresh` after a good index build). Finish it after
# 04:00 and the next 03:00 dream is less than 23 h behind it, so that night skips ("nightly throttle
# (23h) not yet elapsed"), and on unchanged evidence a second pass only adds near-duplicate insights.
# Run it before 04:00, or take it as standing in for the next night.
#
# Exit: 0 ok, 2 the install record is incomplete, 3 not the authority, 64 usage; otherwise the
# unit's own status (systemd-run --wait returns it).
set -euo pipefail

usage() { echo "usage: ams-dream-now.sh   (no arguments; runs ON the authority, ssh <brain-alias> 'bash ~/apps/mem0-scripts/ams-dream-now.sh' from a replica)"; }
case "${1:-}" in
    "") ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 64 ;;
esac

ENVF="$HOME/.mem0/stack.env"
env_val() {  # $1 = key -> its first value in stack.env, CR dropped; empty when the file or key is absent
    { sed -n "s/^$1=//p" "$ENVF" 2>/dev/null | head -n1 | tr -d '\r'; } || true
}
fail() { echo "ams-dream-now: $*" >&2; exit 2; }
refuse() { echo "ams-dream-now: refusing - $*" >&2; exit 3; }

role="$(env_val MEM0_ROLE)"
[ -n "$role" ] || role="$(tr -d '[:space:]' < "$HOME/.mem0/role" 2>/dev/null || true)"
if [ "$role" != brain ]; then
    refuse "this box is role=${role:-unknown}, not the authority. The dream runs where the corpus and the canonical key live; start it from here with: ssh <brain-alias> 'bash ~/apps/mem0-scripts/ams-dream-now.sh'"
fi
kind="$(env_val MEM0_HOST_KIND)"
if [ "$kind" != native ]; then
    refuse "MEM0_HOST_KIND=${kind:-unset} in ~/.mem0/stack.env: only the native Linux authority runs the dream as a systemd chain step. A WSL brain's dream is the Windows scheduled task; force it there with dream-consolidate.ps1 -Force."
fi
sec="$(env_val MEM0_SECRETS_DIR)"
[ -n "$sec" ] || fail '~/.mem0/stack.env has no MEM0_SECRETS_DIR (the directory holding the .cred files); re-run install/linux-authority.sh, which records it'
for c in ams-api-key ams-canonical-key ams-service-key; do
    [ -r "$sec/$c.cred" ] || fail "missing $sec/$c.cred (the credential ams-step-dream.service loads)"
done
PY="$HOME/apps/mem0-server/.venv/bin/python"
SCRIPTS="$HOME/apps/mem0-scripts"
[ -x "$PY" ] || fail "missing $PY (the authority's server venv)"
for f in ams-step.sh dream-consolidate.py codex-usage-report.py; do
    [ -f "$SCRIPTS/$f" ] || fail "missing $SCRIPTS/$f (re-run install/linux-authority.sh, which deploys the chain scripts)"
done
command -v systemd-run >/dev/null || fail "systemd-run not found"

# a plain `ssh host 'cmd'` may reach a shell without the user runtime dir, and systemd-run --user needs it
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
unit="ams-dream-now-$(date -u +%Y%m%dT%H%M%SZ)"
say() { echo "ams-dream-now: $*" >&2; }
say "starting $unit.service. It runs as a systemd user unit and keeps running if this session drops or you press Ctrl-C."
say "follow it from another shell: journalctl --user -u $unit -f"
say "the receipt is step 'dream' in ~/.mem0/maintenance/receipts.jsonl"
rc=0
systemd-run --user --wait --collect --unit="$unit" \
    -p "LoadCredentialEncrypted=ams-api-key:$sec/ams-api-key.cred" \
    -p "LoadCredentialEncrypted=ams-canonical-key:$sec/ams-canonical-key.cred" \
    -p "LoadCredentialEncrypted=ams-service-key:$sec/ams-service-key.cred" \
    -p "Environment=MEM0_HOST_KIND=native" \
    -p "Environment=MEM0_CODEX_TRANSPORT=native" \
    -p "Environment=CODEX_HOME=$sec/codex" \
    -p "ExecStartPre=-$PY $SCRIPTS/codex-usage-report.py --probe" \
    /bin/bash -c 'export MEM0_API_KEY_FILE="$CREDENTIALS_DIRECTORY/ams-api-key"; exec "$@"' _ \
    /bin/bash "$SCRIPTS/ams-step.sh" dream "$PY" "$SCRIPTS/dream-consolidate.py" --force || rc=$?
if [ "$rc" -eq 0 ]; then
    say "$unit.service finished: result success (exit 0)."
else
    say "$unit.service finished: result failure (exit $rc). Why: journalctl --user -u $unit"
fi
say "that is the unit's result, not the dream's verdict (a skip for the judge lock or the quota gate exits 0 too). The verdict is the dream's own 'dream:' lines: journalctl --user -u $unit | grep 'dream:', or tail -n 40 ~/.mem0/maintenance/logs/dream.log; the receipt is step 'dream' in ~/.mem0/maintenance/receipts.jsonl"
exit "$rc"
