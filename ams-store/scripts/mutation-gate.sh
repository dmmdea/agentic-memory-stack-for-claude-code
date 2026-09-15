#!/usr/bin/env bash
# mutation-gate.sh - the local mutation gate for ams-store (blueprint section 10.2).
#
# A passing test suite proves the tests run. It does not prove they would NOTICE if a rule
# were lost. This gate asks the second question: for every merge rule, it makes the one
# minimal code change that breaks that rule and checks the named test goes RED.
#
# For each mutation in internal/porting/mutations.go it:
#
#   1. applies the mutation to the work tree   (scripts/mutationgate apply <id>)
#   2. BUILDS the module - a mutation that does not compile is NOT a red test, it is a
#      broken mutation, and reporting it as red is exactly the lie this gate exists to
#      catch
#   3. runs the named test alone and requires it to FAIL
#   4. restores the file from git
#
# A mutation whose test stays green is a rule nothing tests: the assertion was written
# against a seam, or against a value the mutated code happens to produce anyway.
#
# Before any of that it runs each named test UNMUTATED and requires it to PASS, because a
# test that was already failing would report every mutation red for the wrong reason.
#
# This is a LOCAL gate, never a CI job: it is N+1 times the suite.
#
# Usage:
#   scripts/mutation-gate.sh                 every ready mutation
#   scripts/mutation-gate.sh --only <id>     one mutation (repeatable)
#   scripts/mutation-gate.sh --task 4        only the mutations owned by plan task 4
#   scripts/mutation-gate.sh --no-baseline   skip the unmutated pre-check (faster, weaker)
#
# Runs from anywhere; it locates the module itself. Needs bash, git and go on PATH.

set -u -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MODULE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$MODULE_ROOT" || exit 2

ONLY=()
TASK=""
BASELINE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY+=("${2:?--only needs a mutation id}"); shift 2 ;;
    --task) TASK="${2:?--task needs a task number}"; shift 2 ;;
    --no-baseline) BASELINE=0; shift ;;
    -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "mutation-gate: unknown argument $1" >&2; exit 2 ;;
  esac
done

command -v go  >/dev/null 2>&1 || { echo "mutation-gate: go is not on PATH" >&2; exit 2; }
command -v git >/dev/null 2>&1 || { echo "mutation-gate: git is not on PATH" >&2; exit 2; }

# The gate restores mutated files with git, so it refuses to start on a dirty tree: it
# must never be the thing that discarded uncommitted work.
DIRTY="$(git -C "$MODULE_ROOT" status --porcelain -- . | grep -v '^?? ' || true)"
if [ -n "$DIRTY" ]; then
  echo "mutation-gate: the ams-store work tree has uncommitted changes." >&2
  echo "This gate restores every file it mutates from git, so it refuses to run" >&2
  echo "while there is work here it could destroy. Commit or stash first." >&2
  echo "$DIRTY" >&2
  exit 2
fi

TABLE="$(go run ./scripts/mutationgate list)" || {
  echo "mutation-gate: could not read the mutation table" >&2; exit 2; }

LOG_DIR="$(mktemp -d)"
RESTORE_LIST=""

restore_all() {
  # shellcheck disable=SC2086
  [ -n "$RESTORE_LIST" ] && git -C "$MODULE_ROOT" checkout -- $RESTORE_LIST 2>/dev/null
  RESTORE_LIST=""
}
on_exit() { restore_all; }
trap on_exit EXIT INT TERM

# ---------------------------------------------------------------- selection

selected() {
  local id="$1" task="$2" state="$3"
  [ "$state" = "ready" ] || return 1
  if [ -n "$TASK" ] && [ "$task" != "$TASK" ]; then return 1; fi
  if [ ${#ONLY[@]} -gt 0 ]; then
    local want
    for want in "${ONLY[@]}"; do [ "$want" = "$id" ] && return 0; done
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------- baseline

BASELINE_FAILED=0
if [ "$BASELINE" = "1" ]; then
  echo "== baseline: the named tests must PASS unmutated =="
  SEEN=""
  while IFS=$'\t' read -r id task state pkg test file rule; do
    selected "$id" "$task" "$state" || continue
    case " $SEEN " in *" $pkg:$test "*) continue ;; esac
    SEEN="$SEEN $pkg:$test"
    if go test "$pkg" -run "^${test}\$" -count=1 >"$LOG_DIR/base.$test.log" 2>&1; then
      printf '  PASS  %s %s\n' "$pkg" "$test"
    else
      printf '  FAIL  %s %s  (see %s)\n' "$pkg" "$test" "$LOG_DIR/base.$test.log"
      BASELINE_FAILED=1
    fi
  done <<< "$TABLE"
  if [ "$BASELINE_FAILED" = "1" ]; then
    echo "mutation-gate: a named test fails before any mutation; the run would be meaningless." >&2
    exit 1
  fi
  echo
fi

# ---------------------------------------------------------------- the run

RESULTS=""
COUNT_RED=0
COUNT_SURVIVED=0
COUNT_BROKEN=0
COUNT_PENDING=0

echo "== mutations =="
while IFS=$'\t' read -r id task state pkg test file rule; do
  if [ "$state" = "pending" ]; then
    if [ -z "$TASK" ] || [ "$task" = "$TASK" ]; then
      COUNT_PENDING=$((COUNT_PENDING + 1))
      RESULTS="$RESULTS$id|$task|PENDING|$test|$rule"$'\n'
    fi
    continue
  fi
  selected "$id" "$task" "$state" || continue

  FILES="$(go run ./scripts/mutationgate files "$id")" || { echo "  !! $id: cannot list files" >&2; exit 2; }
  RESTORE_LIST="$FILES"

  if ! go run ./scripts/mutationgate apply "$id" 2>"$LOG_DIR/apply.$id.log"; then
    printf '  %-26s STALE      %s\n' "$id" "$(cat "$LOG_DIR/apply.$id.log")"
    RESULTS="$RESULTS$id|$task|STALE|$test|$rule"$'\n'
    COUNT_BROKEN=$((COUNT_BROKEN + 1))
    restore_all
    continue
  fi

  # A mutation that does not compile proves nothing: the test binary never ran.
  if ! go build ./... >"$LOG_DIR/build.$id.log" 2>&1; then
    printf '  %-26s NOCOMPILE  %s\n' "$id" "$LOG_DIR/build.$id.log"
    RESULTS="$RESULTS$id|$task|NOCOMPILE|$test|$rule"$'\n'
    COUNT_BROKEN=$((COUNT_BROKEN + 1))
    restore_all
    continue
  fi

  if go test "$pkg" -run "^${test}\$" -count=1 >"$LOG_DIR/test.$id.log" 2>&1; then
    printf '  %-26s SURVIVED   %s (%s)\n' "$id" "$test" "$pkg"
    RESULTS="$RESULTS$id|$task|SURVIVED|$test|$rule"$'\n'
    COUNT_SURVIVED=$((COUNT_SURVIVED + 1))
  else
    # `go test` also exits non-zero when it ran NOTHING. A -run that matches no test is
    # not a red test, it is a typo in the table.
    if grep -q "warning: no tests to run" "$LOG_DIR/test.$id.log"; then
      printf '  %-26s NOTEST     %s matches nothing in %s\n' "$id" "$test" "$pkg"
      RESULTS="$RESULTS$id|$task|NOTEST|$test|$rule"$'\n'
      COUNT_BROKEN=$((COUNT_BROKEN + 1))
    else
      printf '  %-26s red        %s\n' "$id" "$test"
      RESULTS="$RESULTS$id|$task|RED|$test|$rule"$'\n'
      COUNT_RED=$((COUNT_RED + 1))
    fi
  fi
  restore_all
done <<< "$TABLE"

# ---------------------------------------------------------------- the table

echo
echo "== mutation gate =="
printf '%-26s %-4s %-10s %s\n' "MUTATION" "TASK" "RESULT" "TEST"
printf '%-26s %-4s %-10s %s\n' "--------------------------" "----" "----------" "----"
while IFS='|' read -r id task result test rule; do
  [ -z "$id" ] && continue
  printf '%-26s %-4s %-10s %s\n' "$id" "$task" "$result" "$test"
done <<< "$RESULTS"

echo
echo "red $COUNT_RED   survived $COUNT_SURVIVED   broken $COUNT_BROKEN   pending $COUNT_PENDING"

if [ "$COUNT_SURVIVED" -gt 0 ] || [ "$COUNT_BROKEN" -gt 0 ]; then
  echo
  echo "A SURVIVED rule is untested; a NOCOMPILE/STALE/NOTEST entry is a broken mutation."
  echo "Logs: $LOG_DIR"
  exit 1
fi
rm -rf "$LOG_DIR"
exit 0
