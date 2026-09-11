#!/usr/bin/env bash
# wait-for-bind.sh <ipv4> [timeout_s] — ExecStartPre for the native authority (spec §4).
# A user unit cannot order after the system tailscaled unit, so mem0.service waits here until
# the bind address exists on some interface. Exit 0 the moment it does; exit 75 (EX_TEMPFAIL,
# systemd retries via Restart=on-failure) when the timeout passes without it. Never 0.0.0.0.
set -u
ip="${1:?usage: wait-for-bind.sh <ipv4> [timeout_s]}"; timeout="${2:-120}"
case "$ip" in 0.0.0.0|::|'') echo "wait-for-bind: refusing wildcard bind '$ip'" >&2; exit 78 ;; esac
for ((i = 0; i < timeout; i++)); do
    if ip -4 -o addr show 2>/dev/null | grep -q " inet ${ip}/"; then exit 0; fi
    sleep 1
done
echo "wait-for-bind: $ip not present after ${timeout}s" >&2
exit 75
