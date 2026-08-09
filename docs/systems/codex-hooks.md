# Codex hooks — the Claude Code hook pipeline

## Purpose

This system is the set of Claude Code lifecycle hooks (registered in `settings.json`) that drive memory **capture** and **retrieval injection** on Windows. On session close / compaction, a Codex-backed extractor writes the session's durable facts to mem0; on every prompt, a resident daemon + a compiled thin client inject the `[MEMORY CONTEXT]` block; SessionStart spawns the accelerators. Every hook is best-effort and fail-open — it can slow nothing and block nothing.

## Questions this doc answers

- Which Claude Code events are hooked, and what runs on each?
- Why does extraction use Codex instead of `claude --print`?
- How does the L1a Stop/PreCompact extractor flow, and why is it fail-open?
- What are the resident daemon and the compiled client, and how do they stay non-blocking?
- How do the extractor and the nightly consolidator avoid colliding on Codex?

## Scope

The `settings.json` hook registrations and their Windows-side handlers: the L1a Stop/PreCompact extractor, the UserPromptSubmit daemon + compiled client, the SessionStart spawns, and PreToolUse — plus the shared Codex mutex, the dead-letter queue, and the fail-open contracts that make all of it safe.

## Non-scope

- **The nightly consolidator** (orient→gather→consolidate→prune, autopromote, drift) → [`dream-skill.md`](./dream-skill.md).
- **The mem0 REST surface** the hooks POST to → [`mem0-api.md`](./mem0-api.md).
- **The bundle's tier/injection policy** (what gets injected per model) → [`model-aware-injection.md`](./model-aware-injection.md).
- **The WSL-side services** (mem0 server, Qdrant, llama-swap).

## Key concepts

- **Fail-open / best-effort:** a hook never blocks the Claude Code UI, session close, or compaction — it exits 0 and does its work detached.
- **Codex (not Claude):** unattended extraction/consolidation run on the Codex CLI (ChatGPT-subscription OAuth) to dodge the Claude Max single-session concurrency block.
- **The resident daemon:** `mem0-hook-daemon.ps1` — an accelerator that keeps the bundle pipeline warm over a named pipe; it is *never a dependency*.
- **The compiled client:** `mem0-hook-client.exe` (from `mem0-hook-client.cs`) — the registered UserPromptSubmit command; a thin exe that talks to the daemon and falls back to the inline PowerShell path on any failure.
- **The shared Codex mutex:** one lock file that the extractor and the consolidator contend for, so Codex is never invoked concurrently.
- **The dead-letter queue (DLQ):** failed mem0 POSTs are queued and retried on the next run, so transient backend outages self-heal.

## How the system works

### Why Codex (not Claude) for unattended cron

Anthropic's Claude Max OAuth enforces a single concurrent session per account. When Claude Code is open in VS Code, subprocess invocations of `claude --print` from hooks fail intermittently with "Not logged in" because the interactive session holds the slot. This was verified with multi-hour debugging: PowerShell-detached invocations, WSL-bridged invocations, explicit `WSLENV` forwarding — all unreliable while the interactive session is active.

Codex CLI authenticates via **ChatGPT subscription OAuth** (separate auth surface, no concurrency block) and runs reliably headless from any Windows shell. `gpt-5.5` quality matches Opus 4.8 for structured extraction tasks. Both the L1a extraction and the nightly consolidation are covered by the existing ChatGPT subscription — zero marginal cost.

### The registered hooks

`install/2-windows-config.ps1` patches these entries into the live `~/.claude/settings.json` (see `claude-config/settings.example.json` for the scrubbed reference):

| Event | Handler | Role |
|---|---|---|
| `Stop` | `stop-extract.ps1` | Dispatch the L1a extractor at session close. |
| `PreCompact` | `stop-extract.ps1` | Snapshot the transcript before compaction mutates it, then extract. |
| `SessionStart` | `storage-cap-check.sh` (WSL), `mem0-hook-daemon-spawn.ps1` (async), `codex-shim-spawn.ps1` (async), `sessionstart-capture.ps1` (async) | Storage guard + spawn the bundle daemon, the Codex HTTP shim (`:18792`), and the session précis. |
| `UserPromptSubmit` | `mem0-hook-client.exe` | Inject the `[MEMORY CONTEXT]` block for the prompt. |
| `PreToolUse` | *(none — stack-owned entry retired)* | The 0.F contradiction check (`pre-tool-check.ps1`) was retired 2026-08-09 by operator decision (AMS-16, near-100% FP warn corpus); the installer strips it on re-run. |

## Important flows

### L1a Stop / PreCompact extraction

```
Claude Code Stop / PreCompact event
  │
  ▼ (settings.json hook, fires in the Claude Code process)
stop-extract.ps1    (C:\Users\<WIN_USER>\.claude\scripts\, runs in < 2s)
  │ Start-Process -Hidden detached pwsh
  ▼
l1a-extract.ps1     (background, 10-min throttle, best-effort — never blocks Claude Code)
  │
  ├─ Test-Throttle('l1a', 600s) → skip if < 10 min since last successful run
  ├─ Drain-Mem0DeadLetter        → retry any previously-failed POSTs first
  ├─ Test-Mem0Health             → skip if mem0 unreachable (writes to DLQ instead)
  ├─ Get-RecentTranscriptTurns   → last 24 turns, capped at 12KB
  ├─ Acquire-CodexLock('l1a')   → skip if the nightly consolidator or another L1a holds the shared mutex
  │
  ▼ Invoke-CodexSubagent (codex.cmd, ChatGPT OAuth, reasoning=low, timeout=60s)
Codex / gpt-5.5
  │ prompt: extract ≤5 durable facts as {"facts":[...]} (plus an episode checkpoint)
  │ ~15-30s per call
  ▼
{"facts": ["fact 1", "fact 2"]}
  │
  ▼ foreach fact: Add-Mem0Memory (POST /v1/memories, infer=false, tier=evidence)
mem0 :18791
  │
  ├─ Success → Mark-Throttle('l1a'); log to ~/.claude/logs/l1a.log
  └─ Failure → dead-letter to ~/.claude/state/mem0-post-failures.jsonl (drained next run)
```

**Key files:**
- Hook dispatcher: `C:\Users\<WIN_USER>\.claude\scripts\stop-extract.ps1`
- Extractor: `C:\Users\<WIN_USER>\.claude\scripts\l1a-extract.ps1` (repo: `scripts/windows/l1a-extract.ps1`)
- Shared helpers: `memory-common.ps1` (repo: `scripts/windows/memory-common.ps1`)

**Throttle:** 10 minutes (600s) between successful extractions. The throttle is marked AFTER a successful Codex call + POST, not before. Prior design marked it before doing any work, which silenced extraction for 10 min on any transient failure.

**Log:** `~/.claude/logs/l1a.log`, rotated at 1MB (keeps 5 archives).
**Usage log:** `~/.claude/logs/codex-usage.jsonl` — per-call token/duration/status records.

### The fail-open PreCompact contract

`stop-extract.ps1` is fire-and-forget: it reads the hook JSON from stdin, and **exits 0 immediately** after spawning the detached worker, so Claude Code's session-close and compaction paths are never blocked. For `PreCompact` specifically, it first copies the transcript to a `precompact-snap-<PID>.jsonl` snapshot **before** compaction can mutate it, and dispatches the extractor against that snapshot (so the pre-compaction turns are still available to extract). The worker `l1a-extract.ps1` "runs detached, exits 0 always (best-effort, never blocks Claude Code)." Nothing on this path can fail loudly enough to affect the user.

### UserPromptSubmit injection (daemon + compiled client)

The registered UserPromptSubmit command is the compiled `mem0-hook-client.exe`. It reads stdin, hashes the deployed lib, probe-then-connects the resident daemon's named pipe (`mem0-hook-daemon`), runs one `bundle` transaction, and writes the returned `[MEMORY CONTEXT]` block to stdout — replacing a ~390ms `powershell.exe` cold-spawn on the warm path.

The **hard constraint** is that the daemon is an **accelerator, never a dependency**: *any* failure — no pipe, connect/response timeout, garbage response, or a `lib_hash` staleness mismatch — falls back to the existing inline PowerShell path (`user-prompt-extract.ps1 -SkipDaemon`), which is byte-identical to the pre-daemon behavior, and (on a missing pipe) triggers a detached daemon respawn. The exit-code contract is fail-open by design: the exe always exits 0, and a child exit code of `2` (which would erase the user's prompt) is mapped to `0`, so a broken fallback can never block the prompt.

The daemon (`mem0-hook-daemon.ps1`) stays resident (single instance via a named mutex; self-shutdown after 2h idle), pays the .NET HTTP init once, keeps the loopback connection warm, and serves `POST /v1/context/bundle` + block rendering. A `lib_hash` (SHA256 over the deployed lib **and** the daemon script) rides every response; a mismatch after a redeploy makes the client shut the stale daemon down so the next prompt starts a fresh one.

### The nightly consolidator (dream)

The nightly consolidation is **not** an L1a hook — it runs from the Task Scheduler entry `ClaudeCode-DreamConsolidator-3am` as `dream-consolidate.ps1` (which superseded the earlier `c1-consolidate.ps1` single-pass consolidator). It shares this system's two safety primitives: it drains the DLQ at startup and acquires the **same** shared Codex mutex before its Codex calls, so a 3am consolidation and a Stop-hook extraction never invoke Codex at once. Full design → [`dream-skill.md`](./dream-skill.md).

## Data and state

| File / resource | Role |
|---|---|
| `~/.claude/state/codex.lock` | The shared Codex mutex (30-min stale reclaim). |
| `~/.claude/state/mem0-post-failures.jsonl` | The dead-letter queue of failed mem0 POSTs. |
| `~/.claude/state/hook-fixtures/` | Sampled stdin fixtures (byte-faithful) for wire-contract regression. |
| `~/.mem0/hook-daemon.log` | Daemon log — op names/counts/durations/hashes only, **no payload**. |
| `~/.claude/logs/l1a.log`, `codex-usage.jsonl` | Extractor + per-call Codex usage logs. |
| named pipe `mem0-hook-daemon` | The client↔daemon transport (ACL: current user only). |

## Interfaces and entry points

The entry points are the `settings.json` hook registrations in the table above. The extractor and consolidator call the mem0 REST API on `:18791`; the daemon serves `POST /v1/context/bundle`; the Codex HTTP shim (spawned at SessionStart) listens on `:18792` for the server-side NLI/gate judgments.

## Dependencies

- **Codex CLI** (`codex.cmd`, ChatGPT-subscription OAuth), gpt-5.5.
- **mem0 REST** on `:18791` (all reads/writes).
- **PowerShell 7 (pwsh)** preferred for the PS hooks; the daemon runs under Windows PowerShell 5.1 (its `JavaScriptSerializer` + `NamedPipeServerStream` PipeSecurity are .NET Framework).
- **The compiled-client toolchain:** `build-hook-client.ps1` (framework `csc`) compiles `mem0-hook-client.cs`.
- **llama-swap** on `:11436` indirectly, via the bundle's embedder.

## Downstream effects

Extracted facts land in mem0 as `tier=evidence` and become the raw material the nightly dream consolidates. The bundle the daemon serves is what the model sees as `[MEMORY CONTEXT]`. Bumping the hook wire contract requires extending `KNOWN_HOOK_CONTRACT_VERSIONS` in `hook_contract.py` in the same change, or the server logs a drift WARN (it never rejects).

## Invariants and assumptions

- **The daemon is an accelerator, never a dependency** — every failure falls back to a behavior byte-identical to the no-daemon path.
- **Hooks never block:** stop-extract and the extractor exit 0 always; the client maps a blocking child exit `2` to `0`.
- **PreCompact snapshots before compaction** — the pre-compaction transcript is preserved for extraction.
- **One Codex at a time** — the shared mutex serializes the extractor and the consolidator.
- **The throttle is marked only after success** — a transient failure never silences the next 10 minutes.

## Error handling

| Condition | Behavior |
|---|---|
| mem0 unreachable on L1a start | Skip extraction; drain DLQ on next run |
| Codex unauthenticated / exits non-zero | Log error; release lock; exit 0 (best-effort) |
| Lock held by other component | Log "skipping: lock held"; exit 0 |
| JSON parse fails on Codex output | Log preview of raw output; exit 0 (no partial write) |
| mem0 POST fails per-fact | Write to DLQ; continue to next fact |
| Daemon pipe absent / timeout / stale `lib_hash` | Fall back to the inline PS path; respawn the daemon detached |
| Unknown `hook_contract_version` | Server logs a WARN (drift signal); never rejects |

All failures are logged (`~/.claude/logs/l1a.log`, `~/.mem0/hook-daemon.log`). No hook ever blocks the Claude Code UI or propagates an error to the caller.

## Security and privacy notes

The daemon's named pipe is ACL'd to the current user only (inherited ACEs dropped). No hook logs raw prompt or memory text — logs carry op names, counts, durations, session ids, and hashes only. Codex runs on a separate OAuth surface (ChatGPT subscription), so unattended extraction never touches the Claude credential. The DLQ and fixtures live under the user profile.

## Observability and debugging

- **Logs:** `l1a.log` (extraction), `hook-daemon.log` (daemon ops), `codex-usage.jsonl` (Codex spend).
- **Drift counters:** `GET /health/deep` → `checks.hook_contract` reports `missing`/`unknown` version counts (hook↔server skew).
- **Staleness:** the `lib_hash` handshake makes a stale-daemon-after-deploy self-correct on the next prompt.
- **`Test-MemoryStack.ps1`** (R9) hashes `mem0-hook-client.cs` and checks the exe is fresh against it.

## Testing notes

The compiled client's fail-open matrix (missing lib, absent pipe, timeouts, garbage responses, `lib_hash` mismatch) is driven by Pester against scripted fake daemons via the `MEM0_HOOK_PIPE` override; `build-hook-client.ps1` smoke-gates a candidate exe before installing it. The stop-extract dispatcher samples byte-faithful stdin fixtures (`hook-fixtures/`) so wire-format drift is detectable. `Test-MemoryStack.ps1` is the live end-to-end probe.

## Common pitfalls

- **Treating the daemon as required** — it is an accelerator; the inline PS path is always the source of truth, and the exe must degrade to it silently.
- **Marking the throttle before doing work** — the fixed design marks it only after a successful Codex call + POST.
- **Looking for `c1-consolidate.ps1`** — the nightly consolidator is now `dream-consolidate.ps1` (see [`dream-skill.md`](./dream-skill.md)).
- **Expecting `infer=true` from hooks** — automated writes always use `infer=false` (see [`mem0-api.md`](./mem0-api.md)).
- **Rejecting on `hook_contract_version`** — it is WARN-only; drift is surfaced, never blocked.

## Source map

- [`../../scripts/windows/stop-extract.ps1`](../../scripts/windows/stop-extract.ps1) — the Stop/PreCompact dispatcher + the PreCompact snapshot.
- [`../../scripts/windows/l1a-extract.ps1`](../../scripts/windows/l1a-extract.ps1) — the detached Codex-backed extractor.
- [`../../scripts/windows/mem0-hook-daemon.ps1`](../../scripts/windows/mem0-hook-daemon.ps1) — the resident UserPromptSubmit bundle accelerator.
- [`../../scripts/windows/mem0-hook-client.cs`](../../scripts/windows/mem0-hook-client.cs) — the compiled thin client (fail-open exit-code contract).
- [`../../scripts/windows/build-hook-client.ps1`](../../scripts/windows/build-hook-client.ps1) — compiles + smoke-gates the client exe.
- [`../../scripts/windows/memory-common.ps1`](../../scripts/windows/memory-common.ps1) — the shared Codex lock, throttle, and DLQ helpers.
- [`../../mem0-server/hook_contract.py`](../../mem0-server/hook_contract.py) — the WARN-only hook-contract drift detector.
- [`../../claude-config/settings.example.json`](../../claude-config/settings.example.json) — the scrubbed hook registrations reference.

## Related docs

- [`dream-skill.md`](./dream-skill.md) — the nightly consolidator that shares the Codex mutex + DLQ.
- [`mem0-api.md`](./mem0-api.md) — the REST surface the hooks read and write.
- [`model-aware-injection.md`](./model-aware-injection.md) — what the bundle injects per model tier.
- [`continuity.md`](./continuity.md) — session continuity / the SessionStart précis.
- [`../flows/memory-capture.md`](../flows/memory-capture.md) — the capture flow these hooks implement.
- [`../glossary.md`](../glossary.md) · [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md)
