# Dream Consolidator - 4-phase pattern (orient -> gather -> consolidate -> prune)
# Replaces c1-consolidate.ps1 in v0.13. Ported from grandamenium/dream-skill MIT.
# Fires nightly 3am via Windows Task Scheduler. Throttled to 24h.
# Codex (gpt-5.5 medium) executes each phase; Fable designed the prompts.

param([switch]$DryRun)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# v1.0 Phase 7A: operator receipt — resolve operator-specific paths so this
# nightly consolidator is operator-agnostic (no hardcoded handle/distro/repo).
# Written by install/2-windows-config.ps1; live fallback if absent.
$DcCfgPath = Join-Path $env:USERPROFILE '.claude\scripts\mem0-stack.config.psd1'
$DcCfg = $null
try { if (Test-Path $DcCfgPath) { $DcCfg = Import-PowerShellDataFile $DcCfgPath } } catch { $DcCfg = $null }
$DcWslUser = if ($DcCfg -and $DcCfg.WslUser) { $DcCfg.WslUser } else { try { ([string](wsl.exe -e bash -lc 'printf %s "$USER"')).Trim() } catch { $env:USERNAME } }
$DcDistro  = if ($DcCfg -and $DcCfg.Distro)  { $DcCfg.Distro } else {
    $prevEnc = [Console]::OutputEncoding
    try { [Console]::OutputEncoding = [System.Text.Encoding]::Unicode; (wsl.exe -l -q | Where-Object { $_.Trim() } | Select-Object -First 1).Trim() } finally { [Console]::OutputEncoding = $prevEnc }
}
$DcRepoWsl = if ($DcCfg -and $DcCfg.RepoRootWsl) { $DcCfg.RepoRootWsl } else { '' }
$DcHomeUnc = "\\wsl.localhost\$DcDistro\home\$DcWslUser"

. (Join-Path $ScriptDir 'memory-common.ps1')
Initialize-MemoryEnv

# 24h throttle (independent of L1a 10-min throttle)
$ThrottleName = 'dream'
if (-not $DryRun -and -not (Test-Throttle -Name $ThrottleName -MinIntervalSeconds 86400)) {
    Write-MemoryLog -Component 'dream' -Message 'skipping: 24h throttle not yet elapsed'
    exit 0
}

# State dir for intermediate phase outputs (audit trail)
$DreamStateDir = Join-Path $env:USERPROFILE '.claude\state\dream'
if (-not (Test-Path $DreamStateDir)) { New-Item -ItemType Directory -Path $DreamStateDir -Force | Out-Null }

# Log rotation + DLQ drain at start
Invoke-LogRotation -MaxBytes 1MB -KeepN 5
$dlq = Drain-Mem0DeadLetter
if ($dlq.drained -gt 0 -or $dlq.remaining -gt 0) {
    Write-MemoryLog -Component 'dream' -Message "DLQ: drained $($dlq.drained), remaining $($dlq.remaining)"
}

# Shared Codex mutex (same lock as L1a - if L1a holds it, dream skips this cycle)
if (-not $DryRun -and -not (Acquire-CodexLock -Owner 'dream' -MaxAgeMinutes 30)) {
    Write-MemoryLog -Component 'dream' -Message 'skipping: codex lock held by another worker'
    exit 0
}

try {

function Save-PhaseState {
    param([string]$Phase, $Payload)
    $path = Join-Path $DreamStateDir "$Phase.json"
    $Payload | ConvertTo-Json -Depth 10 | Set-Content -Path $path -Encoding UTF8
}

function Read-MemoryMd {
    $p = "$DcHomeUnc\.mem0\MEMORY.md"
    if (Test-Path $p) { return (Get-Content $p -Raw) } else { return '' }
}

function Get-RecentInsights {
    # Pull existing tier=insight from mem0 so the consolidator doesn't restate them
    try {
        $resp = Get-Mem0Evidence -Limit 200
        $insights = @($resp.results | Where-Object { $_.metadata.tier -eq 'insight' } | ForEach-Object {
            "- [$($_.id)] $($_.memory.Substring(0, [Math]::Min(180, $_.memory.Length)))"
        })
        return ($insights -join "`n")
    } catch { return '' }
}

function Get-OpenGoalsContext {
    param([int]$Limit = 5)
    try {
        $key = (Get-Content "$DcHomeUnc\.mem0\api-key" -Raw -ErrorAction SilentlyContinue).Trim()
        if (-not $key) { return '' }
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/goals?status=open&limit=$Limit" -Headers @{'X-API-Key' = $key} -TimeoutSec 5
        if (-not $r) { return '' }
        $lines = @()
        foreach ($g in $r) {
            $brand = if ($g.brand) { [string]$g.brand } else { 'unknown' }
            $title = [string]$g.title
            $prio = if ($g.priority) { [int]$g.priority } else { 3 }
            $lines += "- [${brand}] [P${prio}] ${title}"
        }
        return ($lines -join "`n")
    } catch {
        Write-MemoryLog -Component 'dream' -Message "  Get-OpenGoalsContext failed: $_"
        return ''
    }
}

function Get-OpenQuestionsContext {
    param([int]$Limit = 5)
    try {
        $key = (Get-Content "$DcHomeUnc\.mem0\api-key" -Raw -ErrorAction SilentlyContinue).Trim()
        if (-not $key) { return '' }
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/open_questions?status=open&limit=$Limit" -Headers @{'X-API-Key' = $key} -TimeoutSec 5
        if (-not $r) { return '' }
        $lines = @()
        foreach ($q in $r) {
            $brand = if ($q.brand) { [string]$q.brand } else { 'cross-brand' }
            $text = [string]$q.question_text
            if ($text.Length -gt 120) { $text = $text.Substring(0, 120) + '...' }
            $lines += "- [${brand}] $text"
        }
        return ($lines -join "`n")
    } catch {
        Write-MemoryLog -Component 'dream' -Message "  Get-OpenQuestionsContext failed: $_"
        return ''
    }
}

function Get-BlockedGoalsContext {
    param([int]$Limit = 3)
    try {
        $key = (Get-Content "$DcHomeUnc\.mem0\api-key" -Raw -ErrorAction SilentlyContinue).Trim()
        if (-not $key) { return '' }
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/goals?status=blocked&limit=$Limit" -Headers @{'X-API-Key' = $key} -TimeoutSec 5
        if (-not $r) { return '' }
        $lines = @()
        foreach ($g in $r) {
            $brand = if ($g.brand) { [string]$g.brand } else { 'unknown' }
            $title = [string]$g.title
            $lines += "- [${brand}] ${title}"
        }
        return ($lines -join "`n")
    } catch { return '' }
}

function Get-RecentEpisodes {
    param([int]$Limit = 7)
    try {
        $key = (Get-Content "$DcHomeUnc\.mem0\api-key" -Raw -ErrorAction SilentlyContinue).Trim()
        if (-not $key) { return '' }
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/episodes?recent=$Limit" -Headers @{'X-API-Key' = $key} -TimeoutSec 5
        if (-not $r) { return '' }
        $lines = @()
        foreach ($e in $r) {
            $ended = if ($e.ended_at) { ([string]$e.ended_at).Substring(0, [Math]::Min(10, ([string]$e.ended_at).Length)) } else { '?' }
            $brand = if ($e.brand) { $e.brand } else { 'unknown' }
            $goal = if ($e.goal_text) {
                $g = $e.goal_text
                if ($g.Length -gt 130) { $g = $g.Substring(0, 130) + '...' }
                $g
            } else { '' }
            $lines += "- [$ended] ${brand}: $goal"
        }
        return ($lines -join "`n")
    } catch {
        Write-MemoryLog -Component 'dream' -Message "  Get-RecentEpisodes failed: $_"
        return ''
    }
}

function Get-Last36hTranscriptSignals {
    # Read the last 36h of Claude Code session transcripts (jsonl under
    # ~/.claude/projects/*/) and extract candidate signals. Returns plain-text
    # blob; Codex parses it in phase 2.
    $projDir = Join-Path $env:USERPROFILE '.claude\projects'
    if (-not (Test-Path $projDir)) { return '' }
    $cutoff = (Get-Date).AddHours(-36)
    $files = Get-ChildItem -Path $projDir -Filter '*.jsonl' -Recurse -ErrorAction SilentlyContinue |
             Where-Object { $_.LastWriteTime -gt $cutoff } |
             Sort-Object LastWriteTime -Descending |
             Select-Object -First 10  # cap to 10 transcripts to stay under Codex context
    $blob = New-Object System.Text.StringBuilder
    foreach ($f in $files) {
        $turns = Get-RecentTranscriptTurns -TranscriptPath $f.FullName -MaxTurns 48 -MaxChars 6000
        if ($turns) {
            $null = $blob.AppendLine("=== transcript: $($f.Name) (mtime=$($f.LastWriteTime.ToString('o'))) ===")
            $null = $blob.AppendLine($turns)
            $null = $blob.AppendLine('')
        }
    }
    return $blob.ToString()
}

# ============== PHASE 1: ORIENT ==============
Write-MemoryLog -Component 'dream' -Message '=== phase 1: orient ==='
$memorymd = Read-MemoryMd
$existingInsights = Get-RecentInsights
$recentEpisodes = Get-RecentEpisodes -Limit 7
$openGoals = Get-OpenGoalsContext -Limit 5
$blockedGoals = Get-BlockedGoalsContext -Limit 3
$openQuestions = Get-OpenQuestionsContext -Limit 5
Save-PhaseState -Phase 'orient' -Payload @{
    memorymd_chars = $memorymd.Length
    existing_insights_count = ($existingInsights -split "`n").Count
    recent_episodes_count = ($recentEpisodes -split "`n" | Where-Object { $_ -match '\S' }).Count
    open_goals_count = if ($openGoals) { ($openGoals -split "`n" | Where-Object { $_ -match '\S' }).Count } else { 0 }
    blocked_goals_count = if ($blockedGoals) { ($blockedGoals -split "`n" | Where-Object { $_ -match '\S' }).Count } else { 0 }
    open_questions_count = if ($openQuestions) { ($openQuestions -split "`n" | Where-Object { $_ -match '\S' }).Count } else { 0 }
    timestamp = (Get-Date).ToString('o')
}

if (-not (Test-Mem0Health)) {
    Write-MemoryLog -Component 'dream' -Message '  mem0 unreachable, aborting'
    return
}

# ============== PHASE 2: GATHER (with AGI-paper Information Gain bridge) ==============
Write-MemoryLog -Component 'dream' -Message '=== phase 2: gather (surprise-weighted) ==='
$transcripts = Get-Last36hTranscriptSignals
if ([string]::IsNullOrWhiteSpace($transcripts)) {
    Write-MemoryLog -Component 'dream' -Message '  no recent transcripts, skipping gather'
    return
}

$gatherPrompt = @"
You are reading the last 36h of conversation transcripts to identify HIGH-SIGNAL events for memory consolidation. Output STRICT JSON:
{"signals":[{"kind":"correction|decision|surprise|contradiction","text":"...","source_transcript":"...","priority":1-5}]}

Prioritize signals that REDUCE UNCERTAINTY or CONTRADICT existing memory (Information Gain principle):
- CORRECTIONS: user said "actually X" / "no, Y" / "wait" / "stop" / "that's wrong" - these are the strongest signals.
- DECISIONS: locked-in choices ("ok let's do X", "go with Y", "approved")
- SURPRISES: outcomes the user didn't expect (positive or negative)
- CONTRADICTIONS: claims that conflict with the existing memory index below

Existing memory index (do NOT restate; only flag what contradicts it):
$memorymd

Existing insights (do NOT restate; only update if newer evidence supersedes):
$existingInsights

Recent sessions (episodic context — use to detect goal continuity and contradictions across sessions; the Information Gain principle prefers SURPRISES vs these established goals):
$recentEpisodes

Active goals (top OPEN by priority):
$openGoals

Currently BLOCKED goals (sources of friction):
$blockedGoals

Open frontier questions (Epistemic Reachability — what we know we don't know):
$openQuestions

PRIORITY: surprises in the transcripts that RESOLVE an open question are HIGHEST signal. Treat them as the strongest source of insight.

VALUE IMPROVEMENT priority: surprises that ADVANCE the OPEN goals or UNBLOCK the BLOCKED goals are HIGHEST signal. Corrections that CONTRADICT an existing goal's premise are also HIGHEST signal. Treat goal continuity across sessions as Information Gain when the current session moves a goal forward or reveals a block.

Rules:
- Max 8 signals. Drop low-priority ones first.
- Each signal: <=30 words, self-contained, declarative.
- priority 5 = corrects an existing canonical/insight; priority 1 = generic confirmation.
- If nothing surprising/correcting/deciding happened: {"signals":[]}.

Transcripts:
$transcripts
"@

$gatherStart = Get-Date
$gatherRaw = $null
try { $gatherRaw = Invoke-CodexSubagent -Prompt $gatherPrompt -ReasoningEffort 'medium' -TimeoutSeconds 180 }
catch { Write-MemoryLog -Component 'dream' -Message "  gather codex failed: $_"; return }
$gatherDurationMs = [int]((Get-Date) - $gatherStart).TotalMilliseconds
$gatherTokens = Parse-CodexTokenUsage -RawOutput $gatherRaw

$gatherText = Get-CodexResponseText -RawOutput $gatherRaw
$gatherParsed = Extract-JsonFromText -Text $gatherText -ExpectedKey 'signals'
$signals = if ($gatherParsed) { @($gatherParsed.signals) } else { @() }
Save-PhaseState -Phase 'gather' -Payload @{ signals = $signals; codex_ms = $gatherDurationMs; tokens = $gatherTokens }
Write-MemoryLog -Component 'dream' -Message "  gathered $($signals.Count) signals (codex ${gatherDurationMs}ms, $gatherTokens tokens)"

if ($signals.Count -eq 0) {
    Write-MemoryLog -Component 'dream' -Message '  no signals; nothing to consolidate'
    Mark-Throttle -Name $ThrottleName
    return
}

# v0.13.1: cross-process mutex with semantic-dedup. If dedup is mid-deletion, skip this cycle
# to avoid posting insights with source_memory_ids that point to records dedup is about to delete.
$dedupLock = "$DcHomeUnc\.mem0\dedup.lock"
if (Test-Path $dedupLock) {
    $age = (Get-Date) - (Get-Item $dedupLock).LastWriteTime
    if ($age.TotalMinutes -lt 30) {
        Write-MemoryLog -Component 'dream' -Message "  semantic-dedup mutex held (lock $([int]$age.TotalMinutes)min old), skipping consolidate phase"
        Mark-Throttle -Name $ThrottleName
        return
    }
}

# ============== PHASE 3: CONSOLIDATE ==============
Write-MemoryLog -Component 'dream' -Message '=== phase 3: consolidate ==='
$signalBullets = ($signals | ForEach-Object { "- [$($_.kind) p$($_.priority)] $($_.text)" }) -join "`n"

# Pull last 30 evidence-tier writes for grounding source_memory_ids
$evResp = Get-Mem0Evidence -Limit 100
$evidence = @($evResp.results | Where-Object { $_.metadata.tier -in @('evidence', $null) } | Select-Object -First 30)
$evidenceBullets = ($evidence | ForEach-Object { "- [$($_.id)] $($_.memory.Substring(0, [Math]::Min(180, $_.memory.Length)))" }) -join "`n"
$evidenceIds = @($evidence | ForEach-Object { $_.id })

$consolidatePrompt = @"
You are a memory consolidator. Synthesize 1-3 INSIGHTS that emerge from BOTH the surprise/correction signals below AND the recent evidence. Output STRICT JSON:
{"insights":[{"text":"...","source_signal_indexes":[0,2],"source_memory_ids":["..."],"confidence":0.7}]}

Rules:
- Each insight: <=40 words, declarative, durable.
- An insight must integrate >=1 signal AND >=1 evidence id (lineage matters).
- Resolve contradictions: newer evidence + corrections WIN over older statements.
- If nothing crosses the bar of "more than the sum of its parts": {"insights":[]}.

Surprise/correction signals (priority-weighted):
$signalBullets

Recent evidence (use ids in source_memory_ids):
$evidenceBullets
"@

$consolidateStart = Get-Date
try { $consolidateRaw = Invoke-CodexSubagent -Prompt $consolidatePrompt -ReasoningEffort 'medium' -TimeoutSeconds 180 }
catch { Write-MemoryLog -Component 'dream' -Message "  consolidate codex failed: $_"; return }
$consolidateMs = [int]((Get-Date) - $consolidateStart).TotalMilliseconds
$consolidateTokens = Parse-CodexTokenUsage -RawOutput $consolidateRaw

$consolidateText = Get-CodexResponseText -RawOutput $consolidateRaw
$consolidateParsed = Extract-JsonFromText -Text $consolidateText -ExpectedKey 'insights'
if ($null -eq $consolidateParsed) {
    Write-MemoryLog -Component 'dream' -Message "  consolidate: malformed JSON from codex; skipping throttle mark"
    $preview = if ($consolidateText.Length -gt 300) { $consolidateText.Substring(0, 300) } else { $consolidateText }
    Write-MemoryLog -Component 'dream' -Message "  preview: $preview"
    return
}
$insights = @($consolidateParsed.insights)
Save-PhaseState -Phase 'consolidate' -Payload @{ insights = $insights; codex_ms = $consolidateMs; tokens = $consolidateTokens }

$posted = 0
if ($insights.Count -gt 0 -and -not $DryRun) {
    foreach ($ins in $insights) {
        if ([string]::IsNullOrWhiteSpace($ins.text)) { continue }
        # Resolve any short prefixes returned by the LLM back to full UUIDs.
        # The prompt shows full UUIDs in the bullet list (v0.14.1 fix) but be defensive: if the
        # LLM truncates anyway, accept any prefix that uniquely matches an evidence id.
        $resolvedLineage = @()
        foreach ($rawId in $ins.source_memory_ids) {
            if (-not $rawId) { continue }
            $idStr = [string]$rawId
            # Match: exact full-UUID hit OR unique-prefix hit
            $matched = @($evidenceIds | Where-Object { $_ -eq $idStr -or $_.StartsWith($idStr) })
            if ($matched.Count -eq 1) {
                $resolvedLineage += $matched[0]
            } elseif ($matched.Count -gt 1) {
                Write-MemoryLog -Component 'dream' -Message "  lineage: prefix $idStr matched $($matched.Count) evidence ids (ambiguous, dropping)"
            } else {
                Write-MemoryLog -Component 'dream' -Message "  lineage: id $idStr matched no evidence in this window (dropping)"
            }
        }
        $lineage = $resolvedLineage
        if ($lineage.Count -eq 0) { $lineage = $evidenceIds | Select-Object -First 5 }
        $ok = Add-Mem0Memory -Text $ins.text -Source 'dream-consolidator' -Metadata @{
            tier = 'insight'
            category = 'insight'
            confidence = ($ins.confidence -as [double])
            source_memory_ids = $lineage
            window_evidence_count = $evidence.Count
            window_signal_count = $signals.Count
            consolidated_at = (Get-Date).ToString('o')
            dream_phase = 'consolidate'
        }
        if ($ok) { $posted++ }
        # v0.13.1: stamp source evidence with touched_by_dream so decay-scan's protection isn't dead code
        if ($ok -and $lineage.Count -gt 0) {
            $patchKey = (Get-Content "$DcHomeUnc\.mem0\api-key" -Raw -ErrorAction SilentlyContinue).Trim()
            if ($patchKey) {
                foreach ($sourceMid in $lineage) {
                    try {
                        $patchBody = @{
                            metadata = @{ touched_by_dream = (Get-Date).ToString('o') }
                            actor = 'dream-consolidator'
                            reason = "cited as source_memory_id by insight"
                        } | ConvertTo-Json -Depth 4
                        Invoke-RestMethod -Method Patch -Uri "http://127.0.0.1:18791/v1/memories/$sourceMid/metadata" -Headers @{'X-API-Key'=$patchKey; 'Content-Type'='application/json'} -Body $patchBody -TimeoutSec 5 | Out-Null
                    } catch {
                        # Best-effort - don't abort the cycle if a single PATCH fails
                        Write-MemoryLog -Component 'dream' -Message "  PATCH touched_by_dream failed for ${sourceMid}: $_"
                    }
                }
            }
        }
    }
}
Write-MemoryLog -Component 'dream' -Message "  consolidated $($insights.Count) insights, posted $posted (codex ${consolidateMs}ms, $consolidateTokens tokens)"

# ============== PHASE 4: PRUNE & INDEX ==============
Write-MemoryLog -Component 'dream' -Message '=== phase 4: prune & index ==='
$indexExit = 0
if (-not $DryRun -and -not $DcRepoWsl) {
    Write-MemoryLog -Component 'dream' -Message '  skip index-build: no RepoRootWsl in receipt (run install to write ~/.claude/scripts/mem0-stack.config.psd1)'
    Mark-Throttle -Name $ThrottleName
} elseif (-not $DryRun) {
    $indexResult = wsl.exe -d $DcDistro -e bash -c "/home/$DcWslUser/apps/mem0-server/.venv/bin/python $DcRepoWsl/scripts/wsl/memory-index-build.py 2>&1"
    $indexExit = $LASTEXITCODE
    Write-MemoryLog -Component 'dream' -Message "  $indexResult"
    if ($indexExit -ne 0) {
        Write-MemoryLog -Component 'dream' -Message "  index build failed (exit=$indexExit); throttle NOT marked"
        return
    }
    Mark-Throttle -Name $ThrottleName
}
Save-PhaseState -Phase 'prune' -Payload @{
    posted_insights = $posted
    index_rebuilt = ($indexExit -eq 0)
    index_exit_code = $indexExit
    timestamp = (Get-Date).ToString('o')
}

# Token + duration totals
Write-CodexUsageLog -Component 'dream' `
    -TokensUsed (($gatherTokens -as [int]) + ($consolidateTokens -as [int])) `
    -DurationMs ($gatherDurationMs + $consolidateMs) `
    -Status 'ok' `
    -FactsPosted $posted

Write-MemoryLog -Component 'dream' -Message "=== dream cycle done (DryRun=$DryRun) ==="

} finally {
    if (-not $DryRun) { Release-CodexLock }
}
exit 0
