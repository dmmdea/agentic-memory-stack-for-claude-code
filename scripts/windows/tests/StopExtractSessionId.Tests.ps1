# The Stop / PreCompact spawner hands the L1a worker the hook's real session id.
#
# PreCompact copies the transcript to <temp>\precompact-snap-<PID>.jsonl before compaction mutates it
# and dispatched the worker at that copy. The worker derived the episode's session id from the file
# NAME, so every compaction posted an episode under a phantom session 'precompact-snap-<PID>' (22 in
# the production episodic DB, with no workspace or brand) instead of the real one. The spawner now
# passes the hook's session_id (and the original transcript path) alongside the snapshot.
#
# The real stop-extract.ps1 runs under Windows PowerShell 5.1 (its production runtime) in a sandboxed
# profile and temp dir; the worker is a stub that records the arguments it was started with.

BeforeAll {
    $script:winDir = Split-Path -Parent $PSScriptRoot
    $script:sid = [guid]::NewGuid().ToString()

    function script:Invoke-Spawner {
        param([string]$Stdin, [string]$Sandbox)
        $dir = Join-Path $Sandbox 'scripts'
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Copy-Item (Join-Path $script:winDir 'stop-extract.ps1') $dir
        Copy-Item (Join-Path $script:winDir 'user-prompt-lib.ps1') $dir
        # the worker stub: record every argument it was started with, one per line
        Set-Content -Path (Join-Path $dir 'l1a-extract.ps1') -NoNewline -Value 'Set-Content -LiteralPath $env:L1A_ARGS_OUT -Value ($args -join "`n") -Encoding UTF8'
        $out = Join-Path $Sandbox 'worker-args.txt'
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $dir 'stop-extract.ps1') + '"'
        $psi.UseShellExecute = $false
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.EnvironmentVariables['USERPROFILE'] = $Sandbox
        $psi.EnvironmentVariables['TEMP'] = $Sandbox
        $psi.EnvironmentVariables['TMP'] = $Sandbox
        $psi.EnvironmentVariables['MEM0_HOOK_PIPE'] = 'session-id-test-no-such-pipe'
        $psi.EnvironmentVariables['L1A_ARGS_OUT'] = $out
        $p = [System.Diagnostics.Process]::Start($psi)
        $p.StandardInput.Write($Stdin)
        $p.StandardInput.Close()
        $null = $p.StandardOutput.ReadToEnd()
        $null = $p.StandardError.ReadToEnd()
        if (-not $p.WaitForExit(60000)) { try { $p.Kill() } catch {}; throw 'stop-extract.ps1 did not exit' }
        $p.ExitCode | Should -Be 0
        # the worker is started detached: wait for its record
        $deadline = [DateTime]::UtcNow.AddSeconds(45)
        while (-not (Test-Path -LiteralPath $out) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
        Test-Path -LiteralPath $out | Should -BeTrue -Because 'the spawner must start the worker'
        Start-Sleep -Milliseconds 300
        return @(Get-Content -LiteralPath $out | Where-Object { $_ -ne '' })
    }

    function script:New-Sandbox {
        $sb = Join-Path $TestDrive ("sb-{0}" -f ([guid]::NewGuid().ToString('N')))
        New-Item -ItemType Directory -Path $sb -Force | Out-Null
        $tp = Join-Path $sb "$($script:sid).jsonl"
        Set-Content -Path $tp -Value '{"message":{"role":"user","content":"hello"}}' -Encoding UTF8
        return @{ Dir = $sb; Transcript = $tp }
    }

    function script:ArgAfter([string[]]$WorkerArgs, [string]$Name) {
        $i = [array]::IndexOf($WorkerArgs, $Name)
        if ($i -lt 0 -or $i + 1 -ge $WorkerArgs.Count) { return $null }
        return $WorkerArgs[$i + 1]
    }
}

Describe 'stop-extract.ps1 passes the real session id to the L1a worker' {

    It 'PreCompact: the worker gets the hook session id, the snapshot, and the ORIGINAL transcript path' {
        $s = New-Sandbox
        $stdin = ([ordered]@{ session_id = $script:sid; transcript_path = $s.Transcript; hook_event_name = 'PreCompact'; trigger = 'auto' } | ConvertTo-Json -Compress)
        $a = Invoke-Spawner -Stdin $stdin -Sandbox $s.Dir
        (ArgAfter $a '-SessionId') | Should -Be $script:sid
        (ArgAfter $a '-OriginTranscriptPath') | Should -Be $s.Transcript
        (ArgAfter $a '-EventName') | Should -Be 'PreCompact'
        $snap = ArgAfter $a '-TranscriptPath'
        $snap | Should -Match 'precompact-snap-\d+\.jsonl$' -Because 'compaction still needs the pre-compaction snapshot to extract from'
        $snap | Should -Not -Be $s.Transcript
    }

    It 'Stop: the worker gets the hook session id and no origin path (the transcript is not swapped)' {
        $s = New-Sandbox
        $stdin = ([ordered]@{ session_id = $script:sid; transcript_path = $s.Transcript; hook_event_name = 'Stop' } | ConvertTo-Json -Compress)
        $a = Invoke-Spawner -Stdin $stdin -Sandbox $s.Dir
        (ArgAfter $a '-SessionId') | Should -Be $script:sid
        (ArgAfter $a '-TranscriptPath') | Should -Be $s.Transcript
        $a | Should -Not -Contain '-OriginTranscriptPath'
    }

    It 'no session_id in the hook payload: no -SessionId is passed (the worker falls back to the file name)' {
        $s = New-Sandbox
        $stdin = ([ordered]@{ transcript_path = $s.Transcript; hook_event_name = 'Stop' } | ConvertTo-Json -Compress)
        $a = Invoke-Spawner -Stdin $stdin -Sandbox $s.Dir
        $a | Should -Not -Contain '-SessionId'
    }

    It 'a session_id that is not a plain token is dropped rather than put on a command line' {
        $s = New-Sandbox
        $stdin = ([ordered]@{ session_id = 'a b"; calc'; transcript_path = $s.Transcript; hook_event_name = 'Stop' } | ConvertTo-Json -Compress)
        $a = Invoke-Spawner -Stdin $stdin -Sandbox $s.Dir
        $a | Should -Not -Contain '-SessionId'
        ($a -join ' ') | Should -Not -Match 'calc'
    }
}

Describe 'l1a-extract.ps1 keys the episode on the session the spawner passed' {
    # The worker's episode POST needs codex and the authority, so its wiring is pinned structurally:
    # the parameters exist, the session comes from Resolve-L1aSessionId (unit-tested in
    # MemoryCommon.Tests.ps1), and the file-name derivation that produced phantom sessions is gone.
    BeforeAll {
        $script:l1a = Get-Content -Raw (Join-Path (Split-Path -Parent $PSScriptRoot) 'l1a-extract.ps1')
    }
    It 'declares -SessionId and -OriginTranscriptPath' {
        $script:l1a | Should -Match '\[string\]\$SessionId\s*='
        $script:l1a | Should -Match '\[string\]\$OriginTranscriptPath\s*='
    }
    It 'posts the resolved session id, not one derived from the snapshot file name' {
        $script:l1a | Should -Match 'session_id\s*=\s*\$episodeSessionId'
        $script:l1a | Should -Match 'Resolve-L1aSessionId\s+-SessionId\s+\$SessionId\s+-TranscriptPath\s+\$TranscriptPath'
        $script:l1a | Should -Not -Match "GetFileNameWithoutExtension\(\`$TranscriptPath\)"
    }
    It 'reads brand and workspace from the original transcript when a snapshot was analysed' {
        $script:l1a | Should -Match 'Get-BrandFromTranscriptPath\s+-Path\s+\$sourceTranscript'
    }
}
