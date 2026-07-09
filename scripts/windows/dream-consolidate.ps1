# Dream Consolidator - 4-phase pattern (orient -> gather -> consolidate -> prune)
# Replaces c1-consolidate.ps1 in v0.13. Ported from grandamenium/dream-skill MIT.
# Fires nightly 3am via Windows Task Scheduler. Throttled to 24h.
# Codex (gpt-5.5 medium) executes each phase; Fable designed the prompts.

param([switch]$DryRun, [switch]$Force)

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
. (Join-Path $ScriptDir 'autopromote-lib.ps1')
Initialize-MemoryEnv

# 24h throttle (independent of L1a 10-min throttle). -Force bypasses ONLY this throttle
# check (for a manual /dream-now); the Codex lock below still applies so a -Force run can
# never collide with the nightly task or an in-flight L1a.
$ThrottleName = 'dream'
if (-not $DryRun -and -not $Force -and -not (Test-Throttle -Name $ThrottleName -MinIntervalSeconds 86400)) {
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

# ── Phase 5 anti-drift: retrieval-drift canary snapshot (ZERO Codex) ──────────
# Queries live mem0 (local EmbeddingGemma+Qdrant) for whether a fixed set of canary
# self-facts is retrievable, BEFORE vs AFTER the consolidation. Advisory only: it NEVER
# throws and NO-OPs under -DryRun or when the receipt lacks RepoRootWsl. The Python reads
# the search key from ~/.mem0/api-key itself (read_api_key_fallback), so no key wiring here.
# Mirrors the memory-index-build.py invocation (same venv python + -d $DcDistro bash -c).
function Invoke-DriftSnapshot {
    param([string]$Phase, [string]$OutWsl)  # Phase = 'before' | 'after'
    if ($DryRun -or [string]::IsNullOrWhiteSpace($DcRepoWsl)) { return $null }
    try {
        $py     = "/home/$DcWslUser/apps/mem0-server/.venv/bin/python"
        $script = "$DcRepoWsl/eval/retrieval-drift/retrieval_drift.py"
        # rm the prior file FIRST so a FAILED snapshot (mem0 down / bad key) cannot leave a STALE
        # file from an earlier night that compare would then treat as this cycle's truth (false
        # alarm). The CLI writes --out only on success (exit 0); require exit 0 before trusting it.
        $out = wsl.exe -d $DcDistro -e bash -c "rm -f '$OutWsl'; $py $script snapshot --out '$OutWsl' 2>&1"
        $snapExit = $LASTEXITCODE
        Write-MemoryLog -Component 'dream' -Message "  drift $Phase snapshot (exit=$snapExit): $out"
        if ($snapExit -ne 0) {
            Write-MemoryLog -Component 'dream' -Message "  drift $Phase snapshot FAILED (exit=$snapExit) -- skipping drift compare this cycle (no false alarm)"
            return $null
        }
        return $OutWsl
    } catch {
        Write-MemoryLog -Component 'dream' -Message "  drift $Phase snapshot FAILED (non-fatal): $_"
        return $null
    }
}

# ── 4C PROMOTION GATE — live orchestration ────────────────────────────────────
# Get-PromotionGateVerdict was MOVED to autopromote-lib.ps1 (2026-06-23, E-audit MED: it
# had zero unit coverage). It now lives with its pure 4C helpers and is unit-tested there
# with mocked Qdrant/Codex (DreamGateVerdict.Tests.ps1). autopromote-lib.ps1 is dot-sourced
# above (line ~26), so the function is in scope here; it still calls Invoke-CodexSubagent /
# Get-CodexResponseText / Parse-CodexTokenUsage (from memory-common.ps1, dot-sourced above)
# at runtime. No behaviour change — pure relocation.

# ── 4C PROMOTION GATE — shadow calibration log (fail-open JSONL) ──────────────
# One line per gated nominee to ~/.mem0/promotion-gate.jsonl (UNC). Read by the
# calibration step that sets the corroboration threshold before Stage 2 (enforce).
function Write-PromotionGateLog {
    param([object]$Verdict, [string]$Mode, [bool]$DryRunFlag)
    try {
        $logPath = Join-Path $DcHomeUnc '.mem0\promotion-gate.jsonl'
        $rec = [ordered]@{
            ts                   = (Get-Date).ToString('o')
            schema_version       = 'pg-v1'
            mode                 = $Mode
            dry_run              = [bool]$DryRunFlag
            memory_id            = [string]$Verdict.memoryId
            candidate_preview    = [string]$Verdict.candidatePreview
            source               = [string]$Verdict.source
            source_class         = [string]$Verdict.sourceClass
            sibling_count        = [int]$Verdict.siblingCount
            sibling_threshold    = [double]$Verdict.siblingThreshold
            was_reobserved       = [bool]$Verdict.wasReObserved
            corroboration_count  = [int]$Verdict.corroborationCount
            near_canonical_count = [int]$Verdict.nearCanonicalCount
            contradicts          = [bool]$Verdict.contradicts
            contradiction_parsed = [bool]$Verdict.contradictionParsed
            gate_promote         = [bool]$Verdict.gate.promote
            gate_class           = [string]$Verdict.gate.gateClass
            gate_reason          = [string]$Verdict.gate.reason
            codex_ms             = $Verdict.codexMs
            codex_tokens         = $Verdict.codexTokens
        }
        $line = $rec | ConvertTo-Json -Compress -Depth 5
        $dir = Split-Path -Parent $logPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        # No-BOM UTF-8 append: PS5.1 Add-Content -Encoding UTF8 prepends a BOM that breaks the
        # line-by-line JSONL parser the calibration step relies on (codebase convention —
        # mirrors user-prompt-lib's WriteAllText). AppendAllText creates the file BOM-less.
        [System.IO.File]::AppendAllText($logPath, $line + "`n", (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        Write-MemoryLog -Component 'dream' -Message "  autopromote: GATE log append failed (non-fatal): $_"
    }
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

# Phase 5 anti-drift: BEFORE snapshot of canary retrievability. Placed AFTER the signals==0
# and dedup-lock early-returns so it runs ONLY on nights that actually consolidate. $driftBefore
# is $null under -DryRun / no-RepoRootWsl, which skips the AFTER+compare block below.
$driftBefore = Invoke-DriftSnapshot -Phase 'before' -OutWsl '/tmp/dream-drift-before.json'

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
                        # v1.12 F1: PS 5.1 sends a STRING -Body as Latin-1 (non-ASCII -> 400); send UTF-8 BYTES.
                        Invoke-RestMethod -Method Patch -Uri "http://127.0.0.1:18791/v1/memories/$sourceMid/metadata" -Headers @{'X-API-Key'=$patchKey; 'Content-Type'='application/json'} -Body ([System.Text.Encoding]::UTF8.GetBytes($patchBody)) -TimeoutSec 5 | Out-Null
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

# ============== PHASE 3.5: AUTONOMOUS CANONICAL PROMOTION ==============
# Phase 2 (2026-06-19): Codex nominates canonical-worthy evidence memories under a
# STRICT precision-first bar; the consolidator promotes up to 3/night via
# mem0-canonize.sh --actor dream-autopromote.
# DryRun makes zero promotions and zero file writes; nominees are logged only.
Write-MemoryLog -Component 'dream' -Message '=== phase 3.5: autonomous canonical promotion ==='

# ── Gather candidates: recent durable evidence + current canonical set ──
$promoteEvidenceResp = Get-Mem0Evidence -Limit 200
$promoteEvidence = @($promoteEvidenceResp.results | Where-Object {
    $_.metadata.tier -notin @('canonical', 'insight') -and
    -not [string]::IsNullOrWhiteSpace($_.memory)
})

# Fetch existing canonical facts for dedup (normalize for overlap check)
$canonicalResp = $null
$canonicalFacts = @()
$canonicalNorm  = @()
try {
    $ckeyForFetch = (Get-Content "$DcHomeUnc\.mem0\api-key" -Raw -ErrorAction SilentlyContinue).Trim()
    if ($ckeyForFetch) {
        # FIX 6: filter-only fetch (no semantic query) so the COMPLETE canonical
        # set is returned, not just a ranked-200 relevance slice.
        # A4a (2026-06-22): mem.search REQUIRES a scope key in filters (user_id/agent_id/
        # run_id) — without it the server 500s and this whole fetch was silently caught as
        # "non-fatal", leaving $canonicalNorm EMPTY so the canonical-dedup guard never ran.
        # Add the runtime user scope ($DcWslUser, line 17; no sentinels in this file).
        # v1.12 F1: PS 5.1 sends a STRING -Body as Latin-1 (non-ASCII -> 400); send UTF-8 BYTES.
        $canonicalResp = Invoke-RestMethod -Uri "http://127.0.0.1:18791/v1/memories/search" `
            -Method Post `
            -Headers @{'X-API-Key' = $ckeyForFetch; 'Content-Type' = 'application/json'} `
            -Body ([System.Text.Encoding]::UTF8.GetBytes((@{ query = ''; filters = @{ tier = 'canonical'; user_id = $DcWslUser }; limit = 1000 } | ConvertTo-Json -Depth 4 -Compress))) `
            -TimeoutSec 10
        if ($canonicalResp -and $canonicalResp.results) {
            $canonicalFacts = @($canonicalResp.results | ForEach-Object { $_.memory })
            $canonicalNorm  = @($canonicalFacts | ForEach-Object { ($_ -replace '\s+', ' ').ToLower().Trim() })
        }
    }
} catch {
    Write-MemoryLog -Component 'dream' -Message "  autopromote: canonical fetch failed (non-fatal): $_"
}

$promoteEvidenceBullets = ($promoteEvidence | Select-Object -First 50 | ForEach-Object {
    "- [$($_.id)] $($_.memory.Substring(0, [Math]::Min(160, $_.memory.Length)))"
}) -join "`n"

$promotePrompt = @"
You are a precision-first memory curator. Review the evidence memories below and nominate ONLY those that meet ALL of the following strict criteria for promotion to canonical (ground-truth) tier:

PROMOTE ONLY IF the memory is:
1. EVERGREEN — will still be true in a year (no time-bound or session-specific content)
2. DECLARATIVE — a fact, operator preference, locked decision, or environment invariant (never a task, status update, ship log, or action item)
3. GROUND-TRUTH — operator-stated or a verifiable environment fact (not speculation or inference)
4. CROSS-SESSION — relevant beyond the current session or project
5. HIGH-CONFIDENCE — you are certain it belongs in the authority tier

NEVER nominate: tasks, ship logs, status updates, transient debugging notes, brand/voice content, speculation, anything imperative (rules/orders), anything already in the canonical set, or anything where you are unsure.

PRECISION OVER RECALL: when in doubt, omit. A missed nomination is cheap (it will be re-reviewed tomorrow); a wrong authority-write is not.

Return STRICT JSON only — no prose, no markdown:
[{"memory_id":"<id>","reason":"<why this is canonical: <=30 words>","confidence":<0.0-1.0>}]

If nothing meets the bar, return: []

Evidence memories to evaluate:
$promoteEvidenceBullets

Existing canonical facts (do NOT re-nominate anything that duplicates these):
$($canonicalFacts | Select-Object -First 30 | ForEach-Object { "- $_" } | Out-String)
"@

$promoteRaw      = $null
$promoteStart    = Get-Date
$codexWasCalled  = $false

if ($promoteEvidence.Count -eq 0) {
    Write-MemoryLog -Component 'dream' -Message '  autopromote: no evidence candidates, skipping Codex'
} else {
    $codexWasCalled = $true
    try {
        $promoteRaw = Invoke-CodexSubagent -Prompt $promotePrompt -ReasoningEffort 'medium' -TimeoutSeconds 180
    } catch {
        Write-MemoryLog -Component 'dream' -Message "  autopromote: Codex call failed (non-fatal): $_"
        $promoteRaw = $null
    }
}

# FIX 3: only record duration when Codex was actually called; $null otherwise.
$promoteDurationMs = if ($codexWasCalled) { [int]((Get-Date) - $promoteStart).TotalMilliseconds } else { $null }

# ── Codex JSON text (passed to Invoke-AutopromoteDecision) ───────────────────
$promoteCodexJson = $null
$promoteCodexFailed = $false
if ($codexWasCalled) {
    if ($promoteRaw) {
        $promoteCodexJson = Get-CodexResponseText -RawOutput $promoteRaw
    } else {
        $promoteCodexFailed = $true
    }
}

# ── Complete nomination pipeline (parse → structural-filter → cap → dedup) ───
# Invoke-AutopromoteDecision is defined in autopromote-lib.ps1 (dot-sourced above).
$decisionResult = Invoke-AutopromoteDecision `
    -CodexJson       $promoteCodexJson `
    -CodexFailed     $promoteCodexFailed `
    -EvidenceMemories $promoteEvidence `
    -CanonicalNorm   $canonicalNorm `
    -DryRun          $DryRun
foreach ($logLine in $decisionResult.logs) {
    Write-MemoryLog -Component 'dream' -Message "  $logLine"
}
$survivingNominees = @($decisionResult.survivingNominees)
$dedupedNominees   = @($decisionResult.dedupedNominees)
$overCapNominees   = @($decisionResult.overCapNominees)

# FIX 1: guard state write behind -not $DryRun; in DryRun log the summary instead.
if (-not $DryRun) {
    Save-PhaseState -Phase 'promote' -Payload @{
        candidates    = $promoteEvidence.Count
        nominated     = ($survivingNominees.Count + $dedupedNominees.Count + $overCapNominees.Count)
        surviving     = $survivingNominees.Count
        deduped       = $dedupedNominees.Count
        over_cap      = $overCapNominees.Count
        codex_ms      = $promoteDurationMs  # FIX 3: $null when Codex not called
        dry_run       = $false
        timestamp     = (Get-Date).ToString('o')
    }
} else {
    Write-MemoryLog -Component 'dream' -Message "  autopromote: DryRun=true -- phase state not written (candidates=$($promoteEvidence.Count) surviving=$($survivingNominees.Count) deduped=$($dedupedNominees.Count) over_cap=$($overCapNominees.Count))"
}

# ── Promote surviving nominees (non-DryRun only; DryRun already logged by Invoke-AutopromoteDecision) ──
$promotedCount    = 0
$promoteFailed    = 0
$gateBlockedCount = 0   # 4C: nominees a gate ENFORCE verdict kept out of canonical
$gateCodexTokens  = 0   # 4C: cloud tokens spent by the gate's contradiction judge (cost accounting)
$promoteSummary   = @()  # for MORNING-SUMMARY section

foreach ($nom in $survivingNominees) {
    $evidenceRec   = $promoteEvidence | Where-Object { $_.id -eq $nom.memory_id } | Select-Object -First 1
    $candidateText = if ($evidenceRec) { [string]$evidenceRec.memory } else { '' }
    $shortText     = $candidateText.Substring(0, [Math]::Min(120, $candidateText.Length))
    $reason        = [string]$nom.reason
    $confidence    = [double]$nom.confidence

    Write-MemoryLog -Component 'dream' -Message "  autopromote: nominee id=$($nom.memory_id) confidence=$confidence reason=$reason"

    # ── 4C PROMOTION GATE (shadow-first) ──────────────────────────────────────
    # $env:MEM0_PROMOTION_GATE_MODE in {off, shadow, enforce} (default shadow).
    #   off     -> gate disabled (no compute, no log) — kill switch.
    #   shadow  -> compute + LOG the verdict; promotion decision UNCHANGED (calibration).
    #   enforce -> a BLOCK verdict skips the canonize (the nominee stays evidence).
    # Computed for DryRun too (so a dry run yields real shadow data); the BLOCK
    # only short-circuits the non-DryRun canonize path below.
    # Mode source precedence (E/T4, 2026-06-22): $env override (ad-hoc) -> receipt config
    # $DcCfg.PromotionGateMode (PERSISTENT across reboots; the operator's enforce flip) ->
    # 'shadow' default. Reversible: edit PromotionGateMode in ~/.claude/scripts/mem0-stack.config.psd1.
    $gateMode = $env:MEM0_PROMOTION_GATE_MODE
    if ([string]::IsNullOrWhiteSpace($gateMode) -and $DcCfg -and $DcCfg.PromotionGateMode) { $gateMode = [string]$DcCfg.PromotionGateMode }
    if ([string]::IsNullOrWhiteSpace($gateMode)) { $gateMode = 'shadow' }
    $gateMode = $gateMode.Trim().ToLower()
    $gateBlocked = $false
    if ($gateMode -ne 'off' -and -not [string]::IsNullOrWhiteSpace($candidateText)) {
        $gateErrored = $false
        $gatePromote = $false
        try {
            $gateVerdict = Get-PromotionGateVerdict -MemoryId $nom.memory_id -CandidateText $candidateText -EvidenceRecord $evidenceRec
            Write-PromotionGateLog -Verdict $gateVerdict -Mode $gateMode -DryRunFlag $DryRun
            $gatePromote = [bool]$gateVerdict.gate.promote
            $gateCodexTokens += [int]$gateVerdict.codexTokens
            $gv = if ($gatePromote) { 'PROMOTE' } else { 'BLOCK' }
            Write-MemoryLog -Component 'dream' -Message "  autopromote: GATE [$gateMode] id=$($nom.memory_id) -> $gv class=$($gateVerdict.gate.gateClass) src=$($gateVerdict.sourceClass) N=$($gateVerdict.corroborationCount) contradicts=$($gateVerdict.contradicts) :: $($gateVerdict.gate.reason)"
        } catch {
            # The gate must NEVER crash the consolidator. Mark it errored; Resolve-GateBlocked
            # turns that into a fail-safe BLOCK in enforce only (shadow/off stay unchanged).
            Write-MemoryLog -Component 'dream' -Message "  autopromote: GATE error id=$($nom.memory_id) (non-fatal): $_"
            $gateErrored = $true
        }
        # Single, unit-tested decision point (Resolve-GateBlocked): off/shadow NEVER block;
        # enforce blocks on a non-promote verdict OR a gate error.
        $gateBlocked = Resolve-GateBlocked -GateMode $gateMode -GatePromote $gatePromote -GateErrored $gateErrored
    }

    if ($DryRun) {
        # DryRun logs were already emitted by Invoke-AutopromoteDecision; just build the summary.
        $promoteSummary += "- [DRY-RUN, not promoted] $shortText [reason: $reason]"
        continue
    }

    # 4C: enforce-mode gate BLOCK keeps this nominee out of the authority tier.
    if ($gateBlocked) {
        $gateBlockedCount++
        $promoteSummary += "- [GATE-BLOCKED, not promoted] $shortText [reason: $reason]"
        Write-MemoryLog -Component 'dream' -Message "  autopromote: GATE ENFORCE blocked id=$($nom.memory_id) — not promoted to canonical"
        continue
    }

    # Non-DryRun: call mem0-canonize.sh --actor dream-autopromote
    $canonizeResult = $null
    $canonizeExit   = 0
    try {
        if ([string]::IsNullOrWhiteSpace($DcRepoWsl)) {
            Write-MemoryLog -Component 'dream' -Message '  autopromote: no RepoRootWsl — cannot call canonize.sh'
            $canonizeExit = 1
        } else {
            # Escape single-quotes in the reason/id for the bash -c argument.
            # Memory IDs are UUIDs (no quotes needed), but be defensive.
            # Reason may contain apostrophes; replace ' with '"'"' for bash single-quote safety.
            $sq = "'"
            $sqEsc = "'" + '"' + "'" + '"' + "'"
            $escapedReason = $nom.reason.Replace($sq, $sqEsc)
            $escapedMid    = $nom.memory_id.Replace($sq, $sqEsc)
            # v1.12 B1 (MEM-7): run the DEPLOYED canonize script (~/apps/mem0-scripts,
            # synced by deploy.sh) - never the dev working tree, where an uncommitted
            # edit becomes production behavior at 3am. (ASCII hyphen ON PURPOSE: this
            # no-BOM file is read as ANSI by PS 5.1, and an em-dash's 0x94 byte is a
            # smart-quote that desyncs the 5.1 tokenizer - PS51Compat caught exactly that.)
            $canonizeCmd   = "bash /home/$DcWslUser/apps/mem0-scripts/mem0-canonize.sh --actor dream-autopromote '${escapedMid}' '${escapedReason}'"
            $canonizeResult = wsl.exe -d $DcDistro -e bash -c $canonizeCmd 2>&1
            $canonizeExit = $LASTEXITCODE
        }
    } catch {
        Write-MemoryLog -Component 'dream' -Message "  autopromote: canonize.sh threw: $_"
        $canonizeExit = 1
    }

    # $LASTEXITCODE proved unreliable across the wsl.exe boundary under Task Scheduler (it came back
    # empty 2026-06-21..23 despite the canonize running, so neither promoted++ nor failed++ ran: the
    # phantom promoted=0/failed=0 with no error logged). Judge success by the canonize OUTPUT instead.
    # mem0-canonize.sh prints a JSON body with tier set to canonical only on a real promotion; a
    # curl/HTTP error (e.g. 403) prints no such body. The failure body is logged in the else branch.
    $canonizeOk = (([string]$canonizeResult) -match 'tier.{1,8}canonical')
    if ($canonizeOk) {
        $promotedCount++
        $promoteSummary += "- $shortText [reason: $reason]"
        Write-MemoryLog -Component 'dream' -Message "  autopromote: promoted id=$($nom.memory_id) confidence=$confidence transport=autonomous"
    } else {
        $promoteFailed++
        # 422 = canary blocked (imperative text) — non-fatal, expected for edge cases
        $canonizePreview = if ($canonizeResult) { [string]$canonizeResult } else { '(no output)' }
        Write-MemoryLog -Component 'dream' -Message "  autopromote: promotion failed (exit=$canonizeExit) id=$($nom.memory_id): $($canonizePreview.Substring(0, [Math]::Min(200, $canonizePreview.Length)))"
        $promoteSummary += "- [FAILED] $shortText [reason: $reason]"
    }

    # Audit log every promotion attempt (success or failure)
    Write-MemoryLog -Component 'dream' -Message "  autopromote: audit id=$($nom.memory_id) reason=$reason confidence=$confidence exit=$canonizeExit transport=autonomous"
}

# ── MORNING-SUMMARY surface (FIX 1: only written in non-DryRun) ──
$morningSummaryPath = Join-Path $DreamStateDir 'morning-summary.md'
$morningSummaryTs   = (Get-Date).ToString('yyyy-MM-dd HH:mm')
$morningSummarySection = @"

## Autonomous canonical promotions — $morningSummaryTs (review/demote as needed)
$(if ($promoteSummary.Count -gt 0) { $promoteSummary -join "`n" } else { '- (none promoted this cycle)' })
"@
if (-not $DryRun) {
    try {
        Add-Content -Path $morningSummaryPath -Value $morningSummarySection -Encoding UTF8
    } catch {
        Write-MemoryLog -Component 'dream' -Message "  autopromote: morning-summary append failed (non-fatal): $_"
    }
} else {
    Write-MemoryLog -Component 'dream' -Message "  autopromote: DryRun=true -- morning-summary not written"
}

$durationLabel = if ($null -ne $promoteDurationMs) { "${promoteDurationMs}ms" } else { 'skipped' }
Write-MemoryLog -Component 'dream' -Message "  autopromote done: promoted=$promotedCount failed=$promoteFailed gate_blocked=$gateBlockedCount deduped=$($dedupedNominees.Count) over_cap=$($overCapNominees.Count) gate_codex_tokens=$gateCodexTokens (DryRun=$DryRun, $durationLabel)"

# ============== PHASE 4: PRUNE & INDEX ==============
Write-MemoryLog -Component 'dream' -Message '=== phase 4: prune & index ==='
$indexExit = 0
if (-not $DryRun -and -not $DcRepoWsl) {
    Write-MemoryLog -Component 'dream' -Message '  skip index-build: no RepoRootWsl in receipt (run install to write ~/.claude/scripts/mem0-stack.config.psd1)'
    Mark-Throttle -Name $ThrottleName
} elseif (-not $DryRun) {
    # v1.12 B1 (MEM-7): invoke the DEPLOYED builder (~/apps/mem0-scripts, synced by
    # deploy.sh) - never the dev working tree, where an uncommitted edit becomes
    # production behavior at 3am (same change as memory-index-refresh.ps1).
    $indexResult = wsl.exe -d $DcDistro -e bash -c "/home/$DcWslUser/apps/mem0-server/.venv/bin/python /home/$DcWslUser/apps/mem0-scripts/memory-index-build.py 2>&1"
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

# Brand-scope integrity audit (2026-06-20) — nightly check that no canonical fact ABOUT a
# brand is left brand-untagged (the bug that hid a brand-scoped canonical fact). Zero
# Codex, local Qdrant only, NON-FATAL. Writes ~/.mem0/brand-scope-status.json which the
# SessionStart storage-cap hook surfaces as a warning when any record is mis-scoped.
if (-not $DryRun -and $DcRepoWsl) {
    try {
        # v1.12 B1 (MEM-7): deployed runtime root, not the dev working tree.
        $bsAudit = wsl.exe -d $DcDistro -e bash -c "/home/$DcWslUser/apps/mem0-server/.venv/bin/python /home/$DcWslUser/apps/mem0-scripts/brand-scope-audit.py 2>&1"
        Write-MemoryLog -Component 'dream' -Message "  brand-scope audit: $bsAudit"
    } catch {
        Write-MemoryLog -Component 'dream' -Message "  brand-scope audit failed (non-fatal): $_"
    }
}

# Phase 5 anti-drift: AFTER snapshot + compare. Runs only if a BEFORE snapshot was taken
# ($driftBefore set => not DryRun, receipt has RepoRootWsl, and reaching here => the index build
# succeeded — phase 4 returns on index failure). Retrieval is deterministic (local
# EmbeddingGemma+Qdrant, no Codex) and the dream holds the Codex lock so L1a cannot write between
# the snapshots, so before==after unless THIS consolidation changed the store. NON-FATAL: a
# compare exit 2 (a canary that was retrievable became unretrievable) logs a WARN + appends an
# alarm record to ~/.mem0/consolidation-drift.jsonl (which Test-MemoryStack surfaces). The
# consolidation already happened — the guard never fails the dream.
if ($driftBefore) {
    try {
        $driftAfter = Invoke-DriftSnapshot -Phase 'after' -OutWsl '/tmp/dream-drift-after.json'
        if ($driftAfter) {
            $py     = "/home/$DcWslUser/apps/mem0-server/.venv/bin/python"
            $script = "$DcRepoWsl/eval/retrieval-drift/retrieval_drift.py"
            $cmp     = wsl.exe -d $DcDistro -e bash -c "$py $script compare '$driftBefore' '$driftAfter' 2>&1"
            $cmpExit = $LASTEXITCODE
            Write-MemoryLog -Component 'dream' -Message "  drift compare (exit=$cmpExit): $cmp"
            if ($cmpExit -eq 2) {
                $driftFlag = Join-Path $DcHomeUnc '.mem0\consolidation-drift.jsonl'
                $driftRec  = [ordered]@{
                    ts             = (Get-Date).ToString('o')
                    schema_version = 'drift-v1'
                    detail         = ($cmp -join ' ')
                } | ConvertTo-Json -Compress -Depth 5
                $driftDir = Split-Path -Parent $driftFlag
                if (-not (Test-Path $driftDir)) { New-Item -ItemType Directory -Path $driftDir -Force | Out-Null }
                # No-BOM UTF-8 append (codebase JSONL convention — Add-Content -Encoding UTF8 prepends
                # a BOM that breaks line-by-line parsing; see Write-PromotionGateLog).
                [System.IO.File]::AppendAllText($driftFlag, $driftRec + "`n", (New-Object System.Text.UTF8Encoding($false)))
                Write-MemoryLog -Component 'dream' -Message '  !! RETRIEVAL DRIFT ALARM — a canary fact became unretrievable after consolidation; flagged to consolidation-drift.jsonl'
            }
        }
    } catch {
        Write-MemoryLog -Component 'dream' -Message "  drift compare FAILED (non-fatal): $_"
    }
}

# Token + duration totals
Write-CodexUsageLog -Component 'dream' `
    -TokensUsed (($gatherTokens -as [int]) + ($consolidateTokens -as [int]) + ($gateCodexTokens -as [int])) `
    -DurationMs ($gatherDurationMs + $consolidateMs) `
    -Status 'ok' `
    -FactsPosted $posted

Write-MemoryLog -Component 'dream' -Message "=== dream cycle done (DryRun=$DryRun) ==="

} finally {
    if (-not $DryRun) { Release-CodexLock }
}
exit 0
