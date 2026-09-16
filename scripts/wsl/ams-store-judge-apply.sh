#!/usr/bin/env bash
# ams-store-judge-apply.sh - apply the nightly judge plan to the hub's checkout, then push.
#
# The chain step (ams-step-store-judge.service) runs this through ams-step.sh, so the receipt,
# the boot guard and the exit code are handled there; this script is the loop.
#
# What it does, per store in the hub checkout:
#   ams-store judge-apply --plan <plan> --store <dir> --workspace <ws>
# and then ONE sync pass, which is what carries the result to the bare repo and what every PC
# picks up at its next session boundary.
#
# A MISSING PLAN IS NOT A FAILURE. dream-consolidate.py writes the plan only when it validates
# against the generated schema, and refuses to write a malformed one on purpose: a night with no
# plan is a deterministic-only night, and the deterministic work is the part that keeps every
# index under the caps. So a missing plan skips the apply loop and still syncs - that is the
# "the floor lands even when the plan is empty" clause of the P4-1b gate, and it is the reason
# this script does not `set -e` around the apply.
#
# Exit: 0 when every store either applied or was skipped for a reason the receipt names; 1 when
# the checkout or the binary is missing (a misconfigured hub must be loud); the sync's own exit
# code when the push fails, so a hub that cannot reach its own bare repo is not a silent success.
set -u
export LC_ALL=C

BIN="${AMS_STORE_BIN:-/usr/local/bin/ams-store}"
CHECKOUT="${AMS_STORE_CHECKOUT:-}"
HUB_HOST="${AMS_STORE_HUB_HOST:-}"
PLAN="${AMS_STORE_PLAN:-$HOME/.mem0/maintenance/dream/store-judge.json}"

[ -n "$CHECKOUT" ] || { echo "ams-store-judge: AMS_STORE_CHECKOUT is unset; this box holds no hub checkout" >&2; exit 1; }
[ -x "$BIN" ] || { echo "ams-store-judge: $BIN is not installed or not executable" >&2; exit 1; }

PROJECTS="$CHECKOUT/projects"
STATE="$CHECKOUT/state"
[ -d "$PROJECTS" ] || { echo "ams-store-judge: $PROJECTS does not exist" >&2; exit 1; }

# judge-apply reads the corpus key from the ENVIRONMENT only (a key on a command line reaches the
# process list and the shell history). systemd hands us a credential FILE, so load it here.
if [ -z "${MEM0_API_KEY:-}" ] && [ -n "${MEM0_API_KEY_FILE:-}" ] && [ -r "${MEM0_API_KEY_FILE}" ]; then
    MEM0_API_KEY="$(cat "$MEM0_API_KEY_FILE")"
    export MEM0_API_KEY
fi

applied=0; skipped=0; failed=0
if [ -s "$PLAN" ]; then
    echo "ams-store-judge: applying $PLAN"
    for d in "$PROJECTS"/*/memory; do
        [ -d "$d" ] || continue
        ws="$(basename "$(dirname "$d")")"
        if "$BIN" judge-apply --plan "$PLAN" --store "$d" --workspace "$ws" \
                --projects-root "$PROJECTS" --state-root "$STATE"; then
            applied=$((applied + 1))
        else
            rc=$?
            # 3 = refused (not the hub), 4 = the per-PC lock is held. Both are reasons, not
            # crashes, and neither should take the other stores down with it.
            echo "ams-store-judge: $ws exit=$rc" >&2
            if [ "$rc" = 3 ] || [ "$rc" = 4 ]; then skipped=$((skipped + 1)); else failed=$((failed + 1)); fi
        fi
    done
else
    echo "ams-store-judge: no plan at $PLAN; deterministic-only night (the sync below still derives every store)"
fi

sync_args="--once --projects-root $PROJECTS --state-root $STATE"
[ -n "$HUB_HOST" ] && sync_args="$sync_args --hub-host $HUB_HOST"
# shellcheck disable=SC2086
"$BIN" sync $sync_args
rc=$?
echo "ams-store-judge: applied=$applied skipped=$skipped failed=$failed sync_exit=$rc"
[ "$failed" = 0 ] || exit 1
exit "$rc"
