#!/usr/bin/env bash
# ams-pcloud-copy.sh — mirror the NEWEST stack-backup set to the pCloud path (spec §4: the pCloud
# copy is ordered after the mount; the Windows replicas refresh from it in Phase 2).
# Destination: $MEM0_PCLOUD_DIR > stack.env MEM0_PCLOUD_DIR > ~/pCloudDrive/memory-backups/<hostname>.
# A destination that is not a directory is a FAILED step (the receipt must say the off-box copy
# did not happen), never a quiet skip.
#
# What the copy guarantees (exit codes: 2 no source, 3 mount missing, 4 no manifest, 5 stale or
# failed source set, 6 copy did not verify):
#   - FRESH: it refuses to mirror a set whose manifest is older than AMS_PCLOUD_MAX_AGE_H (26 h)
#     or whose stack-backup receipt is not ok. Re-copying the newest existing set on the night
#     stack-backup failed made the step read green while the cloud fell a day behind.
#   - ORDERED: data files first, the manifest LAST. A set is complete only when its manifest
#     exists, so an interrupted copy leaves an incomplete set, never a manifest that points at
#     missing files.
#   - VERIFIED: every file's size in the cloud must equal the local one before the manifest travels.
#   - RETAINED: the newest AMS_PCLOUD_KEEP_SETS (7) COMPLETE sets are kept and older sets are
#     deleted; the newest complete set is never deleted. Retention runs only after a verified copy.
# Only real artifacts travel (the extension list below): SQLite -wal/-shm sidecars and .tmp files
# never do. On refusal the step also writes the outcome line ams-step.sh reads (AMS_OUTCOME_FILE).
set -euo pipefail
src="$HOME/.mem0/backups"
dst="${MEM0_PCLOUD_DIR:-}"
if [ -z "$dst" ] && [ -f "$HOME/.mem0/stack.env" ]; then
    dst="$(sed -n 's/^MEM0_PCLOUD_DIR=//p' "$HOME/.mem0/stack.env" | head -n1)"
fi
[ -n "$dst" ] || dst="$HOME/pCloudDrive/memory-backups/$(hostname)"
max_age_h="${AMS_PCLOUD_MAX_AGE_H:-26}"
keep="${AMS_PCLOUD_KEEP_SETS:-7}"
case "$keep" in ''|*[!0-9]*) keep=7 ;; esac
[ "$keep" -ge 1 ] || keep=1   # never delete the newest complete set

[ -d "$src" ] || { echo "pcloud-copy: no backup dir $src" >&2; exit 2; }
# The leaf (memory-backups/<host>) is ours to create; its PARENT must already exist — that is
# what proves the cloud drive is mounted (an unmounted mount point has no memory-backups dir).
if [ ! -d "$dst" ]; then
    [ -d "$(dirname "$dst")" ] || { echo "pcloud-copy: destination parent $(dirname "$dst") is not a directory (mount missing?)" >&2; exit 3; }
    mkdir -p "$dst"
fi
newest="$(ls -1 "$src"/manifest-*.json 2>/dev/null | sort | tail -n1 || true)"
[ -n "$newest" ] || { echo "pcloud-copy: no manifest-*.json in $src" >&2; exit 4; }
stamp="$(basename "$newest" .json)"; stamp="${stamp#manifest-}"

refuse() {  # <reason> <json counts> <message>
    echo "pcloud-copy: REFUSING set $stamp: $3" >&2
    [ -n "${AMS_OUTCOME_FILE:-}" ] && printf '%s %s\n' "failed:$1" "$2" > "$AMS_OUTCOME_FILE"
    exit "${4:-5}"
}

# --- freshness: the newest local manifest, and stack-backup's latest receipt
age_h=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 ))
if [ "$age_h" -ge "$max_age_h" ]; then
    refuse stale-set "{\"age_h\":$age_h}" "newest manifest is ${age_h} h old (limit ${max_age_h} h) - stack-backup did not produce a set"
fi
receipts="$HOME/.mem0/maintenance/receipts.jsonl"
if [ -s "$receipts" ]; then
    last_ok="$(python3 - "$receipts" <<'PYRECEIPT'
import json, sys
ok = None
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("step") == "stack-backup":
        ok = r.get("ok")
print("yes" if ok is True or ok is None else "no")
PYRECEIPT
)"
    if [ "$last_ok" != "yes" ]; then
        refuse stack-backup-not-ok "{\"age_h\":$age_h}" "stack-backup's latest receipt is not ok"
    fi
fi

# --- copy: data files first, the manifest last, sizes verified before the manifest travels
copy_one() {  # <file>
    local f="$1" b
    b="$(basename "$f")"
    if [ ! -f "$dst/$b" ] || ! cmp -s "$f" "$dst/$b"; then
        cp -f "$f" "$dst/.$b.tmp" && mv -f "$dst/.$b.tmp" "$dst/$b"
    fi
    if [ "$(stat -c %s "$f")" != "$(stat -c %s "$dst/$b")" ]; then
        refuse copy-size-mismatch "{\"file\":\"$b\"}" "size of $b in the cloud differs from the local copy - manifest not copied, nothing pruned" 6
    fi
}
n=0
for f in "$src"/*-"$stamp".*; do
    [ -f "$f" ] || continue
    case "$f" in
        "$src"/manifest-*) continue ;;
        *.db|*.jsonl|*.snapshot|*.md|*.baseline|*.json|*.tar) ;;
        *) continue ;;   # -wal/-shm sidecars, .tmp partials, anything unknown
    esac
    copy_one "$f"
    n=$((n + 1))
done
copy_one "$newest"
n=$((n + 1))
echo "pcloud-copy: set $stamp -> $dst ($n file(s), age ${age_h} h)"

# --- retention: keep the newest $keep COMPLETE sets (a set is complete when its manifest exists)
mapfile -t complete < <(ls -1 "$dst"/manifest-*.json 2>/dev/null | sed 's#.*/manifest-##; s#\.json$##' | sort -r)
if [ "${#complete[@]}" -ge 1 ]; then
    newest_complete="${complete[0]}"
    for old in "${complete[@]:$keep}"; do
        [ "$old" = "$newest_complete" ] && continue
        rm -f "$dst/manifest-$old.json"   # manifest first: the set turns incomplete before its files go
        rm -f "$dst"/*-"$old".*
        echo "pcloud-copy: retention removed set $old"
    done
    # a dead partial (data files, no manifest) older than the newest complete set is debris; a
    # partial NEWER than it may be a copy still in flight and is left alone
    for f in "$dst"/*-[0-9]*-[0-9]*.*; do
        [ -f "$f" ] || continue
        s="$(basename "$f")"; s="${s%.*}"; s="${s: -15}"
        case "$s" in [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) ;; *) continue ;; esac
        [ -e "$dst/manifest-$s.json" ] && continue
        if [[ "$s" < "$newest_complete" ]]; then
            rm -f "$f"
            echo "pcloud-copy: retention removed partial $(basename "$f")"
        fi
    done
fi
