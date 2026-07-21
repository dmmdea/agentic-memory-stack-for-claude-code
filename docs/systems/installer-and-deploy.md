# Installer and deploy

## Purpose

This system is how the memory stack gets *onto* a machine and how code changes reach the *running* runtime afterward. Two distinct paths live here:

- **Install** (`install.ps1` and its four phases) — the one-time, operator-facing bring-up that provisions WSL services, deploys Windows-side hook scripts, registers Claude Code hooks and scheduled tasks, and verifies the result.
- **Deploy** (`scripts/wsl/deploy.sh`) — the single, ongoing path that pushes updated server modules, maintenance scripts, and systemd units from the repository into the live WSL runtime, gated so a broken change never reaches a restart.

Both exist to solve the same underlying hazard: production spans multiple roots (a WSL app directory, systemd timers, a Windows `~/.claude/scripts` deploy layer) that can silently drift apart. The install path builds them consistently; the deploy path keeps them in sync through one gated pipeline; and a set of parity/skew checks make any remaining drift *visible* rather than silent.

## Questions this doc answers

- What does `install.ps1` do, in what order, and is it safe to re-run after a failure?
- What gets installed on the WSL side versus the Windows side?
- What is the install **Receipt**, what fields does it carry, and who reads it?
- How do the operator-neutral **Sentinel** placeholders in the shipped source become real values on a machine?
- What is the **One-Brain Rule** role gate, and what does a `replica` box skip?
- How does `deploy.sh` push a change safely, and what stops a bad change from restarting the service?
- What is **R9 Parity** and how do the skew/parity checks catch a drifted deploy?

## Scope

- The top-level orchestrator `install.ps1` and the four phase scripts (`0-prereqs`, `1-wsl-services`, `2-windows-config`, `3-verify`).
- The install Receipt, Sentinel resolution, distro-agnostic hook emission, the `brands.json` fallback, and the `brain`/`replica` role gate.
- The single deploy pipeline (`deploy.sh`) and its rsync → units → import-smoke → restart → health-gate order.
- The verification-time skew guard and the R9 parity check that detect repo-vs-deployed drift.

## Non-scope

- **What the deployed components *do*.** The mem0 server API, tier policy, the nightly consolidation ("Dream"), the hook pipeline, and DPAPI key custody are documented in their own system docs — this doc covers only how they are *placed and updated*.
- **Building llama.cpp + llama-swap.** That is the one prerequisite the installer cannot auto-satisfy; the guide is [`../../install/llama-swap-setup.md`](../../install/llama-swap-setup.md).
- **Offline/travel failover** (authority/replica failover, the Outbox, restore scripts) — a separate concern from the install/deploy machinery.
- **Data backup and machine migration** — moving the memory *data* is a different path from installing the *code*.

## Key concepts

- **Receipt** — the install-time file (`~/.claude/scripts/mem0-stack.config.psd1`) recording the operator's deploy choices, read at runtime by scripts that must resolve operator-specific values without hardcoding them.
- **Sentinel** — an operator-neutral placeholder token (`__WSL_USER__`, `__WIN_USER__`, `__WSL_DISTRO__`, `__MEM0_BIND__`, `__REPO_ROOT_WSL__`) shipped in the repository's scripts and systemd units, substituted for real values at deploy time.
- **Brain** / **Replica** — the brain box holds sole write authority over the store; a replica box consumes it read-only.
- **One-Brain Rule** — exactly one machine runs the nightly canonical-mutation tasks; enforced at install by the role gate.
- **R9 Parity** — the SHA256 repo-vs-deployed drift check for every hot-path deployed script and config.

These terms are defined in [`../glossary.md`](../glossary.md); this doc is their authoritative "how it works" source.

## How the system works

### The install orchestrator

`install.ps1` runs as the normal interactive Windows user (no elevation) and **requires PowerShell 7+ (pwsh)** — it throws immediately on Windows PowerShell 5.1, because `2-windows-config.ps1` does not even *parse* under 5.1 (BOM-less UTF-8 with em-dashes decodes as ANSI and breaks quote tracking). The only step that needs an elevated shell is the one-time `wsl --install`, which is a documented prerequisite, not part of `install.ps1`.

It resolves the **WSL distro** by auto-detecting the default distro (`wsl -l -q`, first entry) unless `-Distro` is passed, then runs four phases in strict order, aborting on any hard failure:

1. **`[0/4]` Prerequisites** — `install/0-prereqs.ps1`. Fails fast if anything required is missing.
2. **`[1/4]` WSL services** — `install/1-wsl-services.sh`, invoked over `wsl.exe` with the WSL user, Windows user, and distro passed as arguments.
3. **`[2/4]` Windows config** — `install/2-windows-config.ps1 -WslUser -Distro -Role`.
4. **`[3/4]` Verify** — `install/3-verify.ps1`. A verify failure is a warning, not a hard abort (the stack may still be partially functional).

**Idempotency.** Every phase is safe to re-run: each expensive or external step is guarded by an existence/post-state check (the Qdrant binary, the mem0 venv, the embedder GGUF, the keys, the systemd units, and the hook registrations all skip when already done). So **re-running after a failure resumes safely** — completed steps are skipped and only the incomplete tail runs. The WSL phase's failure trap prints the failing line and reminds that a re-run is safe.

### WSL side (phase 1)

`1-wsl-services.sh` provisions the Linux half:

- **Qdrant** binary + a config that pins a **loopback bind** (`127.0.0.1:6333`), rewritten on every run so an old install can't stay LAN-exposed.
- **mem0 server** under `~/apps/mem0-server/` with its venv. The full **`MEM0_MODULES` import closure** — every module `app.py` imports — is copied so a fresh install never crash-loops on a missing module; security floors for transitive deps are enforced and post-condition-asserted.
- The **DPAPI key-fetch script**, the mem0 **API key**, and the **canonical-key** (generated only when neither a plaintext key nor a DPAPI blob already exists).
- The **WSL-side receipt** `~/.mem0/stack.env` (`MEM0_WSL_USER`, `MEM0_WIN_USER`, `MEM0_DISTRO`, `MEM0_REPO_ROOT_WSL`, `MEM0_BIND`), which `deploy.sh` later sources.
- The **EmbeddingGemma-300m** embedder GGUF staged to a stable path (the llama-swap model entry itself is an out-of-repo manual step).
- **systemd-user units** (`mem0`, `qdrant`, `l10-audit`, `decay-scan`, `stack-backup`, and the hygiene sweep timers), with the same Sentinel substitution the Windows side uses, plus maintenance scripts deployed to `~/apps/mem0-scripts/` so timers exec *deployed* copies rather than a dev working tree.

### Windows side (phase 2)

`2-windows-config.ps1` is where the operator-neutral machinery concentrates.

**The Receipt.** It writes `~/.claude/scripts/mem0-stack.config.psd1`, the single source of truth for the operator-specific dimensions. Fields:

| Field | Meaning |
|---|---|
| `WslUser`, `WinUser`, `Distro` | the three identity dimensions |
| `Role` | `brain` or `replica` (the One-Brain Rule role) |
| `RepoRootWin`, `RepoRootWsl` | the operator-chosen repository path, both views |
| `EvalRootWsl` | optional checkout carrying the `eval/` harnesses; empty by default (the orchestrator does not pass it), and the Dream's drift canary falls back to `RepoRootWsl` when it is empty |
| `ApiKeyUnc` | UNC path to the WSL-side API key |
| `AuthorityUrl` | the memory authority this box talks to, mirrored into `~/.mem0/authority-url` (which is what the shim actually reads) |
| `PromotionGateMode` | ships `shadow` |

**The memory authority (`-AuthorityUrl`).** Phase 2 also writes `~/.mem0/authority-url` **inside WSL** — the per-host file the MCP shim, `replay-ops.py`, and the SessionStart bundle resolve their authority from (`MEM0_URL` env → this file → loopback) — plus `~/.mem0/role`, so WSL-side code can enforce the One-Brain Rule without reading back into the Windows receipt.

It is a *file* and not an environment variable for a concrete reason: the mem0 MCP entry launches the shim as `wsl.exe -d <distro> -e <python> <shim>`, which execs the binary directly — no login shell, no `WSLENV` pass-through — so a `MEM0_URL` set on the Windows side never reaches the shim process. Before this, a replica silently fell back to loopback, found no local server, and returned `QUEUED_OFFLINE` on every write while the Outbox filled up unnoticed.

Three rules keep that failure from coming back by another door:

| Rule | Behaviour |
|---|---|
| **Omitting the flag inherits** | `-AuthorityUrl` defaults to *empty*, not loopback. With no value the installer takes the address already on the box (`~/.mem0/authority-url`, else the previous receipt); only a first install with no prior state falls back to loopback. A plain re-run therefore cannot silently revert a replica — the regression this whole mechanism exists to prevent. |
| **A replica may not point at itself** | Installing `-Role replica` against a loopback authority is **refused**. Its Outbox would replay into the box's own disposable store and be ledgered as delivered, so the writes are lost on teardown with nothing reporting it. `3-verify.ps1` asserts the same thing, because a live local mem0 answers `/health` and would otherwise pass every reachability check. |
| **The value is whitelisted, not escaped** | It reaches a shell, so anything outside `scheme://host[:port][/path]` is rejected at resolution time rather than quoted and hoped for. |

R9-tracked deployed scripts (for example `Test-MemoryStack.ps1` and `dream-consolidate.ps1`) read this at runtime instead of hardcoding any developer path or handle.

**Sentinel resolution.** Deployed scripts and units ship carrying `__WSL_USER__` / `__WIN_USER__` / `__WSL_DISTRO__` tokens. During deploy, `Resolve-StackTokens` performs a *literal* (non-regex) replace of each token with the install value; byte-identical scripts that carry no token are copied verbatim, which keeps R9 parity honest.

**Distro-agnostic hook emission.** The hook commands written into `settings.json` omit `-d <distro>` **when the stack's distro is the WSL default** — a bare `wsl.exe` reaches the default distro, so the emitted `settings.json` stays portable across machines whose default distros differ. Only a *non-default* AMS distro gets an explicit `-d`, at the cost of a machine-specific `settings.json` (the installer warns when it does this).

**The `brands.json` fallback.** The operator's private brand map (`claude-config/brands.json`) is gitignored, so a fresh clone has only the tracked `brands.example.json` template. The installer deploys the local `brands.json` if present, else the example template — and only when no deployed `brands.json` already exists, so a customized map survives re-installs.

**The One-Brain Rule role gate.** With `-Role brain` (default) the installer registers the two nightly **canonical-mutation** scheduled tasks. With `-Role replica` it registers *neither* and additionally **removes any previously-registered ones** — because consolidation and dedup mutate the one shared brain and there is no cross-machine lock. (See *Important flows* for the task names.)

### Verify (phase 3)

`3-verify.ps1` runs an end-to-end smoke test and adds two structural guards beyond the service probes (see *Important flows*): the **skew guard** and the **role-aware task checks**.

### The deploy pipeline

`deploy.sh` is the single path from repo to live runtime. It sources `~/.mem0/stack.env` for the sentinel values (`WSL_USER` is always `$USER`) and runs the pipeline in a fixed order with a hard gate before any restart (see *Important flows*).

## Important flows

### Install run (orchestrated)

`install.ps1` → phase 0 (prereqs) → phase 1 (WSL services over `wsl.exe`) → phase 2 (Windows config: receipt, sentinel resolution, hooks, MCP registration, scheduled tasks) → phase 3 (verify). Any hard failure in phases 0–2 aborts with an actionable message; a re-run resumes.

### Deploy pipeline (ordered, gated)

1. **rsync server modules** (`*.py` + `requirements.txt`, plus the DPAPI key script and the `VERSION` stamp) → `~/apps/mem0-server/`. Never deletes runtime extras; `tests/` is deliberately not deployed.
2. **rsync maintenance scripts** (`*.py`, `*.sh`) → `~/apps/mem0-scripts/`.
3. **install sentinel-resolved systemd units** → `~/.config/systemd/user/`. The **`__MEM0_BIND__`** sentinel resolves from **`MEM0_BIND`** (sourced from `~/.mem0/stack.env`, default loopback `127.0.0.1`); the same `sed` substitution the installer uses handles `__WSL_USER__`, `__WIN_USER__`, `__WSL_DISTRO__`, `__REPO_ROOT_WSL__`.
4. **import-smoke** the server in its live venv (`python -c "import app"`). **On failure it refuses to restart** — the runtime keeps running the previous code and the deploy exits non-zero. This is the gate that closes the "module hand-copied but never wired" defect class.
5. **restart + health gate.** Restart `mem0.service`, poll `/health` until ready (30s ceiling), then assert **`/health/deep`** returns `ok:true` (printing each sub-check). A failed health gate exits non-zero.

`--dry-run` prints what would change (including changed units) and writes nothing.

### Skew guard (verify time)

`3-verify.ps1` extracts *every* `~/.claude/scripts/<file>` path referenced anywhere in `settings.json` and asserts each file exists on disk. This is deliberately broader than just hook commands — it catches the class where a shared/synced `settings.json` advances ahead of a machine's local deploy layer (a missing PreCompact capture script once deadlocked live sessions). Over-detection is the safe direction.

### Role-aware task checks (verify time)

Verify reads `Role` from the Receipt. On a `brain` box it asserts **both** nightly tasks are *registered*; on a `replica` box it asserts both are *absent*. The two tasks are:

- `ClaudeCode-DreamConsolidator-3am` — the nightly 4-phase consolidator.
- `ClaudeCode-SemanticDedup-430am` — the tier-sensitive semantic dedup (offset from the 3am run so the per-machine `dedup.lock` never blocks the Dream).

### R9 Parity (health-check time)

`Test-MemoryStack.ps1` check **R9** ("deployed hooks freshness") SHA256-compares the repo source against the deployed copy of every hot-path hook script and tracked config (plus a `.sha256` sidecar for the compiled hook client, which has no committed binary to hash). Because deployed scripts carry Sentinels, R9 first normalizes the repo text with the same receipt-driven substitution before hashing, so a legitimate substitution is not mistaken for drift. Any real mismatch or missing file is a **WARN** naming the offender and the redeploy command. R9 is the safety net that makes a drifted or partial deploy visible.

## Data and state

- **Receipt (Windows):** `~/.claude/scripts/mem0-stack.config.psd1` — operator dimensions + role, read at runtime.
- **Receipt (WSL):** `~/.mem0/stack.env` — mirrors the identity/distro/repo/bind dimensions for the bash side and `deploy.sh`.
- **Keys:** `~/.mem0/api-key` (mode 600) and `~/.mem0/canonical-key` (or its `.dpapi` blob).
- **Deployed runtime roots:** `~/apps/mem0-server/` (server + venv + `VERSION` stamp), `~/apps/mem0-scripts/` (maintenance scripts timers exec), `~/.config/systemd/user/` (units), `~/.claude/scripts/` (Windows hook deploy layer).
- **Claude Code config touched by install:** `~/.claude/settings.json` (hooks + `ENABLE_TOOL_SEARCH`), `~/.claude.json` (mem0 MCP server), `~/.claude/CLAUDE.md` (memory-tier protocol snippet, appended once). Each is backed up before modification.
- **Scheduled tasks (brain only):** `ClaudeCode-DreamConsolidator-3am`, `ClaudeCode-SemanticDedup-430am`.

## Interfaces and entry points

- `install.ps1 [-NonInteractive] [-LogFile <path>] [-Distro <name>] [-Role brain|replica]` — the operator entry point (pwsh 7+).
- The four phase scripts are individually runnable (each auto-detects the distro if run standalone); `2-windows-config.ps1` additionally accepts `-EvalRootWsl`.
- `deploy.sh [--dry-run]` — the ongoing deploy entry point (run inside WSL).
- `Test-MemoryStack.ps1` — the manual, non-mutating health verifier that hosts R9 (pwsh 7+; not invoked by any hook or task).

## Dependencies

- **PowerShell 7+ (pwsh)** for the install phases and `Test-MemoryStack.ps1`.
- **WSL2 with mirrored networking + systemd** (user services), a resolvable default distro.
- **Claude Code CLI** and the **Codex CLI** (ChatGPT-authenticated), checked in phase 0.
- **llama-swap on `:11436`** serving the EmbeddingGemma embedder + reranker — the one prerequisite the installer cannot build (see [`../../install/llama-swap-setup.md`](../../install/llama-swap-setup.md)).
- Standard Linux tooling in WSL (Python 3.12+, Node 22+, `curl`, `rsync`, `git`).

## Downstream effects

- Changing the **Receipt schema** (field names) affects every runtime consumer that reads it (`Test-MemoryStack.ps1`, `dream-consolidate.ps1`); a renamed field silently breaks operator-agnostic resolution.
- Changing a **Sentinel token** must be changed in *all four* places at once — the repo source, the installer's `Resolve-StackTokens`, `deploy.sh`'s `sed` block, and R9's normalizer — or parity checks will report false drift or miss real drift.
- Adding a new deployed hot-path script means adding it to the phase-2 deploy list, the phase-3 checks, and R9's `$hookNames` — otherwise it deploys unchecked.
- Adding a server module means adding it to phase 1's `MEM0_MODULES` closure, or a fresh install crash-loops on import.

## Invariants and assumptions

- **One-Brain Rule:** exactly one box runs the nightly canonical-mutation tasks. A `replica` install never registers them and removes any it finds.
- **Loopback binds:** Qdrant (`6333`) and mem0 (`18791`) bind `127.0.0.1`; the shipped `MEM0_BIND` default is loopback.
- **No restart on a broken build:** `deploy.sh` never restarts `mem0.service` if the import-smoke fails.
- **Deployed, not dev-tree:** timers and hooks exec *deployed* copies, never a live working tree, so an uncommitted edit is never production behavior.
- **Operator-neutral at rest:** the shipped source carries Sentinels, not real values; nothing in the repo encodes a specific operator's paths or handles.
- **Idempotent phases:** every install step is existence-guarded and re-runnable.

## Error handling

- **Prereq failure (phase 0):** prints each missing item with a fix hint and exits non-zero; the orchestrator aborts before touching anything.
- **WSL phase failure:** an `ERR` trap prints the failing line and exit code and reminds that a re-run skips completed steps.
- **Windows phase failure:** the hook-client build is smoke-gated and aborts *before* hook registration, so `settings.json` is never left pointing at a missing exe. MCP registration is fail-soft (warns and continues on a malformed `~/.claude.json`).
- **Verify failure:** reported as issues (non-fatal to the install) with a per-check remediation.
- **Deploy failure:** import-smoke failure and health-gate timeout both exit non-zero *without* leaving the service in a worse state (import-smoke fails before restart; a failed health gate points at `journalctl`).

## Security and privacy notes

- The repository ships **PII-free**: operator identity lives only in Sentinels resolved locally at install, and the docs/CI PII gate enforces neutrality at rest.
- Keys are provisioned with least exposure: the API key is mode `600`; the canonical-key is never regenerated over an existing DPAPI blob (which would split-brain the key and regress the at-rest posture).
- The role gate is a **safety** control: it prevents a read-replica from running destructive nightly mutations against the shared brain.
- Loopback binds keep Qdrant and mem0 off the LAN by default; a multi-box brain that must bind more widely does so through `MEM0_BIND`, deliberately, not by accident.
- Scheduled tasks run at **`RunLevel Limited`** (non-elevated) as the interactive user.

## Observability and debugging

- **Install:** run with `-LogFile install.log` for a transcript; each phase prints a banner and per-check OK/MISSING/FAIL lines.
- **Health:** `Test-MemoryStack.ps1` reports three dimensions (LIVENESS / INVARIANTS / RECOVERY) with subtotals; R9 lives under RECOVERY as "deployed hooks freshness".
- **Deploy:** `deploy.sh` echoes each rsynced surface, the import-smoke result, and the `/health/deep` sub-checks; use `--dry-run` to preview.
- **Service logs (WSL):** `systemctl --user status mem0.service` and `journalctl --user -u mem0.service -n 50`.
- **Common symptom → cause:** a session deadlock at "Prompt is too long" points at a skewed deploy layer (a `settings.json`-referenced script missing on disk) — the phase-3 skew guard is the check that surfaces it.

## Testing notes

- **Verify (phase 3)** is the install-time end-to-end smoke: service reachability, Windows file presence, hook registration, the skew guard, role-aware task checks, canonical-key custody, the Codex headless call, and a mem0 add→search round-trip.
- **R9 parity** is the ongoing repo-vs-deployed regression check; run `Test-MemoryStack.ps1` after any deploy.
- **Full server suite:** `deploy.sh` prints the exact `pytest` invocation for `~/apps/mem0-server`; an installer-parity test pins the deployed Windows script set as a superset of R9's tracked names.
- Re-running any phase is itself a test of idempotency.

## Common pitfalls

- **Running under Windows PowerShell 5.1.** `install.ps1` throws by design; use `pwsh -File install.ps1`.
- **Assuming a hard-coded distro name.** The distro is auto-detected and sentinelized; do not bake one in.
- **Editing a deployed script in place** (`~/.claude/scripts` or `~/apps/...`) instead of the repo — R9 will WARN drift, and the next deploy overwrites it.
- **Adding a sentinel/module/hook in only one place.** All of source, installer, `deploy.sh`, and R9 (or phase-1's `MEM0_MODULES`) must agree.
- **Expecting `deploy.sh` to restart on a failing import** — it deliberately does not.
- **Registering nightly tasks on a replica.** Use `-Role replica`; the gate keeps the One-Brain Rule intact.
- **Committing `claude-config/brands.json`.** It is gitignored on purpose; the tracked template is `brands.example.json`.

## Source map

- [`../../install.ps1`](../../install.ps1) — the orchestrator (phase sequence, pwsh gate, distro/role resolution).
- [`../../install/0-prereqs.ps1`](../../install/0-prereqs.ps1) — phase 0 prerequisite checks.
- [`../../install/1-wsl-services.sh`](../../install/1-wsl-services.sh) — phase 1 WSL services (Qdrant, mem0, `MEM0_MODULES`, keys, units, WSL receipt).
- [`../../install/2-windows-config.ps1`](../../install/2-windows-config.ps1) — phase 2 receipt, sentinel resolution, distro-agnostic hooks, `brands.json` fallback, role gate.
- [`../../install/3-verify.ps1`](../../install/3-verify.ps1) — phase 3 smoke test, skew guard, role-aware checks.
- [`../../scripts/wsl/deploy.sh`](../../scripts/wsl/deploy.sh) — the single deploy pipeline (rsync → units → import-smoke → restart → health gate).
- [`../../scripts/windows/Test-MemoryStack.ps1`](../../scripts/windows/Test-MemoryStack.ps1) — the health verifier that hosts the R9 parity check.

## Related docs

- [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) — the whole system these paths install and update.
- [`../glossary.md`](../glossary.md) — Brain, Replica, One-Brain Rule, Receipt, Sentinel, R9 Parity.
- [`codex-hooks.md`](codex-hooks.md) — the hook pipeline the installer registers in `settings.json`.
- [`dream-skill.md`](dream-skill.md) — the nightly consolidator the brain-role scheduled task runs.
- [`continuity.md`](continuity.md) — session-continuity behavior that R9 and the skew guard protect.
- [`dpapi-canonical-key.md`](dpapi-canonical-key.md) — canonical-key custody the install/deploy paths provision.
- [`mem0-api.md`](mem0-api.md) — the server whose modules and health endpoints the deploy pipeline gates on.
- [`tier-policy.md`](tier-policy.md) — the trust tiers whose canonical mutations the role gate protects.
- [`../flows/memory-capture.md`](../flows/memory-capture.md) and [`../flows/memory-retrieval.md`](../flows/memory-retrieval.md) — the behaviors the deployed components implement.
- [`../../install/llama-swap-setup.md`](../../install/llama-swap-setup.md) — the one prerequisite the installer cannot auto-satisfy.
