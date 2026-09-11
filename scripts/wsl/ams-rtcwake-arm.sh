#!/usr/bin/env bash
# ams-rtcwake-arm.sh [--dry-run] [HH:MM] — arm the RTC to wake the box at the next HH:MM (default
# 02:45, before the 03:00 chain). Each wake consumes the alarm, so the chain re-arms it as its last
# step. Proven 2026-09-10: `rtcwake -m no` sets /sys/class/rtc/rtc0/wakealarm and the box boots
# from S5 on it, twice, unattended. Needs passwordless sudo for rtcwake (present on the authority).
set -eu
DRY=0; [ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
at="${1:-02:45}"
target=$(date -d "today $at" +%s); now=$(date +%s)
[ "$target" -gt "$now" ] || target=$(date -d "tomorrow $at" +%s)
if [ "$DRY" = 1 ]; then echo "would arm rtcwake for $(date -d @"$target") epoch $target"; exit 0; fi
sudo -n rtcwake -m no -t "$target" >/dev/null
echo "rtcwake armed for $(date -d @"$target") epoch $(cat /sys/class/rtc/rtc0/wakealarm)"
