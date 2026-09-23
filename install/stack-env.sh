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
