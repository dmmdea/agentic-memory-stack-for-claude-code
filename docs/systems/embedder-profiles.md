# Embedder Profiles

## Purpose

Names every embedding space the stack can run in and makes each component resolve its model, task
prefixes, token budget, collection names and cosine thresholds from that one definition. Vectors from
two embedding models are different spaces even at the same width: a query embedded by the wrong model,
or a threshold fitted on another model's score scale, degrades retrieval with no error anywhere. A
profile makes the space explicit, so changing the embedder is a measured, reversible switch instead of
a hunt through literals.

## Questions this doc answers

- Which embedding space is this box in, and how do I check it?
- Why are the memories and the wiki in different spaces?
- What does it take to move the store to another embedder, and how do I roll back?
- Why can a cosine threshold not be copied from one model to another?
- What was measured when EmbeddingGemma-2 was evaluated, and what did it decide?
- Why did the memories move to EmbeddingGemma-2 in 1.35.0, and what did that cost?
- How do images, audio and video become memories, and how are they searched?

## Scope

`mem0-server/embedder_profile.py` (the profiles and the resolver), the prefix shim's use of it
(`egemma_embedder.py`, `config.build_embedder`), every consumer that names a collection or applies a
cosine threshold, the migration tool `scripts/wsl/embedder-migrate.py`, and media memories
(`mem0-server/media.py`, 1.35.0).

## Non-scope

The reranker (`bge-reranker-v2-m3`) scores with its own model and is independent of the embedding
space; only the candidate pool it reorders changes. The sparse BM25 leg (`fastembed`) is independent of
the dense model and is copied verbatim by a migration. llama-swap entries are operator configuration
(see `install/llama-swap-setup.md`).

## Key concepts

- **Profile** — one embedding space: the llama-swap alias that serves it, the context it is served at,
  the embedding-input token budget, the query and document task prefixes, a template version, the
  collection names built in it, and its calibrated thresholds. Shipped profiles: `egemma-300m`
  (EmbeddingGemma-300m) and `egemma2` (EmbeddingGemma-2).
- **Active profile** — the memories' space: `MEM0_EMBED_PROFILE` (env > `~/.mem0/stack.env`). Every
  installer records it: a fresh install records `DEFAULT_PROFILE` (`egemma2` since 1.35.0), a store found
  without a record gets `LEGACY_PROFILE` (`egemma-300m`, the space every store was built in before
  profiles). A box that records nothing is read as `egemma-300m`, so a code upgrade never moves a store.
  Memories, entities and episodes are always in it.
- **Media memory** — a memory whose text is a caption and which carries images, audio or video
  (EmbeddingGemma-2 only, served with its `--mmproj` projector): its dense vector is one embedding of the
  caption and the media together (see "Media memories" below).
- **Wiki profile** — the LLM Wiki index's space: `MEM0_WIKI_EMBED_PROFILE`, default the active one. The
  wiki is a derived index of long pages and can live in another space (see below).
- **Template version** — bumped whenever a task prefix changes; a prefix change is a space change.
- **Embed identity** — `~/.mem0/embed-identity.json`, written by the migration tool: for each built
  collection, its profile, model, template version, point count, source and time.

## How the system works

`embedder_profile.active()` resolves the profile; `collection(kind)`, `embed_model()`, `threshold(name)`,
`long_model()` and `describe()` answer from it. The server builds its embedder and binds its Qdrant
collection from the active profile at start; scripts import the same module (the server directory is on
their path), so a chain step and the server can never disagree about the space. The model override is
scoped to its profile (`MEM0_EMBED_MODEL_<PROFILE>`); the older unscoped `MEM0_EMBED_MODEL`, which names an
EmbeddingGemma-300m file on the native authority (`embeddinggemma-ams`), applies to `egemma-300m` only, so a
profile switch cannot keep embedding queries with the old model.

### Thresholds are per space

Cosine scales are not portable. Measured on the same store (2026-10-08 lab, below):

| | EmbeddingGemma-300m | EmbeddingGemma-2 |
|---|---|---|
| off-topic question, top-1 cosine | 0.17–0.29 | 0.61–0.69 |
| relevant question, top-1 cosine (min EN / ES) | 0.40 / 0.40 | 0.72 / 0.73 |
| nearest-neighbour cosine, median | 0.65 | 0.84 |

So every cut-off lives in the profile: the context-bundle gate, the raw-trace episode floor, the NLI
write-gate pre-filter, the evidence-sweep floor, the autopromote sibling threshold, the semantic-dedup
tiers and the reranker skip. Three keep an operator knob (`MEM0_RELEVANCE_THRESHOLD`,
`MEM0_RAW_FALLBACK_COSINE_FLOOR`, `MEM0_NLI_GATE_COSINE_FLOOR`); the rest are calibrated values.

| threshold | egemma-300m | egemma2 | how egemma2 was set |
|---|---|---|---|
| relevance gate | 0.30 | 0.70 | clean-separation point of `relevance_probes.jsonl` (band 0.694–0.720, 0.026 wide vs 0.108) |
| episode floor | 0.20 | 0.68 | middle of the clean band [0.65, 0.71] of `episode_probes.jsonl` (the lab's episode vectors were built from the 800-character payload; re-check after a real migration, which embeds the full summary) |
| NLI floor | 0.5 | 0.79 | nearest-neighbour quantile map (2,000 memories, rank corr 0.88) |
| evidence-sweep floor | 0.45 | 0.77 | same map |
| autopromote sibling | 0.6 | 0.825 | same map |
| semantic-dedup tiers | .97/.95/.94/.94/.95 | .993/.99/.988/.988/.99 | full-corpus tail: at ≥ 0.988 no pair EmbeddingGemma-300m kept is deleted |
| reranker skip (fused score) | 1.0 | 1.0 | not a cosine: rank fusion is the same in every space, 1.0 = every leg ranks the head first ([hybrid fusion](fusion.md)) |

## The EmbeddingGemma-2 evaluation (2026-10-08) and the decision it made

Lab on a separate GPU workstation: Qdrant 1.19.1 with the authority's 2026-10-07 backup restored (16,946 memories, 2,110
entities, 3,896 episodes, 45 wiki pages), two lab mem0 servers from this branch on the same data and the
same reranker, llama.cpp b11490. The lab's EmbeddingGemma-300m reproduced prod's stored vectors (mean cosine
0.998); EmbeddingGemma-2 Q8_0 matched BF16 (cosine ≥ 0.9996). Query sets: 240 sampled memories with one
natural English and one Spanish question each; 45 wiki pages with a head question, a Spanish head question
and a question about a detail past character 1,500 of the body. MRR@10, paired bootstrap 95% CI.

- **Memories, dense only:** EmbeddingGemma-300m 0.953 EN / 0.928 ES, EmbeddingGemma-2 0.931 / 0.908. The
  task prefix was not the cause (search, question-answering and no prefix all rank the models the same way).
- **Memories, the per-prompt path (hybrid, no rerank):** 0.504 / 0.488 vs 0.369 / 0.381 — EmbeddingGemma-2
  lost 66 queries and won 1. mem0 fuses `(cosine + bm25 + entity) / max` additively, and a compressed, high
  cosine scale hands the ranking to the keyword and entity terms.
- **Memories, hybrid + rerank:** a tie (0.643 vs 0.645 EN).
- **Memories, revisited with the fusion fixed** (1.34.0, rank fusion, [hybrid fusion](fusion.md)), same lab,
  freshness on, 160 answerable paraphrase targets and 200 identifier targets: the per-prompt path gives
  EmbeddingGemma-2 0.944 EN / 0.951 ES on paraphrases (was 0.542 / 0.575) and EmbeddingGemma-300m 0.972 /
  0.959; EmbeddingGemma-2 still trails in every cell, by 0.01-0.04 (significantly in four of nine). The
  mixing was the cause of the large gap, not the model; the remaining gap keeps the memories where they are.
- **Wiki, deep-detail questions:** EmbeddingGemma-2 on whole pages 0.788 (4K alias) / 0.811 (8K alias) vs
  EmbeddingGemma-300m's best recipe 0.687 (+0.10, CI [+0.016, +0.194]); head questions a tie. Giving
  EmbeddingGemma-300m more of the page did not help it (+0.006).
- **VRAM:** EmbeddingGemma-2 at ctx 4096 costs what EmbeddingGemma-300m costs at 2048 (+735 vs +742 MiB);
  ctx 8192 adds about 600 MiB and bought no significant retrieval, so no long alias by default. Per model,
  not in total: a box that keeps the memories on EmbeddingGemma-300m and serves the wiki on
  EmbeddingGemma-2 holds both, so a small card that already runs a heavy model beside the memory stack
  needs those ~735 MiB free.

Decision then: **the memories stay on EmbeddingGemma-300m; the wiki index moves to EmbeddingGemma-2**
(`MEM0_WIKI_EMBED_PROFILE=egemma2`). The wiki's search cut-off moves with it: about 0.22 separates
on-topic from off-topic on EmbeddingGemma-300m, about 0.62 on EmbeddingGemma-2.

## The switch to EmbeddingGemma-2 (1.35.0)

The operator reversed that decision the same day (2026-10-08): EmbeddingGemma-2 replaces
EmbeddingGemma-300m everywhere — memories, entities, episodes and the wiki — for its images, audio and
video, and EmbeddingGemma-300m is retired once the stores have moved. What it costs on text was measured
before the switch, on the lab's captured queries with the fusion retuned for EmbeddingGemma-2 alone
([hybrid fusion](fusion.md#the-embedding-model-question)): about 0.02 MRR@10 on paraphrases and
identifiers and 0.03 on what the per-prompt hook injects, against EmbeddingGemma-300m. The retune itself
(k = 1, keyword weight 0.5) gained +0.011 on identifiers and +0.009 on injected results at no
paraphrase cost.

What the projector was checked for (2026-10-08, llama.cpp b11490, the authority's RTX 3050):

- **Text is unchanged**: text vectors with and without `--mmproj` are identical (cosine 1.0), so the
  calibrated thresholds hold for text, and the Q8_0 projector matches BF16 (cosine ≥ 0.9993 on images,
  speech and a tone).
- **Captions find their media**: four images (three distinct renders and a wallpaper) were each the top
  match of their own English caption (0.75-0.81 against 0.43-0.52 for the others), and of a Spanish
  caption; three audio clips (two spoken sentences and a tone) each top-matched their own description,
  and a spoken sentence scored 0.84 against the text memory it says and 0.62 against another. A short
  mp4 clip embeds.
- **VRAM**: served at ctx 4096 / ubatch 2048 with the projector: 1,196 MiB loaded, 1,466 MiB peak after
  image embeds, 1,536 MiB after a video. Ubatch 4096 would cost 1,870 MiB to take inputs past ~1,900
  tokens, which memories (a 4,000-character cap) never reach, so the hot alias's token budget is 1,900.
  The long alias (ubatch 8192) stays optional per box for whole wiki pages.

## Media memories

`POST /v1/memories` takes `media: [{type, data, filename?}]` (base64 `data`; `type` image, audio or video,
checked against the bytes) with `infer: false` and the caption as the text. mem0 stores the caption as a
normal memory (id, hash, history, the BM25 leg, entities, tier, admission); the server then stores each
file content-addressed under `~/.mem0/media/<sha[:2]>/<sha>.<ext>` (`MEM0_MEDIA_DIR`), records
`{type, sha256, mime, ext, bytes, filename}` per item in the payload's `media`, and replaces the dense
vector with one embedding of the caption (document prefix) followed by the media, through Qdrant's
`update_vectors` on the unnamed dense vector only, so the sparse BM25 vector stays. `media_embedded` in
the payload and in the add result says whether that happened: a failed media embed (projector not
loaded, input too long) keeps the caption-only vector and never loses the write. A media memory is not
deduplicated by its caption.

`POST /v1/memories/search` takes the same `media` field: the query vector becomes one embedding of the
query text (query prefix) and the media, so a photo or a clip finds the memories about it. The query text
is required; it says what to look for. The swap is scoped to that one search's query embed and spent
by it (`egemma_embedder.QUERY_MEDIA`); the entity leg and every stored text keep text embeddings. A media
search is never reranked: the cross-encoder reads only the query text against each caption and would sink
the memories the media found. Media a search cannot fit beside its query in the embed window are a 400.
The nightly semantic dedup never deletes a media memory (its vector may be its caption's alone).

`GET /v1/memories/{id}/media/{n}` returns the n-th file. The MCP shim's `memory_add` and `memory_search`
take `media_paths` (Windows or WSL paths), and `memory_get_media` saves a file locally to look at or
listen to. Limits: 4 items per memory (`MEM0_MEDIA_MAX_ITEMS`), 20 MB each (`MEM0_MEDIA_MAX_BYTES`), and
the caption plus the media must fit the alias's ubatch (an image costs 280 tokens, audio 25 per second,
a short video about 600). A PUT to a media memory re-embeds its caption with the stored media. Deleting
a media memory keeps its files: a name is a content hash that another memory may share, and nothing
prunes the directory yet, so files of deleted memories stay on disk and in the backups.

## Important flows

- **Moving the memories to another space** (the 1.35.0 move to EmbeddingGemma-2 uses it): see
  [MIGRATION.md](../MIGRATION.md#moving-to-another-embedding-space). In short: serve the new alias,
  `embedder-migrate.py --to <profile>` builds the new collections beside the old ones, `--verify`; then,
  with mem0 stopped, `--catch-up` (a `--dry-run` first) and the switch through
  `install/linux-authority.sh --embed-profile <profile>`, which records the profile and starts mem0. The
  tool refuses to write a collection the stack is using, so no catch-up runs into the new space after the
  switch. Rollback is the same tool in reverse (with mem0 stopped) plus the old profile.
- **Moving the wiki** is a rebuild, not a migration: set `MEM0_WIKI_EMBED_PROFILE`, serve its alias on
  the authority, run `wiki-index-build.py`. A PC that refreshes the wiki without serving the alias sends
  its builds and searches to the brain (`wiki-index.sh`). The old wiki collection
  is kept (4 MB, rebuildable from the vault in seconds).

## Data and state

Collections are named by model (`mem0_egemma_768`, `mem0_eg2_768`, …), never reused across spaces:
incremental indexers skip by point id or content hash, so a reused name would silently keep old-model
vectors. `~/.mem0/embed-identity.json` records what each collection was built with. `stack-backup.sh`
snapshots the active space's collections and, while they exist, the other generation's (the rollback
anchor); the manifest records the profile. Media files live in `~/.mem0/media`; each backup set carries
them as one `media-<ts>.tar` (absent while there is no media), and both restore paths extract it
additively (names are content hashes, so an existing name already holds the same bytes).

## Interfaces and entry points

`GET /health` (`embedder`, `embed_profile`), `GET /health/deep` (`embed_profile`: profile, model, template
version, collections, wiki, `media`; `checks.media`: enabled, directory, media embeds ok/failed since
start, the last error), `scripts/wsl/embedder-migrate.py`, `install/linux-authority.sh --embed-profile`,
the media fields of `POST /v1/memories` and `/v1/memories/search`, `GET /v1/memories/{id}/media/{n}`.

## Invariants and assumptions

- A collection holds vectors of exactly one profile. A profile change never writes into a collection of
  another profile.
- Query and document prefixes come from the profile; a prefix change bumps `template_version`.
- Thresholds come from the profile of the space they compare in.
- With nothing configured, behaviour is byte-identical to the stack before profiles: an unrecorded box is
  the legacy space, whatever `DEFAULT_PROFILE` says. Only installers apply the default, to a box with no
  store, and they record it.
- A media memory's dense vector is in the same space as every text vector; only a profile with
  `media=True` accepts media.

## Error handling

An unknown profile name is a startup failure (`FAIL: MEM0_EMBED_PROFILE=… is not a known embedding
profile`), never a silent fallback. The migration tool refuses source == target, fails loud on an embedder
that returns the wrong width, retries cold or busy embedder answers, and reports what it did before a
failure.

## Observability and debugging

`curl <authority>/health/deep | jq .embed_profile` shows the space. `embedder-migrate.py --verify` proves a
built collection matches its source (counts) and its model (a sampled re-embed must reproduce each stored
vector, cosine ≥ 0.995).

## Testing notes

`mem0-server/tests/test_embedder_profile.py` (resolution, scoping, thresholds, the wiki split);
`mem0-server/tests/test_media.py` (types from bytes, storage, the embed request, the search swap through
mem0's real search path, the add and fetch endpoints, the shim). The lab eval that set the egemma2 values
is kept in the private operator repo (`eval/embedder-ab/`).

## Common pitfalls

- Same width is not the same space: `dim == 768` cannot tell the two EmbeddingGemma models apart.
- A higher cosine scale is not a better model. EmbeddingGemma-2 scores off-topic questions where
  EmbeddingGemma-300m scores relevant ones.
- The per-prompt path is hybrid and unreranked; compare models through it, not through dense search alone.

## Source map

`mem0-server/embedder_profile.py`, `mem0-server/config.py` (`build_embedder`), `mem0-server/egemma_embedder.py`,
`mem0-server/media.py`, `scripts/wsl/embedder-migrate.py`.

## Related docs

[mem0 API](mem0-api.md), [wiki index](wiki-index.md), [reranker](reranker.md),
[ADR: embedder profiles](../architecture/decisions/embedder-profiles.md),
[ADR: EmbeddingGemma on llama-swap](../architecture/decisions/embeddinggemma-on-llama-swap.md).
