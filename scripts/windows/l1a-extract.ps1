# L1a extractor - Windows-native PowerShell
# Spawned by stop-extract dispatcher with: -TranscriptPath <path> -EventName <Stop|PreCompact|SessionEnd>
#   [-SessionId <the hook's session id>] [-OriginTranscriptPath <the real transcript, when -TranscriptPath is a PreCompact snapshot>]
# Runs detached, exits 0 always (best-effort, never blocks Claude Code).
#
# Architecture: Stop hook (Claude Code) -> stop-extract.ps1 (Start-Process -Hidden) ->
# this script in detached PowerShell -> codex.cmd subagent (ChatGPT subscription auth,
# no concurrent-session conflict with Claude Max) -> POST extracted facts to mem0 at
# 127.0.0.1:18791 (WSL mirrored networking).

param(
    [string]$TranscriptPath = '',
    [string]$EventName = 'Stop',
    # The hook's own session id. PreCompact points -TranscriptPath at a temp snapshot whose file
    # name is not a session, so the episode's session comes from here (Resolve-L1aSessionId).
    [string]$SessionId = '',
    # The real transcript behind a PreCompact snapshot: workspace/brand/start time are read from it.
    [string]$OriginTranscriptPath = ''
)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'memory-common.ps1')
Initialize-MemoryEnv

# Recursion guard: codex doesn't fire Claude Code hooks (different vendor), so
# subprocess invocation can't re-trigger our Stop hook. Env guard is belt-and-braces.
if ($env:L1A_REENTRANT -eq '1') { exit 0 }
$env:L1A_REENTRANT = '1'

# W4 F12: UNCONDITIONAL attempt stamp - written here, before the throttle and every
# other early return below. WHY: last-l1a stamps only on a SUCCESSFUL extraction, so
# "sessions happened and had nothing durable to extract" was indistinguishable from
# "the extractor is dead" and the capability manifest would convict a healthy box.
# With both stamps the server can require attempt-fresh AND success-stale-for-days
# before it accuses anything (capabilities.py _l1a_state). Best-effort by design:
# a stamp failure must never cost an extraction. ($script:StateDir already exists —
# Initialize-MemoryEnv above creates it.)
try { Set-Content -Path (Join-Path $script:StateDir 'last-l1a-attempt') -Value (Get-UnixEpoch) -Encoding UTF8 -NoNewline } catch { }

# A held lock is WAITED on (polling every 2 s, 20 s by default) before a run gives up. Skipping at
# once cost 24% of extractions (114 of 481 runs in one week), and a session whose last Stop skipped
# was never extracted. AMS_L1A_LOCK_WAIT_SECONDS overrides the bound; it applies to the shared Codex
# mutex and to the per-session lock alike.
$lockWaitSeconds = 20
if ($env:AMS_L1A_LOCK_WAIT_SECONDS -match '^\d{1,3}$') { $lockWaitSeconds = [int]$env:AMS_L1A_LOCK_WAIT_SECONDS }

# The run, as a function so the file reads top to bottom: Invoke-L1aMain (gate, health, which
# transcripts) -> Invoke-L1aTranscript (the per-session lock, up to N windows) -> Invoke-L1aWindow
# (one complete pass: window, model call, facts, usage row, cursor, episode). The last line of the file
# calls Invoke-L1aMain.
function Invoke-L1aMain {
    # Who does the LLM work decides the scope: the Codex CLI is one subscription's quota shared by every
    # job on the box (box-wide gate, shared lock, one window); the Claude API has no shared quota
    # (per-session gate and lock, several windows). See Get-L1aThrottleScope in memory-common.ps1.
    $provider = Get-L1aProvider
    $scope = Get-L1aThrottleScope -Provider $provider
    $throttleSeconds = Get-L1aThrottleSeconds -Scope $scope

    # The session is named as the episode names it (the hook's id wins; a PreCompact snapshot's file
    # name is not a session, so the real transcript's name is used when there is one).
    $primarySource = if ($OriginTranscriptPath) { $OriginTranscriptPath } else { $TranscriptPath }
    $sessionName = 'unknown'
    try { if ($SessionId -or $primarySource) { $sessionName = Resolve-L1aSessionId -SessionId $SessionId -TranscriptPath $primarySource } } catch { }
    $sessionKey = Get-L1aSessionKey -SessionId $sessionName

    # Throttle: spacing between SUCCESSFUL extractions (not mere fires - audit
    # 2026-06-08: prior code marked the throttle before doing anything, so a
    # transient health/parse/POST failure silenced the next window).
    # 1.41: on the Claude API the gate is per session (90 s); box-wide 10 min is the Codex quota's.
    # The skipped turns are not lost: the cursor only moves forward, so the next run picks them up.
    if (-not (Test-L1aThrottle -Scope $scope -SessionKey $sessionKey -Seconds $throttleSeconds)) {
        # 1.40.1: say so. This exit left no line, so a session whose every Stop fell inside another
        # session's window looked the same in the log as a session whose hook never fired.
        $gateWhy = if ($scope -eq 'session') { "this session was extracted under $throttleSeconds s ago" } else { "a successful extraction ran on this box under $([int]($throttleSeconds / 60)) min ago" }
        Write-MemoryLog -Component 'l1a' -Message "throttled: event=$EventName session=$sessionName scope=$scope - $gateWhy, so this run is skipped"
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

    if (-not (Wait-Mem0Health)) {
        # The cursor is not advanced, so the session's next run re-reads these turns.
        Write-MemoryLog -Component 'l1a' -Message '  mem0 unreachable after 5 probes over ~3.5 min, aborting (cursor not advanced)'
        exit 0
    }

    if ([string]::IsNullOrWhiteSpace($TranscriptPath) -or -not (Test-Path $TranscriptPath)) {
        Write-MemoryLog -Component 'l1a' -Message '  no transcript file, aborting'
        exit 0
    }

    # Housekeeping, one cheap pass per run: per-session stamps and locks, and the cursor files an older
    # release keyed on a PreCompact snapshot's name, once they are a week old.
    Remove-L1aOldState

    # The cursor is keyed on the session's REAL transcript. A PreCompact run reads a snapshot (a
    # byte-for-byte copy taken by stop-extract.ps1, named precompact-snap-<PID>.jsonl); keyed on that
    # name the real transcript's cursor never moved, so the next Stop re-extracted what PreCompact had
    # just posted. The reader never reports an offset past the snapshot's length.
    $cursorPath = $primarySource
    $useSessionLock = ($provider -eq 'anthropic')
    $maxWindows = Get-L1aMaxWindows -Provider $provider
    Invoke-L1aTranscript -ReadPath $TranscriptPath -CursorPath $cursorPath -SourceTranscript $primarySource `
        -EpisodeSessionHint $SessionId -Scope $scope -SessionKey $sessionKey -Seconds $throttleSeconds `
        -Provider $provider -MaxWindows $maxWindows -UseSessionLock $useSessionLock | Out-Null

    # SessionStart catch-up (Claude API only): sessions that ended without an extraction that reached their
    # last turns. A Stop skipped by the 90 s gate, a crash and a closed laptop all leave bytes past the
    # cursor, and nothing else would come back for them. At most 2 other transcripts that have a cursor and
    # bytes past it and were last written 15 minutes to 7 days ago (a younger one may be live), most recent
    # first, one window each, under their own session ids, behind the same gate and lock. No scheduler, no
    # marker file. AMS_L1A_CATCHUP=0 turns it off.
    if ($EventName -eq 'SessionStart' -and $provider -eq 'anthropic' -and ([string]$env:AMS_L1A_CATCHUP).Trim() -ne '0') {
        $projectsRoot = Join-Path (Get-AmsHomeDir) (Join-Path '.claude' 'projects')
        $mine = @([System.IO.Path]::GetFileNameWithoutExtension($primarySource), $sessionName)
        foreach ($cand in @(Get-L1aCatchupCandidates -ProjectsRoot $projectsRoot -ExcludeNames $mine -Max 2)) {
            $candKey = Get-L1aSessionKey -SessionId $cand.BaseName
            if (-not (Test-L1aThrottle -Scope $scope -SessionKey $candKey -Seconds $throttleSeconds)) {
                Write-MemoryLog -Component 'l1a' -Message "throttled: event=$EventName session=$($cand.BaseName) scope=$scope - catch-up skipped, this session was extracted under $throttleSeconds s ago"
                continue
            }
            Write-MemoryLog -Component 'l1a' -Message "=== catch-up: session=$($cand.BaseName) transcript=$($cand.FullName) ==="
            Invoke-L1aTranscript -ReadPath $cand.FullName -CursorPath $cand.FullName -SourceTranscript $cand.FullName `
                -EpisodeSessionHint '' -Scope $scope -SessionKey $candKey -Seconds $throttleSeconds `
                -Provider $provider -MaxWindows 1 -UseSessionLock $true | Out-Null
        }
    }
}

function Invoke-L1aTranscript {
    # One transcript: take the session lock (Claude API) and run up to $MaxWindows windows, each a complete
    # pass of the pipeline, stopping early when a window has nothing to give or leaves nothing behind.
    # The session lock is held across the windows, so two events of one session (Stop and PreCompact,
    # parallel Stops) cannot extract the same window twice; the Codex provider keeps its shared mutex,
    # taken around each model call inside the window.
    param(
        [string]$ReadPath, [string]$CursorPath, [string]$SourceTranscript, [string]$EpisodeSessionHint,
        [string]$Scope, [string]$SessionKey, [int]$Seconds, [string]$Provider, [int]$MaxWindows, [bool]$UseSessionLock
    )
    $lockName = ''
    if ($UseSessionLock) {
        $lockName = 'l1a-session-' + $SessionKey + '.lock'
        $lockResult = Acquire-CodexLockWithWait -Owner 'l1a' -WaitSeconds $lockWaitSeconds -LockName $lockName
        if (-not $lockResult.acquired) {
            Write-MemoryLog -Component 'l1a' -Message "  session lock held by another run of this session; skipping this extraction (waited $($lockResult.waited)s)"
            return
        }
        if ($lockResult.waited -gt 0) {
            Write-MemoryLog -Component 'l1a' -Message "  session lock acquired after $($lockResult.waited)s wait"
            # The gate was evaluated BEFORE the wait. The run that held the lock has marked it and advanced
            # the cursor; this run would only post paraphrased near-duplicates of what it just posted.
            if (-not (Test-L1aThrottle -Scope $Scope -SessionKey $SessionKey -Seconds $Seconds)) {
                Write-MemoryLog -Component 'l1a' -Message '  another run finished while this one waited for the lock; skipping this extraction'
                Release-CodexLock -LockName $lockName
                return
            }
        }
    }
    try {
        for ($windowNo = 1; $windowNo -le $MaxWindows; $windowNo++) {
            $res = @(Invoke-L1aWindow -ReadPath $ReadPath -CursorPath $CursorPath -SourceTranscript $SourceTranscript `
                -EpisodeSessionHint $EpisodeSessionHint -Scope $Scope -SessionKey $SessionKey -Seconds $Seconds `
                -WindowNo $windowNo -MaxWindows $MaxWindows -UseSessionLock $UseSessionLock)
            if ($res.Count -eq 0 -or [string]$res[$res.Count - 1] -ne 'continue') { break }
        }
    } finally {
        if ($lockName) { Release-CodexLock -LockName $lockName }
    }
}

function Invoke-L1aWindow {
    # One complete pass: the next window of text past the cursor, the model call, the facts, the usage
    # row, the throttle stamp, the cursor, the episode. Returns 'continue' when the cursor stopped short of
    # the length seen at the start (more to read), else 'stop'. Every early exit is a 'stop' that leaves
    # the cursor where it was, so the next run re-reads the same window.
    param(
        [string]$ReadPath, [string]$CursorPath, [string]$SourceTranscript, [string]$EpisodeSessionHint,
        [string]$Scope, [string]$SessionKey, [int]$Seconds, [int]$WindowNo, [int]$MaxWindows, [bool]$UseSessionLock
    )
    # Cursor-aware windowing (2026-07-14): only extract turns appended SINCE the last successful
    # extraction of THIS transcript, so overlapping recent-24-turn windows stop re-emitting the
    # same facts every Stop. $advanceTo is captured NOW (before the slow codex call) so appends
    # during extraction are picked up next run, not skipped. Get-L1aReadMode decides where the pass reads:
    # forward from the cursor (from the start of the file on a first run that may take several windows),
    # or the last-24-turns tail with the cursor set to the length seen (a shrink, a read error, a first
    # run limited to one window).
    $advanceTo = 0L
    try { $advanceTo = [long](Get-Item -LiteralPath $ReadPath).Length } catch { $advanceTo = 0L }
    $cursor = Get-L1aCursor -TranscriptPath $CursorPath
    $turns = $null
    $consumedTo = $advanceTo
    $windowTag = ''
    if ($MaxWindows -gt 1) { $windowTag = " $WindowNo/$MaxWindows" }
    $readMode = Get-L1aReadMode -Cursor $cursor -Length $advanceTo -MaxWindows $MaxWindows
    if ($readMode -eq 'none') {
        # transcript is byte-for-byte unchanged since the last successful extraction — nothing new.
        # (The DLQ drain above already retried any previously-failed posts, so this is safe.)
        # NOTE: -eq, NOT -le. A SHRINK ($advanceTo < $cursor: rotation/truncation/corruption) must
        # NOT skip — a stale cursor above the file length would silently stop extraction forever.
        # It falls through to the fallback (full window) below, which re-extracts and resets the cursor.
        Write-MemoryLog -Component 'l1a' -Message '  no new transcript content since last extraction; skipping'
        return 'stop'
    }
    $usedWindow = $false
    if ($readMode -eq 'forward') {
        # The forward window: the next 12,000 characters of text past the cursor, read in order. The
        # old read took the LAST 24 records of the gap and the cursor then jumped to the end of the file,
        # so everything between the cursor and that tail was never extracted. With no cursor yet the
        # window starts at byte 0.
        if ($cursor -lt 0) { $cursor = 0L }
        $win = $null
        try { $win = Get-TranscriptWindowSince -TranscriptPath $ReadPath -SinceBytes $cursor -MaxChars 12000 } catch { $win = $null }
        if ($null -ne $win) {
            $usedWindow = $true
            if ([string]::IsNullOrWhiteSpace($win.Text)) {
                # Nothing to extract in what the scan passed: tool results and the like. Move the cursor
                # past it (those bytes are not re-read) and go on if the scan stopped at its byte bound
                # rather than at the end. A window that consumed nothing (a partial last line) waits.
                if ($win.ConsumedTo -gt $cursor) {
                    Set-L1aCursor -TranscriptPath $CursorPath -Bytes $win.ConsumedTo
                    Write-MemoryLog -Component 'l1a' -Message "  no text turns in $($win.ConsumedTo - $cursor) bytes past the cursor (window$windowTag); cursor moved on without a model call"
                    if ($win.ConsumedTo -lt $advanceTo) { return 'continue' }
                } else {
                    Write-MemoryLog -Component 'l1a' -Message '  no complete new record past the cursor; skipping'
                }
                return 'stop'
            }
            $turns = $win.Text
            $consumedTo = [long]$win.ConsumedTo
        }
    }
    if (-not $usedWindow) {
        # a first extraction limited to one window, a rotation/shrink, or a read error above: the
        # last-24-turns behaviour (never lose extraction), and the cursor goes to the length seen.
        $turns = Get-RecentTranscriptTurns -TranscriptPath $ReadPath -MaxTurns 24 -MaxChars 12000
        $consumedTo = $advanceTo
    }
    if ([string]::IsNullOrWhiteSpace($turns)) {
        Write-MemoryLog -Component 'l1a' -Message '  empty turns, aborting'
        return 'stop'
    }
    Write-MemoryLog -Component 'l1a' -Message "  window${windowTag}: cursor=$cursor consumed_to=$consumedTo length=$advanceTo chars=$($turns.Length)"

    # EPHEMERA GATE (gate verdict 2026-08-19, memory-frontier hand-label): of the 60
    # memories the mechanical audit accused, only 5 were genuinely stale while 20
    # were EPHEMERAL - point-in-time work pointers that should never have been stored
    # (80% of the accused STALE+EPHEMERAL rows; 81% across all 135 hand-labelled
    # rows; the verdict extrapolates ephemera to ~22% of path-naming memories).
    # The same hand-label found 23 VALID accused memories
    # whose substance was durable and whose path was incidental - which is why the
    # gate below carries an explicit KEEP clause. The verdict killed the validity
    # schema and named THIS prompt rule the cheapest fix: stop admitting ephemera at
    # write time instead of detecting them at read time.
    $prompt = @"
You are a memory fact extractor. Read the conversation excerpt below and output STRICT JSON only - no prose, no markdown fences, no preamble.

Output this exact shape:
{"facts":["fact 1","fact 2"],"episode":{"goal":"1-2 sentence goal","summary":"2-4 sentence summary","advanced_goals":[{"goal_title":"...","delta_text":"..."}],"blocked_goals":[{"goal_title":"...","block_reason":"..."}],"open_questions":["...","..."]}}

Rules for facts (apply the INFERABILITY GATE first — it is the most important rule):
- INFERABILITY GATE: before keeping a fact, ask "could a competent engineer who knows general software/tools but has NEVER worked on THIS project infer or guess this?" If YES, DROP it. Keep ONLY genuinely project-specific facts that cannot be known without having been here (our ports, paths, collection names, config values, decisions, IDs, flags, versions, locked-in choices). Generic best-practices and things the reader already knows are noise.
- EPHEMERA GATE (second, equally binding): DROP point-in-time work-state that the next session's events supersede, even when it is project-specific. That means: one-off task/PR/branch/worktree pointers ("the task-3 report is at <path>", "branch X has worktree Y", "PR #12 is open"); session checkpoints and progress markers ("phase 2 is next", "deployed to staging, awaiting review"); temp or scratch locations (paths under AppData\Local\Temp, scratchpad dirs, Desktop exports, render output dirs); and point-in-time counts or sizes ("the ledger had 19,883 lines"). Test: would this still be true AND worth retrieving in 30 days, by someone with no memory of this session? If NO, DROP it. KEEP a fact that names a path ONLY when the sentence asserts a decision, rule, or measured finding that stays true without re-checking the path ("worktrees live under <work-root>\worktrees, never under Drive" is a rule; "the egemma migration was rolled back because X" is a finding). A sentence whose only claim is "artifact X is/lives at path P" is a pointer, not a fact - DROP it, however important the artifact feels right now.
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

Facts and the episode are independent. facts may be [] while the episode is still required: the episode records that this session HAPPENED and what it was about, and a session with nothing durable to keep is still a session.
The episode MUST be populated (goal + summary + advanced_goals + blocked_goals + open_questions, arrays may be empty, even if brief) whenever the conversation has substantive turns - real work done, a decision made, a question worked through, a file or system changed - even when no fact passes the gates above. Only a truly trivial exchange (a greeting, a one-word acknowledgement, no work) may output {"facts":[],"episode":null}.

Conversation excerpt:
$turns
"@

    # Shared Codex mutex: don't fire if C1 (or another L1a) is mid-Codex-call
    # (audit finding 2026-06-08: prior design had separate locks, allowing concurrent
    # Codex calls that contended for ChatGPT subscription quota).
    # 2026-09: a run that finds the lock held now WAITS (polling every 2 s, 20 s by default) before it
    # gives up. Skipping at once cost 24% of extractions (114 of 481 runs in one week), and a session
    # whose last Stop skipped was never extracted. AMS_L1A_LOCK_WAIT_SECONDS overrides the bound.
    # The wait bound is read at the top of the file. On the Claude API this mutex is not taken at all:
    # its reason (one subscription's quota) does not apply there, and the per-session lock taken in
    # Invoke-L1aTranscript is held across the whole window instead.
    $codexLockTaken = $false
    if (-not $UseSessionLock) {
        $lockResult = Acquire-CodexLockWithWait -Owner 'l1a' -WaitSeconds $lockWaitSeconds
        if (-not $lockResult.acquired) {
            Write-MemoryLog -Component 'l1a' -Message "  codex lock held by another worker; skipping this extraction (waited $($lockResult.waited)s)"
            return 'stop'
        }
        $codexLockTaken = $true
        if ($lockResult.waited -gt 0) {
            Write-MemoryLog -Component 'l1a' -Message "  codex lock acquired after $($lockResult.waited)s wait"
            # The throttle and the turn window above were evaluated BEFORE the wait. A run on the same
            # transcript (Stop + PreCompact, parallel Stops) may have finished meanwhile: it marked the
            # throttle and advanced the cursor, so this window is stale, and running codex on it would
            # spend quota and post paraphrased near-duplicates that the server's byte-identical hash
            # cannot catch. Re-check both, and give the lock back if the work is already done.
            $cursorNow = Get-L1aCursor -TranscriptPath $CursorPath
            # (From window 2 on the stamp is this run's own, so only the cursor says whether another run got there.)
            $throttleBlocks = ($WindowNo -eq 1) -and -not (Test-L1aThrottle -Scope $Scope -SessionKey $SessionKey -Seconds $Seconds)
            $cursorReached = ($cursorNow -ne $cursor) -and ($advanceTo -gt 0) -and ($cursorNow -ge $advanceTo)
            if ($throttleBlocks -or $cursorReached) {
                Write-MemoryLog -Component 'l1a' -Message '  another run finished while this one waited for the lock; skipping this extraction'
                Release-CodexLock
                return 'stop'
            }
        }
    }

    Write-MemoryLog -Component 'l1a' -Message '  calling codex subagent for extraction'
    $raw = $null
    $codexStart = Get-Date
    $lastMsgPath = ''
    try {
        # 2026-09-07: pinned to the CLASSIFY model. This is the highest-volume job in the stack
        # (92% of all logged Codex tokens); structured extraction is Astra's weakest measured
        # lane, so it must not silently inherit a synthesis model from config.toml.
        # 60 -> 90s: the observed max over 1,939 logged calls is 62.4s, i.e. 60s has already
        # been breached once; 90s is ~3x p99 (29.6s).
        $lastMsgPath = New-CodexLastMessagePath
        $raw = Invoke-CodexSubagent -Prompt $prompt -ReasoningEffort $script:CodexEffortExtractor -TimeoutSeconds 90 -Model $script:AmCodexModelClassify -LastMessagePath $lastMsgPath
    } catch {
        Write-MemoryLog -Component 'l1a' -Message "  codex subagent failed: $_"
        $failOutcome = if ("$_" -like '*timed out*') { 'timeout' } else { 'exit_nonzero' }
        Write-CodexUsageLog -Component 'l1a' -Status 'error' -DurationMs ([int]((Get-Date) - $codexStart).TotalMilliseconds) `
            -ModelRequested $script:AmCodexModelClassify -EffortRequested $script:CodexEffortExtractor -Outcome $failOutcome
        Remove-CodexLastMessagePath -Path $lastMsgPath   # every exit path clears its own -o file
        if ($codexLockTaken) { Release-CodexLock }
        return 'stop'
    }
    if ($codexLockTaken) { Release-CodexLock }
    $codexDurationMs = [int]((Get-Date) - $codexStart).TotalMilliseconds
    $codexTokens = (Parse-CodexTokenUsage -RawOutput $raw)

    $codexHdr = Parse-CodexHeader -RawOutput $raw
    if ([string]::IsNullOrWhiteSpace($raw)) {
        Write-MemoryLog -Component 'l1a' -Message '  codex returned empty'
        Write-CodexUsageLog -Component 'l1a' -Status 'error' -DurationMs $codexDurationMs -TokensUsed $codexTokens `
            -ModelRequested $script:AmCodexModelClassify -EffortRequested $script:CodexEffortExtractor `
            -ModelResolved $codexHdr.Model -EffortResolved $codexHdr.Effort -Outcome 'empty'
        Remove-CodexLastMessagePath -Path $lastMsgPath
        return 'stop'
    }

    # Extract just the model response from Codex's verbose output, then parse JSON
    $modelText = Get-CodexResponseText -RawOutput $raw -LastMessagePath $lastMsgPath
    Remove-CodexLastMessagePath -Path $lastMsgPath
    $parsed = Extract-JsonFromText -Text $modelText -ExpectedKey 'facts'
    if ($null -eq $parsed) {
        $preview = if ($raw.Length -gt 200) { $raw.Substring(0, 200) } else { $raw }
        Write-MemoryLog -Component 'l1a' -Message "  json parse failed; preview: $preview"
        # 2026-09-07: this branch was the ONLY exit path that wrote no ledger row, which is why
        # the 5 observed parse failures never appeared among 1,990 usage rows. A failure the
        # ledger cannot count is a failure nobody fixes.
        Write-CodexUsageLog -Component 'l1a' -Status 'error' -DurationMs $codexDurationMs -TokensUsed $codexTokens `
            -ModelRequested $script:AmCodexModelClassify -EffortRequested $script:CodexEffortExtractor `
            -ModelResolved $codexHdr.Model -EffortResolved $codexHdr.Effort -Outcome 'parse_fail'
        return 'stop'
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

    # The transcript the session actually lives in: it differs from -TranscriptPath only for a
    # PreCompact snapshot (a temp copy named precompact-snap-<PID>.jsonl, whose directory routes
    # to no brand). Workspace and brand are read from THIS path, for the facts and the episode alike.
    $sourceTranscript = $SourceTranscript

    # C3: resolve the brand BEFORE the facts loop. The episode below used to be the only record
    # that carried a brand (the call sat after the loop), so every fact was brand-neutral and
    # surfaced in every brand's recall. The map is read once; each fact is routed with its own
    # text, which only matters in a content-rule workspace (a path rule wins first). NOTE: plain
    # $sourceTranscript, NOT ($TranscriptPath ?? '') - the ?? null-coalescing operator is PS7-only
    # and the Stop hook runs this under Windows PowerShell 5.1, where ?? is a PARSE ERROR that
    # silently kills the ENTIRE worker (root cause of the 2026-06-16 capture outage).
    # $sourceTranscript is never $null (a [string] param, default ''), so ?? would be redundant.
    $brandMap = $null
    $brandInfo = @{ brand = $null; workspace = $null; project = $null }
    try {
        $brandMap = Get-BrandMap
        $brandInfo = Get-BrandFromTranscriptPath -Path $sourceTranscript -Map $brandMap
    } catch {
        Write-MemoryLog -Component 'l1a' -Message "  brand routing failed (facts post brand-neutral): $_"
    }

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
            $factMeta = @{
                event = $EventName
                tier = 'evidence'
                extracted_at = (Get-Date).ToString('o')
            }
            $factBrand = $null
            try { $factBrand = Resolve-BrandFromMap -Map $brandMap -Path $sourceTranscript -Text $fact } catch { $factBrand = $null }
            if ($factBrand) { $factMeta['brand'] = $factBrand }   # unrouted stays brand-neutral: no key at all
            $memId = Add-Mem0Memory -Text $fact -Source 'l1a-extractor' -Metadata $factMeta
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
    # The box-wide last-l1a stamp is written whatever the scope (the health and verifier rows read it).
    Mark-L1aThrottle -Scope $Scope -SessionKey $SessionKey
    # This window was processed; the cursor goes to where the window stopped (the whole length seen,
    # for the tail read), so the next window or run starts exactly there.
    Set-L1aCursor -TranscriptPath $CursorPath -Bytes $consumedTo

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

            # The episode's session: the hook's session id when the spawner passed one, else the
            # transcript file name (format: <project-dir>/<uuid>.jsonl). A PreCompact snapshot is
            # named precompact-snap-<PID>, which is not a session.
            # (PowerShell variables are case-insensitive: the local must not shadow the -SessionId parameter.)
            $episodeSessionId = Resolve-L1aSessionId -SessionId $EpisodeSessionHint -TranscriptPath $ReadPath

            # brand/workspace/project: $brandInfo was resolved above from $sourceTranscript (the real
            # transcript, not a PreCompact snapshot), before the facts loop.

            # Best-effort started_at: use transcript file mtime as a proxy for session start
            $sessionStartedAt = (Get-Date).ToString('o')
            if ($sourceTranscript -and (Test-Path $sourceTranscript)) {
                try {
                    $sessionStartedAt = (Get-Item $sourceTranscript).CreationTimeUtc.ToString('o')
                } catch {}
            }

            # v0.16: force-array normalization (HIGH-2 fix: Codex sometimes returns a single
            # PSCustomObject instead of a 1-element array when there is only one item.
            # @(...) coerces: null → @(), single object → 1-element array, array → unchanged).
            $advancedGoals  = @($parsed.episode.advanced_goals  | Where-Object { $_ })
            $blockedGoals   = @($parsed.episode.blocked_goals   | Where-Object { $_ })
            $openQuestions  = @($parsed.episode.open_questions  | Where-Object { $_ })

            $episodePayload = @{
                session_id     = $episodeSessionId
                started_at     = $sessionStartedAt
                ended_at       = (Get-Date).ToUniversalTime().ToString('o')
                transcript_path = $sourceTranscript
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
            # 1.38.3: 30 s, not 5. This worker is detached, and the authority embeds the episode
            # before it answers: on a cold embedder that took about 6 s, so the client gave up while
            # the server went on to store the episode (8 false "episode post failed" lines,
            # 2026-10-19..21).
            Invoke-RestMethod -Uri "$($script:Mem0Url)/v1/episodes" `
                -Method Post `
                -Body ([System.Text.Encoding]::UTF8.GetBytes($episodePayload)) `
                -ContentType 'application/json' `
                -Headers @{'X-API-Key' = $apiKey} `
                -TimeoutSec 30 | Out-Null
            Write-MemoryLog -Component 'l1a' -Message "  posted episode for session $episodeSessionId (goal: $($parsed.episode.goal.Substring(0, [Math]::Min(80, $parsed.episode.goal.Length))))"
        } catch {
            # Best-effort: POST /v1/episodes is live (v0.20 Phase B). A failure here is
            # non-fatal — the session's durable facts already landed in mem0; the episode
            # (and its routed ship-logs) is the only loss.
            Write-MemoryLog -Component 'l1a' -Message "  episode post failed (non-fatal; durable facts already in mem0): $_"
        }
    }

    # More to read past this window's cursor? (Never true on a tail read: that sets the cursor to the
    # length seen.) Stopping on 'stop' is how a run ends early when a window has nothing to give.
    if ($consumedTo -lt $advanceTo) { return 'continue' }
    return 'stop'
}

try {
    Invoke-L1aMain
} catch {
    Write-MemoryLog -Component 'l1a' -Message "  unhandled error: $_"
} finally {
    Remove-Item env:L1A_REENTRANT -ErrorAction SilentlyContinue
}

exit 0
