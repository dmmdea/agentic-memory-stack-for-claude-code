# Operations runbook

When something breaks — or a session banner asks for attention — look here first. Each entry: symptom → diagnostic queries → fix.

> Linux paths are relative to your WSL user's home (`~`); Windows paths use `$env:USERPROFILE`. The installer substitutes your real usernames into the deployed scripts.

---

## Quick health check (run this first when anything seems off)

```powershell
# In any PowerShell on Windows (WSL mirrored networking makes these reachable):
'qdrant=http://127.0.0.1:6333/healthz',
'mem0=http://127.0.0.1:18791/health',
'llama-swap=http://127.0.0.1:11436/v1/models' | ForEach-Object {
    $name,$url = $_ -split '=',2
    try { Invoke-RestMethod -Uri $url -TimeoutSec 5 | Out-Null; Write-Host "  $name OK" -f Green }
    catch { Write-Host "  $name DOWN ($($_.Exception.Message))" -f Red }
}

# Deep end-to-end (store + embedder dimension + collection binding; slower):
Invoke-RestMethod http://127.0.0.1:18791/health/deep

# Codex (the extraction/judgment LLM) still authenticated?
"Reply with exactly: ok" | codex exec --skip-git-repo-check -c model_reasoning_effort='"low"' -
```

`/health` also reports the stack release version. Anything red → matching section below. For the full scripted check, run the deployed **`Test-MemoryStack.ps1`** (`~/.claude/scripts/Test-MemoryStack.ps1`) — liveness + invariants, pass/fail per row.

---

## The scheduled machinery (what should be running when)

| When | Job | Where to check |
|---|---|---|
| every prompt | memory injection + episode checkpoint + correction capture | `~/.claude/logs/user-prompt-extract.log` |
| session start | banners (storage cap, audit flags, review queue), resume précis, dream catch-up spawn | the banner itself; `~/.claude/logs/` |
| session end / compaction | L1a fact extraction (10-min throttle) | `~/.claude/logs/l1a.log` |
| daily 3:00 (Task Scheduler, WakeToRun) | dream consolidation | `schtasks /Query /TN "ClaudeCode-DreamConsolidator-3am" /FO LIST /V`; `~/.claude/logs/dream.log` |
| daily 4:30 (Task Scheduler) | semantic dedup | `~/.mem0/tier-ledger-YYYY-MM.jsonl` (deletes are logged; monthly segments) |
| Sun 02:00 / 03:30 / 04:00 / 05:00 / 05:30 | decay-scan / stack-backup / goals-stale-sweep / contradiction-sweep / episodic-reconcile | `systemctl --user list-timers` in WSL |
| every 6 h | L10 heuristic audit | `~/.mem0/audit-flags.jsonl` |

```bash
# WSL: are the timers armed?
systemctl --user list-timers --all | grep -E "decay|backup|goals|contradiction|reconcile|l10"
```

---

## "The session banner says contradictions await review"

This is the reconciliation system working as designed: a Codex verdict flagged records as stale/contradicting, and the **never-auto-hide** policy routes them to you instead of hiding them.

```bash
# WSL — see the queue (one JSON line per candidate: memory_id, canonical_id, candidate text, justification)
cat ~/.mem0/contradiction-promote-review.jsonl

# The sweep runs on the mem0 venv python; the deployed copy lives in ~/apps/mem0-scripts/
PY=~/apps/mem0-server/.venv/bin/python

# Enforce a reviewed candidate (hides it from durable/operational retrieval; forensic 'history' still sees it)
$PY ~/apps/mem0-scripts/contradiction-sweep.py --promote <memory_id>

# It was a false flag / you promoted the wrong one — one-command recovery
$PY ~/apps/mem0-scripts/contradiction-sweep.py --unstamp <memory_id>

# Re-judge everything currently flagged (auto-CLEARS false positives; never auto-hides)
$PY ~/apps/mem0-scripts/contradiction-sweep.py --rejudge-stamped --judge codex --apply
```

The Codex judge needs the Windows shim up (`:18792`; it self-starts at session start when enabled, idle-stops after 4 h). If the sweep reports `outcome=no-op:codex-shim-unreachable`, start a Claude Code session (or run the shim spawn script) and retry — it deliberately refuses to fall back to a local judge.

---

## "The banner says audit flags need review"

L10's 6-hourly heuristics flagged writes (oversize / injection-shaped / credential-shaped / missing provenance). Flags are advisory — nothing is hidden.

```bash
PY=~/apps/mem0-server/.venv/bin/python
$PY ~/apps/mem0-scripts/audit-flags-triage.py --summary     # what's flagged, grouped
# --resolve marks the WHOLE current backlog reviewed (there is no per-id mode);
# hold back categories you still want visible with --keep-types
$PY ~/apps/mem0-scripts/audit-flags-triage.py --resolve --reason "reviewed: benign"
```

---

## "A memory I know exists isn't surfacing"

Retrieval is **precision-first** — records are hidden by design for several reasons. Check which one:

```bash
# 1. Was it rejected at admission? (reason per rejection: tier / superseded_by / contradicts_canonical / brand / age)
tail -20 ~/.mem0/admission-rejected.jsonl

# 2. Ask in the forensic class — history disables the hide checks:
#    mcp__mem0__memory_search query="..." query_class="history"

# 3. Brand scope: a brandless search returns ONLY brand-neutral records (fail-closed).
#    Pass brand="..." or allow_cross_brand=true deliberately.
```

- **Superseded / contradicts-canonical** → that's reconciliation; `--unstamp` if wrong (section above).
- **It's `insight` tier and you expected it in the per-prompt block** → insights are deliberately filtered from the hot path; use `memory_search`.
- **Nothing injected at all on a prompt** → abstention-first: nothing cleared the 0.30 gate. That's correct behavior for off-domain prompts.

---

## "L1a fires but no facts get extracted"

**Diagnose:**

```powershell
Get-Content "$env:USERPROFILE\.claude\logs\l1a.log" -Tail 30
Invoke-RestMethod http://127.0.0.1:18791/health          # mem0 up?
"Reply: ok" | codex exec --skip-git-repo-check -c model_reasoning_effort='"low"' -   # Codex auth?
```

**Common fixes:**
- **Log empty / no `=== start ===` lines** → the Stop/PreCompact hook isn't firing. Check `~/.claude/settings.json` `Stop`/`PreCompact` point at `stop-extract.ps1`; restart VS Code.
- **"codex subagent failed" / auth error** → `codex login` (ChatGPT sign-in).
- **"json parse failed"** → Codex returned non-JSON (read the raw preview in the log; usually transient).
- **"extracted N, posted 0"** → mem0 rejecting writes: `journalctl --user -u mem0.service -n 50`. A 413 means the fact exceeded the storage cap (facts must be atomic; the extractor prompt enforces this).
- **"no facts extracted" on a real session** → often correct: the inferability gate drops generic content. Verify against a session with genuinely project-specific facts.
- Failed posts self-heal from the dead-letter queue on the next run (`~/.claude/state/mem0-post-failures.jsonl`; poison quarantine after 5 attempts).

---

## "The nightly dream didn't run"

**Diagnose:**

```powershell
schtasks /Query /TN "ClaudeCode-DreamConsolidator-3am" /FO LIST /V | Select-String "Last Run|Last Result|Next Run"
Get-Content "$env:USERPROFILE\.claude\logs\dream.log" -Tail 40
```

**Fixes:**
- **Missed night (PC off, etc.)** → self-healing: at the next session start, `dream-catchup.ps1` re-runs a dream that's >48 h stale or has pending queues. To force one now: `dream-consolidate.ps1 -Force` (respects the shared Codex lock).
- **Task missing/broken** → re-register idempotently: rerun `install\2-windows-config.ps1`.
- **Ran but 0 insights** → often correct (no consolidation-worthy evidence). Check the log's Codex output preview.
- The MEMORY.md index refresh is decoupled (`memory-index-refresh.ps1`, 6-h throttle) — a down dream no longer freezes the index.

---

## "Codex says 'Not logged in'"

```powershell
codex login status
```
- Wrong/expired auth → `codex logout` then `codex login`, pick **Sign in with ChatGPT**.
- Extraction, the dream, and the judgment shim share one Codex lock — a stuck lock shows in logs as "codex lock held"; it stale-reclaims automatically.

---

## "MCP tools not appearing in Claude Code"

```powershell
# Registered?
(Get-Content "$env:USERPROFILE\.claude.json" -Raw | ConvertFrom-Json).mcpServers.mem0

# Shim runs? (Ctrl+C to exit; an import error = Python/venv issue).
# The shim file is deployed to the WINDOWS ~/.claude/scripts and run through the WSL venv:
$shim = (wsl -e wslpath -a ($env:USERPROFILE + "\.claude\scripts\mem0-mcp-shim.py")).Trim()
wsl -e bash -lc "~/apps/mem0-server/.venv/bin/python $shim < /dev/null"
```

- **mem0 down** → all tools fail: `systemctl --user start mem0.service`.
- **Server config changed** → MCP servers spawn at session start; restart VS Code.
- **Banner corruption** (fastmcp ANSI banner on stdout) → `mcp.run(show_banner=False)` must be set (it is, in the shipped shim).

---

## "mem0 returns 500"

```bash
systemctl --user status mem0.service
journalctl --user -u mem0.service -n 50
curl http://127.0.0.1:6333/collections/mem0_egemma_768     # Qdrant collection healthy?
curl -s http://127.0.0.1:11436/v1/models | grep -o embeddinggemma   # embedder being served?
```

- **Qdrant refused** → `systemctl --user restart qdrant.service`.
- **Embedder refused / wrong dim** → llama-swap issue: `systemctl --user restart llama-swap.service`; confirm `embeddinggemma` is in its config (see `install/llama-swap-setup.md`). First call after a cold start can be slow (model load) — `/health/deep` needs a generous timeout.
- **ImportError at startup** → venv or a missing module from `MEM0_MODULES` (a fresh-install class of bug now guarded by the import-closure test); rerun the installer.

---

## "Qdrant lost a collection / points missing"

```bash
curl http://127.0.0.1:6333/collections
curl http://127.0.0.1:6333/collections/mem0_egemma_768    # .points_count, .status
```

- **Points dropped to 0 / collection gone** → restore from the weekly snapshot: see [`data-backup.md`](./data-backup.md) and `scripts/wsl/stack-restore.sh`. Deletions by dedup/decay are individually restorable: the **full payloads** are preserved in `~/.mem0/dedup-report.jsonl` (`deleted_full_payload`) and `~/.mem0/decay-report.jsonl` (`full_payload`); the monthly tier-ledger segments (`tier-ledger-YYYY-MM.jsonl`) carry the id/reason/actor audit trail.
- **Status red** → corrupted; restore the latest Qdrant snapshot from `~/.mem0/backups/`.

---

## "Disk is filling up"

```bash
du -sh ~/.mem0 ~/qdrant-server/storage 2>/dev/null; du -sh $(wslpath "$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r')")/.claude/logs 2>/dev/null
```

- `~/.mem0/backups/` keeps the last 8 of each artifact — prune older manually if needed.
- Logs rotate automatically (1 MB, 5 archives); ledgers segment monthly.
- The SessionStart storage-cap banner warns at growth boundaries; it never auto-prunes.

---

## "I see weird memories I didn't write"

1. **L1a extracted junk** → find by source (`memory_search`, metadata `source=l1a-extractor`), delete via `memory_delete` (ledgered). Persistent junk = tighten a genuinely noisy transcript pattern, but remember the inferability gate already drops most.
2. **Poisoning via tool output** → the layered defenses (redaction, delimiter-boxed judge prompts, L10 injection-shaped flags, NLI write-gate if enabled) exist for this; check `audit-flags.jsonl` and triage. Canonical cannot be forged regardless (HMAC-gated).

---

## Full-chain smoke test

```powershell
& "$env:USERPROFILE\.claude\scripts\Test-MemoryStack.ps1"
```

Liveness (services, health, MCP registration, hooks SHA-match) + invariants (search behavior, injection gates), pass/fail per row. The install-time equivalent is `install\3-verify.ps1`.

---

## Known issues / past bugs (so you don't re-debug them)

| Symptom | Root cause | Status |
|---|---|---|
| `claude --print` from hooks: "Not logged in" | Max OAuth single-session enforcement | BY DESIGN — the stack uses Codex |
| Per-prompt injection silently dead under VS Code | stdout instead of the `hookSpecificOutput` envelope + a Windows concurrent-spawn race + a short daemon timeout | FIXED v1.11.0 (exec-form hook, envelope, 8 s timeout) |
| Fresh installs crash-loop `mem0.service` | `redact.py` imported but not deployed by the installer | FIXED v1.11.1 (+ import-closure gate in the publish pipeline) |
| Hook POSTs fail with 400 on non-ASCII | UTF-8 bytes not declared on `-Body` | FIXED (encoding sweep) |
| MCP tools time out at handshake | fastmcp ANSI banner on stdout | FIXED — `show_banner=False` |
| A correct fact vanished from retrieval | early auto-enforce hid records on a single Codex YES | FIXED — never-auto-hide + review queue + `--unstamp` |
| Sweep flags valid historical ship-logs as stale | contradiction prompt reused for the supersession question | FIXED — dedicated STALE/KEEP judge (precision 35→67%) |
| Embedder first-call timeout after idle | llama-swap cold model load | EXPECTED — retry / generous deep-health timeout |
