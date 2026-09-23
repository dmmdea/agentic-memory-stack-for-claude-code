# CompactorLock.Tests.ps1 - the PowerShell compactor's side of the store lock (1.31.3): the Go
# file lock ams-store.lock taken from PowerShell in ams-store's own format and rules, and the
# skipped-night counter that makes a wedged lock holder loud. PowerShell-only by nature: these
# are the transitional interop of the legacy compactor with the binary, deleted with it in
# Phase 5, so they live outside the four suites internal/porting maps to Go counterparts.
BeforeAll {
    . (Join-Path (Split-Path -Parent $PSScriptRoot) 'memory-store-lib.ps1')
}
Describe 'compactor skipped nights (a wedged lock holder must not silence the nightly forever)' {
    It 'counts distinct nights, WARNs at 2, FAILs at 4 and names the holder' {
        $sr = Join-Path $TestDrive 'skips'
        $t0 = [datetime]'2026-09-20T05:00:00'
        Register-AmCompactorSkip -StateRoot $sr -Holder 'pid 42 (ams-store, reason sync)' -Now $t0
        Register-AmCompactorSkip -StateRoot $sr -Holder 'pid 42 (ams-store, reason sync)' -Now $t0.AddMinutes(30)
        $s = Read-AmJsonFile -Path (Join-Path $sr 'compact-lock-skips.json')
        @($s.skipped_nights).Count | Should -Be 1 -Because 'two skips in one night are one skipped night'
        (Get-AmCompactorSkipVerdict -State $s).Status | Should -Be 'OK'
        Register-AmCompactorSkip -StateRoot $sr -Holder 'pid 42 (ams-store, reason sync)' -Now $t0.AddDays(1)
        $v = Get-AmCompactorSkipVerdict -State (Read-AmJsonFile -Path (Join-Path $sr 'compact-lock-skips.json'))
        $v.Status | Should -Be 'WARN'
        $v.Detail | Should -Match 'pid 42'
        Register-AmCompactorSkip -StateRoot $sr -Now $t0.AddDays(2)
        Register-AmCompactorSkip -StateRoot $sr -Now $t0.AddDays(3)
        (Get-AmCompactorSkipVerdict -State (Read-AmJsonFile -Path (Join-Path $sr 'compact-lock-skips.json'))).Status | Should -Be 'FAIL'
        Clear-AmCompactorSkips -StateRoot $sr
        (Get-AmCompactorSkipVerdict -State (Read-AmJsonFile -Path (Join-Path $sr 'compact-lock-skips.json'))).Status | Should -Be 'OK'
        (Get-AmCompactorSkipVerdict -State ([pscustomobject]@{ skipped_nights = @('a', 'b', 'c', 'd'); skips = 4 }) -TaskPresent $false).Status | Should -Be 'OK' -Because 'a retired task is not judged'
    }
}

Describe 'the Go file lock from PowerShell (Enter-AmStoreFileLock) - same file, same format, same rules' {
    It 'takes a free lock in ams-store''s JSON shape and releases only its own' {
        $sr = Join-Path $TestDrive 'fl1'
        $l = Enter-AmStoreFileLock -StateRoot $sr -Reason 'memory-compact'
        $l.Held | Should -BeTrue
        $h = Get-Content -LiteralPath (Join-Path $sr 'ams-store.lock') -Raw | ConvertFrom-Json
        [int]$h.pid | Should -Be $PID
        [int64]$h.start_time_unix | Should -BeGreaterThan 0
        $h.reason | Should -Be 'memory-compact'
        (Enter-AmStoreFileLock -StateRoot $sr).Held | Should -BeFalse -Because 'a live holder (this process) excludes a second taker'
        Exit-AmStoreFileLock -Lock $l
        Test-Path -LiteralPath (Join-Path $sr 'ams-store.lock') | Should -BeFalse
    }
    It 'breaks a stale holder (older than 10 minutes) and a recycled pid, never a live one' {
        $sr = Join-Path $TestDrive 'fl2'
        [System.IO.Directory]::CreateDirectory($sr) | Out-Null
        $p = Join-Path $sr 'ams-store.lock'
        $stale = [ordered]@{ pid = $PID; start_time_unix = 0; host = 'h'; acquired_at = [datetime]::UtcNow.AddMinutes(-11).ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ'); reason = 'sync' }
        [System.IO.File]::WriteAllText($p, ($stale | ConvertTo-Json -Compress))
        $l = Enter-AmStoreFileLock -StateRoot $sr
        $l.Held | Should -BeTrue -Because 'past the 10-minute window the holder is dead by rule'
        Exit-AmStoreFileLock -Lock $l
        Test-Path -LiteralPath ($p + '.breaking') | Should -BeFalse
    }
    It 'leaves a lock that was re-taken by someone else when releasing' {
        $sr = Join-Path $TestDrive 'fl3'
        $l = Enter-AmStoreFileLock -StateRoot $sr
        $other = [ordered]@{ pid = $PID; start_time_unix = 0; host = 'h'; acquired_at = [datetime]::UtcNow.AddSeconds(5).ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ'); reason = 'sync' }
        [System.IO.File]::WriteAllText($l.Path, ($other | ConvertTo-Json -Compress))
        Exit-AmStoreFileLock -Lock $l
        Test-Path -LiteralPath $l.Path | Should -BeTrue -Because 'the file now records another holder'
    }
}

Describe 'skipped nights run noon to noon (a retry across local midnight is one night)' {
    It 'counts 23:50 and 00:10 as one night, and the next 05:00 nightly as the second' {
        $sr = Join-Path $TestDrive 'nights'
        $d = [datetime]::new(2026, 9, 20, 23, 50, 0, [DateTimeKind]::Local)
        Register-AmCompactorSkip -StateRoot $sr -Now $d
        Register-AmCompactorSkip -StateRoot $sr -Now $d.AddMinutes(20)            # 00:10, past local midnight
        Register-AmCompactorSkip -StateRoot $sr -Now $d.AddHours(5).AddMinutes(10) # the 05:00 nightly, same night
        $s = Read-AmJsonFile -Path (Join-Path $sr 'compact-lock-skips.json')
        @($s.skipped_nights).Count | Should -Be 1
        Register-AmCompactorSkip -StateRoot $sr -Now $d.AddDays(1).AddHours(5).AddMinutes(10)
        @((Read-AmJsonFile -Path (Join-Path $sr 'compact-lock-skips.json')).skipped_nights).Count | Should -Be 2
    }
}

Describe 'git locks a killed process leaves behind (Invoke-AmGitLockRecovery)' {
    BeforeAll {
        function script:New-GitDirWithLocks([string]$Name) {
            $sr = Join-Path $TestDrive $Name
            $gd = Join-Path $sr 'history.git'
            New-Item -ItemType Directory -Force -Path (Join-Path $gd 'refs\heads') | Out-Null
            Set-Content -LiteralPath (Join-Path $gd 'HEAD') -Value 'ref: refs/heads/main'
            Set-Content -LiteralPath (Join-Path $gd 'index.lock') -Value ''
            Set-Content -LiteralPath (Join-Path $gd 'refs\heads\main.lock') -Value ''
            return $sr
        }
    }
    It 'removes every lock when no git process for the repo is alive, and records it' {
        $sr = script:New-GitDirWithLocks 'gl1'
        $other = [pscustomobject]@{ ProcessId = 5; CommandLine = 'git.exe --git-dir=D:/elsewhere/.git status' }
        $rec = Invoke-AmGitLockRecovery -StateRoot $sr -GitProcesses @($other) -Trigger 'test'
        @($rec.removed).Count | Should -Be 2
        @($rec.kept).Count | Should -Be 0
        Test-Path -LiteralPath (Join-Path $sr 'history.git\index.lock') | Should -BeFalse
        $v = Get-AmGitLockVerdict -Record (Read-AmJsonFile -Path (Join-Path $sr 'git-lock-recovery.json'))
        $v.Status | Should -Be 'WARN'
        $v.Detail | Should -Match 'index\.lock'
    }
    It 'keeps every lock while a git process for that repo is alive (or one whose command line cannot be read)' {
        $sr = script:New-GitDirWithLocks 'gl2'
        $gd = Join-Path $sr 'history.git'
        $mine = [pscustomobject]@{ ProcessId = 6; CommandLine = ('git.exe --git-dir=' + $gd.Replace([string][char]92, '/') + ' commit -q') }
        $rec = Invoke-AmGitLockRecovery -StateRoot $sr -GitProcesses @($mine)
        @($rec.removed).Count | Should -Be 0
        @($rec.kept).Count | Should -Be 2
        Test-Path -LiteralPath (Join-Path $gd 'index.lock') | Should -BeTrue
        (Get-AmGitLockVerdict -Record (Read-AmJsonFile -Path (Join-Path $sr 'git-lock-recovery.json'))).Status | Should -Be 'FAIL'
        $sr3 = script:New-GitDirWithLocks 'gl3'
        $blind = [pscustomobject]@{ ProcessId = 7; CommandLine = '' }
        @((Invoke-AmGitLockRecovery -StateRoot $sr3 -GitProcesses @($blind)).kept).Count | Should -Be 2
    }
    It 'reports a lock older than 10 minutes as a FAIL (it jams every sync), a fresh one not at all' {
        $sr = script:New-GitDirWithLocks 'gl4'
        @(Get-AmStaleGitLocks -StateRoot $sr).Count | Should -Be 0 -Because 'a fresh lock is a sync in progress'
        (Get-Item -LiteralPath (Join-Path $sr 'history.git\index.lock')).LastWriteTimeUtc = [datetime]::UtcNow.AddMinutes(-30)
        $stale = @(Get-AmStaleGitLocks -StateRoot $sr)
        $stale.Count | Should -Be 1
        (Get-AmGitLockVerdict -Record $null -StaleLocks $stale).Status | Should -Be 'FAIL'
        (Get-AmGitLockVerdict -Record $null -StaleLocks @()).Status | Should -Be 'OK'
    }
}
