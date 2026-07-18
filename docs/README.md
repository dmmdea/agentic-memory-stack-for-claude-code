# Documentation map

Everything in this repo's documentation, organized so you can find the right doc for the task in front of you ([Diátaxis](https://diataxis.fr/)-style). Sections follow the docs taxonomy: **systems** (how one part works), **flows** (behavior that crosses parts), **architecture** (why the shape is what it is), **glossary**, **reference**, and **templates**.

Agents: [`../AGENTS.md`](../AGENTS.md) / [`../CLAUDE.md`](../CLAUDE.md) tell you where to look and how to work; these docs explain how the application works. Read the relevant docs before changing a system, and update them in the same change.

Optional: run `npx mdts` from the repository root to browse these Markdown docs in a local browser page and click through the links between them. Because `npx` may download and execute the package from npm, you are responsible for reviewing and trusting it before running the command.

## Start here (explanation)

| Doc | What it explains |
|---|---|
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | **The whole system**: six layers, trust tiers, life of a memory, diagrams, safety invariants, design decisions |

Then read the system and flow docs for the area you're changing.

## Systems — how one part works

| Doc | What it explains |
|---|---|
| [`systems/memory-model.md`](./systems/memory-model.md) | The model the machinery serves: layers, tiers, query classes, the life of a memory |
| [`systems/tier-policy.md`](./systems/tier-policy.md) | The trust-tier protocol and its server-side enforcement (the `403` boundary) |
| [`systems/admission-gate.md`](./systems/admission-gate.md) | Retrieval admission policy — why a record isn't surfacing (hide reasons, query classes, forensics) |
| [`systems/reconciliation.md`](./systems/reconciliation.md) | How the store stays honest: the sweeps, the two judges, verdict semantics, never-auto-hide + the review queue |
| [`systems/model-aware-injection.md`](./systems/model-aware-injection.md) | Why the injected `[MEMORY CONTEXT]` block is scaled to the consuming model's tier |
| [`systems/episodic.md`](./systems/episodic.md) | The SQLite + FTS5 episodic sidecar: session-level temporal records and schema |
| [`systems/goals.md`](./systems/goals.md) | Persistent multi-session objectives tracked alongside episodic memory |
| [`systems/open-questions.md`](./systems/open-questions.md) | The cross-session open-questions registry and why it is global, not per-session |
| [`systems/continuity.md`](./systems/continuity.md) | Session-continuity behavior (resume, checkpoints) and the problem it solves |
| [`systems/codex-hooks.md`](./systems/codex-hooks.md) | The L1a extractor + C1 consolidator hooks, and why unattended cron runs on Codex |
| [`systems/dream-skill.md`](./systems/dream-skill.md) | The nightly 4-phase consolidation ("dream") pattern — *design doc; marked DESIGN in-file* |
| [`systems/l10-audit.md`](./systems/l10-audit.md) | The post-hoc audit job: what it flags and how to triage the flags |
| [`systems/mem0-api.md`](./systems/mem0-api.md) | The mem0 server's REST endpoints and the MCP shim in detail |
| [`systems/reranker.md`](./systems/reranker.md) | The bge-reranker stage — *design doc; marked DESIGN in-file* |
| [`systems/dpapi-canonical-key.md`](./systems/dpapi-canonical-key.md) | Canonical-key custody: DPAPI blob, runtime injection, recovery, rotation |
| [`systems/llama-swap-binding.md`](./systems/llama-swap-binding.md) | The loopback-bind requirement for local inference — *historical record; the current setup guide is [`../install/llama-swap-setup.md`](../install/llama-swap-setup.md)* |

## Flows — behavior across systems

| Doc | What it explains |
|---|---|
| [`flows/memory-capture.md`](./flows/memory-capture.md) | How conversations become memory: the four capture moments, the inferability gate, corrections, the nightly consolidation |
| [`flows/memory-retrieval.md`](./flows/memory-retrieval.md) | How memory reaches the agent: embedding, hybrid scoring + calibration, the four delivery channels, abstention |

## Architecture & decisions

| Doc | What it explains |
|---|---|
| [`architecture/README.md`](./architecture/README.md) | What belongs in architecture vs systems vs flows; cross-system structure and durable constraints |
| [`architecture/decisions/README.md`](./architecture/decisions/README.md) | How ADRs work here: when to write one, the frontmatter contract, supersession |

## Glossary

| Doc | What it explains |
|---|---|
| [`glossary.md`](./glossary.md) | The canonical meaning of domain terms used across these docs and the code |

## Install & set up (tutorial / how-to)

| Doc | When |
|---|---|
| [`../skill/install-agentic-memory-stack/SKILL.md`](../skill/install-agentic-memory-stack/SKILL.md) | The guided install walkthrough (or just run `install.ps1` — see the top-level README) |
| [`../install/llama-swap-setup.md`](../install/llama-swap-setup.md) | Building llama.cpp + llama-swap with both GGUFs (the one prerequisite the installer can't auto-satisfy) |
| [`../skill/install-agentic-memory-stack/references/troubleshooting.md`](../skill/install-agentic-memory-stack/references/troubleshooting.md) | Install-time failure matrix |

## Reference — operations, contracts, and development

These live at the `docs/` root because they describe the repository and its running system rather than a single system or flow.

| Doc | When |
|---|---|
| [`operations.md`](./operations.md) | Something's broken or a banner fired: symptom → diagnosis → fix, service management, schedules, the review queue |
| [`api-contracts.md`](./api-contracts.md) | The REST API + MCP tool surface — the compatibility contract upgrades must preserve |
| [`MIGRATION.md`](./MIGRATION.md) | **Moving to a new machine with your memory intact** — snapshot, fresh install, restore, re-key, verify, decommission |
| [`data-backup.md`](./data-backup.md) | Backing up the memory data itself, which the code backup does not cover |
| [`DEVELOPMENT.md`](./DEVELOPMENT.md) | Changing the stack itself: repo tour, test suites, the deploy path, release ritual, conventions |
| [`TESTING.md`](./TESTING.md) | Where the three test suites live and how to run them |

## Templates & style

| Doc | When |
|---|---|
| [`templates/system.md`](./templates/system.md) | Starting a new system doc |
| [`templates/flow.md`](./templates/flow.md) | Starting a new flow doc |
| [`templates/adr.md`](./templates/adr.md) | Starting a new architecture decision record |
| [`STYLE.md`](./STYLE.md) | The writing rules and the operator-neutral vocabulary every doc must use |

## Development history and the maintainer archive

This repository is the primary source of truth for the product and is edited directly, through pull requests. [`../CHANGELOG.md`](../CHANGELOG.md) is the product's version authority (as of v1.16.2); earlier, pre-inversion history is summarized in its first entries.

A separate maintainer archive holds working material that is not part of the shipped product and is not mirrored here: research fit-analyses behind each design, per-release build plans, audit records, the faithfulness and retrieval eval harnesses, dependency-version and upgrade notes, and historical runbooks. Nothing in this index depends on that archive — the docs above are the maintained current-state set.

> **Docs and code must agree.** Every change that alters behavior, responsibilities, flows, invariants, interfaces, or assumptions updates the affected docs in the same change. If a doc and the code disagree, fix one or flag the mismatch — do not leave it.
