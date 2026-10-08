#!/usr/bin/env bash
# ams-step.sh [--guard] <step> <cmd...> — run one chain step and receipt it (spec §4/§9).
# Receipt: ~/.mem0/maintenance/receipts.jsonl, one line per run:
#   {ts, step, ok, status, exit, duration_ms, receipt_id, note, work}
# OUTCOME CONTRACT (C1): the job gets $AMS_OUTCOME_FILE and MAY write ONE line to it,
#   <status>[:<reason>] <json-object-of-counts>      status = ok | degraded | failed
# e.g. `degraded:posted-0-of-3 {"consolidated":3,"posted":0}`. Exit 0 says the process finished, not
# that it did its job; the outcome line says the second thing:
#   exit != 0                  -> ok:false status:failed, note = stderr tail
#   exit 0 + failed:<reason>   -> ok:false status:failed exit:0, note = reason (never stamps the chain)
#   exit 0 + degraded:<reason> -> ok:true  status:degraded, note = reason
#   exit 0 + no line, or ok    -> ok:true  status:ok
# `work` is the counts object ({} when absent). Text that is not `<status>[:reason] [{json}]` reads
# degraded with note `outcome-unparsable: <first 120 chars>`. When the note would otherwise be empty
# and the status is not ok, the job's LAST stdout line is the note: the journal drops stdout lines
# that carry no unit attribution, so the receipt has to hold that evidence itself.
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
# C locale for everything below: the authority's user manager exports a Spanish LC_NUMERIC/LC_TIME,
# under which $EPOCHREALTIME carries a comma ("value too great for base", no receipt, exit 1 on
# the first live chain) and `date +%a` prints "dom", never matching --weekly Sun.
export LC_ALL=C
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
receipt() {  # ok exit duration_ms note   (the guard / weekly no-ops: status ok, no work)
    printf '{"ts":"%s","step":"%s","ok":%s,"status":"ok","exit":%s,"duration_ms":%s,"receipt_id":"%s","note":%s,"work":{}}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$step" "$1" "$2" "$3" "$rid" "$(printf '%s' "$4" | json_escape)" >> "$rp"
}
# finalize_receipt rc duration_ms outcome_file stderr_file stdout_file: resolve the outcome contract,
# append the receipt line, print "true" when the step counts as a success (ok:true).
finalize_receipt() {
    python3 - "$rp" "$step" "$rid" "$@" <<'PY'
import datetime as dt, json, re, sys
rp, step, rid, rc, ms, outcome_f, err_f, out_f = sys.argv[1:9]
rc = int(rc)
def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""
def last_line(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""
status, reason, work, unparsable = "ok", "", {}, ""
lines = [ln.strip() for ln in read(outcome_f).splitlines() if ln.strip()]
if lines:
    m = re.match(r"^(ok|degraded|failed)(?::(\S*))?(?:\s+(\{.*\}))?$", lines[-1])
    parsed = None
    if m:
        try:
            parsed = json.loads(m.group(3)) if m.group(3) else {}
        except ValueError:
            parsed = None
    if m and isinstance(parsed, dict):
        status, reason, work = m.group(1), (m.group(2) or "").strip(), parsed
    else:
        status, unparsable = "degraded", "outcome-unparsable: " + lines[-1][:120]
if rc != 0:
    status = "failed"
    note = read(err_f)[-400:].rstrip(chr(10)) or last_line(read(out_f))
else:
    note = unparsable or reason
    if status != "ok" and not note:
        note = last_line(read(out_f))
ok = rc == 0 and status != "failed"
rec = {"ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "step": step, "ok": ok,
       "status": status, "exit": rc, "duration_ms": int(ms), "receipt_id": rid, "note": note, "work": work}
with open(rp, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
print("true" if ok else "false")
PY
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
# The corpus PARTITION travels with the authority (ams_env.user_id): MEM0_DEFAULT_USER_ID first,
# then the stack's own user. The store judge is the job that needs it - it writes facts into the
# corpus - and an empty user_id is refused by the authority per REQUEST, so without this every
# migration fails on its own and the night still reads as green.
if [ -z "${MEM0_USER_ID:-}" ] && [ -s "$HOME/.mem0/stack.env" ]; then
    MEM0_USER_ID="$(sed -n 's/^MEM0_DEFAULT_USER_ID=//p' "$HOME/.mem0/stack.env" | head -n1)"
    [ -n "$MEM0_USER_ID" ] || MEM0_USER_ID="$(sed -n 's/^MEM0_WSL_USER=//p' "$HOME/.mem0/stack.env" | head -n1)"
    [ -n "$MEM0_USER_ID" ] && export MEM0_USER_ID
fi
# The embedding space travels with the stack too (mem0-server/embedder_profile.py): a chain step runs
# from a unit that sets none of it, and a python job that fell back to the default profile would read
# and write another space's collections than the server is bound to. stack.env first-wins by sed, the
# environment outranks it, and a box without the key exports nothing (the default space, as before).
if [ -z "${MEM0_EMBED_PROFILE:-}" ] && [ -s "$HOME/.mem0/stack.env" ]; then
    MEM0_EMBED_PROFILE="$(sed -n 's/^MEM0_EMBED_PROFILE=//p' "$HOME/.mem0/stack.env" | head -n1 | tr -d '\r')"
    if [ -n "$MEM0_EMBED_PROFILE" ]; then export MEM0_EMBED_PROFILE; else unset MEM0_EMBED_PROFILE; fi
fi
# $EPOCHREALTIME (bash >= 5) in microseconds: `date +%s%3N` is GNU-only; the uutils coreutils
# shipped on Ubuntu 26.04 ignores the width and prints nanoseconds (first live chain run
# receipted 1,834,879,975 ms for a 2 s step).
t0=${EPOCHREALTIME//[!0-9]/}
err="$(mktemp)"; outlog="$(mktemp)"; AMS_OUTCOME_FILE="$(mktemp)"; export AMS_OUTCOME_FILE
# stderr is captured synchronously, then echoed: a `2> >(tee …)` substitution is not waited
# for and the receipt read raced it (review 2026-09-10: 2 of 5000 lines landed in the note).
# stdout goes through a plain pipe into tee, which IS waited for: it streams to the journal as
# before and leaves a copy for the receipt (last line -> note when the outcome is not ok).
"$@" 2>"$err" | tee "$outlog"; rc=${PIPESTATUS[0]}
cat "$err" >&2
ms=$(( (${EPOCHREALTIME//[!0-9]/} - t0) / 1000 ))
okflag="$(finalize_receipt "$rc" "$ms" "$AMS_OUTCOME_FILE" "$err" "$outlog")"
if [ "$rc" -eq 0 ] && [ "$okflag" = true ]; then
    [ "$STAMP" = 1 ] && date +%s > "$stamp"
fi
rm -f "$err" "$outlog" "$AMS_OUTCOME_FILE"
exit "$rc"
