# shellcheck shell=bash
# embed-profile.sh — shell access to embedder_profile.py (mem0-server), the one definition of the
# stack's embedding space (profile name, llama-swap model alias, collections). Sourced by the
# backup, manifest, restore and installer scripts; none of them carries a collection name or a
# model alias of its own. Nothing here sets shell options: the callers differ (set -e / -u / neither).
#
# The module is found repo-relative first (<repo>/scripts/wsl/../../mem0-server, the layout the
# tests and the installers run in), then at ~/apps/mem0-server (the deployed layout, where the
# scripts sit flat in ~/apps/mem0-scripts). embedder_profile is stdlib-only, so the system python3
# is enough: a fresh install has no server venv yet at the point the installers ask.
#
#   ep_py <python> [args...]   run <python> with `ep` imported; args arrive as sys.argv[1:]
#                              (rc 2: the module cannot be found; otherwise python's own status)
#   ep_table                   one TSV line per profile, the active one first:
#                              name, active(1|0), model alias, template version, memories,
#                              entities, episodes, wiki collection
#   ep_load                    ep_table parsed into EP_* variables (below); never fails when the
#                              module is absent (see the fallback); rc 1 = a configuration error
#   ep_field <profile> <attr>  one attribute of a profile (model, memories, dims, doc_prefix ...)
#   ep_default_profile         the profile an existing, unrecorded store was built in
#   ep_alias <profile>         the alias this box resolves for that profile (env + stack.env)
#   ep_env_key <profile>       MEM0_EMBED_MODEL_<PROFILE>: the scoped alias override's name
#   ep_base_url                the embedder's base URL (MEM0_EMBED_BASE_URL or the default)

EP_DIR=""

ep_locate() {
    [ -z "$EP_DIR" ] || return 0
    local here d
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || here=""
    for d in "$here/../../mem0-server" "$HOME/apps/mem0-server"; do
        if [ -f "$d/embedder_profile.py" ]; then EP_DIR="$(cd "$d" && pwd)"; return 0; fi
    done
    return 1
}

ep_py() {
    ep_locate || return 2
    local code="$1"; shift
    python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import embedder_profile as ep; del sys.argv[1]
'"$code" "$EP_DIR" "$@"
}

ep_table() {
    ep_py '
act = ep.active()
def row(p, is_active):
    if is_active:   # the active space honours the operator overrides (MEM0_QDRANT_COLLECTION ...)
        c = [ep.collection(k, p) for k in ("memories", "entities", "episodes", "wiki")]
    else:           # a space that is not bound keeps its own names: the overrides name the bound one
        c = [p.memories, p.entities, p.episodes, p.wiki]
    print("\t".join([p.name, "1" if is_active else "0", ep.embed_model(p), p.template_version, *c]))
row(act, True)
for n in sorted(ep.PROFILES):
    if n != act.name:
        row(ep.PROFILES[n], False)
'
}

# ep_load sets: EP_STATUS (ok | fallback), EP_PROFILE, EP_MODEL, EP_TEMPLATE, EP_MEM, EP_ENT, EP_EPI,
# EP_WIKI (the active space) and EP_OTHER_MEMS (the memories collection of every other space, space
# separated). When embedder_profile.py cannot be found at all the scripts must still run: a nightly
# backup that dies on a missing module is worse than one that backs up the pre-profile names. That
# is the ONE place a collection literal stays — the EmbeddingGemma-300m names every store was built
# on before profiles existed — and it says so (EP_STATUS=fallback, EP_PROFILE=unknown, a WARN).
ep_load() {
    EP_STATUS=ok; EP_PROFILE=""; EP_MODEL=""; EP_TEMPLATE=""; EP_MEM=""; EP_ENT=""; EP_EPI=""; EP_WIKI=""; EP_OTHER_MEMS=""
    local rows rc=0 name act model tmpl mem ent epi wiki
    rows="$(ep_table)" || rc=$?
    if [ "$rc" = 2 ]; then
        echo "WARN: embedder_profile.py not found beside the repo or in ~/apps/mem0-server - using the pre-profile EmbeddingGemma-300m collection names" >&2
        EP_STATUS=fallback; EP_PROFILE=unknown
        EP_MEM="${MEM0_QDRANT_COLLECTION:-${MEM0_COLLECTION:-mem0_egemma_768}}"
        EP_ENT="${EP_MEM}_entities"
        EP_EPI="${MEM0_EPISODES_COLLECTION:-episodes_egemma_768}"
        EP_WIKI="${MEM0_WIKI_COLLECTION:-wiki_pages_egemma_768}"
        return 0
    fi
    [ "$rc" = 0 ] || return 1
    while IFS=$'\t' read -r name act model tmpl mem ent epi wiki; do
        [ -n "$name" ] || continue
        if [ "$act" = 1 ]; then
            EP_PROFILE="$name"; EP_MODEL="$model"; EP_TEMPLATE="$tmpl"
            EP_MEM="$mem"; EP_ENT="$ent"; EP_EPI="$epi"; EP_WIKI="$wiki"
        else
            EP_OTHER_MEMS="$EP_OTHER_MEMS${EP_OTHER_MEMS:+ }$mem"
        fi
    done <<< "$rows"
    [ -n "$EP_PROFILE" ] && [ -n "$EP_MEM" ]
}

ep_field() {  # <profile> <attr>
    ep_py 'print(getattr(ep.get(sys.argv[1]), sys.argv[2]))' "$1" "$2"
}

ep_default_profile() {
    ep_py 'print(ep.DEFAULT_PROFILE)'
}

ep_alias() {  # <profile>
    ep_py 'print(ep.embed_model(ep.get(sys.argv[1])))' "$1"
}

# The scoped override's name follows embedder_profile._model_key (upper-case, '-' -> '_'); the
# installers write it into stack.env and the unit, so test_systemd_parity.py asks the module whether
# a variable of this name really selects the alias.
ep_env_key() {  # <profile>
    printf 'MEM0_EMBED_MODEL_%s' "$(printf '%s' "$1" | tr 'a-z-' 'A-Z_')"
}

ep_base_url() {
    ep_py 'print(ep.base_url())'
}
