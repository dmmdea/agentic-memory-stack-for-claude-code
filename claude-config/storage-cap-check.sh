#!/usr/bin/env bash
# STEP 16 — SessionStart hook: warn if any memory store exceeds hard cap.
# v0.12: NO cold-archive tier, so we enforce growth via caps + L7 decay.
# v0.17 Phase 0.E: brand context auto-load (brand inferred from cwd).
# Always exits 0. Emits a single banner line if anything's over cap (else silent).

set +e
warnings=""

# v0.17 Phase 0.E brand inference; v1.0 Phase 7B: operator-agnostic — rules from
# the deployed brands.json beside this script (no private brand names hardcoded;
# operators add their own). Neutral fallback if absent/unparseable.
infer_brand_from_cwd() {
    local cwd="$1"
    local sdir cfg
    sdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
    cfg="$sdir/brands.json"
    if [ -f "$cfg" ]; then
        python3 - "$cwd" "$cfg" <<'PY' 2>/dev/null
import sys, json, re
cwd = sys.argv[1].lower()
try:
    rules = json.load(open(sys.argv[2])).get("rules", [])
except Exception:
    rules = []
out = ""
for r in rules:
    p = r.get("pattern")
    if p and re.search(p, cwd, re.I):
        out = r.get("brand", ""); break
print(out)
PY
    else
        # neutral default
        case "$cwd" in *agentic-memory*|*ai-ecosystem*|*mem0*) echo "ai-ecosystem" ;; *) echo "" ;; esac
    fi
}

# v0.22 Pillar 1: initiative inference from cwd (mirrors the hook's
# Get-SessionInitiative). Two initiatives can share one brand
# (agentic-memory-stack and local-offload both = ai-ecosystem), so the
# SessionStart goal injection must scope by the repo leaf or local-offload
# goals bleed into agentic-memory-stack sessions. git repo-root leaf, falling
# back to the cwd leaf when cwd is not inside a git repo. Empty -> unscoped.
infer_initiative_from_cwd() {
    local cwd="$1"
    [ -z "$cwd" ] && { echo ""; return; }
    local top
    top=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)
    if [ -n "$top" ]; then
        basename "$top"
    else
        basename "$cwd"
    fi
}

# mem0 SQLite (history.db) — cap 500 MB
MEM0_DB="$HOME/.mem0/history.db"
if [ -f "$MEM0_DB" ]; then
  size_mb=$(stat -c%s "$MEM0_DB" 2>/dev/null | awk '{printf "%.0f", $1/1024/1024}')
  [ "${size_mb:-0}" -gt 500 ] && warnings+="mem0 SQLite ${size_mb} MB (cap 500). "
fi

# Qdrant storage dir — cap 1024 MB
QDRANT_DIR="$HOME/qdrant-server/storage"
if [ -d "$QDRANT_DIR" ]; then
  size_mb=$(du -sm "$QDRANT_DIR" 2>/dev/null | awk '{print $1}')
  [ "${size_mb:-0}" -gt 1024 ] && warnings+="Qdrant storage ${size_mb} MB (cap 1024). "
fi

# L10 audit flags — alert on DELTA above baseline (post-wipe v0.13 — see Task A.4)
FLAGS="$HOME/.mem0/audit-flags.jsonl"
BASE="$HOME/.mem0/audit-flags.baseline"
if [ -f "$FLAGS" ]; then
  fcount=$(wc -l < "$FLAGS" 2>/dev/null)
  baseline=$(cat "$BASE" 2>/dev/null || echo 0)
  delta=$(( fcount - baseline ))
  [ "$delta" -gt 20 ] && warnings+="L10 audit-flags: ${delta} NEW since baseline (total ${fcount}). Review ~/.mem0/audit-flags.jsonl. "
fi

# v0.17 Phase 0.C: recent decisions surface (cross-restart persistence)
# Read top 5 entries from ~/.mem0/recent-decisions.jsonl (most recent first).
# Format: ts (YYYY-MM-DD HH:MM), answer (≤80 chars), question_preview (≤60 chars).
DECISIONS="$HOME/.mem0/recent-decisions.jsonl"
if [ -f "$DECISIONS" ] && [ -s "$DECISIONS" ]; then
  decision_lines=$(tail -5 "$DECISIONS" 2>/dev/null | tac 2>/dev/null || tail -5 "$DECISIONS")
  if [ -n "$decision_lines" ]; then
    echo "[agentic-memory-stack] recent decisions (last 5):"
    echo "$decision_lines" | while IFS= read -r dline; do
      if [ -z "$dline" ]; then continue; fi
      # Parse ts, answer, question_preview from JSON using python3 (stdlib only)
      parsed=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    ts = d.get('ts','')[:16].replace('T',' ')
    ans = d.get('answer','')[:80]
    q   = d.get('question_preview','')[:60]
    print(f'  - {ts}: {ans} (Q: {q})')
except Exception:
    pass
" "$dline" 2>/dev/null)
      [ -n "$parsed" ] && echo "$parsed"
    done
  fi
fi

# v0.17 Phase 0.E: brand context auto-load
SESSION_CWD="${CLAUDE_CWD:-$PWD}"
BRAND="$(infer_brand_from_cwd "$SESSION_CWD")"
# v0.22 Pillar 1: initiative axis for goal scoping (same cwd source as brand).
INITIATIVE="$(infer_initiative_from_cwd "$SESSION_CWD")"
KEY="$(cat "$HOME/.mem0/api-key" 2>/dev/null)"
if [ -n "$BRAND" ] && [ -n "$KEY" ]; then
  echo "[agentic-memory-stack] brand context ($BRAND):"
  # Top 5 canonical memories for this brand (highest trust)
  canon=$(curl -fsS --max-time 4 -H "X-API-Key: $KEY" "http://127.0.0.1:18791/v1/memories?user_id=__WSL_USER__&limit=300" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    recs = []
    for r in d.get('results', []):
        md = r.get('metadata') or {}
        if md.get('tier') == 'canonical' and md.get('brand') == '$BRAND':
            recs.append(r)
        if len(recs) >= 5:
            break
    for r in recs:
        text = (r.get('memory') or '')[:120]
        print(f'  - [canonical] {text}')
except Exception:
    pass
" 2>/dev/null)
  [ -n "$canon" ] && echo "$canon"
  # Top 3 open goals for this brand, scoped to the session's initiative
  # (v0.22 Pillar 1): server returns this initiative + cross-cutting (NULL)
  # goals only, so another initiative's goals under the same brand don't bleed
  # in. URL-encode the initiative (cwd-leaf fallback may contain spaces).
  INIT_Q=""
  if [ -n "$INITIATIVE" ]; then
    INIT_ENC=$(python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))" "$INITIATIVE" 2>/dev/null)
    [ -n "$INIT_ENC" ] && INIT_Q="&initiative=$INIT_ENC"
  fi
  goals=$(curl -fsS --max-time 3 -H "X-API-Key: $KEY" "http://127.0.0.1:18791/v1/goals?status=open&brand=$BRAND&limit=3${INIT_Q}" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for g in d:
        title = (g.get('title') or '')[:100]
        prio = g.get('priority', 3)
        print(f'  - [goal P{prio} OPEN] {title}')
except Exception:
    pass
" 2>/dev/null)
  [ -n "$goals" ] && echo "$goals"
fi

# v0.13 SessionStart hydration: emit a one-line pointer to the stack repo's
# session_summary.md if present. v1.0 Phase 7B: resolve the repo from the operator
# receipt (~/.mem0/stack.env MEM0_REPO_ROOT_WSL) instead of a hardcoded dev path.
# Silent if the receipt/file is absent (e.g. a third party who didn't keep the repo).
[ -f "$HOME/.mem0/stack.env" ] && . "$HOME/.mem0/stack.env"
SS="${MEM0_REPO_ROOT_WSL:-}/docs/session_summary.md"
if [ -n "${MEM0_REPO_ROOT_WSL:-}" ] && [ -f "$SS" ]; then
  # Extract the first non-empty line under "**What's next:**"
  next=$(awk '
    /^\*\*What.s next:\*\*/ {found=1; next}
    found && /^[[:space:]]*$/ {next}
    found && /^-[[:space:]]/ {gsub(/^-[[:space:]]+/, ""); print; exit}
    found && /^[^*[:space:]]/ {exit}
  ' "$SS" 2>/dev/null | head -c 200)
  [ -n "$next" ] && echo "[agentic-memory-stack] last session next-up: $next"
fi

# MEMORY.md staleness - dream-consolidator should rebuild it nightly
MEMORYMD="$HOME/.mem0/MEMORY.md"
if [ -f "$MEMORYMD" ]; then
  age_days=$(( ( $(date +%s) - $(stat -c %Y "$MEMORYMD") ) / 86400 ))
  [ "$age_days" -gt 8 ] && warnings+="MEMORY.md stale (${age_days}d old; dream-consolidator may be failing). "
fi

[ -n "$warnings" ] && echo "[storage-cap] $warnings Run /memory-prune to address."
exit 0
