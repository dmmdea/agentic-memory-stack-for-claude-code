#!/usr/bin/env bash
# wiki-index.sh — the LLM Wiki's semantic index (wiki_pages_<space>, the wiki collection of the
# embedding space in mem0-server/embedder_profile.py) from a REPLICA box.
#
# The index lives on the brain box's Qdrant (docs/systems/wiki-index.md). A replica's own
# Qdrant is dormant by design, so this wrapper reaches the brain's loopback Qdrant through an
# SSH tunnel; the embedder is this box's own llama-swap (mirrored networking: localhost in WSL).
# THIS box's profile (MEM0_EMBED_PROFILE in its stack.env, passed through below like the alias)
# chooses the collection and the model: it must be the brain's space for the pages to be findable.
#
#   wiki-index.sh snapshot   < tar of the vault's wiki/   -> ~/wiki-index/wiki
#   wiki-index.sh build                                    (snapshot -> brain Qdrant, then stamps the brain's
#                                                           ~/wiki-index/last-build: the freshness the nightly reads)
#   wiki-index.sh search "query" [--k N]                   (JSON lines, top-K pages)
#   wiki-index.sh build-here < tar of wiki/                 (ON THE BRAIN: build from a pushed snapshot)
#   wiki-index.sh search-here "query" [--k N]               (ON THE BRAIN: search its own Qdrant)
#
# The wiki has its own embedding space (MEM0_WIKI_EMBED_PROFILE), and the AUTHORITY's is the one
# that counts: build and search read it from the authority's /health/deep (embed_profile.wiki).
# When this box's llama-swap serves that space's model, the work runs here through the tunnel as
# before, in that space. When it does not, the snapshot (build) or the query (search) goes over
# SSH to the brain, which embeds with its own model: a replica can never write the wiki in another
# space than the authority's. An authority that reports no wiki space keeps the old behaviour.
#
# The snapshot step exists because the vault sits on a cloud-synced folder that WSL does not
# mount; the Windows side pushes wiki/ in as a tar stream (the operator's refresh driver).
#
# The brain's SSH alias resolves, in order: WIKI_BRAIN_SSH; MEM0_BRAIN_SSH in ~/.mem0/stack.env;
# the ~/.ssh/config Host whose HostName is the authority-url's host; that host itself.
# Env: WIKI_SNAPSHOT (dir), WIKI_TUNNEL_PORT (first of ten local ports, default 16333).
set -euo pipefail

SNAP="${WIKI_SNAPSHOT:-$HOME/wiki-index/wiki}"
PORT_BASE="${WIKI_TUNNEL_PORT:-16333}"
PY="${WIKI_PY:-$HOME/apps/mem0-server/.venv/bin/python}"
DIR="$(cd "$(dirname "$0")" && pwd)"

# The embedding space and the alias this box serves it under come from its own stack.env (the python
# reads the file itself; exporting is for a caller whose HOME differs). The environment wins; the alias
# overrides are scoped to their profile, as in wiki-index-nightly.sh.
export_from_stack() {  # <KEY>: export KEY from ~/.mem0/stack.env unless the environment already has it
    local k="$1" v=""
    [ -z "${!k:-}" ] || return 0
    [ -s "$HOME/.mem0/stack.env" ] && v="$(sed -n "s/^$k=//p" "$HOME/.mem0/stack.env" | head -n1 | tr -d '\r')"
    [ -z "$v" ] || export "$k=$v"
    return 0
}
export_from_stack MEM0_EMBED_PROFILE
export_from_stack MEM0_EMBED_MODEL
export_from_stack MEM0_EMBED_BASE_URL
if [ -n "${MEM0_EMBED_PROFILE:-}" ]; then
    pk="$(printf '%s' "$MEM0_EMBED_PROFILE" | tr 'a-z-' 'A-Z_')"
    export_from_stack "MEM0_EMBED_MODEL_$pk"
    export_from_stack "MEM0_EMBED_LONG_MODEL_$pk"
fi
SOCK="/tmp/wiki-qdrant-$$.sock"
PORT=""
BRAIN=""  # global: the EXIT trap reads it after tunnel_open has returned (set -u)

brain_alias() {
    local a="" host=""
    a="${WIKI_BRAIN_SSH:-}"
    [ -n "$a" ] || { [ -s "$HOME/.mem0/stack.env" ] && a="$(sed -n 's/^MEM0_BRAIN_SSH=//p' "$HOME/.mem0/stack.env" | head -n1)"; }
    if [ -z "$a" ] && [ -s "$HOME/.mem0/authority-url" ]; then
        host="$(head -n1 "$HOME/.mem0/authority-url" | sed -E 's#^[a-z]+://##; s#[:/].*$##')"
        # A Host block that names this HostName carries the user and key the operator already set.
        [ -s "$HOME/.ssh/config" ] && a="$(awk -v h="$host" '
            tolower($1)=="host" {alias=$2}
            tolower($1)=="hostname" && $2==h && alias!="" {print alias; exit}' "$HOME/.ssh/config")"
        [ -n "$a" ] || a="$host"
    fi
    [ -n "$a" ] || { echo "wiki-index: no brain alias (set WIKI_BRAIN_SSH or MEM0_BRAIN_SSH in ~/.mem0/stack.env)" >&2; return 1; }
    printf '%s' "$a"
}

# The authority's wiki space, from its /health/deep: the profile name; '' when the authority answers
# but reports none (an authority older than profiles: the caller keeps the old behaviour); '?' when
# it could not be read (unreachable, timed out — /health/deep embeds, so a cold embedder is slow —
# or not JSON). '?' must never mean "build here in this box's space": the caller sends the work to
# the brain, which embeds with its own model and so is always in the right space.
authority_wiki_profile() {
    local url="" body=""
    [ -s "$HOME/.mem0/authority-url" ] && url="$(head -n1 "$HOME/.mem0/authority-url" | tr -d '\r')"
    [ -n "$url" ] || return 0
    body="$(curl -s -m 30 "${url%/}/health/deep" 2>/dev/null)" || { echo "?"; return 0; }
    printf '%s' "$body" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("?"); sys.exit(0)
print(((d.get("embed_profile") or {}).get("wiki") or {}).get("profile") or "")' 2>/dev/null || echo "?"
}

# 0 when THIS box's llama-swap lists the model it would use for profile $1 (its own alias for that
# space: embedder_profile.embed_model, with this box's scoped overrides from its stack.env).
serves_profile() {
    local alias base
    alias="$("$PY" -c '
import sys
sys.path.insert(0, sys.argv[2])
import embedder_profile as ep
print(ep.embed_model(ep.get(sys.argv[1])))' "$1" "$HOME/apps/mem0-server" 2>/dev/null)" || return 1
    [ -n "$alias" ] || return 1
    base="${MEM0_EMBED_BASE_URL:-http://localhost:11436/v1}"
    # the body first, then the match: `curl | grep -q` under pipefail can read a served alias as missing
    local models=""
    models="$(curl -s -m 10 "${base%/}/models" 2>/dev/null)" || return 1
    printf '%s' "$models" | grep -q "\"$alias\""
}

# Prints local | remote: where this run embeds. A local run in the authority's space exports
# MEM0_WIKI_EMBED_PROFILE so this box's python resolves the authority's collection and model.
WIKI_MODE=""
wiki_mode() {
    local p
    p="$(authority_wiki_profile)"
    if [ -z "$p" ]; then WIKI_MODE=local; return 0; fi
    if [ "$p" = "?" ]; then
        echo "wiki-index: could not read the authority's wiki space; the brain embeds (always its own space)" >&2
        WIKI_MODE=remote; return 0
    fi
    if serves_profile "$p"; then
        export MEM0_WIKI_EMBED_PROFILE="$p"
        WIKI_MODE=local
    else
        echo "wiki-index: this box does not serve the authority's wiki space ($p); the brain embeds" >&2
        WIKI_MODE=remote
    fi
}

tunnel_open() {
    # A per-process control socket; the first free port in a small range so two sessions
    # can hold tunnels at once. ExitOnForwardFailure makes a busy port fail fast instead of
    # silently serving nothing.
    local p
    BRAIN="$(brain_alias)" || return 1
    for p in $(seq "$PORT_BASE" $((PORT_BASE + 9))); do
        if ssh -f -N -M -S "$SOCK" -o ExitOnForwardFailure=yes -o BatchMode=yes \
               -o ConnectTimeout=10 -L "$p:127.0.0.1:6333" "$BRAIN" 2>/dev/null; then
            PORT="$p"
            # 1.30.1: the alias must be a global here — a `local` died as "unbound variable" when
            # the trap fired after the function returned, and the tunnel outlived the run.
            trap 'ssh -S "$SOCK" -O exit "$BRAIN" >/dev/null 2>&1 || true' EXIT
            return 0
        fi
    done
    echo "wiki-index: could not open a tunnel to $BRAIN:6333 (ports $PORT_BASE..$((PORT_BASE + 9)))" >&2
    return 1
}

# Freshness stamp for the nightly's contract (docs/systems/wiki-index.md): the brain records the
# time of the last successful build by ANY path in ~/wiki-index/last-build, through the control
# socket the tunnel already holds. Fail-soft: a stamp that could not be written must not turn a
# built index into a failed run; the nightly then simply measures from its own pull.
stamp_last_build() {
    if ! ssh -n -S "$SOCK" -o BatchMode=yes -o ConnectTimeout=10 "$BRAIN" \
            'mkdir -p "$HOME/wiki-index" && date +%s > "$HOME/wiki-index/last-build"' >/dev/null 2>&1; then
        echo "wiki-index: built, but could not stamp last-build on $BRAIN (the nightly will measure freshness from its own pull)" >&2
    fi
}

case "${1:-}" in
    snapshot)
        tmp="$SNAP.new"
        rm -rf "$tmp"; mkdir -p "$tmp"
        # The tar carries a single top-level "wiki/" (tar -C <vault> -cf - wiki).
        tar -C "$tmp" --strip-components=1 -xf -
        n=$(find "$tmp" -name '*.md' | wc -l)
        if [ "$n" -eq 0 ]; then
            echo "wiki-index: snapshot received no pages — refusing to replace $SNAP" >&2
            rm -rf "$tmp"; exit 1
        fi
        rm -rf "$SNAP"; mv "$tmp" "$SNAP"
        echo "wiki-index: snapshot $n pages -> $SNAP"
        ;;
    build)
        [ -d "$SNAP" ] || { echo "wiki-index: no snapshot at $SNAP — run 'snapshot' first" >&2; exit 1; }
        wiki_mode
        if [ "$WIKI_MODE" = remote ]; then
            BRAIN="$(brain_alias)"
            # One top-level wiki/ in the stream: the shape 'snapshot' and 'build-here' take.
            tar -C "$(dirname "$SNAP")" -cf - "$(basename "$SNAP")" \
                | ssh -o BatchMode=yes -o ConnectTimeout=10 "$BRAIN" '"$HOME"/apps/mem0-scripts/wiki-index.sh build-here'
            exit $?
        fi
        tunnel_open
        WIKI_ROOT="$SNAP" WIKI_QDRANT_HOST=127.0.0.1 WIKI_QDRANT_PORT="$PORT" \
            "$PY" "$DIR/wiki-index-build.py"
        stamp_last_build
        ;;
    search)
        shift
        [ $# -ge 1 ] || { echo "usage: wiki-index.sh search \"query\" [--k N]" >&2; exit 2; }
        wiki_mode
        if [ "$WIKI_MODE" = remote ]; then
            BRAIN="$(brain_alias)"
            # The arguments travel as NUL-separated stdin, not as words for the brain's login shell:
            # no quoting layer, whatever that shell is and whatever bytes the query holds.
            printf '%s\0' "$@" | ssh -o BatchMode=yes -o ConnectTimeout=10 "$BRAIN" \
                '"$HOME"/apps/mem0-scripts/wiki-index.sh search-here --args-on-stdin'
            exit $?
        fi
        tunnel_open
        WIKI_QDRANT_HOST=127.0.0.1 WIKI_QDRANT_PORT="$PORT" \
            "$PY" "$DIR/wiki-search.py" "$@"
        ;;
    build-here)
        # ON THE BRAIN: a replica that does not serve the wiki's space pushed its snapshot. Build it
        # into this box's own Qdrant with this box's model, then stamp last-build like any build.
        tmp="$SNAP.push.$$"
        rm -rf "$tmp"; mkdir -p "$tmp"
        tar -C "$tmp" --strip-components=1 -xf -
        n=$(find "$tmp" -name '*.md' | wc -l)
        if [ "$n" -eq 0 ]; then
            echo "wiki-index: pushed snapshot has no pages — nothing built" >&2
            rm -rf "$tmp"; exit 1
        fi
        rc=0
        WIKI_ROOT="$tmp" "$PY" "$DIR/wiki-index-build.py" || rc=$?
        rm -rf "$tmp"
        if [ "$rc" -eq 0 ]; then
            mkdir -p "$(dirname "$SNAP")" && date +%s > "$(dirname "$SNAP")/last-build"
        fi
        exit "$rc"
        ;;
    search-here)
        shift
        if [ "${1:-}" = "--args-on-stdin" ]; then
            mapfile -d '' -t args
            set -- ${args[@]+"${args[@]}"}
        fi
        [ $# -ge 1 ] || { echo "usage: wiki-index.sh search-here \"query\" [--k N]" >&2; exit 2; }
        "$PY" "$DIR/wiki-search.py" "$@"
        ;;
    *)
        echo "usage: wiki-index.sh snapshot < wiki.tar | build | search \"query\" [--k N] | build-here < wiki.tar | search-here \"query\"" >&2
        exit 2
        ;;
esac
