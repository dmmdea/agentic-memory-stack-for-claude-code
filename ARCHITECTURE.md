# Architecture

5 phases × 10 active layers (12 total including historical: M2 removed in v0.13, R2 wired in v0.13 C.1). Live runtime processes = 4 (mem0-server, Qdrant, llama-swap, Codex CLI) — Ollama decommissioned in v0.22 (embedder moved to EmbeddingGemma on llama-swap). See `docs/runbook-v0.13.md` for the distinction between layers and processes. Reading the layer map top-to-bottom mirrors the actual data flow: rules load first → write happens → store → read → background curation.

## Data flow (write path + read path)

```
                                    POLICY (always-on)
                                    ┌──────────────────────────────┐
                                    │ P1 Tier Protocol (CLAUDE.md) │
                                    │ P2 Storage Caps              │
                                    └──────────────┬───────────────┘
                                                   │ frames interpretation
                                                   ▼
WRITE PATH                                   READ PATH (when Claude calls mcp__mem0__memory_search)
──────────                                   ─────────
Stop / PreCompact hook                       Claude Code
  │                                            │
  ▼                                            ▼
stop-extract.ps1 (PS, 1-2s)                  mem0-mcp-shim.py (stdio)
  │ Start-Process detached pwsh                │ HTTP
  ▼                                            ▼
l1a-extract.ps1 (background, 10-min throttle) mem0 :18791 (FastAPI wrapper)
  │ codex exec --skip-git-repo-check           │
  │   -c model_reasoning_effort='"low"'        ▼
  ▼                                          mem.search(query, filters, top_k, threshold)
Codex CLI / ChatGPT OAuth                      │
  │ gpt-5.5, low reasoning, ~20-30s            ▼
  ▼                                          R1 Embedder (EmbeddingGemma-300m, llama-swap) → 768d vector
{"facts":["...","..."]}                        │
  │                                            ▼
  ▼ POST /v1/memories                        M3 Qdrant :6333 — cosine ANN over collection
mem0 :18791 (FastAPI wrapper)                  │
  │                                            ▼
  ▼ infer=false, metadata{tier=evidence}     R2 Reranker (bge-reranker, optional) → top-K reorder
M3 Qdrant :6333 stores 768d + payload          │
                                               ▼
                                             results returned to Claude as MCP tool response

3am Task Scheduler                           REFINEMENT (background)
  │                                          ┌─────────────────────────────────────────┐
  ▼                                          │ C1 Consolidator: evidence → insight     │
c1-consolidate.ps1                            │   daily 3am via Task Scheduler          │
  │ codex exec --skip-git-repo-check          │ C2 Promote/Demote: evidence → canonical │
  │   -c model_reasoning_effort='"medium"'    │   inline MCP tools + L10 auto-rule      │
  ▼                                          │ C3 Audit: heuristics + Bayesian trust   │
{"insights":["...","..."]}                    │   systemd-user 6h timer (l10-audit)     │
  │                                          └─────────────────────────────────────────┘
  ▼ POST as tier=insight
mem0 → M3 Qdrant
```

## Layer table (canonical reference)

| Phase | ID | Name | What | Endpoint / Hook | Reads from | Writes to |
|---|---|---|---|---|---|---|
| **POLICY** | **P1** | Tier Protocol | Defines tier interpretation (evidence/canonical/insight/temporal). Injection is model-aware (v0.22): full for frontier Opus/Fable, lighter for small Haiku, none for the offload harness — see [`docs/modular/model-aware-injection.md`](docs/modular/model-aware-injection.md). | `~/.claude/CLAUDE.md` snippet | every session | (read-only) |
| | **P2** | Storage Caps | Warns at growth boundaries | SessionStart hook → `storage-cap-check.sh` | mem0/Qdrant file sizes | stdout banner |
| **INGEST** | **I1** | Extractor | Codex (gpt-5.5, low effort) extracts durable facts | Stop / PreCompact → `stop-extract.ps1` → `l1a-extract.ps1` | transcript JSONL (last 24 turns, 12KB cap) | M1 |
| | **I2** | Defense Gate | (disabled by default) Prompt-Guard-2-86M ONNX classifier | `:8089` | writes from I1 | M1 (if pass) |
| **STORES** | **M1** | Semantic Memory | mem0 v2.0.4 — facts/insights/canonical, tiered, ADD-only | `:18791` HTTP + X-API-Key + MCP shim | I1 writes; C1/C2 promotions | M3 |
| | ~~**M2**~~ | ~~Episodic MCP surface~~ | **REMOVED 2026-06-09 (v0.13).** agentmemory v0.9.27 was never wired to a lifecycle hook; 0 sessions/0 memories were ever captured. Removed to eliminate dead surface. Episodic memory is a deliberate v0.14+ gap pending design (see Phase E in `docs/superpowers/plans/2026-06-09-memory-stack-v013-build.md`). | n/a | n/a | n/a |
| | **M3** | Vector Index | Qdrant 1.18.2 — 768d ANN, collection `mem0_egemma_768` (v0.22; old nomic `memories` kept for rollback) | `:6333` (systemd-user) | M1 writes | (terminal) |
| **RETRIEVAL** | **R1** | Embedder | EmbeddingGemma-300m → 768d, multilingual EN/ES (CPU, llama.cpp/llama-swap). v0.22 (2026-06-13): replaced English-only nomic-embed-text; needs asymmetric task prefixes applied by `mem0-server/egemma_embedder.py`. Ollama decommissioned. | `:11436/v1/embeddings` | read/write callers | stateless |
| | ~~**R2**~~ | ~~Reranker~~ | **NOT WIRED in v0.12.** bge-reranker-v2-m3 GGUF is on disk + llama-swap can serve it, but `mem0-server/app.py` does not call it from search. `SearchIn.rerank` is accepted but ignored. (Audit finding 2026-06-08.) Treat retrieval as embedder-only until a two-stage search endpoint is implemented. | n/a | n/a | n/a |
| **REFINEMENT** | **C1** | Consolidator | Codex (gpt-5.5, medium effort) synthesizes insights | Windows Task Scheduler daily 3am → `c1-consolidate.ps1` | M1 (tier=evidence, last 36h, top 30) | M1 (tier=insight) |
| | **C2** | Promote/Demote + Auto-promote | MCP tools + L10 30d durability auto-rule | `PATCH /v1/memories/{id}/tier`; ledger at `~/.mem0/tier-ledger.jsonl` | M1 (any tier) | M1 (target tier) + ledger |
| | **C3** | Audit | Heuristic flags + Bayesian trust over recent writes | `l10-audit.timer` (6h) | M1 (since last audit) | `~/.mem0/audit-flags.jsonl` |

## Why these specific choices

### Why Codex CLI as the subagent LLM?

Anthropic's Claude Max OAuth enforces single-concurrent-session per account. When Claude Code is open in VS Code, subprocess `claude --print` invocations from hooks fail intermittently with "Not logged in" because the interactive session holds the slot. Verified with multi-hour debugging, including PowerShell-detached invocations, WSL-bridged invocations, and explicit `WSLENV` forwarding — all unreliable while the interactive session is active.

Codex CLI uses **ChatGPT subscription OAuth** (separate auth surface) and works reliably headless from any Windows shell, regardless of what other Anthropic-side processes are running. gpt-5.5 quality is comparable to Opus 4.8 for structured extraction tasks.

### Why mem0 with a custom FastAPI wrapper?

mem0 v2.0.4 ships an official server, but it's Docker-first and Windows-fragile under WSL2. The custom wrapper at `mem0-server/app.py` exposes the same REST surface (`/v1/memories` POST/GET/PUT/DELETE, `/v1/memories/search` POST, `/health`) plus a `PATCH /v1/memories/{id}/tier` endpoint for promote/demote operations.

### Why Qdrant (not Chroma, FAISS, pgvector, etc.)?

Production-grade, fast, runs as a single binary, persistent on disk, well-supported by mem0. Chroma is simpler but slower; FAISS doesn't persist natively; pgvector requires Postgres.

### Why `EmbeddingGemma-300m` for the embedder? (v0.22, 2026-06-13)

768d, runs on CPU via llama.cpp/llama-swap, and — critically — **multilingual**. The
corpus is EN+ES; the previous `nomic-embed-text-v1.5` is structurally English-only, a
real defect. An eval over 30 real EN/ES query pairs (200-memory pool) measured nomic ES
recall@1 at 0.33 vs EmbeddingGemma 0.93; EN was a tie (~0.9). A post-cutover verify gate
against the live re-embedded collection confirmed recall@1 = 0.90 on BOTH EN and ES.
EmbeddingGemma requires asymmetric task prefixes (`task: search result | query: …` for
queries, `title: none | text: …` for documents) that neither llama.cpp nor mem0's stock
embedder applies — `mem0-server/egemma_embedder.py` is the prefix shim, installed onto the
Memory instance via `config.build_embedder()`. The swap required a full 2165-vector re-embed
into a new collection (`mem0_egemma_768`); llama.cpp's nomic is a different vector space
(parity cos=0.91), so a backend repoint alone would not have worked. The search similarity
threshold was recalibrated 0.4 → 0.30 (EmbeddingGemma's positive cosine separation is lower;
ES gold cosine bottomed at 0.36). This migration also let Ollama be fully decommissioned.

(Historical note: an earlier EmbeddingGemma trial was rejected as "worse on this domain" —
that test predated the prefix shim. With the correct asymmetric prefixes it wins clearly on
the bilingual corpus.)

**Rolling back the EmbeddingGemma migration (v0.22).** The old nomic `memories`
collection + its Qdrant snapshots are retained untouched as the rollback anchor; a
one-shot health-gated timer (`egemma-rollback-prune.timer`, fires 2026-06-21) prunes
them once the migration is confirmed durable. **If you ever roll back, do this IN
ORDER:**
1. **`systemctl --user disable --now egemma-rollback-prune.timer`** — STEP 1, ALWAYS
   FIRST. The prune is health-gated (it now reads the LIVE bound collection from
   `/health/deep` and SKIPs if mem0 is not bound to `mem0_egemma_768`), but disabling
   the timer is the belt-and-braces guarantee that a destructive one-shot can never
   race a rollback.
2. Repoint mem0: `config.py` `collection_name` → `memories`, embedder → nomic (`:11435`);
   redeploy + `systemctl --user restart mem0.service`.
3. Verify `/health/deep` reports `"collection":"memories"` and search works.
The gate's binding-check is the backstop if step 1 is skipped — after a rollback
`/health/deep` reports `memories`, the prune SKIPs, and it logs a `ROLLBACK DETECTED`
line to `~/.mem0/audit-flags.jsonl`. (v0.22 H2.)

### Why `bge-reranker-v2-m3` for reranking?

Best small cross-encoder on BEIR benchmarks. 568M params, Q4_K_M GGUF is 419MB. TTL-loaded via llama-swap so it only consumes VRAM when invoked (rare — only when N>5 or top score <0.85).

### Why a 3am cron for consolidation?

Codex calls run quietly in the background. 3am is a time when (a) the user is typically asleep, (b) no Codex usage is competing for ChatGPT subscription quota, (c) the Task Scheduler `-WakeToRun` flag wakes the PC from sleep specifically for this job. Once-daily prevents semantic drift (re-running on unchanged evidence produces near-duplicate insights).

### Why L10 audit at 6h cadence?

Enough to catch poisoned writes within a working day. Fast enough that contradicting facts get flagged before they pollute the model's working context. The audit is heuristic-only (no LLM cost).

## Failed approaches (do not retry)

Listed in `docs/runbook-v0.12.md` § "Failed approaches tried and ruled out". Key items:

- `claude --print` subprocess from any context → intermittent "Not logged in" due to Anthropic Max concurrent-session enforcement
- WSL bash → claude.exe via interop → fails regardless of `WSLENV` forwarding
- Local llama-swap models for extraction → quality benchmarks failed in prior testing
- Paid Anthropic API key → explicitly out of scope (no paid APIs beyond existing subscriptions)
