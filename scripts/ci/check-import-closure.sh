#!/usr/bin/env bash
# check-import-closure.sh — the installer import-closure gate.
#
# Every module in mem0-server/*.py must be listed in MEM0_MODULES in
# install/1-wsl-services.sh. When one is missing the installer never copies it and
# fresh installs crash-loop (CLAUDE.md, "Working in this repo").
#
# Pure bash: no live stack, no third-party deps. Runs locally the same as in CI.
set -euo pipefail

INSTALLER="install/1-wsl-services.sh"
SERVER_DIR="mem0-server"

[ -f "$INSTALLER" ] || { echo "::error::$INSTALLER not found (run from the repo root)"; exit 1; }
[ -d "$SERVER_DIR" ] || { echo "::error::$SERVER_DIR/ not found (run from the repo root)"; exit 1; }

declared=$(grep -oE 'MEM0_MODULES="[^"]*"' "$INSTALLER" \
  | head -1 \
  | sed -E 's/^MEM0_MODULES="//; s/"$//' \
  | tr ' ' '\n' | sed '/^$/d' | sort -u)

if [ -z "$declared" ]; then
  echo "::error::could not parse MEM0_MODULES from $INSTALLER"
  exit 1
fi

# Test helpers are not shipped by the installer, so they are not part of the closure.
actual=$(find "$SERVER_DIR" -maxdepth 1 -name '*.py' -printf '%f\n' \
  | grep -vE '^(test_.*|conftest)\.py$' | sort -u)

missing=$(comm -13 <(printf '%s\n' "$declared") <(printf '%s\n' "$actual") || true)
stale=$(comm -23 <(printf '%s\n' "$declared") <(printf '%s\n' "$actual") || true)

status=0

if [ -n "$missing" ]; then
  status=1
  echo "::error::module(s) present in $SERVER_DIR/ but missing from MEM0_MODULES —"
  echo "::error::the installer would not copy them and fresh installs would crash-loop:"
  printf '%s\n' "$missing" | sed 's/^/  - /'
fi

if [ -n "$stale" ]; then
  status=1
  echo "::error::module(s) listed in MEM0_MODULES but absent from $SERVER_DIR/:"
  printf '%s\n' "$stale" | sed 's/^/  - /'
fi

if [ "$status" -eq 0 ]; then
  echo "import closure OK — $(printf '%s\n' "$declared" | wc -l) modules declared and present."
fi

exit "$status"
