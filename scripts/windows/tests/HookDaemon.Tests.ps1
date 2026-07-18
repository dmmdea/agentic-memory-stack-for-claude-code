#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# HookDaemon.Tests.ps1 — v0.20 A.5: Pester 5 coverage of the resident-daemon
# client (user-prompt-lib.ps1) and the daemon protocol dispatcher
# (mem0-hook-daemon.ps1 -DefineOnly).
#
# The contract under test is FAIL-OPEN: Invoke-DaemonBundle must return $null
# on every failure mode (no pipe, connect timeout, garbage response, response
# timeout, ok=false, lib-hash mismatch) so the hook's inline path runs
# unchanged. Real named pipes are exercised via an in-process fake daemon
# (runspace thread serving one scripted response).
#
# Run: pwsh -NoProfile -Command "Invoke-Pester C:\path\to\agentic-memory-stack-for-claude-code\scripts\windows\tests\ -Output Detailed"

BeforeAll {
    $libPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'user-prompt-lib.ps1'
    if (-not (Test-Path $libPath)) { throw "user-prompt-lib.ps1 not found at $libPath" }
    . $libPath

    $daemonPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'mem0-hook-daemon.ps1'
    if (-not (Test-Path $daemonPath)) { throw "mem0-hook-daemon.ps1 not found at $daemonPath" }
    . $daemonPath -DefineOnly
    # Keep test diagnostics out of the real daemon log (Write-Log -> Write-DaemonLog)
    $script:DaemonLogPath = Join-Path $TestDrive 'hook-daemon-test.log'

    # In-process fake daemon: one server stream on a unique pipe, serves ONE
    # connection with a scripted behavior, then exits. Runs on a runspace
    # thread so the blocking WaitForConnection never blocks Pester.
    function script:Start-FakeDaemon {
        param(
            [string]$PipeName,
            [string]$ResponseLine,     # written + newline unless -NeverRespond
            [switch]$NeverRespond      # accept + read, then sleep (client must hit its response deadline)
        )
        $ps = [powershell]::Create()
        [void]$ps.AddScript({
            param($PipeName, $ResponseLine, $NeverRespond)
            $server = [System.IO.Pipes.NamedPipeServerStream]::new($PipeName, [System.IO.Pipes.PipeDirection]::InOut, 1, [System.IO.Pipes.PipeTransmissionMode]::Byte, [System.IO.Pipes.PipeOptions]::None)
            try {
                $server.WaitForConnection()
                $buf = [byte[]]::new(65536)
                [void]$server.Read($buf, 0, $buf.Length)   # consume the request line
                if ($NeverRespond) {
                    [System.Threading.Thread]::Sleep(5000)
                } else {
                    $bytes = [System.Text.Encoding]::UTF8.GetBytes($ResponseLine + "`n")
                    $server.Write($bytes, 0, $bytes.Length)
                    $server.Flush()
                    try { $server.WaitForPipeDrain() } catch {}
                }
                try { $server.Disconnect() } catch {}
            } catch {} finally { $server.Dispose() }
        }).AddArgument($PipeName).AddArgument($ResponseLine).AddArgument([bool]$NeverRespond)
        $handle = $ps.BeginInvoke()
        # Wait until the pipe is visible before letting the client at it
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        while ($sw.ElapsedMilliseconds -lt 2000 -and -not (Test-DaemonPipePresent -PipeName $PipeName)) {
            Start-Sleep -Milliseconds 25
        }
        return @{ PS = $ps; Handle = $handle }
    }

    function script:Stop-FakeDaemon {
        param($Fake)
        try { $Fake.PS.Stop() } catch {}
        try { $Fake.PS.Dispose() } catch {}
    }

    function script:New-TestPipeName { 'a5test-' + [guid]::NewGuid().ToString('N') }
}

Describe 'Get-FileSha256Hex (deploy-staleness handshake input)' {

    It 'returns 64 lowercase hex chars and is content-stable' {
        $f = Join-Path $TestDrive 'hash-probe.txt'
        Set-Content -Path $f -Value 'stable content' -NoNewline
        $h1 = Get-FileSha256Hex -Path $f
        $h2 = Get-FileSha256Hex -Path $f
        $h1 | Should -Match '^[0-9a-f]{64}$'
        $h2 | Should -Be $h1
    }

    It 'changes when the file content changes (a comment edit = new deploy identity)' {
        $f = Join-Path $TestDrive 'hash-probe2.txt'
        Set-Content -Path $f -Value 'before' -NoNewline
        $h1 = Get-FileSha256Hex -Path $f
        Set-Content -Path $f -Value 'before # touched' -NoNewline
        Get-FileSha256Hex -Path $f | Should -Not -Be $h1
    }

    It 'returns $null for a missing file (caller then skips the daemon entirely)' {
        Get-FileSha256Hex -Path (Join-Path $TestDrive 'no-such-file.ps1') | Should -BeNullOrEmpty
    }
}

Describe 'Get-HandshakeHash (v0.21 Phase B M3/M6 combined lib+daemon digest)' {

    BeforeEach {
        $script:hsDir = Join-Path $TestDrive ("hs-{0}" -f ([guid]::NewGuid().ToString('N')))
        New-Item -ItemType Directory -Path $script:hsDir -Force | Out-Null
        Set-Content -Path (Join-Path $script:hsDir 'user-prompt-lib.ps1') -Value '# lib v1' -NoNewline
        Set-Content -Path (Join-Path $script:hsDir 'mem0-hook-daemon.ps1') -Value '# daemon v1' -NoNewline
    }

    It 'equals SHA256(Sha256Hex(lib)+Sha256Hex(daemon)) computed independently (same formula all sides)' {
        $libH = Get-FileSha256Hex -Path (Join-Path $script:hsDir 'user-prompt-lib.ps1')
        $daeH = Get-FileSha256Hex -Path (Join-Path $script:hsDir 'mem0-hook-daemon.ps1')
        $expected = Get-StringSha256Hex ($libH + $daeH)
        Get-HandshakeHash -ScriptDir $script:hsDir | Should -Be $expected
        $expected | Should -Match '^[0-9a-f]{64}$'
    }

    It 'a DAEMON-file content change with an UNCHANGED lib flips the digest (closes the daemon-only-deploy hole)' {
        $before = Get-HandshakeHash -ScriptDir $script:hsDir
        # lib byte-identical; only the daemon script changes (e.g. a comment edit)
        Set-Content -Path (Join-Path $script:hsDir 'mem0-hook-daemon.ps1') -Value '# daemon v2 (comment changed)' -NoNewline
        $after = Get-HandshakeHash -ScriptDir $script:hsDir
        $after | Should -Not -Be $before
    }

    It 'a LIB content change also flips the digest (unchanged from the old single-file behavior)' {
        $before = Get-HandshakeHash -ScriptDir $script:hsDir
        Set-Content -Path (Join-Path $script:hsDir 'user-prompt-lib.ps1') -Value '# lib v2' -NoNewline
        Get-HandshakeHash -ScriptDir $script:hsDir | Should -Not -Be $before
    }

    It 'returns $null when either file is missing (caller skips the daemon)' {
        Remove-Item (Join-Path $script:hsDir 'mem0-hook-daemon.ps1') -Force
        Get-HandshakeHash -ScriptDir $script:hsDir | Should -BeNullOrEmpty
    }
}

Describe 'Test-DaemonResponse (pure response validation)' {

    It 'accepts ok=true with the matching lib hash' {
        $r = [pscustomobject]@{ ok = $true; context_block = 'x'; lib_hash = 'h1' }
        Test-DaemonResponse -Response $r -ExpectedLibHash 'h1' | Should -Be 'ok'
    }

    It 'flags a lib-hash mismatch (stale daemon after a deploy)' {
        $r = [pscustomobject]@{ ok = $true; context_block = 'x'; lib_hash = 'old-hash' }
        Test-DaemonResponse -Response $r -ExpectedLibHash 'new-hash' | Should -Be 'hash_mismatch'
    }

    It 'rejects ok=false responses as invalid' {
        $r = [pscustomobject]@{ ok = $false; error = 'no_api_key'; lib_hash = 'h1' }
        Test-DaemonResponse -Response $r -ExpectedLibHash 'h1' | Should -Be 'invalid'
    }

    It 'rejects a response missing lib_hash (handshake cannot be verified)' {
        $r = [pscustomobject]@{ ok = $true; context_block = 'x' }
        Test-DaemonResponse -Response $r -ExpectedLibHash 'h1' | Should -Be 'invalid'
    }

    It 'rejects a null response' {
        Test-DaemonResponse -Response $null -ExpectedLibHash 'h1' | Should -Be 'invalid'
    }
}

Describe 'Test-DaemonPipePresent (no-open existence probe)' {

    It 'returns $false for an absent pipe' {
        Test-DaemonPipePresent -PipeName (New-TestPipeName) | Should -BeFalse
    }

    It 'returns $true while a server holds the pipe' {
        $name = New-TestPipeName
        $server = [System.IO.Pipes.NamedPipeServerStream]::new($name, [System.IO.Pipes.PipeDirection]::InOut, 1, [System.IO.Pipes.PipeTransmissionMode]::Byte, [System.IO.Pipes.PipeOptions]::Asynchronous)
        try { Test-DaemonPipePresent -PipeName $name | Should -BeTrue }
        finally { $server.Dispose() }
    }
}

Describe 'Invoke-DaemonBundle fail-open matrix (client decision logic)' {

    BeforeEach {
        $script:req = @{ op = 'bundle'; session_id = 'a5-test'; prompt = 'test prompt'; brand = $null;
                         workspace = 'ai-ecosystem'; project = $null; transcript_path = 'x'; hook_contract_version = '20.0' }
    }

    It 'no pipe -> $null, fast (no 2.5s stall), no throw' {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $r = Invoke-DaemonBundle -Request $script:req -ExpectedLibHash 'h1' -PipeName (New-TestPipeName)
        $sw.Stop()
        $r | Should -BeNullOrEmpty
        $sw.ElapsedMilliseconds | Should -BeLessThan 500
    }

    It 'empty ExpectedLibHash -> $null without touching the pipe (staleness unverifiable)' {
        Invoke-DaemonBundle -Request $script:req -ExpectedLibHash '' -PipeName (New-TestPipeName) | Should -BeNullOrEmpty
    }

    It 'valid response with matching hash -> returns the response with context_block' {
        $name = New-TestPipeName
        $respLine = (@{ ok = $true; context_block = "[MEMORY CONTEXT - test]`nline2"; lib_hash = 'h1';
                        diag = @{ episode_id = 7; action = 'updated'; memories = 1; goals = 0; oq = 0; ms = 12 } } | ConvertTo-Json -Compress -Depth 6)
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine $respLine
        try {
            $r = Invoke-DaemonBundle -Request $script:req -ExpectedLibHash 'h1' -PipeName $name
            $r | Should -Not -BeNullOrEmpty
            $r.ok | Should -BeTrue
            $r.context_block | Should -Be "[MEMORY CONTEXT - test]`nline2"
            $r.diag.episode_id | Should -Be 7
        } finally { Stop-FakeDaemon $fake }
    }

    It 'garbage (non-JSON) response -> $null' {
        $name = New-TestPipeName
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine 'this is not json {{{'
        try {
            Invoke-DaemonBundle -Request $script:req -ExpectedLibHash 'h1' -PipeName $name | Should -BeNullOrEmpty
        } finally { Stop-FakeDaemon $fake }
    }

    It 'lib-hash mismatch -> $null (stale daemon never serves a deploy)' {
        $name = New-TestPipeName
        $respLine = (@{ ok = $true; context_block = 'stale'; lib_hash = 'OLD' } | ConvertTo-Json -Compress)
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine $respLine
        try {
            Invoke-DaemonBundle -Request $script:req -ExpectedLibHash 'NEW' -PipeName $name | Should -BeNullOrEmpty
        } finally { Stop-FakeDaemon $fake }
    }

    It 'ok=false response -> $null' {
        $name = New-TestPipeName
        $respLine = (@{ ok = $false; error = 'no_api_key'; lib_hash = 'h1' } | ConvertTo-Json -Compress)
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine $respLine
        try {
            Invoke-DaemonBundle -Request $script:req -ExpectedLibHash 'h1' -PipeName $name | Should -BeNullOrEmpty
        } finally { Stop-FakeDaemon $fake }
    }

    It 'response deadline exceeded (daemon hangs) -> $null at ~ResponseTimeoutMs, not the HTTP 3s' {
        $name = New-TestPipeName
        $fake = Start-FakeDaemon -PipeName $name -NeverRespond
        try {
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            $r = Invoke-DaemonBundle -Request $script:req -ExpectedLibHash 'h1' -PipeName $name -ResponseTimeoutMs 400
            $sw.Stop()
            $r | Should -BeNullOrEmpty
            $sw.ElapsedMilliseconds | Should -BeLessThan 2000
        } finally { Stop-FakeDaemon $fake }
    }

    It 'Start-HookDaemonDetached with a missing daemon path -> $false, no throw' {
        Start-HookDaemonDetached -DaemonPath (Join-Path $TestDrive 'no-daemon-here.ps1') | Should -BeFalse
    }
}

Describe 'ConvertFrom-DaemonRawResponse (raw fast-path response parsing, zero JSON machinery)' {

    BeforeAll {
        $script:goodHash = ('a' * 64)
        function script:B64([string]$s) { [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($s)) }
        function script:NewRawLine {
            param([string]$Hash, [string]$Block = "[MEMORY CONTEXT - x]`nline2", [string]$Prompt = 'pick 1 or 2', [string]$TPath = 'C:\t\u.jsonl', [string]$Sid = 'abc', [string]$Brand = 'ai-ecosystem')
            '{"ok":true,"served":true,"lib_hash":"' + $Hash + '","sid_b64":"' + (B64 $Sid) + '","context_b64":"' + (B64 $Block) + '","prompt_b64":"' + (B64 $Prompt) + '","tpath_b64":"' + (B64 $TPath) + '","brand_b64":"' + (B64 $Brand) + '","diag_b64":"' + (B64 'episode_id=1') + '"}'
        }
    }

    It 'decodes all fields from a valid response (any key order, b64-safe)' {
        $r = ConvertFrom-DaemonRawResponse -Line (NewRawLine -Hash $script:goodHash) -ExpectedLibHash $script:goodHash
        $r.verdict | Should -Be 'ok'
        $r.context_block | Should -Be "[MEMORY CONTEXT - x]`nline2"
        $r.prompt | Should -Be 'pick 1 or 2'
        $r.transcript_path | Should -Be 'C:\t\u.jsonl'
        $r.session_id | Should -Be 'abc'
        $r.brand | Should -Be 'ai-ecosystem'
    }

    It 'empty b64 fields decode to $null (e.g. no context to inject)' {
        $line = '{"ok":true,"served":true,"lib_hash":"' + $script:goodHash + '","sid_b64":"","context_b64":"","prompt_b64":"","tpath_b64":"","brand_b64":"","diag_b64":""}'
        $r = ConvertFrom-DaemonRawResponse -Line $line -ExpectedLibHash $script:goodHash
        $r.verdict | Should -Be 'ok'
        $r.context_block | Should -BeNullOrEmpty
        $r.prompt | Should -BeNullOrEmpty
    }

    It 'stale-lib hash -> hash_mismatch verdict' {
        $r = ConvertFrom-DaemonRawResponse -Line (NewRawLine -Hash ('b' * 64)) -ExpectedLibHash $script:goodHash
        $r.verdict | Should -Be 'hash_mismatch'
    }

    It 'rejects ok:false / missing served / garbage / empty' {
        ConvertFrom-DaemonRawResponse -Line ('{"ok":false,"error":"x","lib_hash":"' + $script:goodHash + '"}') -ExpectedLibHash $script:goodHash | Should -BeNullOrEmpty
        ConvertFrom-DaemonRawResponse -Line ('{"ok":true,"lib_hash":"' + $script:goodHash + '"}') -ExpectedLibHash $script:goodHash | Should -BeNullOrEmpty
        ConvertFrom-DaemonRawResponse -Line 'not json at all' -ExpectedLibHash $script:goodHash | Should -BeNullOrEmpty
        ConvertFrom-DaemonRawResponse -Line '' -ExpectedLibHash $script:goodHash | Should -BeNullOrEmpty
    }
}

Describe 'Invoke-DaemonRawTransaction fail-open matrix' {

    It 'no pipe -> $null, fast, no throw' {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $r = Invoke-DaemonRawTransaction -RawStdin '{"hook_event_name":"UserPromptSubmit"}' -ExpectedLibHash ('a' * 64) -PipeName (New-TestPipeName)
        $sw.Stop()
        $r | Should -BeNullOrEmpty
        $sw.ElapsedMilliseconds | Should -BeLessThan 800
    }

    It 'valid framed response -> decoded fields' {
        $name = New-TestPipeName
        $hash = 'c' * 64
        $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes('[MEMORY CONTEXT - raw]'))
        $pb64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes('full prompt text'))
        $line = '{"ok":true,"served":true,"lib_hash":"' + $hash + '","sid_b64":"","context_b64":"' + $b64 + '","prompt_b64":"' + $pb64 + '","tpath_b64":"","brand_b64":"","diag_b64":""}'
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine $line
        try {
            $r = Invoke-DaemonRawTransaction -RawStdin '{"hook_event_name":"UserPromptSubmit","prompt":"x y z"}' -ExpectedLibHash $hash -PipeName $name
            $r | Should -Not -BeNullOrEmpty
            $r.context_block | Should -Be '[MEMORY CONTEXT - raw]'
            $r.prompt | Should -Be 'full prompt text'
        } finally { Stop-FakeDaemon $fake }
    }

    It 'hash-mismatch framed response -> $null (inline fallback + shutdown signal)' {
        $name = New-TestPipeName
        $line = '{"ok":true,"served":true,"lib_hash":"' + ('d' * 64) + '","sid_b64":"","context_b64":"","prompt_b64":"","tpath_b64":"","brand_b64":"","diag_b64":""}'
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine $line
        try {
            Invoke-DaemonRawTransaction -RawStdin '{"x":1}' -ExpectedLibHash ('e' * 64) -PipeName $name | Should -BeNullOrEmpty
        } finally { Stop-FakeDaemon $fake }
    }

    It 'empty stdin or empty expected hash -> $null without touching the pipe' {
        Invoke-DaemonRawTransaction -RawStdin '' -ExpectedLibHash ('a' * 64) -PipeName (New-TestPipeName) | Should -BeNullOrEmpty
        Invoke-DaemonRawTransaction -RawStdin '{"x":1}' -ExpectedLibHash '' -PipeName (New-TestPipeName) | Should -BeNullOrEmpty
    }
}

Describe 'Daemon raw pipeline (Invoke-DaemonRawBundle via -DefineOnly, mocked HTTP)' {

    BeforeEach {
        $script:LibHash = 'f' * 64
        $script:BaseUrl = 'http://127.0.0.1:1'
        $script:HookContractVersion = '20.0'
        $script:StaleExitRequested = $false   # v0.22 L2: reset the self-exit flag per test
        $script:stateDir = Join-Path $TestDrive ("state-{0}" -f ([guid]::NewGuid().ToString('N')))
        $script:fixDir = Join-Path $script:stateDir 'hook-fixtures'
        New-Item -ItemType Directory -Path $script:stateDir -Force | Out-Null
        $script:sid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000'
        $script:rawSubstantive = '{"hook_event_name":"UserPromptSubmit","prompt":"what is the state of the admission gate","transcript_path":"C:\\x\\agentic-memory-stack\\' + $script:sid + '.jsonl"}'
    }

    It 'substantive prompt: bundle POST + render, rate-limit token consumed, 0.B fields returned' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        Mock Invoke-Mem0Post {
            '{"ok":true,"checkpoint":{"ok":true,"episode_id":9,"action":"created"},"memories":[{"id":"m1","memory":"raw path memory","metadata":{"tier":"evidence","brand":null}}],"goals":[],"open_questions":[]}'
        }
        $r = Invoke-DaemonRawBundle -RawStdin $script:rawSubstantive -StateDir $script:stateDir -FixtureDir $script:fixDir
        $r.ok | Should -BeTrue
        $r.served | Should -BeTrue
        $r.lib_hash | Should -Be ('f' * 64)
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.context_b64)) | Should -Match 'raw path memory'
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.sid_b64)) | Should -Be $script:sid
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.prompt_b64)) | Should -Be 'what is the state of the admission gate'
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.brand_b64)) | Should -Be 'ai-ecosystem'
        $r.needs_0b | Should -BeFalse   # M4: a non-decision prompt -> daemon verdict false
        Test-Path (Join-Path $script:stateDir "user-prompt-rate-limit-$($script:sid)") | Should -BeTrue
        Should -Invoke Invoke-Mem0Post -Times 1 -Exactly -ParameterFilter { $Uri -like '*context/bundle' }
    }

    It 'decision-like prompt -> needs_0b TRUE (M4: 0.B verdict computed daemon-side)' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        Mock Invoke-Mem0Post { '{"ok":true,"episode_id":3,"action":"updated"}' }
        # "1 and 2" is trivial (word-count gate -> checkpoint-only) yet decision-like
        $raw = '{"hook_event_name":"UserPromptSubmit","prompt":"1 and 2","transcript_path":"C:\\x\\agentic-memory-stack\\' + $script:sid + '.jsonl"}'
        $r = Invoke-DaemonRawBundle -RawStdin $raw -StateDir $script:stateDir -FixtureDir $script:fixDir
        $r.served | Should -BeTrue
        $r.needs_0b | Should -BeTrue
    }

    It 'immediate second substantive prompt: rate-limited -> checkpoint-only POST' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        Mock Invoke-Mem0Post { '{"ok":true,"checkpoint":{"episode_id":9,"action":"updated"},"memories":[],"goals":[],"open_questions":[],"episode_id":9,"action":"updated"}' }
        $null = Invoke-DaemonRawBundle -RawStdin $script:rawSubstantive -StateDir $script:stateDir -FixtureDir $script:fixDir
        $r2 = Invoke-DaemonRawBundle -RawStdin $script:rawSubstantive -StateDir $script:stateDir -FixtureDir $script:fixDir
        $r2.served | Should -BeTrue
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r2.diag_b64)) | Should -Match 'checkpoint-only \(rate-limited\)'
        Should -Invoke Invoke-Mem0Post -Times 1 -Exactly -ParameterFilter { $Uri -like '*episodes/checkpoint' }
    }

    It 'trivial prompt -> checkpoint-only POST (no bundle, no surfacing)' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        Mock Invoke-Mem0Post { '{"ok":true,"episode_id":3,"action":"updated"}' }
        $raw = '{"hook_event_name":"UserPromptSubmit","prompt":"ok","transcript_path":"C:\\x\\agentic-memory-stack\\' + $script:sid + '.jsonl"}'
        $r = Invoke-DaemonRawBundle -RawStdin $raw -StateDir $script:stateDir -FixtureDir $script:fixDir
        $r.served | Should -BeTrue
        $r.context_b64 | Should -Be ''
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.diag_b64)) | Should -Match 'checkpoint-only \(trivial\)'
        Should -Invoke Invoke-Mem0Post -Times 0 -ParameterFilter { $Uri -like '*context/bundle' }
    }

    It 'non-UserPromptSubmit event -> served-nothing (exit-0 equivalent), no HTTP' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        Mock Invoke-Mem0Post { throw 'must not be called' }
        $r = Invoke-DaemonRawBundle -RawStdin '{"hook_event_name":"PreToolUse","prompt":"x y z"}' -StateDir $script:stateDir -FixtureDir $script:fixDir
        $r.served | Should -BeTrue
        $r.context_b64 | Should -Be ''
        $r.prompt_b64 | Should -Be ''
        Should -Invoke Invoke-Mem0Post -Times 0
    }

    It 'unparseable stdin -> served-nothing with stdin_parse_failed diag (WARN+exit-0 equivalent)' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        $r = Invoke-DaemonRawBundle -RawStdin 'garbage {{' -StateDir $script:stateDir -FixtureDir $script:fixDir
        $r.served | Should -BeTrue
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.diag_b64)) | Should -Be 'stdin_parse_failed'
    }

    It 'bundle POST failure -> served with no block but 0.B fields intact (inline parity)' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        Mock Invoke-Mem0Post { throw 'connection refused' }
        $r = Invoke-DaemonRawBundle -RawStdin $script:rawSubstantive -StateDir $script:stateDir -FixtureDir $script:fixDir
        $r.ok | Should -BeTrue
        $r.served | Should -BeTrue
        $r.context_b64 | Should -Be ''
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.diag_b64)) | Should -Match 'bundle_failed'
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.prompt_b64)) | Should -Be 'what is the state of the admission gate'
    }

    It 'bundle_raw dispatch with bad base64 -> ok=false (client falls back inline)' {
        $r = Invoke-DaemonRequest -Req ([pscustomobject]@{ op = 'bundle_raw'; stdin_b64 = '!!!not-b64!!!' })
        $r.ok | Should -BeFalse
        $r.error | Should -Be 'bad_stdin_b64'
    }

    It 'stale expected_lib_hash -> short-circuits BEFORE any side effect: no token, no HTTP, inline rate-limit stays open (L1)' {
        $script:LibHash = 'f' * 64   # the daemon''s current digest
        Mock Get-Mem0ApiKeyCached { 'k' }
        Mock Invoke-Mem0Post { throw 'must not be called on a stale short-circuit' }
        $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($script:rawSubstantive))
        $r = Invoke-DaemonRequest -Req ([pscustomobject]@{ op = 'bundle_raw'; expected_lib_hash = ('e' * 64); stdin_b64 = $b64 })
        # response is accepted by the parser (served=true, valid lib_hash) but the
        # OLD lib_hash trips the client''s hash check -> inline + shutdown
        $r.ok | Should -BeTrue
        $r.served | Should -BeTrue
        $r.lib_hash | Should -Be ('f' * 64)
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.diag_b64)) | Should -Be 'stale_lib'
        # NO side effects: no bundle/checkpoint POST, no rate-limit token file
        Should -Invoke Invoke-Mem0Post -Times 0
        Test-Path (Join-Path $script:stateDir "user-prompt-rate-limit-$($script:sid)") | Should -BeFalse
        # ...so a follow-up inline rate-limit decision on the same sid is NOT limited
        $rl = Get-RateLimitDecision -StateDir $script:stateDir -SessionId $script:sid -NowFileTimeUtc ([System.DateTime]::Now.ToFileTimeUtc()) -CooldownMs 1000
        $rl.RateLimited | Should -BeFalse
        # v0.22 L2: the short-circuit also requests daemon self-exit so the serve
        # loop stops after answering this one prompt -> deterministic rollover
        # (one inline prompt) instead of relying on the client's best-effort shutdown
        $script:StaleExitRequested | Should -BeTrue
    }

    It 'matching expected_lib_hash -> full bundle proceeds (short-circuit only on mismatch)' {
        $script:LibHash = 'f' * 64
        Mock Get-Mem0ApiKeyCached { 'k' }
        Mock Invoke-Mem0Post { '{"ok":true,"checkpoint":{"episode_id":5,"action":"created"},"memories":[],"goals":[],"open_questions":[]}' }
        $b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($script:rawSubstantive))
        $r = Invoke-DaemonRequest -Req ([pscustomobject]@{ op = 'bundle_raw'; expected_lib_hash = ('f' * 64); stdin_b64 = $b64 })
        $r.served | Should -BeTrue
        [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($r.diag_b64)) | Should -Not -Be 'stale_lib'
        Should -Invoke Invoke-Mem0Post -Times 1 -Exactly -ParameterFilter { $Uri -like '*context/bundle' }
        # v0.22 L2: a matching hash must NOT request self-exit (daemon keeps serving)
        $script:StaleExitRequested | Should -BeFalse
    }
}

Describe 'Daemon protocol dispatch (Invoke-DaemonRequest via -DefineOnly)' {

    BeforeEach {
        $script:LibHash = 'test-lib-hash'
        $script:BaseUrl = 'http://127.0.0.1:1'   # never reached: Invoke-Mem0Post is mocked
    }

    It 'ping -> ok + lib_hash (handshake probe)' {
        $r = Invoke-DaemonRequest -Req ([pscustomobject]@{ op = 'ping' })
        $r.ok | Should -BeTrue
        $r.lib_hash | Should -Be 'test-lib-hash'
    }

    It 'shutdown -> acked with ok=true' {
        $r = Invoke-DaemonRequest -Req ([pscustomobject]@{ op = 'shutdown' })
        $r.ok | Should -BeTrue
        $r.op | Should -Be 'shutdown'
    }

    It 'unknown op -> ok=false unknown_op (still carries lib_hash)' {
        $r = Invoke-DaemonRequest -Req ([pscustomobject]@{ op = 'frobnicate' })
        $r.ok | Should -BeFalse
        $r.error | Should -Be 'unknown_op'
        $r.lib_hash | Should -Be 'test-lib-hash'
    }

    It 'bundle: renders [MEMORY CONTEXT] through the real lib renderer (mocked HTTP)' {
        Mock Get-Mem0ApiKeyCached { 'test-key' }
        Mock Invoke-Mem0Post {
            '{"ok":true,"checkpoint":{"ok":true,"episode_id":42,"action":"updated"},"memories":[{"id":"m1","memory":"daemon parity memory","metadata":{"tier":"evidence","brand":null}}],"goals":[{"title":"test goal","priority":2,"status":"open"}],"open_questions":[]}'
        }
        $req = [pscustomobject]@{ op = 'bundle'; session_id = 's1'; prompt = 'p'; brand = $null;
                                  workspace = 'ai-ecosystem'; project = $null; transcript_path = 't'; hook_contract_version = '20.0' }
        $r = Invoke-DaemonRequest -Req $req
        $r.ok | Should -BeTrue
        $r.lib_hash | Should -Be 'test-lib-hash'
        $r.context_block | Should -Match '\[MEMORY CONTEXT'
        $r.context_block | Should -Match 'daemon parity memory'
        $r.context_block | Should -Match 'test goal'
        $r.diag.episode_id | Should -Be 42
        $r.diag.action | Should -Be 'updated'
        $r.diag.memories | Should -Be 1
    }

    It 'v1.0 R2: op=bundle forwards the client tier + renders tier-aware (small legend)' {
        # Regression guard for the audit HIGH: the legacy op=bundle path must
        # forward $Req.tier into the bundle body AND pass -Tier to the renderer,
        # matching op=bundle_raw + the inline path. With tier=small the rendered
        # block must carry the small-tier legend.
        Mock Get-Mem0ApiKeyCached { 'test-key' }
        $script:capturedBody = $null
        Mock Invoke-Mem0Post {
            $script:capturedBody = $Body
            '{"ok":true,"checkpoint":{"ok":true,"episode_id":7,"action":"created"},"memories":[{"id":"m1","memory":"small tier parity memory","metadata":{"tier":"evidence","brand":null}}],"goals":[],"open_questions":[]}'
        }
        $req = [pscustomobject]@{ op = 'bundle'; session_id = 's1'; prompt = 'p'; brand = $null;
                                  workspace = 'ai-ecosystem'; project = $null; initiative = 'agentic-memory-stack';
                                  tier = 'small'; transcript_path = 't'; hook_contract_version = '20.0' }
        $r = Invoke-DaemonRequest -Req $req
        $r.ok | Should -BeTrue
        $r.context_block | Should -Match 'Memory tiers:'            # small-tier legend => -Tier small propagated to render
        $r.context_block | Should -Match 'small tier parity memory'
        $script:capturedBody | Should -Match '"tier"\s*:\s*"small"'  # tier forwarded into the bundle request body
        $script:capturedBody | Should -Match 'agentic-memory-stack'  # initiative forwarded
    }

    It 'bundle with no API key available -> ok=false no_api_key (client falls back inline)' {
        Mock Get-Mem0ApiKeyCached { $null }
        $req = [pscustomobject]@{ op = 'bundle'; session_id = 's1'; prompt = 'p'; brand = $null;
                                  workspace = 'ai-ecosystem'; project = $null; transcript_path = 't'; hook_contract_version = '20.0' }
        $r = Invoke-DaemonRequest -Req $req
        $r.ok | Should -BeFalse
        $r.error | Should -Be 'no_api_key'
    }

    It 'bundle when the HTTP call throws -> ok=false bundle_failed (never a crash)' {
        Mock Get-Mem0ApiKeyCached { 'test-key' }
        Mock Invoke-Mem0Post { throw 'connection refused' }
        $req = [pscustomobject]@{ op = 'bundle'; session_id = 's1'; prompt = 'p'; brand = $null;
                                  workspace = 'ai-ecosystem'; project = $null; transcript_path = 't'; hook_contract_version = '20.0' }
        $r = Invoke-DaemonRequest -Req $req
        $r.ok | Should -BeFalse
        $r.error | Should -Match 'bundle_failed'
    }
}
