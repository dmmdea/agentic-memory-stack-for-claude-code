#!/usr/bin/env bash
# ams-step.sh [--guard] <step> <cmd...> — run one chain step and receipt it (spec §4/§9).
# Receipt: ~/.mem0/maintenance/receipts.jsonl, one line per run:
#   {ts, step, ok, exit, duration_ms, receipt_id, note}
# --guard: the chain's boot guard — when last-chain-success is newer than the most recent scheduled
#          boundary (AMS_CHAIN_SCHEDULE, default 03:00 local) the step is a no-op with a receipt that
#          says so (OnBootSec=15min would otherwise re-run a night that completed). Calendar-aware
#          on purpose: the first "< 20 h" rule let an evening hand run void the 03:00 night.
#          A guarded step that succeeds stamps last-chain-success. AMS_GUARD_NOW=<epoch> for tests.
# --guarded: the same check as --guard but NEVER stamps: every step between the first and the
#          stamping step (stack-backup) carries it, so a boot re-run of a completed night is a
#          chain of receipted no-ops and a partial night re-runs from the top.
# --weekly <Day>: the step runs only on that weekday (`date +%a`); other days receipt a no-op.
#          AMS_STEP_TODAY=<Day> overrides for tests.
set -u
GUARD=0; STAMP=0; WEEKLY=""
while :; do
    case "${1:-}" in
        --guard) GUARD=1; STAMP=1; shift ;;
        --guarded) GUARD=1; shift ;;
        --weekly) WEEKLY="${2:?--weekly needs a day (Sun)}"; shift 2 ;;
        *) break ;;
    esac
done
step="${1:?usage: ams-step.sh [--guard|--guarded] [--weekly Day] <step> <cmd...>}"; shift
[ $# -gt 0 ] || { echo "ams-step: no command for step $step" >&2; exit 64; }
dir="$HOME/.mem0/maintenance"; mkdir -p "$dir"
rp="$dir/receipts.jsonl"; stamp="$dir/last-chain-success"
rid="$step-$(date -u +%Y%m%dT%H%M%SZ)-$(head -c 3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
json_escape() { python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }
receipt() {  # ok exit duration_ms note
    printf '{"ts":"%s","step":"%s","ok":%s,"exit":%s,"duration_ms":%s,"receipt_id":"%s","note":%s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$step" "$1" "$2" "$3" "$rid" "$(printf '%s' "$4" | json_escape)" >> "$rp"
}
if [ -n "$WEEKLY" ]; then
    today="${AMS_STEP_TODAY:-$(date +%a)}"
    if [ "$today" != "$WEEKLY" ]; then
        receipt true 0 0 "weekly: not $WEEKLY; no-op"
        exit 0
    fi
fi
if [ "$GUARD" = 1 ] && [ -f "$stamp" ]; then
    now="${AMS_GUARD_NOW:-$(date +%s)}"
    sched="${AMS_CHAIN_SCHEDULE:-03:00}"
    last="$(cat "$stamp" 2>/dev/null || echo 0)"
    # python3, not `date -d`: the boundary arithmetic must read the same on GNU and uutils date.
    boundary="$(python3 - "$now" "$sched" <<'PY'
import datetime as dt, sys
now = dt.datetime.fromtimestamp(int(sys.argv[1])); h, m = (int(x) for x in sys.argv[2].split(":"))
b = now.replace(hour=h, minute=m, second=0, microsecond=0)
if b > now:
    b -= dt.timedelta(days=1)
print(int(b.timestamp()))
PY
)"
    if [ "$last" -ge "$boundary" ]; then
        receipt true 0 0 "guard: chain succeeded since the last $sched boundary ($(( now - last ))s ago); no-op"
        exit 0
    fi
fi
# Every job resolves the authority the same way (ams_env.mem0_url): the unit need not repeat it.
if [ -z "${MEM0_URL:-}" ] && [ -s "$HOME/.mem0/authority-url" ]; then
    MEM0_URL="$(head -n1 "$HOME/.mem0/authority-url")"; export MEM0_URL
fi
# $EPOCHREALTIME (bash >= 5) in microseconds: `date +%s%3N` is GNU-only; the uutils coreutils
# shipped on Ubuntu 26.04 ignores the width and prints nanoseconds (first live chain run
# receipted 1,834,879,975 ms for a 2 s step).
t0=${EPOCHREALTIME/./}
err="$(mktemp)"
# stderr is captured synchronously, then echoed: a `2> >(tee …)` substitution is not waited
# for and the receipt read raced it (review 2026-09-10: 2 of 5000 lines landed in the note).
"$@" 2>"$err"; rc=$?
cat "$err" >&2
ms=$(( (${EPOCHREALTIME/./} - t0) / 1000 ))
if [ "$rc" -eq 0 ]; then
    receipt true 0 "$ms" ""
    [ "$STAMP" = 1 ] && date +%s > "$stamp"
else
    receipt false "$rc" "$ms" "$(tail -c 400 "$err")"
fi
rm -f "$err"
exit "$rc"
