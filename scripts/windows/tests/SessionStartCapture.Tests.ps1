#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# SessionStartCapture.Tests.ps1 - the real sessionstart-capture.ps1 run under Windows PowerShell 5.1
# in a sandbox (a sandboxed USERPROFILE, a stub l1a-extract.ps1 that only records that it was
# spawned). 19% of SessionStart spawns were exact same-second duplicates that raced each other
# for the codex lock; a per-session marker now lets exactly one of them through.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester <repo>\scripts\windows\tests\SessionStartCapture.Tests.ps1 -Output Detailed"

BeforeAll {
    $script:winDir = Split-Path -Parent $PSScriptRoot
    $script:ps51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

    function script:New-CaptureSandbox {
        $root = Join-Path $TestDrive ('ss-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        $home_ = Join-Path $root 'home'
        $bin = Join-Path $root 'bin'
        $proj = Join-Path $home_ '.claude\projects\g--My-Drive-Elsewhere'
        foreach ($d in @($bin, $proj, (Join-Path $home_ '.claude\state'), (Join-Path $home_ '.mem0'))) { [System.IO.Directory]::CreateDirectory($d) | Out-Null }
        Copy-Item (Join-Path $script:winDir 'sessionstart-capture.ps1') $bin
        Set-Content -LiteralPath (Join-Path $bin 'l1a-extract.ps1') -Encoding ASCII -Value @(
            'param([string]$TranscriptPath = "", [string]$EventName = "")'
            'Add-Content -LiteralPath (Join-Path $env:USERPROFILE "spawns.log") -Value ($EventName + " " + $TranscriptPath)'
        )
        # the embedder pre-warm must never reach a real authority
        Set-Content -LiteralPath (Join-Path $home_ '.mem0\authority-url') -Value 'http://127.0.0.1:1' -Encoding ASCII -NoNewline
        $prior = Join-Path $proj ([guid]::NewGuid().ToString() + '.jsonl')
        Set-Content -LiteralPath $prior -Value '{"message":{"role":"user","content":"hello"}}' -Encoding UTF8
        return @{ Root = $root; Home = $home_; Bin = $bin; Prior = $prior; Spawns = (Join-Path $home_ 'spawns.log'); State = (Join-Path $home_ '.claude\state') }
    }

    function script:Start-Capture($Sb, [string]$SessionId) {
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $script:ps51
        $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $Sb.Bin 'sessionstart-capture.ps1') + '"'
        $psi.UseShellExecute = $false
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.EnvironmentVariables['USERPROFILE'] = $Sb.Home
        $psi.EnvironmentVariables.Remove('MEM0_URL')
        $p = [System.Diagnostics.Process]::Start($psi)
        $cur = Join-Path $Sb.Home ('.claude\projects\g--My-Drive-Elsewhere\' + $SessionId + '.jsonl')   # the CURRENT session: excluded
        $p.StandardInput.Write((@{ session_id = $SessionId; transcript_path = $cur; hook_event_name = 'SessionStart'; source = 'startup' } | ConvertTo-Json -Compress))
        $p.StandardInput.Close()
        return $p
    }

    function script:Wait-Capture($Procs) {
        foreach ($p in $Procs) { if (-not $p.WaitForExit(60000)) { try { $p.Kill() } catch {}; throw 'sessionstart-capture.ps1 did not exit' } }
    }

    # spawned workers are detached: poll for the record, then give a would-be duplicate time to show up.
    # A read can race the worker's own append, so an unreadable file counts as "not yet".
    function script:Read-SpawnLines($Sb) {
        try { return @(if (Test-Path $Sb.Spawns) { Get-Content $Sb.Spawns -ErrorAction Stop | Where-Object { $_ } }) } catch { return @() }
    }
    function script:Get-SpawnLines($Sb, [int]$MinLines, [int]$SettleSeconds = 5) {
        $deadline = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $deadline) {
            if (@(Read-SpawnLines $Sb).Count -ge $MinLines) { break }
            Start-Sleep -Milliseconds 300
        }
        Start-Sleep -Seconds $SettleSeconds
        for ($i = 0; $i -lt 5; $i++) {
            try { return @(if (Test-Path $Sb.Spawns) { Get-Content $Sb.Spawns -ErrorAction Stop | Where-Object { $_ } }) } catch { Start-Sleep -Milliseconds 300 }
        }
        return @()
    }

    $script:haveFive1 = Test-Path -LiteralPath $script:ps51
}

Describe 'SessionStart spawn de-duplication (capture-l1a-codex-lock-skips-and-failures)' {
    BeforeAll { if (-not $script:haveFive1) { Set-ItResult -Skipped -Because 'Windows PowerShell 5.1 is not present' } }

    It 'a lone SessionStart spawns the extractor once and leaves the per-session marker' {
        $sb = New-CaptureSandbox
        $sid = [guid]::NewGuid().ToString()
        Wait-Capture @(Start-Capture $sb $sid)
        $lines = @(Get-SpawnLines $sb 1 2)
        $lines.Count | Should -Be 1
        $lines[0] | Should -Match '^SessionStart '
        Test-Path (Join-Path $sb.State "sessionstart-spawn-$sid") | Should -BeTrue
    }

    It 'a second start of the same session inside the same second is dropped (fresh marker: no spawn)' {
        $sb = New-CaptureSandbox
        $sid = [guid]::NewGuid().ToString()
        Set-Content -LiteralPath (Join-Path $sb.State "sessionstart-spawn-$sid") -Value '1' -Encoding ASCII   # what the first start left, just now
        Wait-Capture @(Start-Capture $sb $sid)
        @(Get-SpawnLines $sb 1 5).Count | Should -Be 0
    }

    It 'a marker older than the window is stale: the same session may spawn again (a real resume)' {
        $sb = New-CaptureSandbox
        $sid = [guid]::NewGuid().ToString()
        $m = Join-Path $sb.State "sessionstart-spawn-$sid"
        Set-Content -LiteralPath $m -Value '1' -Encoding ASCII
        (Get-Item $m).LastWriteTime = (Get-Date).AddMinutes(-5)
        Wait-Capture @(Start-Capture $sb $sid)
        @(Get-SpawnLines $sb 1 2).Count | Should -Be 1
        ((Get-Date) - (Get-Item $m).LastWriteTime).TotalSeconds | Should -BeLessThan 60 -Because 'the marker is refreshed'
    }

    It 'two SessionStart hooks for one session launched together spawn the extractor exactly once' {
        $sb = New-CaptureSandbox
        $sid = [guid]::NewGuid().ToString()
        $a = Start-Capture $sb $sid
        $b = Start-Capture $sb $sid
        Wait-Capture @($a, $b)
        @(Get-SpawnLines $sb 1 6).Count | Should -Be 1
    }

    It 'a different session is not held back by another session''s marker' {
        $sb = New-CaptureSandbox
        Set-Content -LiteralPath (Join-Path $sb.State ('sessionstart-spawn-' + [guid]::NewGuid().ToString())) -Value '1' -Encoding ASCII
        Wait-Capture @(Start-Capture $sb ([guid]::NewGuid().ToString()))
        @(Get-SpawnLines $sb 1 2).Count | Should -Be 1
    }
}
