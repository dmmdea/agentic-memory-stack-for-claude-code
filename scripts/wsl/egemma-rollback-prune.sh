#!/usr/bin/env bash
# egemma-rollback-prune.sh — one-shot v0.22 rollback-window cleanup (fires 2026-06-21+).
#
# Deletes the OLD nomic `memories` Qdrant collection + its migration snapshot(s),
# which were retained as the rollback anchor for the v0.22 EmbeddingGemma re-embed.
# HEALTH-GATED: prunes ONLY if the migration is confirmed healthy (mem0 deep-health
# embedder ok + mem0 is STILL BOUND to mem0_egemma_768 + the new collection is green
# with a full point count). If anything looks off (a rollback happened, mem0 down,
# new collection thin), it SKIPS the deletion, logs a warning to the audit flags,
# and leaves the anchor intact. After a successful run it self-disables its timer.
#
# ROLLBACK SAFETY (v0.22 H2): the decisive guard is the LIVE bound collection
# reported by /health/deep ("collection":"<name>") — NOT the mere existence of the
# egemma collection (which survives a rollback). After a documented rollback
# (config.py collection -> memories), /health/deep reports "memories" and this gate
# SKIPS, so it can never delete the live store out from under a rolled-back stack.
# Even so, disabling this timer is STEP 1 of any documented rollback (belt + braces).
#
# TESTABILITY: endpoints are env-overridable and EGEMMA_PRUNE_DRY_RUN=1 prints
# "DECISION: PRUNE|SKIP ..." and exits BEFORE any deletion (used by the committed
# gate test test_egemma_rollback_prune.py — verifies skip-on-rollback without
# touching live data).
set +e
MEM0_URL="${EGEMMA_PRUNE_MEM0_URL:-http://localhost:18791}"
QDRANT_URL="${EGEMMA_PRUNE_QDRANT_URL:-http://localhost:6333}"
EXPECTED_COLLECTION="${EGEMMA_PRUNE_EXPECTED_COLLECTION:-mem0_egemma_768}"
LOG="${EGEMMA_PRUNE_LOG:-$HOME/.mem0/egemma-rollback-prune.log}"
AUDIT_FLAGS="${EGEMMA_PRUNE_AUDIT_FLAGS:-$HOME/.mem0/audit-flags.jsonl}"
DRY_RUN="${EGEMMA_PRUNE_DRY_RUN:-0}"
mkdir -p "$(dirname "$LOG")" "$(dirname "$AUDIT_FLAGS")"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
echo "[$(ts)] egemma-rollback-prune START (dry_run=$DRY_RUN)" >> "$LOG"

DEEP=$(curl -sf "$MEM0_URL/health/deep")
NEW=$(curl -sf "$QDRANT_URL/collections/$EXPECTED_COLLECTION" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin)["result"];print(d.get("points_count",0),d.get("status",""))' 2>/dev/null)
OLD=$(curl -sf "$QDRANT_URL/collections/memories" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin)["result"];print(d.get("points_count",0),d.get("status",""))' 2>/dev/null)
echo "[$(ts)] deep=$DEEP | new($EXPECTED_COLLECTION)=$NEW | old(memories)=$OLD" >> "$LOG"

NEWPTS=$(echo "$NEW" | awk '{print $1}')
NEWSTATUS=$(echo "$NEW" | awk '{print $2}')

# v0.22 H2: the collection mem0 is ACTUALLY bound to at runtime. /health/deep now
# reports it ("collection":"<name>") from the live Memory instance. This is the
# decisive rollback detector: the old artifact-based gate (dim:768 + egemma
# collection green) stays GREEN even after a rollback (the egemma collection still
# exists and egemma is still served on :11436), so it could delete `memories` out
# from under a rolled-back stack now writing to `memories`. Binding is the truth.
BOUND=$(echo "$DEEP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("collection",""))' 2>/dev/null)
echo "[$(ts)] bound collection=$BOUND (expected $EXPECTED_COLLECTION)" >> "$LOG"

# HEALTH GATE: mem0 embedder ok (dim 768) AND mem0 is STILL BOUND to the expected
# collection (NOT rolled back to `memories`) AND the new collection is green with
# >=1000 points.
if ! echo "$DEEP" | grep -q '"dim":768' \
   || [ "$BOUND" != "$EXPECTED_COLLECTION" ] \
   || [ -z "$NEWPTS" ] || [ "$NEWPTS" -lt 1000 ] || [ "$NEWSTATUS" != "green" ]; then
  REASON="migration unhealthy or rolled back; verify before manual prune"
  if [ -n "$BOUND" ] && [ "$BOUND" != "$EXPECTED_COLLECTION" ]; then
    REASON="ROLLBACK DETECTED — mem0 is bound to '$BOUND', not $EXPECTED_COLLECTION; refusing to delete the rollback anchor. Disable this timer (systemctl --user disable egemma-rollback-prune.timer) as step 1 of any rollback."
  fi
  echo "[$(ts)] SKIP — $REASON" >> "$LOG"
  printf '{"ts":"%s","event":"egemma-rollback-prune-skipped","reason":%s,"bound_collection":%s,"deep":%s}\n' \
    "$(ts)" \
    "$(printf '%s' "$REASON" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '"skip"')" \
    "$(printf '%s' "${BOUND:-null}" | python3 -c 'import sys,json;s=sys.stdin.read().strip();print(json.dumps(s) if s and s!="null" else "null")' 2>/dev/null || echo 'null')" \
    "$(echo "${DEEP:-null}" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read().strip() or "null"))' 2>/dev/null || echo '"null"')" \
    >> "$AUDIT_FLAGS"
  echo "DECISION: SKIP — $REASON"
  exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "[$(ts)] DRY RUN — gate PASSED (would prune); no deletion performed." >> "$LOG"
  echo "DECISION: PRUNE — migration healthy (bound=$BOUND, new=$NEWPTS green); would delete memories + snapshots."
  exit 0
fi

echo "[$(ts)] migration healthy (new=$NEWPTS green). Pruning old collection + snapshots." >> "$LOG"
curl -sf -o /dev/null -w '%{http_code}' -X DELETE "$QDRANT_URL/collections/memories" >> "$LOG" 2>&1
echo " <- DELETE memories" >> "$LOG"

SNAPDIR="$HOME/qdrant-server/snapshots/memories"
if [ -d "$SNAPDIR" ]; then
  rm -f "$SNAPDIR"/memories-*.snapshot && echo "[$(ts)] removed snapshots in $SNAPDIR" >> "$LOG"
fi

# one-shot: disable our own timer so it never fires again
systemctl --user disable egemma-rollback-prune.timer >> "$LOG" 2>&1
echo "[$(ts)] egemma-rollback-prune DONE (timer disabled)" >> "$LOG"
echo "DECISION: PRUNE — done."
exit 0
