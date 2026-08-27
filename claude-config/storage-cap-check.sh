#!/usr/bin/env bash
# STEP 16 — SessionStart hook: warn if any memory store exceeds hard cap.
# v0.12: NO cold-archive tier, so we enforce growth via caps + L7 decay.
# v0.17 Phase 0.E: brand context auto-load (brand inferred from cwd).
# Always exits 0. Emits a single banner line if anything's over cap (else silent).

set +e
# W4: unconditional liveness stamp for the sessionstart-banner capability row. It sits
# ABOVE the MEM0_UP cold gate on purpose — on a cold morning every server-dependent
# section below is skipped, and the hook still ran. Fail-open (|| true).
mkdir -p "$HOME/.mem0" 2>/dev/null && date +%s > "$HOME/.mem0/last-sessionstart-banner" 2>/dev/null || true
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
# matched rule's "campaign" (e.g. campaign-a). Empty = no funnel (store/shared).
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

# Auto-memory (System A) lint surface. memory-lint.ps1 writes this summary from the SessionStart
# spawn; it is a plain recompute with no watermark, so a count here is always current. Only the
# ACTIONABLE classes reach the banner (orphan / dangling / duplicate slug / over a hard cap /
# the compactor having gone silent / the history repo growing a remote) - long-line noise is left
# to the write-time hook, which reports it where it can actually be fixed. A run receipt from the
# last 24h is surfaced so a night's changes are visible without opening anything.
_AMLINT=""
for _c in "$HOME/.claude/state/automemory/lint-summary.json" "/mnt/c/Users/$USER/.claude/state/automemory/lint-summary.json"; do
  [ -f "$_c" ] && { _AMLINT="$_c"; break; }
done
if [ -z "$_AMLINT" ] && [ -n "${_WINPROFILE:-}" ] && [ -f "$_WINPROFILE/.claude/state/automemory/lint-summary.json" ]; then
  _AMLINT="$_WINPROFILE/.claude/state/automemory/lint-summary.json"
fi
if [ -z "$_AMLINT" ]; then
  case "${BASH_SOURCE[0]:-}" in
    /mnt/c/Users/*)
      _amp="$(echo "${BASH_SOURCE[0]}" | sed -E 's#^(/mnt/c/Users/[^/]+)/.*#\1#')/.claude/state/automemory/lint-summary.json"
      [ -f "$_amp" ] && _AMLINT="$_amp" ;;
  esac
fi
if [ -n "$_AMLINT" ]; then
  _amline=$(AMLINT="$_AMLINT" python3 - <<'PY' 2>/dev/null
import json, os, datetime
try:
    d = json.load(open(os.environ["AMLINT"], encoding="utf-8"))
except Exception:
    raise SystemExit(0)
parts = []
# The lint fails open on every path and never rewrites this file on failure, so an old summary
# would otherwise be presented as this morning's truth. Report the staleness instead of counts.
gen = d.get("generated_at")
stale_h = None
if gen:
    try:
        import re as _re
        # Python 3.10 rejects more than 6 fractional digits; trim so an older runtime cannot
        # make this guard inert and silently present stale counts as current.
        g = _re.sub(r"(\.\d{1,6})\d*", r"\1", gen.replace("Z", "+00:00"))
        t = datetime.datetime.fromisoformat(g)
        stale_h = round((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600.0, 1)
    except Exception:
        stale_h = None
if stale_h is not None and stale_h > 12:
    parts.append(f"auto-memory lint: STALE ({stale_h}h old) - memory-lint.ps1 has not completed since then")
else:
    c = d.get("counts") or {}
    n = int(c.get("actionable") or 0)
    if n:
        kinds = {}
        for f in d.get("findings") or []:
            k = f.get("kind")
            if k in ("orphan", "dangling", "dup-slug", "over-sync-limit", "over-inject-limit",
                     "compactor-silent", "compactor-unproductive", "history-remote", "scan-error"):
                kinds[k] = kinds.get(k, 0) + 1
        detail = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
        parts.append(f"auto-memory lint: {n} actionable ({detail})")
age = d.get("last_receipt_age_hours")
if age is not None and age <= 24:
    parts.append(f"compactor ran {age}h ago (receipt: ~/.claude/state/automemory/compact-receipts.jsonl)")
print(". ".join(parts))
PY
)
  [ -n "$_amline" ] && warnings+="$_amline. "
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
# a campaign-a session never sees campaign-b's rules and vice-versa.
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

# MEMORY.md staleness. W3/F19: MEMORY.md is rebuilt by memory-index-refresh.ps1
# on its own 6h SessionStart throttle, DECOUPLED from the dream — the old
# "dream-consolidator may be failing" hint misdiagnosed this for months.
MEMORYMD="$HOME/.mem0/MEMORY.md"
if [ -f "$MEMORYMD" ]; then
  age_days=$(( ( $(date +%s) - $(stat -c %Y "$MEMORYMD") ) / 86400 ))
  [ "$age_days" -gt 8 ] && warnings+="MEMORY.md stale (${age_days}d old; memory-index-refresh.ps1 is not firing at SessionStart). "
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
  # 2026-08-24 judge-resilience: at SessionStart the spawn hook may still be binding
  # the shim when the old inline probe fired (--max-time 2), which silently skipped
  # whole rejudge weeks. ensure-codex-shim.sh (idempotent, bounded) closes that — but
  # it can legitimately take ~30s+, and THIS hook runs synchronously in SessionStart:
  # blocking here past the harness hook timeout would kill the whole hook's output
  # (brand auto-load, cap warnings). So the ENTIRE ensure→gate→rejudge chain runs in
  # a detached subshell; SessionStart latency is unchanged, and a failed ensure
  # degrades to the exact pre-fix behavior (probe misses, rejudge skips this week).
  if [ "$_do" = 1 ] && [ "${MEM0_UP:-0}" = 1 ]; then
    _ENSURE="$HOME/apps/mem0-scripts/ensure-codex-shim.sh"
    [ -f "$_ENSURE" ] || _ENSURE="$MEM0_REPO_ROOT_WSL/scripts/wsl/ensure-codex-shim.sh"
    _PYB="$HOME/apps/mem0-server/.venv/bin/python"; [ -x "$_PYB" ] || _PYB=python3
    _SWEEP="$HOME/apps/mem0-scripts/contradiction-sweep.py"
    [ -f "$_SWEEP" ] || _SWEEP="$MEM0_REPO_ROOT_WSL/scripts/wsl/contradiction-sweep.py"
    (
      [ -f "$_ENSURE" ] && bash "$_ENSURE" 45 >/dev/null 2>&1
      if curl -sf --max-time 2 http://127.0.0.1:18792/health >/dev/null 2>&1; then
        # marker touched only when the rejudge actually fires — a failed ensure must
        # not burn the weekly throttle on a run that never happened.
        touch "$RESOLVE_MARKER"
        nohup "$_PYB" "$_SWEEP" --rejudge-stamped --judge codex --apply >/dev/null 2>&1 &
      fi
    ) >/dev/null 2>&1 &
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

# W3 AMS-05 heartbeat digest — one own-line (MEM-13 convention), silent when all
# clear. CHEAP FILE READS ONLY (the 1s cold-morning guard exists because serial
# curls once blocked sessions 15-30s; /health/deep is never called inline here).
# Windows-side files are reached via this script's own /mnt/c deployment path
# (review F10 — never $HOME for Windows artifacts: the two-homes bug class).
# No ack-stamp by design (review F11): overnight autonomous sessions would
# false-ack; the section count is a 48h window (idempotent, race-free) and
# alarm items key on live state so they re-show until actually cleared.
_hb=""
_WINPROFILE=""
case "${BASH_SOURCE[0]:-}" in
  /mnt/c/Users/*) _WINPROFILE="$(echo "${BASH_SOURCE[0]}" | sed -E 's#^(/mnt/c/Users/[^/]+)/.*#\1#')" ;;
esac
if [ -n "$_WINPROFILE" ]; then
  _MS="$_WINPROFILE/.claude/state/dream/morning-summary.md"
  if [ -f "$_MS" ]; then
    # Tail-read (bounded — the file rotates at ~128KB but be defensive) and count
    # sections whose header date parses within 48h. Headers may carry mojibake
    # dashes from pre-W3 PS 5.1 runs — match on the leading '## ' + date shape only.
    _cutoff=$(( $(date +%s) - 172800 ))
    _nsec=$(tail -c 65536 "$_MS" 2>/dev/null | grep -E '^## ' | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}' | while read -r _d; do
      _e=$(date -d "$_d" +%s 2>/dev/null) || continue
      [ "$_e" -ge "$_cutoff" ] && echo x
    done | wc -l)
    [ "${_nsec:-0}" -gt 0 ] && _hb+="${_nsec} morning-summary section(s) in last 48h (review: ~/.claude/state/dream/morning-summary.md). "
  fi
fi
_RDS="$HOME/.mem0/retrieval-drift-state.json"
if [ -f "$_RDS" ]; then
  _rd=$(python3 -c "
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
bits=[]
if d.get('alarm'): bits.append('DRIFT ALARM standing (%s/%s retrievable, hwm %s)' % (d.get('before_retrievable'), d.get('n_total'), d.get('hwm')))
if int(d.get('consecutive_snapshot_failures') or 0) >= 2: bits.append('DRIFT GUARD DEAD (>=2 snapshot failures)')
if d.get('compat_fallback') and not d.get('last_compare_ts'): bits.append('drift guard in compat-fallback (update moat checkout)')
print('; '.join(bits))" "$_RDS" 2>/dev/null)
  [ -n "$_rd" ] && _hb+="$_rd. "
fi
_CSW="$HOME/.mem0/contradiction-sweep.jsonl"
if [ -s "$_CSW" ]; then
  # grep -c prints the count even on zero matches (exit 1) — no ||-fallback, it
  # would append a second line and break the -ge integer test (diff-review fix 4).
  # W5 T3.5: the streak reads NORMAL sweep lines only — mode-tagged lines
  # (rejudge-stamped / evidence-sweep / retrieval-pairs) have their own
  # semantics and a burst of them must not fake a dead judgment leg.
  _noop=$(grep -v '"mode":' "$_CSW" 2>/dev/null | tail -n 3 | grep -c '"outcome": *"no-op' 2>/dev/null)
  [ "${_noop:-0}" -ge 3 ] && _hb+="contradiction sweep: 3+ consecutive no-op runs (judgment leg dead — see AMS-07). "
fi
# W6 D5: job-queue signals — a cheap FILE read of the jobs.py mirror (never
# the sqlite db; the banner's no-/health/deep pin applies to db opens in
# spirit). Age-gated (F7d): a stale mirror is itself the alarm.
_JHB="$HOME/.mem0/jobs-heartbeat.json"
if [ -s "$_JHB" ]; then
  _jq=$(python3 -c "
import json, datetime as dt
try:
    d = json.load(open('$_JHB'))
    ts = dt.datetime.fromisoformat(str(d.get('ts')).replace('Z', '+00:00'))
    age_h = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600
    bits = []
    if age_h > 192: bits.append('job-queue mirror STALE %.0fh (dead queue?)' % age_h)
    if int(d.get('failed_24h') or 0) > 0: bits.append('%d job(s) FAILED in 24h (jobs.py status <name>)' % int(d['failed_24h']))
    if d.get('oldest_queued_age_s') and float(d['oldest_queued_age_s']) > 172800: bits.append('a queued job has waited %.0fh (stuck row?)' % (float(d['oldest_queued_age_s'])/3600))
    if d.get('oldest_running_age_s') and float(d['oldest_running_age_s']) > 172800: bits.append('a job has been RUNNING %.0fh (wedged claim - jobs.py status <name>)' % (float(d['oldest_running_age_s'])/3600))
    print('; '.join(bits))
except Exception:
    pass" 2>/dev/null)
  [ -n "$_jq" ] && _hb+="$_jq. "
fi
[ -n "$_hb" ] && echo "[heartbeat] $_hb"

[ -n "$warnings" ] && echo "[storage-cap] $warnings Triage with: python scripts/wsl/audit-flags-triage.py --summary  (then --resolve --reason ...)."
exit 0
