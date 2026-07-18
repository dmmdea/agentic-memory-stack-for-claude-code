# CLAUDE.md — agentic-memory-stack (project guide for AI coding agents)

This file is loaded automatically by Claude Code when the working directory is this
repo (other agents: read it directly). It tells you what this project is, how to work
in it, and where things live.

## What this project is

A persistent, multi-tier, measurably-faithful **memory backend for Claude Code** on
Windows + WSL2. Five components (4 runtime processes + 1 credential file):

1. **mem0-server** (`mem0-server/app.py`) — FastAPI wrapper around mem0 on
   `127.0.0.1:18791`. Owns add/search/list/update/tier-change. `X-API-Key` auth.
2. **Qdrant** — vector store on `127.0.0.1:6333` (loopback only; never bind 0.0.0.0).
   768-dim collection `mem0_egemma_768`.
3. **llama-swap** — local inference on `127.0.0.1:11436`: EmbeddingGemma-300m
   (embedder; asymmetric task prefixes applied by `mem0-server/egemma_embedder.py`)
   + bge-reranker-v2-m3. Setup guide: `install/llama-swap-setup.md`.
4. **Codex CLI** — optional; runs the unattended extraction/consolidation jobs.
5. **canonical key** (`~/.mem0/canonical-key`, mode 600) — HMAC signing key for
   `tier=canonical` promotions via `scripts/wsl/mem0-canonize.sh` (user-direct CLI
   only; agents cannot canonize through the API/MCP).

## Install / verify / upgrade

- **Install:** run `install.ps1` from an elevated PowerShell — it drives the 4 phases
  (`install/0-prereqs.ps1` → `1-wsl-services.sh` → `2-windows-config.ps1` →
  `3-verify.ps1`). It is idempotent: re-running after a failure resumes safely.
- **Prereq you must do yourself:** llama-swap on `:11436` —
  see `install/llama-swap-setup.md`.
- **Verify anytime:** `install/3-verify.ps1`, or
  `curl http://127.0.0.1:18791/health/deep` (from WSL).
- **Upgrade:** `git pull` + re-run `install.ps1`. Release notes live on the GitHub
  Releases page of this repository.

## Where things live

| Path | What |
|---|---|
| `install/` + `install.ps1` | 4-phase idempotent, operator-agnostic installer |
| `mem0-server/` | The FastAPI server + admission gate, tiers, episodic store, embedder shim |
| `scripts/windows/` | PowerShell runtime: hooks, compiled UserPromptSubmit client, extractor |
| `scripts/wsl/` | WSL-side maintenance jobs (sweeps, backups, canonize CLI) |
| `systemd/` | User-level unit files the installer deploys |
| `skill/` | The `install-agentic-memory-stack` Claude Code skill |
| `docs/` | All detailed documentation — see the map below |

## Documentation map

Detailed documentation lives in `docs/`. `AGENTS.md`/`CLAUDE.md` tell agents where to
look and how to work; `docs/` explains how the application works.

| Where | What it holds |
|---|---|
| [`docs/README.md`](docs/README.md) | The docs index — start here to find the doc for your task |
| `docs/systems/` | How one system works today: responsibilities, interfaces, invariants |
| `docs/flows/` | Behavior that moves end to end across several systems |
| [`docs/architecture/`](docs/architecture/README.md) | Cross-system structure and durable technical constraints |
| [`docs/architecture/decisions/`](docs/architecture/decisions/README.md) | ADRs — only `Accepted` ones are current guidance |
| [`docs/glossary.md`](docs/glossary.md) | Canonical meaning of the domain terms used in docs and code |
| `docs/templates/` | Starting points: [system](docs/templates/system.md), [flow](docs/templates/flow.md), [ADR](docs/templates/adr.md) |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Whole-system data-flow and layer map — read before changing server code |

### Agent workflow

1. Read `AGENTS.md`.
2. Use `docs/README.md` to find relevant documentation.
3. Read the system, flow, architecture, and glossary docs related to the area being changed.
4. Inspect source files for implementation details.
5. Make the code change.
6. Update relevant docs if behavior, responsibilities, flows, invariants, assumptions, interfaces, or glossary-defined concepts changed.
7. Ensure docs and code agree before finishing.

### Rules

- **Read the relevant docs before changing a system** — not after the change is written.
- **Update affected docs in the same change**, never as a follow-up.
- **Docs and code must agree** — if they disagree, fix one or flag the mismatch.
- **Do not dump system detail into this file.** Deep behavior belongs in `docs/`; this
  guide stays a router.

## Trust tiers (the core protocol)

`evidence` → `insight` → `canonical`. Writes land as evidence/temporal; `canonical`
is ground truth and can only be set via the HMAC-signed CLI path. When two memories
disagree, higher tier wins; same tier → newer wins. The admission gate fail-closes on
brand scope: pass `brand=` when your context has one.

## Working in this repo

- Server changes: run the test suite before calling anything done —
  `cd mem0-server && MEM0_KEY=$(cat ~/.mem0/api-key) MEM0_URL=http://127.0.0.1:18791 .venv-or-system-python -m pytest -q`
  (any Python 3.12+ with `httpx` + `pytest` works for the suite).
- Installer changes: `mem0-server/tests/test_config_import_closure.py` must stay green —
  it guards against the class of bug where a new server module is missing from
  `install/1-wsl-services.sh` `MEM0_MODULES` and fresh installs crash-loop.
- Windows-side changes: Pester suites under `scripts/windows/tests/`.
