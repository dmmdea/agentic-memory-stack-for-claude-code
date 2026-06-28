# pre-tool-check.ps1 - v0.17 Phase 0.F PreToolUse contradiction-check (logging-only)
#
# Fires before Claude runs Edit / Write / Bash / MultiEdit. Searches mem0 for
# tier=canonical hits that match the tool args. Writes warnings to
# ~/.mem0/pre-tool-warnings.jsonl WITHOUT enforcing (no blocking - DX risk too
# high until false-positive rate is measured after 1 week of logging).
#
# v0.20 Phase A.2 — latency fast path. Measured v0.19 baseline was ~1.0-1.3s
# per tool call because EVERY relevant tool input paid: UNC api-key read
# (~90ms over \\wsl.localhost), JSON parse (~120ms PS5.1 assembly load) and an
# HTTP search round-trip (~250-550ms). The fix: the FIRST work after reading
# stdin is the 0.F pattern gate (Test-CanonicalAssertionCandidate — ports /
# IPv4s / decision vocabulary, i.e. what tier=canonical assertions are made
# of). The gate runs on the RAW stdin text BEFORE the JSON parse (raw JSON
# escaping cannot hide a port/IP/keyword, so there are no false negatives;
# a false positive merely takes the old slow path). Non-matching input exits
# in ~spawn time with zero UNC/HTTP/dir I/O. On match the input is re-gated on
# the extracted tool query for precision, and the api-key comes from
# Get-Mem0ApiKeyCached (user-prompt-lib.ps1, local 1h cache) instead of the
# per-spawn UNC read.
#
# Fixture sampling (F.3.3 contract corpus) stays BEFORE the gate by design:
# it is a local-only write (%USERPROFILE%\.claude\state), fires 1-in-10, and
# costs <1ms on the 9-in-10 skip — the corpus must keep sampling non-matching
# payload shapes too, since it exists to detect wire-format drift.
#
# Claude Code PreToolUse hook contract:
#   stdin JSON: { hook_event_name, tool_name, tool_input, transcript_path, session_id }
#
# Performance budget: non-matching (typical) <=300ms; matching <=1.5s. Search
# timeout: 2s. Fail open on any error. PS5.1 compatible: no ?? operator.
#
# Pester: scripts/windows/tests/PreToolCheck.Tests.ps1 dot-sources this file
# with $env:PRETOOL_TEST_MODE='1' (defines functions, does not execute).

$ErrorActionPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# 0. Constants + lazy logging (no directory I/O until a log line is needed)
# ---------------------------------------------------------------------------

# v0.20 A.2 perf note: the fast path must execute ZERO cmdlets — the first
# cmdlet from a PS5.1 module pays its module load (measured: Utility ~75ms via
# Get-Random/ConvertFrom-Json, Management ~45ms via Join-Path/Add-Content).
# Top-level setup therefore uses .NET statics only; cmdlets are fine on the
# (rare) matching slow path and inside Write-Log.
$script:LogDir   = $env:USERPROFILE + '\.claude\logs'
$script:LogFile  = $script:LogDir + '\pre-tool-check.log'
$script:BaseUrl  = 'http://127.0.0.1:18791'
$script:WarnFile = '\\wsl.localhost\__WSL_DISTRO__\home\__WSL_USER__\.mem0\pre-tool-warnings.jsonl'
$script:ApiKeyUncPath = '\\wsl.localhost\__WSL_DISTRO__\home\__WSL_USER__\.mem0\api-key'
# Directory of THIS script (deployed: C:\Users\__WIN_USER__\.claude\scripts) — the lib
# ships alongside both hooks.
$script:ScriptDir = [System.IO.Path]::GetDirectoryName($PSCommandPath)

# v0.19 M15: hook contract version stamped on the 0.F search POST. '17.0' is
# intentional — the search wire contract is unchanged by the v0.20 A.2 reorder;
# bump only IF the contract changes.
$script:HookContractVersion = '17.0'

function Write-Log {
    param([string]$Msg)
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    try {
        if (-not (Test-Path -LiteralPath $script:LogDir)) { New-Item -ItemType Directory -Force $script:LogDir | Out-Null }
        Add-Content -Path $script:LogFile -Value "[$ts] $Msg" -Encoding UTF8
    } catch {}
}

# ---------------------------------------------------------------------------
# 1. The 0.F pattern gate — what does a canonical assertion look like?
# tier=canonical memories in this stack are port/service bindings, node IPs,
# and locked decisions (retired/forbidden/never/always/policy vocabulary).
# A tool input that contains none of those cannot contradict one.
# ---------------------------------------------------------------------------

function Test-CanonicalAssertionCandidate {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $false }
    # Ports + listeners: ":18791", "port 8081", "--port", "listen", "bind", "localhost"
    if ($Text -match '(?i):\d{2,5}\b|\bports?\b|--port\b|\blisten\w*\b|\bbind\w*\b|\blocalhost\b') { return $true }
    # IPv4 literals ("<internal-ip>", "127.0.0.1")
    if ($Text -match '\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b') { return $true }
    # Decision / canonical-assertion vocabulary
    if ($Text -match '(?i)\b(canonical|tier|mem0|qdrant|decision|decided|locked|retired|forbidden|deprecated|reserved|never|always|policy)\b') { return $true }
    return $false
}

# ---------------------------------------------------------------------------
# 2. Fixture sampling (v0.17 F.3.3 / v0.19 L13 byte-faithful corpus) —
# local-only write, 1-in-10, amortized <1ms.
# ---------------------------------------------------------------------------

function Save-PreToolFixture {
    param([string]$StdinRaw)
    try {
        # 1-in-10 sample WITHOUT Get-Random (first Get-Random costs ~75ms of
        # Microsoft.PowerShell.Utility module load on the fast path).
        if (([Math]::Abs([guid]::NewGuid().GetHashCode()) % 10) -ne 0) { return }
        # v0.20 Phase F (L9): the byte-faithful write + keep-20 prune live in
        # user-prompt-lib.ps1 (Save-HookFixture, shared with the UserPromptSubmit
        # hook + daemon). The lib is dot-sourced HERE, inside the 1-in-10 roll,
        # so the 9-in-10 fast path pays nothing extra.
        if (-not (Test-Path 'Function:\Save-HookFixture')) {
            $libPath = Join-Path $script:ScriptDir 'user-prompt-lib.ps1'
            if (Test-Path $libPath) { try { . $libPath } catch {} }
        }
        if (Test-Path 'Function:\Save-HookFixture') {
            [void](Save-HookFixture -FixtureDir (Join-Path $env:USERPROFILE '.claude\state\hook-fixtures') `
                -EventName 'PreToolUse' -ContractVersion $script:HookContractVersion `
                -RawBytes $StdinRaw -SampleRoll $true)
        }
    } catch {}
}

# ---------------------------------------------------------------------------
# 3. The pipeline (testable: Pester mocks Invoke-RestMethod and calls this)
# ---------------------------------------------------------------------------

function Invoke-PreToolCheck {
    param([string]$StdinRaw)

    if (-not $StdinRaw) { return }

    # Fixture sampling first (local-only; must keep covering non-matching shapes)
    Save-PreToolFixture -StdinRaw $StdinRaw

    # === v0.20 A.2 FAST PATH: raw-text pattern gate BEFORE any parse/UNC/HTTP ===
    if (-not (Test-CanonicalAssertionCandidate -Text $StdinRaw)) { return }

    # --- Slow path: parse stdin JSON ---
    $toolName  = $null
    $toolInput = $null
    $sessionId = $null
    try {
        $hookEvent = $StdinRaw | ConvertFrom-Json -ErrorAction Stop
        $toolName  = $hookEvent.tool_name
        $toolInput = $hookEvent.tool_input
        $sessionId = $hookEvent.session_id
    } catch {
        Write-Log "WARN: stdin parse failed: $($_.Exception.Message)"
        return
    }

    # Skip if not a relevant tool
    $relevantTools = @('Bash', 'Edit', 'MultiEdit', 'Write')
    if ($toolName -notin $relevantTools) { return }

    # Extract query string from tool_input
    $query = $null
    if ($toolName -eq 'Bash') {
        $cmd = $toolInput.command
        if ($cmd) {
            $query = if ($cmd.Length -gt 500) { $cmd.Substring(0, 500) } else { $cmd }
        }
    } elseif ($toolName -eq 'Edit' -or $toolName -eq 'MultiEdit') {
        $fp  = $toolInput.file_path
        $old = $toolInput.old_string
        if ($old -and $old.Length -gt 200) { $old = $old.Substring(0, 200) }
        $query = "$fp"
        if ($old) { $query = "$fp $old" }
    } elseif ($toolName -eq 'Write') {
        $fp      = $toolInput.file_path
        $content = $toolInput.content
        if ($content -and $content.Length -gt 200) { $content = $content.Substring(0, 200) }
        $query = "$fp"
        if ($content) { $query = "$fp $content" }
    }

    if (-not $query -or $query.Trim().Length -lt 5) { return }

    # Precision re-gate on the EXTRACTED tool query: the raw-text gate may have
    # matched on payload fields outside the tool input (cwd, transcript_path).
    if (-not (Test-CanonicalAssertionCandidate -Text $query)) { return }

    # --- API key: local cache (v0.20 A.2), UNC fallback if the lib is missing ---
    # NOTE: existence checks use Test-Path Function:\ — `Get-Command <missing>`
    # triggers full PSModulePath auto-discovery (~1.7s measured), Test-Path
    # on the Function: provider does not.
    if (-not (Test-Path 'Function:\Get-Mem0ApiKeyCached')) {
        $libPath = Join-Path $script:ScriptDir 'user-prompt-lib.ps1'
        if (Test-Path $libPath) { try { . $libPath } catch {} }
    }
    $apiKey = $null
    if (Test-Path 'Function:\Get-Mem0ApiKeyCached') {
        $apiKey = Get-Mem0ApiKeyCached
    } else {
        try { $apiKey = (Get-Content -LiteralPath $script:ApiKeyUncPath -Raw -ErrorAction Stop).Trim() } catch {}
    }
    if (-not $apiKey) { return }   # no API key - never block tools

    # --- Search mem0 for tier=canonical hits (timeout 2s) ---
    $hits        = @()
    $hitTexts    = @()
    $hitScores   = @()
    $searchQuery = $query.Trim()

    try {
        # Search with user_id filter (server-side); post-filter by tier=canonical in PS.
        # v0.18 fix-pass HIGH: query_class='canonical' is REQUIRED — the server-side
        # admission gate (Phase C) strips tier=canonical from default-class results,
        # which made the PS post-filter below dead code (0 hits ever).
        $searchBody = @{
            query                 = $searchQuery
            filters               = @{ user_id = '__WSL_USER__' }
            limit                 = 5
            threshold             = 0.55
            rerank                = $false
            query_class           = 'canonical'
            hook_contract_version = $script:HookContractVersion   # v0.19 M15
        } | ConvertTo-Json -Depth 4 -Compress

        $resp = Invoke-RestMethod `
            -Uri "$($script:BaseUrl)/v1/memories/search" `
            -Method Post `
            -Body $searchBody `
            -ContentType 'application/json' `
            -Headers @{ 'X-API-Key' = $apiKey } `
            -TimeoutSec 2 `
            -ErrorAction Stop

        $allHits = @($resp.results)

        # Post-filter: keep only tier=canonical
        foreach ($h in $allHits) {
            $tier = $null
            if ($h.metadata -and $h.metadata.tier) { $tier = $h.metadata.tier }
            if ($tier -eq 'canonical') {
                $hits += $h
            }
        }
    } catch {
        Write-Log "0.F search failed ($toolName): $($_.Exception.Message)"
        return
    }

    # --- If hits found, append warning to pre-tool-warnings.jsonl (no enforcement) ---
    if ($hits.Count -gt 0) {
        foreach ($h in $hits) {
            $text  = $h.memory
            $score = $h.score
            if (-not $text)  { $text = '' }
            if (-not $score) { $score = 0 }
            if ($text.Length -gt 200) { $text = $text.Substring(0, 200) + '...' }
            $hitTexts  += $text
            $hitScores += [Math]::Round([double]$score, 3)
        }

        # Build tool_args_preview
        $previewMax  = 200
        $argsPreview = $searchQuery
        if ($argsPreview.Length -gt $previewMax) { $argsPreview = $argsPreview.Substring(0, $previewMax) + '...' }

        # Compose JSONL line using string concat (PS5.1 safe; no em-dash)
        $ts = (Get-Date -Format 'o')
        $sidSafe = if ($sessionId) { $sessionId } else { 'unknown' }

        # Manually build JSON to stay PS5.1 compatible and avoid nested depth issues
        $textsJson  = ($hitTexts  | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }) -join ','
        $scoresJson = ($hitScores | ForEach-Object { $_.ToString([System.Globalization.CultureInfo]::InvariantCulture) }) -join ','

        $warnLine = "{""ts"":""$ts"",""session_id"":""$sidSafe"",""tool"":""$toolName"",""tool_args_preview"":""$($argsPreview -replace '"', '\"')"",""matched_canonical"":[$textsJson],""match_scores"":[$scoresJson]}"

        try {
            Add-Content -Path $script:WarnFile -Value $warnLine -Encoding UTF8 -ErrorAction Stop
            Write-Log "0.F WARNING: $toolName matched $($hits.Count) canonical hit(s) - logged (no block). Preview: $($argsPreview.Substring(0,[Math]::Min(80,$argsPreview.Length)))"
        } catch {
            Write-Log "0.F: could not write pre-tool-warnings.jsonl: $($_.Exception.Message)"
        }
    }
}

# ---------------------------------------------------------------------------
# 4. Entry point — ALWAYS exit 0, never block tool execution.
# PRETOOL_TEST_MODE=1 (Pester) dot-sources function definitions only.
# ---------------------------------------------------------------------------

if ($env:PRETOOL_TEST_MODE -ne '1') {
    $stdinRaw = $null
    try { $stdinRaw = [Console]::In.ReadToEnd() } catch {}
    Invoke-PreToolCheck -StdinRaw $stdinRaw
    exit 0
}
