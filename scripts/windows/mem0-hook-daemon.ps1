# mem0-hook-daemon.ps1 — v0.20 A.5: resident UserPromptSubmit accelerator.
#
# WHY: the inline hook pays per-spawn costs it can never amortize — .NET HTTP
# first-request init (~180ms) on every prompt. This daemon stays resident,
# pays that init ONCE, keeps the loopback connection warm (ServicePoint
# keep-alive), holds the preloaded lib + API key, and serves the
# POST /v1/context/bundle call + [MEMORY CONTEXT] rendering over a named pipe.
#
# HARD CONSTRAINT (A.5 design): the daemon is an ACCELERATOR, never a
# dependency. The client (user-prompt-extract.ps1 via lib Invoke-DaemonBundle)
# treats ANY failure — no pipe, connect timeout, bad response, lib-hash
# mismatch — as "use the inline path", which is byte-identical to A.3.
#
# Protocol (newline-delimited JSON over pipe 'mem0-hook-daemon', 1 instance):
#   request  {op:'bundle', session_id, prompt, brand, workspace, project,
#             transcript_path, hook_contract_version}
#         |  {op:'ping'} | {op:'shutdown'}
#   response {ok, context_block, lib_hash, diag:{...}} (bundle)
#         |  {ok, op, lib_hash} (ping/shutdown) | {ok:false, error, lib_hash}
#
# Staleness handshake (v0.21 Phase B M3/M6): lib_hash = SHA256 over the COMBINED
# digest of the deployed user-prompt-lib.ps1 AND THIS daemon script
# (Sha256Hex(lib) + Sha256Hex(daemon), re-hashed) that THIS process loaded at
# start. Folding the daemon's own bytes in means a daemon-ONLY redeploy (the
# per-prompt orchestration lives here, not in the lib) ALSO flips the digest.
# Every response carries it; the client compares against the current combined
# digest and sends {op:'shutdown'} on mismatch so the next prompt starts a
# fresh daemon (deploys never serve stale logic). The wire field stays named
# lib_hash so an upgraded client vs an old daemon mismatches by design.
#
# Lifecycle: single instance via named mutex; self-shutdown after 2h idle;
# spawned detached by the client on pipe-absent fallback and best-effort by
# the SessionStart launcher (mem0-hook-daemon-spawn.ps1).
#
# Security: pipe ACL = current user only (PipeSecurity, inherited ACEs
# dropped). Log (~/.mem0/hook-daemon.log, 1MB rotate) carries NO payload
# contents — op names, counts, durations, hashes only.
#
# Runs under powershell.exe 5.1 (JavaScriptSerializer + the PipeSecurity
# NamedPipeServerStream constructor are .NET Framework). -DefineOnly lets
# Pester dot-source the functions without starting the listener.

param(
    [string]$PipeName = 'mem0-hook-daemon',
    [int]$IdleTimeoutMinutes = 120,
    [switch]$DefineOnly
)

$ErrorActionPreference = 'SilentlyContinue'

$script:DaemonLogPath = $env:USERPROFILE + '\.mem0\hook-daemon.log'
# v0.22 review L2: set true by the stale-lib short-circuit in Invoke-DaemonRequest
# so the serve loop self-exits after answering one mismatch prompt (deterministic
# rollover, independent of the client's best-effort SendShutdown).
$script:StaleExitRequested = $false

function Write-DaemonLog {
    # 1MB rotation (single .1 backup, same pattern as admission-rejected.jsonl).
    # PRIVACY: callers must never pass prompt/memory text.
    param([string]$Msg)
    try {
        $dir = [System.IO.Path]::GetDirectoryName($script:DaemonLogPath)
        if (-not [System.IO.Directory]::Exists($dir)) { [void][System.IO.Directory]::CreateDirectory($dir) }
        if ([System.IO.File]::Exists($script:DaemonLogPath) -and (([System.IO.FileInfo]::new($script:DaemonLogPath)).Length -gt 1MB)) {
            $arch = $script:DaemonLogPath + '.1'
            if ([System.IO.File]::Exists($arch)) { [System.IO.File]::Delete($arch) }
            [System.IO.File]::Move($script:DaemonLogPath, $arch)
        }
        [System.IO.File]::AppendAllText($script:DaemonLogPath, '[' + [System.DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss') + '] ' + $Msg + [System.Environment]::NewLine)
    } catch {}
}

# Format-MemoryContextBlock / Select-AdmittedMemoryResults look up 'Write-Log'
# for their count-only diagnostics — route those to the daemon log.
function Write-Log { param([string]$Msg) Write-DaemonLog $Msg }

function ConvertTo-DaemonB64 {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    return [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Text))
}

function Limit-RepeatedGoalsOq {
    <#
    v1.12 HK-5: the SAME open goals + questions re-rendered on EVERY substantive
    prompt (1.8-6.2 KB measured per injection; ~100-300 KB repeated context per
    long session) on top of the SessionStart banner that already carried them.
    The daemon is resident, so it can remember what each session already saw:
    when the goals+OQ set is UNCHANGED since the last injection for this session,
    blank those sections (Format-MemoryContextBlock omits empty sections) —
    memories stay per-prompt fresh. Re-inject immediately when the set changes
    (new goal, resolved question) and every 12th substantive prompt as a
    post-compaction/drift guard. Fail-open: any error = old behavior (re-inject).
    #>
    param($Bundle, [string]$SessionId)
    if ($null -eq $Bundle -or [string]::IsNullOrWhiteSpace($SessionId)) { return $Bundle }
    try {
        $gSig = @($Bundle.goals | ForEach-Object { "$($_.id)|$($_.title)|$($_.status)" }) -join ';'
        $qSig = @($Bundle.open_questions | ForEach-Object { "$($_.id)|$($_.question_text)|$($_.status)" }) -join ';'
        $sig  = $gSig + '##' + $qSig
        if ($null -eq $script:GoalsOqSeen) { $script:GoalsOqSeen = @{} }
        $st = $script:GoalsOqSeen[$SessionId]
        if ($null -ne $st -and $st.sig -eq $sig -and $st.n -lt 12) {
            $st.n = $st.n + 1
            $Bundle.goals = @()
            $Bundle.open_questions = @()
        } else {
            $script:GoalsOqSeen[$SessionId] = @{ sig = $sig; n = 1 }
        }
    } catch { }
    return $Bundle
}

function Invoke-DaemonRawBundle {
    <#
    .SYNOPSIS
    v0.20 A.5 iteration 2: serve the WHOLE per-prompt pipeline from verbatim
    hook stdin — parse, fixture sampling, session-id extraction, brand
    inference, triviality gate, rate limit, bundle-or-checkpoint POST,
    [MEMORY CONTEXT] render. MIRRORS user-prompt-extract.ps1 sections 1-4
    exactly (same constants, same file paths, same fail-open semantics); the
    client keeps only stdout emission + Phase 0.B. Always returns
    ok=true/served=true once stdin reached us — per-stage failures degrade
    exactly like the inline path does (e.g. bundle POST failure -> no block,
    0.B still runs client-side). ok=false happens only for daemon-level
    faults, which send the client down the full inline path.
    Response free-text fields are base64 so the client can extract them with
    anchored regexes and zero JSON machinery (see lib block comment).
    -StateDir/-FixtureDir overrides exist for Pester only.
    #>
    param(
        [string]$RawStdin,
        [string]$StateDir   = ($env:USERPROFILE + '\.claude\state'),
        [string]$FixtureDir = ($env:USERPROFILE + '\.claude\state\hook-fixtures')
    )
    $resp = @{ ok = $true; served = $true; lib_hash = $script:LibHash; needs_0b = $false
               sid_b64 = ''; context_b64 = ''; prompt_b64 = ''; tpath_b64 = ''; brand_b64 = ''; diag_b64 = '' }

    # --- mirror §1: stdin parse (parse failure = served-nothing, like WARN+exit 0)
    $hookEvent = $null
    try { $hookEvent = ConvertFrom-HookJson $RawStdin } catch { $hookEvent = $null }
    if (-not $hookEvent) {
        $resp.diag_b64 = ConvertTo-DaemonB64 'stdin_parse_failed'
        return $resp
    }
    $prompt = $hookEvent.prompt
    $transcriptPath = $hookEvent.transcript_path

    # --- mirror §1: fixture sampling — verbatim raw bytes, 1-in-10 guid hash,
    # same filename contract, same keep-20 prune. v0.20 Phase F (L9): the
    # writer is the SAME lib function the inline hook calls — daemon and
    # inline fixture corpora stay byte-identical by construction.
    try {
        [void](Save-HookFixture -FixtureDir $FixtureDir -EventName 'UserPromptSubmit' `
            -ContractVersion $script:HookContractVersion -RawBytes $RawStdin `
            -SampleRoll (([Math]::Abs([guid]::NewGuid().GetHashCode()) % 10) -eq 0))
    } catch {}

    # --- mirror §1: event + empty-prompt gates (exit-0 equivalents)
    if ($hookEvent.hook_event_name -ne 'UserPromptSubmit') { return $resp }
    if (-not $prompt) { return $resp }

    # --- mirror §2: session_id from transcript filename
    $sessionId = $null
    if ($transcriptPath) {
        $basename = [System.IO.Path]::GetFileNameWithoutExtension($transcriptPath)
        if ($basename -match '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') { $sessionId = $basename }
    }
    if (-not $sessionId) {
        $fnwe = [System.IO.Path]::GetFileNameWithoutExtension($transcriptPath)
        if (-not $fnwe) { $fnwe = 'noop' }
        $sessionId = "unknown-$fnwe"
        Write-DaemonLog 'WARN: session_id fallback used (non-UUID transcript filename)'
    }

    # --- mirror §3: brand inference (lib; $null = fail-closed downstream)
    $brand = $null
    if ($transcriptPath) { $brand = Get-InferredBrandFromPath -Path $transcriptPath }
    $workspace = 'ai-ecosystem'
    $project = $null
    # v0.22 Pillar 1 + Pillar 2 (B latency fix): resolve initiative + tier from
    # the per-session sidecar FIRST (written at SessionStart). The sidecar caches
    # both the cwd-derived initiative (so we skip the ~70ms per-prompt git spawn
    # in the common case) and the resolved tier. Sidecar MISS -> fall back to the
    # pre-v0.22 per-prompt computation (Get-SessionInitiative) + tier resolution
    # via the transcript tail. $null initiative = unscoped; default tier frontier.
    $initiative = $null
    $tier = 'frontier'
    $sidecar = $null
    try { $sidecar = Get-SessionSidecar -SessionId $sessionId } catch { $sidecar = $null }
    if ($sidecar) {
        $initiative = $sidecar.initiative
        if (-not [string]::IsNullOrWhiteSpace([string]$sidecar.tier)) { $tier = [string]$sidecar.tier }
    } else {
        # Sidecar miss (session predates SessionStart sidecar, or write failed):
        # pay the git spawn once and resolve the tier from the transcript tail.
        $cwd = $null
        try { $cwd = [string]$hookEvent.cwd } catch { $cwd = $null }
        if ($cwd) { try { $initiative = Get-SessionInitiative -Cwd $cwd } catch { $initiative = $null } }
        try { $tier = Get-SessionTier -SessionId $sessionId -TranscriptPath $transcriptPath } catch { $tier = 'frontier' }
        if ([string]::IsNullOrWhiteSpace($tier)) { $tier = 'frontier' }
    }

    # fields the client needs for 0.B regardless of what happens below
    $resp.sid_b64    = ConvertTo-DaemonB64 $sessionId
    $resp.prompt_b64 = ConvertTo-DaemonB64 ([string]$prompt)
    $resp.tpath_b64  = ConvertTo-DaemonB64 ([string]$transcriptPath)
    $resp.brand_b64  = ConvertTo-DaemonB64 ([string]$brand)
    # v0.21 Phase B (M4): compute the 0.B decision verdict daemon-side under the
    # combined handshake (the daemon dot-sources the deployed lib and a hash
    # mismatch forces inline fallback, so this verdict can never be stale). The
    # client reads needs_0b instead of re-implementing Test-DecisionLikePrompt
    # in C# (the duplicate gate is deleted) — no silent drift.
    $resp.needs_0b   = [bool](Test-DecisionLikePrompt -Prompt ([string]$prompt))

    # Step 1 (2026-06-30): real-time correction-capture. When the prompt looks like
    # the operator correcting the agent, append it to the durable learn-rules queue
    # (~/.mem0/learn-rules.jsonl). Rides this already-firing daemon path -> no new
    # hook, no Windows parallel-hook-spawn race (anthropics/claude-code#37988).
    # STRICTLY fail-open: never affects the bundle or needs_0b.
    try {
        if (Test-CorrectionLikePrompt -Prompt ([string]$prompt)) {
            [void](Add-LearnRuleCapture -Prompt ([string]$prompt) -SessionId $sessionId -TranscriptPath ([string]$transcriptPath) -Brand ([string]$brand) -Initiative ([string]$initiative))
        }
    } catch {}

    # --- mirror §4: triviality gate (same constants as the inline path)
    $isTrivial = $true
    $promptForSearch = $null
    if ($prompt -and $prompt.Length -gt 5) {
        $promptForSearch = if ($prompt.Length -gt 500) { $prompt.Substring(0, 500) } else { $prompt }
        $trivial = @('continue', 'yes', 'no', 'ok', 'okay', 'sure', 'go', 'next', 'stop', '1', '2', '3', 'a', 'b', 'c', 'thanks', 'thx')
        $wordCount = ($promptForSearch -split '\s+').Count
        $isTrivial = $wordCount -lt 3 -or $trivial -contains $promptForSearch.ToLower().Trim()
    }

    # --- mirror §4: per-session rate limit (1s cooldown, fail-open, stale sweep)
    # v0.20 Phase F (L9): same lib functions as the inline path (parity by
    # construction); corrupt-state logging routes through this file's Write-Log
    # shim into the daemon log. Consume-on-fire stays below, unchanged.
    $rateLimited = $false
    $rateLimitState = $null
    try {
        $rlDecision = Get-RateLimitDecision -StateDir $StateDir -SessionId $sessionId `
            -NowFileTimeUtc ([System.DateTime]::Now.ToFileTimeUtc()) -CooldownMs 1000
        $rateLimited = $rlDecision.RateLimited
        $rateLimitState = $rlDecision.StatePath
        Invoke-RateLimitStateSweep -StateDir $StateDir -MaxAgeHours 1
    } catch {
        Write-DaemonLog 'rate-limit check failed; failing open'
        $rateLimited = $false
    }

    $apiKey = Get-Mem0ApiKeyCached
    if (-not $apiKey) {
        # inline equivalent: "could not read API key - skipping" + exit 0
        # (0.B would also be skipped inline, so mark prompt absent too)
        $resp.prompt_b64 = ''
        $resp.diag_b64 = ConvertTo-DaemonB64 'no_api_key'
        return $resp
    }

    $swReq = [System.Diagnostics.Stopwatch]::StartNew()
    if ((-not $isTrivial) -and (-not $rateLimited)) {
        # consume cooldown token only when surfacing actually fires (v0.19 L2)
        try { if ($rateLimitState) { [System.IO.File]::WriteAllText($rateLimitState, [string][System.DateTime]::Now.ToFileTimeUtc()) } } catch {}
        try {
            $bundleBody = ConvertTo-HookJson @{
                session_id            = $sessionId
                prompt                = $promptForSearch
                brand                 = $brand
                workspace             = $workspace
                project               = $project
                initiative            = $initiative   # v0.22 Pillar 1: cwd-derived repo leaf
                tier                  = $tier          # v0.22 Pillar 2: consuming-model tier (server ignores until Phase D)
                transcript_path       = $transcriptPath
                hook_contract_version = $script:HookContractVersion
            }
            $bundleText = Invoke-Mem0Post -Uri ($script:BaseUrl + '/v1/context/bundle') -Body $bundleBody -ApiKey $apiKey -TimeoutMs 3000
            $bundleR = ConvertFrom-HookJson $bundleText
            $bundleR = Limit-RepeatedGoalsOq -Bundle $bundleR -SessionId $sessionId   # v1.12 HK-5
            # v0.22 D: render per tier (resolved above from sidecar/transcript).
            # frontier/mid = full format; small = flat + legend. Fail-open frontier.
            $contextBlock = Format-MemoryContextBlock -Bundle $bundleR -Brand $brand -Tier $tier
            $resp.context_b64 = ConvertTo-DaemonB64 $contextBlock
            $resp.diag_b64 = ConvertTo-DaemonB64 ("episode_id=$($bundleR.checkpoint.episode_id) action=$($bundleR.checkpoint.action) memories=$(@($bundleR.memories).Count) goals=$(@($bundleR.goals).Count) oq=$(@($bundleR.open_questions).Count) daemon_ms=$($swReq.ElapsedMilliseconds)")
        } catch {
            # inline equivalent: "0.A/0.D bundle FAILED" -> no block, 0.B still runs
            $resp.diag_b64 = ConvertTo-DaemonB64 ('bundle_failed: ' + $_.Exception.Message)
        }
    } else {
        try {
            $body = ConvertTo-HookJson @{
                session_id            = $sessionId
                transcript_path       = $transcriptPath
                prompt_text           = $prompt.Substring(0, [Math]::Min(300, $prompt.Length))
                brand                 = $brand
                workspace             = $workspace
                project               = $project
                hook_contract_version = $script:HookContractVersion
            }
            $respText = Invoke-Mem0Post -Uri ($script:BaseUrl + '/v1/episodes/checkpoint') -Body $body -ApiKey $apiKey -TimeoutMs 1000
            $ckR = ConvertFrom-HookJson $respText
            $reason = if ($rateLimited) { 'rate-limited' } else { 'trivial' }
            $resp.diag_b64 = ConvertTo-DaemonB64 ("checkpoint-only ($reason) episode_id=$($ckR.episode_id) action=$($ckR.action) daemon_ms=$($swReq.ElapsedMilliseconds)")
        } catch {
            $resp.diag_b64 = ConvertTo-DaemonB64 ('checkpoint_failed: ' + $_.Exception.Message)
        }
    }
    return $resp
}

function Invoke-DaemonRequest {
    <#
    .SYNOPSIS
    Dispatch one parsed request -> response hashtable. Pure protocol logic on
    top of lib functions (Get-Mem0ApiKeyCached, Invoke-Mem0Post,
    Format-MemoryContextBlock) so Pester can cover it with mocks
    (-DefineOnly + stubbed $script:LibHash/$script:BaseUrl).
    #>
    param($Req)
    $op = $null
    try { $op = [string]$Req.op } catch { $op = $null }
    if ($op -eq 'ping')     { return @{ ok = $true; op = 'ping';     lib_hash = $script:LibHash } }
    if ($op -eq 'shutdown') { return @{ ok = $true; op = 'shutdown'; lib_hash = $script:LibHash } }
    if ($op -eq 'bundle_raw') {
        # v0.21 Phase B (L1): refuse stale service BEFORE any side effect. When
        # the client sends its expected handshake hash and it no longer matches
        # ours, a deploy happened after this daemon started -> return the
        # response envelope immediately (served=true so the client's parser
        # accepts the line, but with the OLD lib_hash so its hash check trips
        # verdict=hash_mismatch -> inline fallback + shutdown) WITHOUT writing a
        # rate-limit token or making any HTTP call. No surfacing token is burned
        # per lib deploy. Absent field -> old-client behavior (full bundle).
        $expected = $null
        try { $expected = [string]$Req.expected_lib_hash } catch { $expected = $null }
        if ($expected -and ($expected -ne $script:LibHash)) {
            # v0.22 review L2: deterministic rollover. The client's best-effort
            # SendShutdown can lose its connect race, leaving this stale daemon
            # alive — every subsequent prompt would then re-detect the mismatch and
            # pay the ~1.1s inline path until a shutdown finally lands. Instead,
            # request our OWN exit right after answering this first stale-detecting
            # prompt (the serve loop sets $running=$false post-write). The mutex
            # frees on exit and the next prompt respawns a fresh daemon, so rollover
            # is bounded to exactly ONE inline prompt regardless of SendShutdown.
            $script:StaleExitRequested = $true
            return @{ ok = $true; served = $true; lib_hash = $script:LibHash; needs_0b = $false
                      sid_b64 = ''; context_b64 = ''; prompt_b64 = ''; tpath_b64 = ''; brand_b64 = ''
                      diag_b64 = (ConvertTo-DaemonB64 'stale_lib') }
        }
        $raw = $null
        try {
            $b64 = [string]$Req.stdin_b64
            if ($b64) { $raw = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) }
        } catch { $raw = $null }
        if (-not $raw) { return @{ ok = $false; error = 'bad_stdin_b64'; lib_hash = $script:LibHash } }
        try { return Invoke-DaemonRawBundle -RawStdin $raw } catch {
            return @{ ok = $false; error = ('raw_dispatch_failed: ' + $_.Exception.Message); lib_hash = $script:LibHash }
        }
    }
    if ($op -ne 'bundle')   { return @{ ok = $false; error = 'unknown_op'; lib_hash = $script:LibHash } }

    $apiKey = Get-Mem0ApiKeyCached
    if (-not $apiKey) { return @{ ok = $false; error = 'no_api_key'; lib_hash = $script:LibHash } }

    $swReq = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        # Same body the inline path sends to POST /v1/context/bundle (A.3).
        # v0.22 D / v1.0 R2: forward the consuming-model tier + initiative the
        # client resolved (parity with op=bundle_raw L232-233 and the inline path)
        # so the server applies the per-tier memory/goal/OQ caps + the tier-scaled
        # relevance_threshold, and the render is tier-aware. A legacy client that
        # omits tier -> 'frontier' (fail-open; == prior behavior, never under-serve).
        $reqTier = if (-not [string]::IsNullOrWhiteSpace([string]$Req.tier)) { [string]$Req.tier } else { 'frontier' }
        $bundleBody = ConvertTo-HookJson @{
            session_id            = $Req.session_id
            prompt                = $Req.prompt
            brand                 = $Req.brand
            workspace             = $Req.workspace
            project               = $Req.project
            initiative            = $Req.initiative
            tier                  = $reqTier
            transcript_path       = $Req.transcript_path
            hook_contract_version = $Req.hook_contract_version
        }
        $bundleText = Invoke-Mem0Post -Uri ($script:BaseUrl + '/v1/context/bundle') -Body $bundleBody -ApiKey $apiKey -TimeoutMs 3000
        $bundleR = ConvertFrom-HookJson $bundleText
        $bundleR = Limit-RepeatedGoalsOq -Bundle $bundleR -SessionId ([string]$Req.session_id)   # v1.12 HK-5

        # Identical rendering to the inline path: lib Format-MemoryContextBlock
        # incl. client-side admission Layers 1/2/3 + the same rejected-candidate
        # audit file defaults. Tier-aware (v1.0 R2), fail-open frontier.
        $contextBlock = Format-MemoryContextBlock -Bundle $bundleR -Brand $Req.brand -Tier $reqTier

        return @{
            ok            = $true
            context_block = $contextBlock
            lib_hash      = $script:LibHash
            diag          = @{
                episode_id = $bundleR.checkpoint.episode_id
                action     = $bundleR.checkpoint.action
                memories   = @($bundleR.memories).Count
                goals      = @($bundleR.goals).Count
                oq         = @($bundleR.open_questions).Count
                ms         = $swReq.ElapsedMilliseconds
            }
        }
    } catch {
        return @{ ok = $false; error = ('bundle_failed: ' + $_.Exception.Message); lib_hash = $script:LibHash }
    }
}

if ($DefineOnly) { return }

# ---------------------------------------------------------------------------
# Startup: single instance, JSON, transport, lib, hash, HTTP warm
# ---------------------------------------------------------------------------

$createdNew = $false
$script:DaemonMutex = [System.Threading.Mutex]::new($true, 'mem0-hook-daemon-singleton', [ref]$createdNew)
if (-not $createdNew) { exit 0 }   # another instance owns the pipe

# JSON via JavaScriptSerializer (PS5.1 / .NET Framework) — shared with lib
# through $script:Jss (Get-HookJsonSerializer reuses it).
[void][System.Reflection.Assembly]::LoadWithPartialName('System.Web.Extensions')
$script:Jss = [Activator]::CreateInstance([System.Web.Script.Serialization.JavaScriptSerializer])
$script:Jss.MaxJsonLength = 16MB

# Same transport tuning as the inline hook (A.3): no Expect:100-continue
# stall, no Nagle coalescing on loopback.
[System.Net.ServicePointManager]::Expect100Continue = $false
[System.Net.ServicePointManager]::UseNagleAlgorithm = $false

$script:BaseUrl = 'http://127.0.0.1:18791'
# Stamped on daemon-side bundle/checkpoint POSTs + fixture filenames. MUST
# match $HookContractVersion in user-prompt-extract.ps1 (deployed together;
# R9 hash-checks both).
$script:HookContractVersion = '20.0'

# Dot-source the DEPLOYED lib (same dir) and record its hash for the
# staleness handshake.
$libPath = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path) + '\user-prompt-lib.ps1'
if (-not [System.IO.File]::Exists($libPath)) {
    Write-DaemonLog "ERROR: user-prompt-lib.ps1 not found at $libPath - daemon cannot serve, exiting (hooks keep working inline)"
    exit 1
}
try { . $libPath } catch {
    Write-DaemonLog "ERROR: lib dot-source failed: $($_.Exception.Message) - exiting (hooks keep working inline)"
    exit 1
}
# v0.21 Phase B (M3/M6): combined digest = SHA256( Sha256Hex(lib) +
# Sha256Hex(this daemon script) ). A daemon-only redeploy changes the daemon
# hash and therefore the digest, forcing the same mismatch->shutdown->fresh-daemon
# rollover a lib edit triggers.
# v0.21 review fix-pass: call the shared Get-HandshakeHash helper instead of
# hand-duplicating the formula here, so daemon startup, the inline PS client
# (user-prompt-extract.ps1), and the Get-HandshakeHash unit test all consume ONE
# canonical definition (the C# exe stays a deliberate cross-language port pinned
# by HookClient.Tests.ps1). Get-HandshakeHash returns $null if EITHER the lib or
# the daemon script is unhashable; the guard below covers that null case.
$script:ScriptDir = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path)
$script:LibHash = Get-HandshakeHash -ScriptDir $script:ScriptDir
if (-not $script:LibHash) {
    Write-DaemonLog 'ERROR: could not compute combined handshake digest - exiting (hooks keep working inline)'
    exit 1
}

# Pay the .NET HTTP first-request init (~180ms) ONCE, here, off any hook's
# critical path; also opens the keep-alive loopback connection.
$swWarm = [System.Diagnostics.Stopwatch]::StartNew()
try {
    $warm = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create($script:BaseUrl + '/health')
    $warm.Method = 'GET'
    $warm.Timeout = 3000
    $warm.Proxy = $null
    $warmResp = $warm.GetResponse()
    $warmResp.Close()
    Write-DaemonLog "HTTP warm OK ($($swWarm.ElapsedMilliseconds)ms)"
} catch {
    Write-DaemonLog "HTTP warm failed ($($_.Exception.Message)) - serving anyway, first bundle pays init"
}

# Pipe ACL: current user only. Drop inherited ACEs, grant FullControl to the
# owning SID alone — no Everyone/AuthenticatedUsers ACE exists on the pipe.
$pipeSecurity = [System.IO.Pipes.PipeSecurity]::new()
$meSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$pipeSecurity.AddAccessRule([System.IO.Pipes.PipeAccessRule]::new($meSid, [System.IO.Pipes.PipeAccessRights]::FullControl, [System.Security.AccessControl.AccessControlType]::Allow))

$idleMs = $IdleTimeoutMinutes * 60000
$reqCount = 0
Write-DaemonLog "daemon start pid=$PID lib_hash=$($script:LibHash.Substring(0,12)).. pipe=$PipeName idle timer armed (${IdleTimeoutMinutes}m)"

# ---------------------------------------------------------------------------
# Serve loop: one request per connection, serial (maxInstances=1). A new
# server stream per iteration keeps state simple; the sub-ms pipe-absent
# window between Dispose and re-create is fail-open by design (client falls
# back inline + respawn attempt no-ops on the mutex).
# ---------------------------------------------------------------------------

$running = $true
while ($running) {
    $server = $null
    try {
        $server = [System.IO.Pipes.NamedPipeServerStream]::new(
            $PipeName,
            [System.IO.Pipes.PipeDirection]::InOut,
            1,
            [System.IO.Pipes.PipeTransmissionMode]::Byte,
            [System.IO.Pipes.PipeOptions]::Asynchronous,
            32768, 65536, $pipeSecurity)

        $waitTask = $server.WaitForConnectionAsync()
        if (-not $waitTask.Wait($idleMs)) {
            Write-DaemonLog "idle ${IdleTimeoutMinutes}m with no requests - self-shutdown (served $reqCount total)"
            break
        }

        $swServe = [System.Diagnostics.Stopwatch]::StartNew()
        $line = Read-PipeLineWithDeadline -Stream $server -TimeoutMs 2000
        $req = $null
        $respObj = $null
        if ($line) {
            try { $req = $script:Jss.DeserializeObject($line) } catch { $req = $null }
            if ($req) {
                try { $respObj = Invoke-DaemonRequest -Req $req } catch {
                    $respObj = @{ ok = $false; error = ('dispatch_failed: ' + $_.Exception.Message); lib_hash = $script:LibHash }
                }
                if (([string]$req.op) -eq 'shutdown') { $running = $false }
            } else {
                $respObj = @{ ok = $false; error = 'bad_request_json'; lib_hash = $script:LibHash }
            }
        } else {
            $respObj = @{ ok = $false; error = 'request_read_timeout'; lib_hash = $script:LibHash }
        }

        $reqCount++
        try {
            $outBytes = [System.Text.Encoding]::UTF8.GetBytes($script:Jss.Serialize($respObj) + "`n")
            $server.Write($outBytes, 0, $outBytes.Length)
            $server.Flush()
            $server.WaitForPipeDrain()
        } catch {}
        try { $server.Disconnect() } catch {}
        $opName = if ($req) { [string]$req.op } else { '?' }
        Write-DaemonLog "req #$reqCount op=$opName ok=$($respObj.ok) ms=$($swServe.ElapsedMilliseconds)"
        # v0.22 review L2: a stale-lib short-circuit (deploy after this daemon
        # started) requested self-exit. Stop now, AFTER answering this prompt, so
        # the mutex frees and the next prompt respawns a fresh daemon — rollover
        # is bounded to one inline prompt and does not depend on the client's
        # best-effort SendShutdown reaching us.
        if ($script:StaleExitRequested) {
            Write-DaemonLog 'stale lib detected -> self-shutdown after serving the mismatch prompt (fresh daemon next prompt)'
            $running = $false
        }
    } catch {
        Write-DaemonLog "serve loop error: $($_.Exception.Message)"
        [System.Threading.Thread]::Sleep(200)   # don't spin on persistent faults
    } finally {
        if ($server) { try { $server.Dispose() } catch {} }
    }
}

Write-DaemonLog "daemon stop pid=$PID served=$reqCount"
exit 0
