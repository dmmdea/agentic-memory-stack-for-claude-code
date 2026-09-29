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
# WIKI_MAX_STALE_H, WIKI_FRESH_H, WIKI_PY.
#
# Freshness, not pull age: the index is as fresh as the NEWER of two brain stamps,
# ~/wiki-index/last-pull (this script's pull) and ~/wiki-index/last-build (the last successful
# build by EITHER path: this script, or a session's `wiki-index.sh build`). Nothing here retries
# a missed night: a PC that was off at 03:00 is covered by its own session-side refresh (the
# replica's SessionStart catch-up), and this step makes the aging visible instead of green.
#
# Exit policy (receipted by ams-step.sh; outcome contract C1 through AMS_OUTCOME_FILE):
#   pulled + built                            -> exit 0, `ok`, last-build stamped.
#   no source reachable, freshness <= 24 h    -> exit 0, `ok`; each source's ssh exit and stderr
#                                                ride in the outcome JSON.
#   no source reachable, freshness > 24 h     -> exit 0, `degraded:no-source-fresh-<h>h`.
#   no source reachable, freshness > WIKI_MAX_STALE_H (default 72 h), or no stamp ever recorded
#                                             -> exit 1, so a real outage is red in the morning.
set -u
export LC_ALL=C

STACK_ENV="$HOME/.mem0/stack.env"
stack_val() { [ -s "$STACK_ENV" ] && sed -n "s/^$1=//p" "$STACK_ENV" | head -n1; }

SNAP="${WIKI_SNAPSHOT:-$HOME/wiki-index/wiki}"
STAMP="$(dirname "$SNAP")/last-pull"
BUILD_STAMP="$(dirname "$SNAP")/last-build"
KEY="${WIKI_PULL_KEY:-$(stack_val MEM0_WIKI_PULL_KEY)}"
KEY="${KEY:-$HOME/.ssh/id_ed25519_wiki_pull}"
SOURCES="${WIKI_SOURCES:-$(stack_val MEM0_WIKI_SOURCES)}"
MAX_STALE_H="${WIKI_MAX_STALE_H:-72}"
FRESH_H="${WIKI_FRESH_H:-24}"
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

# A JSON string body: control characters dropped, backslash and quote escaped, length capped.
json_esc() {
    printf '%s' "$1" | tr -d '\000-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | cut -c1-200
}

# One outcome line (contract C1) when the chain step asked for one; a hand run has no file.
outcome() {
    if [ -n "${AMS_OUTCOME_FILE:-}" ]; then printf '%s\n' "$1" > "$AMS_OUTCOME_FILE"; fi
    return 0
}

# An epoch stamp, or 0 when the file is missing, empty or not a number.
stamp_val() {
    local v
    v="$(cat "$1" 2>/dev/null || true)"
    case "$v" in ''|*[!0-9]*) v=0 ;; esac
    printf '%s' "$v"
}

pulled=""; pulled_n=0
src_json=""
errf="$SNAP.err"
# Split on commas AND whitespace: the installer writes commas (1.31.1); a receipt written
# before that, or a hand-typed WIKI_SOURCES, may still use spaces.
read -r -a SOURCE_LIST <<< "${SOURCES//,/ }"
for src in "${SOURCE_LIST[@]}"; do
    tmp="$SNAP.new"; rm -rf "$tmp"; mkdir -p "$tmp"
    # The remote word is ignored by the forced command; it documents intent in the ssh log.
    # ssh's stderr is kept (host down, tailnet offline, sshd refusing and a forced-command
    # failure all read differently) and so is ssh's own exit status, not tar's.
    timeout 180 ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 \
            -o StrictHostKeyChecking=accept-new "$src" wiki-tar 2>"$errf" \
        | tar -C "$tmp" --strip-components=1 -xf - 2>/dev/null
    rcs=("${PIPESTATUS[@]}")
    ssh_rc="${rcs[0]}"; tar_rc="${rcs[1]}"
    if [ "$tar_rc" -eq 0 ]; then
        n="$(find "$tmp" -name '*.md' | wc -l)"
        if [ "$n" -gt 0 ]; then
            rm -rf "$SNAP"; mv "$tmp" "$SNAP"; date +%s > "$STAMP"
            pulled="$src"; pulled_n="$n"; echo "wiki-index: pulled $n pages from $src"
            break
        fi
    fi
    rm -rf "$tmp"
    err_tail="$(tail -c 300 "$errf" 2>/dev/null | tr '\n\t' '  ' | sed -e 's/[[:space:]]*$//')"
    echo "wiki-index: $src unreachable or empty (ssh exit $ssh_rc): $err_tail" >&2
    src_json="$src_json${src_json:+,}{\"source\":\"$(json_esc "$src")\",\"exit\":$ssh_rc,\"stderr\":\"$(json_esc "$err_tail")\"}"
done
rm -f "$errf"

now="$(date +%s)"
last_pull="$(stamp_val "$STAMP")"
last_build="$(stamp_val "$BUILD_STAMP")"
fresh_last="$last_pull"; [ "$last_build" -gt "$fresh_last" ] && fresh_last="$last_build"
fresh_age_s=$(( now - fresh_last ))
fresh_age_h=$(( fresh_age_s / 3600 ))

if [ -z "$pulled" ]; then
    if [ "$fresh_last" -eq 0 ]; then
        echo "wiki-index: no wiki source reachable and no pull or build has ever been recorded" >&2
        exit 1
    fi
    if [ "$fresh_age_s" -gt $(( MAX_STALE_H * 3600 )) ]; then
        echo "wiki-index: no wiki source reachable and the index is ${fresh_age_h} h old (limit ${MAX_STALE_H} h)" >&2
        exit 1
    fi
    echo "wiki-index: no wiki source reachable; index kept as-is (index is ${fresh_age_h} h old, limit ${MAX_STALE_H} h)"
    work="{\"pulled\":0,\"fresh_age_h\":$fresh_age_h,\"sources\":[$src_json]}"
    if [ "$fresh_age_s" -gt $(( FRESH_H * 3600 )) ]; then
        outcome "degraded:no-source-fresh-${fresh_age_h}h $work"
    else
        outcome "ok $work"
    fi
    exit 0
fi

WIKI_ROOT="$SNAP" "$PY" "$DIR/wiki-index-build.py"
rc=$?
if [ "$rc" -eq 0 ]; then
    date +%s > "$BUILD_STAMP"
    outcome "ok {\"pulled\":$pulled_n,\"source\":\"$(json_esc "$pulled")\",\"fresh_age_h\":0}"
fi
exit "$rc"
