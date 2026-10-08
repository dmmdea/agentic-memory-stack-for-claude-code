#!/usr/bin/env bash
# upgrade-check.sh - READ-ONLY inventory + outdated scan for the agentic-memory-stack
# components (mem0-server Python venv, Qdrant, llama-swap, Codex CLI). Classifies the
# Python updates as SECURITY (pip-audit CVE) / SAFE / MAJOR so the
# /upgrade-memory-stack skill can present a plan. Makes NO changes.
#
# Dependency policy: floors, not caps. The installer sets minimums (cryptography, starlette,
# pyjwt, mem0ai) and nothing pins a version below "latest"; the /health/deep sparse_leg canary and
# the hook contract are the tripwires for a breaking release. Only the transitive majors below
# are held back.
#   protobuf/thinc  majors held (breaking-change risk; transitive via mem0ai[nlp]/spaCy)
#
# Exit status: 0 = every leg ran; 2 = the security scan could not run (no venv, no pip-audit,
# or pip-audit died without a verdict). When pip-audit is missing the OSV querybatch fallback
# still lists advisories, but the status stays 2: a missing scanner is a defect to fix, not a
# clean bill of health. Findings do not change the exit status; read the output.
#
# Test hooks (env): MEM0_VENV (venv path), LLAMA_SWAP_BIN (llama-swap binary to interrogate).
set -uo pipefail
VENV="${MEM0_VENV:-$HOME/apps/mem0-server/.venv}"
PIP="$VENV/bin/pip"
POLICY_PINNED=""                      # no package is held below latest by policy (floors only)
MAJOR_HELD="protobuf thinc"           # majors we deliberately keep
RC=0                                  # 2 once the security scan could not run

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
  echo "  security scan UNAVAILABLE: no venv to scan"
  RC=2
else
  AUDIT_BIN="$VENV/bin/pip-audit"
  if [ -x "$AUDIT_BIN" ]; then
    AUDIT=$("$AUDIT_BIN" 2>&1); AUDIT_RC=$?   # 2>&1: pip-audit prints its verdict to stderr
    ROWS=$(echo "$AUDIT" | grep -iE "GHSA|CVE|PYSEC|Fix Versions" | head -20)
    if echo "$AUDIT" | grep -qi "No known vulnerabilities"; then
      echo "  security (pip-audit): clean (no known CVEs)"
    elif [ -n "$ROWS" ]; then
      echo "  security (pip-audit): REVIEW:"; echo "$ROWS" | sed 's/^/    /'
    else
      echo "  security scan UNAVAILABLE: pip-audit exited $AUDIT_RC without a verdict:"
      echo "$AUDIT" | head -3 | sed 's/^/    /'
      RC=2
    fi
  else
    echo "  security scan UNAVAILABLE: pip-audit is not installed in this venv"
    echo "    fix: $PIP install pip-audit   (the installer does this on a re-run)"
    RC=2
    # No scanner, but the advisory database is public: ask OSV about the installed set.
    echo "  fallback (OSV querybatch over the installed set):"
    FREEZE=$(mktemp)
    "$PIP" list --format=json 2>/dev/null > "$FREEZE"
    OSV_OUT=$(python3 - "$FREEZE" <<'PY'
import json, subprocess, sys
try:
    pkgs = json.load(open(sys.argv[1]))
    queries = [{"package": {"name": p["name"], "ecosystem": "PyPI"}, "version": p["version"]} for p in pkgs]
    if not queries:
        raise ValueError("empty package list")
    body = subprocess.run(
        ["curl", "-s", "-f", "-m", "60", "-X", "POST", "-H", "Content-Type: application/json",
         "--data-binary", "@-", "https://api.osv.dev/v1/querybatch"],
        input=json.dumps({"queries": queries}), capture_output=True, text=True, timeout=90, check=True,
    ).stdout
    results = json.loads(body)["results"]
    if len(results) != len(queries):
        raise ValueError("OSV returned %d results for %d packages" % (len(results), len(queries)))
except Exception as exc:
    print("ERROR %s" % exc)
    sys.exit(0)
rows = []
for q, res in zip(queries, results):
    ids = sorted({v["id"] for v in (res.get("vulns") or [])})
    if ids:
        rows.append("%s==%s: %s" % (q["package"]["name"], q["version"], ", ".join(ids)))
print("\n".join(rows) if rows else "CLEAN %d" % len(queries))
PY
)
    rm -f "$FREEZE"
    case "$OSV_OUT" in
      ERROR*) echo "    OSV fallback failed (${OSV_OUT#ERROR }); the security scan did not run at all" ;;
      CLEAN*) echo "    no known advisories for ${OSV_OUT#CLEAN } installed packages (OSV)" ;;
      *)      echo "    REVIEW (OSV advisories):"; echo "$OSV_OUT" | sed 's/^/      /' ;;
    esac
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
show("PINNED (held below latest by policy)", pins)
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
LB="${LLAMA_SWAP_BIN:-}"
[ -z "$LB" ] && LB=$(ps -eo args 2>/dev/null | grep -i "[l]lama-swap" | head -1 | awk '{print $1}')
[ -z "$LB" ] && LB=$(command -v llama-swap 2>/dev/null)
LV=""
# `version: v256 (6701d0d), built at ...` today, `version: 230` before the v prefix appeared.
[ -n "$LB" ] && [ -x "$LB" ] && LV=$("$LB" --version 2>/dev/null | grep -oiE 'version:? *v?[0-9]+' | grep -oE '[0-9]+' | head -1)
[ -z "$LV" ] && LV="?"
LL=$(gh_latest mostlygeek/llama-swap)
echo "  binary: ${LB:-?}"
if [ "$LV" = "?" ]; then
  LSTATE="UNKNOWN - could not read the installed version from '${LB:-?} --version'"
elif [ "$LV" = "$LL" ]; then
  LSTATE=CURRENT
else
  LSTATE='UPDATE - binary swap + config compat; affects the WHOLE ecosystem, extra caution'
fi
echo "  installed $LV | latest $LL | $LSTATE"

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
# The embedder generation is the active embedding profile's: mem0-server/embedder_profile.py is the one
# definition of it (env MEM0_EMBED_PROFILE > ~/.mem0/stack.env > default). python3 imports it (no
# third-party imports) from the repo-relative mem0-server, then the deployed ~/apps/mem0-server.
# A deploy that predates the module can only be on EmbeddingGemma-300m, the one embedder before
# profiles existed, so that is the only literal left; a profile the module rejects is reported, not guessed.
EMB_LABEL="EmbeddingGemma-300m"; EMB_PROFILE=""; EMB_ALIAS=""; EMB_DEFAULT=1; EMB_ERR=""
EMB_OUT=$(python3 -c '
import os, sys
for d in sys.argv[1:]:
    if os.path.isfile(os.path.join(d, "embedder_profile.py")):
        sys.path.insert(0, d)
        break
else:
    print("no-module"); sys.exit(0)
import embedder_profile as ep
try:
    p = ep.active()
except SystemExit as e:
    print("unresolved|" + str(e).replace("|", "/")); sys.exit(0)
print("ok|%s|%s|%s|%d" % (p.label, p.name, ep.embed_model(p), p.name == ep.DEFAULT_PROFILE))
' "$(dirname "${BASH_SOURCE[0]}")/../mem0-server" "$HOME/apps/mem0-server" 2>/dev/null) || EMB_OUT="unresolved|python3 could not import embedder_profile"
case "$EMB_OUT" in
  ok\|*) IFS='|' read -r _ EMB_LABEL EMB_PROFILE EMB_ALIAS EMB_DEFAULT <<< "$EMB_OUT" ;;
  unresolved\|*) EMB_LABEL="embedding profile unresolved"; EMB_ERR="${EMB_OUT#unresolved|}" ;;
  *) ;;                                  # no-module: a pre-profile deploy, EmbeddingGemma-300m
esac
echo "## Models  (${EMB_LABEL}, bge-reranker-v2-m3)"
[ -n "$EMB_ERR" ] && echo "  ! $EMB_ERR"
[ "$EMB_DEFAULT" != "1" ] && echo "  embedding profile: $EMB_PROFILE (llama-swap alias $EMB_ALIAS) - its vectors live in their own collections"
echo "  fixed GGUF artifacts - a model change is a DELIBERATE swap (re-embed + re-eval), NOT an auto-upgrade."
echo ""
echo "Next: /upgrade-memory-stack applies the SAFE set with snapshot -> full-suite gate -> rollback."
[ "$RC" -ne 0 ] && echo "NOTE: exiting $RC - the security scan did not complete (see 'security scan UNAVAILABLE' above)."
exit "$RC"
