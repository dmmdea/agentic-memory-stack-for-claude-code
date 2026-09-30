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
# Migration cap (P5-6, decided 2026-09-29, measured on the largest store): judge-apply migrates at
# most 5 facts per store per night by default, and that store's inflow was larger than 5/night, so
# it never left "40 over-cap, 40 pullable". A store OVER the compaction trigger (20000 B or 160
# lines) is therefore passed --max-migrations 15; every other store keeps 5. The blast cap still
# bounds every run, and the line floor still bypasses this cap.
#
# The step outcome (contract C1): when the chain step exports AMS_OUTCOME_FILE this script writes
# ONE line to it, "<status>[:<reason>] <json counts>". The counts are stores, offered, migrated,
# add_failed and updated (summed from every judge-apply --json), plus actionable and
# unparsed_pointer from a lint pass over the hub checkout. A night that offered migrations, migrated
# none and had corpus write failures reads "degraded:add-failed-<n>": the run fails closed and exits
# 0 by design (a transient embedder outage must not turn the chain red), so the outcome is where the
# outage becomes visible instead of reading as a healthy night.
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
# The operator's brand map (contract C3), when configured: judge-apply tags a migrated fact with the
# brand it resolves to. Unset or unreadable means brand-neutral, as before the map existed.
BRAND_MAP="${MEM0_BRAND_MAP:-}"
# On the brain the path lives in stack.env (the unit does not load it), so read it from there.
if [ -z "$BRAND_MAP" ] && [ -s "$HOME/.mem0/stack.env" ]; then
    BRAND_MAP="$(sed -n 's/^MEM0_BRAND_MAP=//p' "$HOME/.mem0/stack.env" | head -n 1 | tr -d "\"'")"
fi
# Over-trigger thresholds mirror store.TriggerBytes / store.TriggerLines.
TRIGGER_BYTES=20000
TRIGGER_LINES=160
MAX_MIGRATIONS_DEFAULT=5
MAX_MIGRATIONS_OVER_TRIGGER=15

# json_num <file> <key>: the first integer value of "key" in a JSON document, empty when absent.
# Dependency-free on purpose (the hub wrapper must not need jq or python to report).
json_num() {
    grep -o "\"$2\"[[:space:]]*:[[:space:]]*[0-9]*" "$1" 2>/dev/null | head -n 1 | grep -o '[0-9]*$'
}

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

sync_args="--once --projects-root $PROJECTS --state-root $STATE"
[ -n "$HUB_HOST" ] && sync_args="$sync_args --hub-host $HUB_HOST"

# P5-10 (2026-09-19): sync BEFORE the apply as well as after. The plan was written against a
# checkout the dream step had just synced; the minutes in between are enough for a PC to push
# an edit to a planned file, and a decision applied to a day-old copy is what resurrected two
# facts on night 3. Exit 0 and 6 (conflict recorded, work tree = merge result) are current;
# anything else leaves the plan unapplied, loudly - the post-apply sync below still runs so the
# deterministic floor lands whatever happened here.
# shellcheck disable=SC2086
"$BIN" sync $sync_args
pre_rc=$?
case "$pre_rc" in
    0|6) ;;
    *) echo "ams-store-judge: pre-apply sync exit=$pre_rc; the plan would apply to a stale checkout - not applied" >&2
       PLAN="" ;;
esac

applied=0; skipped=0; failed=0
stores=0; offered=0; migrated=0; add_failed=0; updated=0
result_file="$(mktemp)"
trap 'rm -f "$result_file"' EXIT

# add_count <var> <key>: add the count judge-apply reported for <key> to the running total <var>.
add_count() {
    local n
    n="$(json_num "$result_file" "$2")"
    printf -v "$1" '%d' $(( ${!1} + ${n:-0} ))
}

if [ -n "$PLAN" ] && [ -s "$PLAN" ]; then
    echo "ams-store-judge: applying $PLAN"
    for d in "$PROJECTS"/*/memory; do
        [ -d "$d" ] || continue
        ws="$(basename "$(dirname "$d")")"
        # A store over the compaction trigger gets the larger migration cap (P5-6).
        max_mig="$MAX_MIGRATIONS_DEFAULT"
        if [ -f "$d/MEMORY.md" ]; then
            idx_bytes="$(wc -c < "$d/MEMORY.md" | tr -d ' ')"
            idx_lines="$(awk 'END { print NR }' "$d/MEMORY.md")"
            if [ "${idx_bytes:-0}" -ge "$TRIGGER_BYTES" ] || [ "${idx_lines:-0}" -ge "$TRIGGER_LINES" ]; then
                max_mig="$MAX_MIGRATIONS_OVER_TRIGGER"
            fi
        fi
        apply_args=(judge-apply --plan "$PLAN" --store "$d" --workspace "$ws"
                    --projects-root "$PROJECTS" --state-root "$STATE"
                    --max-migrations "$max_mig" --json)
        [ -n "$BRAND_MAP" ] && apply_args+=(--brand-map "$BRAND_MAP")
        : > "$result_file"
        "$BIN" "${apply_args[@]}" > "$result_file"
        rc=$?
        # --json keeps the machine result off the journal's stderr lines, so put it there: the
        # status, the note and every "line kept" orphan are how an outage is diagnosed at 9 am.
        echo "ams-store-judge: $ws max_migrations=$max_mig exit=$rc result: $(tr -d '\n' < "$result_file" | tr -s ' ')"
        if [ "$rc" -eq 0 ]; then
            applied=$((applied + 1))
            stores=$((stores + 1))
            add_count offered offered
            add_count migrated migrated
            add_count add_failed add_failed
            add_count updated updated
        else
            # 3 = refused (not the hub), 4 = the per-PC lock is held. Both are reasons, not
            # crashes, and neither should take the other stores down with it.
            echo "ams-store-judge: $ws exit=$rc" >&2
            if [ "$rc" = 3 ] || [ "$rc" = 4 ]; then skipped=$((skipped + 1)); else failed=$((failed + 1)); fi
        fi
    done
else
    echo "ams-store-judge: no plan at $PLAN; deterministic-only night (the sync below still derives every store)"
fi

# shellcheck disable=SC2086
"$BIN" sync $sync_args
rc=$?
echo "ams-store-judge: applied=$applied skipped=$skipped failed=$failed pre_sync_exit=$pre_rc sync_exit=$rc"

# The hub checkout is linted every night, after the apply and the sync that derived it. Nothing
# else ran lint on the hub (its summary was twelve days old and described a different fleet), so a
# blind or wedged index there could go unnoticed. The counts go into the step outcome below.
actionable=""; unparsed_pointer=""
lint_args=(lint --quiet --projects-root "$PROJECTS" --state-root "$STATE"
           --summary-out "$STATE/lint-summary.json")
[ -n "$HUB_HOST" ] && lint_args+=(--hub-host "$HUB_HOST")
if "$BIN" "${lint_args[@]}" > /dev/null; then
    actionable="$(json_num "$STATE/lint-summary.json" actionable)"
    unparsed_pointer="$(json_num "$STATE/lint-summary.json" unparsed_pointer)"
else
    echo "ams-store-judge: hub lint failed; its counts are left out of the outcome" >&2
fi

# The step outcome (contract C1). Only when the chain step asked for one.
if [ -n "${AMS_OUTCOME_FILE:-}" ]; then
    status="ok"
    if [ "$offered" -gt 0 ] && [ "$migrated" -eq 0 ] && [ "$add_failed" -gt 0 ]; then
        status="degraded:add-failed-$add_failed"
    fi
    counts="{\"stores\":$stores,\"offered\":$offered,\"migrated\":$migrated,\"add_failed\":$add_failed,\"updated\":$updated"
    [ -n "$actionable" ] && counts="$counts,\"actionable\":$actionable"
    [ -n "$unparsed_pointer" ] && counts="$counts,\"unparsed_pointer\":$unparsed_pointer"
    printf '%s %s}\n' "$status" "$counts" > "$AMS_OUTCOME_FILE"
fi

[ "$failed" = 0 ] || exit 1
case "$pre_rc" in 0|6) ;; *) exit "$pre_rc" ;; esac
exit "$rc"
