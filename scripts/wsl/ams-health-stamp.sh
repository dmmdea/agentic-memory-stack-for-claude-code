#!/usr/bin/env bash
# ams-health-stamp.sh — pull /health/maintenance from the local authority and keep the last answer
# beside the receipts (the morning summary and Gatus read it; a failed pull is a failed step).
set -eu
url="$(cat "$HOME/.mem0/authority-url" 2>/dev/null || echo http://127.0.0.1:18791)"
out="$HOME/.mem0/maintenance/health-maintenance.json"; mkdir -p "$(dirname "$out")"
curl -sf -m 10 "${url%/}/health/maintenance" -o "$out.tmp" && mv "$out.tmp" "$out"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("ok=%s stale=%s pool=%s%% dataset=%s%% usage=%s%% boots7d=%d" % (d["ok"], d["stale_steps"], d["pool"]["used_pct"], (d.get("dataset") or {}).get("used_pct"), (d.get("usage") or {}).get("used_percent"), len(d["boots_7d"])))' "$out"
