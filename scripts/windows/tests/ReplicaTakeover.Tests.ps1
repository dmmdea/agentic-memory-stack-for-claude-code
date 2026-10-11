#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# ReplicaTakeover.Tests.ps1 - 1.35.2: a Windows PC replica actually takes over, with last night's memory.
#
# A replica takes over only through the mem0-offline-watcher task: after 3 failed probes of the authority it
# restores the newest snapshot set in its local cache and starts the local store. On 2026-10-11 neither half
# held: a desktop replica had run as one for four weeks with no watcher (nothing registered it: it was a manual
# step), and both PC replicas held a two-day-old set because the cache was refreshed only by
# `travel-mode.ps1 on` (at a takeover, when the authority is down and the refresh is skipped) or by hand.
# This suite pins the fix:
#   travel-mode.ps1 refresh         seeds the cache while online and writes replica-cache-refresh.json
#   offline-watcher.ps1             starts mem0-replica-cache-refresh when one is due; a takeover restores
#                                   again when the cache got a newer set since the last restore
#   install-offline-watcher.ps1     registers the refresh task beside the watcher
#   2-windows-config.ps1            registers both on every replica, removes both from a brain
#   Test-MemoryStack.ps1 / 3-verify flag an unarmed replica and a stale cache
# travel-mode.ps1 refresh is RUN here against temp folders and a local HTTP stub standing in for the
# authority; nothing touches a real distro, task or cache.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester <repo>/scripts/windows/tests/ReplicaTakeover.Tests.ps1 -Output Detailed"

BeforeAll {
    $script:winDir     = Split-Path -Parent $PSScriptRoot
    $script:repoRoot   = (Resolve-Path (Join-Path $script:winDir '..\..')).Path
    $script:travelPs1  = Join-Path $script:repoRoot 'scripts\travel\travel-mode.ps1'
    $script:watcherPs1 = Join-Path $script:repoRoot 'scripts\travel\offline-watcher.ps1'
    $script:installOw  = Join-Path $script:repoRoot 'scripts\travel\install-offline-watcher.ps1'
    $script:winCfg     = Join-Path $script:repoRoot 'install\2-windows-config.ps1'
    $script:verifyPs1  = Join-Path $script:repoRoot 'install\3-verify.ps1'
    $script:tmsPs1     = Join-Path $script:winDir 'Test-MemoryStack.ps1'

    function script:Get-CodeText {
        param([string]$Path)   # the file without its comment lines, so prose can never satisfy a pin
        ((Get-Content -LiteralPath $Path -Raw -Encoding UTF8) -split "`r?`n" |
            Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
    }
    function script:Import-Functions {
        param([string]$Path, [string[]]$Names)
        $errs = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$errs)
        $errs | Should -BeNullOrEmpty -Because "$Path must parse"
        foreach ($n in $Names) {
            $fn = $ast.Find({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
            $fn | Should -Not -BeNullOrEmpty -Because "$n must exist in $Path"
            . ([scriptblock]::Create(([regex]'^function ').Replace($fn.Extent.Text, 'function script:', 1)))
        }
    }
    function script:New-Set {
        # A COMPLETE snapshot set (the four files that share a stamp) in $Dir, plus a media tar when asked.
        param([string]$Dir, [string]$Stamp, [string]$Body = 'x', [switch]$Media)
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        foreach ($f in "episodic-$Stamp.db", "history-$Stamp.db", "qdrant-$Stamp.snapshot") { [IO.File]::WriteAllText((Join-Path $Dir $f), $Body) }
        $m = @{ files = @{ qdrant_snapshot = "qdrant-$Stamp.snapshot" } }
        if ($Media) { $m.files.media = "media-$Stamp.tar"; [IO.File]::WriteAllText((Join-Path $Dir "media-$Stamp.tar"), 'tar') }
        ($m | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath (Join-Path $Dir "manifest-$Stamp.json") -Encoding UTF8
    }
}

Describe 'offline-watcher.ps1: when a cache refresh is due' {
    BeforeAll { . $script:watcherPs1 -DefineOnly }
    BeforeEach { $script:now = 1800000000L }
    It 'is due when there is no record (first run, or a record that could not be read)' {
        Test-CacheRefreshDue -Record $null -NowEpoch $now | Should -BeTrue
    }
    It 'waits 12 h after a refresh that left the cache current' {
        $r = [pscustomobject]@{ state = 'done'; ok = $true; finished_epoch = $now - 11 * 3600 }
        Test-CacheRefreshDue -Record $r -NowEpoch $now | Should -BeFalse
        $r.finished_epoch = $now - 12 * 3600
        Test-CacheRefreshDue -Record $r -NowEpoch $now | Should -BeTrue
    }
    It 'retries 6 h after a refresh that failed, not every 2 minutes' {
        $r = [pscustomobject]@{ state = 'done'; ok = $false; finished_epoch = $now - 5 * 3600 }
        Test-CacheRefreshDue -Record $r -NowEpoch $now | Should -BeFalse
        $r.finished_epoch = $now - 6 * 3600
        Test-CacheRefreshDue -Record $r -NowEpoch $now | Should -BeTrue
    }
    It 'waits on a running refresh past the task''s 2 h limit, and retries 6 h after it started (it died)' {
        $r = [pscustomobject]@{ state = 'running'; started_epoch = $now - 3 * 3600 }
        Test-CacheRefreshDue -Record $r -NowEpoch $now | Should -BeFalse -Because 'a refresh killed at 2 h must not be restarted back to back'
        $r.started_epoch = $now - 6 * 3600
        Test-CacheRefreshDue -Record $r -NowEpoch $now | Should -BeTrue
    }
    It 'is due when the record carries no usable time, or a time in the future (a clock moved back)' -ForEach @(
        @{ R = [pscustomobject]@{ state = 'done'; ok = $true } }
        @{ R = [pscustomobject]@{ state = 'done'; ok = $true; finished_epoch = 'soon' } }
        @{ R = [pscustomobject]@{ state = 'done'; ok = $true; finished_epoch = 0 } }
        @{ R = [pscustomobject]@{ state = 'done'; ok = $true; finished_epoch = 1800000000L + 7200 } }
    ) {
        Test-CacheRefreshDue -Record $R -NowEpoch $now | Should -BeTrue
    }
}

Describe 'offline-watcher.ps1: a takeover restores again when the cache holds a newer set than it restored' {
    BeforeAll { . $script:watcherPs1 -DefineOnly }
    BeforeEach { $script:now = 1800000000L }
    It 'restores when there is no restore marker' {
        Test-ReplicaRestoreStale -Marker $null -CacheStamp '20261010-030356' -NowEpoch $now | Should -BeTrue
    }
    It 'restores when the last restore is older than 24 h' {
        $m = [pscustomobject]@{ Epoch = $now - 25 * 3600; Stamp = '20261010-030356' }
        Test-ReplicaRestoreStale -Marker $m -CacheStamp '20261010-030356' -NowEpoch $now | Should -BeTrue
    }
    It 'restores once for a marker from before 1.35.2, which names no set' {
        $m = [pscustomobject]@{ Epoch = $now - 3600; Stamp = '' }
        Test-ReplicaRestoreStale -Marker $m -CacheStamp '20261010-030356' -NowEpoch $now | Should -BeTrue
    }
    It 'restarts the replica it restored within 24 h when the cache holds nothing newer' {
        $m = [pscustomobject]@{ Epoch = $now - 3600; Stamp = '20261010-030356' }
        Test-ReplicaRestoreStale -Marker $m -CacheStamp '20261010-030356' -NowEpoch $now | Should -BeFalse
        Test-ReplicaRestoreStale -Marker $m -CacheStamp '' -NowEpoch $now | Should -BeFalse -Because 'no readable cache is not a reason to restore'
    }
    It 'restores again when the cache holds a newer set, however and whenever it got there' {
        $m = [pscustomobject]@{ Epoch = $now - 600; Stamp = '20261009-053234' }
        Test-ReplicaRestoreStale -Marker $m -CacheStamp '20261010-030356' -NowEpoch $now | Should -BeTrue
    }
    It 'reads both marker formats' {
        $p = Join-Path $TestDrive 'replica-restored.txt'
        Set-Content -LiteralPath $p -Value '2026-10-11T06:00:00.0000000Z 20261010-030356'
        (Read-RestoreMarker $p).Stamp | Should -Be '20261010-030356'
        Set-Content -LiteralPath $p -Value '2026-10-09T05:40:00.0000000-05:00'
        $old = Read-RestoreMarker $p
        $old.Stamp | Should -Be ''
        $old.Epoch | Should -BeGreaterThan 0
        Read-RestoreMarker (Join-Path $TestDrive 'none.txt') | Should -BeNullOrEmpty
    }
    It 'finds the cache where the refresh recorded it, else by the authority host, never for an IP or local host' {
        Get-LocalCacheDir -Record ([pscustomobject]@{ local_dir = 'E:\cache\brain' }) -Authority 'http://brain-host:18791' | Should -Be 'E:\cache\brain'
        Get-LocalCacheDir -Record $null -Authority 'http://brain-host.tailnet.ts.net:18791' | Should -Be 'D:\memory-backups\brain-host'
        Get-LocalCacheDir -Record $null -Authority 'http://192.0.2.7:18791' | Should -Be ''
        Get-LocalCacheDir -Record $null -Authority 'http://localhost:18791' | Should -Be ''
    }
}

Describe 'offline-watcher.ps1: Get-CacheNewestStamp agrees with travel-mode.ps1 Get-NewestCompleteSet' {
    BeforeAll {
        . $script:watcherPs1 -DefineOnly
        script:Import-Functions $script:travelPs1 @('Get-SetFiles', 'Get-SetMediaFile', 'Get-NewestCompleteSet')
    }
    It 'picks the same set in <Name>' -ForEach @(
        @{ Name = 'a cache with one complete set'; Build = { param($d) script:New-Set $d '20261010-030356' } }
        @{ Name = 'a newer incomplete set beside an older complete one'; Build = { param($d) script:New-Set $d '20261009-053234'; script:New-Set $d '20261010-030356'; Remove-Item (Join-Path $d 'qdrant-20261010-030356.snapshot') } }
        @{ Name = 'an empty folder'; Build = { param($d) New-Item -ItemType Directory -Force -Path $d | Out-Null } }
        @{ Name = 'a missing folder'; Build = { param($d) } }
    ) {
        $d = Join-Path $TestDrive ('n' + [guid]::NewGuid().ToString('N'))
        & $Build $d
        "$(Get-CacheNewestStamp $d)" | Should -Be "$(Get-NewestCompleteSet $d)"
    }
}

Describe 'offline-watcher.ps1: the tick, run end to end' {
    BeforeAll {
        # The real tick in a child pwsh: a fake profile, a local authority stub (or a closed port), a distro
        # that does not exist, and Start-ScheduledTask replaced by a function that records the call.
        $script:port = $null; $script:listener = $null
        foreach ($try in 1..5) {
            $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
            $probe.Start(); $p_ = $probe.LocalEndpoint.Port; $probe.Stop()
            $l = [System.Net.HttpListener]::new(); $l.Prefixes.Add("http://localhost:$p_/")
            try { $l.Start(); $script:listener = $l; $script:port = $p_; break } catch { $l.Close() }
        }
        $script:listener | Should -Not -BeNullOrEmpty
        $script:stub = Start-ThreadJob -ArgumentList $script:listener -ScriptBlock {
            param($l)
            while ($l.IsListening) {
                try { $c = $l.GetContext(); $b = [Text.Encoding]::UTF8.GetBytes('{"ok":true}'); $c.Response.ContentType = 'application/json'; $c.Response.OutputStream.Write($b, 0, $b.Length); $c.Response.Close() } catch { break }
            }
        }
        function script:Invoke-Tick {
            param([string]$Authority, [string]$UserProfile)
            $calls = Join-Path $UserProfile 'start-calls.txt'
            & pwsh -NoProfile -Command "`$env:USERPROFILE = '$UserProfile'; `$env:MEM0_WSL_DISTRO = 'no-such-distro-for-tests'; function Start-ScheduledTask { param([string]`$TaskName) Add-Content -LiteralPath '$calls' -Value `$TaskName }; & '$($script:watcherPs1)' -Authority '$Authority' *>`$null" 2>&1 | Out-Null
            [pscustomobject]@{ Calls = @(if (Test-Path $calls) { Get-Content $calls }); State = (Get-Content -Raw (Join-Path $UserProfile '.claude\state\offline-mode.json') | ConvertFrom-Json) }
        }
        function script:New-TickProfile {
            param($Record)
            $h = Join-Path $TestDrive ('t' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Force -Path (Join-Path $h '.claude\state') | Out-Null
            if ($Record) { ($Record | ConvertTo-Json -Compress) | Set-Content -LiteralPath (Join-Path $h '.claude\state\replica-cache-refresh.json') }
            $h
        }
    }
    AfterAll {
        try { $script:listener.Stop(); $script:listener.Close() } catch {}
        $script:stub | Remove-Job -Force -ErrorAction SilentlyContinue
    }
    It 'starts mem0-replica-cache-refresh on an online tick when no refresh has run' {
        $r = script:Invoke-Tick -Authority "http://localhost:$($script:port)" -UserProfile (script:New-TickProfile)
        $r.Calls | Should -Be @('mem0-replica-cache-refresh')
        $r.State.mode | Should -Be 'online'
    }
    It 'does not start it when the last refresh is recent and left the cache current' {
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $r = script:Invoke-Tick -Authority "http://localhost:$($script:port)" -UserProfile (script:New-TickProfile @{ state = 'done'; ok = $true; finished_epoch = $now - 600 })
        $r.Calls.Count | Should -Be 0
    }
    It 'does not start it while the authority does not answer, and counts the miss' {
        $r = script:Invoke-Tick -Authority 'http://localhost:1' -UserProfile (script:New-TickProfile)
        $r.Calls.Count | Should -Be 0
        $r.State.consecutive_down | Should -Be 1
        $r.State.mode | Should -Be 'online' -Because 'one miss is a blip, not a takeover'
    }
}

Describe 'offline-watcher.ps1: the tick wires the decisions in' {
    BeforeAll { $script:code = script:Get-CodeText $script:watcherPs1 }
    It 'decides a takeover restore from the marker''s set stamp and the cache''s newest complete set' {
        $script:code | Should -Match '\$marker = Read-RestoreMarker \$lastRestore'
        $script:code | Should -Match '\$cacheStamp = Get-CacheNewestStamp \(Get-LocalCacheDir -Record \$refreshRecord -Authority \$Authority\)'
        $script:code | Should -Match '\$stale = Test-ReplicaRestoreStale -Marker \$marker -CacheStamp \$cacheStamp -NowEpoch \$nowEpoch'
        $script:code | Should -Not -Match 'TotalHours -gt 24' -Because 'the old inline check ignored a newer cached set'
    }
    It 'starts the previous restore when a re-restore fails, instead of leaving nothing up' {
        $script:code | Should -Match '(?s)Write-WatcherLog "go_offline: travel-mode\.ps1 on FAILED: \$tmErr"\s*if \(\$marker\) \{\s*Wsl "systemctl --user start qdrant\.service mem0\.service'
    }
    It 'leaves the marker to travel-mode.ps1 on, which names the set it restored' {
        $script:code | Should -Not -Match 'Set-Content \$lastRestore'
        $tm = script:Get-CodeText $script:travelPs1
        $tm | Should -Match '(?s)& "\$PSScriptRoot\\restore-replica\.ps1" -BackupDir \$BackupDir -Stamp \$stamp -Distro \$Distro.*Set-Content -LiteralPath \$marker -Value "\$\(\(Get-Date\)\.ToUniversalTime\(\)\.ToString\(''o''\)\) \$stamp"'
    }
    It 'never waits on the copy: the tick starts a task, it does not run travel-mode refresh itself' {
        $script:code | Should -Not -Match 'travel-mode\.ps1"?\s+refresh'
    }
}

Describe 'travel-mode.ps1: Update-LocalCache seeds the newest complete set' {
    BeforeAll {
        script:Import-Functions $script:travelPs1 @('Get-SetFiles', 'Get-SetMediaFile', 'Test-SetMediaMissing', 'Get-NewestCompleteSet',
            'Get-CompleteStamps', 'Invoke-CacheRetention', 'Get-SetProfile', 'Update-LocalCache')
        function script:Set-Profile { param([string]$Dir, [string]$Stamp, [string]$Prof)
            $p = Join-Path $Dir "manifest-$Stamp.json"; $m = Get-Content -Raw $p | ConvertFrom-Json -AsHashtable; $m.embed_profile = $Prof
            ($m | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $p -Encoding UTF8 }
    }
    BeforeEach {
        $script:cloud = Join-Path $TestDrive ('cloud' + [guid]::NewGuid().ToString('N'))
        $script:local = Join-Path $TestDrive ('local' + [guid]::NewGuid().ToString('N'))
    }
    It 'copies the newest cloud set, media tar included, and reports it seeded with the set''s own file time' {
        script:New-Set $cloud '20261009-053234'
        script:New-Set $cloud '20261011-030356' -Media
        script:New-Set $local '20261009-053234'
        $r = Update-LocalCache $cloud $local 6>$null
        $r.seeded | Should -BeTrue
        $r.error | Should -BeNullOrEmpty
        $r.cloud_stamp | Should -Be '20261011-030356'
        $r.local_stamp | Should -Be '20261011-030356'
        $r.local_set_epoch | Should -BeGreaterThan 0
        Test-Path (Join-Path $local 'media-20261011-030356.tar') | Should -BeTrue
        @(Get-ChildItem $local -Filter '*.part').Count | Should -Be 0
    }
    It 'does nothing when the cache already holds the newest set' {
        script:New-Set $cloud '20261011-030356'
        script:New-Set $local '20261011-030356'
        $r = Update-LocalCache $cloud $local 6>$null
        $r.seeded | Should -BeFalse
        $r.local_stamp | Should -Be '20261011-030356'
    }
    It 'keeps the newest 3 complete sets, and makes room BEFORE copying (2 kept, then the new one)' {
        foreach ($s in '20261007-030000', '20261008-030000', '20261009-030000') { script:New-Set $local $s }
        script:New-Set $cloud '20261010-030000'
        Mock Copy-Item { $script:seenAtCopy = @(Get-ChildItem $local -Filter 'manifest-*.json' | ForEach-Object Name); [IO.File]::Copy($Path, $Destination, $true) }
        Update-LocalCache $cloud $local 6>$null | Out-Null
        $script:seenAtCopy | Should -Not -Contain 'manifest-20261007-030000.json' -Because 'the oldest set is pruned before the copy needs its space'
        @(Get-ChildItem $local -Filter 'manifest-*.json' | ForEach-Object Name | Sort-Object) |
            Should -Be @('manifest-20261008-030000.json', 'manifest-20261009-030000.json', 'manifest-20261010-030000.json')
    }
    It 'prunes on a pass that copies nothing too (a prune that failed once is retried)' {
        foreach ($s in '20261007-030000', '20261008-030000', '20261009-030000', '20261010-030000') { script:New-Set $local $s }
        script:New-Set $cloud '20261010-030000'
        $r = Update-LocalCache $cloud $local 6>$null
        $r.seeded | Should -BeFalse
        @(Get-ChildItem $local -Filter 'manifest-*.json').Count | Should -Be 3
    }
    It 'counts complete sets only: a torn newer set never pushes a restorable one out' {
        foreach ($s in '20261007-030000', '20261008-030000', '20261009-030000') { script:New-Set $local $s }
        script:New-Set $local '20261010-030000'; Remove-Item (Join-Path $local 'qdrant-20261010-030000.snapshot')
        script:New-Set $cloud '20261009-030000'
        Update-LocalCache $cloud $local 6>$null | Out-Null
        foreach ($s in '20261007-030000', '20261008-030000', '20261009-030000') { Test-Path (Join-Path $local "qdrant-$s.snapshot") | Should -BeTrue }
        Test-Path (Join-Path $local 'manifest-20261010-030000.json') | Should -BeFalse -Because 'the torn set is not one of the newest 3 complete sets'
    }
    It 'removes dead .part files, and leaves none when a copy fails' {
        script:New-Set $cloud '20261011-030356'
        New-Item -ItemType Directory -Force -Path $local | Out-Null
        [IO.File]::WriteAllText((Join-Path $local 'qdrant-20261001-030000.snapshot.part'), 'torn')
        Mock Copy-Item { [IO.File]::WriteAllText($Destination, 'half'); throw 'disk full' }
        $r = Update-LocalCache $cloud $local 6>$null
        $r.error | Should -Match 'disk full'
        @(Get-ChildItem $local -Filter '*.part').Count | Should -Be 0
    }
    It 'returns the error and keeps the restorable set when the copy fails' {
        script:New-Set $cloud '20261011-030356'
        script:New-Set $local '20261009-053234'
        Mock Copy-Item { throw 'pCloud stalled' }
        $r = Update-LocalCache $cloud $local 6>$null
        $r.error | Should -Match 'pCloud stalled'
        $r.seeded | Should -BeFalse
        $r.local_stamp | Should -Be '20261009-053234'
    }
    It 'reports the newer set as held when only the optional media tar failed (the set is complete without it)' {
        script:New-Set $cloud '20261011-030356' -Media
        script:New-Set $local '20261009-053234'
        Mock Copy-Item { if ("$Path" -like '*.tar') { throw 'tar stalled' } else { [IO.File]::Copy($Path, $Destination, $true) } }
        $r = Update-LocalCache $cloud $local 6>$null
        $r.error | Should -Match 'tar stalled'
        $r.local_stamp | Should -Be '20261011-030356' -Because 'a takeover restores what is on disk, and the record must say so'
    }
    It 'never copies a set made in another embedding profile, and keeps the sets this box can restore' {
        script:New-Set $cloud '20261011-030356'; script:Set-Profile $cloud '20261011-030356' 'egemma2'
        foreach ($s in '20261007-030000', '20261008-030000', '20261009-030000') { script:New-Set $local $s }
        $r = Update-LocalCache $cloud $local -BoxProfile 'egemma-300m' -LegacyProfile 'egemma-300m' 6>$null
        $r.error | Should -Match "profile 'egemma2' but this box restores 'egemma-300m'"
        $r.error | Should -Match '-EmbedProfile egemma2'
        $r.seeded | Should -BeFalse
        Test-Path (Join-Path $local 'manifest-20261011-030356.json') | Should -BeFalse
        @(Get-ChildItem $local -Filter 'manifest-*.json').Count | Should -Be 3
    }
    It 'reads a set without embed_profile as the legacy space, as restore-replica.ps1 does' {
        script:New-Set $cloud '20261011-030356'
        (Update-LocalCache $cloud $local -BoxProfile 'egemma-300m' -LegacyProfile 'egemma-300m' 6>$null).seeded | Should -BeTrue
        $l2 = Join-Path $TestDrive ('l' + [guid]::NewGuid().ToString('N'))
        (Update-LocalCache $cloud $l2 -BoxProfile 'egemma2' -LegacyProfile 'egemma-300m' 6>$null).error | Should -Match "profile 'egemma-300m'"
    }
    It 'copies as before when the box profile is unknown (WSL did not answer)' {
        script:New-Set $cloud '20261011-030356'; script:Set-Profile $cloud '20261011-030356' 'egemma2'
        (Update-LocalCache $cloud $local -BoxProfile '' -LegacyProfile '' 6>$null).seeded | Should -BeTrue
    }
    It 'reports no cloud set when pCloud holds none (a missing drive is just "no sets")' {
        $r = Update-LocalCache (Join-Path $TestDrive 'nope') $local 6>$null
        $r.cloud_stamp | Should -BeNullOrEmpty
        $r.error | Should -BeNullOrEmpty
    }
}

Describe 'travel-mode.ps1: the refresh record' {
    BeforeAll { script:Import-Functions $script:travelPs1 @('New-RefreshResult', 'New-RunningRecord', 'Write-RefreshRecord', 'Read-RefreshRecord') }
    It 'is ok when the cache now holds pCloud''s newest set' {
        $seed = [pscustomobject]@{ cloud_stamp = 'B'; local_stamp = 'B'; local_set_epoch = 150; seeded = $true; error = $null }
        $r = New-RefreshResult -Seed $seed -Prior $null -StartedEpoch 100 -NowEpoch 200 -LocalDir 'L' -CloudDir 'C'
        $r.ok | Should -BeTrue
        $r.error | Should -BeNullOrEmpty
        $r.local_set_epoch | Should -Be 150
        $r.seeded | Should -BeTrue
    }
    It 'is not ok when the copy failed, when the cache is behind, or when pCloud has no set' -ForEach @(
        @{ Seed = [pscustomobject]@{ cloud_stamp = 'B'; local_stamp = 'A'; seeded = $false; error = 'disk full' }; Err = 'disk full' }
        @{ Seed = [pscustomobject]@{ cloud_stamp = 'B'; local_stamp = 'A'; seeded = $false; error = $null }; Err = "holds 'A'.*newest complete set is 'B'" }
        @{ Seed = [pscustomobject]@{ cloud_stamp = $null; local_stamp = 'A'; seeded = $false; error = $null }; Err = 'no complete snapshot set in C' }
    ) {
        $r = New-RefreshResult -Seed $Seed -Prior $null -StartedEpoch 1 -NowEpoch 2 -LocalDir 'L' -CloudDir 'C'
        $r.ok | Should -BeFalse
        $r.error | Should -Match $Err
    }
    It 'records a failure before any seeding, still saying what a takeover would restore' {
        $r = New-RefreshResult -Seed $null -Prior $null -StartedEpoch 1 -NowEpoch 2 -LocalDir 'L' -CloudDir '' -ErrorText 'the authority is unreachable' -FallbackLocalStamp '20261009-053234'
        $r.ok | Should -BeFalse
        $r.error | Should -Be 'the authority is unreachable'
        $r.local_stamp | Should -Be '20261009-053234'
        $r.state | Should -Be 'done'
        $prior = [pscustomobject]@{ local_stamp = '20261008-030000'; local_set_epoch = 99 }
        $r2 = New-RefreshResult -Seed $null -Prior $prior -StartedEpoch 1 -NowEpoch 2 -LocalDir '' -CloudDir '' -ErrorText 'REFUSED'
        $r2.local_stamp | Should -Be '20261008-030000'
        $r2.local_set_epoch | Should -Be 99
    }
    It 'a running record keeps the previous result, so the backoff and the health row still see it' {
        $prior = [pscustomobject]@{ state = 'done'; ok = $false; finished_epoch = 50; local_stamp = 'A'; error = 'pCloud stalled'; local_dir = 'L' }
        $run = New-RunningRecord -Prior $prior -StartedEpoch 100
        $run.state | Should -Be 'running'
        $run.started_epoch | Should -Be 100
        $run.ok | Should -BeFalse
        $run.error | Should -Be 'pCloud stalled'
        $run.local_stamp | Should -Be 'A'
        (New-RunningRecord -Prior $null -StartedEpoch 100).Keys | Should -Not -Contain 'ok'
    }
    It 'writes the record even while a reader holds it open (the rename is retried)' {
        $p = Join-Path $TestDrive 'rec.json'
        Write-RefreshRecord $p ([ordered]@{ state = 'done'; ok = $false })
        $fs = [IO.File]::Open($p, 'Open', 'Read', 'Read')
        $job = Start-ThreadJob -ArgumentList $fs -ScriptBlock { param($f) Start-Sleep -Milliseconds 600; $f.Dispose() }
        try { Write-RefreshRecord $p ([ordered]@{ state = 'done'; ok = $true }) } finally { $job | Wait-Job | Remove-Job; $fs.Dispose() }
        (Read-RefreshRecord $p).ok | Should -BeTrue
        @(Get-ChildItem $TestDrive -Filter 'rec.json.*.tmp').Count | Should -Be 0
    }
}

Describe 'travel-mode.ps1 refresh, run end to end' {
    BeforeAll {
        # A fake user profile: the receipt names a REMOTE authority (so the one-brain guard passes) and the
        # refresh record lands under it. -Authority points the reachability probe at a local stub.
        function script:Invoke-Refresh {
            param([string]$Authority, [string]$Cloud, [string]$Local, [string]$UserProfile)
            $out =& pwsh -NoProfile -Command "`$env:USERPROFILE = '$UserProfile'; `$env:MEM0_WSL_DISTRO = 'no-such-distro-for-tests'; & '$($script:travelPs1)' refresh -Authority '$Authority' -CloudBackupDir '$Cloud' -LocalBackupDir '$Local' *>&1; exit `$LASTEXITCODE" 2>&1
            [pscustomobject]@{ Code = $LASTEXITCODE; Out = ($out | Out-String) }
        }
        function script:New-Profile {
            $p = Join-Path $TestDrive ('home' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Force -Path (Join-Path $p '.claude\scripts') | Out-Null
            "@{ AuthorityUrl = 'http://authority-host:18791'; Distro = 'no-such-distro-for-tests'; Role = 'replica' }" |
                Set-Content -LiteralPath (Join-Path $p '.claude\scripts\mem0-stack.config.psd1')
            $p
        }
        # The authority stub: answers /health with {"ok":true} until stopped.
        # The port comes from the OS (bind port 0, read it back), never Get-Random: another suite seeds the
        # session's random generator, which made the "random" port the same busy one on every combined run.
        $script:listener = $null
        foreach ($try in 1..5) {
            $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
            $probe.Start(); $script:port = $probe.LocalEndpoint.Port; $probe.Stop()
            $l = [System.Net.HttpListener]::new()
            $l.Prefixes.Add("http://localhost:$($script:port)/")
            try { $l.Start(); $script:listener = $l; break } catch { $l.Close() }
        }
        $script:listener | Should -Not -BeNullOrEmpty -Because 'the authority stub needs a free local port'
        $script:stub = Start-ThreadJob -ArgumentList $script:listener -ScriptBlock {
            param($l)
            while ($l.IsListening) {
                try {
                    $c = $l.GetContext()
                    $b = [Text.Encoding]::UTF8.GetBytes('{"ok":true}')
                    $c.Response.ContentType = 'application/json'
                    $c.Response.OutputStream.Write($b, 0, $b.Length)
                    $c.Response.Close()
                } catch { break }
            }
        }
    }
    AfterAll {
        try { $script:listener.Stop(); $script:listener.Close() } catch {}
        $script:stub | Remove-Job -Force -ErrorAction SilentlyContinue
    }
    It 'seeds the cache, writes an ok record and exits 0 while the authority answers' {
        $home_ = script:New-Profile
        $cloud = Join-Path $TestDrive ('c' + [guid]::NewGuid().ToString('N')); $local = Join-Path $TestDrive ('l' + [guid]::NewGuid().ToString('N'))
        script:New-Set $cloud '20261011-030356'
        $r = script:Invoke-Refresh -Authority "http://localhost:$($script:port)" -Cloud $cloud -Local $local -UserProfile $home_
        $r.Code | Should -Be 0 -Because $r.Out
        $rec = Get-Content -Raw (Join-Path $home_ '.claude\state\replica-cache-refresh.json') | ConvertFrom-Json
        $rec.state | Should -Be 'done'
        $rec.ok | Should -BeTrue
        $rec.local_stamp | Should -Be '20261011-030356'
        $rec.seeded | Should -BeTrue
        $rec.local_set_epoch | Should -BeGreaterThan 0
        Test-Path (Join-Path $local 'qdrant-20261011-030356.snapshot') | Should -BeTrue
    }
    It 'touches nothing, records the failure and exits 1 when the authority does not answer' {
        $home_ = script:New-Profile
        $cloud = Join-Path $TestDrive ('c' + [guid]::NewGuid().ToString('N')); $local = Join-Path $TestDrive ('l' + [guid]::NewGuid().ToString('N'))
        script:New-Set $cloud '20261011-030356'
        $r = script:Invoke-Refresh -Authority 'http://localhost:1' -Cloud $cloud -Local $local -UserProfile $home_
        $r.Code | Should -Be 1 -Because $r.Out
        $rec = Get-Content -Raw (Join-Path $home_ '.claude\state\replica-cache-refresh.json') | ConvertFrom-Json
        $rec.ok | Should -BeFalse
        $rec.error | Should -Match 'unreachable'
        Test-Path $local | Should -BeFalse -Because 'pCloud is never read while offline: its drive can stall'
    }
    It 'refuses on the authority itself, and still records it (so the watcher does not retry every 2 minutes)' {
        $home_ = script:New-Profile
        "@{ AuthorityUrl = 'http://127.0.0.1:18791'; Distro = 'no-such-distro-for-tests'; Role = 'brain' }" |
            Set-Content -LiteralPath (Join-Path $home_ '.claude\scripts\mem0-stack.config.psd1')
        $r = script:Invoke-Refresh -Authority "http://localhost:$($script:port)" -Cloud (Join-Path $TestDrive 'c-none') -Local (Join-Path $TestDrive 'l-none') -UserProfile $home_
        $r.Code | Should -Be 1
        $rec = Get-Content -Raw (Join-Path $home_ '.claude\state\replica-cache-refresh.json') | ConvertFrom-Json
        $rec.ok | Should -BeFalse
        $rec.error | Should -Match 'REFUSED'
    }
    It 'exits 0 and writes nothing while another refresh holds the lock' {
        $home_ = script:New-Profile
        $state = Join-Path $home_ '.claude\state'; New-Item -ItemType Directory -Force -Path $state | Out-Null
        $held = [IO.File]::Open((Join-Path $state 'replica-cache-refresh.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
        try {
            $r = script:Invoke-Refresh -Authority "http://localhost:$($script:port)" -Cloud (Join-Path $TestDrive 'c-lock') -Local (Join-Path $TestDrive 'l-lock') -UserProfile $home_
        } finally { $held.Dispose() }
        $r.Code | Should -Be 0 -Because $r.Out
        $r.Out | Should -Match 'already running'
        Test-Path (Join-Path $state 'replica-cache-refresh.json') | Should -BeFalse -Because 'the running refresh owns the record'
    }
}

Describe 'the tasks are registered: install-offline-watcher.ps1, 2-windows-config.ps1 and 3-verify.ps1' {
    It 'install-offline-watcher.ps1 registers mem0-replica-cache-refresh to run travel-mode refresh, one at a time, bounded, windowless' {
        $c = script:Get-CodeText $script:installOw
        $c | Should -Match '\$rAction = New-ScheduledTaskAction -Execute ''wscript\.exe'' -Argument "//nologo `"\$hiddenVbs`" pwsh\.exe -NoProfile -ExecutionPolicy Bypass -File `"\$tm`" refresh"'
        $c | Should -Match '\$tm = Join-Path \$PSScriptRoot ''travel-mode\.ps1'''
        $c | Should -Match "Register-ScheduledTask -TaskName 'mem0-replica-cache-refresh' -Action \`$rAction -Settings \`$rSet -Force -RunLevel Limited"
        $c | Should -Match '\$rSet\s+= New-ScheduledTaskSettingsSet .*-Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit \(New-TimeSpan -Hours 2\)'
    }
    It '2-windows-config.ps1 runs install-offline-watcher.ps1 for a replica, flags a failure, and fails the install on it' {
        $c = script:Get-CodeText $script:winCfg
        $c | Should -Match "(?s)if \(\`$Role -ne 'brain'\) \{.*try \{\s*& \(Join-Path \`$RepoRootWin 'scripts\\travel\\install-offline-watcher\.ps1'\)\s*\} catch \{[^{}]*\`$script:watcherRegistrationFailed = \`$true\s*\}.*\} else \{"
        $c | Should -Match 'if \(\$script:watcherRegistrationFailed\) \{\s*throw'
    }
    It '2-windows-config.ps1 removes both replica tasks from a brain' {
        $c = script:Get-CodeText $script:winCfg
        $c | Should -Match "(?s)\} else \{\s*foreach \(\`$t in @\('mem0-offline-watcher', 'mem0-replica-cache-refresh'\)\) \{.*Unregister-ScheduledTask -TaskName \`$t"
    }
    It '3-verify.ps1 checks both tasks, and only on a replica' {
        $c = script:Get-CodeText $script:verifyPs1
        $m = [regex]::Match($c, "(?s)if \(\`$stackRole -eq 'replica'\) \{\s*Check ""replica authority is NOT loopback.*?\n\}")
        $m.Success | Should -BeTrue -Because 'the replica-only block must be findable'
        $m.Value | Should -Match "Get-ScheduledTask -TaskName 'mem0-offline-watcher'"
        $m.Value | Should -Match "Get-ScheduledTask -TaskName 'mem0-replica-cache-refresh'"
        ([regex]::Matches($c, "Get-ScheduledTask -TaskName 'mem0-offline-watcher'")).Count | Should -Be 1 -Because 'no check outside the replica block'
    }
}

Describe 'Test-MemoryStack.ps1: the takeover rows' {
    BeforeAll {
        script:Import-Functions $script:tmsPs1 @('Get-TmsWatcherVerdict', 'Get-TmsReplicaCacheVerdict')
        $script:now = [datetime]'2026-10-11T12:00:00'
        $script:nowEpoch = 1800000000L
        function script:New-Task { param([string]$State = 'Ready', [string]$Script = 'D:\r\scripts\travel\offline-watcher.ps1')
            [pscustomobject]@{ State = $State; Actions = @([pscustomobject]@{ Arguments = "//nologo `"D:\r\scripts\windows\run-hidden.vbs`" pwsh.exe -NoProfile -ExecutionPolicy Bypass -File `"$Script`"" }) } }
        function script:New-Info { param([datetime]$Last, [int64]$Result = 0) [pscustomobject]@{ LastRunTime = $Last; LastTaskResult = $Result } }
        $script:exists = { param($p) $true }
    }
    It 'FAILs an unarmed replica: no watcher means no takeover' {
        (Get-TmsWatcherVerdict -IsReplica $true -Task $null -Info $null -RefreshTaskPresent $false -Now $now)[0] | Should -Be 'FAIL'
    }
    It 'FAILs a watcher whose script is gone, and a disabled one' {
        (Get-TmsWatcherVerdict -IsReplica $true -Task (New-Task) -Info (New-Info $now) -RefreshTaskPresent $true -Now $now -PathExists { param($p) $false })[0] | Should -Be 'FAIL'
        (Get-TmsWatcherVerdict -IsReplica $true -Task (New-Task -State 'Disabled') -Info (New-Info $now) -RefreshTaskPresent $true -Now $now -PathExists $exists)[0] | Should -Be 'FAIL'
    }
    It 'WARNs when the refresh task is missing, the watcher has stalled, or its last tick failed' {
        (Get-TmsWatcherVerdict -IsReplica $true -Task (New-Task) -Info (New-Info $now) -RefreshTaskPresent $false -Now $now -PathExists $exists)[0] | Should -Be 'WARN'
        (Get-TmsWatcherVerdict -IsReplica $true -Task (New-Task) -Info (New-Info $now.AddMinutes(-30)) -RefreshTaskPresent $true -Now $now -PathExists $exists)[0] | Should -Be 'WARN'
        (Get-TmsWatcherVerdict -IsReplica $true -Task (New-Task) -Info (New-Info $now.AddMinutes(-1) 1) -RefreshTaskPresent $true -Now $now -PathExists $exists)[0] | Should -Be 'WARN'
    }
    It 'is OK for an armed replica that ticked recently, and for a tick that is running right now' {
        $v = Get-TmsWatcherVerdict -IsReplica $true -Task (New-Task) -Info (New-Info $now.AddMinutes(-1)) -RefreshTaskPresent $true -Now $now -PathExists $exists
        $v[0] | Should -Be 'OK'
        $v[1] | Should -Match 'armed'
        (Get-TmsWatcherVerdict -IsReplica $true -Task (New-Task) -Info (New-Info $now.AddSeconds(-20) 267009) -RefreshTaskPresent $true -Now $now -PathExists $exists)[0] | Should -Be 'OK'
    }
    It 'FAILs the watcher on a brain (its go_online stops the live store) and is OK without it' {
        (Get-TmsWatcherVerdict -IsReplica $false -Task (New-Task) -Info $null -RefreshTaskPresent $false -Now $now)[0] | Should -Be 'FAIL'
        (Get-TmsWatcherVerdict -IsReplica $false -Task $null -Info $null -RefreshTaskPresent $true -Now $now)[0] | Should -Be 'FAIL'
        (Get-TmsWatcherVerdict -IsReplica $false -Task $null -Info $null -RefreshTaskPresent $false -Now $now)[0] | Should -Be 'OK'
    }
    It 'cache: WARNs when never refreshed, when the last refresh failed, when it is old, or when nothing is cached' {
        (Get-TmsReplicaCacheVerdict -Record $null -NowEpoch $nowEpoch -Now $now)[0] | Should -Be 'WARN'
        $fail = [pscustomobject]@{ state = 'done'; ok = $false; finished_epoch = $nowEpoch - 3600; local_stamp = '20261009-053234'; error = 'pCloud stalled' }
        $v = Get-TmsReplicaCacheVerdict -Record $fail -NowEpoch $nowEpoch -Now $now
        $v[0] | Should -Be 'WARN'; $v[1] | Should -Match 'pCloud stalled'
        $old = [pscustomobject]@{ state = 'done'; ok = $true; finished_epoch = $nowEpoch - 40 * 3600; local_stamp = '20261011-030356' }
        (Get-TmsReplicaCacheVerdict -Record $old -NowEpoch $nowEpoch -Now $now)[0] | Should -Be 'WARN'
        $none = [pscustomobject]@{ state = 'done'; ok = $false; finished_epoch = $nowEpoch - 60; local_stamp = $null; error = 'unreachable' }
        $v = Get-TmsReplicaCacheVerdict -Record $none -NowEpoch $nowEpoch -Now $now
        $v[0] | Should -Be 'WARN'; $v[1] | Should -Match 'no complete set'; $v[1] | Should -Not -Match "''"
    }
    It 'cache: ages the set from its own file time when recorded (the authority''s time zone does not matter)' {
        $r = [pscustomobject]@{ state = 'done'; ok = $true; finished_epoch = $nowEpoch - 3600; local_stamp = '20261011-030356'; local_set_epoch = $nowEpoch - 60 * 3600 }
        $v = Get-TmsReplicaCacheVerdict -Record $r -NowEpoch $nowEpoch -Now $now
        $v[0] | Should -Be 'WARN' -Because 'the file time says 60 h, whatever the stamp''s clock reads'
        $r.local_set_epoch = $nowEpoch - 9 * 3600
        (Get-TmsReplicaCacheVerdict -Record $r -NowEpoch $nowEpoch -Now ([datetime]'2026-10-15T00:00:00'))[0] | Should -Be 'OK'
    }
    It 'cache: WARNs when the newest set on pCloud is itself old (the authority stopped backing up)' {
        $r = [pscustomobject]@{ state = 'done'; ok = $true; finished_epoch = $nowEpoch - 3600; local_stamp = '20261008-030000' }
        $v = Get-TmsReplicaCacheVerdict -Record $r -NowEpoch $nowEpoch -Now $now
        $v[0] | Should -Be 'WARN'; $v[1] | Should -Match 'nightly backup'
    }
    It 'cache: OK when a recent refresh left last night''s set, naming what a takeover restores' {
        $r = [pscustomobject]@{ state = 'done'; ok = $true; finished_epoch = $nowEpoch - 3600; local_stamp = '20261011-030356'; local_dir = 'D:\memory-backups\brain-host' }
        $v = Get-TmsReplicaCacheVerdict -Record $r -NowEpoch $nowEpoch -Now $now
        $v[0] | Should -Be 'OK'; $v[1] | Should -Match 'restores 20261011-030356'
    }
    It 'cache: a running refresh is OK for 2 h, then a WARN, and both say what a takeover would restore' {
        $v = Get-TmsReplicaCacheVerdict -Record ([pscustomobject]@{ state = 'running'; started_epoch = $nowEpoch - 600; local_stamp = '20261010-030356' }) -NowEpoch $nowEpoch -Now $now
        $v[0] | Should -Be 'OK'; $v[1] | Should -Match '20261010-030356'
        (Get-TmsReplicaCacheVerdict -Record ([pscustomobject]@{ state = 'running'; started_epoch = $nowEpoch - 3 * 3600 }) -NowEpoch $nowEpoch -Now $now)[0] | Should -Be 'WARN'
    }
    Context 'the row block, run with mocked tasks' {
        BeforeAll {
            $src = Get-Content -LiteralPath $script:tmsPs1 -Raw -Encoding UTF8
            $m = [regex]::Match($src, '(?s)(try \{\s*\$owTask = Get-ScheduledTask.*?Add-Check ''RECOVERY'' ''replica snapshot cache'' ''WARN''[^\n]*\r?\n\})')
            $m.Success | Should -BeTrue -Because 'the R2f row block must be findable'
            $script:rowBlock = $m.Groups[1].Value
            function script:Add-Check { param([string]$Dimension, [string]$Component, [string]$Status, [string]$Detail = '') $script:rows.Add([pscustomobject]@{ Component = $Component; Status = $Status; Detail = $Detail }) }
            function script:Run-Rows {
                param([bool]$Replica, [string]$UserProfile)
                $script:rows = [System.Collections.Generic.List[object]]::new()
                $TmsIsReplica = $Replica
                $saved = $env:USERPROFILE; $env:USERPROFILE = $UserProfile
                try { . ([scriptblock]::Create($script:rowBlock)) } finally { $env:USERPROFILE = $saved }
                $script:rows
            }
        }
        It 'a replica with both tasks and a fresh ok record: both rows green' {
            $h = Join-Path $TestDrive ('r' + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Force -Path (Join-Path $h '.claude\state') | Out-Null
            $ne = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            (@{ state = 'done'; ok = $true; finished_epoch = $ne - 600; local_stamp = 'x'; local_set_epoch = $ne - 3600; local_dir = 'D:\c' } | ConvertTo-Json) | Set-Content (Join-Path $h '.claude\state\replica-cache-refresh.json')
            $script:watcherScript = Join-Path $TestDrive 'offline-watcher.ps1'; Set-Content $script:watcherScript '#'
            Mock Get-ScheduledTask { if ($TaskName -eq 'mem0-offline-watcher') { New-Task -Script $script:watcherScript } else { [pscustomobject]@{ State = 'Ready' } } }
            Mock Get-ScheduledTaskInfo { New-Info (Get-Date).AddMinutes(-1) } -RemoveParameterType InputObject
            $rows = script:Run-Rows -Replica $true -UserProfile $h
            ($rows | Where-Object Component -eq 'offline watcher (takeover)').Status | Should -Be 'OK'
            ($rows | Where-Object Component -eq 'replica snapshot cache').Status | Should -Be 'OK'
        }
        It 'a replica with no watcher: FAIL, and the cache row still reports' {
            Mock Get-ScheduledTask { $null }
            $rows = script:Run-Rows -Replica $true -UserProfile (Join-Path $TestDrive 'empty-home')
            ($rows | Where-Object Component -eq 'offline watcher (takeover)').Status | Should -Be 'FAIL'
            ($rows | Where-Object Component -eq 'replica snapshot cache').Status | Should -Be 'WARN'
        }
        It 'a brain: the watcher row only, OK without the tasks' {
            Mock Get-ScheduledTask { $null }
            $rows = script:Run-Rows -Replica $false -UserProfile (Join-Path $TestDrive 'empty-home')
            ($rows | Where-Object Component -eq 'offline watcher (takeover)').Status | Should -Be 'OK'
            @($rows | Where-Object Component -eq 'replica snapshot cache').Count | Should -Be 0
        }
    }
}
