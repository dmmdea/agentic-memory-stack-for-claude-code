#!/usr/bin/env bash
# Daily snapshot of the agentic memory stack (timer OnCalendar 03:30 daily since 2026-07-14). Idempotent - keeps last 8 snapshots (~an 8-day restore window).
#
# v0.14 C hardening:
#   - Qdrant block moved AFTER local-file backups (Qdrant outage can't kill local backups)
#   - SQLite online-backup API (sqlite3 .backup) instead of bare cp (safe for live DB)
#   - Atomic tmp-then-rename for all copies (no partial writes on crash)
#   - Path validation for Qdrant snapshot name (no path traversal)
#   - Post-backup integrity checks: sqlite3 pragma integrity_check + test -s for others
#   - set +e around Qdrant block so local backups always complete first
# Note: NOT set -euo pipefail globally; individual errors handled per-block.

TS=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="$HOME/.mem0/backups"
mkdir -p "$BACKUP_DIR"
rc=0

# DR fix (2026-06-20): the LIVE mem0 vector collection is mem0_egemma_768 (config.py).
# It was "memories" before the EmbeddingGemma migration; the old collection still exists
# frozen (~2165 pts) while the live store grew in mem0_egemma_768 (3028+). Snapshotting the
# stale name silently backed up the WRONG vectors. Single source of truth here so it can't
# drift again. Override via env if the collection is ever renamed.
QDRANT_COLLECTION="${MEM0_QDRANT_COLLECTION:-mem0_egemma_768}"
# Qdrant REST base; overridable so the suite can drive the script against a fake server.
QDRANT_URL="${MEM0_QDRANT_URL:-http://127.0.0.1:6333}"

# stack.env is read BY KEY, never sourced: it is operator-edited, and one unquoted value with a
# space made bash execute the second word and killed the 09-21 and 09-22 nightlies.
stack_env_get() {
  [ -f "$HOME/.mem0/stack.env" ] || return 0
  { grep -m1 "^$1=" "$HOME/.mem0/stack.env" || true; } | cut -d= -f2- | tr -d '\r' | sed -e "s/^[\"']//" -e "s/[\"']\$//"
}

echo "stack-backup: starting TS=$TS BACKUP_DIR=$BACKUP_DIR"

# ── 1. Local file backups (always run; Qdrant outage must not skip these) ─────

# 1a. history.db — use SQLite online backup API (safe for live DB)
HIST_SRC="$HOME/.mem0/history.db"
HIST_DST="$BACKUP_DIR/history-$TS.db"
if [ -f "$HIST_SRC" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$HIST_SRC" ".backup '$HIST_DST.tmp'" 2>/dev/null \
      && mv "$HIST_DST.tmp" "$HIST_DST" \
      && echo "history.db backed up" \
      || { rm -f "$HIST_DST.tmp"; echo "WARN: history.db backup failed" >&2; rc=1; }
    if [ -f "$HIST_DST" ]; then
      result=$(sqlite3 "file:$HIST_DST?mode=ro&immutable=1" 'pragma integrity_check' 2>&1)
      if [ "$result" != "ok" ]; then
        echo "WARN: history.db integrity_check: $result" >&2; rc=1
      else
        echo "history.db integrity OK"
      fi
    fi
  else
    # sqlite3 not installed — fall back to cp (no online-backup guarantee; acceptable for
    # low-write-rate history.db while sqlite3 is absent)
    cp "$HIST_SRC" "$HIST_DST.tmp" 2>/dev/null && mv "$HIST_DST.tmp" "$HIST_DST" \
      && echo "history.db backed up (cp fallback; install sqlite3 for online-backup)" \
      || { rm -f "$HIST_DST.tmp"; echo "WARN: history.db backup failed (cp fallback)" >&2; rc=1; }
  fi
fi

# 1b. tier-ledger — MEM-16 (2026-07-03): the ledger is now legacy
# tier-ledger.jsonl (frozen archive) + monthly segments tier-ledger-YYYY-MM.jsonl.
# Concatenate legacy + segments (in ledger-audit.py's walk order: legacy first,
# then segments sorted by name) into the SINGLE dated backup file, so the prune
# and restore paths keep their one-file-per-snapshot contract unchanged. The
# strict [0-9] glob excludes tier-ledger-restore.jsonl (stack-restore's copy).
LEDGER_SRC="$HOME/.mem0/tier-ledger.jsonl"
LEDGER_DST="$BACKUP_DIR/tier-ledger-$TS.jsonl"
ledger_parts=()
[ -f "$LEDGER_SRC" ] && ledger_parts+=("$LEDGER_SRC")
for seg in "$HOME/.mem0"/tier-ledger-[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl; do
  [ -f "$seg" ] && ledger_parts+=("$seg")
done
if [ "${#ledger_parts[@]}" -gt 0 ]; then
  cat "${ledger_parts[@]}" > "$LEDGER_DST.tmp" && mv "$LEDGER_DST.tmp" "$LEDGER_DST" \
    || { rm -f "$LEDGER_DST.tmp"; echo "WARN: tier-ledger backup failed" >&2; rc=1; }
  test -s "$LEDGER_DST" || { echo "WARN: tier-ledger backup empty" >&2; rc=1; }
fi

# 1c. MEMORY.md
MEMORY_SRC="$HOME/.mem0/MEMORY.md"
MEMORY_DST="$BACKUP_DIR/MEMORY-$TS.md"
if [ -f "$MEMORY_SRC" ]; then
  cp "$MEMORY_SRC" "$MEMORY_DST.tmp" && mv "$MEMORY_DST.tmp" "$MEMORY_DST" \
    || { rm -f "$MEMORY_DST.tmp"; echo "WARN: MEMORY.md backup failed" >&2; rc=1; }
  test -s "$MEMORY_DST" || { echo "WARN: MEMORY.md backup empty" >&2; rc=1; }
fi

# 1d. audit-flags.baseline
BASELINE_SRC="$HOME/.mem0/audit-flags.baseline"
BASELINE_DST="$BACKUP_DIR/audit-flags-$TS.baseline"
if [ -f "$BASELINE_SRC" ]; then
  cp "$BASELINE_SRC" "$BASELINE_DST.tmp" && mv "$BASELINE_DST.tmp" "$BASELINE_DST" \
    || { rm -f "$BASELINE_DST.tmp"; echo "WARN: audit-flags.baseline backup failed" >&2; rc=1; }
  test -s "$BASELINE_DST" || { echo "WARN: audit-flags.baseline backup empty" >&2; rc=1; }
fi

# 1e. episodic.db — v0.15: SQLite + FTS5 episodic sidecar (session goals + summaries).
# Use SQLite online-backup API (same pattern as history.db) — safe for live DB with WAL mode.
EPISODIC_SRC="$HOME/.mem0/episodic.db"
EPISODIC_DST="$BACKUP_DIR/episodic-$TS.db"
if [ -f "$EPISODIC_SRC" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$EPISODIC_SRC" ".backup '$EPISODIC_DST.tmp'" 2>/dev/null \
      && mv "$EPISODIC_DST.tmp" "$EPISODIC_DST" \
      && echo "episodic.db backed up" \
      || { rm -f "$EPISODIC_DST.tmp"; echo "WARN: episodic.db backup failed" >&2; rc=1; }
    if [ -f "$EPISODIC_DST" ]; then
      result=$(sqlite3 "file:$EPISODIC_DST?mode=ro&immutable=1" 'pragma integrity_check' 2>&1)
      if [ "$result" != "ok" ]; then
        echo "WARN: episodic.db integrity_check: $result" >&2; rc=1
      else
        echo "episodic.db integrity OK"
      fi
    fi
  else
    # sqlite3 not installed — fall back to cp
    cp "$EPISODIC_SRC" "$EPISODIC_DST.tmp" 2>/dev/null && mv "$EPISODIC_DST.tmp" "$EPISODIC_DST" \
      && echo "episodic.db backed up (cp fallback; install sqlite3 for online-backup)" \
      || { rm -f "$EPISODIC_DST.tmp"; echo "WARN: episodic.db backup failed (cp fallback)" >&2; rc=1; }
  fi
fi

# 1f. ~/.claude/settings.json (Windows side) — v0.20 Final (adversarial-review
# HIGH): the UserPromptSubmit/SessionStart hook registrations the whole prompt
# pipeline depends on lived ONLY outside every backup. Capture them so DR
# restores the registration alongside the WSL DBs.
# v1.0 Phase 7A: resolve the Windows user from the operator receipt (~/.mem0/stack.env),
# falling back to cmd.exe — never hardcode the developer handle.
WIN_USER_BK="${MEM0_WIN_USER:-}"
[ -z "$WIN_USER_BK" ] && WIN_USER_BK="$(stack_env_get MEM0_WIN_USER)"
[ -z "$WIN_USER_BK" ] && WIN_USER_BK="$(cmd.exe /c 'echo %USERNAME%' 2>/dev/null | tr -d '\r\n ')"
SETTINGS_SRC="/mnt/c/Users/$WIN_USER_BK/.claude/settings.json"
SETTINGS_DST="$BACKUP_DIR/claude-settings-$TS.json"
if [ -f "$SETTINGS_SRC" ]; then
  cp "$SETTINGS_SRC" "$SETTINGS_DST.tmp" && mv "$SETTINGS_DST.tmp" "$SETTINGS_DST" \
    || { rm -f "$SETTINGS_DST.tmp"; echo "WARN: claude settings.json backup failed" >&2; rc=1; }
  test -s "$SETTINGS_DST" || { echo "WARN: claude settings.json backup empty" >&2; rc=1; }
fi

# 1g. L10 admission-audit FLAGS (2026-08-24, review finding): ~/.mem0/audit-flags.jsonl
# held 765 flags in NO backup — a restore resurfaced every reviewed flag as unreviewed.
# Distinct 'l10-flags' prefix ON PURPOSE: the prune glob "audit-flags-*.*" already owns
# the baseline family, and a shared glob halves both retention windows with mtime
# interleave able to evict a whole family.
L10FLAGS_SRC="$HOME/.mem0/audit-flags.jsonl"
L10FLAGS_DST="$BACKUP_DIR/l10-flags-$TS.jsonl"
if [ -f "$L10FLAGS_SRC" ]; then
  cp "$L10FLAGS_SRC" "$L10FLAGS_DST.tmp" && mv "$L10FLAGS_DST.tmp" "$L10FLAGS_DST" \
    || { rm -f "$L10FLAGS_DST.tmp"; echo "WARN: audit-flags.jsonl backup failed" >&2; rc=1; }
  # no test -s: l10-audit opens the flags file append-mode unconditionally, so a
  # 0-byte file is the LEGITIMATE zero-flags state (same reasoning as 1i below);
  # a test -s here would fail every nightly backup on a clean corpus.
fi

# 1h. L10 review STATE (reviewed_keys). The parse check below is the ONLY guard
# this block relies on — do NOT assume every writer of this file is atomic
# (l10-audit's save_state is since 2026-08-24; other writers must be checked,
# not presumed), and the l10-audit timer floats (OnBootSec+6h) so a write can
# coincide with this backup. VALIDATE the copy parses before letting it into the
# retention window: a torn state file restored later would wipe reviewed_keys
# and resurrect every reviewed flag — the exact loss this block exists to prevent.
L10STATE_SRC="$HOME/.mem0/l10-state.json"
L10STATE_DST="$BACKUP_DIR/l10-state-$TS.json"
if [ -f "$L10STATE_SRC" ]; then
  if ! command -v python3 >/dev/null 2>&1; then
    # distinct diagnosis (review M3): a vanished python3 must not read as a
    # daily "torn write?" while the artifact silently drops out of every backup
    echo "WARN: python3 missing - l10-state.json copy cannot be validated, SKIPPED" >&2; rc=1
  elif cp "$L10STATE_SRC" "$L10STATE_DST.tmp" \
     && _l10err=$(python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$L10STATE_DST.tmp" 2>&1); then
    mv "$L10STATE_DST.tmp" "$L10STATE_DST" \
      || { rm -f "$L10STATE_DST.tmp"; echo "WARN: l10-state.json final rename failed" >&2; rc=1; }
  else
    rm -f "$L10STATE_DST.tmp"
    echo "WARN: l10-state.json backup failed or copy did not parse (torn write?): $(echo "${_l10err:-cp failed}" | tail -1)" >&2; rc=1
  fi
fi

# 1i. Contradiction-promote review queue — the human review queue the SAFE resolver
# feeds; losing it silently loses queued genuine contradictions. Zero-byte is a
# legitimate state (queue empty), so no test -s here.
PRQ_SRC="$HOME/.mem0/contradiction-promote-review.jsonl"
PRQ_DST="$BACKUP_DIR/promote-review-$TS.jsonl"
if [ -f "$PRQ_SRC" ]; then
  cp "$PRQ_SRC" "$PRQ_DST.tmp" && mv "$PRQ_DST.tmp" "$PRQ_DST" \
    || { rm -f "$PRQ_DST.tmp"; echo "WARN: contradiction-promote-review.jsonl backup failed" >&2; rc=1; }
fi

# 1j. Stale-paths hand-label worksheet — 135 operator-labelled rows; the one artifact
# in the sidecar that cannot be regenerated (the labels killed a feature; the evidence
# must survive the machine).
WS_SRC="$HOME/.mem0/stale-paths-worksheet.jsonl"
WS_DST="$BACKUP_DIR/stale-worksheet-$TS.jsonl"
if [ -f "$WS_SRC" ]; then
  cp "$WS_SRC" "$WS_DST.tmp" && mv "$WS_DST.tmp" "$WS_DST" \
    || { rm -f "$WS_DST.tmp"; echo "WARN: stale-paths-worksheet backup failed" >&2; rc=1; }
  test -s "$WS_DST" || { echo "WARN: stale-paths-worksheet backup empty" >&2; rc=1; }
fi

echo "stack-backup: local files done (rc=$rc so far)"

# ── 2. Qdrant snapshots (isolated — a crash here does NOT abort the blocks above,
# but every skip/failure path of the PRIMARY collection sets rc=1 like the local-file blocks
# do. The vector collection is the most valuable artifact in the set; on a box without jq this
# block used to parse an empty name, WARN, and exit 0 — so every backup silently shipped
# without it while the nightly reported success.) ──────────────────────────────────────────
QDRANT_SNAP_ROOT="${MEM0_QDRANT_SNAPSHOT_DIR:-$HOME/qdrant-server/snapshots}"

# Trim one collection's server-side snapshot store: keep the newest 2 (the one just verified is
# always among them), delete the rest through the API (which removes the .checksum too), and
# sweep hand-made qdrant-*.snapshot one-offs older than 14 days. Before this the store grew
# ~150 MB a night, a second full copy of the vectors that nothing ever deleted. A failure here
# only WARNs: the backup itself is done, and a transient API error must not turn the night red
# (which would also make the off-box copy refuse the set).
prune_server_snapshots() {  # <collection> <snapshot just verified>
  local coll="$1" cur="$2" name names
  if ! names=$(curl -sf "$QDRANT_URL/collections/$coll/snapshots" \
        | jq -r --arg cur "$cur" '(.result // []) | sort_by([(.creation_time // ""), .name]) | reverse | .[2:][]?.name | select(. != $cur)'); then
    echo "WARN: could not list server-side snapshots of $coll - not pruned" >&2
    return 0
  fi
  for name in $names; do
    case "$name" in *[!A-Za-z0-9._-]*|.*) echo "WARN: odd server-side snapshot name '$name' - not deleted" >&2; continue ;; esac
    curl -sf -X DELETE "$QDRANT_URL/collections/$coll/snapshots/$name" >/dev/null \
      && echo "qdrant: deleted old server-side snapshot $coll/$name" \
      || echo "WARN: could not delete server-side snapshot $coll/$name" >&2
  done
  find "$QDRANT_SNAP_ROOT/$coll" -maxdepth 1 -name 'qdrant-*.snapshot*' -mtime +14 -delete 2>/dev/null
  return 0
}

# Snapshot one collection into $2 and prove the copy before anything server-side is deleted:
# byte size equal to the source, and the sha256 Qdrant wrote beside the snapshot (the
# .checksum file) equal to the copy's; without a checksum file the two files must compare equal.
snapshot_collection() {  # <collection> <dst>
  local coll="$1" dst="$2" snap src want got
  snap=$(curl -sf -X POST "$QDRANT_URL/collections/$coll/snapshots" | jq -r '.result.name // empty')
  if [ -z "$snap" ]; then
    echo "WARN: Qdrant snapshot request for $coll failed or returned empty name — NOT backed up" >&2
    return 1
  fi
  # Validate snapshot name: no empty, no path separators, no dot-prefix (traversal guard)
  case "$snap" in
    ""|*/*|.*) echo "WARN: bad Qdrant snapshot name '$snap' — refusing to copy" >&2; return 1 ;;
  esac
  src="$QDRANT_SNAP_ROOT/$coll/$snap"
  if [ ! -f "$src" ]; then
    echo "WARN: Qdrant snapshot file not found at $src" >&2
    return 1
  fi
  cp "$src" "$dst.tmp" && mv "$dst.tmp" "$dst" \
    && echo "qdrant snapshot $coll/$snap -> $dst" \
    || { rm -f "$dst.tmp"; echo "WARN: failed to copy Qdrant snapshot of $coll" >&2; return 1; }
  test -s "$dst" || { rm -f "$dst"; echo "WARN: qdrant snapshot backup of $coll empty" >&2; return 1; }
  if [ "$(stat -c %s "$src")" != "$(stat -c %s "$dst")" ]; then
    rm -f "$dst"; echo "WARN: qdrant snapshot copy of $coll differs in size from the source - not kept, server-side snapshots untouched" >&2
    return 1
  fi
  if [ -f "$src.checksum" ]; then
    want=$(head -n1 "$src.checksum" | tr -d '\r' | cut -d' ' -f1)
    got=$(sha256sum "$dst" | cut -d' ' -f1)
    if [ "$want" != "$got" ]; then
      rm -f "$dst"; echo "WARN: qdrant snapshot copy of $coll fails its checksum (want $want, got $got) - not kept, server-side snapshots untouched" >&2
      return 1
    fi
  elif ! cmp -s "$src" "$dst"; then
    rm -f "$dst"; echo "WARN: qdrant snapshot copy of $coll differs from the source (no checksum file to check) - not kept, server-side snapshots untouched" >&2
    return 1
  fi
  prune_server_snapshots "$coll" "$snap"
}

if ! command -v jq >/dev/null 2>&1; then
  echo "WARN: jq not installed — cannot parse the Qdrant snapshot name; vector collection NOT backed up (sudo apt install -y jq)" >&2
  rc=1
else
  snapshot_collection "$QDRANT_COLLECTION" "$BACKUP_DIR/qdrant-$TS.snapshot" || rc=1
  # The three small secondary collections ride in the same set (episodes_*, *_entities,
  # wiki_pages_*), under a distinct qcol-<kind> prefix so the qdrant-* prune glob stays
  # disjoint. They are rebuildable, so a failure WARNs but does not fail the night.
  seen=""
  for coll in $(curl -sf "$QDRANT_URL/collections" | jq -r '.result.collections[]?.name' | sort); do
    case "$coll" in *[!A-Za-z0-9._-]*) continue ;; esac
    [ "$coll" = "$QDRANT_COLLECTION" ] && continue
    case "$coll" in
      episodes_*) kind=episodes ;;
      *_entities) kind=entities ;;
      wiki_pages_*) kind=wiki ;;
      *) continue ;;
    esac
    case " $seen " in
      *" $kind "*) echo "WARN: a second $kind collection ($coll) is not snapshotted - one per kind" >&2; continue ;;
    esac
    seen="$seen $kind"
    snapshot_collection "$coll" "$BACKUP_DIR/qcol-$kind-$TS.snapshot" \
      || echo "WARN: $coll snapshot failed (rebuildable; the set is still complete)" >&2
  done
fi

echo "stack-backup: Qdrant block done"

# ── 3. Prune: keep last 8 snapshots of each kind ──────────────────────────────
# A retention entry is a REAL artifact: $kind-<digit...>.<ext> for one explicit extension.
# The old glob "$kind-*.*" also counted SQLite sidecars (-wal/-shm), which any read-only
# opener of a WAL-mode backup leaves NEWER than every real .db, so the next prune kept the
# sidecars and deleted the databases. Sidecars, .tmp partials and strays never match, and
# entries are ordered by the timestamp in the name, not mtime (a copy or restore rewrites mtime).
# NOTE: kinds must be glob-disjoint - "qdrant" must not match qcol-*, and the jsonl flags file
# lives under the distinct "l10-flags" prefix (see 1g).
prune_kind() {  # <kind> <extension>
  local f
  for f in "$BACKUP_DIR/$1"-[0-9]*."$2"; do [ -f "$f" ] && printf '%s\n' "$f"; done \
    | sort -r | tail -n +9 | xargs -r -d '\n' rm -f
}
for spec in qdrant:snapshot history:db tier-ledger:jsonl MEMORY:md audit-flags:baseline episodic:db \
            claude-settings:json l10-flags:jsonl l10-state:json promote-review:jsonl \
            stale-worksheet:jsonl qcol-episodes:snapshot qcol-entities:snapshot qcol-wiki:snapshot; do
  prune_kind "${spec%%:*}" "${spec#*:}"
done
# Orphan sidecars beside backup DBs: an empty WAL holds no unflushed transaction, so it and its
# -shm are debris; a -shm with no -wal at all is debris too. A non-empty WAL is left alone.
for wal in "$BACKUP_DIR"/*.db-wal; do
  [ -e "$wal" ] || continue
  [ -s "$wal" ] && continue
  rm -f "$wal" "${wal%-wal}-shm"
done
for shm in "$BACKUP_DIR"/*.db-shm; do
  [ -e "$shm" ] || continue
  [ -e "${shm%-shm}-wal" ] || rm -f "$shm"
done
# sweep stale partials from crashed runs (older than 60 min = garbage, not a snapshot)
find "$BACKUP_DIR" -maxdepth 1 -name '*.tmp' -mmin +60 -delete 2>/dev/null

du -sh "$BACKUP_DIR"

# ── 4. Backup manifest (v0.17 Phase B) ────────────────────────────────────────
# Write manifest-$TS.json documenting counts + file list for this snapshot.
# Run after all backup files (including Qdrant) are written. Fail open.
MANIFEST_SCRIPT="$(dirname "$0")/stack-backup-manifest.sh"
if [ -f "$MANIFEST_SCRIPT" ]; then
    bash "$MANIFEST_SCRIPT" "$TS" || { echo "WARN: manifest writer failed - this snapshot will be UNLISTABLE by stack-restore" >&2; rc=1; }
else
    echo "WARN: stack-backup-manifest.sh not found at $MANIFEST_SCRIPT" >&2
fi

# Manifests age out with their sets (a manifest for a deleted set is a restore point that lies):
# pruned AFTER tonight's is written so the window is the same 8 as every other kind.
prune_kind manifest json

echo "stack-backup: complete (rc=$rc)"
exit $rc
