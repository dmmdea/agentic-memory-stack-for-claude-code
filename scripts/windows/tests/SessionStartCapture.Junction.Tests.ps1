#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# SessionStartCapture.Junction.Tests.ps1 - the SessionStart capture must not capture one transcript twice
# because a directory junction (or symlink) under ~/.claude/projects lists it under a second path.
#
# The hook globs projects\*\*.jsonl, and a glob descends into an alias directory: every transcript
# behind it is listed a second time, at the same mtime, under a different FullName (Windows PowerShell
# 5.1 and pwsh 7 both do this). Sort-Object does not order the tie, and the watermark used to be
# <FullName>|<mtime ticks>, so whenever the winning path flipped, the same transcript at the same mtime
# looked new and was captured again: a codex extraction plus a POST /v1/episodes, which the server
# does not dedupe.
#
# Every test builds its own temp tree with real directory junctions (New-Item -ItemType Junction needs
# no elevation), runs the real sessionstart-capture.ps1 under Windows PowerShell 5.1 against a sandboxed
# USERPROFILE, and reads what a stub extractor recorded. Only the links are deleted on cleanup, never
# through the link, so the target is never touched.
#
# Run: pwsh -NoProfile -File scripts\windows\Run-PesterTests.ps1 -Path scripts\windows\tests\SessionStartCapture.Junction.Tests.ps1 -Detailed

BeforeAll {
    $script:winDir = Split-Path -Parent $PSScriptRoot
    $script:ps51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

    # Why every test would be skipped (empty when the suite can run): the hook host, then a junction probe.
    $script:skipReason = ''
    if (-not (Test-Path -LiteralPath $script:ps51)) {
        $script:skipReason = 'Windows PowerShell 5.1 (powershell.exe) is not present'
    } else {
        $probe = Join-Path $TestDrive ('jp-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        $probeTarget = Join-Path $probe 'target'
        $probeLink = Join-Path $probe 'link'
        try {
            [System.IO.Directory]::CreateDirectory($probeTarget) | Out-Null
            New-Item -ItemType Junction -Path $probeLink -Target $probeTarget -ErrorAction Stop | Out-Null
            if (-not ((Get-Item -LiteralPath $probeLink -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                $script:skipReason = 'a junction was created here but it is not a reparse point on this volume'
            }
        } catch {
            $script:skipReason = 'a directory junction cannot be created here: ' + $_.Exception.Message
        } finally {
            try { [System.IO.Directory]::Delete($probeLink) } catch {}
        }
    }

    # ---- sandbox ------------------------------------------------------------------------------
    # projects\proj-real   a real directory
    # projects\a-alias     junction -> proj-real   (sorts BEFORE the real directory)
    # projects\z-alias     junction -> proj-real   (sorts AFTER it, so no tie order is lucky)
    function script:New-CaptureRoot {
        $root = Join-Path $TestDrive ('sj-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        $home_ = Join-Path $root 'home'
        $bin = Join-Path $root 'bin'
        $projects = Join-Path $home_ '.claude\projects'
        $state = Join-Path $home_ '.claude\state'
        foreach ($d in @($bin, $projects, $state, (Join-Path $home_ '.mem0'))) { [System.IO.Directory]::CreateDirectory($d) | Out-Null }
        Copy-Item -LiteralPath (Join-Path $script:winDir 'sessionstart-capture.ps1') -Destination $bin
        Set-Content -LiteralPath (Join-Path $bin 'l1a-extract.ps1') -Encoding ASCII -Value @(
            'param([string]$TranscriptPath = "", [string]$EventName = "")'
            '# one file per spawn: concurrent workers appending to one log lose lines and hide a duplicate'
            'Set-Content -LiteralPath (Join-Path $env:USERPROFILE ("spawn-" + [guid]::NewGuid().ToString("N") + ".txt")) -Value ($EventName + " " + $TranscriptPath)'
        )
        # the embedder pre-warm must never reach a real authority
        Set-Content -LiteralPath (Join-Path $home_ '.mem0\authority-url') -Value 'http://127.0.0.1:1' -Encoding ASCII -NoNewline
        return @{
            Root = $root; Home = $home_; Bin = $bin; Projects = $projects; State = $state
            Wm = (Join-Path $state 'last-sessionstart-capture')
            Links = (New-Object System.Collections.ArrayList)
        }
    }

    function script:Add-Dir($Sb, [string]$Path) {
        [System.IO.Directory]::CreateDirectory($Path) | Out-Null
        return $Path
    }

    # a junction at projects\<Name> pointing at $Target (recorded, so cleanup deletes exactly the links)
    function script:Add-Junction($Sb, [string]$Name, [string]$Target) {
        $link = Join-Path $Sb.Projects $Name
        New-Item -ItemType Junction -Path $link -Target $Target | Out-Null
        [void]$Sb.Links.Add($link)
        return $link
    }

    function script:Remove-Junction($Sb, [string]$Path) {
        try { [System.IO.Directory]::Delete($Path) } catch {}
        $Sb.Links.Remove($Path)
    }

    # Delete the LINKS only. [Directory]::Delete on a junction removes the reparse point and never the target.
    function script:Remove-CaptureRoot($Sb) {
        foreach ($l in @($Sb.Links)) { try { [System.IO.Directory]::Delete($l) } catch {} }
    }

    # a transcript <guid>.jsonl in $Dir whose mtime is $AgeMinutes ago; ticks are read back from disk
    function script:Add-Transcript([string]$Dir, [double]$AgeMinutes) {
        $id = [guid]::NewGuid().ToString()
        $path = Join-Path $Dir ($id + '.jsonl')
        Set-Content -LiteralPath $path -Value '{"message":{"role":"user","content":"hello"}}' -Encoding UTF8
        [System.IO.File]::SetLastWriteTimeUtc($path, [datetime]::UtcNow.AddMinutes(-$AgeMinutes))
        return @{ Id = $id; Name = ($id + '.jsonl'); Path = $path; Ticks = (Get-Item -LiteralPath $path).LastWriteTimeUtc.Ticks }
    }

    function script:Set-TranscriptAge([string]$Path, [double]$AgeMinutes) {
        [System.IO.File]::SetLastWriteTimeUtc($Path, [datetime]::UtcNow.AddMinutes(-$AgeMinutes))
        return (Get-Item -LiteralPath $Path).LastWriteTimeUtc.Ticks
    }

    # the standard scene: one real project dir, two junction aliases of it, four sessions (S0 newest)
    function script:New-JunctionSandbox {
        $sb = New-CaptureRoot
        $sb.Real = Add-Dir $sb (Join-Path $sb.Projects 'proj-real')
        $sb.AliasA = Add-Junction $sb 'a-alias' $sb.Real
        $sb.AliasZ = Add-Junction $sb 'z-alias' $sb.Real
        $sb.S = @(5, 15, 25, 35 | ForEach-Object { Add-Transcript $sb.Real $_ })
        return $sb
    }

    # ---- running the hook ------------------------------------------------------------------------
    # $SessionId / $TranscriptPath = the CURRENT session (excluded from the pick); default = a fresh one.
    function script:Invoke-Capture($Sb, [string]$SessionId = '', [string]$TranscriptPath = '') {
        if (-not $SessionId) { $SessionId = [guid]::NewGuid().ToString() }
        if (-not $TranscriptPath) { $TranscriptPath = Join-Path $Sb.Projects ('proj-real\' + $SessionId + '.jsonl') }
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
        $p.StandardInput.Write((@{ session_id = $SessionId; transcript_path = $TranscriptPath; hook_event_name = 'SessionStart'; source = 'startup' } | ConvertTo-Json -Compress))
        $p.StandardInput.Close()
        if (-not $p.WaitForExit(60000)) { try { $p.Kill() } catch {}; throw 'sessionstart-capture.ps1 did not exit' }
        $err = $p.StandardError.ReadToEnd()
        # the hook always exits 0 on purpose; anything else is a parse or launch failure, not a logic result
        if ($p.ExitCode -ne 0) { throw ('sessionstart-capture.ps1 exited ' + $p.ExitCode + ': ' + $err) }
    }

    # Spawned workers are detached. Wait (up to 30 s) for $MinLines records, then settle so a would-be
    # duplicate has time to show up; with $MinLines = 0 only the settle applies (nothing is expected).
    function script:Read-SpawnLines($Sb) {
        return @(Get-ChildItem -LiteralPath $Sb.Home -Filter 'spawn-*.txt' -File -ErrorAction SilentlyContinue | ForEach-Object {
            try { (Get-Content -LiteralPath $_.FullName -ErrorAction Stop | Select-Object -First 1) } catch { 'unreadable' }
        })
    }
    function script:Get-SpawnLines($Sb, [int]$MinLines, [int]$SettleSeconds = 2) {
        $deadline = (Get-Date).AddSeconds(30)
        while ($MinLines -gt 0 -and (Get-Date) -lt $deadline) {
            if (@(Read-SpawnLines $Sb).Count -ge $MinLines) { break }
            Start-Sleep -Milliseconds 250
        }
        Start-Sleep -Seconds $SettleSeconds
        return @(Read-SpawnLines $Sb)
    }

    # forget a previous run: the watermark, the per-session markers and the spawn records
    function script:Reset-CaptureState($Sb) {
        Get-ChildItem -LiteralPath $Sb.Home -Filter 'spawn-*.txt' -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath $Sb.State -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    }

    # ---- the watermark file --------------------------------------------------------------------------
    function script:Get-Watermark($Sb) {
        if (-not (Test-Path -LiteralPath $Sb.Wm)) { return $null }
        return ([string](Get-Content -LiteralPath $Sb.Wm -Raw)).Trim()
    }
    # written the way the live file is: UTF-8 with a BOM and a trailing newline (Windows PowerShell 5.1 Set-Content)
    function script:Set-Watermark($Sb, [string]$Text) {
        [System.IO.File]::WriteAllText($Sb.Wm, $Text + "`r`n", (New-Object System.Text.UTF8Encoding($true)))
    }
    # the hook only rewrites the watermark after it spawned, so an untouched file proves "no capture"
    # without waiting on a detached worker: pin the mtime in the past, compare after the run
    function script:Set-WatermarkAge($Sb) {
        $t = [datetime]::UtcNow.AddHours(-1)
        [System.IO.File]::SetLastWriteTimeUtc($Sb.Wm, $t)
        return $t
    }
}

Describe 'SessionStart capture through a junction alias in projects/' {

    Context 'the candidate pick' {
        It 'precondition: the sandbox lists every transcript three times, same name and same mtime (this is the bug surface)' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-JunctionSandbox
            try {
                $all = @(Get-ChildItem -Path (Join-Path $sb.Projects (Join-Path '*' '*.jsonl')) -File)
                $all.Count | Should -Be ($sb.S.Count * 3)
                foreach ($s in $sb.S) {
                    $copies = @($all | Where-Object { $_.Name -eq $s.Name })
                    $copies.Count | Should -Be 3
                    @($copies | ForEach-Object { $_.LastWriteTimeUtc.Ticks } | Select-Object -Unique).Count | Should -Be 1
                }
                @($all | Where-Object { $_.DirectoryName -eq $sb.Real }).Count | Should -Be $sb.S.Count
                @($all | Where-Object { ($_.Directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 }).Count | Should -Be ($sb.S.Count * 2)
            } finally { Remove-CaptureRoot $sb }
        }

        It 'always picks the transcript under the REAL directory, whichever session is excluded as the current one' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-JunctionSandbox
            try {
                # each run sees a different listing (the excluded session's three copies drop out), which is
                # what made the winning path flip on the live machine; the answer must be the same kind every time
                $runs = @(
                    @{ Current = 0; Via = 'Real';   Expect = 1 }   # a resumed session: its own (non-empty) transcript is the newest
                    @{ Current = 1; Via = 'AliasZ'; Expect = 0 }   # the current transcript_path reached through an alias
                    @{ Current = 2; Via = 'AliasA'; Expect = 0 }
                    @{ Current = -1; Via = 'Real';  Expect = 0 }   # a brand-new session: nothing of its own on disk yet
                )
                foreach ($r in $runs) {
                    Reset-CaptureState $sb
                    $cur = $null
                    if ($r.Current -ge 0) { $cur = $sb.S[$r.Current] }
                    if ($cur) {
                        Invoke-Capture $sb -SessionId $cur.Id -TranscriptPath (Join-Path $sb[$r.Via] $cur.Name)
                    } else {
                        Invoke-Capture $sb
                    }
                    $lines = @(Get-SpawnLines $sb 1 1)
                    $want = $sb.S[$r.Expect]
                    $why = "current=S$($r.Current) via $($r.Via)"
                    $lines.Count | Should -Be 1 -Because $why
                    $lines[0] | Should -Be ('SessionStart ' + (Join-Path $sb.Real $want.Name)) -Because $why
                    $lines[0] | Should -Not -BeLike '*-alias*' -Because "the extractor and the sessions label must see the real path ($why)"
                }
            } finally { Remove-CaptureRoot $sb }
        }

        It 'still captures a transcript that exists only behind a junction (an alias is never dropped from the listing)' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-CaptureRoot
            try {
                $elsewhere = Add-Dir $sb (Join-Path $sb.Root 'elsewhere\proj-x')   # outside projects\: only the link reaches it
                $link = Add-Junction $sb 'only-alias' $elsewhere
                $t = Add-Transcript $elsewhere 5
                Invoke-Capture $sb
                $lines = @(Get-SpawnLines $sb 1 1)
                $lines.Count | Should -Be 1
                $lines[0] | Should -Be ('SessionStart ' + (Join-Path $link $t.Name))
            } finally { Remove-CaptureRoot $sb }
        }

        It 'mtime decides first: a newer transcript behind an alias beats an older one in a real directory' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-CaptureRoot
            try {
                $real = Add-Dir $sb (Join-Path $sb.Projects 'proj-real')
                $null = Add-Transcript $real 30
                $elsewhere = Add-Dir $sb (Join-Path $sb.Root 'elsewhere\proj-x')
                $link = Add-Junction $sb 'only-alias' $elsewhere
                $newer = Add-Transcript $elsewhere 2
                Invoke-Capture $sb
                $lines = @(Get-SpawnLines $sb 1 1)
                $lines.Count | Should -Be 1
                $lines[0] | Should -Be ('SessionStart ' + (Join-Path $link $newer.Name))
            } finally { Remove-CaptureRoot $sb }
        }

        It 'with no real path at all, the same alias wins every time (ties end on the path)' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-CaptureRoot
            try {
                $elsewhere = Add-Dir $sb (Join-Path $sb.Root 'elsewhere\proj-x')
                $first = Add-Junction $sb 'a-alias' $elsewhere
                $null = Add-Junction $sb 'z-alias' $elsewhere
                $t = Add-Transcript $elsewhere 5
                foreach ($run in 1..2) {
                    Reset-CaptureState $sb
                    Invoke-Capture $sb
                    $lines = @(Get-SpawnLines $sb 1 1)
                    $lines.Count | Should -Be 1 -Because "run $run"
                    $lines[0] | Should -Be ('SessionStart ' + (Join-Path $first $t.Name)) -Because "run $run"
                }
            } finally { Remove-CaptureRoot $sb }
        }
    }

    Context 'the watermark' {
        It 'is keyed by the file name and the mtime ticks - never by the directory the file was reached through' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-JunctionSandbox
            try {
                Invoke-Capture $sb
                @(Get-SpawnLines $sb 1 1).Count | Should -Be 1
                $wm = Get-Watermark $sb
                $wm | Should -Be ($sb.S[0].Name + '|' + $sb.S[0].Ticks)
                $wm | Should -Not -Match '[\\/]' -Because 'a path in the watermark is what made the alias flip look like a new transcript'
            } finally { Remove-CaptureRoot $sb }
        }

        It 'a watermark in the new format suppresses the capture whichever path the listing offers' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-JunctionSandbox
            try {
                Set-Watermark $sb ($sb.S[0].Name + '|' + $sb.S[0].Ticks)
                $pinned = Set-WatermarkAge $sb
                Invoke-Capture $sb
                @(Get-SpawnLines $sb 0 2).Count | Should -Be 0
                (Get-Item -LiteralPath $sb.Wm).LastWriteTimeUtc | Should -Be $pinned -Because 'a suppressed start must not touch the watermark'
            } finally { Remove-CaptureRoot $sb }
        }

        It 'a second start does not capture the same transcript again when the path it is reached through has changed (the original failure)' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-CaptureRoot
            try {
                # Two aliases and no real path under projects\, so whichever alias the first start used, the
                # transcript is reachable through the other one afterwards - whatever the sort does with ties.
                $elsewhere = Add-Dir $sb (Join-Path $sb.Root 'elsewhere\proj-x')
                $aliases = @((Add-Junction $sb 'a-alias' $elsewhere), (Add-Junction $sb 'z-alias' $elsewhere))
                $t = Add-Transcript $elsewhere 5
                Invoke-Capture $sb
                $first = @(Get-SpawnLines $sb 1 1)
                $first.Count | Should -Be 1
                $used = @($aliases | Where-Object { $first[0] -like ('SessionStart ' + $_ + '\*') })
                $used.Count | Should -Be 1 -Because 'the first start went through exactly one alias'
                Remove-Junction $sb $used[0]
                $left = @(Get-ChildItem -Path (Join-Path $sb.Projects (Join-Path '*' '*.jsonl')) -File)
                $left.Count | Should -Be 1 -Because 'the same transcript, at the same mtime, is now listed under the other alias only'
                $left[0].FullName | Should -Not -Be (Join-Path $used[0] $t.Name)
                $pinned = Set-WatermarkAge $sb
                Invoke-Capture $sb   # another session's start: a fresh marker key, so only the watermark can stop it
                @(Get-SpawnLines $sb 1 2).Count | Should -Be 1 -Because 'nothing about the transcript changed but the path it was listed under'
                (Get-Item -LiteralPath $sb.Wm).LastWriteTimeUtc | Should -Be $pinned
            } finally { Remove-CaptureRoot $sb }
        }

        It 'a watermark an earlier release wrote (full path plus ticks) through the <Form> path still suppresses the capture' -ForEach @(
            @{ Form = 'Real' }, @{ Form = 'AliasA' }, @{ Form = 'AliasZ' }
        ) {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-JunctionSandbox
            try {
                $legacy = (Join-Path $sb[$Form] $sb.S[0].Name) + '|' + $sb.S[0].Ticks
                Set-Watermark $sb $legacy
                $pinned = Set-WatermarkAge $sb
                Invoke-Capture $sb
                @(Get-SpawnLines $sb 0 2).Count | Should -Be 0 -Because 'upgrading must not cost one extra capture'
                (Get-Item -LiteralPath $sb.Wm).LastWriteTimeUtc | Should -Be $pinned
                Get-Watermark $sb | Should -Be $legacy
            } finally { Remove-CaptureRoot $sb }
        }

        It 'a watermark of a different file whose name merely ends the same way does not suppress the capture' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-JunctionSandbox
            try {
                $s0 = $sb.S[0]
                Set-Watermark $sb ('x' + $s0.Name + '|' + $s0.Ticks)   # no directory separator before the name: not a path to this file
                Invoke-Capture $sb
                $lines = @(Get-SpawnLines $sb 1 1)
                $lines.Count | Should -Be 1
                $lines[0] | Should -Be ('SessionStart ' + (Join-Path $sb.Real $s0.Name))
            } finally { Remove-CaptureRoot $sb }
        }

        It 'a genuinely newer transcript is still captured, on its real path' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-JunctionSandbox
            try {
                Set-Watermark $sb ($sb.S[0].Name + '|' + $sb.S[0].Ticks)
                $newer = Add-Transcript $sb.Real 1
                Invoke-Capture $sb
                $lines = @(Get-SpawnLines $sb 1 1)
                $lines.Count | Should -Be 1
                $lines[0] | Should -Be ('SessionStart ' + (Join-Path $sb.Real $newer.Name))
                Get-Watermark $sb | Should -Be ($newer.Name + '|' + $newer.Ticks)
            } finally { Remove-CaptureRoot $sb }
        }

        It 'the same transcript with a new mtime is captured again (the watermark is the name AND the ticks)' {
            if ($script:skipReason) { Set-ItResult -Skipped -Because $script:skipReason; return }
            $sb = New-JunctionSandbox
            try {
                $s0 = $sb.S[0]
                Invoke-Capture $sb
                @(Get-SpawnLines $sb 1 1).Count | Should -Be 1
                Get-Watermark $sb | Should -Be ($s0.Name + '|' + $s0.Ticks)
                $ticks = Set-TranscriptAge $s0.Path 0.5   # the session went on and the file grew
                $ticks | Should -Not -Be $s0.Ticks
                Invoke-Capture $sb                        # a real second start, reading the watermark the first one wrote
                $lines = @(Get-SpawnLines $sb 2 1)
                $lines.Count | Should -Be 2
                foreach ($l in $lines) { $l | Should -Be ('SessionStart ' + (Join-Path $sb.Real $s0.Name)) }
                Get-Watermark $sb | Should -Be ($s0.Name + '|' + $ticks)
            } finally { Remove-CaptureRoot $sb }
        }
    }
}
