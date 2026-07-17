# 3-verify.ps1 - end-to-end smoke test of the agentic memory stack
# Exits nonzero if anything is broken. Tells you what.

param(
    [Parameter(Mandatory)][string]$WslUser,
    # v1.0 Phase 7A: operator-agnostic. Passed by the orchestrator; auto-detect if standalone.
    [string]$Distro = ''
)

$ErrorActionPreference = 'Continue'
if (-not $Distro) {
    $prevEnc = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::Unicode
        $Distro = (wsl.exe -l -q | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
    } finally { [Console]::OutputEncoding = $prevEnc }
}
$fails = @()
$warns = @()

function Check {
    param([string]$Name, [scriptblock]$Test, [string]$FixHint)
    Write-Host -NoNewline "  $Name ... "
    try {
        $r = & $Test
        if ($r) { Write-Host "OK" -ForegroundColor Green }
        else { Write-Host "MISSING" -ForegroundColor Red; $script:fails += "$Name : $FixHint" }
    } catch {
        Write-Host "FAIL ($_)" -ForegroundColor Red
        $script:fails += "$Name : $FixHint"
    }
}

Write-Host ""
Write-Host "WSL services reachable from Windows (mirrored networking):"
Check "Qdrant :6333" { try { (Invoke-RestMethod -Uri 'http://127.0.0.1:6333/healthz' -TimeoutSec 3) -ne $null } catch { $false } } "wsl: systemctl --user status qdrant.service"
Check "mem0 :18791" { try { (Invoke-RestMethod -Uri 'http://127.0.0.1:18791/health' -TimeoutSec 3).ok } catch { $false } } "wsl: systemctl --user status mem0.service"
# v0.22 EmbeddingGemma migration: mem0's embedder is EmbeddingGemma-300m on llama-swap
# :11436 (single-stack llama.cpp). Ollama fully decommissioned 2026-06-13 — no longer
# a stack dependency. This verifies the embedder returns a 768-dim vector.
Check "EmbeddingGemma :11436" { try { $b = @{model='embeddinggemma'; input='title: none | text: ping'} | ConvertTo-Json; (@((Invoke-RestMethod -Uri 'http://127.0.0.1:11436/v1/embeddings' -Method Post -Body $b -ContentType 'application/json' -TimeoutSec 20).data[0].embedding).Count -eq 768) } catch { $false } } "wsl: systemctl --user status llama-swap.service"

Write-Host ""
Write-Host "Windows-side files + config:"
Check "Runtime scripts present" { @('memory-common.ps1','l1a-extract.ps1','dream-consolidate.ps1','stop-extract.ps1','mem0-mcp-shim.py','storage-cap-check.sh','sessionstart_bundle.py','precompact_capture.py','user-prompt-extract.ps1','user-prompt-lib.ps1','pre-tool-check.ps1','mem0-hook-daemon.ps1','mem0-hook-daemon-spawn.ps1','mem0-hook-client.cs','build-hook-client.ps1') | ForEach-Object { Test-Path "$env:USERPROFILE\.claude\scripts\$_" } | Where-Object { $_ -eq $false } | Measure-Object | ForEach-Object { $_.Count -eq 0 } } "Re-run 2-windows-config.ps1"
Check "Stop hook registered" { $s = Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw | ConvertFrom-Json; ($s.hooks.Stop[0].hooks[0].command -match 'stop-extract.ps1') } "Re-run 2-windows-config.ps1"
Check "PreCompact hook registered" { $s = Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw | ConvertFrom-Json; ($s.hooks.PreCompact[0].hooks[0].command -match 'stop-extract.ps1') } "Re-run 2-windows-config.ps1"
# v0.20 Final (adversarial-review HIGH): the A.5/A.6 accelerated chain is the
# production shape — verify the exe was built+installed, that UserPromptSubmit
# points at it (exactly ONE stack-owned entry: the dedupe must not have left a
# legacy wrapper entry beside it), and that the SessionStart daemon-spawn
# launcher is registered.
Check "mem0-hook-client.exe built + installed" { Test-Path "$env:USERPROFILE\.claude\scripts\mem0-hook-client.exe" } "Run scripts\windows\build-hook-client.ps1 (compiles + smoke-gates + installs), then re-run 2-windows-config.ps1"
Check "UserPromptSubmit registered to compiled client (exactly one stack entry)" {
    $s = Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw | ConvertFrom-Json
    $cmds = @($s.hooks.UserPromptSubmit | ForEach-Object { $_.hooks } | ForEach-Object { $_.command })
    $ours = @($cmds | Where-Object { $_ -like '*mem0-hook-client*' -or $_ -like '*user-prompt-extract.ps1*' })
    ($ours.Count -eq 1) -and ($ours[0] -like '*mem0-hook-client.exe*')
} "Re-run 2-windows-config.ps1 (registers the exe and dedupes both legacy + exe shapes)"
Check "SessionStart daemon-spawn registered" {
    $s = Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw | ConvertFrom-Json
    @($s.hooks.SessionStart | ForEach-Object { $_.hooks } | ForEach-Object { $_.command }) -like '*mem0-hook-daemon-spawn.ps1*'
} "Re-run 2-windows-config.ps1"
Check "mem0 MCP server registered" { $m = Get-Content "$env:USERPROFILE\.claude.json" -Raw | ConvertFrom-Json; $m.mcpServers.mem0 -ne $null } "Re-run 2-windows-config.ps1"
# v1.16 (2026-07-17 remediation §6.2.3): generic deploy-layer skew detector. The shared/synced
# settings.json can advance ahead of this box's machine-local deployed scripts (2026-07-17:
# a config-repo untrack+pull deleted a box's whole deploy layer while settings.json kept
# referencing it — the missing PreCompact script deadlocked live sessions). Every
# ~/.claude/scripts/<file> referenced ANYWHERE in settings.json must exist on disk
# (deliberately broader than hook commands — over-detection is the safe direction here).
Check "No hook references a missing deployed script (skew guard)" {
    $raw = Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw
    $refs = [regex]::Matches($raw, '(?i)[\\/]\.claude[\\/]+scripts[\\/]+([A-Za-z0-9_.-]+)') |
        ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique
    $missing = @($refs | Where-Object { -not (Test-Path "$env:USERPROFILE\.claude\scripts\$_") })
    if ($missing.Count -gt 0) { Write-Host "(missing: $($missing -join ', ')) " -NoNewline -ForegroundColor Yellow }
    $missing.Count -eq 0
} "Deploy layer is skewed vs settings.json - re-run 2-windows-config.ps1 (and check ~/.claude git history for an untrack/clean that removed deployed scripts)"
# v1.16 one-brain role gate (§6.3): the receipt records this box's role. brain -> both
# nightly tasks must be registered; replica -> both must be ABSENT (a replica running
# consolidation/dedup mutates the one shared brain destructively).
$stackRole = 'brain'
$receiptFile = "$env:USERPROFILE\.claude\scripts\mem0-stack.config.psd1"
if (Test-Path $receiptFile) {
    try { $r = Import-PowerShellDataFile $receiptFile; if ($r.Role) { $stackRole = $r.Role } } catch {}
}
if ($stackRole -eq 'brain') {
Check "Task Scheduler 3am dream-consolidate" {
    $t = Get-ScheduledTask -TaskName 'ClaudeCode-DreamConsolidator-3am' -ErrorAction SilentlyContinue
    $t -ne $null -and $t.Actions[0].Arguments -match 'dream-consolidate\.ps1'
} "Re-run 2-windows-config.ps1"
Check "Task Scheduler 4:30am semantic-dedup" {
    $t = Get-ScheduledTask -TaskName 'ClaudeCode-SemanticDedup-430am' -ErrorAction SilentlyContinue
    $t -ne $null -and $t.Actions[0].Arguments -match 'semantic-dedup\.py'
} "Re-run 2-windows-config.ps1"
} else {
Check "Replica role: dream/dedup tasks absent (one-brain rule)" {
    $dream = Get-ScheduledTask -TaskName 'ClaudeCode-DreamConsolidator-3am' -ErrorAction SilentlyContinue
    $dedup = Get-ScheduledTask -TaskName 'ClaudeCode-SemanticDedup-430am' -ErrorAction SilentlyContinue
    ($null -eq $dream) -and ($null -eq $dedup)
} "A read-replica must not run nightly canonical mutations - re-run 2-windows-config.ps1 -Role replica (it unregisters them)"
}
Check "canonical-key exists (DPAPI blob or plaintext mode 600)" {
    # v0.20 Phase D (M9): post-Phase-H a DPAPI box has ONLY the .dpapi blob —
    # the old plaintext-only check false-failed there and its remediation
    # (generate a fresh key) would have split-brained the key from the blob.
    $result = wsl.exe -d $Distro -e bash -lc "if [ -f ~/.mem0/canonical-key.dpapi ]; then echo dpapi; elif [ -f ~/.mem0/canonical-key ]; then stat -c '%a' ~/.mem0/canonical-key 2>/dev/null; else echo missing; fi"
    @('dpapi','600') -contains (($result -as [string]).Trim())
} "No DPAPI blob: in WSL run bash scripts/wsl/generate-canonical-key.sh. DPAPI box (canonical-key.dpapi exists): systemctl --user restart mem0 (re-runs dpapi-fetch-key ExecStartPre) or follow docs/modular/dpapi-canonical-key.md Recovery - do NOT generate a fresh key next to the blob"
Check "decay-scan.timer enabled" {
    $r = wsl.exe -d $Distro -e bash -lc "systemctl --user is-enabled decay-scan.timer 2>/dev/null || echo disabled"
    ($r -as [string]).Trim() -eq 'enabled'
} "In WSL: systemctl --user enable --now decay-scan.timer"
Check "stack-backup.timer enabled" {
    $r = wsl.exe -d $Distro -e bash -lc "systemctl --user is-enabled stack-backup.timer 2>/dev/null || echo disabled"
    ($r -as [string]).Trim() -eq 'enabled'
} "In WSL: systemctl --user enable --now stack-backup.timer"

Write-Host ""
Write-Host "Codex CLI (subagent LLM) smoke test:"
Check "Codex headless call works" {
    try {
        $out = "Reply with exactly: ok" | & "$env:USERPROFILE\AppData\Roaming\npm\codex.cmd" exec --skip-git-repo-check -c model_reasoning_effort='"low"' - 2>&1
        ($out -join "`n") -match '(?ms)codex\s+ok'
    } catch { $false }
} "Run: codex login   (pick 'Sign in with ChatGPT'). Upgrade if needed: npm i -g @openai/codex@latest"

Write-Host ""
Write-Host "mem0 end-to-end (add -> search):"
Check "mem0 add+search round-trip" {
    try {
        $key = wsl.exe -d $Distro -e bash -c "cat /home/$WslUser/.mem0/api-key"
        $key = ($key -as [string]).Trim()
        $body = @{ messages = 'smoke-test memory: agentic memory stack verify timestamp ' + (Get-Date -Format o); user_id = 'verify-test'; infer = $false; metadata = @{ source = 'install-verify'; tier = 'evidence' } } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories' -Method Post -Headers @{'X-API-Key' = $key; 'Content-Type' = 'application/json'} -Body $body -TimeoutSec 15 | Out-Null
        $searchBody = @{ query = 'smoke-test memory'; filters = @{ user_id = 'verify-test' }; top_k = 1; threshold = 0.1 } | ConvertTo-Json -Compress
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories/search' -Method Post -Headers @{'X-API-Key' = $key; 'Content-Type' = 'application/json'} -Body $searchBody -TimeoutSec 15
        $r.results.Count -ge 1
    } catch { $false }
} "Check mem0 server logs: wsl -d $Distro -e bash -c 'journalctl --user -u mem0.service -n 30'"

Write-Host ""
if ($fails.Count -eq 0) {
    Write-Host "ALL VERIFY CHECKS PASSED." -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. Restart VS Code / Claude Code so the new hooks + MCP servers load"
    Write-Host "  2. Use Claude Code normally - L1a fires automatically on Stop/PreCompact hooks (10-min throttle)"
    Write-Host "  3. First dream-consolidate nightly run fires at 3:00 AM tomorrow (Task Scheduler with WakeToRun)"
    Write-Host "  4. Use the MCP tools: mcp__mem0__memory_search, memory_add, memory_promote, memory_demote, etc."
    Write-Host "  5. To promote a memory to tier=canonical, use: bash scripts/wsl/mem0-canonize.sh <id> '<reason>'"
    exit 0
} else {
    Write-Host "VERIFY FAILED - $($fails.Count) issues:" -ForegroundColor Red
    foreach ($f in $fails) { Write-Host "  - $f" -ForegroundColor Yellow }
    exit 1
}
