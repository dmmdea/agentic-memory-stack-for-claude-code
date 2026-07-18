#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# HookClient.Tests.ps1 — v0.20 A.6: Pester 5 coverage of the compiled thin
# UserPromptSubmit client (mem0-hook-client.cs/.exe) and its build gate
# (build-hook-client.ps1), plus the -SkipDaemon / 0.B-only mechanisms added to
# user-prompt-extract.ps1.
#
# The contract under test is the SAME fail-open contract as A.5, now enforced
# by the exe: ANY daemon failure (no pipe, garbage response, lib-hash mismatch)
# must relay verbatim stdin to the PS inline path with -SkipDaemon (so the
# script never re-probes the daemon) and exit 0; a daemon SUCCESS must emit the
# block and only spawn PowerShell for Phase 0.B (MEM0_HOOK_DAEMON_SERVED=1) on
# decision-like prompts. The REAL exe is built by the real build script into
# TestDrive and exercised against scripted fake daemons (MEM0_HOOK_PIPE) and a
# recording stub user-prompt-extract.ps1 — no live daemon, no live HTTP, no
# checkpoint debris.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester C:\path\to\agentic-memory-stack-for-claude-code\scripts\windows\tests\ -Output Detailed"

BeforeAll {
    $winDir = Split-Path -Parent $PSScriptRoot
    $libPath = Join-Path $winDir 'user-prompt-lib.ps1'
    . $libPath   # Get-FileSha256Hex, Test-DaemonPipePresent

    $script:buildScript = Join-Path $winDir 'build-hook-client.ps1'
    $script:extractPath = Join-Path $winDir 'user-prompt-extract.ps1'

    # Build the REAL exe (real csc, real smoke gate) into an isolated bin dir.
    $script:binDir = Join-Path $TestDrive 'bin'
    New-Item -ItemType Directory -Path $script:binDir -Force | Out-Null
    $script:exePath = Join-Path $script:binDir 'mem0-hook-client.exe'
    & $script:buildScript -NoDeployCs -OutExe $script:exePath *> $null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $script:exePath)) { throw "build-hook-client.ps1 failed to produce $script:exePath" }

    # Fixture lib + daemon-script beside the exe: v0.21 Phase B (M3/M6) the exe
    # hashes BOTH <exeDir>\user-prompt-lib.ps1 AND <exeDir>\mem0-hook-daemon.ps1
    # and folds them into ONE combined handshake digest
    # (SHA256(Sha256Hex(lib)+Sha256Hex(daemon))). Fake daemons answer with THAT
    # combined digest so the staleness check passes.
    $script:fixtureLib = Join-Path $script:binDir 'user-prompt-lib.ps1'
    Set-Content -Path $script:fixtureLib -Value '# fixture lib vA for hook-client tests' -NoNewline
    # Stub daemon script: the exe's detached respawn on no-pipe must not start
    # anything resident during tests; ALSO an input to the combined handshake digest.
    $script:fixtureDaemon = Join-Path $script:binDir 'mem0-hook-daemon.ps1'
    Set-Content -Path $script:fixtureDaemon -Value 'exit 0' -NoNewline
    $libH = Get-FileSha256Hex -Path $script:fixtureLib
    $daeH = Get-FileSha256Hex -Path $script:fixtureDaemon
    $script:fixtureHash = Get-StringSha256Hex ($libH + $daeH)

    # Recording stub user-prompt-extract.ps1 beside the exe: records switch +
    # env + verbatim stdin to $env:HOOKCLIENT_RECORD, prints a marker.
    $stub = @'
param([switch]$SkipDaemon)
$raw = [Console]::In.ReadToEnd()
$rec = @{
    skip_daemon       = [bool]$SkipDaemon
    daemon_served_env = "$env:MEM0_HOOK_DAEMON_SERVED"
    stdin             = $raw
} | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText($env:HOOKCLIENT_RECORD, $rec)
[Console]::Out.WriteLine('STUB-FALLBACK-RAN')
exit 0
'@
    Set-Content -Path (Join-Path $script:binDir 'user-prompt-extract.ps1') -Value $stub

    function script:B64([string]$s) { [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($s)) }

    function script:New-TestPipeName { 'a6test-' + [guid]::NewGuid().ToString('N') }

    # In-process fake daemon (same pattern as HookDaemon.Tests.ps1): one server
    # stream on a unique pipe, serves ONE connection with a scripted response.
    # v0.21 Phase B (M7): -SecondRequestRecordPath proves the exe delivers the
    # {op:shutdown} signal on a hash mismatch — after the scripted response +
    # disconnect, open a SECOND server stream on the same name, accept one more
    # connection (2s guard), read one line, and write it to the record path.
    function script:Start-FakeDaemon {
        param([string]$PipeName, [string]$ResponseLine, [string]$SecondRequestRecordPath)
        $ps = [powershell]::Create()
        [void]$ps.AddScript({
            param($PipeName, $ResponseLine, $SecondRequestRecordPath)
            $server = [System.IO.Pipes.NamedPipeServerStream]::new($PipeName, [System.IO.Pipes.PipeDirection]::InOut, 1, [System.IO.Pipes.PipeTransmissionMode]::Byte, [System.IO.Pipes.PipeOptions]::None)
            try {
                $server.WaitForConnection()
                $buf = [byte[]]::new(65536)
                [void]$server.Read($buf, 0, $buf.Length)
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($ResponseLine + "`n")
                $server.Write($bytes, 0, $bytes.Length)
                $server.Flush()
                try { $server.WaitForPipeDrain() } catch {}
                try { $server.Disconnect() } catch {}
            } catch {} finally { $server.Dispose() }

            if ($SecondRequestRecordPath) {
                # Second connection: capture the shutdown request line the exe
                # sends after a hash mismatch.
                $srv2 = [System.IO.Pipes.NamedPipeServerStream]::new($PipeName, [System.IO.Pipes.PipeDirection]::InOut, 1, [System.IO.Pipes.PipeTransmissionMode]::Byte, [System.IO.Pipes.PipeOptions]::Asynchronous)
                try {
                    $waitTask = $srv2.WaitForConnectionAsync()
                    if ($waitTask.Wait(2000)) {
                        $buf2 = [byte[]]::new(65536)
                        $n = $srv2.Read($buf2, 0, $buf2.Length)
                        $reqLine = [System.Text.Encoding]::UTF8.GetString($buf2, 0, $n)
                        [System.IO.File]::WriteAllText($SecondRequestRecordPath, $reqLine)
                        # ack so the exe's SendShutdown read completes cleanly
                        try { $ack = [System.Text.Encoding]::UTF8.GetBytes('{"ok":true,"op":"shutdown"}' + "`n"); $srv2.Write($ack, 0, $ack.Length); $srv2.Flush() } catch {}
                        try { $srv2.Disconnect() } catch {}
                    }
                } catch {} finally { $srv2.Dispose() }
            }
        }).AddArgument($PipeName).AddArgument($ResponseLine).AddArgument($SecondRequestRecordPath)
        $handle = $ps.BeginInvoke()
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

    # Run the exe exactly as Claude Code would: stdin piped, stdout captured.
    function script:Invoke-HookClient {
        param([string]$Stdin, [string]$PipeName, [string]$RecordPath)
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $script:exePath
        $psi.UseShellExecute = $false
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.EnvironmentVariables['MEM0_HOOK_PIPE'] = $PipeName
        $psi.EnvironmentVariables['HOOKCLIENT_RECORD'] = $RecordPath
        $p = [System.Diagnostics.Process]::Start($psi)
        $inBytes = [System.Text.Encoding]::UTF8.GetBytes($Stdin)
        $p.StandardInput.BaseStream.Write($inBytes, 0, $inBytes.Length)
        $p.StandardInput.Close()
        $out = $p.StandardOutput.ReadToEnd()
        $err = $p.StandardError.ReadToEnd()
        $p.WaitForExit()
        return [pscustomobject]@{ ExitCode = $p.ExitCode; StdOut = $out; StdErr = $err }
    }

    function script:Get-Record {
        param([string]$RecordPath)
        if (-not (Test-Path $RecordPath)) { return $null }
        return (Get-Content $RecordPath -Raw | ConvertFrom-Json)
    }
}

Describe 'build-hook-client.ps1 (compile + smoke gate)' {

    It 'builds a working exe end-to-end (-NoDeployCs -OutExe)' {
        # BeforeAll already ran the real build; this locks the contract.
        Test-Path $script:exePath | Should -BeTrue
        (Get-Item $script:exePath).Length | Should -BeGreaterThan 4kb
    }

    It 'smoke gate PASSES the freshly built exe (-SmokeOnly exits 0)' {
        & $script:buildScript -SmokeOnly $script:exePath *> $null
        $LASTEXITCODE | Should -Be 0
    }

    It 'smoke gate REFUSES a corrupted exe (-SmokeOnly exits 1) so it can never be registered' {
        $corrupt = Join-Path $TestDrive 'corrupted-client.exe'
        [System.IO.File]::WriteAllBytes($corrupt, [byte[]](1..512 | ForEach-Object { Get-Random -Maximum 256 }))
        & $script:buildScript -SmokeOnly $corrupt *> $null
        $LASTEXITCODE | Should -Be 1
    }
}

Describe 'mem0-hook-client.exe fail-open matrix (real exe, scripted daemons, recording stub)' {

    BeforeEach {
        $script:recordPath = Join-Path $TestDrive ("record-{0}.json" -f ([guid]::NewGuid().ToString('N')))
        $script:payload = '{"hook_event_name":"PesterProbe","prompt":"x y z","transcript_path":"C:\\nope\\t.jsonl"}'
    }

    It 'no pipe -> PS fallback invoked with -SkipDaemon and VERBATIM stdin, exit 0' {
        $r = Invoke-HookClient -Stdin $script:payload -PipeName (New-TestPipeName) -RecordPath $script:recordPath
        $r.ExitCode | Should -Be 0
        $r.StdOut | Should -Match 'STUB-FALLBACK-RAN'
        $rec = Get-Record $script:recordPath
        $rec.skip_daemon | Should -BeTrue
        $rec.daemon_served_env | Should -BeNullOrEmpty
        $rec.stdin | Should -Be $script:payload
    }

    It 'warm daemon + matching lib hash -> block on stdout, NO PowerShell spawn, exit 0' {
        $name = New-TestPipeName
        $block = "[MEMORY CONTEXT - exe-pester]`nline two"
        $line = '{"ok":true,"served":true,"lib_hash":"' + $script:fixtureHash + '","sid_b64":"' + (B64 'sid-1') +
                '","context_b64":"' + (B64 $block) + '","prompt_b64":"' + (B64 'what is the plan today') +
                '","tpath_b64":"","brand_b64":"","diag_b64":"' + (B64 'episode_id=1 daemon_ms=5') + '"}'
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine $line
        try {
            $r = Invoke-HookClient -Stdin $script:payload -PipeName $name -RecordPath $script:recordPath
            $r.ExitCode | Should -Be 0
            # v-fix 2026-06-30: the exe now wraps the block in the Claude Code
            # UserPromptSubmit additionalContext JSON envelope (plain stdout is
            # dropped by the SDK runtime). Parse it and assert the block round-trips.
            $envelope = ($r.StdOut.TrimEnd("`r","`n") | ConvertFrom-Json)
            $envelope.hookSpecificOutput.hookEventName | Should -Be 'UserPromptSubmit'
            $envelope.hookSpecificOutput.additionalContext | Should -Be $block
            Test-Path $script:recordPath | Should -BeFalse   # no fallback, no 0.B spawn
        } finally { Stop-FakeDaemon $fake }
    }

    It 'garbage daemon response -> inline fallback (-SkipDaemon), exit 0' {
        $name = New-TestPipeName
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine 'this is not json {{{'
        try {
            $r = Invoke-HookClient -Stdin $script:payload -PipeName $name -RecordPath $script:recordPath
            $r.ExitCode | Should -Be 0
            $r.StdOut | Should -Match 'STUB-FALLBACK-RAN'
            (Get-Record $script:recordPath).skip_daemon | Should -BeTrue
        } finally { Stop-FakeDaemon $fake }
    }

    It 'lib-hash mismatch -> inline fallback (-SkipDaemon), exit 0 (stale daemon never serves a deploy)' {
        $name = New-TestPipeName
        $line = '{"ok":true,"served":true,"lib_hash":"' + ('b' * 64) + '","sid_b64":"","context_b64":"' + (B64 'stale block') + '","prompt_b64":"","tpath_b64":"","brand_b64":"","diag_b64":""}'
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine $line
        try {
            $r = Invoke-HookClient -Stdin $script:payload -PipeName $name -RecordPath $script:recordPath
            $r.ExitCode | Should -Be 0
            $r.StdOut | Should -Not -Match 'stale block'
            $r.StdOut | Should -Match 'STUB-FALLBACK-RAN'
            (Get-Record $script:recordPath).skip_daemon | Should -BeTrue
        } finally { Stop-FakeDaemon $fake }
    }

    It 'lib-hash mismatch -> exe delivers an {op:shutdown} on a second connection (M7 shutdown-signal delivery)' {
        $name = New-TestPipeName
        $shutdownRec = Join-Path $TestDrive ("shutdown-{0}.txt" -f ([guid]::NewGuid().ToString('N')))
        $line = '{"ok":true,"served":true,"lib_hash":"' + ('b' * 64) + '","sid_b64":"","context_b64":"","prompt_b64":"","tpath_b64":"","brand_b64":"","diag_b64":""}'
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine $line -SecondRequestRecordPath $shutdownRec
        try {
            $r = Invoke-HookClient -Stdin $script:payload -PipeName $name -RecordPath $script:recordPath
            $r.ExitCode | Should -Be 0
            # poll briefly for the runspace thread to record the second request
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            while ($sw.ElapsedMilliseconds -lt 3000 -and -not (Test-Path $shutdownRec)) { Start-Sleep -Milliseconds 50 }
            Test-Path $shutdownRec | Should -BeTrue
            (Get-Content $shutdownRec -Raw) | Should -Match '"op"\s*:\s*"shutdown"'
        } finally { Stop-FakeDaemon $fake }
    }

    It 'daemon-served needs_0b:true + existing transcript -> 0.B spawn with MEM0_HOOK_DAEMON_SERVED=1 (no -SkipDaemon), block still emitted' {
        # v0.21 Phase B (M4): the 0.B verdict is now the DAEMON-computed needs_0b
        # field; the exe no longer evaluates the prompt itself (the C# duplicate
        # of Test-DecisionLikePrompt was deleted).
        $name = New-TestPipeName
        $transcript = Join-Path $TestDrive 'real-transcript.jsonl'
        Set-Content -Path $transcript -Value '{"type":"assistant"}'
        $block = '[MEMORY CONTEXT - decision-path]'
        $line = '{"ok":true,"served":true,"needs_0b":true,"lib_hash":"' + $script:fixtureHash + '","sid_b64":"' + (B64 'sid-2') +
                '","context_b64":"' + (B64 $block) + '","prompt_b64":"' + (B64 '1 and 2') +
                '","tpath_b64":"' + (B64 $transcript) + '","brand_b64":"","diag_b64":""}'
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine $line
        try {
            $r = Invoke-HookClient -Stdin $script:payload -PipeName $name -RecordPath $script:recordPath
            $r.ExitCode | Should -Be 0
            $r.StdOut | Should -Match '\[MEMORY CONTEXT - decision-path\]'
            $r.StdOut | Should -Match 'STUB-FALLBACK-RAN'
            $rec = Get-Record $script:recordPath
            $rec.skip_daemon | Should -BeFalse
            $rec.daemon_served_env | Should -Be '1'
            $rec.stdin | Should -Be $script:payload
        } finally { Stop-FakeDaemon $fake }
    }

    It 'daemon-served needs_0b:false (or absent) with existing transcript -> no 0.B spawn (verdict is daemon-side now)' {
        $name = New-TestPipeName
        $transcript = Join-Path $TestDrive 'real-transcript-2.jsonl'
        Set-Content -Path $transcript -Value '{"type":"assistant"}'
        # needs_0b omitted entirely (defaults false in the client) even though
        # the prompt text "1 and 2" WOULD match the old C# gate — proving the
        # client no longer self-evaluates the prompt.
        $line = '{"ok":true,"served":true,"lib_hash":"' + $script:fixtureHash + '","sid_b64":"","context_b64":"","prompt_b64":"' + (B64 '1 and 2') + '","tpath_b64":"' + (B64 $transcript) + '","brand_b64":"","diag_b64":""}'
        $fake = Start-FakeDaemon -PipeName $name -ResponseLine $line
        try {
            $r = Invoke-HookClient -Stdin $script:payload -PipeName $name -RecordPath $script:recordPath
            $r.ExitCode | Should -Be 0
            $r.StdOut | Should -BeNullOrEmpty
            Test-Path $script:recordPath | Should -BeFalse
        } finally { Stop-FakeDaemon $fake }
    }

    It 'empty stdin -> exit 0, no output, no fallback spawn' {
        $r = Invoke-HookClient -Stdin '' -PipeName (New-TestPipeName) -RecordPath $script:recordPath
        $r.ExitCode | Should -Be 0
        $r.StdOut | Should -BeNullOrEmpty
        Test-Path $script:recordPath | Should -BeFalse
    }
}

Describe 'mem0-hook-client.exe child-exit-code mapping (M7: code==2?0:code)' {
    # A dedicated bin dir whose stub user-prompt-extract.ps1 prints a marker and
    # exits with a chosen code; the exe is invoked with an ABSENT pipe so it
    # ALWAYS takes the fallback relay. exit 2 -> the exe maps to 0 (a hook exit 2
    # would BLOCK + erase the user's prompt); any other code is relayed verbatim.

    BeforeEach {
        $script:m7bin = Join-Path $TestDrive ("m7bin-{0}" -f ([guid]::NewGuid().ToString('N')))
        New-Item -ItemType Directory -Path $script:m7bin -Force | Out-Null
        $script:m7exe = Join-Path $script:m7bin 'mem0-hook-client.exe'
        & $script:buildScript -NoDeployCs -OutExe $script:m7exe *> $null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $script:m7exe)) { throw "M7 build failed for $script:m7exe" }
        # daemon stub absent-pipe respawn must not start anything
        Set-Content -Path (Join-Path $script:m7bin 'mem0-hook-daemon.ps1') -Value 'exit 0' -NoNewline
    }

    function script:Set-ExitStub {
        param([int]$Code)
        $stub = "param([switch]`$SkipDaemon)`n[void][Console]::In.ReadToEnd()`n[Console]::Out.WriteLine('M7-STUB-MARKER')`nexit $Code"
        Set-Content -Path (Join-Path $script:m7bin 'user-prompt-extract.ps1') -Value $stub
    }

    function script:Invoke-M7Exe {
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $script:m7exe
        $psi.UseShellExecute = $false
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.EnvironmentVariables['MEM0_HOOK_PIPE'] = (New-TestPipeName)   # absent -> fallback
        $p = [System.Diagnostics.Process]::Start($psi)
        $p.StandardInput.Write('{"hook_event_name":"PesterProbe","prompt":"x y z"}')
        $p.StandardInput.Close()
        $out = $p.StandardOutput.ReadToEnd()
        $null = $p.StandardError.ReadToEnd()
        $p.WaitForExit()
        return [pscustomobject]@{ ExitCode = $p.ExitCode; StdOut = $out }
    }

    It 'fallback child exit 2 -> exe exit 0 (a hook exit 2 would block the prompt) + marker relayed' {
        Set-ExitStub -Code 2
        $r = Invoke-M7Exe
        $r.ExitCode | Should -Be 0
        $r.StdOut | Should -Match 'M7-STUB-MARKER'
    }

    It 'fallback child exit 7 -> exe exit 7 (non-2 codes relayed verbatim)' {
        Set-ExitStub -Code 7
        $r = Invoke-M7Exe
        $r.ExitCode | Should -Be 7
        $r.StdOut | Should -Match 'M7-STUB-MARKER'
    }
}

Describe 'build-hook-client.ps1 .sha256 sidecar (v0.21 Phase B L3)' {

    It 'writes a sidecar whose hash matches the installed exe; tamper -> mismatch' {
        $sidecar = $script:exePath + '.sha256'
        Test-Path $sidecar | Should -BeTrue
        $recorded = (Get-Content $sidecar -Raw).Trim()
        $recorded | Should -Match '^[0-9A-Fa-f]{64}$'
        (Get-FileHash $script:exePath -Algorithm SHA256).Hash | Should -Be $recorded
        # Post-build tamper: append a byte to the installed exe -> sidecar no
        # longer matches (R9 CONTENT-DRIFT branch). Restore afterwards so the
        # shared exe stays valid for later tests.
        $orig = [System.IO.File]::ReadAllBytes($script:exePath)
        try {
            $tampered = $orig + [byte]0
            [System.IO.File]::WriteAllBytes($script:exePath, $tampered)
            (Get-FileHash $script:exePath -Algorithm SHA256).Hash | Should -Not -Be $recorded
        } finally {
            [System.IO.File]::WriteAllBytes($script:exePath, $orig)
        }
    }
}

Describe 'user-prompt-extract.ps1 A.6 mechanisms (-SkipDaemon / 0.B-only mode)' {

    It 'declares the -SkipDaemon switch parameter' {
        $tokens = $null; $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($script:extractPath, [ref]$tokens, [ref]$errors)
        $errors | Should -BeNullOrEmpty
        $ast.ParamBlock | Should -Not -BeNullOrEmpty
        @($ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }) | Should -Contain 'SkipDaemon'
    }

    It 'gates the daemon fast path on -not $SkipDaemon (the exe already paid probe/connect)' {
        $src = Get-Content $script:extractPath -Raw
        $src | Should -Match '-not \$SkipDaemon.*Invoke-DaemonRawTransaction|Invoke-DaemonRawTransaction.*-not \$SkipDaemon'
    }

    It 'handles MEM0_HOOK_DAEMON_SERVED=1 (0.B-only mode after an exe daemon success)' {
        $src = Get-Content $script:extractPath -Raw
        $src | Should -Match 'MEM0_HOOK_DAEMON_SERVED'
    }

    It '-SkipDaemon with unparseable stdin: exits 0 quickly with no output (live powershell.exe run, no HTTP)' {
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$script:extractPath`" -SkipDaemon"
        $psi.UseShellExecute = $false
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $p.StandardInput.Write('garbage {{ not json')
        $p.StandardInput.Close()
        $out = $p.StandardOutput.ReadToEnd()
        $null = $p.StandardError.ReadToEnd()
        $p.WaitForExit()
        $p.ExitCode | Should -Be 0
        $out | Should -BeNullOrEmpty
    }
}

Describe 'Test-MemoryStack R9 covers the compiled client (v0.20 A.6)' {

    It 'hashes mem0-hook-client.cs and checks exe presence + freshness + sidecar content (L3)' {
        $src = Get-Content (Join-Path (Split-Path -Parent $PSScriptRoot) 'Test-MemoryStack.ps1') -Raw
        $src | Should -Match "mem0-hook-client\.cs"
        $src | Should -Match "mem0-hook-client\.exe\(MISSING"
        $src | Should -Match "mem0-hook-client\.exe\(STALE"
        $src | Should -Match "mem0-hook-client\.exe\(CONTENT-DRIFT"
        $src | Should -Match "no \.sha256 sidecar"
    }
}
