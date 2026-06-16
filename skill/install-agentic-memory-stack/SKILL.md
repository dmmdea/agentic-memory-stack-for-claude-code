---
name: install-agentic-memory-stack
description: Use when the user wants to install, reinstall, or rebuild the agentic memory stack for Claude Code — multi-tier persistent memory (mem0 + Qdrant + EmbeddingGemma on llama-swap + Codex-driven extraction), with an episodic/goals/open-questions sidecar and a DPAPI-isolated canonical key, on a Windows + WSL2 machine. Triggers on phrases like "install agentic memory", "set up the memory stack", "reinstall after format", "rebuild memory backend", "set up mem0 + qdrant + codex".
disable-model-invocation: true
---

# Install the Agentic Memory Stack

Installs a persistent, multi-tier memory backend for Claude Code on a **Windows + WSL2** machine. Safe on a clean machine, a partially-installed machine, or as an upgrade — the installer is **idempotent and operator-agnostic** (it detects your WSL distro, derives every path from your own home/username, and never hardcodes the developer's machine).

> This is an **external infrastructure stack you install once**, not a Claude Code feature. It runs as WSL systemd services + Windows Claude Code hooks. The 4-phase installer (`PREREQS → WSL SERVICES → WINDOWS CONFIG → VERIFY`) is in `install/`; `install.ps1` orchestrates them.

## When to invoke

User says any of: "install agentic memory stack", "set up the memory stack on this PC", "reinstall after format", "rebuild memory backend", "set up mem0 + qdrant + codex". This installer is destructive (registers hooks, writes services) — it runs only on explicit request, never auto-fired.

## What it installs

The runtime is **mem0-server + Qdrant + llama-swap (EmbeddingGemma embedder + bge-reranker) + Codex CLI**, plus the episodic/goals/open-questions sidecar (same mem0-server process) and a DPAPI-isolated canonical-key credential. On disk:

- **WSL systemd-user services** (`1-wsl-services.sh`): `qdrant.service`, `mem0.service` (with the DPAPI key chain + the `MEM0_DEFAULT_USER_ID`/`MEM0_RAW_FALLBACK_ENABLED`/`MEM0_DURABLE_FRESHNESS_ENABLED` env), and timers `l10-audit`, `decay-scan` (Sun 02:00), `stack-backup` (Sun 03:30), `goals-stale-sweep` + `contradiction-sweep` (Sun 04:00/05:00, report-safe), `episodic-reconcile`. The `egemma-rollback-prune` units ship disabled.
- **Embedder model**: `embeddinggemma-300M-Q8_0.gguf` fetched to `~/models/` (multilingual EN/ES, CPU on llama-swap `:11436`). **No Ollama** (decommissioned v0.22).
- **Credential files** at `~/.mem0/`: `api-key`, `canonical-key.dpapi` (the plaintext key is removed in production), plus the operator receipt `stack.env`.
- **Windows runtime scripts** at `~/.claude/scripts/`: the hook chain (`mem0-hook-client.exe` + `mem0-hook-daemon.ps1` + `-spawn` + `.cs` + `build-hook-client.ps1`), `user-prompt-extract.ps1` + `user-prompt-lib.ps1`, `stop-extract.ps1`, `pre-tool-check.ps1`, `l1a-extract.ps1`, `dream-consolidate.ps1`, `memory-common.ps1`, `Test-MemoryStack.ps1`, `codex-shim.ps1` + `-spawn`, `mem0-mcp-shim.py`, `storage-cap-check.sh`, `model-tiers.json`, and the **operator receipt** `mem0-stack.config.psd1`.
- **Claude Code hooks** (in `~/.claude/settings.json`): `Stop`/`PreCompact` → `stop-extract.ps1` (Codex L1a extraction); `SessionStart` → storage-caps banner + daemon pre-warm + (flag-gated) codex-shim pre-warm; `UserPromptSubmit` → `mem0-hook-client.exe` (injects `[MEMORY CONTEXT]` via the resident daemon, fails open to the inline extractor); `PreToolUse` → `pre-tool-check.ps1`.
- **MCP server** (in `~/.claude.json`): `mem0` (stdio shim — memory + episodic + goals + open-questions tools).
- **Windows Task Scheduler**: `ClaudeCode-DreamConsolidator-3am` (nightly consolidation, `-WakeToRun`).
- **CLAUDE.md patch**: the "Memory tier protocol" section (how Claude interprets evidence/insight/canonical tiers).

See `references/architecture.md` for the full data-flow + the v1.0 faithfulness features (R1–R6).

## Prerequisites (phase 0 verifies all of these and halts with exact fixes)

1. Windows 10+/11, PowerShell 5.1+ (the installer runs under 5.1 or 7).
2. WSL2 with a Linux distro, **`mirrored` networking** (`[wsl2]\nnetworkingMode=mirrored` in `%USERPROFILE%\.wslconfig`) and **systemd enabled** (`[boot]\nsystemd=true` in `/etc/wsl.conf`).
3. In WSL: Python 3.12+, Node 22+, curl, and **llama-swap** on `:11436` (single local inference stack; a llama.cpp build ≥ b6384 for the gemma-embedding arch). No Ollama.
4. **Claude Code** installed + authenticated (`npm i -g @anthropic-ai/claude-code`, then `claude /login`).
5. **Codex CLI** authenticated against a ChatGPT subscription (`npm i -g @openai/codex`, then `codex login` → "Sign in with ChatGPT"). Codex is the subagent LLM for extraction + nightly consolidation — the installer refuses to proceed without it.
6. `git` for Windows.

> The .NET Framework C# compiler (`csc.exe`, ships with every Win10/11) builds `mem0-hook-client.exe`. No extra toolchain.

## How to install

The installer is **self-contained** and **operator-agnostic**: it detects your WSL distro and derives every path from your own username/home — nothing is hardcoded to any developer's machine.

**If you already have this repository locally** (most common — it's how you got this skill), just run the installer from the repo root in Windows PowerShell (not WSL bash):

```powershell
cd <path-to-this-repo>
.\install.ps1                            # auto-detects your default WSL distro
.\install.ps1 -Distro <your-distro>      # multi-distro / non-default; see: wsl -l -q
```

**Fresh machine (don't have it yet)** — clone this repo first (its URL is on its GitHub page), then run the same:

```powershell
git clone https://github.com/dmmdea/agentic-memory-stack-for-claude-code $HOME\agentic-memory-stack
cd $HOME\agentic-memory-stack
.\install.ps1
```

Or via the skills CLI (always `--copy` — the default symlink breaks on Windows): `npx skills add dmmdea/agentic-memory-stack-for-claude-code --copy -a claude-code -g`.

> First install takes ~5–10 min (mostly the one-time ~334 MB EmbeddingGemma fetch + the mem0 venv).

**Idempotent re-run / after-format / new PC:** the same command. Re-running skips already-installed components, refreshes source + the compiled exe (smoke-gated before any hook registration), re-writes the operator receipt, and re-verifies. The repo is the single source of truth — nothing from the old machine is needed beyond the prerequisites.

> Your memory **data** (mem0 SQLite, Qdrant, episodic.db) is NOT in the repo, and the DPAPI canonical-key blob is **not portable** (it's bound to the original Windows user's DPAPI chain). On a new PC you provision a fresh key (see `references/troubleshooting.md` → Canonical-key). Data-backup guidance: `docs/data-backup.md`.

## One manual step: the llama-swap model entries

The installer fetches the EmbeddingGemma GGUF and verifies the `:11436` embed endpoint, but it does **not** rewrite your llama-swap config (external user config). Ensure two models are in your `always_loaded` group, bound to `127.0.0.1`:

1. **`embeddinggemma`** — mem0's embedder. `--embeddings --pooling mean --n-gpu-layers 0 --ctx-size 2048` (2048 is the trained limit — do not raise it). Without it, every add/search embed fails.
2. **`bge-reranker-v2-m3`** — search reranker, `RERANK_DOC_MAX_CHARS=6000`.

The exact YAML stanzas + verification curls are in `references/troubleshooting.md` → llama-swap.

## After install

1. **Restart VS Code / Claude Code** so the new hooks + MCP server load (the installer prints this reminder).
2. In a new session, confirm the MCP tools appear: `mcp__mem0__memory_search`, `memory_add`, `memory_promote`/`demote`, plus the episodic / goals / open-question tools.
3. Confirm `bge-reranker-v2-m3` is in `always_loaded` (the one manual step above).
4. Work normally. L1a extraction fires on Stop/PreCompact (10-min throttle); `[MEMORY CONTEXT]` is injected before each prompt; the dream consolidator runs nightly at 3 AM.

## Verify

```powershell
& $HOME\agentic-memory-stack\scripts\windows\Test-MemoryStack.ps1
```

Expected: **`Memory stack: HEALTHY (3/3 dimensions GREEN)`** — LIVENESS (services + models reachable), INVARIANTS (canonical key loaded `source=runtime`, admission gate, loopback binds, `deployed hooks freshness` SHA-match), RECOVERY (timers, backup/restore drill, episodic/goals, Task Scheduler 3am). A keyless server or a drifted deployed script shows as WARN/FAIL — never silently.

## Promoting a memory to `tier=canonical`

The CLI is the only supported path (agentic Claude cannot canonize via MCP — it has no access to the signing credential):

```bash
# In WSL, from the repo:
bash scripts/wsl/mem0-canonize.sh <memory_id> "<reason>"
```

The DPAPI canonical-key backend, provisioning a new box, recovery, rollback, and the full troubleshooting matrix are in **`references/troubleshooting.md`**.

## What this skill does NOT do

- Does not install the prerequisites (WSL, Python, Node, llama-swap, Claude Code, Codex) — install those first; the installer is a stack-assembler, not a system bootstrapper.
- Does not back up existing memory data (see `docs/data-backup.md`).
- Does not configure llama-swap's model entries (the one manual step above).
- Does not migrate the DPAPI canonical-key across machines (provision a fresh key on the new box).

## Implementation

- `install/0-prereqs.ps1` — fail-fast prereq checker (`-Distro` aware).
- `install/1-wsl-services.sh <wsl-user> <win-user> <distro>` — Qdrant + mem0 server (all `MEM0_MODULES`) + the EmbeddingGemma GGUF + systemd units + keys + the WSL receipt `~/.mem0/stack.env`.
- `install/2-windows-config.ps1 -WslUser <u> -Distro <d>` — deploys runtime scripts (sentinels resolved to your values), writes the operator receipt, builds + smoke-gates `mem0-hook-client.exe` before registering any hook, registers hooks + MCP + Task Scheduler, patches CLAUDE.md.
- `install/3-verify.ps1 -WslUser <u> -Distro <d>` — end-to-end smoke test.
- `install.ps1` orchestrates all four, resolving your distro + WSL user up front.

See also `CLAUDE.md` (tier protocol), `ARCHITECTURE.md` (data flow), `references/`, and `CHANGELOG.md`.
