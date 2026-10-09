"""embedder_profile — the one definition of the stack's embedding space.

A vector store is bound to the exact model AND prompt template that produced its vectors: a query
embedded by another model, or with another task prefix, lands in a different space and retrieval
degrades silently, with no error anywhere. So every component that embeds text or names a
collection resolves both through this module instead of carrying its own literal.

A profile names one space: the llama-swap model alias that serves it, the context the model was
trained for, the embedding-input token budget, the asymmetric task prefixes, a template version
(bump it whenever a prefix changes) and the three collections built in that space. Collection
names carry the model, so two spaces can never share a collection.

Resolution (first match wins):
  profile         MEM0_EMBED_PROFILE env > ~/.mem0/stack.env MEM0_EMBED_PROFILE > LEGACY_PROFILE
  model alias     MEM0_EMBED_MODEL_<PROFILE> (env > stack.env) > the profile's model; the
                  unscoped MEM0_EMBED_MODEL counts for egemma-300m only (see embed_model)
  long alias      MEM0_EMBED_LONG_MODEL_<PROFILE> > the profile's long_model ("none": use the hot one)
  memories        MEM0_QDRANT_COLLECTION > MEM0_COLLECTION (legacy name) > the profile's
  episodes        MEM0_EPISODES_COLLECTION > the profile's
  wiki            MEM0_WIKI_COLLECTION > the wiki profile's (MEM0_WIKI_EMBED_PROFILE > the active one)
  base URL        MEM0_EMBED_BASE_URL > http://localhost:11436/v1

DEFAULT_PROFILE is the space a FRESH install records: EmbeddingGemma-2 since 1.35.0 (operator order
2026-10-08: it replaces EmbeddingGemma-300m everywhere, multimodal). A box that records no profile is
read as LEGACY_PROFILE (EmbeddingGemma-300m), the space every store was built in before profiles
existed, so a code upgrade never moves a store by itself, whatever the default says. Every installer
records the profile in stack.env; an existing store changes space only through
scripts/wsl/embedder-migrate.py, which builds the new collections beside the old ones, followed by
install/linux-authority.sh --embed-profile. egemma-300m stays defined as the source of that migration.

mem0 stores entity vectors in "<memories collection>_entities"; that name follows the memories
collection automatically.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Thresholds:
    """Every cosine cut-off in the stack, per embedding space. Cosine scales are not portable: the
    same off-topic question scores 0.17-0.29 top-1 against EmbeddingGemma-300m and 0.61-0.69 against
    EmbeddingGemma-2 (2026-10-08 lab A/B), so a value fitted on one space is noise on another."""
    relevance_gate: float     # context-bundle gate on the top-1 SEMANTIC cosine (TIER_BUNDLE_POLICY)
    episode_floor: float      # raw-trace episode fallback floor (raw cosine, episodes collection)
    nli_floor: float          # NLI write-gate pre-filter (canonical neighbours, raw cosine)
    evidence_sim_floor: float # contradiction-sweep --evidence-sweep neighbour floor (stored vectors)
    sibling: float            # autopromote corroboration: a sibling counts at cosine >= this
    dedup: tuple              # semantic-dedup per tier: ((tier, cosine), ...) — it DELETES
    dedup_fallback: float     # semantic-dedup, a tier the table does not name
    rerank_skip: float        # skip the cross-encoder when the head's FUSED score (fusion.py) >= this

    def dedup_for(self, tier: str) -> float:
        return dict(self.dedup).get(tier, self.dedup_fallback)


@dataclass(frozen=True)
class EmbedProfile:
    name: str
    label: str
    model: str            # llama-swap alias for the hot path (queries, memories, entities, episodes)
    ctx_tokens: int       # the largest input one embed takes: that alias's llama-server --ubatch-size
                          # (a non-causal embedder needs the whole input in one ubatch)
    token_budget: int     # upper bound for a hot-path embedding input, task prefix included
    dims: int
    query_prefix: str
    doc_prefix: str
    template_version: str
    memories: str
    episodes: str
    wiki: str
    # Long documents (wiki pages): the same GGUF served at the model's full trained context under a
    # second alias. A non-causal embedder needs the whole input in one ubatch, so the window costs
    # VRAM for as long as the alias is loaded; the hot alias stays small and only the indexer loads
    # the long one. For an input that fits both, the two aliases return the same vector (one space).
    long_model: str = ""
    long_ctx_tokens: int = 0
    long_token_budget: int = 0
    thresholds: Thresholds | None = None
    # The served model also embeds images, audio and video into the same space (its alias is started
    # with --mmproj); the server's media memories and media searches require it (media.py).
    media: bool = False
    # Rank-fusion constants (k, keyword weight, entity weight; fusion.py), measured per model on the
    # 2026-10-08 lab: EmbeddingGemma-300m's are the 1.34.0 ones, EmbeddingGemma-2's the 1.35.0 retune.
    fusion: tuple = (2.0, 0.4, 0.25)

    @property
    def entities(self) -> str:
        return self.memories + "_entities"


# Both EmbeddingGemma generations use the same task prefixes (verbatim from each model card's
# config_sentence_transformers.json: "query" and "document"). They are still different spaces:
# the prefixes match, the weights do not.
_EG_QUERY = "task: search result | query: "
_EG_DOC = "title: none | text: "

PROFILES = {
    "egemma-300m": EmbedProfile(
        name="egemma-300m", label="EmbeddingGemma-300m (retired 1.35.0; kept as a migration source)",
        model="embeddinggemma",
        ctx_tokens=2048, token_budget=1900, dims=768,
        query_prefix=_EG_QUERY, doc_prefix=_EG_DOC, template_version="eg-search-v1",
        memories="mem0_egemma_768", episodes="episodes_egemma_768", wiki="wiki_pages_egemma_768",
        # The values the stack was calibrated with on this space (calibrate_relevance.py 2026-06-15,
        # calibrate_episode_floor.py, corroboration_threshold_calibration.py; dedup by operator
        # direction 2026-06-10). Unchanged by the profile refactor. rerank_skip is not a cosine: the
        # fused score is reciprocal rank fusion (fusion.py), the same in every space, and 1.0 means
        # every leg ranks the head first (0.0-0.1% of the lab's searches; docs/systems/fusion.md).
        thresholds=Thresholds(
            relevance_gate=0.30, episode_floor=0.20, nli_floor=0.5, evidence_sim_floor=0.45, sibling=0.6,
            dedup=(("canonical", 0.97), ("stable", 0.95), ("evidence", 0.94), ("temporal", 0.94),
                   ("insight", 0.95)),
            dedup_fallback=0.92, rerank_skip=1.0),
    ),
    # google/embeddinggemma-2: 740M parameters, a 270M text model (Gemma 4 backbone) plus a 170M vision and
    # a 300M audio encoder, all mapped into one 768-d space (Matryoshka 512/256/128 unused here), mean
    # pooling, trained at 8,192 tokens. Its GGUF header advertises the backbone's 262,144: never serve the
    # header value. Needs llama.cpp with the gemma-embedding2 architecture (b11452 or later). Q8_0 text
    # weights measured cosine >= 0.9996 against BF16 on house text, and the Q8_0 projector (the vision +
    # audio encoders, --mmproj) cosine >= 0.9993 against BF16 on images and speech (2026-10-08, b11490).
    # Served on the RTX 3050 authority at --ctx-size 4096 --batch-size 4096 --ubatch-size 2048 with the
    # projector: 1,196 MiB loaded, 1,466 MiB peak after image embeds (1,536 after a video); ubatch 4096
    # would cost 1,870 MiB for inputs past ~1,900 tokens, which memories (4,000-char cap) never reach.
    # The text vectors are identical with and without the projector (cosine 1.0). Images, audio and
    # video are sent as OpenAI-style content parts on /v1/embeddings (media.py); they take no prefix.
    # The wiki's whole-page recipe beat EmbeddingGemma-300m's best by +0.10 MRR at 3,900 tokens; a box
    # that wants that window declares a long alias (MEM0_EMBED_LONG_MODEL_EGEMMA2 + a ubatch-8192 entry).
    "egemma2": EmbedProfile(
        name="egemma2", label="EmbeddingGemma-2", model="embeddinggemma2",
        ctx_tokens=2048, token_budget=1900, dims=768,
        query_prefix=_EG_QUERY, doc_prefix=_EG_DOC, template_version="eg-search-v1",
        memories="mem0_eg2_768", episodes="episodes_eg2_768", wiki="wiki_pages_eg2_768",
        long_ctx_tokens=8192, long_token_budget=7900, media=True, fusion=(1.0, 0.5, 0.25),
        # Calibrated 2026-10-08 in the lab A/B on the restored 2026-10-07 store (16,946 memories), see
        # docs/systems/embedder-profiles.md. Gate 0.70: the clean-separation point of the house
        # relevance probes (off-topic max 0.694, relevant min 0.720 EN / 0.727 ES); the house rule's
        # "minus one 0.05 notch" cannot apply because the band is 0.026 wide (EmbeddingGemma-300m:
        # 0.108). Episode floor 0.68: middle of the clean band [0.65, 0.71]. The neighbour thresholds
        # match quantiles of nearest-neighbour cosine across the two spaces (2,000 memories, rank
        # corr 0.88). Dedup tiers from the full-corpus tail: at >= 0.988 no pair EmbeddingGemma-300m
        # kept would be deleted. rerank_skip is the fused (rank-fusion) score, which does not depend on
        # the space: 1.0 as on EmbeddingGemma-300m.
        thresholds=Thresholds(
            relevance_gate=0.70, episode_floor=0.68, nli_floor=0.79, evidence_sim_floor=0.77,
            sibling=0.825,
            dedup=(("canonical", 0.993), ("stable", 0.99), ("evidence", 0.988), ("temporal", 0.988),
                   ("insight", 0.99)),
            dedup_fallback=0.988, rerank_skip=1.0),
    ),
}

# The space a fresh install records (installers only; the server never falls back to it).
DEFAULT_PROFILE = "egemma2"
# The space of every store and backup set made before profiles existed (before 1.33.0): a box with no
# recorded profile, or a set whose manifest names none, means THIS, whatever DEFAULT_PROFILE says.
LEGACY_PROFILE = "egemma-300m"
DEFAULT_BASE_URL = "http://localhost:11436/v1"


def _stack_env(key: str) -> str:
    """One KEY from ~/.mem0/stack.env ('' when absent). Read per call: tests and the migration
    tool change it between calls, and nothing calls this on a hot path."""
    try:
        for line in (Path.home() / ".mem0" / "stack.env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip()
    except OSError:
        pass
    return ""


def _setting(key: str) -> str:
    return (os.environ.get(key) or "").strip() or _stack_env(key)


def profile_name() -> str:
    """The recorded profile, else LEGACY_PROFILE: an unrecorded box is a store from before profiles, never
    a fresh one (installers record the profile, a fresh install included)."""
    return _setting("MEM0_EMBED_PROFILE") or LEGACY_PROFILE


def active() -> EmbedProfile:
    """The profile in force. An unknown name is a configuration error, never a silent default:
    falling back would bind the server to a space its store was not built in."""
    name = profile_name()
    try:
        return PROFILES[name]
    except KeyError:
        raise SystemExit(f"FAIL: MEM0_EMBED_PROFILE={name!r} is not a known embedding profile "
                         f"({', '.join(sorted(PROFILES))})")


def get(name: str) -> EmbedProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise SystemExit(f"FAIL: unknown embedding profile {name!r} ({', '.join(sorted(PROFILES))})")


def _model_key(p: EmbedProfile, long: bool = False) -> str:
    return ("MEM0_EMBED_LONG_MODEL_" if long else "MEM0_EMBED_MODEL_") + p.name.upper().replace("-", "_")


def long_model(profile: EmbedProfile | None = None) -> tuple[str, int]:
    """(alias, token budget) for long documents. A profile without a long alias, or a box that does
    not serve one (MEM0_EMBED_LONG_MODEL_<PROFILE>=none), embeds long documents through the hot alias
    with the hot budget: the text is cut sooner, the space is the same."""
    p = profile or active()
    scoped = _setting(_model_key(p, long=True))
    if scoped.lower() == "none" or not (scoped or p.long_model):
        return embed_model(p), p.token_budget
    return (scoped or p.long_model), (p.long_token_budget or p.token_budget)


def wiki_profile() -> EmbedProfile:
    """The space the LLM Wiki index lives in. It may differ from the memories' space: the wiki is a
    derived index of long pages, rebuilt from the vault in seconds, and it is where a long-context
    embedder pays (2026-10-08 lab: EmbeddingGemma-2 on whole pages beat EmbeddingGemma-300m on
    detail questions, deep MRR +0.10, while the memories stay on EmbeddingGemma-300m, which measured
    better on short facts). MEM0_WIKI_EMBED_PROFILE (env > stack.env) > the active profile."""
    name = _setting("MEM0_WIKI_EMBED_PROFILE")
    return get(name) if name else active()


def embed_model(profile: EmbedProfile | None = None) -> str:
    """The llama-swap alias a box serves this space under (the native authority serves its own copy
    of a GGUF under its own name, e.g. `embeddinggemma-ams`).

    The override is scoped to its profile: MEM0_EMBED_MODEL_<PROFILE> (EGEMMA2, EGEMMA_300M). The
    unscoped MEM0_EMBED_MODEL predates profiles and names an EmbeddingGemma-300m file, so it applies
    to egemma-300m only; otherwise switching the profile would keep embedding queries with the old
    model against the new vectors."""
    p = profile or active()
    scoped = _setting(_model_key(p))
    if scoped:
        return scoped
    if p.name == "egemma-300m":
        legacy = _setting("MEM0_EMBED_MODEL")
        if legacy:
            return legacy
    return p.model


def base_url() -> str:
    return (_setting("MEM0_EMBED_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def collection(kind: str = "memories", profile: EmbedProfile | None = None) -> str:
    """The collection for `kind` (memories | entities | episodes | wiki) in the active space."""
    p = profile or active()
    if kind == "memories":
        return _setting("MEM0_QDRANT_COLLECTION") or _setting("MEM0_COLLECTION") or p.memories
    if kind == "entities":
        return collection("memories", p) + "_entities"
    if kind == "episodes":
        return _setting("MEM0_EPISODES_COLLECTION") or p.episodes
    if kind == "wiki":
        # the wiki has its own space unless the caller names a profile (wiki_profile)
        return _setting("MEM0_WIKI_COLLECTION") or (p if profile else wiki_profile()).wiki
    raise ValueError(f"unknown collection kind {kind!r}")


_THRESHOLD_ENV = {
    "relevance_gate": "MEM0_RELEVANCE_THRESHOLD",
    "episode_floor": "MEM0_RAW_FALLBACK_COSINE_FLOOR",
    "nli_floor": "MEM0_NLI_GATE_COSINE_FLOOR",
}


def threshold(name: str, profile: EmbedProfile | None = None) -> float:
    """One threshold of the active space. The three with an operator knob (env or stack.env) take
    it first; the rest are fixed per profile (they are calibrated values, not tuning dials)."""
    p = profile or active()
    key = _THRESHOLD_ENV.get(name)
    if key:
        raw = _setting(key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return float(getattr(p.thresholds, name))


def threshold_overrides(profile: EmbedProfile | None = None) -> dict:
    """The threshold knobs set right now and the profile value each one replaces. A knob is not scoped
    to a space: one fitted on EmbeddingGemma-300m and left behind applies verbatim after a switch, so
    the server reports them on /health/deep and warns at start when the space is not the default."""
    p = profile or active()
    out = {}
    for name, key in _THRESHOLD_ENV.items():
        raw = _setting(key)
        if raw:
            out[name] = {"env": key, "value": raw, "profile_value": getattr(p.thresholds, name)}
    return out


def describe(profile: EmbedProfile | None = None) -> dict:
    """What /health/deep and the receipts report: the binding a reader can check."""
    p = profile or active()
    w = wiki_profile()
    return {
        "profile": p.name,
        "label": p.label,
        "model": embed_model(p),
        "ctx_tokens": p.ctx_tokens,
        "token_budget": p.token_budget,
        "long_model": long_model(p)[0],
        "long_token_budget": long_model(p)[1],
        "template_version": p.template_version,
        "media": p.media,
        "fusion": list(p.fusion),
        "collections": {k: collection(k, p) for k in ("memories", "entities", "episodes")},
        "wiki": {"profile": w.name, "model": embed_model(w), "collection": collection("wiki", w)},
        "threshold_overrides": threshold_overrides(p),
    }
