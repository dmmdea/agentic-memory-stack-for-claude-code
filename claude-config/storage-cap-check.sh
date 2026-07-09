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

# Campaign (funnel) inference from cwd — mirrors infer_brand_from_cwd but returns the
# matched rule's "campaign" (e.g. biohacker-collective). Empty = no funnel (store/shared).
# Isolates funnel-specific canonical rules: a session surfaces shared facts (no campaign)
# + ONLY its own funnel's rules, never another funnel's (2026-06-20 cross-funnel fix).
infer_campaign_from_cwd() {
    local cwd="$1"
    local sdir cfg
    sdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
    cfg="$sdir/brands.json"
    [ -f "$cfg" ] || { echo ""; return; }
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
        out = r.get("campaign", ""); break
print(out)
PY
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

# L10 audit flags — MEM-11 (2026-07-03): the old banner printed raw line count
# minus a baseline file that holds 0, i.e. EVERY flag ever written (328) — 4.6x
# the real backlog and unfixable by triage (audit-flags.jsonl is append-only;
# --resolve marks reviewed_keys in l10-state.json, it never shrinks the file).
# Count what SLOWDRIP counts (l10-audit.py / audit-flags-triage.py --summary):
# flags whose "<memory_id>:<flag_type>" dedup-key is NOT in
# l10-state.json["reviewed_keys"] — so the banner (71 today) matches the number
# the operator actually clears with the triage tool. Pure-local: two file
# reads, no server call.
FLAGS="$HOME/.mem0/audit-flags.jsonl"
L10STATE="$HOME/.mem0/l10-state.json"
if [ -f "$FLAGS" ]; then
  l10counts=$(python3 - "$FLAGS" "$L10STATE" <<'PY' 2>/dev/null
import json, sys
flags_p, state_p = sys.argv[1], sys.argv[2]
try:
    reviewed = set(json.load(open(state_p, encoding="utf-8")).get("reviewed_keys", []))
except Exception:
    reviewed = set()   # no/unreadable state -> all flags unreviewed (conservative, same as SLOWDRIP)
unrev = total = 0
try:
    for line in open(flags_p, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        total += 1
        if f"{r.get('memory_id')}:{r.get('flag_type')}" not in reviewed:
            unrev += 1
except OSError:
    pass
print(f"{unrev} {total}")
PY
)
  n_unrev="${l10counts%% *}"; n_total="${l10counts##* }"
  [ "${n_unrev:-0}" -gt 20 ] && warnings+="L10 audit-flags: ${n_unrev} unreviewed (total ${n_total}). Review ~/.mem0/audit-flags.jsonl. "
fi

# Recent-sessions surface (cross-restart). 2026-06-24: REPOINTED from recent-decisions.jsonl to
# episodic.db. recent-decisions.jsonl was written by UserPromptSubmit 0.B (decision capture), a
# PER-TURN hook that does NOT fire in the Claude Code VSCode-extension / Agent-SDK runtime — so it
# froze on 2026-06-16 and this banner showed stale 06-16 decisions forever. Episodes ARE captured by
# the SessionStart/PreCompact LIFECYCLE hooks (which DO fire), so they stay fresh. Show the last 5
# episodes that have a real goal (skip empty placeholder rows).
EPDB="$HOME/.mem0/episodic.db"
if [ -f "$EPDB" ]; then
  ep=$(python3 - "$EPDB" <<'PY' 2>/dev/null
import sys, sqlite3
try:
    con = sqlite3.connect(sys.argv[1]); con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT e.goal_text AS goal, e.ended_at AS ended, s.brand AS brand "
        "FROM episodes e LEFT JOIN sessions s ON e.session_id = s.session_id "
        "WHERE e.goal_text IS NOT NULL AND TRIM(e.goal_text) <> '' "
        "ORDER BY e.ended_at DESC LIMIT 5"
    ).fetchall()
    out = []
    for r in rows:
        ended = (r["ended"] or "")[:16].replace("T", " ")
        goal = (r["goal"] or "")[:90]
        brand = (r["brand"] or "")
        tag = ("[" + brand + "] ") if brand else ""
        out.append("  - " + ended + ": " + tag + goal)
    if out:
        print("[agentic-memory-stack] recent sessions (last 5):")
        print("\n".join(out))
except Exception:
    pass
PY
)
  [ -n "$ep" ] && echo "$ep"
fi

# v0.17 Phase 0.E: brand context auto-load
SESSION_CWD="${CLAUDE_CWD:-$PWD}"
BRAND="$(infer_brand_from_cwd "$SESSION_CWD")"
# Funnel/campaign axis (2026-06-20): isolates funnel-specific canonical rules so a
# biohacker session never sees recovery's rules and vice-versa.
CAMPAIGN="$(infer_campaign_from_cwd "$SESSION_CWD")"
# v0.22 Pillar 1: initiative axis for goal scoping (same cwd source as brand).
INITIATIVE="$(infer_initiative_from_cwd "$SESSION_CWD")"
KEY="$(cat "$HOME/.mem0/api-key" 2>/dev/null)"
# v1.12 F3 (HK-4): cold-morning guard. This hook runs SYNCHRONOUSLY at SessionStart;
# when the mem0 server isn't up yet (WSL just booted, services starting) every curl
# below burns its full --max-time SERIALLY and the session start blocks 15-30s+.
# Probe once for 1s; when cold, print the local-file blocks only (episodic recents,
# storage warnings — no server needed) and skip every server-dependent section.
MEM0_UP=1
curl -sf --max-time 1 http://127.0.0.1:18791/health >/dev/null 2>&1 || MEM0_UP=0
if [ "$MEM0_UP" = 0 ]; then
  echo "[agentic-memory-stack] memory server still starting — brand facts/goals skipped this session (they return next session)"
fi
if [ "$MEM0_UP" = 1 ] && [ -n "$BRAND" ] && [ -n "$KEY" ]; then
  echo "[agentic-memory-stack] brand context ($BRAND):"
  # Canonical memories for this brand (highest trust). v0.30 FIX (2026-06-19): fetch via
  # the canonical SEARCH path, NOT the list endpoint. GET /v1/memories is a plain
  # get_all(top_k) with NO tier filter, so canonical facts outside the top-N window were
  # silently dropped (the hook surfaced 1 of 7). query_class=canonical + threshold=0 returns
  # EVERY canonical record for the brand, query-independently (verified 2026-06-19).
  canon_body=$(python3 -c "
import sys, json
print(json.dumps({
    'query': sys.argv[1] + ' canonical ground-truth facts',
    'query_class': 'canonical',
    'threshold': 0,
    'limit': 50,
    'filters': {'tier': 'canonical', 'user_id': '__WSL_USER__', 'brand': sys.argv[1]},
}))
" "$BRAND" 2>/dev/null)
  canon=$(curl -fsS --max-time 3 -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" "http://127.0.0.1:18791/v1/memories/search" -d "$canon_body" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    recs = []
    for r in d.get('results', []):
        md = r.get('metadata') or {}
        rc = md.get('campaign') or ''
        if md.get('tier') == 'canonical' and md.get('brand') == '$BRAND' and (not rc or rc == '$CAMPAIGN'):
            recs.append(r)
        if len(recs) >= 8:
            break
    for r in recs:
        text = (r.get('memory') or '')[:120]
        print(f'  - [canonical] {text}')
except Exception:
    pass
" 2>/dev/null)
  # v0.28 Phase 2a: advisory frame — emitted iff ≥1 canonical fact exists.
  # The frame is advisory ("verify before risky actions"), never an imperative.
  if [ -n "$canon" ]; then
    echo "Locked facts you can lean on this session — verify before risky actions:"
    echo "$canon"
  fi
  # Top 3 open goals for this brand, scoped to the session's initiative
  # (v0.22 Pillar 1): server returns this initiative + cross-cutting (NULL)
  # goals only, so another initiative's goals under the same brand don't bleed
  # in. URL-encode the initiative (cwd-leaf fallback may contain spaces).
  INIT_Q=""
  if [ -n "$INITIATIVE" ]; then
    INIT_ENC=$(python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))" "$INITIATIVE" 2>/dev/null)
    [ -n "$INIT_ENC" ] && INIT_Q="&initiative=$INIT_ENC"
  fi
  goals=$(curl -fsS --max-time 2 -H "X-API-Key: $KEY" "http://127.0.0.1:18791/v1/goals?status=open&brand=$BRAND&limit=3${INIT_Q}" 2>/dev/null \
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

# B1 (2026-06-28, Phase 1+2): durable/evidence ranked-bundle enrichment — the thin precis the
# (now-dead) per-prompt UserPromptSubmit hook used to inject, which the canonical+goals lines do
# NOT cover. Runs REGARDLESS of brand (gated only on the api key): a brandless session still has
# brand-neutral facts AND must consume any fresh PreCompact marker so it can't linger. Reuses the
# live /v1/context/bundle: Phase 2 — a FRESH PreCompact marker supplies a real CONVERSATION query
# (tier=frontier, K<=2); otherwise a RECENCY pseudo-query (most-recent episode goal; precision-first
# tier=small, K<=1) + distilled. Silent on abstention. Helper is fail-silent with its own HTTP
# timeout + checkpoint=False (no synthetic episode in the resume banner).
if [ "${MEM0_UP:-0}" = 1 ] && [ -n "$KEY" ]; then
  SDIR_B1="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
  recall=$(python3 "$SDIR_B1/sessionstart_bundle.py" --brand "$BRAND" --initiative "$INITIATIVE" 2>/dev/null)
  [ -n "$recall" ] && echo "$recall"
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

# Brand-scope integrity (2026-06-20): the nightly brand-scope-audit writes this status.
# Warn if any canonical fact ABOUT a brand is brand-untagged (invisible to that brand's
# sessions — the bug that hid the Brand-A pre-filled-pens fact). Self-clears next clean run.
BSSTATUS="$HOME/.mem0/brand-scope-status.json"
if [ -f "$BSSTATUS" ]; then
  nmis=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('n_misscoped',0))" "$BSSTATUS" 2>/dev/null || echo 0)
  [ "${nmis:-0}" -gt 0 ] && warnings+="brand-scope: ${nmis} canonical fact(s) brand-untagged (invisible to brand sessions; see brand-scope-audit.py). "
fi

# Contradiction-resolve trigger (2026-06-30): run the SAFE Codex resolver when the shim is UP
# (session-time only — it is NOT up at the Sun 05:00 systemd timer). It CLEARS false advisory flags
# + QUEUES genuine contradictions for human review (NEVER auto-hides — Codex over-promotes: a live
# run hid 3/4 CONSISTENT facts). Weekly-throttled; detached so it never blocks SessionStart. The
# weekly local sweep keeps minting advisory flags; this is what authoritatively resolves them.
RESOLVE_MARKER="$HOME/.mem0/last-contradiction-rejudge"
if [ -n "${MEM0_REPO_ROOT_WSL:-}" ]; then
  _do=1
  if [ -f "$RESOLVE_MARKER" ]; then
    _age=$(( ( $(date +%s) - $(stat -c %Y "$RESOLVE_MARKER" 2>/dev/null || echo 0) ) / 86400 ))
    [ "$_age" -lt 7 ] && _do=0
  fi
  # v1.12: gate on MEM0_UP (cold morning = no sweep) + exec the DEPLOYED copy
  # (~/apps/mem0-scripts, B1/MEM-7), never the dev working tree. Placement stays at
  # SessionStart deliberately: the Codex shim is only reliably up at session time,
  # and the resolver is queue-only (never auto-hides) by design.
  if [ "$_do" = 1 ] && [ "${MEM0_UP:-0}" = 1 ] && curl -sf --max-time 2 http://127.0.0.1:18792/health >/dev/null 2>&1; then
    touch "$RESOLVE_MARKER"
    _PYB="$HOME/apps/mem0-server/.venv/bin/python"; [ -x "$_PYB" ] || _PYB=python3
    _SWEEP="$HOME/apps/mem0-scripts/contradiction-sweep.py"
    [ -f "$_SWEEP" ] || _SWEEP="$MEM0_REPO_ROOT_WSL/scripts/wsl/contradiction-sweep.py"
    nohup "$_PYB" "$_SWEEP" --rejudge-stamped --judge codex --apply >/dev/null 2>&1 &
  fi
fi

# Contradiction review queue: the safe resolver QUEUES genuine contradictions for human review
# instead of auto-hiding — surface the outstanding count so the operator promotes the real ones.
# MEM-13 (2026-07-03): own line, not the [storage-cap] warnings blob — the queue must be visible
# even when nothing is over cap. /health/deep mirrors it as checks.pending_contradiction_reviews.
RQ="$HOME/.mem0/contradiction-promote-review.jsonl"
if [ -s "$RQ" ]; then
  nrev=$(grep -c . "$RQ" 2>/dev/null)
  [ "${nrev:-0}" -gt 0 ] && echo "${nrev} contradiction verdict(s) await review (genuine? -> contradiction-sweep.py --promote <id>; list -> ~/.mem0/contradiction-promote-review.jsonl)"
fi

[ -n "$warnings" ] && echo "[storage-cap] $warnings Triage with: python scripts/wsl/audit-flags-triage.py --summary  (then --resolve --reason ...)."
exit 0
