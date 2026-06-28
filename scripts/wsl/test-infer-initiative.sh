#!/usr/bin/env bash
# Zero-dependency unit test for infer_initiative_from_cwd (claude-config/storage-cap-check.sh).
# bats is not installed on this box, so this is a plain-bash equivalent of the planned bats
# test: it EXTRACTS only the function (sourcing the whole SessionStart hook would fire its
# storage-cap side effects), then exercises the three branches and asserts.
#   Run: bash scripts/wsl/test-infer-initiative.sh   (exit 0 = pass, 1 = fail)
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SCRIPT="$REPO/claude-config/storage-cap-check.sh"
[ -f "$SCRIPT" ] || { echo "FATAL: $SCRIPT not found"; exit 1; }

# Extract ONLY the function definition (no hook side effects).
eval "$(sed -n '/^infer_initiative_from_cwd()/,/^}/p' "$SCRIPT")"

fail=0
check() { if [ "$2" = "$3" ]; then echo "  ok  : $1"; else echo "  FAIL: $1 -> expected [$2] got [$3]"; fail=1; fi; }

# 1) empty cwd -> empty (unscoped)
check "empty cwd -> ''" "" "$(infer_initiative_from_cwd "")"

# 2) cwd inside a git repo -> the repo-ROOT leaf (not the cwd leaf), so two initiatives that
#    share a brand don't bleed. Use this repo: a subdir resolves to the repo root's basename.
check "cwd in git repo -> repo-root leaf" "$(basename "$REPO")" "$(infer_initiative_from_cwd "$REPO/claude-config")"

# 3) cwd NOT in a git repo -> the cwd leaf
tmp="$(mktemp -d)"; mkdir -p "$tmp/initiative-leaf"
check "cwd outside git -> cwd leaf" "initiative-leaf" "$(infer_initiative_from_cwd "$tmp/initiative-leaf")"
rm -rf "$tmp"

if [ "$fail" -eq 0 ]; then echo "ALL PASS (3/3)"; exit 0; else echo "FAILURES"; exit 1; fi
