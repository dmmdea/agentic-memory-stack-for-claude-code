#!/usr/bin/env bash
# ams-pcloud-copy.sh — mirror the NEWEST stack-backup set to the pCloud path (spec §4: the pCloud
# copy is ordered after the mount; the Windows replicas refresh from it in Phase 2).
# Destination: $MEM0_PCLOUD_DIR > stack.env MEM0_PCLOUD_DIR > ~/pCloudDrive/memory-backups/<hostname>.
# A destination that is not a directory is a FAILED step (the receipt must say the off-box copy
# did not happen), never a quiet skip. Only the newest set travels: pCloud is a mirror of the
# latest restore point, the dataset and the ZFS replica hold the history.
set -euo pipefail
src="$HOME/.mem0/backups"
dst="${MEM0_PCLOUD_DIR:-}"
if [ -z "$dst" ] && [ -f "$HOME/.mem0/stack.env" ]; then
    dst="$(sed -n 's/^MEM0_PCLOUD_DIR=//p' "$HOME/.mem0/stack.env" | head -n1)"
fi
[ -n "$dst" ] || dst="$HOME/pCloudDrive/memory-backups/$(hostname)"
[ -d "$src" ] || { echo "pcloud-copy: no backup dir $src" >&2; exit 2; }
[ -d "$dst" ] || { echo "pcloud-copy: destination $dst is not a directory (mount missing?)" >&2; exit 3; }
newest="$(ls -1 "$src"/manifest-*.json 2>/dev/null | sort | tail -n1 || true)"
[ -n "$newest" ] || { echo "pcloud-copy: no manifest-*.json in $src" >&2; exit 4; }
stamp="$(basename "$newest" .json)"; stamp="${stamp#manifest-}"
n=0
for f in "$src"/*-"$stamp".*; do
    [ -f "$f" ] || continue
    if [ ! -f "$dst/$(basename "$f")" ] || ! cmp -s "$f" "$dst/$(basename "$f")"; then
        cp -f "$f" "$dst/.$(basename "$f").tmp" && mv -f "$dst/.$(basename "$f").tmp" "$dst/$(basename "$f")"
    fi
    n=$((n + 1))
done
echo "pcloud-copy: set $stamp -> $dst ($n file(s))"
