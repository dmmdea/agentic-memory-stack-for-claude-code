#!/usr/bin/env bash
# install/linux-client.sh — the Linux THIN CLIENT install (native Linux, no WSL, no local store).
#
# A thin client is a box that uses a remote Brain's memory authority over the network and keeps
# nothing of its own except the Outbox. It installs:
#   - the MCP shim + the outbox replay driver into ~/.claude/scripts (the same two files the
#     Windows installer deploys into WSL), served by a small python venv;
#   - the per-host files the shim resolves at startup: ~/.mem0/authority-url, ~/.mem0/role
#     (= client) and ~/.mem0/api-key (copied from --api-key-file, mode 0600);
#   - the `mem0` MCP server entry in Claude Code (user scope), replacing any previous one;
#   - the CLAUDE.md memory tier protocol section (same marker as the Windows installer).
# It then proves the install end to end: a real MCP session over stdio calling memory_health
# against the authority. Idempotent: re-run to upgrade (scripts refreshed, floors re-applied).
#
# One-Brain Rule: a client has no local replica, so while the authority is unreachable reads
# return the shim's offline result and writes queue to ~/.mem0/outbox.jsonl; the shim drains the
# outbox the next time it starts with the authority reachable. A loopback authority is refused:
# nothing listens there on a client, and replay-ops.py refuses to replay into loopback for this
# role for the same reason it does on a replica.
#
# Usage:
#   bash install/linux-client.sh --authority http://<brain-host>:18791 --api-key-file <file> [--user-id <tenant>]
#   bash install/linux-client.sh --authority http://<brain-host>:18791            # key already in ~/.mem0/api-key
#   bash install/linux-client.sh --authority ... --dry-run                         # print the plan, write nothing
#
# --user-id is the mem0 tenant the Brain stores your memories under (its own install's WSL
# username). It defaults to this box's login name, which is only right when the two match.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AUTHORITY=""
API_KEY_FILE=""
USER_ID="${USER:-$(id -un)}"
DRY_RUN=0
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
CLIENT_DIR="${MEM0_CLIENT_DIR:-$HOME/apps/mem0-client}"
MEM0_DIR="$HOME/.mem0"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
# Parity with the Windows installer's WSL-side list minus l10-audit.py (a server-side audit
# that reads the store directly; a client has no store).
CLIENT_FILES="mem0-mcp-shim.py replay-ops.py"

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --authority) AUTHORITY="${2:-}"; shift 2 ;;
        --api-key-file) API_KEY_FILE="${2:-}"; shift 2 ;;
        --user-id) USER_ID="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 2 ;;
    esac
done

fail() { echo "FAIL: $*" >&2; exit 1; }
say()  { echo "==> $*"; }
plan() { if [ "$DRY_RUN" = 1 ]; then echo "    [dry-run] $*"; return 0; fi; return 1; }

is_local_url() {
    # Same fail-closed rule as replay-ops.py: loopback, unspecified, empty or malformed => local.
    local url="$1" host
    host="$(printf '%s' "$url" | sed -nE 's#^[a-zA-Z][a-zA-Z0-9+.-]*://\[?([^]/:]+)\]?(:[0-9]+)?(/.*)?$#\1#p')"
    [ -z "$host" ] && return 0
    case "$host" in
        127.*|localhost|0.0.0.0|::1|::) return 0 ;;
    esac
    return 1
}

# ---------------------------------------------------------------- 0. prerequisites
say "[0] prerequisites"
[ "$(id -u)" != 0 ] || fail "run as the user who runs Claude Code, not root"
[ -n "$AUTHORITY" ] || fail "--authority http://<brain-host>:18791 is required"
AUTHORITY="${AUTHORITY%/}"
if is_local_url "$AUTHORITY"; then
    fail "a thin client must point at a REMOTE authority; '$AUTHORITY' is loopback/unspecified/malformed (nothing listens locally on a client, and the One-Brain Rule forbids replaying into loopback)"
fi
[[ "$USER_ID" =~ ^[A-Za-z0-9._-]+$ ]] || fail "--user-id must be a plain tenant name (letters, digits, . _ -), got '$USER_ID'"
command -v python3 >/dev/null || fail "python3 is required"
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || fail "python3 >= 3.12 required (found $PYV)"
command -v curl >/dev/null || fail "curl is required"
command -v claude >/dev/null || fail "the Claude Code CLI ('claude') must be on PATH"
for f in $CLIENT_FILES; do
    [ -f "$REPO_ROOT/scripts/wsl/$f" ] || fail "missing $REPO_ROOT/scripts/wsl/$f (run from a repo checkout)"
done
STACK_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"
echo "    python $PYV, claude $(claude --version 2>/dev/null | head -1), stack $STACK_VERSION"
echo "    authority: $AUTHORITY"
echo "    user_id (mem0 tenant): $USER_ID"

# ---------------------------------------------------------------- 1. per-host files
say "[1] per-host files in $MEM0_DIR"
if plan "mkdir -p $MEM0_DIR (0700); write authority-url, role=client; api-key from ${API_KEY_FILE:-<existing>}"; then :; else
    mkdir -p "$MEM0_DIR"; chmod 700 "$MEM0_DIR"
    umask 077
    printf '# memory authority for this host (written by install/linux-client.sh)\n%s\n' "$AUTHORITY" > "$MEM0_DIR/authority-url"
    printf 'client\n' > "$MEM0_DIR/role"
    if [ -n "$API_KEY_FILE" ]; then
        [ -s "$API_KEY_FILE" ] || fail "--api-key-file '$API_KEY_FILE' is missing or empty"
        tr -d '[:space:]' < "$API_KEY_FILE" > "$MEM0_DIR/api-key"; printf '\n' >> "$MEM0_DIR/api-key"
    fi
    umask 022
    [ -s "$MEM0_DIR/api-key" ] || fail "no API key: pass --api-key-file <file> (the key the authority accepts, from the Brain's ~/.mem0/api-key)"
    chmod 600 "$MEM0_DIR/api-key" "$MEM0_DIR/authority-url" "$MEM0_DIR/role"
    echo "    authority-url, role=client, api-key ($(wc -c < "$MEM0_DIR/api-key") bytes) in place"
fi

# ---------------------------------------------------------------- 2. authority reachability
say "[2] authority health"
if plan "GET $AUTHORITY/health"; then :; else
    H="$(curl -fsS -m 10 "$AUTHORITY/health" 2>/dev/null || true)"
    printf '%s' "$H" | grep -q '"ok"[[:space:]]*:[[:space:]]*true' || fail "authority $AUTHORITY did not answer /health with ok:true (got: ${H:-<no answer>}). Is the Brain up and reachable from this box?"
    echo "    $H"
fi

# ---------------------------------------------------------------- 3. venv + scripts
say "[3] client venv at $CLIENT_DIR and scripts in $SCRIPTS_DIR"
if plan "python3 -m venv $CLIENT_DIR/.venv; pip install 'fastmcp>=3' httpx; deploy $CLIENT_FILES with __WSL_USER__ -> $USER_ID"; then :; else
    mkdir -p "$CLIENT_DIR" "$SCRIPTS_DIR"
    [ -x "$CLIENT_DIR/.venv/bin/python" ] || python3 -m venv "$CLIENT_DIR/.venv"
    "$CLIENT_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check --upgrade pip
    # Floors only, no caps (house rule): the shim needs fastmcp>=3 and httpx.
    "$CLIENT_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check 'fastmcp>=3' httpx
    "$CLIENT_DIR/.venv/bin/python" -c 'import fastmcp, httpx' || fail "shim dependencies failed to import in the venv"
    # The deployed copies carry the operator sentinel __WSL_USER__ (the mem0 tenant every tool
    # defaults to). The Windows installer resolves it at deploy time; so must this one, or every
    # write lands under a literal placeholder tenant. Literal, whole-token substitution.
    for f in $CLIENT_FILES; do
        sed "s|__WSL_USER__|$USER_ID|g" "$REPO_ROOT/scripts/wsl/$f" > "$SCRIPTS_DIR/$f"
        if grep -q "__WSL_USER__\|__WIN_USER__\|__WSL_DISTRO__" "$SCRIPTS_DIR/$f"; then fail "unresolved operator sentinel left in $SCRIPTS_DIR/$f"; fi
        echo "    installed: $f (tenant $USER_ID)"
    done
fi
SHIM="$SCRIPTS_DIR/mem0-mcp-shim.py"
PY="$CLIENT_DIR/.venv/bin/python"

# ---------------------------------------------------------------- 4. MCP registration
say "[4] Claude Code MCP entry 'mem0' (user scope)"
if plan "claude mcp remove mem0 -s user (if present); claude mcp add-json -s user mem0 {stdio: $PY $SHIM}"; then :; else
    if claude mcp get mem0 >/dev/null 2>&1; then
        claude mcp remove mem0 -s user >/dev/null 2>&1 || claude mcp remove mem0 >/dev/null 2>&1 || true
        echo "    replaced the previous mem0 entry"
    fi
    claude mcp add-json -s user mem0 "{\"type\":\"stdio\",\"command\":\"$PY\",\"args\":[\"$SHIM\"]}" >/dev/null
    claude mcp get mem0 >/dev/null 2>&1 || fail "claude mcp add-json did not register 'mem0'"
    echo "    registered: $PY $SHIM"
fi

# ---------------------------------------------------------------- 5. CLAUDE.md tier protocol
say "[5] CLAUDE.md memory tier protocol section"
CLAUDE_MD="$CLAUDE_DIR/CLAUDE.md"
SNIPPET="$REPO_ROOT/claude-config/claude-md-memory-protocol.md"
MARKER='## Memory tier protocol (agentic-memory-stack)'
if plan "append $SNIPPET to $CLAUDE_MD unless the marker is present"; then :; else
    if [ -f "$CLAUDE_MD" ] && grep -qF "$MARKER" "$CLAUDE_MD"; then
        echo "    already present (skipping)"
    elif [ -f "$SNIPPET" ]; then
        mkdir -p "$CLAUDE_DIR"
        [ -f "$CLAUDE_MD" ] && cp -a "$CLAUDE_MD" "$CLAUDE_MD.bak-$(date +%Y%m%d%H%M%S)"
        { [ -f "$CLAUDE_MD" ] && printf '\n\n'; cat "$SNIPPET"; } >> "$CLAUDE_MD"
        echo "    appended"
    else
        echo "    WARN: snippet not found at $SNIPPET - skipped"
    fi
fi

# ---------------------------------------------------------------- 6. receipt
say "[6] receipt"
if plan "write $MEM0_DIR/client-receipt.json"; then :; else
    SHA="$(sha256sum "$SHIM" | cut -c1-64)"
    printf '{"role":"client","authority":"%s","user_id":"%s","stack_version":"%s","shim_sha256":"%s","python":"%s","installed_at":"%s"}\n' \
        "$AUTHORITY" "$USER_ID" "$STACK_VERSION" "$SHA" "$PY" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$MEM0_DIR/client-receipt.json"
    echo "    $MEM0_DIR/client-receipt.json"
fi

# ---------------------------------------------------------------- 7. end-to-end proof
say "[7] end-to-end: a real MCP session over stdio calling memory_health against the authority"
if plan "spawn the shim, initialize, tools/call memory_health, expect ok:true"; then exit 0; fi
"$PY" - "$PY" "$SHIM" <<'PYEOF'
import json, subprocess, sys, time
py, shim = sys.argv[1], sys.argv[2]
p = subprocess.Popen([py, shim], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
def send(o):
    p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()
def recv(want_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        line = p.stdout.readline()
        if not line:
            break
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("id") == want_id:
            return m
    raise SystemExit("FAIL: no response for id %s; stderr: %s" % (want_id, p.stderr.read()[-800:]))
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "linux-client-install", "version": "1"}}})
init = recv(1)
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_health", "arguments": {}}})
r = recv(2)
p.stdin.close(); p.terminate()
res = r.get("result") or {}
text = ""
for c in res.get("content") or []:
    if c.get("type") == "text":
        text += c.get("text", "")
sc = res.get("structuredContent")
payload = sc if isinstance(sc, dict) and sc else None
if payload is None:
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {}
if isinstance(payload, dict) and "result" in payload and isinstance(payload["result"], dict):
    payload = payload["result"]
ok = bool(payload.get("ok")) if isinstance(payload, dict) else False
print("    server:", (init.get("result") or {}).get("serverInfo", {}).get("name"), "| memory_health:", json.dumps(payload)[:300])
if not ok:
    raise SystemExit("FAIL: memory_health did not report ok:true through the shim")
print("    END-TO-END OK")
PYEOF
echo
echo "Linux thin client installed. Verify any time with: claude mcp list   (mem0 must show Connected)"
