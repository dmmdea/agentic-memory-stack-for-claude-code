# CodexShim.Tests.ps1 — v0.27.1 R5 KEYSTONE: unit coverage for the Codex HTTP shim.
# Dot-sources codex-shim.ps1 -DefineOnly (loads functions, never binds a port). The
# request core (Invoke-CodexShimRequest) takes injected scriptblocks for codex, the
# key, and the codex lock, so every route/branch is testable without a listener, a
# real codex, the UNC key path, or the live lock file.
BeforeAll {
    . (Join-Path (Split-Path -Parent $PSScriptRoot) 'codex-shim.ps1') -DefineOnly

    $script:Key = 'UNIT-TEST-KEY-abc123'
    $script:KeyProvider = { 'UNIT-TEST-KEY-abc123' }
    $script:OkInvoker = { param($p, $e, $t) @{ response = "JUDGED:${p}:${e}:${t}"; tokens = 42 } }
    $script:GrantLock = { $true }
    $script:DenyLock = { $false }
    $script:NoopRelease = { }

    function Invoke-Judge {
        param([hashtable]$Headers, [string]$Body, [scriptblock]$Codex, [scriptblock]$Acquire, [scriptblock]$Release)
        if (-not $Codex)   { $Codex   = $script:OkInvoker }
        if (-not $Acquire) { $Acquire = $script:GrantLock }
        if (-not $Release) { $Release = $script:NoopRelease }
        Invoke-CodexShimRequest -Method 'POST' -Path '/judge' -Headers $Headers -Body $Body `
            -CodexInvoker $Codex -KeyProvider $script:KeyProvider -LockAcquirer $Acquire -LockReleaser $Release
    }
    function Auth { return @{ 'x-api-key' = $script:Key } }
}

Describe 'GET /health' {
    It 'returns 200 with ok=true, service, version (no auth required)' {
        $r = Invoke-CodexShimRequest -Method 'GET' -Path '/health' -KeyProvider { 'never-checked' }
        $r.status | Should -Be 200
        $obj = $r.body | ConvertFrom-Json
        $obj.ok | Should -BeTrue
        $obj.service | Should -Be 'codex-shim'
        $obj.version | Should -Not -BeNullOrEmpty
    }
    It 'rejects a non-GET method with 405' {
        (Invoke-CodexShimRequest -Method 'POST' -Path '/health').status | Should -Be 405
    }
    It 'tolerates a trailing slash' {
        (Invoke-CodexShimRequest -Method 'GET' -Path '/health/').status | Should -Be 200
    }
}

Describe 'POST /judge auth' {
    It 'returns 401 when no X-API-Key header is present' {
        (Invoke-Judge -Headers @{} -Body '{"prompt":"x"}').status | Should -Be 401
    }
    It 'returns 401 on a wrong key' {
        (Invoke-Judge -Headers @{ 'x-api-key' = 'WRONG' } -Body '{"prompt":"x"}').status | Should -Be 401
    }
    It 'returns 401 (not 500) when the key provider yields empty' {
        $r = Invoke-CodexShimRequest -Method 'POST' -Path '/judge' -Headers (Auth) -Body '{"prompt":"x"}' `
            -CodexInvoker $script:OkInvoker -KeyProvider { '' } -LockAcquirer $script:GrantLock -LockReleaser $script:NoopRelease
        $r.status | Should -Be 401
    }
    It 'accepts the correct key' {
        (Invoke-Judge -Headers (Auth) -Body '{"prompt":"hello"}').status | Should -Be 200
    }
}

Describe 'POST /judge happy path' {
    It 'invokes codex and returns ok=true with response + tokens + duration_ms' {
        $r = Invoke-Judge -Headers (Auth) -Body '{"prompt":"is A==B?","effort":"low"}'
        $r.status | Should -Be 200
        $obj = $r.body | ConvertFrom-Json
        $obj.ok | Should -BeTrue
        $obj.response | Should -Match 'JUDGED:is A==B\?:low:'
        $obj.tokens_used | Should -Be 42
        $obj.PSObject.Properties.Name | Should -Contain 'duration_ms'
    }
    It 'defaults effort to low and clamps an out-of-range effort' {
        $obj = (Invoke-Judge -Headers (Auth) -Body '{"prompt":"p","effort":"bogus"}').body | ConvertFrom-Json
        $obj.response | Should -Match ':low:'
    }
    It 'clamps timeout_seconds below the floor up to the minimum' {
        $obj = (Invoke-Judge -Headers (Auth) -Body '{"prompt":"p","timeout_seconds":1}').body | ConvertFrom-Json
        # min is 10s
        $obj.response | Should -Match ':10$'
    }
    It 'clamps timeout_seconds above the ceiling down to the max' {
        $obj = (Invoke-Judge -Headers (Auth) -Body '{"prompt":"p","timeout_seconds":99999}').body | ConvertFrom-Json
        $obj.response | Should -Match ':180$'
    }
}

Describe 'POST /judge bad input' {
    It 'returns 400 on invalid JSON' {
        (Invoke-Judge -Headers (Auth) -Body 'not json {').status | Should -Be 400
    }
    It 'returns 400 when prompt is missing' {
        (Invoke-Judge -Headers (Auth) -Body '{"effort":"low"}').status | Should -Be 400
    }
    It 'returns 400 when prompt is blank' {
        (Invoke-Judge -Headers (Auth) -Body '{"prompt":"   "}').status | Should -Be 400
    }
    It 'returns 413 when the body exceeds the size cap' {
        $big = '{"prompt":"' + ('x' * 70000) + '"}'
        (Invoke-Judge -Headers (Auth) -Body $big).status | Should -Be 413
    }
    It 'returns 405 on GET /judge' {
        (Invoke-CodexShimRequest -Method 'GET' -Path '/judge' -Headers (Auth) -KeyProvider $script:KeyProvider).status | Should -Be 405
    }
}

Describe 'POST /judge codex + lock failure modes' {
    It 'returns 503 (lock_contended) when the codex lock cannot be acquired' {
        $r = Invoke-Judge -Headers (Auth) -Body '{"prompt":"p"}' -Acquire $script:DenyLock
        $r.status | Should -Be 503
        ($r.body | ConvertFrom-Json).error_type | Should -Be 'lock_contended'
    }
    It 'does NOT invoke codex when the lock is contended' {
        $script:CalledCodex = $false
        $spy = { param($p, $e, $t) $script:CalledCodex = $true; @{ response = 'x'; tokens = 0 } }
        Invoke-Judge -Headers (Auth) -Body '{"prompt":"p"}' -Codex $spy -Acquire $script:DenyLock | Out-Null
        $script:CalledCodex | Should -BeFalse
    }
    It 'returns 504 (timeout) when codex times out' {
        $r = Invoke-Judge -Headers (Auth) -Body '{"prompt":"p"}' -Codex { param($p, $e, $t) throw 'Invoke-CodexSubagent timed out after 60s' }
        $r.status | Should -Be 504
        ($r.body | ConvertFrom-Json).error_type | Should -Be 'timeout'
    }
    It 'returns 502 (codex_error) on a non-timeout codex failure' {
        $r = Invoke-Judge -Headers (Auth) -Body '{"prompt":"p"}' -Codex { param($p, $e, $t) throw 'codex exited 3 : boom' }
        $r.status | Should -Be 502
        ($r.body | ConvertFrom-Json).error_type | Should -Be 'codex_error'
    }
    It 'ALWAYS releases the codex lock, even when codex throws' {
        $script:Released = $false
        Invoke-Judge -Headers (Auth) -Body '{"prompt":"p"}' `
            -Codex { param($p, $e, $t) throw 'codex exited 1 : x' } `
            -Release { $script:Released = $true } | Out-Null
        $script:Released | Should -BeTrue
    }
    It 'releases the lock on the happy path too' {
        $script:Released2 = $false
        Invoke-Judge -Headers (Auth) -Body '{"prompt":"p"}' -Release { $script:Released2 = $true } | Out-Null
        $script:Released2 | Should -BeTrue
    }
}

Describe 'unknown routes' {
    It 'returns 404 on an unknown path' {
        (Invoke-CodexShimRequest -Method 'GET' -Path '/nope').status | Should -Be 404
    }
}

Describe 'Test-ConstantTimeEqual' {
    It 'is true for equal strings' { Test-ConstantTimeEqual 'abcDEF123' 'abcDEF123' | Should -BeTrue }
    It 'is false for different strings of equal length' { Test-ConstantTimeEqual 'abcDEF123' 'abcDEF124' | Should -BeFalse }
    It 'is false for different lengths' { Test-ConstantTimeEqual 'abc' 'abcd' | Should -BeFalse }
    It 'handles null/empty without throwing' { Test-ConstantTimeEqual $null '' | Should -BeTrue }
}

Describe 'Read-CappedStream (the load-bearing body-cap guard)' {
    # Regression for the audit MED: a chunked / absent-Content-Length request reports
    # ContentLength64 = -1, so a length-only gate would fall through to an UNBOUNDED
    # read. Read-CappedStream bounds the read by BYTES regardless of declared length.
    BeforeAll {
        # Pester v5: helpers must live in BeforeAll to be visible inside It blocks.
        function New-Stream([int]$Len) {
            $b = New-Object byte[] $Len
            for ($i = 0; $i -lt $Len; $i++) { $b[$i] = 65 }  # 'A'
            return [System.IO.MemoryStream]::new($b)
        }
    }
    It 'returns the body when under the cap' {
        $s = New-Stream 50
        $r = Read-CappedStream -Stream $s -MaxBytes 100
        $r.tooLarge | Should -BeFalse
        $r.body.Length | Should -Be 50
    }
    It 'accepts a body exactly at the cap' {
        $r = Read-CappedStream -Stream (New-Stream 100) -MaxBytes 100
        $r.tooLarge | Should -BeFalse
        $r.body.Length | Should -Be 100
    }
    It 'flags tooLarge one byte over the cap' {
        $r = Read-CappedStream -Stream (New-Stream 101) -MaxBytes 100
        $r.tooLarge | Should -BeTrue
        $r.body | Should -BeNullOrEmpty
    }
    It 'is BOUNDED: never reads more than cap+1 bytes from a huge stream (the bypass fix)' {
        $s = New-Stream 1000000   # 1 MB — simulates an unbounded chunked body
        $r = Read-CappedStream -Stream $s -MaxBytes 100
        $r.tooLarge | Should -BeTrue
        $s.Position | Should -BeLessOrEqual 101 -Because 'the read must stop at cap+1, never materialize the whole 1MB body'
    }
}

Describe 'live HttpListener (integration)' {
    BeforeAll {
        $script:shimScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'codex-shim.ps1'
        # Grab a free ephemeral loopback port.
        $tl = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $tl.Start(); $script:port = $tl.LocalEndpoint.Port; $tl.Stop()
        $script:shimJob = Start-Job -ScriptBlock {
            param($sp, $p)
            . $sp -DefineOnly
            Start-CodexShim -Port $p -IdleTimeoutMinutes 1 -NoSingleton
        } -ArgumentList $script:shimScript, $script:port
        # Wait until it's listening.
        $script:up = $false
        foreach ($n in 1..40) {
            try { $r = Invoke-WebRequest -Uri "http://localhost:$($script:port)/health" -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { $script:up = $true; break } } catch { Start-Sleep -Milliseconds 200 }
        }
    }
    AfterAll {
        if ($script:shimJob) { Stop-Job $script:shimJob -ErrorAction SilentlyContinue; Remove-Job $script:shimJob -Force -ErrorAction SilentlyContinue }
    }
    It 'comes up and serves GET /health = 200' {
        $script:up | Should -BeTrue -Because 'the listener must bind the ephemeral port'
        $r = Invoke-WebRequest -Uri "http://localhost:$($script:port)/health" -UseBasicParsing -TimeoutSec 3
        $r.StatusCode | Should -Be 200
        ($r.Content | ConvertFrom-Json).service | Should -Be 'codex-shim'
    }
    It 'returns 404 on an unknown route' {
        $code = -1
        try { Invoke-WebRequest -Uri "http://localhost:$($script:port)/nope" -UseBasicParsing -TimeoutSec 3 } catch { $code = [int]$_.Exception.Response.StatusCode }
        $code | Should -Be 404
    }
    It 'returns 401 on POST /judge with no API key (auth reached through the real loop)' {
        $code = -1
        try { Invoke-WebRequest -Uri "http://localhost:$($script:port)/judge" -Method POST -Body '{"prompt":"x"}' -ContentType 'application/json' -UseBasicParsing -TimeoutSec 3 } catch { $code = [int]$_.Exception.Response.StatusCode }
        $code | Should -Be 401
    }
    It 'returns 413 for a CHUNKED over-cap body (the bypass is closed end-to-end, pre-auth)' {
        $req = [System.Net.HttpWebRequest]::Create("http://localhost:$($script:port)/judge")
        $req.Method = 'POST'; $req.SendChunked = $true; $req.ContentType = 'application/json'
        $req.Timeout = 8000; $req.ReadWriteTimeout = 8000   # never hang the suite
        $s = $req.GetRequestStream()
        $big = New-Object byte[] 80000   # > 64 KiB, sent with NO Content-Length
        $s.Write($big, 0, $big.Length); $s.Close()
        $code = -1
        try { $resp = $req.GetResponse(); $code = [int]$resp.StatusCode; $resp.Close() }
        catch [System.Net.WebException] { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } }
        $code | Should -Be 413
    }
}

Describe 'codex-shim-spawn.ps1 flag-gate' {
    BeforeAll {
        $script:winDir2 = Split-Path -Parent $PSScriptRoot
        $script:ps51 = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        function New-SpawnSandbox {
            $sb = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
            $sd = Join-Path $sb '.claude\scripts'
            New-Item -ItemType Directory -Path $sd -Force | Out-Null
            foreach ($f in 'codex-shim-spawn.ps1', 'codex-shim.ps1', 'memory-common.ps1') {
                Copy-Item (Join-Path $script:winDir2 $f) $sd
            }
            return $sb
        }
        function Get-ShimProcs {
            @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -and $_.CommandLine -match 'codex-shim\.ps1' })
        }
    }
    It 'marker ABSENT => exit 0 and nothing is launched on the port' {
        $sb = New-SpawnSandbox
        # Free ephemeral port for the spawn to (not) use.
        $tl = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $tl.Start(); $p = $tl.LocalEndpoint.Port; $tl.Stop()
        $saved = $env:USERPROFILE; $savedPort = $env:MEM0_CODEX_SHIM_PORT
        try {
            $env:USERPROFILE = $sb; $env:MEM0_CODEX_SHIM_PORT = "$p"
            # Pipe (and thus CLOSE) stdin so the launcher's [Console]::In.ReadToEnd()
            # returns — exactly as Claude Code's SessionStart hook delivers it. Without
            # this the script blocks forever waiting for stdin EOF.
            '' | & $script:ps51 -NoProfile -ExecutionPolicy Bypass -File (Join-Path $sb '.claude\scripts\codex-shim-spawn.ps1') *> $null
            $LASTEXITCODE | Should -Be 0
            Start-Sleep -Milliseconds 600
            $listening = [bool](Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue)
            $listening | Should -BeFalse -Because 'with no codex-shim.enabled marker the launcher must be a no-op'
        } finally {
            $env:USERPROFILE = $saved
            if ($savedPort) { $env:MEM0_CODEX_SHIM_PORT = $savedPort } else { Remove-Item Env:\MEM0_CODEX_SHIM_PORT -ErrorAction SilentlyContinue }
        }
    }
    It 'marker PRESENT but port already listening => does NOT double-spawn' {
        $sb = New-SpawnSandbox
        New-Item -ItemType Directory -Path (Join-Path $sb '.claude\state') -Force | Out-Null
        Set-Content -Path (Join-Path $sb '.claude\state\codex-shim.enabled') -Value '1'
        # Occupy the port ourselves so the launcher's probe sees it as already-running.
        $tl = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $tl.Start(); $p = $tl.LocalEndpoint.Port
        $saved = $env:USERPROFILE; $savedPort = $env:MEM0_CODEX_SHIM_PORT
        $before = Get-ShimProcs
        try {
            $env:USERPROFILE = $sb; $env:MEM0_CODEX_SHIM_PORT = "$p"
            # Pipe (and thus CLOSE) stdin so the launcher's [Console]::In.ReadToEnd()
            # returns — exactly as Claude Code's SessionStart hook delivers it. Without
            # this the script blocks forever waiting for stdin EOF.
            '' | & $script:ps51 -NoProfile -ExecutionPolicy Bypass -File (Join-Path $sb '.claude\scripts\codex-shim-spawn.ps1') *> $null
            $LASTEXITCODE | Should -Be 0
            Start-Sleep -Milliseconds 600
            $after = Get-ShimProcs
            $after.Count | Should -BeLessOrEqual $before.Count -Because 'the TCP probe must short-circuit when the shim port is already bound'
        } finally {
            $tl.Stop()
            $env:USERPROFILE = $saved
            if ($savedPort) { $env:MEM0_CODEX_SHIM_PORT = $savedPort } else { Remove-Item Env:\MEM0_CODEX_SHIM_PORT -ErrorAction SilentlyContinue }
        }
    }
}

Describe 'Resolve-ShimPort' {
    It 'prefers an explicit override' { Resolve-ShimPort -Override 12345 | Should -Be 12345 }
    It 'falls back to the default when no override and no env' {
        $saved = $env:MEM0_CODEX_SHIM_PORT
        Remove-Item Env:\MEM0_CODEX_SHIM_PORT -ErrorAction SilentlyContinue
        try { Resolve-ShimPort | Should -Be 18792 } finally { if ($saved) { $env:MEM0_CODEX_SHIM_PORT = $saved } }
    }
    It 'reads a valid env port when present and no override' {
        $saved = $env:MEM0_CODEX_SHIM_PORT
        $env:MEM0_CODEX_SHIM_PORT = '18799'
        try { Resolve-ShimPort | Should -Be 18799 } finally {
            if ($saved) { $env:MEM0_CODEX_SHIM_PORT = $saved } else { Remove-Item Env:\MEM0_CODEX_SHIM_PORT -ErrorAction SilentlyContinue }
        }
    }
}
