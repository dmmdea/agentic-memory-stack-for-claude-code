#!/usr/bin/env bash
# mutation-gate.sh - the local mutation gate for ams-store (blueprint section 10.2).
#
# A passing test suite proves the tests run. It does not prove they would NOTICE if a rule
# were lost. This gate asks the second question: for every merge rule, it makes the one
# minimal code change that breaks that rule and checks the named test goes RED.
#
# For each mutation in internal/porting/mutations.go it:
#
#   1. copies the target file aside (the pre-image)
#   2. applies the mutation                    (scripts/mutationgate apply <id>)
#   3. BUILDS the module - a mutation that does not compile is NOT a red test, it is a
#      broken mutation, and reporting it as red is exactly the lie this gate exists to
#      catch
#   4. runs the named test alone and requires it to FAIL
#   5. restores the file with `git checkout --` and VERIFIES the bytes came back
#
# A mutation whose test stays green is a rule nothing tests: the assertion was written
# against a seam, or against a value the mutated code happens to produce anyway.
#
# Before any of that it runs each named test UNMUTATED and requires it to PASS, because a
# test that was already failing would report every mutation red for the wrong reason.
#
# Step 5 verifies rather than trusts because the first run of this gate did not. Under WSL
# a Windows `git worktree` checkout carries a `.git` FILE holding a `D:\...` gitdir path,
# which Linux git cannot follow: every `git checkout --` failed silently, every mutation
# stayed in the tree, and the run stacked fifteen mutations on top of each other and
# reported four rules SURVIVED that had never been tested in isolation. A restore that can
# fail quietly is worse than no restore. Now git is still the restore - and the pre-image
# is the proof it worked, with a byte compare after every single one.
#
# This is a LOCAL gate, never a CI job: it is N+1 times the suite.
#
# Usage:
#   scripts/mutation-gate.sh                 every ready mutation
#   scripts/mutation-gate.sh --only <id>     one mutation (repeatable)
#   scripts/mutation-gate.sh --task 4        only the mutations owned by plan task 4
#   scripts/mutation-gate.sh --no-baseline   skip the unmutated pre-check (faster, weaker)
#
# Runs from anywhere; it locates the module itself. Needs bash and go on PATH.

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
    -h|--help) sed -n '2,45p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "mutation-gate: unknown argument $1" >&2; exit 2 ;;
  esac
done

command -v go >/dev/null 2>&1 || { echo "mutation-gate: go is not on PATH" >&2; exit 2; }

# Is git usable on THIS tree from THIS shell? A Windows worktree read from WSL is the case
# that is not, and the gate has to know before it starts, not after it has mutated a file.
GIT_OK=0
if command -v git >/dev/null 2>&1 && git -C "$MODULE_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  GIT_OK=1
fi

if [ "$GIT_OK" = "1" ]; then
  DIRTY="$(git -C "$MODULE_ROOT" status --porcelain -- . | grep -v '^?? ' || true)"
  if [ -n "$DIRTY" ]; then
    echo "mutation-gate: the ams-store work tree has uncommitted changes." >&2
    echo "This gate mutates tracked files in place, so it refuses to run while there is" >&2
    echo "work here it could be confused with. Commit or stash first." >&2
    echo "$DIRTY" >&2
    exit 2
  fi
else
  echo "mutation-gate: WARNING - git cannot read this tree from this shell." >&2
  echo "  (a Windows 'git worktree' checkout read from WSL is the usual cause: its .git" >&2
  echo "   file names a D:\\... gitdir Linux git will not follow)" >&2
  echo "  Every restore falls back to the pre-image copy and is still byte-verified," >&2
  echo "  but the uncommitted-changes pre-check could not run. Check 'git status' after." >&2
  echo >&2
fi

TABLE="$(go run ./scripts/mutationgate list)" || {
  echo "mutation-gate: could not read the mutation table" >&2; exit 2; }

WORK_DIR="$(mktemp -d)"
LOG_DIR="$WORK_DIR/logs"
PRE_DIR="$WORK_DIR/pre"
mkdir -p "$LOG_DIR" "$PRE_DIR"

CURRENT_FILES=""

# save_pre copies the files a mutation is about to touch. The copy is the ONLY thing that
# can prove the restore worked, so it is taken before anything is written.
save_pre() {
  local f
  for f in $1; do
    mkdir -p "$PRE_DIR/$(dirname "$f")"
    cp -p -- "$MODULE_ROOT/$f" "$PRE_DIR/$f" || return 1
  done
  return 0
}

# restore_verified puts the files back and PROVES it. git is the restore; the pre-image is
# the proof, and the fallback when git is not usable here. A restore that cannot be proved
# aborts the whole run: continuing would test a tree nobody can describe.
restore_verified() {
  local f failed=0
  [ -z "$CURRENT_FILES" ] && return 0
  if [ "$GIT_OK" = "1" ]; then
    # shellcheck disable=SC2086
    git -C "$MODULE_ROOT" checkout -- $CURRENT_FILES 2>/dev/null
  fi
  for f in $CURRENT_FILES; do
    if ! cmp -s -- "$MODULE_ROOT/$f" "$PRE_DIR/$f"; then
      cp -p -- "$PRE_DIR/$f" "$MODULE_ROOT/$f" || failed=1
      cmp -s -- "$MODULE_ROOT/$f" "$PRE_DIR/$f" || failed=1
    fi
  done
  CURRENT_FILES=""
  if [ "$failed" = "1" ]; then
    echo "mutation-gate: FATAL - could not restore a mutated file." >&2
    echo "The tree is left mutated. Pre-images: $PRE_DIR" >&2
    return 1
  fi
  return 0
}

on_exit() { restore_verified || true; }
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

if [ "$BASELINE" = "1" ]; then
  echo "== baseline: the named tests must PASS unmutated =="
  BASELINE_FAILED=0
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

record() { RESULTS="$RESULTS$1|$2|$3|$4"$'\n'; }

echo "== mutations =="
while IFS=$'\t' read -r id task state pkg test file rule; do
  if [ "$state" = "pending" ]; then
    if [ -z "$TASK" ] && [ ${#ONLY[@]} -eq 0 ]; then
      COUNT_PENDING=$((COUNT_PENDING + 1))
      record "$id" "$task" "PENDING" "$test"
    elif [ -n "$TASK" ] && [ "$task" = "$TASK" ]; then
      COUNT_PENDING=$((COUNT_PENDING + 1))
      record "$id" "$task" "PENDING" "$test"
    fi
    continue
  fi
  selected "$id" "$task" "$state" || continue

  FILES="$(go run ./scripts/mutationgate files "$id")" || {
    echo "mutation-gate: cannot list the files of $id" >&2; exit 2; }
  save_pre "$FILES" || { echo "mutation-gate: cannot save the pre-image of $id" >&2; exit 2; }
  CURRENT_FILES="$FILES"

  if ! go run ./scripts/mutationgate apply "$id" 2>"$LOG_DIR/apply.$id.log"; then
    printf '  %-26s STALE      %s\n' "$id" "$(cat "$LOG_DIR/apply.$id.log")"
    record "$id" "$task" "STALE" "$test"
    COUNT_BROKEN=$((COUNT_BROKEN + 1))
    restore_verified || exit 3
    continue
  fi

  # A mutation that does not compile proves nothing: the test binary never ran.
  if ! go build ./... >"$LOG_DIR/build.$id.log" 2>&1; then
    printf '  %-26s NOCOMPILE  %s\n' "$id" "$LOG_DIR/build.$id.log"
    record "$id" "$task" "NOCOMPILE" "$test"
    COUNT_BROKEN=$((COUNT_BROKEN + 1))
    restore_verified || exit 3
    continue
  fi

  if go test "$pkg" -run "^${test}\$" -count=1 >"$LOG_DIR/test.$id.log" 2>&1; then
    printf '  %-26s SURVIVED   %s (%s)\n' "$id" "$test" "$pkg"
    record "$id" "$task" "SURVIVED" "$test"
    COUNT_SURVIVED=$((COUNT_SURVIVED + 1))
  elif grep -q "warning: no tests to run" "$LOG_DIR/test.$id.log"; then
    # `go test` also exits non-zero when it ran NOTHING. A -run that matches no test is
    # not a red test, it is a typo in the table.
    printf '  %-26s NOTEST     %s matches nothing in %s\n' "$id" "$test" "$pkg"
    record "$id" "$task" "NOTEST" "$test"
    COUNT_BROKEN=$((COUNT_BROKEN + 1))
  else
    printf '  %-26s red        %s\n' "$id" "$test"
    record "$id" "$task" "RED" "$test"
    COUNT_RED=$((COUNT_RED + 1))
  fi
  restore_verified || exit 3
done <<< "$TABLE"

# ---------------------------------------------------------------- the table

echo
echo "== mutation gate =="
printf '%-26s %-4s %-10s %s\n' "MUTATION" "TASK" "RESULT" "TEST"
printf '%-26s %-4s %-10s %s\n' "--------------------------" "----" "----------" "----"
while IFS='|' read -r id task result test; do
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
rm -rf "$WORK_DIR"
exit 0
