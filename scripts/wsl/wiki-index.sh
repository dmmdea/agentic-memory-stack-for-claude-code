#!/usr/bin/env bash
# wiki-index.sh — the LLM Wiki's semantic index (wiki_pages_egemma_768) from a REPLICA box.
#
# The index lives on the brain box's Qdrant (docs/systems/wiki-index.md). A replica's own
# Qdrant is dormant by design, so this wrapper reaches the brain's loopback Qdrant through an
# SSH tunnel; the embedder is this box's own llama-swap (mirrored networking: localhost in WSL).
#
#   wiki-index.sh snapshot   < tar of the vault's wiki/   -> ~/wiki-index/wiki
#   wiki-index.sh build                                    (snapshot -> brain Qdrant)
#   wiki-index.sh search "query" [--k N]                   (JSON lines, top-K pages)
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
SOCK="/tmp/wiki-qdrant-$$.sock"
PORT=""

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

tunnel_open() {
    # A per-process control socket; the first free port in a small range so two sessions
    # can hold tunnels at once. ExitOnForwardFailure makes a busy port fail fast instead of
    # silently serving nothing.
    local brain p
    brain="$(brain_alias)" || return 1
    for p in $(seq "$PORT_BASE" $((PORT_BASE + 9))); do
        if ssh -f -N -M -S "$SOCK" -o ExitOnForwardFailure=yes -o BatchMode=yes \
               -o ConnectTimeout=10 -L "$p:127.0.0.1:6333" "$brain" 2>/dev/null; then
            PORT="$p"
            trap 'ssh -S "$SOCK" -O exit "$brain" >/dev/null 2>&1 || true' EXIT
            return 0
        fi
    done
    echo "wiki-index: could not open a tunnel to $brain:6333 (ports $PORT_BASE..$((PORT_BASE + 9)))" >&2
    return 1
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
        tunnel_open
        WIKI_ROOT="$SNAP" WIKI_QDRANT_HOST=127.0.0.1 WIKI_QDRANT_PORT="$PORT" \
            "$PY" "$DIR/wiki-index-build.py"
        ;;
    search)
        shift
        [ $# -ge 1 ] || { echo "usage: wiki-index.sh search \"query\" [--k N]" >&2; exit 2; }
        tunnel_open
        WIKI_QDRANT_HOST=127.0.0.1 WIKI_QDRANT_PORT="$PORT" \
            "$PY" "$DIR/wiki-search.py" "$@"
        ;;
    *)
        echo "usage: wiki-index.sh snapshot < wiki.tar | build | search \"query\" [--k N]" >&2
        exit 2
        ;;
esac
