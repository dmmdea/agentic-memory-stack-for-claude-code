#!/usr/bin/env bash
# ams-health-stamp.sh — pull /health/maintenance from the local authority and keep the last answer
# beside the receipts (the morning summary and Gatus read it; a failed pull is a failed step).
# The first line is the verdict: `health ok=<b> failed=<steps> degraded=<steps> pool <pct>% <health>`.
# It exits non-zero when a step's latest run failed or the pool is not ONLINE, so the chain's last
# reading step is itself red on a bad night (a green step over an `ok=False` line is how a failed
# backup went unseen for two mornings). degraded steps are printed, and turn `ok` false, but stay exit 0.
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
print("health ok=%s failed=%s degraded=%s pool %s%% %s" % (d.get("ok"), failed, degraded, pool.get("used_pct"), pool.get("health", "unknown")))
print("ok=%s stale=%s pool=%s%% dataset=%s%% usage=%s%% boots7d=%d" % (d.get("ok"), d.get("stale_steps"), pool.get("used_pct"), (d.get("dataset") or {}).get("used_pct"), (d.get("usage") or {}).get("used_percent"), len(d.get("boots_7d") or [])))
sys.exit(2 if failed != "-" or pool.get("health_alarm") else 0)
PY
