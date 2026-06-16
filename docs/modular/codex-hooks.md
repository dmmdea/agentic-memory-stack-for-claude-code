# Codex Hooks — L1a Extractor + C1 Consolidator

## Why Codex (not Claude) for unattended cron

Anthropic's Claude Max OAuth enforces a single concurrent session per account. When Claude Code is open in VS Code, subprocess invocations of `claude --print` from hooks fail intermittently with "Not logged in" because the interactive session holds the slot. This was verified with multi-hour debugging: PowerShell-detached invocations, WSL-bridged invocations, explicit `WSLENV` forwarding — all unreliable while the interactive session is active.

Codex CLI authenticates via **ChatGPT subscription OAuth** (separate auth surface, no concurrency block) and runs reliably headless from any Windows shell. `gpt-5.5` quality matches Opus 4.8 for structured extraction tasks. Both I1 (extraction) and C1 (consolidation) are covered by the existing ChatGPT subscription — zero marginal cost.

## L1a Stop-Hook Flow

```
Claude Code Stop / PreCompact event
  │
  ▼ (settings.json hook, fires in the Claude Code process)
stop-extract.ps1    (C:\Users\youruser\.claude\scripts\, runs in < 2s)
  │ Start-Process -Hidden detached pwsh
  ▼
l1a-extract.ps1     (background, 10-min throttle, best-effort — never blocks Claude Code)
  │
  ├─ Test-Throttle('l1a', 600s) → skip if < 10 min since last successful run
  ├─ Drain-Mem0DeadLetter        → retry any previously-failed POSTs first
  ├─ Test-Mem0Health             → skip if mem0 unreachable (writes to DLQ instead)
  ├─ Get-RecentTranscriptTurns   → last 24 turns, capped at 12KB
  ├─ Acquire-CodexLock('l1a')   → skip if C1 or another L1a holds the shared mutex
  │
  ▼ Invoke-CodexSubagent (codex.cmd, ChatGPT OAuth, reasoning=low, timeout=60s)
Codex / gpt-5.5
  │ prompt: extract ≤5 durable facts as {"facts":["...",...]}
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
- Hook dispatcher: `C:\Users\youruser\.claude\scripts\stop-extract.ps1`
- Extractor: `C:\Users\youruser\.claude\scripts\l1a-extract.ps1` (repo: `scripts/windows/l1a-extract.ps1`)
- Shared helpers: `memory-common.ps1` (repo: `scripts/windows/memory-common.ps1`)

**Throttle:** 10 minutes (600s) between successful extractions. The throttle is marked AFTER a successful Codex call + POST, not before. Prior design marked it before doing any work, which silenced extraction for 10 min on any transient failure.

**Log:** `~/.claude/logs/l1a.log`, rotated at 1MB (keeps 5 archives).
**Usage log:** `~/.claude/logs/codex-usage.jsonl` — per-call token/duration/status records.

## Shared Codex Mutex

`Acquire-CodexLock` / `Release-CodexLock` use a single lock file at `~/.claude/state/codex.lock`. Both L1a and C1 contend for it. Stale locks older than 30 minutes are reclaimed automatically.

Rationale: prior design had separate locks per component, which allowed concurrent Codex subprocess invocations from a Stop hook firing while the 3am consolidator was running, causing ChatGPT subscription quota collisions.

## Dead-Letter Queue (DLQ)

Failed mem0 POSTs are appended to `~/.claude/state/mem0-post-failures.jsonl` as newline-delimited JSON. Both L1a and C1 call `Drain-Mem0DeadLetter` at startup to retry outstanding entries. This ensures transient Qdrant/Ollama/mem0 failures self-heal without manual intervention.

## C1 Consolidator

```
Windows Task Scheduler — daily 3:00 AM (ClaudeCode-DreamConsolidator-3am, -WakeToRun)
  │
  ▼
c1-consolidate.ps1  (repo: scripts/windows/c1-consolidate.ps1)
  │
  ├─ Drain-Mem0DeadLetter
  ├─ Acquire-CodexLock('c1')   → skip if L1a holds the mutex
  ├─ Get-Mem0Evidence(limit=100) → filter to last 36h tier=evidence, cap 30 most-recent
  │
  ▼ Invoke-CodexSubagent (reasoning=medium, timeout=180s)
Codex / gpt-5.5
  │ prompt: synthesize 1-3 cross-cutting insights {"insights":["...",...]}
  │ ~30-90s
  ▼
foreach insight: Add-Mem0Memory (tier=insight, source=c1-consolidator, source_memory_ids=[...])
```

**Replacement in D.1:** `c1-consolidate.ps1` will be superseded by `dream-consolidate.ps1` (4-phase orient→gather→consolidate→prune). The Windows Task Scheduler entry (`ClaudeCode-DreamConsolidator-3am`) will be updated to point to the new script. `c1-consolidate.ps1` is kept under `legacy/` for one release as a rollback option. See `docs/modular/dream-skill.md` for the full design.

## Failure Modes

| Condition | Behavior |
|---|---|
| mem0 unreachable on L1a start | Skip extraction; drain DLQ on next run |
| Codex unauthenticated / exits non-zero | Log error; release lock; exit 0 (best-effort) |
| Lock held by other component | Log "skipping: lock held"; exit 0 |
| JSON parse fails on Codex output | Log preview of raw output; exit 0 (no partial write) |
| mem0 POST fails per-fact | Write to DLQ; continue to next fact |
| mem0 unreachable on C1 start | Skip consolidation; drain DLQ on next run |

All failures are logged to `~/.claude/logs/l1a.log` or `~/.claude/logs/c1.log`. Neither script ever blocks the Claude Code UI or propagates errors to the hook caller.
