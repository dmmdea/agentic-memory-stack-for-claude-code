# CLAUDE.md — agentic-memory-stack (project rules)

Loaded automatically by Claude Code when the working directory is this repo. Authoritative for *this* project; the user's global `~/.claude/CLAUDE.md` still wins on conflict.

## What this project is
The agentic memory stack that Claude (and any future Anthropic model running for the operator) uses for semantic memory. Live at v0.30+ (v1.0 milestone). Five components matter (4 runtime processes + 1 credential file):

1. **mem0-server** (`mem0-server/app.py`) — FastAPI wrapper around mem0 v2.0.4 on `127.0.0.1:18791`. Owns add/search/list/update/tier-change. X-API-Key auth.
2. **Qdrant** — vector store on `127.0.0.1:6333` (LOOPBACK ONLY; do not bind 0.0.0.0). 768-d collection `mem0_egemma_768` (the v0.22 EmbeddingGemma re-embed; old nomic `memories` collection retained untouched for rollback). **Rolling back the v0.22 migration: STEP 1 is always `systemctl --user disable --now egemma-rollback-prune.timer`** (the one-shot prune deletes the `memories` rollback anchor; disable it before repointing `config.py`). Full ordered runbook in `ARCHITECTURE.md`. The prune gate also reads the live bound collection from `/health/deep` and SKIPs if mem0 is not bound to `mem0_egemma_768` (v0.22 H2 backstop).
3. **llama-swap** — single local inference stack on `127.0.0.1:11436` (`always_loaded` group). Serves **EmbeddingGemma-300m** (mem0's embedder, multilingual EN/ES, CPU, 768-d — replaced English-only nomic in v0.22) and `bge-reranker-v2-m3` (R2 reranker, ctx 8192, `RERANK_DOC_MAX_CHARS=6000`). The embedder needs asymmetric task prefixes that mem0's stock embedder won't apply, so `mem0-server/egemma_embedder.py` wraps it. **Ollama fully decommissioned 2026-06-13** (`:11435` CPU + `:11434` GPU no longer in mem0's path).
4. **Codex CLI** — gpt-5.5 via ChatGPT subscription OAuth. Runs unattended cron (L1a Stop-hook extractor, dream-skill consolidator). Used because Claude Max OAuth blocks concurrent sessions and unattended cron cannot share the interactive slot.
5. **canonical-key** (`~/.mem0/canonical-key`, mode 600) — HMAC signing key for `tier=canonical` promotions via `scripts/wsl/mem0-canonize.sh`. Agentic Claude cannot canonize via MCP. Added v0.14 B.

Bigger picture lives in `ARCHITECTURE.md`. v0.13 runbook is `docs/runbook-v0.13.md` (lands in Task B.3). Plan tracker is `docs/superpowers/plans/2026-06-09-memory-stack-v013-build.md` + `docs/progress.md`.

Day-to-day session continuity: `docs/session_summary.md` (overwritten per session — fresh state) and `docs/progress.md` (durable, newest-first decision log). Read both at SessionStart.

## Ground Truth Hierarchy
When two memory blocks disagree, trust in this exact order. Quote the source in your reply when it matters.

1. **L0 — User's direct words in the current conversation.** Always wins. If the operator just said X, X is true even if mem0 says Y.
2. **L1 — Repo state on disk.** Files you can `Read` here, this commit. Beats memory.
3. **L2 — mem0 `tier=canonical`** with `actor=user-direct` and a non-empty `reason`. the operator explicitly locked these in. Authoritative for cross-session facts.
4. **L3 — mem0 `tier=insight`** with `source=c1-consolidator` (or future `dream-consolidator`) and `source_memory_ids` lineage. Synthesized; trust unless the operator contradicts.
5. **L4 — mem0 `tier=stable`** (promoted from evidence after manual review). Trust as background.
6. **L5 — mem0 `tier=evidence`.** Default tier on add. Treat as advisory; consequential claims need verification (Read/Grep/curl/log) before acting.
7. **L6 — mem0 `tier=temporal`.** Time-scoped facts; check the validity window in metadata before trusting.
8. **L7 — Anything else** (auto-memory MEMORY.md if ever enabled, scratch notes, this turn's reasoning).

When acting on consequential claims (deploys, config changes, irreversible edits), require L0-L2 evidence or verify with a tool call first. Cite the level briefly (`"per mem0 tier=canonical id 7f3…"` is enough).

## Tier policy (enforced server-side; do NOT try to bypass)
- `tier=canonical` cannot be set via `POST /v1/memories`. Add as `evidence`, then `PATCH /tier` with `actor='user-direct'` AND non-empty `reason`. Server returns 403 otherwise.
- `tier=insight` requires `source` (on add) or `actor` (on promote) containing `c1` or `consolidator`. Reserved for the nightly consolidator.
- `tier=evidence` and `tier=temporal` are the only tiers a normal `POST` accepts.
- `MAX_MEMORY_CHARS = 4000` (env `MEM0_MAX_MEMORY_CHARS`) enforced on add/update. Prefer atomic facts (≤25 words) for retrieval precision; milestone/checkpoint summaries up to the cap are fine — the v0.22 model-aware injection truncates per-item, so stored size doesn't affect prompt budget. Raised from 1500 in v0.22 (it was 413-rejecting legitimate milestone memories).

See `docs/modular/tier-policy.md` for the full table, `docs/modular/mem0-api.md` for endpoint semantics (both land in Task B.3).

## Editing rules
- **Verify before claiming done.** Run `scripts\windows\Test-MemoryStack.ps1` after any change touching mem0-server, llama-swap, MCP shim, or hooks. Quote the output.
- **One commit per task.** Plan tasks are atomic units; squash work *within* a task as you go, but ship one commit per task with `v0.13 X.N:` prefix.
- **Tests live next to code.** `mem0-server/tests/` for server. Always sync the edited `app.py` to `~/apps/mem0-server/app.py` and restart `systemctl --user restart mem0` after any code change to the server.
- **Pip installs go in the `~/apps/mem0-server/.venv`** (Python 3.12). the operator's global is 3.13.
- **No paid APIs.** Subscriptions only (Claude Max for Claude itself; ChatGPT for Codex). If you find yourself drafting an `OPENAI_API_KEY` env var, stop.

## Common operations (copy-pasteable)
- Restart server: `wsl.exe -e bash -lc "systemctl --user restart mem0 && sleep 2 && curl -s http://127.0.0.1:18791/health"`
- Health snapshot: `& scripts\windows\Test-MemoryStack.ps1`
- Promote canonical (v0.14+, CLI required): `from your stack repo run `bash scripts/wsl/mem0-canonize.sh <id> '<reason>'` (in WSL)`
- Search with reranker: `mcp__mem0__memory_search(query="...", limit=5, rerank=True)` (v0.13+)
- List recent: `mcp__mem0__memory_list(limit=50)`
- Run dream-consolidate manually: `& C:\Users\$env:USERNAME\.claude\scripts\dream-consolidate.ps1`

## Things that are *not* this project
- mem0 cloud, OpenMemory subdir, Letta, Graphiti, OB1, MetaMCP, Postiz, agentmemory, BGE-M3 + TEI three-in-one. All evaluated, all rejected — see `CHANGELOG.md` history. Do not propose them.

## Plugins / alternatives evaluated and rejected (2026-06-09)
- **[thedotmack/claude-mem](https://github.com/thedotmack/claude-mem)** — REDUNDANT. Same mission (compress observations, re-inject across sessions) but uses Chroma vector DB (has a [known 35GB-RAM bug, issue #707](https://github.com/thedotmack/claude-mem/issues/707)), uses Anthropic Agent SDK for compression (paid API calls, violates `$0 marginal cost` rule), and has the SAME hook surface (`Stop`/`SessionStart`/`PostToolUse`) that would collide with our L1a extractor + dream-consolidator. Adopting it now would throw away 14 audit-remediated commits + tier policy + Ground Truth Hierarchy. Don't install.
- **[mksglu/context-mode](https://github.com/mksglu/context-mode)** — COMPLEMENTARY, DEFERRED to v0.14 evaluation. Solves a different problem (MCP tool-output bloat in active context, not durable memory). Local, no paid APIs. Defer because it registers conflicting `Stop`/`PreCompact`/`SessionStart` hooks and auto-copies a CLAUDE.md "routing file" that would fight this one. Worth a real evaluation in isolation in v0.14.
- **[Agent Exploration Toward AGI (SSRN-6748619)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6748619)** — v0.14 architectural reference (Beihang/Peking/Tsinghua/UC Berkeley et al. 111-page framework). Defines a 5-level trajectory (Responder→Reasoner→Agent→Prospector→Ecosystem) and 3 foundations (Information Gain, Value Improvement, Epistemic Reachability). Bridge for v0.13: dream-consolidator gather phase weights surprising/contradicting evidence (Information Gain principle). Full integration deferred to v0.14.

## When you find yourself unsure
Run the verification, read the diff, and ask the operator. He prefers a one-line "I checked X, found Y, propose Z" over a long speculation.
