#!/usr/bin/env bash
# wiki-index-refresh.sh — the session-side refresh of the LLM Wiki's semantic index
# (docs/systems/wiki-index.md, "Two refresh paths"). Runs on a PC that mounts the vault, from Git Bash.
#
#   bash ~/.claude/scripts/wiki-index-refresh.sh [vault-dir]
#
# Pushes the vault's curated wiki/ tree into WSL as a tar stream and runs the WSL-side wrapper
# (~/apps/mem0-scripts/wiki-index.sh snapshot + build), which embeds it through a tunnel into the
# brain's Qdrant and stamps ~/wiki-index/last-build there. Idempotent: unchanged pages are skipped,
# deleted pages are removed. Only the wiki/ subtree is read — never the vault root or raw/
# (cloud-hydration guardrail).
#
# The vault directory is operator configuration, never baked in: the argument, else $WIKI_VAULT,
# else the first line of ~/.mem0/wiki-vault (a Windows or Git Bash path). On success this writes
# ~/.claude/state/last-wiki-refresh, the local stamp the SessionStart catch-up
# (wiki-index-catchup.ps1) compares vault page times against, so a hand refresh after an ingest
# also clears the catch-up.
# Env: WSL_DISTRO (the distro holding the stack; default distro when unset).
set -euo pipefail

VAULT="${1:-${WIKI_VAULT:-}}"
CONF="$HOME/.mem0/wiki-vault"
if [ -z "$VAULT" ] && [ -s "$CONF" ]; then VAULT="$(head -n1 "$CONF" | tr -d '\r')"; fi
if [ -z "$VAULT" ]; then
    echo "wiki-index-refresh: no vault configured (pass the directory, set WIKI_VAULT, or write it to $CONF)" >&2
    exit 1
fi
# A Windows-form path (G:\...) becomes the Git Bash form when cygpath is there to do it.
case "$VAULT" in *\\*|[A-Za-z]:*) command -v cygpath >/dev/null 2>&1 && VAULT="$(cygpath -u "$VAULT")" ;; esac
if [ ! -d "$VAULT/wiki" ]; then
    echo "wiki-index-refresh: no wiki/ tree under the vault directory ($VAULT)" >&2
    exit 1
fi

echo "wiki-index-refresh: vault $VAULT"
WSL_ARGS=()
[ -n "${WSL_DISTRO:-}" ] && WSL_ARGS=(-d "$WSL_DISTRO")
tar -C "$VAULT" -cf - wiki \
    | wsl.exe "${WSL_ARGS[@]}" -e bash -lc \
        '~/apps/mem0-scripts/wiki-index.sh snapshot && ~/apps/mem0-scripts/wiki-index.sh build'

STATE="$HOME/.claude/state"
mkdir -p "$STATE"
date +%s > "$STATE/last-wiki-refresh"
