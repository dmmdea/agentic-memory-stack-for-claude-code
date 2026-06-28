#!/usr/bin/env bash
# upgrade-check.sh - READ-ONLY inventory + outdated scan for the agentic-memory-stack
# components (mem0-server Python venv, Qdrant, llama-swap, Codex CLI). Classifies the
# Python updates as SECURITY (pip-audit CVE) / SAFE / MAJOR / PINNED so the
# /upgrade-memory-stack skill can present a plan. Makes NO changes.
#
# Pin policy (intentional holds - bump only via UPGRADE.md, then re-test + VERSIONS.md):
#   cryptography  capped <49   (GHSA-537c-gmf6-5ccf; as-tested 48.x - see VERSIONS.md)
#   mem0ai        ==2.0.4      (stack is built/tested on this exact version)
#   protobuf/thinc  majors held (breaking-change risk; transitive via mem0ai[nlp]/spaCy)
set -uo pipefail
VENV="${MEM0_VENV:-$HOME/apps/mem0-server/.venv}"
PIP="$VENV/bin/pip"
POLICY_PINNED="cryptography mem0ai"   # held by policy (not merely a major bump)
MAJOR_HELD="protobuf thinc"           # majors we deliberately keep

# latest release tag from the public GitHub API (no gh CLI / no auth needed in WSL)
gh_latest() {
  curl -s "https://api.github.com/repos/$1/releases/latest" 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('tag_name','?'))" 2>/dev/null \
    | sed 's/^v//' || echo "?"
}

echo "=== agentic-memory-stack upgrade check ($(date -u +%FT%TZ)) - READ ONLY ==="
echo ""
echo "## Python deps  (venv: $VENV)"
if [ ! -x "$PIP" ]; then
  echo "  ! venv pip not found at $PIP (set MEM0_VENV)"
else
  echo -n "  security (pip-audit): "
  AUDIT=$("$VENV/bin/pip-audit" 2>&1 || true)   # 2>&1: pip-audit prints its verdict to stderr
  if echo "$AUDIT" | grep -qi "No known vulnerabilities"; then
    echo "clean (no known CVEs)"
  else
    echo "REVIEW:"; echo "$AUDIT" | grep -iE "GHSA|CVE|PYSEC|Fix Versions" | head -20 | sed 's/^/    /'
  fi
  OUT=$(mktemp)
  "$PIP" list --outdated --format=json 2>/dev/null > "$OUT"   # to a file: the heredoc below owns stdin
  python3 - "$POLICY_PINNED" "$MAJOR_HELD" "$OUT" <<'PY'
import sys, json
pinned = set(sys.argv[1].split()); major_held = set(sys.argv[2].split())
def major(v):
    try: return int(str(v).split('.')[0])
    except Exception: return None
try:
    rows = json.load(open(sys.argv[3]))
except Exception:
    rows = []
safe, majors, pins = [], [], []
for p in rows:
    n, cur, lat = p['name'], p['version'], p['latest_version']
    mc, ml = major(cur), major(lat)
    is_major = mc is not None and ml is not None and ml > mc
    if n.lower() in pinned:                   pins.append(f"{n} {cur}->{lat} (policy pin)")
    elif n.lower() in major_held or is_major: majors.append(f"{n} {cur}->{lat}")
    else:                                     safe.append(f"{n} {cur}->{lat}")
def show(title, items):
    print(f"  {title} ({len(items)}):")
    print("\n".join(f"    - {i}" for i in items) if items else "    (none)")
show("SAFE (no CVE, minor/patch - eligible for the safe-upgrade pass)", safe)
show("MAJOR (breaking-change risk - hold unless needed, then test in isolation)", majors)
show("PINNED (policy - bump ONLY via UPGRADE.md)", pins)
PY
  rm -f "$OUT"
fi

echo ""
echo "## Qdrant  (vector DB)"
QV=$(curl -s http://localhost:6333/ 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('version','?'))" 2>/dev/null || echo "?")
QL=$(gh_latest qdrant/qdrant)
echo "  installed $QV | latest $QL | $([ "$QV" = "$QL" ] && echo CURRENT || echo 'UPDATE - binary swap + service restart; back up first, verify collection compat')"

echo ""
echo "## llama-swap  (inference proxy - ECOSYSTEM-SHARED: serves embeddings+reranker+all local models)"
LB=$(ps -eo args 2>/dev/null | grep -i "[l]lama-swap" | head -1 | awk '{print $1}')
[ -z "$LB" ] && LB=$(command -v llama-swap 2>/dev/null)
LV=$([ -n "$LB" ] && [ -x "$LB" ] && "$LB" --version 2>/dev/null | grep -oiE 'version:? *[0-9]+' | grep -oE '[0-9]+' | head -1 || echo "?")
LL=$(gh_latest mostlygeek/llama-swap)
echo "  binary: ${LB:-?}"
echo "  installed $LV | latest $LL | $([ "$LV" = "$LL" ] && echo CURRENT || echo 'UPDATE - binary swap + config compat; affects the WHOLE ecosystem, extra caution')"

echo ""
echo "## Codex CLI  (LLM judge)"
if command -v codex >/dev/null 2>&1; then
  CV=$(codex --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | tail -1)
  CL=$(npm view @openai/codex version 2>/dev/null || echo "?")
  echo "  installed ${CV:-?} | latest $CL | $([ "$CV" = "$CL" ] && echo CURRENT || echo 'npm i -g @openai/codex@latest (low risk)')"
else
  echo "  codex not on this PATH (it is a Windows npm global) - check on Windows:"
  echo "    codex --version   vs   npm view @openai/codex version"
fi

echo ""
echo "## Models  (EmbeddingGemma-300m, bge-reranker-v2-m3)"
echo "  fixed GGUF artifacts - a model change is a DELIBERATE swap (re-embed + re-eval), NOT an auto-upgrade."
echo ""
echo "Next: /upgrade-memory-stack applies the SAFE set with snapshot -> full-suite gate -> rollback."
