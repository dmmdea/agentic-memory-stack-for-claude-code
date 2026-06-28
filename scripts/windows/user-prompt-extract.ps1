# user-prompt-extract.ps1 — v0.17 Phase 0.A + 0.B UserPromptSubmit hook
#
# Fires on every user message. Jobs:
#   0.A: upsert an in_progress episode checkpoint so partial state survives
#        VS Code restarts (the Stop hook may never fire on interruption).
#   0.B: Auto-capture user decisions: if the last assistant turn had numbered options
#        AND the current prompt is short + numeric, write to mem0 as tier=stable +
#        append to ~/.mem0/recent-decisions.jsonl.
#   0.D: proactive memory surfacing ([MEMORY CONTEXT] block on stdout).
#
# v0.20 A.3 (latency directive 2026-06-12): 0.A + 0.D now cost ONE HTTP
# round-trip — POST /v1/context/bundle returns admission-gated memories + open
# goals + open questions and performs the episode-checkpoint upsert server-side
# (the hook previously made 4+ sequential calls: checkpoint, search, goals,
# open_questions; measured ~1.1-1.2s wall). Trivial / rate-limited prompts make
# a single POST /v1/episodes/checkpoint instead. Client-side admission
# (Select-AdmittedMemoryResults) still filters the bundle's memories.
#
# v0.20 A.5: the bundle call + [MEMORY CONTEXT] rendering are served by the
# resident daemon (mem0-hook-daemon.ps1, named pipe) when it is up — the
# daemon holds the warm .NET HTTP stack + preloaded lib, removing the
# per-spawn HTTP-init cost. Cheap client-side stages (stdin parse, rate
# limit, fixtures, brand inference, 0.B) stay HERE. ANY daemon failure (no
# pipe / timeout / bad response / lib-hash mismatch) falls back to the A.3
# inline path below, unchanged; on no-pipe the daemon is respawned detached
# for the NEXT prompt (never blocking this one).
#
# v0.20 A.3 perf rules for THIS file (same as pre-tool-check.ps1 A.2):
#   - The happy path executes ZERO cmdlets. In PS5.1 the first cmdlet of a
#     module pays the module load: Utility ~75ms (Get-Random, Get-Date,
#     ConvertFrom/To-Json, Invoke-RestMethod, Select-Object, Sort-Object),
#     Management ~45ms (Join-Path, Test-Path, New-Item, Add-Content).
#   - JSON: System.Web.Script.Serialization.JavaScriptSerializer loaded via
#     reflection (PS5.1-only — fine, hooks run under powershell.exe 5.1).
#   - HTTP: raw System.Net.HttpWebRequest (no proxy, explicit timeouts).
#   - Function existence checks use $ExecutionContext.SessionState (Get-Command
#     on a missing name triggers ~1.7s PSModulePath discovery; Test-Path
#     Function:\ loads Management).
#   Cmdlets remain only on rare branches (0.B transcript scan, fixture prune).
#
# H9 fix: realistic budget is 1500ms (not 500ms). A Stopwatch enforces this
# budget before the network call; failures are logged to
# ~/.claude/logs/user-prompt-extract.log but never block the user's next
# message (exit 0 always).
#
# Claude Code hook contract: hook event data arrives via stdin as JSON with fields
#   { hook_event_name, prompt, transcript_path }
# Same contract as Stop hook (stdin JSON, not env vars).
#
# v0.20 A.6: the REGISTERED UserPromptSubmit command is now the compiled thin
# client (mem0-hook-client.exe, built from mem0-hook-client.cs by
# build-hook-client.ps1) — it performs the daemon transaction itself and
# spawns THIS script only when PowerShell work remains:
#   -SkipDaemon                -> the exe's daemon txn FAILED (no pipe /
#      timeout / garbage / hash mismatch). Run the FULL inline path below but
#      never re-probe the daemon — the exe already paid the probe/connect and
#      already triggered the detached respawn on the no-pipe case.
#   MEM0_HOOK_DAEMON_SERVED=1  -> the exe's daemon txn SUCCEEDED and the
#      [MEMORY CONTEXT] block is already emitted on the exe's stdout. We exist
#      ONLY for Phase 0.B (decision capture, exe pre-gated): parse stdin
#      locally and take the DaemonServedBundle path (sections 1-4 skipped).
# Registered directly (no exe), neither is set and behavior is identical to A.5.

param([switch]$SkipDaemon)

$ErrorActionPreference = 'SilentlyContinue'

# H9: budget enforcement — start the stopwatch immediately at hook entry.
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$BudgetMs = 1500

# ---------------------------------------------------------------------------
# 0. Setup: log file + JSON serializer + API key
# ---------------------------------------------------------------------------

$LogDir  = $env:USERPROFILE + '\.claude\logs'
$LogFile = $LogDir + '\user-prompt-extract.log'

function Write-Log {
    param([string]$Msg)
    try {
        if (-not [System.IO.Directory]::Exists($script:LogDir)) { [void][System.IO.Directory]::CreateDirectory($script:LogDir) }
        [System.IO.File]::AppendAllText($script:LogFile, '[' + [System.DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss') + '] ' + $Msg + [System.Environment]::NewLine)
    } catch {}
}

# v0.20 A.5: JavaScriptSerializer init + transport tuning MOVED below the
# daemon fast path (they cost ~65ms of assembly-load/JIT per spawn and the
# fast path needs neither — the daemon parses stdin and makes the HTTP call).
# They run before anything that parses JSON or POSTs (inline fallback + 0.B).

function Invoke-Mem0Post {
    # Raw POST → response text. No proxy lookup, explicit timeout. Throws on
    # HTTP/network errors (callers wrap in try/catch like the old IRM calls).
    param([string]$Uri, [string]$Body, [string]$ApiKey, [int]$TimeoutMs = 3000)
    $req = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create($Uri)
    $req.Method = 'POST'
    $req.ContentType = 'application/json'
    $req.Headers.Add('X-API-Key', $ApiKey)
    $req.Timeout = $TimeoutMs
    $req.ReadWriteTimeout = $TimeoutMs
    $req.Proxy = $null
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
    $req.ContentLength = $bytes.Length
    $rs = $req.GetRequestStream()
    try { $rs.Write($bytes, 0, $bytes.Length) } finally { $rs.Close() }
    $resp = $req.GetResponse()
    try {
        $sr = [System.IO.StreamReader]::new($resp.GetResponseStream())
        try { return $sr.ReadToEnd() } finally { $sr.Close() }
    } finally { $resp.Close() }
}

$BaseUrl = 'http://127.0.0.1:18791'

# v0.18 MED-17: hook contract version stamped on the checkpoint/bundle POSTs
# and saved fixtures. v0.20 A.3 bumped '17.0' -> '20.0' in the SAME commit that
# adds '20.0' to the server's KNOWN_HOOK_CONTRACT_VERSIONS (the wire contract
# changed: one batched /v1/context/bundle call replaces the checkpoint + search
# + goals + open_questions fan-out). Bump again only IF the contract changes.
$HookContractVersion = '20.0'

# v0.18 MED-20/21: pure logic (decision predicate + 0.D admission policy) lives in
# user-prompt-lib.ps1 so Pester can test it without running this pipeline.
# v0.19 M13: a failed dot-source previously fail-opened with a quiet WARN that
# nothing monitors — 0.B/0.D silently died. Log LOUDLY (ERROR + consequence +
# remediation); Test-MemoryStack's deployed-hooks-freshness row catches the
# missing/stale-lib deploy state independently.
# v0.20 A.2: dot-source BEFORE the api-key read so the key can come from
# Get-Mem0ApiKeyCached (local 1h cache) instead of the per-spawn UNC
# \\wsl.localhost read (~90ms measured).
$ScriptDir = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path)
$LibPath = $ScriptDir + '\user-prompt-lib.ps1'
if ([System.IO.File]::Exists($LibPath)) {
    try {
        . $LibPath
    } catch {
        Write-Log "ERROR: user-prompt-lib.ps1 dot-source FAILED at $LibPath : $($_.Exception.Message) - Phase 0.B decision capture and 0.D memory injection are DISABLED until fixed (redeploy user-prompt-lib.ps1 from the stack repo's scripts/windows to $LibPath)"
    }
} else {
    Write-Log "ERROR: user-prompt-lib.ps1 NOT FOUND at $LibPath - Phase 0.B decision capture and 0.D memory injection are DISABLED until deployed (deploy user-prompt-lib.ps1 from the stack repo's scripts/windows to $LibPath)"
}

function Test-FunctionAvailable {
    # Engine-level lookup: no module auto-discovery (Get-Command on a missing
    # name costs ~1.7s), no Management load (Test-Path Function:\ would).
    param([string]$Name)
    return $null -ne $ExecutionContext.SessionState.InvokeCommand.GetCommand($Name, 'Function')
}

# ---------------------------------------------------------------------------
# 0.5 v0.20 A.5 FAST PATH: hand VERBATIM stdin to the resident daemon.
# The daemon (warm HTTP stack, preloaded lib) performs stdin parse, fixture
# sampling, session-id extraction, brand inference, triviality gate, rate
# limit, the bundle/checkpoint POST and the [MEMORY CONTEXT] render —
# mirroring sections 1-4 below exactly. This client touches NO JSON
# machinery here (request = string concat, response = anchored regexes over
# base64 fields) because JSS init costs ~65ms/spawn.
# ANY failure -> $fast stays $null -> sections 1-4 below run UNCHANGED
# (fail-open; the daemon is an accelerator, never a dependency). On no-pipe
# the daemon is respawned detached for the NEXT prompt. The handshake digest
# (v0.21 Phase B M3/M6: combined SHA256 over the deployed lib + the deployed
# mem0-hook-daemon.ps1, via Get-HandshakeHash) guarantees a daemon never serves
# logic older than EITHER deployed file — a lib OR a daemon-only edit forces
# inline + shutdown signal -> fresh daemon next prompt.
# ---------------------------------------------------------------------------

$stdinRaw = $null
try { $stdinRaw = [Console]::In.ReadToEnd() } catch { $stdinRaw = $null }

$script:DaemonServedBundle = $false
$fastPrompt = $null; $fastTranscript = $null; $fastSession = $null; $fastBrand = $null

# v0.20 A.6 — 0.B-only mode: mem0-hook-client.exe already completed the daemon
# transaction (sections 1-4 done daemon-side, block already emitted) and
# pre-gated a plausible decision prompt. Parse stdin locally (off the hot path
# now — the exe is the hot path) and join the DaemonServedBundle flow so ONLY
# Phase 0.B runs. Any surprise here exits 0: the inline path must never run in
# this mode (it would double-pay the bundle/checkpoint the daemon already did).
if ($stdinRaw -and ($env:MEM0_HOOK_DAEMON_SERVED -eq '1')) {
    try {
        [void][System.Reflection.Assembly]::LoadWithPartialName('System.Web.Extensions')
        $jss0B = [Activator]::CreateInstance([System.Web.Script.Serialization.JavaScriptSerializer])
        $jss0B.MaxJsonLength = 16MB
        $he0B = $jss0B.DeserializeObject($stdinRaw)
        if ($he0B -and $he0B.hook_event_name -eq 'UserPromptSubmit' -and $he0B.prompt -and
            $he0B.transcript_path -and [System.IO.File]::Exists([string]$he0B.transcript_path)) {
            $needs0B = $false
            try { $needs0B = Test-DecisionLikePrompt -Prompt $he0B.prompt } catch { $needs0B = $false }
            if (-not $needs0B) { exit 0 }   # exe pre-gate disagrees -> nothing left to do
            $script:DaemonServedBundle = $true
            $fastPrompt = $he0B.prompt; $fastTranscript = [string]$he0B.transcript_path
            $fastSession = $null   # section 2 re-derives it from the transcript path
            if (Test-FunctionAvailable 'Get-InferredBrandFromPath') {
                $fastBrand = Get-InferredBrandFromPath -Path $fastTranscript
            }
            Write-Log "0.B-only mode (exe daemon-served): decision-like prompt, running capture"
        } else { exit 0 }
    } catch { exit 0 }
}
elseif ($stdinRaw -and (-not $SkipDaemon) -and (Test-FunctionAvailable 'Invoke-DaemonRawTransaction')) {
    try {
        $libHashNow = Get-HandshakeHash -ScriptDir $ScriptDir
        $fast = $null
        if ($libHashNow) {
            $fast = Invoke-DaemonRawTransaction -RawStdin $stdinRaw -ExpectedLibHash $libHashNow -SpawnDaemonPath ($ScriptDir + '\mem0-hook-daemon.ps1')
        }
        if ($fast) {
            $script:DaemonServedBundle = $true
            if ($fast.context_block) { [Console]::Out.WriteLine($fast.context_block) }
            Write-Log "0.A+0.D served by daemon: session=$($fast.session_id) $($fast.diag)"
            $fastPrompt = $fast.prompt; $fastTranscript = $fast.transcript_path
            $fastSession = $fast.session_id; $fastBrand = $fast.brand
            # Only Phase 0.B remains client-side. Cheap pre-gates here; if no
            # decision capture is possible (the overwhelmingly common case),
            # exit now — the fallback init below (JSS/transport/api-key)
            # never runs on this path.
            $needs0B = $false
            if ($fastPrompt -and $fastTranscript -and [System.IO.File]::Exists($fastTranscript)) {
                try { $needs0B = Test-DecisionLikePrompt -Prompt $fastPrompt } catch { $needs0B = $false }
            }
            if (-not $needs0B) { exit 0 }
        }
    } catch { $script:DaemonServedBundle = $false }
}

# ---------------------------------------------------------------------------
# 0.6 Fallback / 0.B initialization (moved here by A.5 — identical content)
# ---------------------------------------------------------------------------

# JSON without the Utility module (v0.20 A.3): JavaScriptSerializer handles
# both directions; DeserializeObject returns Dictionary<string,object> whose
# keys PowerShell exposes as properties (read AND write), so downstream code
# is shape-compatible with ConvertFrom-Json output.
[void][System.Reflection.Assembly]::LoadWithPartialName('System.Web.Extensions')
$script:Jss = [Activator]::CreateInstance([System.Web.Script.Serialization.JavaScriptSerializer])
$script:Jss.MaxJsonLength = 16MB

# Transport tuning (v0.20 A.3): Expect100Continue=true (the PS5.1/.NET default)
# makes every POST send "Expect: 100-continue" and stall for the interim
# response (~80-100ms measured against uvicorn); Nagle adds small-payload
# coalescing delay on loopback.
[System.Net.ServicePointManager]::Expect100Continue = $false
[System.Net.ServicePointManager]::UseNagleAlgorithm = $false

# v0.20 A.2: API key via local cache (UNC read only on cache miss/stale).
# Direct UNC fallback preserved for a missing/broken lib deploy.
$ApiKeyPath = '\\wsl.localhost\__WSL_DISTRO__\home\__WSL_USER__\.mem0\api-key'
$apiKey = $null
if (Test-FunctionAvailable 'Get-Mem0ApiKeyCached') {
    $apiKey = Get-Mem0ApiKeyCached
} else {
    try { $apiKey = ([System.IO.File]::ReadAllText($ApiKeyPath)).Trim() } catch {}
}
if (-not $apiKey) {
    Write-Log "WARN: could not read API key - skipping"
    exit 0
}

# ---------------------------------------------------------------------------
# 1. Read stdin JSON (Claude Code hook contract)
# (v0.20 A.5: stdin was already consumed above; parse from $stdinRaw. When
# the daemon served the bundle, sections 1-4 are skipped — it already did
# the same work — and 0.B variables come from the daemon response.)
# ---------------------------------------------------------------------------

$hookEvent   = $null
$prompt      = $null
$transcriptPath = $null

if ($script:DaemonServedBundle) {
    $prompt         = $fastPrompt
    $transcriptPath = $fastTranscript
} else {
try {
    if ($stdinRaw) {
        $hookEvent = $script:Jss.DeserializeObject($stdinRaw)
        $prompt    = $hookEvent.prompt
        $transcriptPath = $hookEvent.transcript_path
        # v0.17 F.3.3: save payload fixture for hook contract regression corpus (v0.18 MED-14: 1-in-10 sampling)
        # v0.20 Phase F (L9): the byte-faithful write (v0.19 L13 — verbatim
        # $stdinRaw, UTF-8 no BOM, key order untouched, contract version in the
        # FILENAME) + keep-20 prune moved to the lib (Save-HookFixture), shared
        # with the daemon and pre-tool-check so the three writers cannot drift.
        # 1-in-10 sample via guid hash (Get-Random = Utility load) stays here.
        try {
            if (Test-FunctionAvailable 'Save-HookFixture') {
                [void](Save-HookFixture -FixtureDir ($env:USERPROFILE + '\.claude\state\hook-fixtures') `
                    -EventName 'UserPromptSubmit' -ContractVersion $HookContractVersion `
                    -RawBytes $stdinRaw -SampleRoll (([Math]::Abs([guid]::NewGuid().GetHashCode()) % 10) -eq 0))
            }
        } catch {}
    }
} catch {
    Write-Log "WARN: stdin parse failed: $($_.Exception.Message)"
    exit 0
}

if ($hookEvent.hook_event_name -ne 'UserPromptSubmit') {
    # Not the right event — exit silently (hook config should match, but guard anyway)
    exit 0
}

if (-not $prompt) {
    # Empty prompt — nothing to checkpoint
    exit 0
}
}

# ---------------------------------------------------------------------------
# 2. Extract session_id from transcript filename (UUID from path)
# ---------------------------------------------------------------------------

$sessionId = $null
if ($script:DaemonServedBundle) {
    # v0.20 A.5: the daemon already extracted it from the same transcript path
    $sessionId = $fastSession
}
if ((-not $sessionId) -and $transcriptPath) {
    $basename = [System.IO.Path]::GetFileNameWithoutExtension($transcriptPath)
    # Transcript filename is the session UUID (e.g. a71c302b-ecb7-413c-874f-aacd5955e3c5)
    if ($basename -match '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
        $sessionId = $basename
    }
}
if (-not $sessionId) {
    # Fallback: generate a deterministic-ish ID from the transcript path hash
    $fnwe = [System.IO.Path]::GetFileNameWithoutExtension($transcriptPath)
    if (-not $fnwe) { $fnwe = 'noop' }
    $sessionId = "unknown-$fnwe"
    Write-Log "WARN: could not extract UUID session_id from path '$transcriptPath', using '$sessionId'"
}

# ---------------------------------------------------------------------------
# 3. Infer brand/workspace/project from transcript path
# ---------------------------------------------------------------------------

$brand     = $null
$workspace = 'ai-ecosystem'
$project   = $null

# v0.19 M14/L10: inference extracted to user-prompt-lib.ps1 (Pester-testable);
# v1.0 Phase 7B: brand rules are operator-configured in brands.json. $brand stays
# $null for unknown paths — Select-AdmittedMemoryResults then fails closed (null-
# brand memories only; brand-tagged ones never leak into an unrecognized session).
if ($script:DaemonServedBundle) {
    $brand = $fastBrand   # v0.20 A.5: same inference, done daemon-side
} elseif ($transcriptPath -and (Test-FunctionAvailable 'Get-InferredBrandFromPath')) {
    $brand = Get-InferredBrandFromPath -Path $transcriptPath
}

# v0.22 Pillar 1 + Pillar 2 (B latency fix): resolve initiative + tier for the
# inline bundle request. Read the per-session sidecar (written at SessionStart)
# FIRST so the common case skips the ~70ms-per-prompt Get-SessionInitiative git
# spawn AND the transcript scan. Sidecar MISS -> fall back to the pre-v0.22
# per-prompt computation (git spawn) + transcript-tail tier resolution.
# Two initiatives can share one brand (agentic-memory-stack and local-offload
# both = ai-ecosystem); initiative is the distinguishing axis the server uses to
# scope goal/OQ injection. tier is plumbed through but the server ignores it
# until Phase D (frontier == today's behavior). Daemon-served prompts already
# resolved both daemon-side, so this only runs on the inline path.
$initiative = $null
$tier = 'frontier'
if (-not $script:DaemonServedBundle) {
    $sidecar = $null
    if (Test-FunctionAvailable 'Get-SessionSidecar') {
        try { $sidecar = Get-SessionSidecar -SessionId $sessionId } catch { $sidecar = $null }
    }
    if ($sidecar) {
        $initiative = $sidecar.initiative
        if (-not [string]::IsNullOrWhiteSpace([string]$sidecar.tier)) { $tier = [string]$sidecar.tier }
    } else {
        $cwd = $null
        try { if ($hookEvent) { $cwd = [string]$hookEvent.cwd } } catch { $cwd = $null }
        if ($cwd -and (Test-FunctionAvailable 'Get-SessionInitiative')) {
            try { $initiative = Get-SessionInitiative -Cwd $cwd } catch { $initiative = $null }
        }
        if (Test-FunctionAvailable 'Get-SessionTier') {
            try { $tier = Get-SessionTier -SessionId $sessionId -TranscriptPath $transcriptPath } catch { $tier = 'frontier' }
        }
        if ([string]::IsNullOrWhiteSpace($tier)) { $tier = 'frontier' }
    }
}

# ---------------------------------------------------------------------------
# 4. v0.20 A.3: ONE network round-trip per prompt.
# Substantive prompts -> POST /v1/context/bundle (admission-gated memories +
# open goals + open questions in one payload; the 0.A episode checkpoint is
# performed server-side as part of the call).
# Trivial / rate-limited prompts -> POST /v1/episodes/checkpoint only.
# ---------------------------------------------------------------------------

# v0.20 A.5: when the daemon served this prompt, ALL of section 4 (gate,
# rate limit, bundle/checkpoint, render) already happened daemon-side with
# identical logic — we are only here for Phase 0.B.
if (-not $script:DaemonServedBundle) {

# Triviality gate (was inline in Phase 0.D): <3 words or common short replies
# never trigger proactive surfacing.
$isTrivial = $true
$promptForSearch = $null
if ($prompt -and $prompt.Length -gt 5) {
    $promptForSearch = if ($prompt.Length -gt 500) { $prompt.Substring(0, 500) } else { $prompt }
    $trivial = @('continue', 'yes', 'no', 'ok', 'okay', 'sure', 'go', 'next', 'stop', '1', '2', '3', 'a', 'b', 'c', 'thanks', 'thx')
    $wordCount = ($promptForSearch -split '\s+').Count
    $isTrivial = $wordCount -lt 3 -or $trivial -contains $promptForSearch.ToLower().Trim()
}

# v0.18 MED-16: proactive-search rate-limit (1s cooldown via state file).
# Closes the rapid-fire DoS vector: each substantive prompt fires a mem0
# search fan-out server-side; a prompt flood multiplies that load. When
# rate-limited we skip ONLY the bundle (proactive surfacing) — the checkpoint
# still fires. Fail open: a corrupt/unreadable state file must never crash
# the hook.
# v0.19 L2: (1) this section only READS the stamp — the token is consumed
# (written) right before the bundle call actually fires, so trivial/decision
# prompts that never search don't burn the cooldown for the next prompt;
# (2) the state file is keyed by session_id (user-prompt-rate-limit-<session_id>)
# so concurrent Claude Code windows stop starving each other's surfacing, with
# a cheap sweep deleting stale state files (>1h) on each pass.
# v0.20 Phase F (L9): decision + sweep extracted to the lib
# (Get-RateLimitDecision / Invoke-RateLimitStateSweep — Pester-pinned; the
# daemon's bundle_raw mirror calls the SAME functions, so the two paths cannot
# drift). Consume-on-fire stays below. Missing lib -> fail open (0.D admission
# is lib-dependent anyway and R9 monitors lib deploys).
$rateLimited = $false
$rateLimitState = $null
$cooldownMs = 1000  # min 1s between proactive searches (per session)
try {
    $stateDir = $env:USERPROFILE + '\.claude\state'
    if (Test-FunctionAvailable 'Get-RateLimitDecision') {
        $rlDecision = Get-RateLimitDecision -StateDir $stateDir -SessionId $sessionId `
            -NowFileTimeUtc ([System.DateTime]::Now.ToFileTimeUtc()) -CooldownMs $cooldownMs
        $rateLimited = $rlDecision.RateLimited
        $rateLimitState = $rlDecision.StatePath
        Invoke-RateLimitStateSweep -StateDir $stateDir -MaxAgeHours 1
    }
} catch {
    Write-Log "0.D rate-limit check failed ($($_.Exception.Message)); failing open"
    $rateLimited = $false
}

$contextBlock = $null
if ($sw.ElapsedMilliseconds -gt $BudgetMs) { Write-Log "H9: budget exceeded before network call ($($sw.ElapsedMilliseconds)ms)"; exit 0 }

if ((-not $isTrivial) -and (-not $rateLimited)) {
    # v0.19 L2 (MED-16): consume the cooldown token HERE — only when proactive
    # surfacing actually fires, never for prompts that skip it.
    try { if ($rateLimitState) { [System.IO.File]::WriteAllText($rateLimitState, [string][System.DateTime]::Now.ToFileTimeUtc()) } } catch {}
    try {
        $bundleBody = $script:Jss.Serialize(@{
            session_id            = $sessionId
            prompt                = $promptForSearch
            brand                 = $brand
            workspace             = $workspace
            project               = $project
            initiative            = $initiative   # v0.22 Pillar 1: cwd-derived repo leaf
            tier                  = $tier          # v0.22 Pillar 2: consuming-model tier (server ignores until Phase D)
            transcript_path       = $transcriptPath
            hook_contract_version = $HookContractVersion
        })

        $bundleText = Invoke-Mem0Post -Uri "$BaseUrl/v1/context/bundle" -Body $bundleBody -ApiKey $apiKey -TimeoutMs 3000
        $bundleR = $script:Jss.DeserializeObject($bundleText)

        Write-Log "0.A+0.D bundle: session=$sessionId episode_id=$($bundleR.checkpoint.episode_id) action=$($bundleR.checkpoint.action) memories=$(@($bundleR.memories).Count) goals=$(@($bundleR.goals).Count) oq=$(@($bundleR.open_questions).Count)"

        # Render [MEMORY CONTEXT] — client-side admission Layers 1/2/3 applied
        # inside (Select-AdmittedMemoryResults); rejection audit unchanged.
        if (Test-FunctionAvailable 'Format-MemoryContextBlock') {
            # v0.22 D: render per tier (resolved above: sidecar -> transcript ->
            # frontier). frontier/mid = full format; small = flat + legend.
            $contextBlock = Format-MemoryContextBlock -Bundle $bundleR -Brand $brand -Tier $tier
        }
    } catch {
        Write-Log "0.A/0.D bundle FAILED for session=$sessionId : $($_.Exception.Message)"
    }
} else {
    if ($rateLimited) { Write-Log "0.D rate-limited: proactive surfacing skipped (cooldown ${cooldownMs}ms)" }
    try {
        $body = $script:Jss.Serialize(@{
            session_id            = $sessionId
            transcript_path       = $transcriptPath
            prompt_text           = $prompt.Substring(0, [Math]::Min(300, $prompt.Length))
            brand                 = $brand
            workspace             = $workspace
            project               = $project
            hook_contract_version = $HookContractVersion   # v0.18 MED-17
        })

        $respText = Invoke-Mem0Post -Uri "$BaseUrl/v1/episodes/checkpoint" -Body $body -ApiKey $apiKey -TimeoutMs 1000
        $resp = $script:Jss.DeserializeObject($respText)

        Write-Log "0.A checkpoint: session=$sessionId episode_id=$($resp.episode_id) action=$($resp.action)"
    } catch {
        Write-Log "0.A checkpoint FAILED for session=$sessionId : $($_.Exception.Message)"
        # Don't exit — still attempt 0.B if transcript is available
    }
}

# Emit the context block (stdout -> Claude's context) before 0.B bookkeeping.
# Direct console write — piping a multi-KB string through Write-Output costs
# ~60-80ms of pipeline setup on PS5.1 (v0.20 A.3 measurement).
if ($contextBlock) { [Console]::Out.WriteLine($contextBlock) }

}   # end v0.20 A.5 daemon-served guard (section 4)

# ---------------------------------------------------------------------------
# 5. Phase 0.B: Auto-capture user decisions
# ---------------------------------------------------------------------------

# --- Phase 0.B decision detection (wrapped so early exits don't block the tail) ---
$questionPreview = $null

# Only attempt decision capture if we have a transcript and a short decision-like prompt
# (v0.18 MED-20: predicate extracted to user-prompt-lib.ps1 Test-DecisionLikePrompt)
if ($transcriptPath -and [System.IO.File]::Exists($transcriptPath)) {
    $isDecisionLike = $false
    try { $isDecisionLike = Test-DecisionLikePrompt -Prompt $prompt } catch {
        Write-Log "0.B: Test-DecisionLikePrompt unavailable: $($_.Exception.Message)"
    }

    if ($isDecisionLike) {
        # Read last ~8 lines of transcript to find the last assistant turn
        $lastLines = @()
        try {
            $lastLines = Get-Content -Path $transcriptPath -Tail 8 -Encoding UTF8 -ErrorAction Stop
        } catch {
            Write-Log "0.B: could not read transcript: $($_.Exception.Message)"
        }

        # Find the last assistant turn content before the user prompt
        $numberedPattern = [regex]'\n\s*\*{0,2}\d+\.\s|\n\s*[ABCD]\.\s|\n\d+\.\s|\*\*\d+\.'

        for ($i = $lastLines.Count - 1; $i -ge 0; $i--) {
            $line = $lastLines[$i]
            if (-not $line) { continue }

            try {
                $parsed = $script:Jss.DeserializeObject($line)
            } catch { continue }

            # Look for assistant message
            $msgRole    = $null
            $msgContent = $null

            if ($parsed.type -eq 'assistant' -or ($parsed.message -and $parsed.message.role -eq 'assistant')) {
                $msgRole = 'assistant'
                # Extract text content
                if ($parsed.message -and $parsed.message.content) {
                    $content = $parsed.message.content
                    if ($content -is [string]) {
                        $msgContent = $content
                    } elseif ($content -is [array]) {
                        $texts = @()
                        foreach ($c in $content) {
                            if ($c.type -eq 'text' -and $c.text) { $texts += $c.text }
                        }
                        $msgContent = $texts -join "`n"
                    }
                } elseif ($parsed.content) {
                    if ($parsed.content -is [string]) { $msgContent = $parsed.content }
                }
            }

            if ($msgRole -eq 'assistant' -and $msgContent) {
                # Check if it contains a numbered options pattern
                if ($numberedPattern.IsMatch("`n" + $msgContent)) {
                    $questionPreview = $msgContent.Substring(0, [Math]::Min(300, $msgContent.Length))
                    break
                }
                # Also check multi-line format: multiple "1. " or "2. " occurrences
                $occurrences = ([regex]'\b\d+\.\s').Matches($msgContent).Count
                if ($occurrences -ge 2) {
                    $questionPreview = $msgContent.Substring(0, [Math]::Min(300, $msgContent.Length))
                    break
                }
            }
        }
    }
}

if ($questionPreview) {
    # Build decision record
    $decision = @{
        ts               = [System.DateTime]::Now.ToString('o')
        session_id       = $sessionId
        question_preview = $questionPreview
        answer           = $prompt.Substring(0, [Math]::Min(200, $prompt.Length))
        kind             = 'user-decision'
        transcript_path  = $transcriptPath
    }

    Write-Log "0.B decision detected: session=$sessionId answer='$($decision.answer)' q_preview='$($questionPreview.Substring(0,[Math]::Min(80,$questionPreview.Length)))...'"

    # Append to ~/.mem0/recent-decisions.jsonl (WSL path via UNC)
    $decisionPath = '\\wsl.localhost\__WSL_DISTRO__\home\__WSL_USER__\.mem0\recent-decisions.jsonl'
    try {
        $line = $script:Jss.Serialize($decision)
        [System.IO.File]::AppendAllText($decisionPath, $line + [System.Environment]::NewLine)
        Write-Log "0.B: appended to recent-decisions.jsonl"
    } catch {
        Write-Log "0.B: WARN could not write recent-decisions.jsonl: $($_.Exception.Message)"
    }

    # POST to mem0 as tier=evidence user-decision
    $sessionShort = $sessionId.Substring(0, [Math]::Min(8, $sessionId.Length))
    $memBody = $script:Jss.Serialize(@{
        messages = "User decision (session $sessionShort at $($decision.ts.Substring(0,16))): Q: $($decision.question_preview.Substring(0,[Math]::Min(200,$decision.question_preview.Length))) | A: $($decision.answer)"
        user_id  = '__WSL_USER__'
        infer    = $false
        metadata = @{
            source           = 'user-decision'
            kind             = 'decision'
            tier             = 'evidence'   # POST /v1/memories only accepts evidence|temporal; promote to stable/canonical via CLI if durable
            _stable_intent   = $true        # flag: this decision SHOULD be promoted to stable after review
                                            # v0.19 L3: renamed from 'stable_intent' to the underscore =
                                            # server-internal convention (mem0-mcp-shim's _canonical_intent /
                                            # _insight_intent); server strips BOTH spellings (_INTENT_KEYS)
                                            # so pre-v0.19 records need no migration
            question_preview = $decision.question_preview.Substring(0, [Math]::Min(200, $decision.question_preview.Length))
            session_id       = $sessionId
            brand            = if ($brand) { $brand } else { 'ai-ecosystem' }
        }
    })

    if ($sw.ElapsedMilliseconds -le $BudgetMs) {
        try {
            $null = Invoke-Mem0Post -Uri "$BaseUrl/v1/memories" -Body $memBody -ApiKey $apiKey -TimeoutMs 1000
            Write-Log "0.B: mem0 write OK"
        } catch {
            Write-Log "0.B: WARN mem0 write failed: $($_.Exception.Message)"
        }
    } else {
        Write-Log "H9: budget exceeded before 0.B mem0 write ($($sw.ElapsedMilliseconds)ms); skipping"
    }
}

exit 0
