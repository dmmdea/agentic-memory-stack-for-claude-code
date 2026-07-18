# Agentic Memory Stack — architecture (v1.0)

Read on demand from `SKILL.md`. The canonical, in-repo deep dive is `ARCHITECTURE.md` + `docs/systems/*` + `docs/flows/*`; this is the skill-side summary.

## Live runtime processes

| Process | Where | Role |
|---|---|---|
| `mem0-server` (`:18791`) | WSL systemd-user | FastAPI wrapper over mem0 2.0.4; also hosts the episodic/goals/open-questions SQLite sidecar + the admission gate + the canonical-key HMAC enforcement. |
| Qdrant (`:6333`) | WSL systemd-user | Vector store. Collections: `mem0_egemma_768` (memories) + `episodes_egemma_768` (R4 episode embeddings). Loopback-bound. |
| llama-swap (`:11436`) | WSL | Single local inference stack: **EmbeddingGemma-300m** (mem0's CPU embedder, 768-dim, multilingual) + **bge-reranker-v2-m3**. Loopback-bound. (Ollama decommissioned v0.22.) |
| Codex CLI | Windows | The subagent LLM for ALL LLM judgment — L1a extraction, nightly consolidation, the contradiction-sweep judge, the NLI write-gate. ChatGPT-subscription auth. Reached from WSL python via the loopback `codex-shim.ps1` HTTP shim (`:18792`). |
| Claude Code hooks | Windows | Stop/PreCompact (extract), SessionStart (warm + caps), UserPromptSubmit (`[MEMORY CONTEXT]` injection via the resident named-pipe daemon), PreToolUse (audit gate). |

## Conceptual layer map

`POLICY` (tier protocol in CLAUDE.md + storage caps) → `INGEST` (Codex L1a extractor, hook-fired) → `STORES` (mem0 + Qdrant + episodic.db) → `RETRIEVAL` (EmbeddingGemma embedder + bge-reranker) → `REFINEMENT` (nightly dream-consolidate + promote/demote + heuristic audit).

## Trust tiers

`evidence` (default; time-decayed on the durable read path) → `insight` (consolidator-written) → `canonical` (ground truth; HMAC-gated, CLI-only promotion). Plus `temporal` (valid-until, decay-scanned) and `stable` (atemporal). The admission gate filters search results by scope + tier + recency + supersession + a task-relevance floor.

## v1.0 faithfulness features (R1–R6)

The v1.0 milestone turned the stack from "store + inject and hope" into "measurably faithful memory." Each was research-grounded (`docs/research/2026-06-14-faithful-self-evolvers-analysis.md`) + adversarially audited:

- **R1 — causal-intervention faithfulness eval** (`eval/faithfulness/`): CMI do-intervention (no/with/corrupted memory) measuring Behavior-Change Rate, Counterfactual Sensitivity, Adoption. Agent-under-test = Codex; deterministic substring scoring.
- **R3 — extractor specificity** (the #1 lever): the Codex extractor keeps specific/executable, drops inferable facts ("could Claude already know this? → drop"); typed Title/Description/Content schema. Measured lift: adoption 1.0 vs 0.5, counterfactual-sensitivity 0.667 vs 0.5 on a non-saturated probe set.
- **R2 — abstention-first, entity-side gated injection**: inject only when project-specific entities are present + relevance clears the semantic floor; cap K at 1–2; NOOP (inject nothing) as the default.
- **R5 — governance**: NLI write-gate (Codex-judged, opt-in) + Weibull freshness read-gate (evidence/temporal tiers; atemporal tiers never decay) + weekly contradiction-sweep (Codex-authoritative; local sweep stamps a `_pending` key the admission gate ignores) + episodic-ledger reconciliation.
- **R6 — placement / attention hygiene**: the single most-relevant memory is rendered LAST in the `[MEMORY CONTEXT]` block (adjacent to the prompt = the recency peak).
- **R4 — raw-trace fallback**: on a low-confidence/empty condensed retrieval, surface ONE compact verbatim decisive-turn snippet from the SQLite episode (semantic episode retrieval; cosine floor 0.20). Ships **ON** by default (`MEM0_RAW_FALLBACK_ENABLED=1` in `systemd/mem0.service`); set `=0` to disable.

## Operator-agnostic install (v0.30 / Phase 7A)

The installer works for any operator. The four operator-specific dimensions (WSL user, Windows user, WSL distro, repo path) are resolved at install time into an **install receipt** (`~/.claude/scripts/mem0-stack.config.psd1` + `~/.mem0/stack.env`). Receipt-driven scripts (`Test-MemoryStack.ps1`, `dream-consolidate.ps1`) read it at runtime; other deployed scripts carry bounded sentinels (`__WSL_USER__`/`__WIN_USER__`/`__WSL_DISTRO__`/`__REPO_ROOT_WSL__`) substituted at deploy and reversed by the R9 "deployed hooks freshness" SHA-parity normalizer. No developer path or handle ships in the deployable surface.

## Why Codex (not Claude) as the subagent

Claude Max OAuth enforces a single concurrent session — headless `claude --print` from hooks fails intermittently when an interactive session holds the slot (by design; headless is Anthropic's paid-API territory). Codex CLI authenticates via a separate ChatGPT-subscription OAuth (no concurrency block) and runs reliably headless from any Windows shell, at zero marginal cost. Quality matches for structured extraction. **All LLM judgment routes to Codex; local llama-swap models are embedding/reranking only.**
