---
name: install-agentic-memory-stack
description: Use when the user wants to install, reinstall, or rebuild the agentic memory stack for Claude Code — multi-tier persistent memory (mem0 + Qdrant + EmbeddingGemma on llama-swap + Codex-driven extraction), with an episodic/goals/open-questions sidecar and a DPAPI-isolated canonical key, on a Windows + WSL2 machine (or as a replica of a Linux authority). Triggers on phrases like "install agentic memory", "set up the memory stack", "reinstall after format", "rebuild memory backend", "set up mem0 + qdrant + codex".
disable-model-invocation: true
---

# Install the Agentic Memory Stack

Installs a persistent, multi-tier memory backend for Claude Code on a **Windows + WSL2** machine: either self-contained (the default role, the PC is its own authority, no network needed) or as a **replica** of a native-Linux authority. Safe on a clean machine, a partially-installed machine, or as an upgrade — the installer is **idempotent and operator-agnostic** (it detects your WSL distro, derives every path from your own home/username, and never hardcodes the developer's machine). The native-Linux authority and the Linux client have their own installers; the repository `README.md` ("Choose your topology") picks between them and gives the credential, tenant and API-key steps.

> This is an **external infrastructure stack you install once**, not a Claude Code feature. It runs as WSL systemd services + Windows Claude Code hooks. The 4-phase installer (`PREREQS → WSL SERVICES → WINDOWS CONFIG → VERIFY`) is in `install/`; `install.ps1` orchestrates them.

## When to invoke

User says any of: "install agentic memory stack", "set up the memory stack on this PC", "reinstall after format", "rebuild memory backend", "set up mem0 + qdrant + codex". This installer is destructive (registers hooks, writes services) — it runs only on explicit request, never auto-fired.

## What it installs

The runtime is **mem0-server + Qdrant + llama-swap (EmbeddingGemma embedder + bge-reranker) + Codex CLI**, plus the episodic/goals/open-questions sidecar (same mem0-server process) and a DPAPI-isolated canonical-key credential. On disk:

- **WSL systemd-user services** (`1-wsl-services.sh`): `qdrant.service`, `mem0.service` (with the DPAPI key chain + the `MEM0_DEFAULT_USER_ID`/`MEM0_RAW_FALLBACK_ENABLED`/`MEM0_DURABLE_FRESHNESS_ENABLED` env), and timers `l10-audit`, `decay-scan` (Sun 02:00), `stack-backup` (Sun 03:30), `goals-stale-sweep` + `contradiction-sweep` (Sun 04:00/05:00; the sweep abandons only auto-created goals idle 90 days and, since 1.37.0, open questions untouched 30 days), `episodic-reconcile`, `retrieval-pairs` (Sun 07:00) and `goal-recurrence-promote` (daily 04:45). The `egemma-rollback-prune` units ship disabled.
- **Embedder models**, fetched to `~/models/`: `embeddinggemma-2-Q8_0.gguf` + `mmproj-embeddinggemma-2-Q8_0.gguf` (the default `egemma2` profile of a fresh install, sha256-checked) and the legacy `embeddinggemma-300M-Q8_0.gguf`. Served by llama-swap on `:11436`. **No Ollama.**
- **Credential files** at `~/.mem0/`: `api-key`, `canonical-key.dpapi` (the plaintext key is removed on boxes where the per-box DPAPI cutover has been run; plaintext mode-600 otherwise), plus the operator receipt `stack.env`.
- **Windows runtime scripts** at `~/.claude/scripts/`: the hook chain (`mem0-hook-client.exe` + `mem0-hook-daemon.ps1` + `-spawn` + `.cs` + `build-hook-client.ps1`), `user-prompt-extract.ps1` + `user-prompt-lib.ps1`, `stop-extract.ps1`, `l1a-extract.ps1`, `dream-consolidate.ps1`, `memory-common.ps1`, `Test-MemoryStack.ps1`, `codex-shim.ps1` + `-spawn`, `mem0-mcp-shim.py`, `storage-cap-check.sh`, `model-tiers.json`, and the **operator receipt** `mem0-stack.config.psd1`.
- **Claude Code hooks** (in `~/.claude/settings.json`): `Stop`/`PreCompact` → `stop-extract.ps1` (Codex L1a extraction); `SessionStart` → storage-caps banner + daemon pre-warm + codex-shim pre-warm (opt-out via `~/.claude/state/codex-shim.disabled`); `UserPromptSubmit` → `mem0-hook-client.exe` (injects `[MEMORY CONTEXT]` via the resident daemon, fails open to the inline extractor). The stack registers no `PreToolUse` hook.
- **MCP server** (in `~/.claude.json`): `mem0` (stdio shim — memory + episodic + goals + open-questions tools).
- **Windows Task Scheduler**: on a brain, `ClaudeCode-DreamConsolidator-3am` (nightly consolidation, `-WakeToRun`) and `ClaudeCode-SemanticDedup-430am`; on a replica, `mem0-offline-watcher` and `mem0-replica-cache-refresh` instead.
- **CLAUDE.md patch**: the "Memory tier protocol" section (how Claude interprets evidence/insight/canonical tiers).

See `references/architecture.md` for the full data-flow + the v1.0 faithfulness features (R1–R6).

## Prerequisites (phase 0 verifies all of these and halts with exact fixes)

1. Windows 10+/11 with PowerShell 7 (`pwsh`) — install phases 2–3 are `#Requires -Version 7`; under Windows PowerShell 5.1 they refuse with a clear error. (The deployed runtime hooks still run under the built-in 5.1.)
2. WSL2 with a Linux distro, **`mirrored` networking** (`[wsl2]\nnetworkingMode=mirrored` in `%USERPROFILE%\.wslconfig`) and **systemd enabled** (`[boot]\nsystemd=true` in `/etc/wsl.conf`).
3. In WSL: Python 3.12+ (Ubuntu 24.04 has it; 22.04's `python3` is 3.10 and fails the check), Node 22+, curl, jq, and **llama-swap** on `:11436` on a GPU, serving the embedder of the default `egemma2` profile (EmbeddingGemma-2, needs a llama.cpp build ≥ **b11452**) and the reranker. Step by step: `install/llama-swap-setup.md`. No Ollama.
4. **Claude Code** installed (`npm i -g @anthropic-ai/claude-code`). A subscription login (`claude /login`) is reported, not required.
5. **Codex CLI** from npm, so the shim lands at `%USERPROFILE%\AppData\Roaming\npm\codex.cmd` (`npm i -g @openai/codex`), then `codex login` → "Sign in with ChatGPT". An API-key login is not accepted. Codex is the subagent LLM for extraction + nightly consolidation — the installer refuses to proceed without it.
6. `git` 2.38+ for Windows.
7. For a **replica** only: a native-Linux authority already installed, a private network that reaches it, and a file holding its API key (`README.md`, section 3b).

> The .NET Framework C# compiler (`csc.exe`, ships with every Win10/11) builds `mem0-hook-client.exe`. No extra toolchain.

## How to install

The installer is **self-contained** and **operator-agnostic**: it detects your WSL distro and derives every path from your own username/home — nothing is hardcoded to any developer's machine.

**If you already have this repository locally**, run the installer from the repo root in PowerShell 7 (`pwsh`; not Windows PowerShell 5.1, not WSL bash):

```powershell
cd <path-to-this-repo>
pwsh -File .\install.ps1                            # auto-detects your default WSL distro; role brain (self-contained)
pwsh -File .\install.ps1 -Distro <your-distro>      # multi-distro / non-default; see: wsl -l -q
pwsh -File .\install.ps1 -Role replica -AuthorityUrl http://<authority-host>:18791 -ApiKeyFile <key-file>   # a replica of a Linux authority
```

A zip download carries the internet-zone mark: run `Get-ChildItem -Recurse | Unblock-File` first, or start with `pwsh -ExecutionPolicy Bypass -File .\install.ps1`. The authority's `--user-id` must equal this PC's WSL user name, or the PC reads and writes a separate, empty memory.

**Fresh machine (don't have it yet)** — clone this repo first, then run the same:

```powershell
git clone https://github.com/dmmdea/agentic-memory-stack-for-claude-code $HOME\agentic-memory-stack
cd $HOME\agentic-memory-stack
pwsh -File .\install.ps1
```

The skills CLI (`npx skills add dmmdea/agentic-memory-stack-for-claude-code --copy -a claude-code -g`; always `--copy`, the default symlink breaks on Windows) installs only this skill folder, which guides the install. It does not contain `install.ps1`, `install/` or `scripts/`: clone the repository as above to run the installer.

> First install takes ~5–10 min (mostly the one-time model fetches, about 1 GB, plus the mem0 venv). If verify fails, the installer prints a red `INSTALL INCOMPLETE`, lists the failed checks with their fixes and exits 1; fix them and re-run.

**Idempotent re-run / after-format / new PC:** the same command. Re-running skips already-installed components, refreshes source + the compiled exe (smoke-gated before any hook registration), re-writes the operator receipt, and re-verifies. The repo is the single source of truth — nothing from the old machine is needed beyond the prerequisites.

> Your memory **data** (mem0 SQLite, Qdrant, episodic.db) is NOT in the repo, and the DPAPI canonical-key blob is **not portable** (it's bound to the original Windows user's DPAPI chain). On a new PC you provision a fresh key (see `references/troubleshooting.md` → Canonical-key). Data-backup guidance: `docs/data-backup.md`.

### Linux thin client (a box with no store of its own)

For a native-Linux machine that should use an existing authority over the network, run `bash install/linux-client.sh --authority http://<authority-host>:18791 --api-key-file <file> --user-id <tenant>` from a checkout instead of the 4-phase installer. It installs the MCP shim + outbox replay (and, when `pwsh` 7 and a logged-in `codex` are present, the Stop/PreCompact/SessionStart capture hooks; there is no per-prompt injection on Linux) and ends with a real `memory_health` call through the shim. `--dry-run` prints the plan. For a Linux **replica** (dormant local copy for offline reads, refreshed daily from the authority over SSH) run `bash install/linux-replica.sh --authority ... --brain-ssh <alias> [--brain-wsl <distro>:<user>]` instead; it needs the profile's embedder on `:11436` locally and ends with a real restore. The authority itself is `bash install/linux-authority.sh` (`README.md`, section 3b).

## One manual step: the llama-swap model entries

The installer fetches the EmbeddingGemma GGUFs and verifies the `:11436` embed endpoint, but it does **not** rewrite your llama-swap config (external user config), and phase 0 fails until `:11436` answers. Set it up first, following `install/llama-swap-setup.md`. Ensure two models are in a non-exclusive, non-swapping `support` group (`swap: false`, `exclusive: false`), each with `ttl: 300` (every model unloads when idle; none is kept resident), bound to `127.0.0.1`:

1. **`embeddinggemma2`** — mem0's embedder on a fresh install: EmbeddingGemma-2 with its `--mmproj` projector, `--ctx-size 4096 --ubatch-size 2048`, every layer on the GPU, llama.cpp ≥ b11452. Without it every add/search embed fails and verify ends red. A box already on the legacy profile serves `embeddinggemma` (the 300m model) instead.
2. **`bge-reranker-v2-m3`** — search reranker, `RERANK_DOC_MAX_CHARS=6000`.

The exact YAML stanzas, downloads with checksums and verification curls are in `install/llama-swap-setup.md`; `references/troubleshooting.md` repeats the entries.

## After install

1. **Restart VS Code / Claude Code** so the new hooks + MCP server load (the installer prints this reminder).
2. In a new session, confirm the MCP tools appear: `mcp__mem0__memory_search`, `memory_add`, `memory_promote`/`demote`, plus the episodic / goals / open-question tools.
3. Confirm `bge-reranker-v2-m3` and the embedder are in the `support` group with `ttl: 300` (the one manual step above).
4. Work normally. L1a extraction fires on Stop/PreCompact (10-min throttle); `[MEMORY CONTEXT]` is injected before each prompt; the dream consolidator runs nightly at 3 AM.

## Verify

```powershell
& "$env:USERPROFILE\.claude\scripts\Test-MemoryStack.ps1"   # the deployed copy; works from any checkout location
```

`install\3-verify.ps1` (run by the installer) must end `ALL VERIFY CHECKS PASSED.` On an authority, `curl http://<authority-host>:18791/health/deep` should show `"ok": true`; to remove the stack see the `README.md` "Uninstall / roll back" section.

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
- Does not create the native-Linux authority's key credentials; see `README.md` section 3b.
- Does not migrate the DPAPI canonical-key across machines (provision a fresh key on the new box).

## Implementation

- `install/0-prereqs.ps1` — fail-fast prereq checker (`-Distro` aware).
- `install/1-wsl-services.sh <wsl-user> <win-user> <distro>` — Qdrant + mem0 server (all `MEM0_MODULES`) + the EmbeddingGemma GGUF + systemd units + keys + the WSL receipt `~/.mem0/stack.env`.
- `install/2-windows-config.ps1 -WslUser <u> -Distro <d>` — deploys runtime scripts (sentinels resolved to your values), writes the operator receipt, builds + smoke-gates `mem0-hook-client.exe` before registering any hook, registers hooks + MCP + Task Scheduler, patches CLAUDE.md.
- `install/3-verify.ps1 -WslUser <u> -Distro <d>` — end-to-end smoke test.
- `install.ps1` orchestrates all four, resolving your distro + WSL user up front.

See also `CLAUDE.md` (tier protocol), `ARCHITECTURE.md` (data flow), `references/`, and `CHANGELOG.md`.
