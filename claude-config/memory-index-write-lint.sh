#!/usr/bin/env bash
# memory-index-write-lint.sh - PostToolUse (Write|Edit) guard for the native auto-memory
# INDEX. The harness itself nags when the index passes its 200-LINE injection cap, but
# nothing checks BYTES PER LINE - and that is what actually fills the 25,000-byte per-file
# sync limit (a store hit 96% of the byte cap at only 64% of the line cap, because index
# lines had grown to ~190 B against a ~110 B hook format).
#
# This closes the loop at the SOURCE: the agent that just wrote an over-long line is told in
# the same turn, while the material is still in context, and fixes it - instead of a nightly
# job compacting the same bloat forever.
#
# Contract: advisory only. Prints to stdout and ALWAYS exits 0 (a hook that can block a write
# to memory is not worth the risk). No dependencies beyond coreutils; returns instantly for
# every path that is not a memory index.

set -u

CAP_LINE_BYTES="${AM_LINE_BYTE_CAP:-130}"
CAP_FILE_BYTES="${AM_SYNC_LIMIT_BYTES:-25000}"
CAP_LINES="${AM_INJECT_LIMIT_LINES:-200}"

payload="$(cat 2>/dev/null || true)"
[ -n "$payload" ] || exit 0

# Pull the written path out of the hook payload without requiring jq.
path="$(printf '%s' "$payload" | sed -n 's/.*"file_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
[ -n "$path" ] || exit 0

# Only ever act on a workspace memory index: <...>/projects/<workspace>/memory/MEMORY.md
case "$path" in
  *[/\\]memory[/\\]MEMORY.md|*/memory/MEMORY.md) : ;;
  *) exit 0 ;;
esac

# Normalise a Windows path (C:\Users\... or C:/Users/...) to the WSL/Git-Bash form.
file="$path"
case "$file" in
  [A-Za-z]:[/\\]*)
    drive="$(printf '%s' "$file" | cut -c1 | tr '[:upper:]' '[:lower:]')"
    rest="$(printf '%s' "$file" | cut -c3- | tr '\\' '/')"
    if [ -d /mnt/c ]; then file="/mnt/$drive$rest"; else file="/$drive$rest"; fi
    ;;
esac
[ -f "$file" ] || exit 0

total_bytes=$(wc -c < "$file" 2>/dev/null || echo 0)
total_lines=$(grep -c '' "$file" 2>/dev/null || echo 0)

# Report the offending lines: index entries whose UTF-8 byte length exceeds the hook budget.
over="$(awk -v cap="$CAP_LINE_BYTES" '
  /^- \[.*\]\(.*\.md\)/ { n = length($0); if (n > cap) { printf "  line %d: %d B  %s\n", NR, n, substr($0, 1, 60) } }
' "$file" 2>/dev/null)"
over_count=$(printf '%s' "$over" | grep -c '^' 2>/dev/null || echo 0)
[ -z "$over" ] && over_count=0

[ "$over_count" -eq 0 ] && [ "$total_bytes" -lt "$CAP_FILE_BYTES" ] && [ "$total_lines" -lt "$CAP_LINES" ] && exit 0

echo "[auto-memory index] ${total_bytes} B / ${total_lines} lines (caps: ${CAP_FILE_BYTES} B sync, ${CAP_LINES} lines injected)"
if [ "$total_bytes" -ge "$CAP_FILE_BYTES" ]; then
  echo "  OVER THE SYNC LIMIT: this file will not sync until it is split or compacted."
elif [ "$total_lines" -ge "$CAP_LINES" ]; then
  echo "  OVER THE INJECTION CAP: entries past line ${CAP_LINES} are not loaded into context."
fi
if [ "$over_count" -gt 0 ]; then
  echo "  ${over_count} index line(s) over ${CAP_LINE_BYTES} B. The index format is one pointer per entry:"
  echo "    - [Title](file.md) - hook"
  echo "  Keep the hook to the trigger for opening the file; detail belongs in the fact file itself."
  printf '%s\n' "$over" | head -n 8
  [ "$over_count" -gt 8 ] && echo "  ... and $((over_count - 8)) more"
fi
exit 0
