#!/usr/bin/env bash
# ams-step.sh [--guard] <step> <cmd...> — run one chain step and receipt it (spec §4/§9).
# Receipt: ~/.mem0/maintenance/receipts.jsonl, one line per run:
#   {ts, step, ok, exit, duration_ms, receipt_id, note}
# --guard: the chain's boot guard — when last-chain-success is < 20 h old the step is a no-op with
#          a receipt that says so (OnBootSec=15min would otherwise re-run a night that completed).
#          A guarded step that succeeds stamps last-chain-success.
set -u
GUARD=0; [ "${1:-}" = "--guard" ] && { GUARD=1; shift; }
step="${1:?usage: ams-step.sh [--guard] <step> <cmd...>}"; shift
[ $# -gt 0 ] || { echo "ams-step: no command for step $step" >&2; exit 64; }
dir="$HOME/.mem0/maintenance"; mkdir -p "$dir"
rp="$dir/receipts.jsonl"; stamp="$dir/last-chain-success"
rid="$step-$(date -u +%Y%m%dT%H%M%SZ)-$(head -c 3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
json_escape() { python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }
receipt() {  # ok exit duration_ms note
    printf '{"ts":"%s","step":"%s","ok":%s,"exit":%s,"duration_ms":%s,"receipt_id":"%s","note":%s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$step" "$1" "$2" "$3" "$rid" "$(printf '%s' "$4" | json_escape)" >> "$rp"
}
if [ "$GUARD" = 1 ] && [ -f "$stamp" ]; then
    age=$(( $(date +%s) - $(cat "$stamp" 2>/dev/null || echo 0) ))
    if [ "$age" -lt $((20 * 3600)) ]; then
        receipt true 0 0 "guard: chain succeeded ${age}s ago (< 20h); no-op"
        exit 0
    fi
fi
t0=$(date +%s%3N)
err="$(mktemp)"
# stderr is captured synchronously, then echoed: a `2> >(tee …)` substitution is not waited
# for and the receipt read raced it (review 2026-09-10: 2 of 5000 lines landed in the note).
"$@" 2>"$err"; rc=$?
cat "$err" >&2
ms=$(( $(date +%s%3N) - t0 ))
if [ "$rc" -eq 0 ]; then
    receipt true 0 "$ms" ""
    [ "$GUARD" = 1 ] && date +%s > "$stamp"
else
    receipt false "$rc" "$ms" "$(tail -c 400 "$err")"
fi
rm -f "$err"
exit "$rc"
