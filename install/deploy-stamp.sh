# shellcheck shell=bash
# deploy-stamp.sh - the release sha an installer records beside each runtime it deploys (sourced by
# the installers; not executable).
#
# The deployed tree has no .git, so anything that must say which commit it runs (the nightly
# backup manifest's git_sha was "unknown" on every set after the cutover) cannot ask git. The
# installer asks once, from the checkout it is running out of, and writes one line - a 40-hex sha,
# or the word "unknown" - to a file named DEPLOYED_SHA beside VERSION (the mem0 app dir) and beside
# the deployed maintenance scripts. Readers take the first line and treat "unknown" as unknown.

deploy_stamp_sha() {  # $1 = the checkout root -> prints the sha, or "unknown"; never fails
    local root="$1" sha=""
    if [ -e "$root/.git" ]; then
        sha="$(git -C "$root" rev-parse HEAD 2>/dev/null || true)"
    fi
    # a tree that was copied out of a checkout (no .git) may carry the stamp it was built with
    if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]] && [ -r "$root/DEPLOYED_SHA" ]; then
        sha="$(head -n1 "$root/DEPLOYED_SHA" | tr -d '[:space:]')"
    fi
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || sha="unknown"
    printf '%s' "$sha"
}

deploy_stamp_write() {  # $1 = the checkout root, then the directories to stamp
    local root="$1" sha dir
    shift
    sha="$(deploy_stamp_sha "$root")"
    for dir in "$@"; do
        printf '%s\n' "$sha" > "$dir/DEPLOYED_SHA"
    done
    echo "    release sha ${sha:0:12} stamped (DEPLOYED_SHA) into: $*"
}
