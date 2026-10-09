# shellcheck shell=bash
# stack-env.sh — the ONE writer of ~/.mem0/stack.env (sourced by the installers; not executable).
#
# stack.env has several kinds of reader, and they do not agree on quoting:
#   - bash SOURCES it (deploy.sh, storage-cap-check.sh, ams-step.sh ...): a value with a space
#     runs its second word as a command, and $ ` ~ ; | & ( ) < > expand or execute;
#   - sed reads the raw text after the first '=' (the wiki-index step's stack_val,
#     stack-promote.sh, the installers' own inherit);
#   - Python splits on the first '=' and strips it (scripts/wsl/ams_env.py, job_liveness.py).
# Quoting a value would satisfy bash and break the other two (they would keep the quotes). So
# the only safe file is one where no value NEEDS quoting: every value is a plain token and lists
# are comma-separated. 1.31.1: an unquoted space-separated MEM0_WIKI_SOURCES made
# `deploy.sh --dry-run` die with "<second host>: command not found".
#
# stack_env_write refuses the WHOLE file, before writing anything, when any value is not a plain
# token. The caller's install aborts instead of shipping a file one class of reader
# misparses.

# letters, digits and . _ / : @ % + = , -  (paths, user@host:repo, IPs, comma lists)
STACK_ENV_VALUE_RE='^[A-Za-z0-9_./:@%+=,-]*$'

stack_env_check() {  # $1 = KEY, $2 = value; prints the reason and returns 1 when refused
    if ! [[ "$1" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
        echo "stack.env key '$1' is not an UPPER_SNAKE name" >&2
        return 1
    fi
    if ! [[ "$2" =~ $STACK_ENV_VALUE_RE ]]; then
        echo "stack.env value for $1 contains whitespace or a shell metacharacter: '$2' (allowed: letters, digits and . _ / : @ % + = , - ; a list is comma-separated)" >&2
        return 1
    fi
    return 0
}

stack_env_list() {  # $1 = a list separated by commas and/or whitespace -> "a,b,c" (no empties)
    local s="${1//,/ }" out="" w
    local -a words
    read -r -a words <<< "$s"
    for w in "${words[@]}"; do out="${out:+$out,}$w"; done
    printf '%s' "$out"
}

# Operator-owned keys: no installer flag sets them, the operator adds them by hand, and every
# writer rewrites the whole file, so each writer carries them over from the existing file
# (stack_env_carry) or a re-run deletes them silently. CARRIED IS NOT THE SAME AS READ, and who
# reads each key differs: the mem0 server unit has no EnvironmentFile= and never loads this file into
# its environment, so a reader that wants a key from here opens the file itself (after the process
# environment). mem0-server/tests/test_stack_env_writers.py pins every claim below.
#   MEM0_BRAIN_SSH: the brain's SSH alias for scripts/wsl/wiki-index.sh (docs/systems/wiki-index.md).
#   MEM0_PROMOTION_GATE_MODE (shadow|enforce; the code default is shadow, and a brain that loses the
#     line silently stops enforcing): read by the nightly dream (scripts/wsl/dream-consolidate.py) and
#     by the server's promotion_gate health check, both environment first, then this file, then shadow.
#   MEM0_SHARED_BRANDS and MEM0_BRAND_MAP (which brand labels every scope may see, and the operator's
#     brand map path): read, after the process environment, by the server's admission gate
#     (mem0-server/admission_gate.py) and by scripts/wsl/brand_routing.py; ams-store-judge-apply.sh
#     reads the map path the same way. The Windows hooks read the shared labels from their own
#     environment and brands.json, not from here.
#   MEM0_POOL_HEALTH_ACK: the operator's dated pool-health acknowledgment, `<STATE>:<YYYY-MM-DD>`
#     (docs/systems/mem0-api.md, /health/maintenance), read by that endpoint on every call.
#   MEM0_WIKI_EMBED_PROFILE: the LLM Wiki index's embedding space when it differs from the memories'
#     (docs/systems/embedder-profiles.md): read, after the process environment, by
#     mem0-server/embedder_profile.py (wiki_profile), so by the server's /health/deep, the wiki builder
#     and wiki search. A rewrite that dropped it would move the wiki back to the memories' space.
#   MEM0_QDRANT_COLLECTION / MEM0_COLLECTION (legacy name) / MEM0_EPISODES_COLLECTION /
#     MEM0_WIKI_COLLECTION (a collection other than the profile's, e.g. a restore copy) and the
#     threshold knobs MEM0_RELEVANCE_THRESHOLD / MEM0_RAW_FALLBACK_COSINE_FLOOR /
#     MEM0_NLI_GATE_COSINE_FLOOR: read, after the process environment, by mem0-server/embedder_profile.py
#     (collection, threshold), so by the server and every job. A rewrite that dropped a collection
#     override would silently rebind the server to the profile's own collection.
#   MEM0_NLI_GATE_ENABLED (the NLI write gate): NOT read from here. mem0-server/app.py reads it once,
#     at import, from the process environment only, so a value here is recorded and carried but does
#     not turn the gate on. To turn it on, set Environment=MEM0_NLI_GATE_ENABLED=1 in a mem0.service
#     drop-in of your own (systemctl --user edit mem0 writes override.conf; the installer rewrites
#     only native.conf) and restart the service.
#   MEM0_MEDIA_EMBEDDER (1.35.1; on|off, default on): off records that this box serves its embedding
#     alias text-only (no --mmproj projector; a replica on a small card). Read, after the process
#     environment, by mem0-server/embedder_profile.py (media_enabled), per call: media adds and media
#     searches then answer 400 here and /health/deep reports checks.media.enabled false.
# The installer flags that set a key here: linux-authority.sh --promotion-gate-mode, and on a WSL box
# install.ps1 -MediaEmbedder (MEM0_SET_MEDIA_EMBEDDER in install/1-wsl-services.sh). A writer that
# sets a key passes its name as a skip argument to stack_env_carry (or replaces the carried line) so
# the line is written exactly once.
# A new hand-set key goes here.
STACK_ENV_OPERATOR_KEYS="MEM0_BRAIN_SSH MEM0_PROMOTION_GATE_MODE MEM0_SHARED_BRANDS MEM0_BRAND_MAP MEM0_NLI_GATE_ENABLED MEM0_POOL_HEALTH_ACK MEM0_WIKI_EMBED_PROFILE MEM0_QDRANT_COLLECTION MEM0_COLLECTION MEM0_EPISODES_COLLECTION MEM0_WIKI_COLLECTION MEM0_RELEVANCE_THRESHOLD MEM0_RAW_FALLBACK_COSINE_FLOOR MEM0_NLI_GATE_COSINE_FLOOR MEM0_MEDIA_EMBEDDER"
# The embedding-space keys (mem0-server/embedder_profile.py reads them from this file when the process
# environment has none) are carried by PATTERN, not by name, so a profile added later is covered:
# MEM0_EMBED_PROFILE, the alias overrides MEM0_EMBED_MODEL (EmbeddingGemma-300m only) and
# MEM0_EMBED_MODEL_<PROFILE> / MEM0_EMBED_LONG_MODEL_<PROFILE>, and MEM0_EMBED_BASE_URL. A rewrite that
# dropped MEM0_EMBED_PROFILE would rebind the server to the default space's collections while the store
# sits in another (searches score noise, /health/deep stays green); one that dropped an alias would
# send the queries to a different conversion of the model. linux-authority.sh and linux-replica.sh
# write the profile (and the authority the active alias) themselves and pass those names as skips.
STACK_ENV_EMBED_KEY_RE='^MEM0_EMBED_(PROFILE|MODEL|MODEL_[A-Z0-9_]+|LONG_MODEL_[A-Z0-9_]+|BASE_URL)$'

stack_env_carry() {  # $1 = the existing stack.env, $2.. = keys to skip (set by a flag) -> "KEY=VALUE" lines for the operator-owned and embedding-space keys it records
    local k v file="$1" s skip
    shift
    [ -f "$file" ] || return 0
    local -a keys
    read -r -a keys <<< "$STACK_ENV_OPERATOR_KEYS"
    # the embedding-space keys the file records, in file order, each once
    while IFS= read -r k; do
        [[ "$k" =~ $STACK_ENV_EMBED_KEY_RE ]] && keys+=("$k")
    done < <(sed -n 's/^\(MEM0_EMBED_[A-Z0-9_]*\)=.*$/\1/p' "$file" | tr -d '\r' | awk '!seen[$0]++')
    for k in "${keys[@]}"; do
        skip=0
        for s in "$@"; do [ "$s" != "$k" ] || skip=1; done
        [ "$skip" = 0 ] || continue
        # first occurrence, as every sed reader takes it; a CR from a hand edit is dropped
        v="$(sed -n "s/^$k=//p" "$file" | head -n1 | tr -d '\r')"
        [ -z "$v" ] || printf '%s=%s\n' "$k" "$v"
    done
    return 0
}

stack_env_write() {  # $1 = file, then KEY=VALUE ...; all checked before anything is written
    local file="$1" kv k v body=""
    shift
    for kv in "$@"; do
        k="${kv%%=*}"; v="${kv#*=}"
        stack_env_check "$k" "$v" || return 1
        body+="$k=$v"$'\n'
    done
    local tmp="$file.tmp.$$"
    printf '%s' "$body" > "$tmp" || { rm -f "$tmp"; return 1; }
    mv -f "$tmp" "$file"
}
