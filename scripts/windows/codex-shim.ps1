# codex-shim.ps1 — v0.27.1 R5 KEYSTONE: Windows-resident Codex HTTP shim.
#
# WHY THIS EXISTS (load-bearing):
#   The remaining R5 governance items run WSL-side python (the mem0 server add()
#   write-gate + scripts/wsl/contradiction-sweep.py) but ALL LLM judgment must use
#   Codex (gpt-5.5 via ChatGPT OAuth), which is Windows-only. Invoking Codex by
#   spawning powershell.exe FROM WSL is UNRELIABLE: codex runs, but its stdout is
#   mangled across the WSL->Windows process boundary (a RemoteException stderr
#   artifact; Get-CodexResponseText returns empty). Verified clean ONLY when run
#   Windows-direct.
#
#   This shim runs ENTIRELY Windows-side (where codex output is clean) and exposes
#   Invoke-CodexSubagent over loopback HTTP. WSL POSTs to it via mirrored
#   networking (`http://localhost:<port>`), so only the final clean JSON crosses
#   the boundary as an HTTP response body — TCP bytes, never a spawned-process
#   stdout. VERIFIED: WSL `curl http://localhost:<port>/health` returns clean JSON.
#
# NETWORKING NOTE (verified 2026-06-15): HTTP.sys routes by Host header, so the
#   listener binds BOTH `http://localhost:<port>/` AND `http://127.0.0.1:<port>/`
#   prefixes (a request with Host: 127.0.0.1 against a localhost-only prefix gets
#   HTTP 400 "Invalid Hostname"). Both loopback prefixes bind NON-ELEVATED; only
#   the `+` wildcard needs admin (which we deliberately do not use — loopback-only
#   is the security posture).
#
# AUTH: requires the mem0 API key (X-API-Key header) — same key/trust-domain as
#   the mem0 server (only the mem0 server + the sweep, which already hold the key,
#   call this). Exposing Codex on a listening port without auth would let any local
#   process spend the ChatGPT subscription / run arbitrary prompts.
#
# CONCURRENCY: single-threaded GetContext loop (one request at a time) PLUS the
#   shared ~/.claude/state/codex.lock (Acquire/Release-CodexLock) so the shim never
#   runs codex concurrently with the L1a extractor or the C1/dream consolidator.
#
# Usage:
#   codex-shim.ps1                 # run the daemon (loopback HTTP, idle self-shutdown)
#   codex-shim.ps1 -DefineOnly     # dot-source for tests: load functions, don't listen
#
# PS 5.1 (production hook runtime) AND pwsh 7 (CI) compatible.

[CmdletBinding()]
param(
    [switch]$DefineOnly,
    [int]$Port = 0,                       # 0 => resolve from env MEM0_CODEX_SHIM_PORT or default 18792
    [int]$IdleTimeoutMinutes = 240        # self-shutdown after this long with no requests (free the port)
)

$ErrorActionPreference = 'Stop'

# Dot-source the shared lib (Invoke-CodexSubagent, Get-CodexResponseText,
# Parse-CodexTokenUsage, Acquire/Release-CodexLock, Get-Mem0Key, Initialize-MemoryEnv,
# Invoke-LogRotation, $script:CodexCmd, $script:LogDir, $script:StateDir).
. (Join-Path $PSScriptRoot 'memory-common.ps1')

$script:ShimVersion       = '0.27.1'
$script:ShimMaxBodyBytes  = 65536        # reject prompts larger than 64 KiB (413)
$script:ShimDefaultPort   = 18792        # loopback HTTP shim port (override via env if it collides)
$script:ShimMinTimeoutSec = 10
$script:ShimMaxTimeoutSec = 180

function Resolve-ShimPort {
    param([int]$Override = 0)
    if ($Override -gt 0) { return $Override }
    if ($env:MEM0_CODEX_SHIM_PORT) {
        $p = 0
        if ([int]::TryParse($env:MEM0_CODEX_SHIM_PORT, [ref]$p) -and $p -gt 0 -and $p -lt 65536) { return $p }
    }
    return $script:ShimDefaultPort
}

function Write-ShimLog {
    # NEVER logs prompt/response content — only route, status, timing, error_type.
    param([Parameter(Mandatory)][string]$Message)
    try {
        if (-not (Test-Path -LiteralPath $script:LogDir)) {
            New-Item -ItemType Directory -Path $script:LogDir -Force | Out-Null
        }
        $logFile = Join-Path $script:LogDir 'codex-shim.log'
        $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
        Add-Content -LiteralPath $logFile -Value "[$ts] $Message" -Encoding UTF8
    } catch {}
}

function Test-ConstantTimeEqual {
    # Length-independent constant-time string compare (avoids a timing side-channel
    # on the API key). Mirrors the server's hmac.compare_digest intent.
    param([string]$A, [string]$B)
    if ($null -eq $A) { $A = '' }
    if ($null -eq $B) { $B = '' }
    $ba = [System.Text.Encoding]::UTF8.GetBytes($A)
    $bb = [System.Text.Encoding]::UTF8.GetBytes($B)
    $diff = $ba.Length -bxor $bb.Length
    $n = [Math]::Max($ba.Length, $bb.Length)
    for ($i = 0; $i -lt $n; $i++) {
        $x = if ($i -lt $ba.Length) { $ba[$i] } else { 0 }
        $y = if ($i -lt $bb.Length) { $bb[$i] } else { 0 }
        $diff = $diff -bor ($x -bxor $y)
    }
    return ($diff -eq 0)
}

function ConvertTo-ShimResponse {
    param([int]$Status, [hashtable]$Obj)
    return @{
        status      = $Status
        contentType = 'application/json'
        body        = ($Obj | ConvertTo-Json -Depth 6 -Compress)
    }
}

function New-RealCodexInvoker {
    # Production codex invoker: runs Invoke-CodexSubagent (timeout-enforced),
    # extracts the clean response + token usage. Returns @{response; tokens}; throws
    # on timeout ('*timed out*') or non-zero exit ('*exited*') exactly as the lib does.
    return {
        param([string]$Prompt, [string]$Effort, [int]$TimeoutSec)
        $raw   = Invoke-CodexSubagent -Prompt $Prompt -ReasoningEffort $Effort -TimeoutSeconds $TimeoutSec
        $resp  = Get-CodexResponseText -RawOutput $raw
        $tok   = Parse-CodexTokenUsage -RawOutput $raw
        return @{ response = $resp; tokens = $tok }
    }
}

function Invoke-CodexShimRequest {
    # TESTABLE CORE — pure request->response. All side effects (codex, key read,
    # codex lock) are injected so this is unit-testable without a listener, a real
    # codex, the UNC key path, or the live lock file.
    #
    # Returns @{ status=[int]; contentType=[string]; body=[string] (JSON) }.
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [hashtable]$Headers = @{},          # keys MUST be lowercased by the caller
        [string]$Body = '',
        [scriptblock]$CodexInvoker = $null, # { param($prompt,$effort,$timeoutSec) -> @{response;tokens} | throw }
        [scriptblock]$KeyProvider  = $null, # { -> [string] expected API key }
        [scriptblock]$LockAcquirer = $null, # { -> [bool] }
        [scriptblock]$LockReleaser = $null  # { }
    )
    if (-not $CodexInvoker) { $CodexInvoker = New-RealCodexInvoker }
    if (-not $KeyProvider)  { $KeyProvider  = { Get-Mem0Key } }
    if (-not $LockAcquirer) { $LockAcquirer = { Acquire-CodexLock -Owner 'codex-shim' } }
    if (-not $LockReleaser) { $LockReleaser = { Release-CodexLock } }

    $path = ($Path -replace '/+$', ''); if ($path -eq '') { $path = '/' }

    # --- GET /health (unauthenticated liveness; returns no secrets) ---
    if ($path -eq '/health') {
        if ($Method -ne 'GET') {
            return (ConvertTo-ShimResponse 405 @{ ok = $false; error = 'method not allowed'; error_type = 'method' })
        }
        return (ConvertTo-ShimResponse 200 @{
            ok            = $true
            service       = 'codex-shim'
            version       = $script:ShimVersion
            codex_present = (Test-Path -LiteralPath $script:CodexCmd)
        })
    }

    # --- POST /judge (authenticated codex call) ---
    if ($path -eq '/judge') {
        if ($Method -ne 'POST') {
            return (ConvertTo-ShimResponse 405 @{ ok = $false; error = 'method not allowed'; error_type = 'method' })
        }
        # Auth
        $presented = ''
        if ($Headers.ContainsKey('x-api-key')) { $presented = [string]$Headers['x-api-key'] }
        $expected = ''
        try { $expected = [string](& $KeyProvider) } catch { $expected = '' }
        if ([string]::IsNullOrEmpty($expected) -or -not (Test-ConstantTimeEqual $presented $expected)) {
            return (ConvertTo-ShimResponse 401 @{ ok = $false; error = 'unauthorized'; error_type = 'auth' })
        }
        # Body size guard
        if ($Body.Length -gt $script:ShimMaxBodyBytes) {
            return (ConvertTo-ShimResponse 413 @{ ok = $false; error = 'request too large'; error_type = 'too_large' })
        }
        # Parse JSON
        $req = $null
        try { $req = $Body | ConvertFrom-Json } catch { $req = $null }
        if (-not $req) {
            return (ConvertTo-ShimResponse 400 @{ ok = $false; error = 'invalid json body'; error_type = 'bad_request' })
        }
        $prompt = $null
        if ($req.PSObject.Properties['prompt']) { $prompt = [string]$req.prompt }
        if ([string]::IsNullOrWhiteSpace($prompt)) {
            return (ConvertTo-ShimResponse 400 @{ ok = $false; error = "missing 'prompt'"; error_type = 'bad_request' })
        }
        $effort = 'low'
        if ($req.PSObject.Properties['effort'] -and -not [string]::IsNullOrWhiteSpace([string]$req.effort)) {
            $e = ([string]$req.effort).ToLowerInvariant()
            if ($e -in @('low', 'medium', 'high')) { $effort = $e }
        }
        $timeoutSec = 60
        if ($req.PSObject.Properties['timeout_seconds']) {
            $t = 0
            if ([int]::TryParse([string]$req.timeout_seconds, [ref]$t) -and $t -gt 0) { $timeoutSec = $t }
        }
        if ($timeoutSec -lt $script:ShimMinTimeoutSec) { $timeoutSec = $script:ShimMinTimeoutSec }
        if ($timeoutSec -gt $script:ShimMaxTimeoutSec) { $timeoutSec = $script:ShimMaxTimeoutSec }

        # Shared codex lock — never run codex concurrently with L1a / C1.
        $gotLock = $false
        try { $gotLock = [bool](& $LockAcquirer) } catch { $gotLock = $false }
        if (-not $gotLock) {
            # Caller (write-gate) fails OPEN on this; the sweep retries later.
            return (ConvertTo-ShimResponse 503 @{ ok = $false; error = 'codex busy'; error_type = 'lock_contended' })
        }
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $result = & $CodexInvoker $prompt $effort $timeoutSec
            $sw.Stop()
            $respText = ''
            $tokens = 0
            if ($result -is [hashtable]) {
                if ($result.ContainsKey('response')) { $respText = [string]$result['response'] }
                if ($result.ContainsKey('tokens') -and $result['tokens']) { $tokens = [int]$result['tokens'] }
            } else {
                $respText = [string]$result
            }
            return (ConvertTo-ShimResponse 200 @{
                ok          = $true
                response    = $respText
                tokens_used = $tokens
                duration_ms = [int]$sw.Elapsed.TotalMilliseconds
            })
        } catch {
            $sw.Stop()
            $msg = $_.Exception.Message
            if ($msg -like '*timed out*') {
                return (ConvertTo-ShimResponse 504 @{ ok = $false; error = $msg; error_type = 'timeout'; duration_ms = [int]$sw.Elapsed.TotalMilliseconds })
            }
            return (ConvertTo-ShimResponse 502 @{ ok = $false; error = $msg; error_type = 'codex_error'; duration_ms = [int]$sw.Elapsed.TotalMilliseconds })
        } finally {
            try { & $LockReleaser } catch {}
        }
    }

    # --- unknown route ---
    return (ConvertTo-ShimResponse 404 @{ ok = $false; error = 'not found'; error_type = 'not_found' })
}

function Read-CappedStream {
    # Read at most MaxBytes+1 bytes from a stream, NEVER more — bounding memory
    # regardless of the declared Content-Length. This is the real body-cap guard:
    # a chunked / absent-Content-Length request reports ContentLength64 = -1, so a
    # length-only check (`-1 -gt cap` = $false) would fall through to an UNBOUNDED
    # ReadToEnd() and let an unauthenticated client force a multi-GB in-memory read
    # (audit 2026-06-15 MED). Returns @{ body=[string]; tooLarge=[bool] }: if more
    # than MaxBytes bytes are present, body='' and tooLarge=$true (the read stops at
    # MaxBytes+1, the surplus is never materialized).
    param(
        [Parameter(Mandatory)][System.IO.Stream]$Stream,
        [System.Text.Encoding]$Encoding,
        [int]$MaxBytes = 65536
    )
    if (-not $Encoding) { $Encoding = [System.Text.Encoding]::UTF8 }
    $ceil = $MaxBytes + 1
    $buf = New-Object byte[] $ceil
    $total = 0
    while ($total -lt $ceil) {
        $n = $Stream.Read($buf, $total, ($ceil - $total))
        if ($n -le 0) { break }
        $total += $n
    }
    if ($total -gt $MaxBytes) {
        return @{ body = ''; tooLarge = $true }
    }
    return @{ body = $Encoding.GetString($buf, 0, $total); tooLarge = $false }
}

function Read-ShimRequestContext {
    # Extract (method, path, lowercased-headers, body) from an HttpListenerContext.
    # The body read is byte-bounded via Read-CappedStream so an oversized stream
    # (incl. chunked/absent Content-Length) is never fully buffered.
    param([Parameter(Mandatory)][System.Net.HttpListenerContext]$Context)
    $req = $Context.Request
    $headers = @{}
    foreach ($k in $req.Headers.AllKeys) {
        if ($k) { $headers[$k.ToLowerInvariant()] = $req.Headers[$k] }
    }
    $body = ''
    $tooLarge = $false
    if ($req.HasEntityBody) {
        # Fast-reject a request that DECLARES an over-cap length (skip even the bounded read).
        if ($req.ContentLength64 -gt $script:ShimMaxBodyBytes) {
            $tooLarge = $true
        } else {
            $enc = if ($req.ContentEncoding) { $req.ContentEncoding } else { [System.Text.Encoding]::UTF8 }
            # Bounded read — covers ContentLength64 = -1 (chunked/absent) too.
            $r = Read-CappedStream -Stream $req.InputStream -Encoding $enc -MaxBytes $script:ShimMaxBodyBytes
            $body = $r.body
            $tooLarge = $r.tooLarge
        }
    }
    return @{
        method   = $req.HttpMethod
        path     = $req.Url.AbsolutePath
        headers  = $headers
        body     = $body
        tooLarge = $tooLarge
    }
}

function Start-CodexShim {
    param(
        [int]$Port = 0,
        [int]$IdleTimeoutMinutes = 240,
        [switch]$NoSingleton   # tests only: skip the global mutex so an integration test can bind an ephemeral port even if a real shim is running
    )
    Initialize-MemoryEnv
    $resolvedPort = Resolve-ShimPort -Override $Port

    # Single-instance guard (a duplicate spawn is a silent no-op).
    $mutex = $null
    if (-not $NoSingleton) {
        $createdNew = $false
        $mutex = [System.Threading.Mutex]::new($true, 'codex-http-shim-singleton', [ref]$createdNew)
        if (-not $createdNew) {
            Write-ShimLog "another shim instance owns the singleton mutex - exiting"
            return
        }
    }

    $listener = [System.Net.HttpListener]::new()
    # Bind BOTH loopback prefixes (HTTP.sys routes by Host header).
    $listener.Prefixes.Add("http://localhost:$resolvedPort/")
    $listener.Prefixes.Add("http://127.0.0.1:$resolvedPort/")
    # Slow-client (slow-loris) defense on the single-threaded loop: bound how long a
    # request may take to send its headers/body so one dribbled request can't wedge
    # the serve loop (audit 2026-06-15 LOW). Best-effort — TimeoutManager is not
    # settable on every platform, so guard it.
    try {
        $listener.TimeoutManager.HeaderWait      = [TimeSpan]::FromSeconds(15)
        $listener.TimeoutManager.EntityBody      = [TimeSpan]::FromSeconds(30)
        $listener.TimeoutManager.DrainEntityBody = [TimeSpan]::FromSeconds(15)
        $listener.TimeoutManager.IdleConnection  = [TimeSpan]::FromSeconds(60)
    } catch {}
    try {
        $listener.Start()
    } catch {
        Write-ShimLog "FATAL: HttpListener.Start failed on port $resolvedPort : $($_.Exception.Message)"
        if ($mutex) { $mutex.Dispose() }
        return
    }
    Write-ShimLog "codex-shim v$($script:ShimVersion) listening on http://localhost:$resolvedPort/ (idle-shutdown ${IdleTimeoutMinutes}m)"

    $idleMs = $IdleTimeoutMinutes * 60000
    $reqCount = 0
    try {
        while ($true) {
            $task = $listener.GetContextAsync()
            if (-not $task.Wait($idleMs)) {
                Write-ShimLog "idle ${IdleTimeoutMinutes}m with no requests - self-shutdown"
                break
            }
            $ctx = $task.Result
            $route = '?'; $status = 500
            try {
                $parsed = Read-ShimRequestContext -Context $ctx
                $route = "$($parsed.method) $($parsed.path)"
                if ($parsed.tooLarge) {
                    $resp = ConvertTo-ShimResponse 413 @{ ok = $false; error = 'request too large'; error_type = 'too_large' }
                } else {
                    $resp = Invoke-CodexShimRequest -Method $parsed.method -Path $parsed.path -Headers $parsed.headers -Body $parsed.body
                }
                $status = $resp.status
                $buf = [System.Text.Encoding]::UTF8.GetBytes($resp.body)
                $ctx.Response.StatusCode = $resp.status
                $ctx.Response.ContentType = $resp.contentType
                $ctx.Response.ContentLength64 = $buf.Length
                $ctx.Response.OutputStream.Write($buf, 0, $buf.Length)
            } catch {
                Write-ShimLog "handler error on $route : $($_.Exception.Message)"
                try {
                    $errBuf = [System.Text.Encoding]::UTF8.GetBytes('{"ok":false,"error":"internal error","error_type":"internal"}')
                    $ctx.Response.StatusCode = 500
                    $ctx.Response.ContentType = 'application/json'
                    $ctx.Response.ContentLength64 = $errBuf.Length
                    $ctx.Response.OutputStream.Write($errBuf, 0, $errBuf.Length)
                } catch {}
            } finally {
                try { $ctx.Response.OutputStream.Close() } catch {}
                try { $ctx.Response.Close() } catch {}
                Write-ShimLog "$route -> $status"
            }
            # Rotate logs periodically, NOT every request (audit 2026-06-15 LOW: a
            # per-request directory scan is wasted work on the hot serving path).
            $reqCount++
            if (($reqCount % 50) -eq 0) { try { Invoke-LogRotation } catch {} }
        }
    } finally {
        try { $listener.Stop(); $listener.Close() } catch {}
        if ($mutex) {
            try { $mutex.ReleaseMutex() } catch {}
            try { $mutex.Dispose() } catch {}
        }
        Write-ShimLog "codex-shim stopped"
    }
}

if ($DefineOnly) { return }

Start-CodexShim -Port $Port -IdleTimeoutMinutes $IdleTimeoutMinutes
