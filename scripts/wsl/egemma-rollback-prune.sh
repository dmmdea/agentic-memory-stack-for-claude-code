#!/usr/bin/env bash
# egemma-rollback-prune.sh — profile-gated cleanup of the PREVIOUS embedding space after a migration.
#
# A migration (scripts/wsl/embedder-migrate.py) builds the new space's collections BESIDE the old
# ones, so the old space stays a rollback anchor for as long as the operator wants it. This script
# is what finally deletes it: the OLD profile's collections (memories, entities, episodes, wiki;
# mem0-server/embedder_profile.py names them, nothing here does) and their server-side snapshots.
# It began as the v0.22 one-shot that removed the nomic `memories` collection after the EmbeddingGemma
# move; the gate is the same idea, now stated in terms of profiles.
#
# NEVER ARMED BY AN INSTALLER. The timer ships disabled with a placeholder date; the operator arms
# it after the rollback window (systemctl --user edit egemma-rollback-prune.timer to set OnCalendar,
# then enable --now). Disabling it is STEP 1 of any rollback (belt and braces; the gate below also
# refuses).
#
# HEALTH-GATED: it prunes ONLY if the new space is confirmed live and healthy:
#   - mem0's /health/deep reports embed_profile.profile == the NEW profile AND a bound collection
#     equal to the NEW memories collection (the binding is the truth: after a rollback the old
#     collections still exist and a pure "new collection is green" check would stay green);
#   - the embedder probe is ok at the new profile's width, and the new memories collection is green
#     with at least EGEMMA_PRUNE_MIN_POINTS points.
# Each old collection is deleted only if its counterpart in the new space exists, is green and holds
# points; one that fails is kept and logged. Nothing is ever deleted that carries the name of a new
# collection or of the bound one. If anything looks off it SKIPs, logs a warning to the audit flags
# and leaves everything intact. After a run that left nothing behind it disables its own timer.
#
# Defaults describe the planned migration: NEW egemma2, OLD egemma-300m. A box that never migrated
# runs on egemma-300m, so the gate reads profile=egemma-300m != egemma2 and SKIPs: fail-safe.
#
# TESTABILITY: endpoints are env-overridable and EGEMMA_PRUNE_DRY_RUN=1 prints
# "DECISION: PRUNE|SKIP ..." and exits BEFORE any deletion (used by the committed gate test
# test_egemma_rollback_prune.py — verifies skip-on-rollback without touching live data).
#
# Env: EGEMMA_PRUNE_NEW_PROFILE, EGEMMA_PRUNE_OLD_PROFILE, EGEMMA_PRUNE_EXPECTED_COLLECTION (the new
# memories collection, default: the new profile's), EGEMMA_PRUNE_OLD_COLLECTIONS (space-separated; default:
# the old profile's four), EGEMMA_PRUNE_MIN_POINTS (1000), EGEMMA_PRUNE_SNAPSHOT_DIR
# (~/qdrant-server/snapshots), EGEMMA_PRUNE_MEM0_URL, EGEMMA_PRUNE_QDRANT_URL, EGEMMA_PRUNE_LOG,
# EGEMMA_PRUNE_AUDIT_FLAGS, EGEMMA_PRUNE_DRY_RUN.
set +e
MEM0_URL="${EGEMMA_PRUNE_MEM0_URL:-http://localhost:18791}"
QDRANT_URL="${EGEMMA_PRUNE_QDRANT_URL:-http://localhost:6333}"
NEW_PROFILE="${EGEMMA_PRUNE_NEW_PROFILE:-egemma2}"
OLD_PROFILE="${EGEMMA_PRUNE_OLD_PROFILE:-egemma-300m}"
MIN_POINTS="${EGEMMA_PRUNE_MIN_POINTS:-1000}"
SNAP_ROOT="${EGEMMA_PRUNE_SNAPSHOT_DIR:-$HOME/qdrant-server/snapshots}"
LOG="${EGEMMA_PRUNE_LOG:-$HOME/.mem0/egemma-rollback-prune.log}"
AUDIT_FLAGS="${EGEMMA_PRUNE_AUDIT_FLAGS:-$HOME/.mem0/audit-flags.jsonl}"
DRY_RUN="${EGEMMA_PRUNE_DRY_RUN:-0}"
mkdir -p "$(dirname "$LOG")" "$(dirname "$AUDIT_FLAGS")"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
echo "[$(ts)] egemma-rollback-prune START (dry_run=$DRY_RUN new=$NEW_PROFILE old=$OLD_PROFILE)" >> "$LOG"

json_str() { printf '%s' "$1" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '"skip"'; }

# SKIP <reason> [bound collection] [bound profile]: log it, flag it for the audit, say so, stop.
skip() {
  local reason="$1" bound="${2:-}" bprofile="${3:-}"
  echo "[$(ts)] SKIP — $reason" >> "$LOG"
  printf '{"ts":"%s","event":"egemma-rollback-prune-skipped","reason":%s,"bound_collection":%s,"bound_profile":%s,"deep":%s}\n' \
    "$(ts)" "$(json_str "$reason")" \
    "$(printf '%s' "${bound:-null}" | python3 -c 'import sys,json;s=sys.stdin.read().strip();print(json.dumps(s) if s and s!="null" else "null")' 2>/dev/null || echo 'null')" \
    "$(printf '%s' "${bprofile:-null}" | python3 -c 'import sys,json;s=sys.stdin.read().strip();print(json.dumps(s) if s and s!="null" else "null")' 2>/dev/null || echo 'null')" \
    "$(echo "${DEEP:-null}" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read().strip() or "null"))' 2>/dev/null || echo '"null"')" \
    >> "$AUDIT_FLAGS"
  echo "DECISION: SKIP — $reason"
  exit 0
}

# The profiles' collections come from embedder_profile.py (mem0-server): found repo-relative first
# (a checkout, the tests), then in the deployed server directory. This script is deployed to ~/.mem0/,
# alone, so it carries its own small resolver.
ep_py() {  # <python> [args...]; rc 2 = the module cannot be found
  local d
  for d in "$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)/mem0-server" "$HOME/apps/mem0-server"; do
    [ -f "$d/embedder_profile.py" ] || continue
    python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import embedder_profile as ep; del sys.argv[1]
'"$1" "$d" "${@:2}"
    return $?
  done
  return 2
}

[ "$NEW_PROFILE" != "$OLD_PROFILE" ] || skip "new and old embedding profile are the same ($NEW_PROFILE); nothing to prune"
# name<TAB>value lines: the new space's four collections (the bound memories one honouring the operator's
# override, like the server), the old space's four (their own names: an override names the BOUND one),
# and the width the embedder probe must report.
PROFILE_TABLE=$(ep_py '
new, old = ep.get(sys.argv[1]), ep.get(sys.argv[2])
for k in ("memories", "entities", "episodes", "wiki"):
    print("new_" + k, ep.collection(k, new), sep="\t")
    print("old_" + k, getattr(old, k), sep="\t")
print("dims", new.dims, sep="\t")
' "$NEW_PROFILE" "$OLD_PROFILE" 2>>"$LOG")
[ -n "$PROFILE_TABLE" ] || skip "could not resolve embedding profiles '$NEW_PROFILE' / '$OLD_PROFILE' (embedder_profile.py missing, or an unknown name); refusing to guess which collections to delete"
tv() { printf '%s\n' "$PROFILE_TABLE" | awk -F'\t' -v k="$1" '$1==k {print $2; exit}'; }

EXPECTED_COLLECTION="${EGEMMA_PRUNE_EXPECTED_COLLECTION:-$(tv new_memories)}"
DIMS="$(tv dims)"
# the collections to delete, each paired with its counterpart in the new space ("old:new")
PAIRS=""
if [ -n "${EGEMMA_PRUNE_OLD_COLLECTIONS:-}" ]; then
  # an explicit list has no pairing the profile can supply: each is checked against the expected memories collection
  for c in $EGEMMA_PRUNE_OLD_COLLECTIONS; do PAIRS="$PAIRS $c:$EXPECTED_COLLECTION"; done
else
  for k in memories entities episodes wiki; do
    n="$(tv new_$k)"; [ "$k" = memories ] && n="$EXPECTED_COLLECTION"
    PAIRS="$PAIRS $(tv old_$k):$n"
  done
fi

qpoints() {  # <collection> -> "<points> <status>", empty when the collection does not exist
  curl -sf "$QDRANT_URL/collections/$1" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin)["result"];print(d.get("points_count",0),d.get("status",""))' 2>/dev/null
}

DEEP=$(curl -sf "$MEM0_URL/health/deep")
NEW=$(qpoints "$EXPECTED_COLLECTION")
echo "[$(ts)] deep=$DEEP | new($EXPECTED_COLLECTION)=$NEW | old pairs:$PAIRS" >> "$LOG"

NEWPTS=$(echo "$NEW" | awk '{print $1}')
NEWSTATUS=$(echo "$NEW" | awk '{print $2}')

# v0.22 H2: the collection and the embedding space mem0 is ACTUALLY bound to at runtime.
# /health/deep reports them ("collection":"<name>", "embed_profile":{"profile":"<name>",...}) from the
# live Memory instance. This is the decisive rollback detector: the old artifact-based gate (dim:768 +
# the new collection green) stays GREEN even after a rollback (the new collection still exists and its
# model is still served on :11436), so it could delete the old collections out from under a
# rolled-back stack now writing to them. Binding is the truth.
BOUND=$(echo "$DEEP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("collection") or "")' 2>/dev/null)
BOUND_PROFILE=$(echo "$DEEP" | python3 -c 'import sys,json;print((json.load(sys.stdin).get("embed_profile") or {}).get("profile") or "")' 2>/dev/null)
# Every collection the server reports in use: its space's collections and the LIVE wiki, which has its
# own space (MEM0_WIKI_EMBED_PROFILE) and may be the "old" space's wiki name. None of them is ever deleted.
IN_USE=$(echo "$DEEP" | python3 -c '
import sys, json
d = json.load(sys.stdin); ep = d.get("embed_profile") or {}
names = set((ep.get("collections") or {}).values())
names.add((ep.get("wiki") or {}).get("collection") or "")
names.add(d.get("collection") or "")
print("\n".join(sorted(n for n in names if n)))' 2>/dev/null)
echo "[$(ts)] bound collection=$BOUND profile=$BOUND_PROFILE (expected $EXPECTED_COLLECTION / $NEW_PROFILE)" >> "$LOG"

# HEALTH GATE: mem0 embedder ok at the new width AND mem0 is STILL BOUND to the new profile and its
# memories collection (NOT rolled back) AND the new memories collection is green with enough points.
if ! echo "$DEEP" | grep -q "\"dim\":$DIMS" \
   || [ "$BOUND" != "$EXPECTED_COLLECTION" ] || [ "$BOUND_PROFILE" != "$NEW_PROFILE" ] \
   || [ -z "$NEWPTS" ] || [ "$NEWPTS" -lt "$MIN_POINTS" ] || [ "$NEWSTATUS" != "green" ]; then
  REASON="migration unhealthy or rolled back; verify before manual prune"
  if [ -z "$DEEP" ]; then
    REASON="mem0 /health/deep did not answer; refusing to delete anything without the binding"
  elif [ "$BOUND" != "$EXPECTED_COLLECTION" ] || [ "$BOUND_PROFILE" != "$NEW_PROFILE" ]; then
    REASON="ROLLBACK DETECTED — mem0 is bound to collection '$BOUND' (profile '${BOUND_PROFILE:-none reported}'), not $EXPECTED_COLLECTION (profile $NEW_PROFILE); refusing to delete the rollback anchor. Disable this timer (systemctl --user disable egemma-rollback-prune.timer) as step 1 of any rollback."
  fi
  skip "$REASON" "$BOUND" "$BOUND_PROFILE"
fi

# What would go: every old collection that exists, is not a new-space name or the bound one, and whose
# counterpart in the new space is green and non-empty. The rest are KEPT and logged.
TO_DELETE=""; KEPT=""
for pair in $PAIRS; do
  old="${pair%%:*}"; newc="${pair#*:}"
  case "$old" in ""|*[!A-Za-z0-9._-]*) KEPT="$KEPT $old(bad-name)"; continue ;; esac
  if [ "$old" = "$BOUND" ] || [ "$old" = "$EXPECTED_COLLECTION" ] || printf '%s\n' "$PROFILE_TABLE" | awk -F'\t' '$1 ~ /^new_/ {print $2}' | grep -qxF "$old"; then
    KEPT="$KEPT $old(is-a-new-space-name)"; echo "[$(ts)] KEEP $old — it is the bound or a new-space collection" >> "$LOG"; continue
  fi
  if printf '%s\n' "$IN_USE" | grep -qxF "$old"; then
    KEPT="$KEPT $old(in-use)"; echo "[$(ts)] KEEP $old — the server reports it in use (e.g. the live wiki)" >> "$LOG"; continue
  fi
  oldstat=$(qpoints "$old")
  [ -n "$oldstat" ] || { echo "[$(ts)] $old already gone" >> "$LOG"; continue; }
  nstat=$(qpoints "$newc"); npts=$(echo "$nstat" | awk '{print $1}'); nst=$(echo "$nstat" | awk '{print $2}')
  if [ -z "$npts" ] || [ "$npts" -lt 1 ] || [ "$nst" != "green" ]; then
    KEPT="$KEPT $old(new-counterpart-$newc-not-ready)"; echo "[$(ts)] KEEP $old — its counterpart $newc is missing, empty or not green ($nstat)" >> "$LOG"; continue
  fi
  TO_DELETE="$TO_DELETE $old"
done

if [ "$DRY_RUN" = "1" ]; then
  echo "[$(ts)] DRY RUN — gate PASSED (would prune$TO_DELETE; keep$KEPT); no deletion performed." >> "$LOG"
  echo "DECISION: PRUNE — migration healthy (profile=$BOUND_PROFILE bound=$BOUND, new=$NEWPTS green); would delete:${TO_DELETE:- nothing} + their snapshots; kept:${KEPT:- nothing}."
  exit 0
fi

echo "[$(ts)] migration healthy (new=$NEWPTS green). Pruning$TO_DELETE and their snapshots." >> "$LOG"
for old in $TO_DELETE; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$QDRANT_URL/collections/$old")
  echo "[$(ts)] DELETE $old -> HTTP $code" >> "$LOG"
  case "$code" in 2*) ;; *) KEPT="$KEPT $old(delete-http-$code)"; continue ;; esac
  SNAPDIR="$SNAP_ROOT/$old"
  if [ -d "$SNAPDIR" ]; then
    rm -f "$SNAPDIR"/"$old"-*.snapshot "$SNAPDIR"/"$old"-*.snapshot.checksum && echo "[$(ts)] removed snapshots in $SNAPDIR" >> "$LOG"
    rmdir "$SNAPDIR" 2>/dev/null
  fi
done

# disable our own timer once nothing is left to prune (a kept collection keeps it armed for a retry)
if [ -z "$KEPT" ]; then
  systemctl --user disable egemma-rollback-prune.timer >> "$LOG" 2>&1
  echo "[$(ts)] egemma-rollback-prune DONE (timer disabled)" >> "$LOG"
  echo "DECISION: PRUNE — done."
else
  echo "[$(ts)] egemma-rollback-prune DONE with collections kept:$KEPT (timer left as it was)" >> "$LOG"
  echo "DECISION: PRUNE — done; kept:$KEPT"
fi
exit 0
