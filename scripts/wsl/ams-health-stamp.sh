#!/usr/bin/env bash
# ams-health-stamp.sh — pull /health/maintenance from the local authority and keep the last answer
# beside the receipts (the morning summary and Gatus read it; a failed pull is a failed step).
# The LAST line is the verdict (a red night's receipt note is the last stdout line): `health ok=<b> failed=<steps> degraded=<steps> pool <pct>% <health>`,
# plus ` write-path <last_error>` when the write path is failing (`write_path.ok` false: the last real memory write failed),
# then ` capture stalled <h>h` when `capture.stalled` is true (a PC's L1a finished no run for days while PC sessions went on): named, never red.
# It exits non-zero when a step's latest run failed, the pool is not ONLINE or the write path is failing, so the chain's last
# reading step is itself red on a bad night (a green step over an `ok=False` line is how a failed
# backup went unseen for two mornings). degraded steps are printed, and turn `ok` false, but stay exit 0.
# The exit reads `pool.health_alarm`, which the server clears while the operator's dated pool-health ack
# is active (MEM0_POOL_HEALTH_ACK): the line then ends `DEGRADED (acked until <date>)` and the step is green.
# A healthy write path (or an older server that has none, or a tracker it could not read: `ok` null) adds not one byte.
set -eu
url="$(cat "$HOME/.mem0/authority-url" 2>/dev/null || echo http://127.0.0.1:18791)"
out="$HOME/.mem0/maintenance/health-maintenance.json"; mkdir -p "$(dirname "$out")"
curl -sf -m 10 "${url%/}/health/maintenance" -o "$out.tmp" && mv "$out.tmp" "$out"
python3 - "$out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
pool = d.get("pool") or {}
def names(key):
    return ",".join(str(x.get("step")) for x in d.get(key) or []) or "-"
failed, degraded = names("failed_steps"), names("degraded_steps")
print("ok=%s stale=%s pool=%s%% dataset=%s%% usage=%s%% boots7d=%d" % (d.get("ok"), d.get("stale_steps"), pool.get("used_pct"), (d.get("dataset") or {}).get("used_pct"), (d.get("usage") or {}).get("used_percent"), len(d.get("boots_7d") or [])))
ack = pool.get("health_ack") or {}
acked = " (acked until %s)" % ack.get("until") if ack.get("active") else ""
wp = d.get("write_path") if isinstance(d.get("write_path"), dict) else {}
wp_red = wp.get("ok") is False
wp_note = " write-path " + " ".join(str(wp.get("last_error") or "failing").split()) if wp_red else ""
# A stalled capture (L1a finishing no runs while PC sessions go on) is named, never red: the PCs being off is not
# a chain fault, and a heads-up on its own already rides Gatus. Absent, quiet, unknown or malformed adds not one byte.
cap = d.get("capture") if isinstance(d.get("capture"), dict) else {}
cap_note = " capture stalled %sh" % cap.get("success_age_h") if cap.get("stalled") is True else ""
print("health ok=%s failed=%s degraded=%s pool %s%% %s%s%s%s" % (d.get("ok"), failed, degraded, pool.get("used_pct"), pool.get("health", "unknown"), acked, wp_note, cap_note))
sys.exit(2 if failed != "-" or pool.get("health_alarm") or wp_red else 0)
PY
