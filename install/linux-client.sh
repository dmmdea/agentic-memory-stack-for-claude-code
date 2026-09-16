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
USER_ID=""
DRY_RUN=0
AMS_HUB=""
AMS_BINARY=""
AMS_SUMS=""
AMS_RELEASE_REPO="dmmdea/agentic-memory-stack-for-claude-code"
AMS_BIN_DIR="${AMS_BIN_DIR:-$HOME/.local/bin}"
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
        --ams-hub) AMS_HUB="${2:-}"; shift 2 ;;
        --ams-store-binary) AMS_BINARY="${2:-}"; shift 2 ;;
        --ams-store-sums) AMS_SUMS="${2:-}"; shift 2 ;;
        --ams-release-repo) AMS_RELEASE_REPO="${2:-}"; shift 2 ;;
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
# v1.23.2: the tenant INHERITS on a re-run — an omitted --user-id takes the tenant in the previous
# ~/.mem0/client-receipt.json; only a first install falls back to the login name (the Linux user
# and the mem0 tenant usually differ; a re-run used to rewrite the shim under the wrong tenant).
if [ -z "$USER_ID" ] && [ -f "$MEM0_DIR/client-receipt.json" ]; then
    USER_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("user_id",""))' "$MEM0_DIR/client-receipt.json" 2>/dev/null || true)"
    [ -z "$USER_ID" ] || echo "    tenant inherited from $MEM0_DIR/client-receipt.json: $USER_ID"
fi
[ -n "$USER_ID" ] || USER_ID="${USER:-$(id -un)}"
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

# ---------------------------------------------------------------- 5b. the fleet store
# A client joins the fleet store by holding the binary, the hub transport and the three hooks
# that drive it. It is deliberately ROLELESS: the `role` file is what makes judge-apply willing
# to decide, and only the authority's checkout may carry it. Skipped entirely without --ams-hub,
# so a plain thin client installs exactly as it did before.
say "[5b] ams-store + hub transport"
ams_hub_user() { printf '%s' "${AMS_HUB%%@*}"; }
ams_hub_host() { local r="${AMS_HUB#*@}"; printf '%s' "${r%%:*}"; }
ams_hub_repo() { printf '%s' "${AMS_HUB##*:}"; }

ams_install_binary() {
    local tag="v$STACK_VERSION" asset="ams-store-linux-amd64" dest="$AMS_BIN_DIR/ams-store"
    local src="" want="" have="" sums="" cleanup=""
    case "$(uname -m)" in
        aarch64|arm64) asset="ams-store-linux-arm64" ;;
    esac
    mkdir -p "$AMS_BIN_DIR"
    if [ -n "$AMS_BINARY" ]; then
        [ -r "$AMS_BINARY" ] || fail "--ams-store-binary $AMS_BINARY is not readable"
        src="$AMS_BINARY"
        if [ -n "$AMS_SUMS" ]; then
            [ -r "$AMS_SUMS" ] || fail "--ams-store-sums $AMS_SUMS is not readable"
            want="$(awk -v a="$asset" '{ n=$2; sub(/^\*/,"",n); if (n==a) print $1 }' "$AMS_SUMS" | head -n1)"
            [ -n "$want" ] || fail "--ams-store-sums has no entry for $asset"
        else
            echo "    WARN: --ams-store-binary without --ams-store-sums: the drop is installed UNVERIFIED"
        fi
    else
        local base="https://github.com/$AMS_RELEASE_REPO/releases/download/$tag"
        sums="$(mktemp)"; src="$(mktemp)"; cleanup="$sums $src"
        curl -fsSL -o "$sums" "$base/SHA256SUMS" \
            || fail "cannot fetch $base/SHA256SUMS; pass --ams-store-binary <file> --ams-store-sums <file> for an offline drop"
        want="$(awk -v a="$asset" '{ n=$2; sub(/^\*/,"",n); if (n==a) print $1 }' "$sums" | head -n1)"
        [ -n "$want" ] || fail "release $tag: SHA256SUMS has no entry for $asset"
        if [ -x "$dest" ] && [ "$(sha256sum "$dest" | awk '{print $1}')" = "$want" ]; then
            echo "    ams-store already current ($("$dest" --version 2>/dev/null | head -n1))"
            printf '%s\n' "$want" > "$dest.sha256"
            rm -f $cleanup; return 0
        fi
        curl -fsSL -o "$src" "$base/$asset" || fail "cannot fetch $base/$asset"
    fi
    have="$(sha256sum "$src" | awk '{print $1}')"
    if [ -n "$want" ] && [ "$have" != "$want" ]; then
        [ -z "$cleanup" ] || rm -f $cleanup
        fail "checksum mismatch for $asset ($tag): SHA256SUMS says $want, the file is $have - not installed"
    fi
    # No sudo: a client owns its own bin directory. An authority needs /usr/local/bin because a
    # systemd unit runs the binary; nothing on a client runs it but the user's own session.
    install -m 0755 "$src" "$dest" || { [ -z "$cleanup" ] || rm -f $cleanup; fail "cannot install $dest"; }
    printf '%s\n' "$have" > "$dest.sha256"
    [ -z "$cleanup" ] || rm -f $cleanup
    echo "    installed: $("$dest" --version 2>/dev/null | head -n1) (sha256 ${have:0:12}...)"
}

ams_ssh_block() {  # $1 = hub host, $2 = hub user
    local cfg="$HOME/.ssh/config" begin="# >>> ams-store hub (managed by linux-client.sh)" end="# <<< ams-store hub"
    mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; touch "$cfg"; chmod 600 "$cfg"
    if grep -qF "$begin" "$cfg"; then
        awk -v b="$begin" -v e="$end" 'index($0,b){skip=1} !skip{print} index($0,e){skip=0}' "$cfg" > "$cfg.ams.tmp"
        mv "$cfg.ams.tmp" "$cfg"; chmod 600 "$cfg"
    fi
    printf '\n%s\nMatch host %s user %s\n    IdentityFile ~/.ssh/id_ed25519_ams_hub\n    IdentitiesOnly yes\n%s\n' \
        "$begin" "$1" "$2" "$end" >> "$cfg"
}

ams_setup_client_store() {
    local host user repo proj state
    host="$(ams_hub_host)"; user="$(ams_hub_user)"; repo="$(ams_hub_repo)"
    [ -n "$host" ] && [ -n "$user" ] && [ -n "$repo" ] || fail "--ams-hub must be user@host:repo.git, got '$AMS_HUB'"
    case "$host" in *.*) fail "--ams-hub host '$host' is dotted; reach is the tailnet MagicDNS name only" ;; esac
    proj="$CLAUDE_DIR/projects"; state="$CLAUDE_DIR/state/automemory"
    mkdir -p "$proj" "$state"
    # No role file here, ever: a PC that carried `hub` would start applying judge plans.
    [ -e "$state/role" ] && fail "$state/role exists on a client; remove it (only the authority's checkout is the hub)"
    ams_ssh_block "$host" "$user"
    # The binary pins UserKnownHostsFile=<state>/known_hosts with StrictHostKeyChecking=yes, so
    # an unseeded client fails every sync silently.
    if ssh-keygen -F "$host" -f "$HOME/.ssh/known_hosts" 2>/dev/null | grep -v '^#' > "$state/known_hosts"; then :; fi
    [ -s "$state/known_hosts" ] || fail "the hub's host key is not in ~/.ssh/known_hosts (accept it once: ssh $user@$host) - the store is not usable without it"
    [ -r "$HOME/.ssh/id_ed25519_ams_hub" ] || fail "the hub identity key ~/.ssh/id_ed25519_ams_hub is absent (provision this box's key on the hub first)"
    if [ ! -f "$state/history.git/HEAD" ]; then
        git --git-dir "$state/history.git" --work-tree "$proj" init -q -b main
        echo "    history repo created at $state/history.git (branch main)"
    fi
    local want="$user@$host:$repo" have
    have="$(git --git-dir "$state/history.git" --work-tree "$proj" remote get-url hub 2>/dev/null || true)"
    for r in $(git --git-dir "$state/history.git" --work-tree "$proj" remote 2>/dev/null); do
        [ "$r" = hub ] || { git --git-dir "$state/history.git" --work-tree "$proj" remote remove "$r"; echo "    removed remote '$r' (the remote policy allows only hub)"; }
    done
    if [ -z "$have" ]; then
        git --git-dir "$state/history.git" --work-tree "$proj" remote add hub "$want"
    elif [ "$have" != "$want" ]; then
        git --git-dir "$state/history.git" --work-tree "$proj" remote set-url hub "$want"
    fi
    echo "    store ready (roleless client, remote hub=$want, known_hosts seeded)"
}

if [ -z "$AMS_HUB" ]; then
    echo "    no --ams-hub: this client does not join the fleet store (skipped)"
elif plan "install ams-store into $AMS_BIN_DIR, wire the hub transport for $AMS_HUB, register the gate + sync hooks"; then :; else
    command -v git >/dev/null 2>&1 || fail "git is required for the fleet store"
    ams_install_binary
    ams_setup_client_store
    "$PY" "$REPO_ROOT/claude-config/register-ams-hooks.py" \
        --settings "$CLAUDE_DIR/settings.json" \
        --binary "$AMS_BIN_DIR/ams-store" \
        --hub-host "$(ams_hub_host)" || fail "registering the ams-store hooks failed"
fi

# ---------------------------------------------------------------- 5c. L1a capture
# The capture path is what makes a box contribute to the corpus rather than only read from it:
# a Stop / PreCompact / SessionStart hook spawns a worker that reads the just-finished transcript,
# asks the Codex CLI to extract durable facts, and posts them to the authority. It is written in
# PowerShell and shared byte-for-byte with the Windows install, so a native Linux box needs pwsh 7
# and the codex CLI; without either, the block is skipped LOUDLY rather than silently, because a
# client that captures nothing looks identical to one that captures everything.
say "[5c] L1a capture (pwsh + codex)"
CAPTURE_FILES="memory-common.ps1 l1a-extract.ps1 stop-extract.ps1 sessionstart-capture.ps1"

ams_capture_prereqs() {   # prints what is missing; empty output means ready
    local missing=""
    command -v pwsh >/dev/null 2>&1 || missing="$missing pwsh"
    command -v codex >/dev/null 2>&1 || missing="$missing codex"
    [ -r "$HOME/.codex/auth.json" ] || missing="$missing ~/.codex/auth.json"
    printf '%s' "$missing"
}

CAPTURE_MISSING="$(ams_capture_prereqs)"
if [ -n "$CAPTURE_MISSING" ]; then
    echo "    SKIPPED - missing:$CAPTURE_MISSING"
    echo "    install pwsh 7 (snap install powershell --classic) and the codex CLI"
    echo "    (npm install -g @openai/codex, then 'codex login'), then re-run this installer."
elif plan "deploy $CAPTURE_FILES to $SCRIPTS_DIR with the tenant resolved, and register the Stop / PreCompact / SessionStart capture hooks"; then :; else
    for f in $CAPTURE_FILES; do
        src="$REPO_ROOT/scripts/windows/$f"
        [ -r "$src" ] || fail "capture script missing from the repo: $src"
        # The SAME substitution the shim gets: these files carry the operator sentinel, and an
        # unresolved one posts every extracted fact under a literal '__WSL_USER__' tenant - which
        # the authority accepts, so nothing fails and the facts are simply not where anyone looks.
        sed "s|__WSL_USER__|$USER_ID|g; s|__WIN_USER__|$USER_ID|g" "$src" > "$SCRIPTS_DIR/$f"
        if grep -q "__WSL_USER__\|__WIN_USER__\|__WSL_DISTRO__" "$SCRIPTS_DIR/$f"; then
            fail "unresolved operator sentinel left in $SCRIPTS_DIR/$f"
        fi
        echo "    installed: $f (tenant $USER_ID)"
    done
    # The store hooks are re-registered here only when this box also joined the store; capture
    # stands on its own, so a thin client can contribute to the corpus without a local store.
    HOOK_ARGS="--settings $CLAUDE_DIR/settings.json --pwsh $(command -v pwsh) --capture-dir $SCRIPTS_DIR"
    if [ -n "$AMS_HUB" ]; then
        HOOK_ARGS="$HOOK_ARGS --binary $AMS_BIN_DIR/ams-store --hub-host $(ams_hub_host)"
    fi
    # shellcheck disable=SC2086
    "$PY" "$REPO_ROOT/claude-config/register-ams-hooks.py" $HOOK_ARGS || fail "registering the capture hooks failed"
    echo "    codex: $(codex --version 2>/dev/null | head -n1)"
fi

# ---------------------------------------------------------------- 6. receipt
say "[6] receipt"
if plan "write $MEM0_DIR/client-receipt.json"; then :; else
    SHA="$(sha256sum "$SHIM" | cut -c1-64)"
    AMS_STORE_SHA=""
    [ -r "$AMS_BIN_DIR/ams-store.sha256" ] && AMS_STORE_SHA="$(head -n1 "$AMS_BIN_DIR/ams-store.sha256")"
    printf '{"role":"client","authority":"%s","user_id":"%s","stack_version":"%s","shim_sha256":"%s","python":"%s","ams_hub":"%s","ams_store_sha256":"%s","installed_at":"%s"}\n' \
        "$AUTHORITY" "$USER_ID" "$STACK_VERSION" "$SHA" "$PY" "$AMS_HUB" "$AMS_STORE_SHA" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$MEM0_DIR/client-receipt.json"
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
