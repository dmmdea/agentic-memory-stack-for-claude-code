# L1a extractor - Windows-native PowerShell
# Spawned by stop-extract dispatcher with: -TranscriptPath <path> -EventName <Stop|PreCompact|SessionEnd>
# Runs detached, exits 0 always (best-effort, never blocks Claude Code).
#
# Architecture: Stop hook (Claude Code) -> stop-extract.ps1 (Start-Process -Hidden) ->
# this script in detached PowerShell -> codex.cmd subagent (ChatGPT subscription auth,
# no concurrent-session conflict with Claude Max) -> POST extracted facts to mem0 at
# 127.0.0.1:18791 (WSL mirrored networking).

param(
    [string]$TranscriptPath = '',
    [string]$EventName = 'Stop'
)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'memory-common.ps1')
Initialize-MemoryEnv

# Recursion guard: codex doesn't fire Claude Code hooks (different vendor), so
# subprocess invocation can't re-trigger our Stop hook. Env guard is belt-and-braces.
if ($env:L1A_REENTRANT -eq '1') { exit 0 }
$env:L1A_REENTRANT = '1'

try {
    # Throttle: 10 minutes between SUCCESSFUL extractions (not mere fires - audit
    # 2026-06-08: prior code marked the throttle before doing anything, so a
    # transient health/parse/POST failure silenced the next 10 min)
    if (-not (Test-Throttle -Name 'l1a' -MinIntervalSeconds 600)) {
        exit 0
    }

    Write-MemoryLog -Component 'l1a' -Message "=== start: event=$EventName transcript=$TranscriptPath ==="

    # Rotate logs if they have grown beyond 1MB
    Invoke-LogRotation -MaxBytes 1MB -KeepN 5

    # Drain any previously-failed mem0 POSTs (transient health hiccups self-heal)
    $dlq = Drain-Mem0DeadLetter
    if ($dlq.drained -gt 0 -or $dlq.remaining -gt 0) {
        Write-MemoryLog -Component 'l1a' -Message "  DLQ: drained $($dlq.drained), remaining $($dlq.remaining)"
    }

    if (-not (Test-Mem0Health)) {
        Write-MemoryLog -Component 'l1a' -Message '  mem0 unreachable, aborting'
        exit 0
    }

    if ([string]::IsNullOrWhiteSpace($TranscriptPath) -or -not (Test-Path $TranscriptPath)) {
        Write-MemoryLog -Component 'l1a' -Message '  no transcript file, aborting'
        exit 0
    }

    # Cursor-aware windowing (2026-07-14): only extract turns appended SINCE the last successful
    # extraction of THIS transcript, so overlapping recent-24-turn windows stop re-emitting the
    # same facts every Stop. $advanceTo is captured NOW (before the slow codex call) so appends
    # during extraction are picked up next run, not skipped. ANY uncertainty -> full recent window.
    $advanceTo = 0L
    try { $advanceTo = [long](Get-Item -LiteralPath $TranscriptPath).Length } catch { $advanceTo = 0L }
    $cursor = Get-L1aCursor -TranscriptPath $TranscriptPath
    $turns = $null
    if ($cursor -gt 0 -and $advanceTo -gt 0 -and $advanceTo -eq $cursor) {
        # transcript is byte-for-byte unchanged since the last successful extraction — nothing new.
        # (The DLQ drain above already retried any previously-failed posts, so this is safe.)
        # NOTE: -eq, NOT -le. A SHRINK ($advanceTo < $cursor: rotation/truncation/corruption) must
        # NOT skip — a stale cursor above the file length would silently stop extraction forever.
        # It falls through to the fallback (full window) below, which re-extracts and resets the cursor.
        Write-MemoryLog -Component 'l1a' -Message '  no new transcript content since last extraction; skipping'
        exit 0
    }
    if ($cursor -gt 0 -and $advanceTo -gt $cursor) {
        try { $turns = Get-TranscriptTurnsSince -TranscriptPath $TranscriptPath -SinceBytes $cursor -MaxTurns 24 -MaxChars 12000 } catch { $turns = $null }
    }
    if ([string]::IsNullOrWhiteSpace($turns)) {
        # first extraction of this transcript, a rotation/shrink, an empty since-window, or any
        # error above: fall back to the unchanged last-24-turns behaviour (never lose extraction).
        $turns = Get-RecentTranscriptTurns -TranscriptPath $TranscriptPath -MaxTurns 24 -MaxChars 12000
    }
    if ([string]::IsNullOrWhiteSpace($turns)) {
        Write-MemoryLog -Component 'l1a' -Message '  empty turns, aborting'
        exit 0
    }

    $prompt = @"
You are a memory fact extractor. Read the conversation excerpt below and output STRICT JSON only - no prose, no markdown fences, no preamble.

Output this exact shape:
{"facts":["fact 1","fact 2"],"episode":{"goal":"1-2 sentence goal","summary":"2-4 sentence summary","advanced_goals":[{"goal_title":"...","delta_text":"..."}],"blocked_goals":[{"goal_title":"...","block_reason":"..."}],"open_questions":["...","..."]}}

Rules for facts (apply the INFERABILITY GATE first — it is the most important rule):
- INFERABILITY GATE: before keeping a fact, ask "could a competent engineer who knows general software/tools but has NEVER worked on THIS project infer or guess this?" If YES, DROP it. Keep ONLY genuinely project-specific facts that cannot be known without having been here (our ports, paths, collection names, config values, decisions, IDs, flags, versions, locked-in choices). Generic best-practices and things the reader already knows are noise.
- Prefer FEWER, higher-signal facts over filling a quota; max 5; output [] if nothing is genuinely project-specific.
- Each fact self-contained and declarative, <= 30 words preferred, 60 words HARD MAXIMUM — but NEVER drop the distinguishing detail to hit the limit (a fact that loses the specific value/name/path/number is useless; specific-and-concrete beats short-and-vague).
- ATOMIC facts only: ONE fact = ONE claim about ONE topic. NEVER emit a multi-topic dump (a session recap, a list of changes, several decisions welded into one string). If a candidate fact bundles multiple claims, SPLIT it into separate single-topic facts BEFORE output — each must stand alone when retrieved individually.
- Keep proper nouns, dates, numbers, paths, IDs, flags, versions VERBATIM.
- For a procedure or a conditional, phrase the fact as an actionable rule: "IF <situation> THEN <action>" (e.g. "IF rolling back the egemma migration THEN disable egemma-rollback-prune.timer FIRST").
- Drop: pleasantries, hypotheticals, code blocks, one-off transient specifics (a single ad-hoc search query, a temp path) that will not recur, and anything already covered by a more durable fact.
- Prefer durable, decision-grade facts: decisions, preferences, identity/relationships, system-state changes, locked-in choices.

Rules for episode:
- goal: 1-2 sentences describing what the operator was trying to accomplish in this session.
- summary: 2-4 sentences describing what actually happened, what changed, what was blocked.

ALSO extract:
- advanced_goals: 0-3 goals that this session made concrete progress on. Each: {goal_title (short, durable noun phrase like "Ship the staging release"), delta_text (one sentence: what advanced)}. Drop if no clear progress.
- blocked_goals: 0-2 goals that this session HIT a blocker on. Each: {goal_title, block_reason (one sentence)}.
- open_questions: 0-5 declarative uncertainties RAISED in this session that have no answer yet. NOT idle wondering — questions that block progress or invite future investigation. Each: a single sentence ending in "?".

Rules for goal extraction:
- goal_title is the NOUN PHRASE for a multi-session goal, not a one-off task. "Fix the typo" is not a goal; "Ship v0.16 episodic memory" is.
- Be CONSERVATIVE: prefer 0 goals over fabricated ones. The Information Gain principle prefers absence of noise over precision-fudged signal.
- If session was trivial or no goal-relevant content: {"advanced_goals":[],"blocked_goals":[],"open_questions":[]}.

If facts is empty (truly trivial chat with no durable signal), output {"facts":[],"episode":null}.
Otherwise, episode MUST be populated — every session with at least one extracted fact must produce a goal+summary+advanced_goals+blocked_goals+open_questions (arrays may be empty), even if brief.

Conversation excerpt:
$turns
"@

    # Shared Codex mutex: don't fire if C1 (or another L1a) is mid-Codex-call
    # (audit finding 2026-06-08: prior design had separate locks, allowing concurrent
    # Codex calls that contended for ChatGPT subscription quota).
    if (-not (Acquire-CodexLock -Owner 'l1a')) {
        Write-MemoryLog -Component 'l1a' -Message '  codex lock held by another worker; skipping this extraction'
        exit 0
    }

    Write-MemoryLog -Component 'l1a' -Message '  calling codex subagent for extraction'
    $raw = $null
    $codexStart = Get-Date
    try {
        $raw = Invoke-CodexSubagent -Prompt $prompt -ReasoningEffort $script:CodexEffortExtractor -TimeoutSeconds 60
    } catch {
        Write-MemoryLog -Component 'l1a' -Message "  codex subagent failed: $_"
        Write-CodexUsageLog -Component 'l1a' -Status 'error' -DurationMs ([int]((Get-Date) - $codexStart).TotalMilliseconds)
        Release-CodexLock
        exit 0
    }
    Release-CodexLock
    $codexDurationMs = [int]((Get-Date) - $codexStart).TotalMilliseconds
    $codexTokens = (Parse-CodexTokenUsage -RawOutput $raw)

    if ([string]::IsNullOrWhiteSpace($raw)) {
        Write-MemoryLog -Component 'l1a' -Message '  codex returned empty'
        exit 0
    }

    # Extract just the model response from Codex's verbose output, then parse JSON
    $modelText = Get-CodexResponseText -RawOutput $raw
    $parsed = Extract-JsonFromText -Text $modelText -ExpectedKey 'facts'
    if ($null -eq $parsed) {
        $preview = if ($raw.Length -gt 200) { $raw.Substring(0, 200) } else { $raw }
        Write-MemoryLog -Component 'l1a' -Message "  json parse failed; preview: $preview"
        exit 0
    }

    $facts = @($parsed.facts)
    # 2026-07-21: a zero-fact result used to `exit 0` RIGHT HERE, before the episode POST
    # below. That coupled the episodic record to durable fact extraction: a session that
    # produced no evergreen facts left no episode at all, even when the extractor had
    # returned a perfectly good episode with a goal and summary. Episodes are the record
    # that a session HAPPENED and what it was about; facts are what was worth keeping from
    # it. They are different questions, and the second answering "none" must not erase the
    # first. This is also self-reinforcing: goal staleness is measured from episode_links,
    # so missing episodes make goals look abandoned.
    # Flow now falls through to the episode POST, which keeps its own guard
    # (`$parsed.episode.goal`) — so this still never FORCE-creates an episode, per the
    # Phase 3 rule below; it just stops discarding a real one.
    if ($facts.Count -eq 0) {
        Write-MemoryLog -Component 'l1a' -Message '  no facts extracted (clean session) - continuing to episode POST'
    }

    # Phase 3: partition facts — evergreen atomic facts go to durable mem0; ship-log
    # facts (Test-IsShipLog=true) fold into the episode summary, not mem0 records.
    $split = Split-FactsByShipLog -Facts $facts

    $posted = 0
    $postedMemoryIds = [System.Collections.Generic.List[string]]::new()
    # Only evergreen facts POST to mem0 and populate linked_memory_ids (durable-only).
    foreach ($rawFact in $split.Evergreen) {
        if ([string]::IsNullOrWhiteSpace($rawFact)) { continue }
        # MEM-10 (2026-07-03): belt-and-braces write-time guard. The prompt now
        # demands atomic <=60-word facts, but if Codex still emits a multi-topic
        # dump (>700 chars) it is split at sentence boundaries here — one record
        # per chunk — instead of storing a single blob that trips the l10-audit
        # OVERSIZE line (1200) and embeds many topics into one vector.
        foreach ($fact in (Split-OversizeFact -Fact $rawFact)) {
            if ([string]::IsNullOrWhiteSpace($fact)) { continue }
            $memId = Add-Mem0Memory -Text $fact -Source 'l1a-extractor' -Metadata @{
                event = $EventName
                tier = 'evidence'
                extracted_at = (Get-Date).ToString('o')
            }
            if ($memId) {
                $posted++
                if ($memId -is [string]) { $postedMemoryIds.Add($memId) }
            }
        }
    }
    if ($facts.Count -gt 0) {
        Write-MemoryLog -Component 'l1a' -Message "  done - extracted $($split.Evergreen.Count + $split.ShipLogs.Count) durable+shiplog ($($split.Evergreen.Count) evergreen -> mem0, $($split.ShipLogs.Count) ship-logs -> episode), posted $posted to mem0 (codex ${codexDurationMs}ms, $codexTokens tokens)"
    }
    # One usage record per run: 'empty' keeps the zero-fact case distinguishable in the ledger,
    # which is what the old early-exit reported before the episode POST was reached.
    $usageStatus = if ($facts.Count -eq 0) { 'empty' } else { 'ok' }
    Write-CodexUsageLog -Component 'l1a' -TokensUsed ($codexTokens -as [int]) -DurationMs $codexDurationMs -Status $usageStatus -FactsPosted $posted
    Mark-Throttle -Name 'l1a'
    Set-L1aCursor -TranscriptPath $TranscriptPath -Bytes $advanceTo   # this window was processed; next run only scans NEW turns

    # Phase 3: if ship-logs exist but no episode is being posted they are dropped from
    # durable storage. Do NOT force-create an episode and do NOT write ship-logs to mem0.

    # v0.15: POST episode to /v1/episodes (live since v0.20 Phase B).
    if ($parsed.episode -and $parsed.episode.goal) {
        # Phase 3: build episode summary (with ship-log fold) BEFORE the POST try-block so
        # any bug in the ship-log fold surfaces via the outer handler rather than being
        # swallowed as episode noise. $parsed.episode is non-null (guarded above).
        $episodeSummary = $parsed.episode.summary
        if ($split.ShipLogs.Count -gt 0) {
            $episodeSummary += "`n`nSession ship-logs (routed from durable):`n - " + ($split.ShipLogs -join "`n - ")
        }

        try {
            $apiKey = Get-Mem0Key

            # Extract session UUID from transcript filename (format: <project-dir>/<uuid>.jsonl)
            $sessionId = $null
            if ($TranscriptPath) {
                $fname = [System.IO.Path]::GetFileNameWithoutExtension($TranscriptPath)
                # Claude Code transcript filenames are UUIDs
                if ($fname -match '^[0-9a-f\-]{32,}$') { $sessionId = $fname }
                if (-not $sessionId) { $sessionId = $fname }
            }
            if (-not $sessionId) { $sessionId = [System.Guid]::NewGuid().ToString() }

            # Infer brand/workspace/project from transcript path. NOTE: plain $TranscriptPath,
            # NOT ($TranscriptPath ?? '') — the ?? null-coalescing operator is PS7-only and the
            # Stop hook runs this under Windows PowerShell 5.1, where ?? is a PARSE ERROR that
            # silently kills the ENTIRE worker (root cause of the 2026-06-16 capture outage).
            # $TranscriptPath is a [string] param (defaults '', never $null), so ?? was redundant.
            $brandInfo = Get-BrandFromTranscriptPath -Path $TranscriptPath

            # Best-effort started_at: use transcript file mtime as a proxy for session start
            $sessionStartedAt = (Get-Date).ToString('o')
            if ($TranscriptPath -and (Test-Path $TranscriptPath)) {
                try {
                    $sessionStartedAt = (Get-Item $TranscriptPath).CreationTimeUtc.ToString('o')
                } catch {}
            }

            # v0.16: force-array normalization (HIGH-2 fix: Codex sometimes returns a single
            # PSCustomObject instead of a 1-element array when there is only one item.
            # @(...) coerces: null → @(), single object → 1-element array, array → unchanged).
            $advancedGoals  = @($parsed.episode.advanced_goals  | Where-Object { $_ })
            $blockedGoals   = @($parsed.episode.blocked_goals   | Where-Object { $_ })
            $openQuestions  = @($parsed.episode.open_questions  | Where-Object { $_ })

            $episodePayload = @{
                session_id     = $sessionId
                started_at     = $sessionStartedAt
                ended_at       = (Get-Date).ToUniversalTime().ToString('o')
                transcript_path = $TranscriptPath
                goal           = $parsed.episode.goal
                summary        = $episodeSummary
                message_count  = 0
                brand          = $brandInfo.brand
                workspace      = $brandInfo.workspace
                project        = $brandInfo.project
                linked_memory_ids = @($postedMemoryIds)
                # v0.16 additions
                advanced_goals = $advancedGoals
                blocked_goals  = $blockedGoals
                open_questions = $openQuestions
                # v0.18 MED-17: hook contract version ('17.0' intentional — contract
                # unchanged in v0.18; bump only IF it changes in v0.19+)
                hook_contract_version = '17.0'
            } | ConvertTo-Json -Depth 6 -Compress

            # v1.12 F1: PS 5.1 sends a STRING -Body as Latin-1 - an episode whose
            # goal/summary carries non-ASCII (Spanish sessions, em-dashes) reached
            # FastAPI as invalid UTF-8 -> 400 and the episode was silently lost
            # (the catch below logs it as "non-fatal"). Send UTF-8 BYTES.
            Invoke-RestMethod -Uri "$($script:Mem0Url)/v1/episodes" `
                -Method Post `
                -Body ([System.Text.Encoding]::UTF8.GetBytes($episodePayload)) `
                -ContentType 'application/json' `
                -Headers @{'X-API-Key' = $apiKey} `
                -TimeoutSec 5 | Out-Null
            Write-MemoryLog -Component 'l1a' -Message "  posted episode for session $sessionId (goal: $($parsed.episode.goal.Substring(0, [Math]::Min(80, $parsed.episode.goal.Length))))"
        } catch {
            # Best-effort: POST /v1/episodes is live (v0.20 Phase B). A failure here is
            # non-fatal — the session's durable facts already landed in mem0; the episode
            # (and its routed ship-logs) is the only loss.
            Write-MemoryLog -Component 'l1a' -Message "  episode post failed (non-fatal; durable facts already in mem0): $_"
        }
    }

} catch {
    Write-MemoryLog -Component 'l1a' -Message "  unhandled error: $_"
} finally {
    Remove-Item env:L1A_REENTRANT -ErrorAction SilentlyContinue
}

exit 0
