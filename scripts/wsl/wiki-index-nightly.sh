#!/usr/bin/env bash
# wiki-index-nightly.sh — AMS chain step `wiki-index` on the BRAIN box (docs/systems/wiki-index.md).
#
# The LLM Wiki is a markdown vault on a cloud-synced folder that only the operator's PCs mount.
# This step pulls the vault's curated wiki/ tree from the first reachable PC over SSH — a
# dedicated key the operator pins on each PC to a forced command that can only stream that
# tar — and embeds it into THIS box's Qdrant (wiki_pages_egemma_768), the index every
# replica's `wiki-index.sh search` tunnels to. Idempotent: unchanged pages are skipped.
#
# It is the BACKSTOP for the session-side refresh: a session that edits pages and forgets to
# refresh is caught the next night. Nothing here is a source of truth — the vault is; a lost
# index is rebuilt by the next run.
#
# Configuration (stack.env, written by install/linux-authority.sh --wiki-sources):
#   MEM0_WIKI_SOURCES   user@host list, tried in order; comma-separated since 1.31.1 (the file
#                       is sourced by bash), space-separated in older receipts; both work (required)
#   MEM0_WIKI_PULL_KEY  the identity file                (default ~/.ssh/id_ed25519_wiki_pull)
# Env overrides for tests and hand runs: WIKI_SOURCES, WIKI_PULL_KEY, WIKI_SNAPSHOT,
# WIKI_MAX_STALE_H, WIKI_PY.
#
# Exit policy (receipted by ams-step.sh): pulled + built -> 0. No source reachable -> 0 while the
# last successful pull is younger than WIKI_MAX_STALE_H (default 72 h; PCs are often off at
# 03:00 and the chain catches up in the morning), 1 once it is older, so a real outage surfaces
# in the morning summary instead of hiding behind a green night.
set -u
export LC_ALL=C

STACK_ENV="$HOME/.mem0/stack.env"
stack_val() { [ -s "$STACK_ENV" ] && sed -n "s/^$1=//p" "$STACK_ENV" | head -n1; }

SNAP="${WIKI_SNAPSHOT:-$HOME/wiki-index/wiki}"
STAMP="$(dirname "$SNAP")/last-pull"
KEY="${WIKI_PULL_KEY:-$(stack_val MEM0_WIKI_PULL_KEY)}"
KEY="${KEY:-$HOME/.ssh/id_ed25519_wiki_pull}"
SOURCES="${WIKI_SOURCES:-$(stack_val MEM0_WIKI_SOURCES)}"
MAX_STALE_H="${WIKI_MAX_STALE_H:-72}"
PY="${WIKI_PY:-$HOME/apps/mem0-server/.venv/bin/python}"
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$SOURCES" ]; then
    echo "wiki-index: MEM0_WIKI_SOURCES is not set in $STACK_ENV — the installer drops this step when --wiki-sources is not configured" >&2
    exit 1
fi

# The store is bound to the exact GGUF the brain serves under its own name (config.py); the
# chain's environment does not carry it, so read it from the stack file like the server does.
if [ -z "${MEM0_EMBED_MODEL:-}" ]; then
    v="$(stack_val MEM0_EMBED_MODEL)"
    [ -n "$v" ] && export MEM0_EMBED_MODEL="$v"
fi

mkdir -p "$(dirname "$SNAP")"
pulled=""
# Split on commas AND whitespace: the installer writes commas (1.31.1); a receipt written
# before that, or a hand-typed WIKI_SOURCES, may still use spaces.
read -r -a SOURCE_LIST <<< "${SOURCES//,/ }"
for src in "${SOURCE_LIST[@]}"; do
    tmp="$SNAP.new"; rm -rf "$tmp"; mkdir -p "$tmp"
    # The remote word is ignored by the forced command; it documents intent in the ssh log.
    if timeout 180 ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 \
            -o StrictHostKeyChecking=accept-new "$src" wiki-tar 2>/dev/null \
        | tar -C "$tmp" --strip-components=1 -xf - 2>/dev/null; then
        n="$(find "$tmp" -name '*.md' | wc -l)"
        if [ "$n" -gt 0 ]; then
            rm -rf "$SNAP"; mv "$tmp" "$SNAP"; date +%s > "$STAMP"
            pulled="$src"; echo "wiki-index: pulled $n pages from $src"
            break
        fi
    fi
    rm -rf "$tmp"
    echo "wiki-index: $src unreachable or empty" >&2
done

if [ -z "$pulled" ]; then
    last="$(cat "$STAMP" 2>/dev/null || echo 0)"
    age_h=$(( ( $(date +%s) - last ) / 3600 ))
    if [ "$last" -gt 0 ] && [ "$age_h" -lt "$MAX_STALE_H" ]; then
        echo "wiki-index: no wiki source reachable; index kept as-is (last pull ${age_h} h ago, limit ${MAX_STALE_H} h)"
        exit 0
    fi
    echo "wiki-index: no wiki source reachable and the last pull is ${age_h} h old (limit ${MAX_STALE_H} h)" >&2
    exit 1
fi

WIKI_ROOT="$SNAP" "$PY" "$DIR/wiki-index-build.py"
