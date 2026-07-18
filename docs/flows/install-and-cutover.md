# Install and cutover — bring-up, re-install, and moving machines

## Purpose

This flow is the ordered narrative for getting the stack *onto* a machine and keeping that install healthy over its life: a **fresh install** (four phases in strict order), a **role choice** that decides whether the box is the write authority, an **idempotent re-install / upgrade**, a **cutover** to a new machine with memory intact, and a **rollback** that is just a reinstall from a known-good ref. It deliberately stays at the flow altitude — order, decisions, and cutover — and defers the per-phase mechanics (the Receipt, Sentinel resolution, the deploy pipeline, R9 parity) to the system doc [`../systems/installer-and-deploy.md`](../systems/installer-and-deploy.md).

The through-line is that **the code is reinstalled, never transported.** The installer derives every operator-specific value from the machine it runs on, so a fresh install, a re-run, and a rollback are the *same operation* pointed at different repo states — and a machine move is a fresh install plus a data restore.

## Trigger

| Case | What starts it |
|---|---|
| **Fresh install** | An operator runs `install.ps1` on a new Windows + WSL2 box for the first time. |
| **Re-install / upgrade** | `git pull` (or `git checkout <ref>`) followed by re-running `install.ps1` to pick up changed hook scripts, tasks, or registrations. |
| **Deploy (server-side change)** | `deploy.sh` inside WSL — the ongoing path for pushing changed server modules and maintenance scripts into the running runtime between full re-installs. |
| **Machine cutover** | Standing up the stack on a replacement machine and continuing there — the [`../MIGRATION.md`](../MIGRATION.md) runbook. |
| **Rollback** | A bad change in the running stack — `git checkout <known-good-ref>` and re-run. |

## Participants

- **The orchestrator** — [`../../install.ps1`](../../install.ps1): a pwsh-7+ script that resolves the WSL distro and role, then runs the four phases in strict order, aborting on any hard failure.
- **The four phase scripts** — [`0-prereqs.ps1`](../../install/0-prereqs.ps1), [`1-wsl-services.sh`](../../install/1-wsl-services.sh), [`2-windows-config.ps1`](../../install/2-windows-config.ps1), [`3-verify.ps1`](../../install/3-verify.ps1).
- **The deploy pipeline** — [`../../scripts/wsl/deploy.sh`](../../scripts/wsl/deploy.sh): the single gated repo-to-runtime path for server-side changes.
- **The Receipts** — the machine-local record of the operator's choices: `~/.claude/scripts/mem0-stack.config.psd1` (Windows) and `~/.mem0/stack.env` (WSL).
- **The repository** — the tracked source (scripts carry operator-neutral **Sentinel** tokens; `.example` files are templates), pinned to whatever ref is checked out.
- **The operator** — chooses `-Role`, supplies the (optional) `-Distro`, and provides the one prerequisite the installer cannot build: llama-swap with both GGUFs.

## Step-by-step flow

### Fresh install — phases 0 → 1 → 2 → 3

`install.ps1` requires **PowerShell 7+** (it throws on Windows PowerShell 5.1, which cannot even parse phase 2), auto-detects the default WSL distro (`wsl -l -q`, first entry) unless `-Distro` is passed, echoes the chosen role, then runs the phases **in this order**, aborting on any hard failure in 0–2:

1. **`[0/4]` Prerequisites** — `install/0-prereqs.ps1`. Fails fast if anything required is missing; a non-zero exit aborts before anything is touched.
2. **`[1/4]` WSL services** — `install/1-wsl-services.sh`, invoked over `wsl.exe` with the WSL user, Windows user, and distro as arguments. Provisions Qdrant (loopback bind), the mem0 server + venv + its full import closure, the keys, the systemd-user units and maintenance scripts, and the **WSL Receipt** (`~/.mem0/stack.env`).
3. **`[2/4]` Windows config** — `install/2-windows-config.ps1 -WslUser -Distro -Role`. Writes the **Windows Receipt**, resolves Sentinels into deployed copies, registers the Claude Code hooks and MCP server, applies the role gate (below), and patches `CLAUDE.md`.
4. **`[3/4]` Verify** — `install/3-verify.ps1`. An end-to-end smoke test plus the skew guard and role-aware task checks. A verify failure is a **warning, not a hard abort** — the stack may still be partially functional.

### Role selection — `brain` vs `replica`

`install.ps1 -Role brain|replica` (default **`brain`**) sets the machine's place under the **One-Brain Rule**:

- **`brain`** — this box is the sole memory write authority; phase 2 registers the two nightly canonical-mutation scheduled tasks (`ClaudeCode-DreamConsolidator-3am`, `ClaudeCode-SemanticDedup-430am`).
- **`replica`** — a read-only consumer; phase 2 registers *neither* task **and removes any previously-registered ones**, because consolidation and dedup mutate the one shared brain and there is no cross-machine lock. The role is recorded in the Receipt and re-asserted by verify: a `brain` must have both tasks, a `replica` must have neither.

### Re-install / upgrade — idempotent re-run

Every phase is **existence-guarded and safe to re-run**: the Qdrant binary, the mem0 venv, the embedder GGUF, the keys, the systemd units, and the hook registrations all skip when already present. So an upgrade is simply **`git pull` (or `git checkout <ref>`) → re-run `install.ps1`** — completed steps are skipped and only the incomplete tail runs, which also means a re-run after a *failed* install resumes safely rather than starting over. For server-side code that only needs to reach the running WSL runtime between full re-installs, **`deploy.sh`** is the gated path (rsync → sentinel-resolved units → import-smoke → restart → health gate) — its import-smoke **refuses to restart on a broken build**, so a bad change never becomes running behavior.

### Machine cutover — reinstall + restore, never transport

Moving to a new machine is a *fresh install on the new box*, not a copy of the old one. Three things get three different treatments (full runbook: [`../MIGRATION.md`](../MIGRATION.md)):

- **Code + services + hooks** → a fresh `install.ps1` on the new machine; the installer derives every path from the new machine's users and distro.
- **Memory data** (Qdrant collection, `episodic.db`, ledgers) → **restored from a backup snapshot** — this is the accumulated memory, the reason to migrate.
- **Credentials** (the canonical HMAC key, Claude/Codex OAuth, git auth) → **re-provisioned fresh** on the new machine; the DPAPI-wrapped key is bound to the old machine's Windows user and cannot decrypt elsewhere, and nothing in the store depends on it.

Then the old machine is decommissioned (its writers stopped) so the two stores cannot diverge — the cutover is only complete when exactly one box is writing.

### Rollback — reinstall from a known-good ref

Because install and deploy are idempotent and gated, rollback needs no special tooling: **`git checkout <known-good-ref>`**, then re-run `install.ps1` (Windows + WSL surfaces) or `deploy.sh` (WSL server surface). The re-run overwrites the deployed layer with the known-good source, the deploy gate blocks a restart on a bad build, and R9 parity confirms the deployed copies match the ref. A ref you have installed cleanly before is a safe rollback target.

## Data and state changes

The line between **machine-local** (derived per box, never tracked) and **tracked** (in the repo, operator-neutral) is what makes the code portable:

| State | Where | Machine-local or tracked |
|---|---|---|
| Repo source (scripts with Sentinels, `.example` templates, units) | the checkout | **Tracked** — operator-neutral, pinned to the checked-out ref |
| Windows Receipt `mem0-stack.config.psd1` | `~/.claude/scripts/` | **Machine-local** — the operator's users/distro/role/paths |
| WSL Receipt `stack.env` | `~/.mem0/` | **Machine-local** — the bash-side mirror `deploy.sh` sources |
| Runtime store + keys + queues | `~/.mem0/*` (Qdrant, `episodic.db`, `api-key`, `canonical-key`/DPAPI blob, outbox, markers) | **Machine-local** — provisioned at install, restored at cutover |
| Sentinel-resolved deployed copies | `~/.claude/scripts/`, `~/apps/mem0-server/`, `~/apps/mem0-scripts/`, `~/.config/systemd/user/` | **Machine-local** — real values substituted from the tracked source |
| Claude Code config touched by install | `~/.claude/settings.json`, `~/.claude.json`, `~/.claude/CLAUDE.md` | **Machine-local** — backed up before each modification |

A fresh install *creates* the machine-local set from the tracked source; a re-install *reconciles* it; a cutover *restores* the data half onto a freshly-created code half; a rollback *rewrites* the deployed copies from an older tracked ref.

## Success behavior

- **Fresh install:** phases 0–2 complete without a hard failure and phase 3 verify prints its checks — the machine-local Receipts exist, the deployed layer matches the source, hooks and MCP are registered, and (on a `brain`) both nightly tasks are present. Restarting Claude Code loads the new hooks + MCP.
- **Re-install / upgrade:** the re-run skips completed steps and applies only the delta; `deploy.sh` restarts the server only behind a green import-smoke + health gate.
- **Cutover:** the new box passes verify (empty store), the restore brings the point count up to the pre-move manifest, an old memory retrieves, and the old machine's writers are stopped — one brain, moved.
- **Rollback:** the deployed layer matches the known-good ref and R9 parity is clean.

## Failure behavior

- **Prereq failure (phase 0)** → each missing item is printed with a fix hint; the orchestrator aborts before touching anything.
- **WSL phase failure (phase 1)** → an `ERR` trap prints the failing line and reminds that a re-run skips completed steps.
- **Windows phase failure (phase 2)** → the hook-client build is smoke-gated and aborts *before* hook registration, so `settings.json` is never left pointing at a missing exe; MCP registration is fail-soft.
- **Verify issues (phase 3)** → reported as per-check remediations; **non-fatal** to the install (the stack may still be partially functional).
- **Deploy failure** → import-smoke failure or health-gate timeout exits non-zero *without* leaving the service worse (import-smoke fails before any restart).
- **Running on Windows PowerShell 5.1** → `install.ps1` throws immediately by design; re-run under `pwsh`.

## External dependencies

- **PowerShell 7+ (pwsh)** — for the install phases and the health verifier.
- **WSL2 with mirrored networking + systemd** (user services) and a resolvable default distro.
- **Claude Code CLI** and the **Codex CLI** (ChatGPT-authenticated) — checked in phase 0.
- **llama-swap on `:11436`** serving the EmbeddingGemma embedder + reranker — the one prerequisite the installer cannot build (see [`../../install/llama-swap-setup.md`](../../install/llama-swap-setup.md)).
- **Standard Linux tooling in WSL** (Python 3.12+, Node 22+, `curl`, `rsync`, `git`).
- **A backup snapshot** — required only for the cutover case (produced by the WSL backup timer).

## Invariants and assumptions

1. **Phase order is strict: 0 → 1 → 2 → 3.** Prereqs gate everything; WSL services must exist before Windows config wires hooks to them; verify runs last. A hard failure in 0–2 aborts; verify is advisory.
2. **The role flag is `-Role brain|replica`, default `brain`, and enforces the One-Brain Rule.** Exactly one box runs the nightly canonical-mutation tasks; a `replica` install registers none and removes any it finds.
3. **Every phase is idempotent.** Re-running is safe and resumes a partial install — the property that makes re-install, upgrade, and rollback the same operation as a fresh install.
4. **The code is reinstalled, never transported.** The installer derives all operator-specific values locally from Sentinels; no repo artifact encodes a specific machine, so a fresh install on any box is operator-agnostic.
5. **Machine-local vs tracked never blur.** Receipts, `~/.mem0/*` runtime state, and sentinel-resolved deployed copies are per-machine; the repo source is tracked and operator-neutral. Data and credentials are machine-local and are *restored / re-provisioned* on cutover, not copied.
6. **Deployed, not dev-tree.** Hooks and timers exec *deployed* copies, so an uncommitted edit is never production behavior — and a rollback to a ref fully governs runtime once re-run.
7. **No restart on a broken build.** `deploy.sh` never restarts the service if the import-smoke fails, so a bad upgrade or a bad rollback ref cannot take the runtime down.

## Security and privacy notes

- **Operator-neutral at rest.** The shipped source carries Sentinels, not real values; operator identity materializes only in machine-local Receipts and deployed copies resolved at install. The docs/CI PII gate enforces this neutrality.
- **Keys are provisioned with least exposure and never transported.** The API key is mode `600`; the canonical key is never regenerated over an existing DPAPI blob. On cutover the canonical key is generated *fresh* on the new machine — the DPAPI blob is bound to the old Windows user and cannot decrypt elsewhere.
- **The role gate is a safety control.** It stops a read-replica from running destructive nightly mutations against the one shared brain.
- **Loopback binds by default.** Qdrant (`6333`) and mem0 (`18791`) bind `127.0.0.1`; a wider bind is a deliberate `MEM0_BIND` choice, not an accident. Scheduled tasks run non-elevated as the interactive user.

## Observability and debugging

- **Install transcript** — run `install.ps1 -LogFile install.log`; each phase prints a banner and per-check OK/MISSING/FAIL lines.
- **Verify** — `3-verify.ps1` is the install-time end-to-end smoke; watch for the skew guard ("No hook references a missing deployed script") and the role-aware task checks.
- **Health / parity** — `Test-MemoryStack.ps1` reports LIVENESS / INVARIANTS / RECOVERY, with the R9 parity check under RECOVERY surfacing any repo-vs-deployed drift after a deploy or rollback.
- **Deploy** — `deploy.sh` echoes each rsynced surface, the import-smoke result, and the `/health/deep` sub-checks; `--dry-run` previews without writing.
- **Service logs (WSL)** — `systemctl --user status mem0.service` and `journalctl --user -u mem0.service -n 50`.
- **Common symptom → cause** — a session deadlock at "Prompt is too long" points at a skewed deploy layer (a `settings.json`-referenced script missing on disk); the phase-3 skew guard is the check that surfaces it.

## Testing notes

- **Verify (phase 3)** is the install-time regression: service reachability, Windows file presence, hook registration, the skew guard, role-aware task checks, canonical-key custody, a headless Codex call, and a mem0 add→search round-trip.
- **R9 parity** (`Test-MemoryStack.ps1`) is the ongoing repo-vs-deployed check — run it after any deploy, upgrade, or rollback to confirm the deployed copies match the checked-out ref.
- **Re-running any phase is itself an idempotency test.** An installer-parity test additionally pins the deployed Windows script set as a superset of R9's tracked names.
- **Cutover** has its own verification woven through [`../MIGRATION.md`](../MIGRATION.md) — a clean `3-verify.ps1`, a matching restored point count, and an old memory retrieving on the new box.

## Source map

- [`../../install.ps1`](../../install.ps1) — the orchestrator: pwsh-7 gate, distro auto-detect, `-Role brain|replica`, the strict `0 → 1 → 2 → 3` phase sequence, idempotency note.
- [`../../install/0-prereqs.ps1`](../../install/0-prereqs.ps1) — phase 0 prerequisite checks (fail-fast).
- [`../../install/1-wsl-services.sh`](../../install/1-wsl-services.sh) — phase 1 WSL services + the WSL Receipt.
- [`../../install/2-windows-config.ps1`](../../install/2-windows-config.ps1) — phase 2 Windows Receipt, Sentinel resolution, hooks/MCP registration, the role gate.
- [`../../install/3-verify.ps1`](../../install/3-verify.ps1) — phase 3 smoke test, skew guard, role-aware task checks.
- [`../../scripts/wsl/deploy.sh`](../../scripts/wsl/deploy.sh) — the gated repo-to-runtime deploy pipeline used between full re-installs and for rollback of server code.

## Related docs

- [`../systems/installer-and-deploy.md`](../systems/installer-and-deploy.md) — the deep system detail this flow narrates: the Receipt fields, Sentinel tokens, the deploy pipeline internals, and R9 parity.
- [`../MIGRATION.md`](../MIGRATION.md) — the machine-move runbook: snapshot, fresh install, restore, re-key, verify, decommission.
- [`../data-backup.md`](../data-backup.md) — backing up the memory data the cutover restores (which the code reinstall does not cover).
- [`../systems/offline-travel.md`](../systems/offline-travel.md) — the runtime brain/replica failover: the same One-Brain Rule the install role gate enforces.
- [`../systems/dpapi-canonical-key.md`](../systems/dpapi-canonical-key.md) — canonical-key custody the install provisions and the cutover re-provisions fresh.
- [`../systems/codex-hooks.md`](../systems/codex-hooks.md) — the Claude Code hooks the installer registers into `settings.json`.
- [`../glossary.md`](../glossary.md) — Brain, Replica, One-Brain Rule, Receipt, Sentinel, R9 Parity · [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md).
