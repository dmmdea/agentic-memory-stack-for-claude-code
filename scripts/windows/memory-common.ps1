# memory-common.ps1 - shared helpers for L1a extractor + C1 consolidator
# Windows-native PowerShell. Talks to mem0 over loopback (WSL mirrored networking).
# Auth uses claude.cmd (Windows OAuth, works because Windows-native invocation).

# Per-machine values resolve at RUNTIME, never baked into the deployed text (2026-07-14 audit).
# WHY: the deployed copies live in ~/.claude, which is a git repo SHARED with the other machine.
# A distro substituted in at install time therefore gets committed by whichever box installed last
# and breaks the other one. Runtime resolution keeps the deployed bytes machine-independent.
#   MEM0_URL        - endpoint override when the brain lives on another node (replica -> brain).
#   MEM0_WSL_DISTRO - distro holding ~/.mem0. Falls back to the per-machine install receipt,
#                     because a User-scope env var is INVISIBLE to hook children of a host process
#                     that started before the var was set -- that is exactly why the L1a extractor
#                     silently failed on one box (it resolved 'Ubuntu' and never found the API key).
$script:Mem0Url = if ($env:MEM0_URL) { $env:MEM0_URL } else { 'http://127.0.0.1:18791' }
$script:Mem0WslDistro = if ($env:MEM0_WSL_DISTRO) { $env:MEM0_WSL_DISTRO } else {
    $rcptDistro = $null
    try {
        $rcpt = Join-Path $PSScriptRoot 'mem0-stack.config.psd1'
        if (Test-Path $rcpt) { $rcptDistro = (Import-PowerShellDataFile $rcpt).Distro }
    } catch { $rcptDistro = $null }
    if ($rcptDistro) { $rcptDistro } else { 'Ubuntu' }   # last resort only; the installer always writes the receipt
}
$script:Mem0KeyPath = "\\wsl.localhost\$($script:Mem0WslDistro)\home\__WSL_USER__\.mem0\api-key"
$script:LogDir = Join-Path $env:USERPROFILE '.claude\logs'
$script:StateDir = Join-Path $env:USERPROFILE '.claude\state'

# v0.19 L13: the dead Save-HookFixture function (zero callers) and the stale
# $script:HOOK_CONTRACT_VERSION = 'v0.17' constant were removed — their 1-in-100
# sampling and 'v0.17' version format contradicted the live convention (each hook
# script defines its own $HookContractVersion, e.g. '17.0', samples 1-in-10, and
# writes raw stdin verbatim with the version in the fixture FILENAME).

# Codex CLI (OpenAI ChatGPT subscription OAuth — separate auth from Claude Max,
# so no concurrent-session conflict with the interactive Claude Code session).
# Headless via `codex exec`. Default model gpt-5.5 (current Codex CLI 0.137+).
$script:CodexCmd = Join-Path $env:USERPROFILE 'AppData\Roaming\npm\codex.cmd'
$script:CodexEffortExtractor = 'low'      # I1: structured extraction, low effort is enough
$script:CodexEffortConsolidator = 'medium'  # C1: synthesis, medium for better insight quality

function Initialize-MemoryEnv {
    foreach ($d in @($script:LogDir, $script:StateDir)) {
        if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }
}

function Test-Mem0CacheAclOwnerOnly {
    # v0.22 review L7: is the cache file's DACL owner-only-protected (inheritance
    # off, exactly one ACE for the current user)? Used on the fresh-read path so a
    # pre-v0.21 cache with inherited ACEs is never served verbatim. Returns $false
    # on any error (fail-closed -> the caller refreshes, re-securing the file).
    param([string]$Path)
    try {
        if ([System.IO.File].GetMethod('GetAccessControl', [type[]]@([string]))) {
            $fs = [System.IO.File]::GetAccessControl($Path)
        } else {
            $fs = [System.IO.FileSystemAclExtensions]::GetAccessControl((New-Object System.IO.FileInfo($Path)))
        }
        if (-not $fs.AreAccessRulesProtected) { return $false }
        $me    = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $rules = @($fs.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
        return ($rules.Count -eq 1) -and ($rules[0].IdentityReference.Value -eq $me.Value)
    } catch { return $false }
}

function Test-IsShipLog {
    # Pure keep/route classifier. $true => volatile ship-log (route to episodic).
    # Over-KEEP is the HARD constraint: a value-bearing durable fact must never route.
    # The distinguisher for LONG records is SHIP-SIGNAL (status verb + date/multi-clause),
    # not length alone -- long crowding ship-logs carry that signal; long value facts do not.
    # NOTE (invariant): a long record with ship-signal routes EVEN IF it carries a value
    # marker. This is safe only because the pipeline extracts short atomic value facts
    # SEPARATELY (the Codex inferability gate) before the full narrative reaches this
    # classifier -- the atomic value is emitted as its own short fact (rule 1 KEEPs it);
    # the narrative routes to the episode. Task 2 (write-path) preserves this: $evergreen
    # atomics go to mem0, $shipLog narratives fold into the episode.
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $false }
    $t = $Text.Trim()
    $statusVerb   = '\b(shipped|deployed|done|committed|completed|verified|fixed|started|added|updated|migrated|landed|merged|refactored|wired|removed|renamed)\b'
    $dateAnchor   = '\b20\d{2}-\d{2}-\d{2}\b'
    $atomicMarker = '\b(reserved|token|endpoint|credential|secret|password|version|port|path|hash|key|id|anchor|url)\b|https?://|:\d{2,5}\b|\w+\s*=\s*\S|\bset to\b|[A-Za-z]:\\|\.(ps1|py|js|ts|json|md|sh|exe|dll|yaml|yml|toml|cfg|conf)\b'
    $multiClause  = (($t -split '\r?\n').Count -gt 1) -or (([regex]::Matches($t, ',')).Count -gt 3)
    $shipSignal   = ($t -imatch $statusVerb) -and (($t -match $dateAnchor) -or $multiClause)
    # 1) short value facts -> absolute KEEP (over-KEEP, the hard constraint)
    if ($t.Length -lt 150 -and ($t -imatch $atomicMarker)) { return $false }
    # 2) clear status events (status verb + date/multi-clause) -> route, any length
    if ($shipSignal) { return $true }
    # 3) long records with NO value marker -> crowders -> route
    if ($t.Length -ge 150 -and -not ($t -imatch $atomicMarker)) { return $true }
    # 4) default -> KEEP (long value statement without ship-signal; short non-marker non-event)
    return $false
}

function Split-FactsByShipLog {
    # Partition extracted facts: evergreen -> durable mem0; ship-logs -> episodic.
    param([string[]]$Facts)
    $evergreen = [System.Collections.Generic.List[string]]::new()
    $shipLogs  = [System.Collections.Generic.List[string]]::new()
    foreach ($f in $Facts) {
        if ([string]::IsNullOrWhiteSpace($f)) { continue }
        if (Test-IsShipLog -Text $f) { $shipLogs.Add($f) } else { $evergreen.Add($f) }
    }
    return [pscustomobject]@{ Evergreen = @($evergreen); ShipLogs = @($shipLogs) }
}

function Split-OversizeFact {
    # MEM-10 (2026-07-03): write-time oversize guard for the L1a extractor.
    # The REAL fix is upstream — the extraction prompt now demands atomic
    # single-topic facts (<=30 words preferred, 60 HARD max) — but Codex still
    # occasionally emits a multi-topic dump. Anything over ~700 chars would sail
    # through the server's 4000-char cap yet trip the l10-audit OVERSIZE line
    # (1200) and retrieve poorly (one embedding for many topics). Split at
    # sentence boundaries with greedy repacking; a single monster sentence is
    # hard-wrapped so no emitted chunk can exceed MaxChars. PS 5.1-safe (the
    # Stop hook chain runs under Windows PowerShell — no ?? / ?: here).
    param(
        [string]$Fact,
        [int]$MaxChars = 700
    )
    if ([string]::IsNullOrWhiteSpace($Fact) -or $Fact.Length -le $MaxChars) {
        return @($Fact)
    }
    $chunks = [System.Collections.Generic.List[string]]::new()
    $cur = ''
    foreach ($s in [regex]::Split($Fact.Trim(), '(?<=[.!?])\s+')) {
        if ([string]::IsNullOrWhiteSpace($s)) { continue }
        if ($cur -and (($cur.Length + 1 + $s.Length) -gt $MaxChars)) {
            $chunks.Add($cur)
            $cur = $s
        } elseif ($cur) {
            $cur = "$cur $s"
        } else {
            $cur = $s
        }
        # a single sentence longer than MaxChars: hard-wrap (never emit over-cap)
        while ($cur.Length -gt $MaxChars) {
            $chunks.Add($cur.Substring(0, $MaxChars))
            $cur = $cur.Substring($MaxChars)
        }
    }
    if ($cur) { $chunks.Add($cur) }
    # Plain enumeration return (NOT the `,@()` single-object trick): callers
    # consume via foreach / @(...) collection, and the comma-wrapper would make
    # @(Split-OversizeFact ...) double-box the array into one element.
    return @($chunks)
}

function Get-Mem0Key {
    # v0.20 A.2: same local-cache mechanism as Get-Mem0ApiKeyCached in
    # user-prompt-lib.ps1 (fresh <1h cache authoritative; miss/stale -> UNC read
    # + refresh; UNC down -> stale cache fallback). Avoids the ~90ms
    # \\wsl.localhost network-filesystem read per invocation.
    # v0.21 L2: caching FAILS CLOSED — the owner-only protected ACL is applied to
    # an empty file BEFORE the secret is written; on ACL failure the file is
    # deleted and caching is SKIPPED (key still returned). The stale-cache
    # fallback is bounded to MaxStaleFallbackHours so a rotated-away key is not
    # served indefinitely.
    $MaxStaleFallbackHours = 24
    $cachePath = Join-Path $env:USERPROFILE '.mem0\api-key.cache'
    $cached = $null
    try {
        if (Test-Path -LiteralPath $cachePath) {
            $item   = Get-Item -LiteralPath $cachePath
            $cached = ([System.IO.File]::ReadAllText($cachePath)).Trim()
            if ($cached -and (((Get-Date) - $item.LastWriteTime).TotalMinutes -lt 60)) {
                # v0.22 review L7: only trust a fresh cache whose ACL is owner-only-
                # protected; a pre-v0.21 file with inherited ACEs falls through to
                # refresh (which rewrites ACL-first + atomic).
                if (Test-Mem0CacheAclOwnerOnly -Path $cachePath) {
                    return $cached
                }
            }
        }
    } catch { $cached = $null }
    $key = $null
    try {
        if (Test-Path $script:Mem0KeyPath) {
            $key = (Get-Content $script:Mem0KeyPath -Raw -ErrorAction Stop).Trim()
        }
    } catch { $key = $null }
    if (-not $key) {
        if ($cached) {
            try {
                $staleAge = (Get-Date) - [System.IO.File]::GetLastWriteTime($cachePath)
                if ($staleAge.TotalHours -lt $MaxStaleFallbackHours) { return $cached }
            } catch {}
        }
        throw "mem0 API key not found at $($script:Mem0KeyPath) - is WSL running?"
    }
    # Refresh the cache, FAIL CLOSED + ATOMIC. v0.21 review fix-pass: the whole
    # empty-file -> owner-only ACL -> secret-write trio runs on a per-process temp
    # path, then atomically renames into place — a concurrent same-user spawn can
    # no longer share the create/delete window on the live cache and the secret
    # can never land on a freshly-created inode with inherited ACLs. On ANY failure
    # delete the TEMP (never the live cache) and skip caching (key still returned).
    $tmp = $null
    try {
        $dir = Split-Path -Parent $cachePath
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $tmp = $cachePath + '.' + [System.Guid]::NewGuid().ToString('N') + '.tmp'
        [System.IO.File]::WriteAllText($tmp, '')
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true, $false)
        $me   = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')
        $acl.AddAccessRule($rule)
        # [System.IO.File]::SetAccessControl exists on .NET Framework (PS5.1)
        # but NOT on .NET Core (PS7); fall back to the Core ACL extension so the
        # owner-only ACL is applied (not skipped) on both runtimes.
        if ([System.IO.File].GetMethod('SetAccessControl', [type[]]@([string], [System.Security.AccessControl.FileSecurity]))) {
            [System.IO.File]::SetAccessControl($tmp, $acl)
        } else {
            [System.IO.FileSystemAclExtensions]::SetAccessControl((New-Object System.IO.FileInfo($tmp)), $acl)
        }
        [System.IO.File]::WriteAllText($tmp, $key)
        # Atomic publish: secret-bearing, tight-ACL temp replaces the cache.
        if ([System.IO.File]::Exists($cachePath)) {
            # Replace = single atomic NTFS swap; [NullString]::Value for the
            # no-backup arg ($null coerces to '' and throws on .NET Core).
            if ([System.IO.File].GetMethod('Replace', [type[]]@([string], [string], [string]))) {
                [System.IO.File]::Replace($tmp, $cachePath, [NullString]::Value)
            } else {
                [System.IO.File]::Delete($cachePath)
                [System.IO.File]::Move($tmp, $cachePath)
            }
        } else {
            [System.IO.File]::Move($tmp, $cachePath)
        }
        $tmp = $null
        # v0.22 review L7: File.Replace PRESERVES the destination's DACL, so a
        # pre-existing weak-ACL cache would keep its weak ACL after the swap.
        # Re-apply the owner-only protected ACL to the live cache (idempotent).
        # Build a FRESH FileSecurity — a consumed one's modified flags are cleared.
        $acl2 = New-Object System.Security.AccessControl.FileSecurity
        $acl2.SetAccessRuleProtection($true, $false)
        $acl2.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')))
        if ([System.IO.File].GetMethod('SetAccessControl', [type[]]@([string], [System.Security.AccessControl.FileSecurity]))) {
            [System.IO.File]::SetAccessControl($cachePath, $acl2)
        } else {
            [System.IO.FileSystemAclExtensions]::SetAccessControl((New-Object System.IO.FileInfo($cachePath)), $acl2)
        }
    } catch {
        if ($tmp) { try { [System.IO.File]::Delete($tmp) } catch {} }
    }
    return $key
}

function Test-Mem0Health {
    try {
        $r = Invoke-RestMethod -Uri "$($script:Mem0Url)/health" -TimeoutSec 5
        return ($r.ok -eq $true)
    } catch { return $false }
}

function Get-Mem0Evidence {
    param([int]$Limit = 100)
    $key = Get-Mem0Key
    Invoke-RestMethod -Uri "$($script:Mem0Url)/v1/memories?user_id=__WSL_USER__&limit=$Limit" `
        -Headers @{'X-API-Key' = $key} `
        -TimeoutSec 30
}

function Add-Mem0Memory {
    param(
        [string]$Text,
        [string]$Source,
        [hashtable]$Metadata = @{}
    )
    $key = Get-Mem0Key
    $Metadata['source'] = $Source
    if (-not $Metadata.ContainsKey('tier')) { $Metadata['tier'] = 'evidence' }
    $body = @{
        messages = $Text
        user_id = '__WSL_USER__'
        infer = $false
        metadata = $Metadata
    } | ConvertTo-Json -Depth 5 -Compress
    try {
        # v1.12 F1 (719 silent failures 06-10..07-03): PS 5.1 Invoke-RestMethod encodes a
        # STRING -Body as Latin-1, so any non-ASCII byte in a fact (Spanish text, em-dashes)
        # reached FastAPI as invalid UTF-8 -> 400 -> the fact dead-lettered forever (the DLQ
        # drain re-posts through THIS helper, so retries 400'd identically). Send BYTES.
        # This is the centralized POST for L1a facts, C1 insights and the DLQ drain - one
        # fix covers every caller. (ASCII hyphen on purpose: no-BOM + PS 5.1 reads this
        # file as ANSI, and an em-dash's 0x94 byte is a smart quote to the tokenizer.)
        $r = Invoke-RestMethod -Uri "$($script:Mem0Url)/v1/memories" `
            -Method Post `
            -Headers @{'X-API-Key' = $key; 'Content-Type' = 'application/json'} `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) `
            -TimeoutSec 15
        # Return the new memory id (or $true if mem0 didn't return one) so callers can
        # record source-IDs (audit finding 2026-06-08: C1 insights had no lineage).
        $newId = $null
        if ($r -and $r.results -and $r.results.Count -gt 0) { $newId = $r.results[0].id }
        if ($newId) { return $newId } else { return $true }
    } catch {
        # Dead-letter the failed write so it can be retried later (audit finding
        # 2026-06-08: per-fact POST failures were silently dropped, undermining the
        # whole point of the hook).
        # v0.14 C: preserve original metadata + status_code + initialize attempts=1
        $dlq = Join-Path $script:StateDir 'mem0-post-failures.jsonl'
        $statusCode = 0
        try {
            if ($_.Exception.Response) { $statusCode = [int]$_.Exception.Response.StatusCode }
        } catch {}
        $rec = @{
            text     = $Text
            source   = $Source
            metadata = $Metadata    # preserve original; restored on drain
            attempts = 1
            error    = $_.Exception.Message
            status_code = $statusCode
            timestamp   = (Get-Date).ToString('o')
        } | ConvertTo-Json -Depth 5 -Compress
        try { Add-Content -LiteralPath $dlq -Value $rec -Encoding UTF8 } catch {}
        return $false
    }
}

function Drain-Mem0DeadLetter {
    # Attempt to re-POST anything in the DLQ. Called at the start of each L1a / C1 run
    # so transient mem0/Qdrant/Ollama failures self-heal without manual intervention.
    #
    # v0.14 C hardening:
    #   - Restores original metadata on retry (was re-POSTing with empty @{})
    #   - Increments attempts on each retry attempt
    #   - Quarantines on deterministic failure codes: 413 (too large), 401 (auth), 422 (validation)
    #   - Quarantines after attempts >= 5 (max-attempts guard; prevents infinite retry loops)
    #   - Logs quarantine events to l1a.log at WARN severity

    $MAX_ATTEMPTS = 5
    # Status codes that will never succeed on retry — quarantine immediately
    $POISON_CODES = @(413, 401, 422)

    $dlq       = Join-Path $script:StateDir 'mem0-post-failures.jsonl'
    $quarantine = Join-Path $script:StateDir 'mem0-post-poison.jsonl'
    if (-not (Test-Path -LiteralPath $dlq)) { return @{ drained = 0; remaining = 0 } }
    $lines = @(Get-Content -LiteralPath $dlq -ErrorAction SilentlyContinue)
    if ($lines.Count -eq 0) { return @{ drained = 0; remaining = 0 } }

    $still    = @()
    $drained  = 0
    $poisoned = 0
    $dropped  = 0

    foreach ($line in $lines) {
        try {
            $rec = $line | ConvertFrom-Json
        } catch {
            # Malformed JSON — quarantine with parse-error reason
            $qrec = @{ raw_line = $line; quarantine_reason = 'parse-error'; timestamp = (Get-Date).ToString('o') } | ConvertTo-Json -Compress
            try { Add-Content -LiteralPath $quarantine -Value $qrec -Encoding UTF8 } catch {}
            Write-MemoryLog -Component 'l1a' -Message "WARN: DLQ record quarantined (parse-error): $($line.Substring(0, [Math]::Min(120, $line.Length)))"
            $poisoned++
            continue
        }

        # Determine attempt count (may be missing on records written before v0.14 C)
        $attempts = if ($rec.PSObject.Properties.Name -contains 'attempts') { [int]$rec.attempts } else { 1 }
        $statusCode = if ($rec.PSObject.Properties.Name -contains 'status_code') { [int]$rec.status_code } else { 0 }

        # Quarantine deterministic failures immediately
        $isPoisonCode = $POISON_CODES -contains $statusCode
        # 2026-07-15 offline-first: a connection-level failure (status_code 0 = brain unreachable)
        # must NOT count toward the quarantine cap, or a multi-day offline stretch of Stop hooks
        # would quarantine perfectly good writes. Only server-response failures accrue attempts.
        $isMaxAttempts = ($statusCode -ne 0) -and ($attempts -ge $MAX_ATTEMPTS)

        if ($isPoisonCode -or $isMaxAttempts) {
            $reason = if ($isPoisonCode) {
                switch ($statusCode) {
                    413 { '413-deterministic' }
                    401 { '401-auth' }
                    422 { '422-validation' }
                    default { "$statusCode-deterministic" }
                }
            } else { 'max-attempts' }
            # Build quarantine record from the current DLQ record structure
            $qrec = @{
                text            = if ($rec.PSObject.Properties.Name -contains 'text') { $rec.text } else { $rec.payload.text }
                source          = if ($rec.PSObject.Properties.Name -contains 'source') { $rec.source } else { $rec.payload.source }
                metadata        = if ($rec.PSObject.Properties.Name -contains 'metadata') { $rec.metadata } else { $rec.payload.metadata }
                attempts        = $attempts
                error           = $rec.error
                status_code     = $statusCode
                timestamp       = $rec.timestamp
                quarantine_reason = $reason
            } | ConvertTo-Json -Depth 5 -Compress
            try { Add-Content -LiteralPath $quarantine -Value $qrec -Encoding UTF8 } catch {}
            Write-MemoryLog -Component 'l1a' -Message "WARN: DLQ record quarantined ($reason) after $attempts attempt(s): $($rec.error)"
            $poisoned++
            continue
        }

        # Reconstruct metadata hashtable from the record (PSCustomObject -> hashtable)
        $metaHt = @{}
        $metaSrc = if ($rec.PSObject.Properties.Name -contains 'metadata') { $rec.metadata } `
                   elseif ($rec.PSObject.Properties.Name -contains 'payload') { $rec.payload.metadata } `
                   else { $null }
        if ($metaSrc) {
            $metaSrc.PSObject.Properties | ForEach-Object { $metaHt[$_.Name] = $_.Value }
        }
        $textVal   = if ($rec.PSObject.Properties.Name -contains 'text') { $rec.text } else { $rec.payload.text }
        $sourceVal = if ($rec.PSObject.Properties.Name -contains 'source') { $rec.source } else { $rec.payload.source }

        # Phase-3 policy: ship-log entries must NEVER be replayed into durable mem0,
        # even via the DLQ fallback. Drop silently (not quarantine — this is by policy,
        # not because the record is malformed or undeliverable).
        if (Test-IsShipLog -Text $textVal) {
            Write-MemoryLog -Component 'l1a' -Message "DLQ: dropped ship-log entry per Phase-3 policy: $($textVal.Substring(0, [Math]::Min(80, $textVal.Length)))"
            $dropped++
            continue
        }

        $r = $false
        try {
            $r = Add-Mem0Memory -Text $textVal -Source $sourceVal -Metadata $metaHt
        } catch {}

        if ($r) {
            $drained++
        } else {
            # Re-queue with incremented attempts; preserve all fields.
            # Connection-level failures (status_code 0) do not accrue attempts — see gate above.
            $updatedAttempts = if ($statusCode -eq 0) { $attempts } else { $attempts + 1 }
            $retryRec = @{
                text        = $textVal
                source      = $sourceVal
                metadata    = $metaHt
                attempts    = $updatedAttempts
                error       = $rec.error
                status_code = $statusCode
                timestamp   = $rec.timestamp
            } | ConvertTo-Json -Depth 5 -Compress
            $still += $retryRec
        }
    }

    if ($still.Count -eq 0) {
        Remove-Item -LiteralPath $dlq -ErrorAction SilentlyContinue
    } else {
        $still | Set-Content -LiteralPath $dlq -Encoding UTF8
    }
    return @{ drained = $drained; remaining = $still.Count; quarantined = $poisoned; dropped = $dropped }
}

function Invoke-CodexSubagent {
    param(
        [Parameter(Mandatory)][string]$Prompt,
        [string]$ReasoningEffort = 'low',  # low | medium | high
        [int]$TimeoutSeconds = 120
    )
    if (-not (Test-Path $script:CodexCmd)) {
        throw "codex.cmd not found at $($script:CodexCmd)"
    }
    # Codex CLI authenticates against OpenAI via ChatGPT subscription OAuth
    # (auth_mode=chatgpt at ~/.codex/auth.json) - unlike Claude Max, this works
    # headlessly from subprocess contexts without concurrent-session conflict with
    # the interactive Claude Code. --skip-git-repo-check: don't require a git repo.
    # -c model_reasoning_effort: lower the reasoning budget (default xhigh is pricey).
    $effortArg = 'model_reasoning_effort=' + '"' + $ReasoningEffort + '"'

    # v0.27 R5: ENFORCE -TimeoutSeconds. The prior version declared the parameter but
    # NEVER applied it — a hung codex.cmd/node blocked the caller forever (the L1a
    # Stop-hook extractor and the dream consolidator call this DIRECTLY, with no outer
    # guard; only the eval path had a Python-side timeout). We run the SAME
    # `$prompt | & $CodexCmd exec ...` invocation inside a child powershell.exe we own:
    # codex path + effort go via ENV VARS (zero arg-quoting risk vs cmd.exe), the
    # prompt via stdin (exactly as before). We bound it with Process.WaitForExit(ms)
    # (PS 5.1-safe — Start-Process -Timeout is PS7-only) and on timeout kill the WHOLE
    # tree (powershell -> codex.cmd -> node) with `taskkill /T /F` so nothing orphans
    # (mirrors the Python harness's process-tree kill).
    $childScript = @'
$ErrorActionPreference = 'Continue'
$inp = [Console]::In.ReadToEnd()
$o = $inp | & $env:MEM0_CODEX_CMD exec --skip-git-repo-check -c $env:MEM0_CODEX_EFFORT_ARG - 2>&1
[Console]::Out.Write([string]::Join([char]10, @($o | ForEach-Object { [string]$_ })))
exit $LASTEXITCODE
'@
    $enc = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($childScript))

    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'powershell.exe'
    $psi.Arguments = '-NoProfile -NonInteractive -EncodedCommand ' + $enc
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables['MEM0_CODEX_CMD'] = $script:CodexCmd
    $psi.EnvironmentVariables['MEM0_CODEX_EFFORT_ARG'] = $effortArg

    $p = [System.Diagnostics.Process]::Start($psi)
    try {
        $p.StandardInput.Write($Prompt)
        $p.StandardInput.Close()
        # Read async BEFORE WaitForExit so a large reply can't deadlock the pipe.
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
            try { & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null } catch {}
            try { $p.Kill() } catch {}
            # Drain the async readers post-kill (the killed process closed its pipes)
            # so they don't dangle as unobserved tasks; bounded so cleanup can't hang.
            try { [void]$outTask.Wait(500); [void]$errTask.Wait(500) } catch {}
            throw "Invoke-CodexSubagent timed out after ${TimeoutSeconds}s"
        }
        $output = $outTask.Result
        $errText = $errTask.Result
        if ($p.ExitCode -ne 0) {
            $detail = if ($output) { $output } else { $errText }
            throw "codex exited $($p.ExitCode) : $detail"
        }
        return $output
    } finally {
        try { $p.Dispose() } catch {}
    }
}

function Parse-CodexTokenUsage {
    # Codex CLI outputs "tokens used\nN" near the end. Parse it for cost accounting.
    # Returns int or $null.
    param([Parameter(Mandatory)][string]$RawOutput)
    $lines = $RawOutput -split "`r?`n"
    for ($i = $lines.Length - 1; $i -ge 0; $i--) {
        if ($lines[$i].Trim() -eq 'tokens used' -and $i + 1 -lt $lines.Length) {
            $n = $lines[$i + 1].Trim() -replace ',', ''
            if ($n -match '^\d+$') { return [int]$n }
        }
    }
    return $null
}

function Write-CodexUsageLog {
    # Persist per-call usage to ~/.claude/logs/codex-usage.jsonl for budget visibility
    # (audit finding 2026-06-08: no token/call accounting was persisted).
    param(
        [Parameter(Mandatory)][string]$Component,  # 'l1a' | 'c1'
        [int]$TokensUsed = 0,
        [int]$DurationMs = 0,
        [string]$Status = 'ok',
        [int]$FactsPosted = 0
    )
    $usageLog = Join-Path $script:LogDir 'codex-usage.jsonl'
    $rec = @{
        ts = (Get-Date).ToString('o')
        component = $Component
        tokens_used = $TokensUsed
        duration_ms = $DurationMs
        status = $Status
        items_posted = $FactsPosted
    } | ConvertTo-Json -Compress
    try { Add-Content -LiteralPath $usageLog -Value $rec -Encoding UTF8 } catch {}
}

function Invoke-LogRotation {
    # Rotate any log file in ~/.claude/logs/ exceeding $MaxBytes. Keeps last $KeepN archives.
    # (Audit finding 2026-06-08: l1a.log/c1.log/audit-flags.jsonl had no rotation.)
    param([int]$MaxBytes = 1MB, [int]$KeepN = 5)
    if (-not (Test-Path -LiteralPath $script:LogDir)) { return }
    foreach ($f in (Get-ChildItem -LiteralPath $script:LogDir -File -ErrorAction SilentlyContinue)) {
        if ($f.Length -gt $MaxBytes) {
            $ts = Get-Date -Format 'yyyyMMdd-HHmmss'
            $archived = "$($f.FullName).$ts"
            try {
                Move-Item -LiteralPath $f.FullName -Destination $archived -Force -ErrorAction Stop
            } catch { continue }
            # Trim old archives
            $base = $f.BaseName
            $existing = Get-ChildItem -LiteralPath $script:LogDir -File -Filter "$($f.Name).*" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
            if ($existing.Count -gt $KeepN) {
                $existing | Select-Object -Skip $KeepN | ForEach-Object {
                    try { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop } catch {}
                }
            }
        }
    }
}

function Get-CodexResponseText {
    # Codex CLI output format (v0.137+):
    #   <metadata header lines>
    #   --------
    #   user
    #   <our prompt echoed>
    #
    #   codex
    #   <model response>
    #   tokens used
    #   <token count>
    # Extract just the model response (between the last 'codex' marker and 'tokens used').
    param([Parameter(Mandatory)][string]$RawOutput)
    $lines = $RawOutput -split "`r?`n"
    $startIdx = -1
    $endIdx = $lines.Length
    for ($i = $lines.Length - 1; $i -ge 0; $i--) {
        if ($lines[$i].Trim() -eq 'tokens used') { $endIdx = $i }
        if ($lines[$i].Trim() -eq 'codex') { $startIdx = $i + 1; break }
    }
    if ($startIdx -lt 0) { return $RawOutput }
    return (($lines[$startIdx..($endIdx - 1)] -join "`n").Trim())
}

function Extract-JsonFromText {
    param([string]$Text, [string]$ExpectedKey)
    # Find first balanced {...} block containing the expected key
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    # Strip markdown code fences if present
    $cleaned = $Text -replace '(?s)```(?:json)?\s*', '' -replace '```\s*', ''
    # Try to parse the whole thing first
    try {
        $obj = $cleaned | ConvertFrom-Json
        if ($obj.PSObject.Properties.Name -contains $ExpectedKey) { return $obj }
    } catch { }
    # Fallback: regex for the JSON object containing the expected key
    $pattern = '\{[^{}]*"' + [regex]::Escape($ExpectedKey) + '"\s*:\s*\[[^\]]*\][^{}]*\}'
    $m = [regex]::Match($cleaned, $pattern, [System.Text.RegularExpressions.RegexOptions]::Singleline)
    if ($m.Success) {
        try { return ($m.Value | ConvertFrom-Json) } catch { return $null }
    }
    return $null
}

function Write-MemoryLog {
    param(
        [Parameter(Mandatory)][string]$Component,  # 'l1a' or 'c1'
        [Parameter(Mandatory)][string]$Message
    )
    $logFile = Join-Path $script:LogDir "$Component.log"
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Add-Content -Path $logFile -Value "[$ts] $Message" -Encoding UTF8
}

function Test-Throttle {
    # Check whether enough time has elapsed since last successful run.
    # Pure check - does NOT write the state file (audit finding 2026-06-08: the old
    # combined Test-ThrottleAndMark wrote BEFORE doing any work, so a transient
    # failure burned the throttle window and silenced the next 10 minutes).
    # Caller must explicitly Mark-Throttle on success.
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][int]$MinIntervalSeconds
    )
    $stateFile = Join-Path $script:StateDir "last-$Name"
    $now = [int][double]::Parse((Get-Date -UFormat %s))
    if (Test-Path $stateFile) {
        try {
            $last = [int](Get-Content $stateFile -Raw).Trim()
            if ($now - $last -lt $MinIntervalSeconds) { return $false }
        } catch { }
    }
    return $true
}

function Mark-Throttle {
    # Stamp the success time so the throttle window opens for the next call.
    param([Parameter(Mandatory)][string]$Name)
    $stateFile = Join-Path $script:StateDir "last-$Name"
    $now = [int][double]::Parse((Get-Date -UFormat %s))
    Set-Content -Path $stateFile -Value $now -Encoding UTF8 -NoNewline
}

function Test-ThrottleAndMark {
    # DEPRECATED - kept for backward-compat with any out-of-tree callers.
    # Prefer Test-Throttle + Mark-Throttle so partial failures don't burn the window.
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][int]$MinIntervalSeconds)
    if (-not (Test-Throttle -Name $Name -MinIntervalSeconds $MinIntervalSeconds)) { return $false }
    Mark-Throttle -Name $Name
    return $true
}

function Acquire-CodexLock {
    # Atomic create-new with PID liveness check (v0.13.1 hardening).
    # Returns $true if lock acquired (caller MUST call Release-CodexLock after), $false
    # if another LIVE process holds it. Stale locks (holder PID gone OR mtime > MaxAgeMinutes)
    # are reclaimed.
    param(
        [Parameter(Mandatory)][string]$Owner,    # 'l1a' | 'c1' | 'dream'
        [int]$MaxAgeMinutes = 30
    )
    $lockFile = Join-Path $env:USERPROFILE '.claude\state\codex.lock'
    $lockDir = Split-Path -Parent $lockFile
    if (-not (Test-Path $lockDir)) { New-Item -ItemType Directory -Path $lockDir -Force | Out-Null }
    $contents = "$Owner $((Get-Date).ToString('o')) pid=$PID"
    try {
        # Atomic CreateNew - fails if file exists. No TOCTOU window.
        $fs = [System.IO.File]::Open($lockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($contents)
            $fs.Write($bytes, 0, $bytes.Length)
        } finally {
            $fs.Close()
        }
        return $true
    } catch [System.IO.IOException] {
        # Lock exists - validate the holder is still alive
        $existing = $null
        try { $existing = Get-Content -LiteralPath $lockFile -Raw -ErrorAction Stop } catch { return $false }
        $holderPid = $null
        if ($existing -match 'pid=(\d+)') { $holderPid = [int]$Matches[1] }
        $stale = $false
        if ($holderPid) {
            $proc = Get-Process -Id $holderPid -ErrorAction SilentlyContinue
            if (-not $proc) { $stale = $true }  # PID gone
        }
        if (-not $stale) {
            try {
                $age = (Get-Date) - (Get-Item -LiteralPath $lockFile).LastWriteTime
                if ($age.TotalMinutes -ge $MaxAgeMinutes) { $stale = $true }
            } catch { return $false }
        }
        if (-not $stale) { return $false }
        # Stale - reclaim and retry once (single recursion bound)
        try { Remove-Item -LiteralPath $lockFile -Force -ErrorAction Stop } catch { return $false }
        # Retry once; if it fails again we return false (don't loop)
        try {
            $fs = [System.IO.File]::Open($lockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try {
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($contents)
                $fs.Write($bytes, 0, $bytes.Length)
            } finally {
                $fs.Close()
            }
            return $true
        } catch { return $false }
    } catch {
        return $false
    }
}

function Release-CodexLock {
    $lockFile = Join-Path $env:USERPROFILE '.claude\state\codex.lock'
    if (-not (Test-Path -LiteralPath $lockFile)) { return }
    try {
        $contents = Get-Content -LiteralPath $lockFile -Raw -ErrorAction Stop
        if ($contents -match 'pid=(\d+)') {
            if ([int]$Matches[1] -ne $PID) {
                # Lock is held by a different process (we were already reclaimed). Don't delete.
                return
            }
        }
        Remove-Item -LiteralPath $lockFile -ErrorAction SilentlyContinue
    } catch { return }
}

function Get-BrandFromTranscriptPath {
    # Infer brand/workspace/project from a Claude Code transcript path.
    # Claude Code stores transcripts under a directory whose name encodes the project path,
    # e.g. "d--My-Drive-AI-Ecosystem" or "D--repos-myapp-platform".
    # Returns a hashtable with keys: brand, workspace, project.
    param([string]$Path)
    $segs = $Path -split '[\\/]'
    # Find the segment that looks like a project-dir encoding (contains '--')
    $proj = $segs | Where-Object { $_ -match '^[a-zA-Z]--' } | Select-Object -First 1
    if (-not $proj) { return @{ brand = $null; workspace = $null; project = $null } }
    $lower = $proj.ToLower()
    # v1.0 Phase 7B: operator-agnostic brand routing — rules from the deployed
    # brands.json (beside this lib), neutral default fallback. No private brand
    # names hardcoded in source; operators add their own in brands.json.
    $brand = $null
    $brandCfg = Join-Path $PSScriptRoot 'brands.json'
    $brandRules = $null
    try { if (Test-Path -LiteralPath $brandCfg) { $brandRules = (Get-Content -LiteralPath $brandCfg -Raw | ConvertFrom-Json).rules } } catch {}
    if (-not $brandRules) { $brandRules = @([pscustomobject]@{ pattern = 'ai-ecosystem|agentic-memory|mem0'; brand = 'ai-ecosystem' }) }
    foreach ($r in $brandRules) { if ($r.pattern -and ($lower -match $r.pattern)) { $brand = $r.brand; break } }
    return @{
        brand     = $brand
        workspace = $proj
        project   = $proj
    }
}

function Redact-Secrets {
    # Strip credential-shaped substrings from session text BEFORE it is sent to the extraction
    # LLM (Codex, external) or POSTed to mem0 (a local queryable store) — pasted keys/tokens must
    # not flow into either. Mirrors SkillOpt harvest.redact_secrets (one shared pattern set across
    # the ecosystem's two session readers). Safe prose is untouched. Replacement strings are SINGLE-
    # quoted so $1/$2 reach the .NET regex engine as backreferences, not PowerShell variables;
    # -replace is case-insensitive by default, so no (?i) is needed.
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return $Text }
    $rules = @(
        @('sk-[A-Za-z0-9_-]{10,}', '[REDACTED_OPENAI_KEY]'),
        @('(Authorization:\s*Bearer\s+)[^\s"'']+', '$1[REDACTED]'),
        @('(Authorization:\s*Basic\s+)[^\s"'']+', '$1[REDACTED]'),
        @('\b(api[_-]?key|token|password|secret)\b(\s*[:=]\s*)[^\s"'']+', '$1$2[REDACTED]'),
        # Only the [:=] assignment shape. A bare 'token <word>' / 'password <word>' rule over-
        # redacts prose ("password reset email") and `\s+` would span the `\n\n` turn break and eat
        # the next [role] tag in the joined transcript. Do not add it.
        @('(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', '[REDACTED_PRIVATE_KEY]')
    )
    foreach ($r in $rules) { $Text = $Text -replace $r[0], $r[1] }
    return $Text
}

function Get-RecentTranscriptTurns {
    param(
        [Parameter(Mandatory)][string]$TranscriptPath,
        [int]$MaxTurns = 24,
        [int]$MaxChars = 12000
    )
    if (-not (Test-Path $TranscriptPath)) { return $null }
    # Pathological-transcript guard (v0.23). A corrupted/huge single-line transcript
    # (observed in the wild — a 24.6 MB single line) pegged a CPU core for ~11 HOURS in
    # the L1a Stop-hook extractor, contributing to system-wide slowdowns. TWO O(n^2)
    # traps compound on a giant line: (1) Get-Content -Tail scans BACKWARD for line
    # endings, quadratic on long lines; (2) PowerShell 5.1's ConvertFrom-Json is
    # quadratic on large input. So we (a) read only the last $tailBytes via a bounded
    # stream — O(tailBytes), never the whole file — and (b) skip any record past a sane
    # size ceiling before ConvertFrom-Json ever sees it.
    $tailBytes      = 524288    # 512 KB — comfortably holds $MaxTurns real records
    $maxRecordChars = 262144    # 256 KB per record
    $raw = $null
    $seeked = $false
    try {
        $fs = [System.IO.File]::Open($TranscriptPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            if ($fs.Length -gt $tailBytes) { [void]$fs.Seek(-$tailBytes, [System.IO.SeekOrigin]::End); $seeked = $true }
            $count = [int][Math]::Min([long]$tailBytes, $fs.Length)
            $buf = New-Object byte[] $count
            $read = $fs.Read($buf, 0, $count)
            $raw = [System.Text.Encoding]::UTF8.GetString($buf, 0, $read)
        } finally { $fs.Dispose() }
    } catch { return $null }
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    $records = $raw -split "`n"
    # When we seeked, the first fragment is a partial (mid-line) record — drop it.
    if ($seeked -and $records.Count -gt 1) { $records = $records[1..($records.Count - 1)] }
    $turns = @()
    foreach ($line in ($records | Select-Object -Last $MaxTurns)) {
        $line = $line.TrimEnd("`r")
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line.Length -gt $maxRecordChars) { continue }   # oversized/corrupt — never feed to ConvertFrom-Json
        try {
            $obj = $line | ConvertFrom-Json
            $role = $obj.message.role
            $content = $obj.message.content
            if (-not $role -or -not $content) { continue }
            $text = if ($content -is [string]) {
                $content
            } else {
                ($content | Where-Object { $_.type -eq 'text' } | ForEach-Object { $_.text }) -join "`n"
            }
            if ($text) { $turns += "[$role] $text" }
        } catch { }
    }
    if ($turns.Count -eq 0) { return $null }
    # Redact secrets on the FULL joined text (before truncation) so credentials never reach the
    # extractor LLM or mem0 — this is the single chokepoint every L1a/episodic consumer reads from.
    $joined = Redact-Secrets ($turns -join "`n`n")
    if ($joined.Length -gt $MaxChars) { $joined = $joined.Substring($joined.Length - $MaxChars) }
    return $joined
}

# ---------------------------------------------------------------------------
# L1a transcript cursor (2026-07-14): the extractor ran Get-RecentTranscriptTurns
# (last 24 turns) on EVERY Stop, so consecutive extractions of a growing transcript
# re-processed the OVERLAPPING window and re-emitted the same facts. The server-side
# hash-idempotency guard now no-ops the resulting duplicate WRITES, so this is purely a
# token-saving optimisation on the extractor side: only feed the turns appended SINCE the
# last SUCCESSFUL extraction. The cursor is a byte offset into the append-only JSONL
# transcript, stored per-transcript (keyed by the session-UUID filename) under state/.
# It advances ONLY when an extraction completes (Mark-Throttle points), so a failed codex
# call / POST re-processes the same window next time. ANY uncertainty (no cursor, shrink/
# rotation, empty since-window, or an error) falls back to Get-RecentTranscriptTurns — the
# unchanged behaviour — so this can never LOSE an extraction, only skip redundant ones.
function Get-L1aCursorPath {
    param([Parameter(Mandatory)][string]$TranscriptPath)
    $name = [System.IO.Path]::GetFileNameWithoutExtension($TranscriptPath)
    if ([string]::IsNullOrWhiteSpace($name)) { $name = 'unknown' }
    # sanitize to a safe filename component
    $safe = ($name -replace '[^0-9A-Za-z_\-]', '_')
    if ($safe.Length -gt 80) { $safe = $safe.Substring(0, 80) }
    return (Join-Path $script:StateDir ("l1a-cursor-$safe.txt"))
}

function Get-L1aCursor {
    param([Parameter(Mandatory)][string]$TranscriptPath)
    try {
        $p = Get-L1aCursorPath -TranscriptPath $TranscriptPath
        if (Test-Path $p) {
            $v = (Get-Content -LiteralPath $p -Raw -ErrorAction Stop).Trim()
            $n = 0L
            if ([long]::TryParse($v, [ref]$n) -and $n -ge 0) { return $n }
        }
    } catch { }
    return 0L
}

function Set-L1aCursor {
    param(
        [Parameter(Mandatory)][string]$TranscriptPath,
        [Parameter(Mandatory)][long]$Bytes
    )
    try {
        if (-not (Test-Path $script:StateDir)) { New-Item -ItemType Directory -Path $script:StateDir -Force | Out-Null }
        $p = Get-L1aCursorPath -TranscriptPath $TranscriptPath
        Set-Content -LiteralPath $p -Value ([string]$Bytes) -NoNewline -Encoding ASCII
    } catch { }   # best-effort: a cursor-write failure just means the next run re-extracts (safe)
}

function Get-TranscriptTurnsSince {
    # Like Get-RecentTranscriptTurns, but returns ONLY the turns appended after $SinceBytes.
    # Returns $null when nothing new (or on any read/parse trouble) so the caller falls back.
    param(
        [Parameter(Mandatory)][string]$TranscriptPath,
        [Parameter(Mandatory)][long]$SinceBytes,
        [int]$MaxTurns = 24,
        [int]$MaxChars = 12000
    )
    if (-not (Test-Path $TranscriptPath)) { return $null }
    $tailBytes      = 524288    # same 512 KB bound as Get-RecentTranscriptTurns (pathological-line guard)
    $maxRecordChars = 262144
    $raw = $null
    $droppedPartial = $false
    try {
        $fs = [System.IO.File]::Open($TranscriptPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $len = $fs.Length
            if ($SinceBytes -ge $len) { return $null }   # nothing appended since the cursor
            $start = $SinceBytes
            if (($len - $start) -gt $tailBytes) { $start = $len - $tailBytes; $droppedPartial = $true }  # huge gap: bound + drop first partial
            [void]$fs.Seek($start, [System.IO.SeekOrigin]::Begin)
            $count = [int]($len - $start)
            $buf = New-Object byte[] $count
            $read = $fs.Read($buf, 0, $count)
            $raw = [System.Text.Encoding]::UTF8.GetString($buf, 0, $read)
        } finally { $fs.Dispose() }
    } catch { return $null }
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    $records = $raw -split "`n"
    # Only when we re-bounded past the cursor is the first fragment a partial mid-line record.
    # At a clean cursor boundary (a prior file length) the first record is whole — keep it.
    if ($droppedPartial -and $records.Count -gt 1) { $records = $records[1..($records.Count - 1)] }
    $turns = @()
    foreach ($line in ($records | Select-Object -Last $MaxTurns)) {
        $line = $line.TrimEnd("`r")
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line.Length -gt $maxRecordChars) { continue }
        try {
            $obj = $line | ConvertFrom-Json
            $role = $obj.message.role
            $content = $obj.message.content
            if (-not $role -or -not $content) { continue }
            $text = if ($content -is [string]) {
                $content
            } else {
                ($content | Where-Object { $_.type -eq 'text' } | ForEach-Object { $_.text }) -join "`n"
            }
            if ($text) { $turns += "[$role] $text" }
        } catch { }
    }
    if ($turns.Count -eq 0) { return $null }
    $joined = Redact-Secrets ($turns -join "`n`n")
    if ($joined.Length -gt $MaxChars) { $joined = $joined.Substring($joined.Length - $MaxChars) }
    return $joined
}
