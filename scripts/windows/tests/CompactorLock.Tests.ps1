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
        function script:New-StoreGitDir([string]$Name, [int]$AgeMinutes = 5) {
            # a real-shaped git dir: HEAD + objects/ + refs/, and git's own lock names
            $sr = Join-Path $TestDrive $Name
            $gd = Join-Path $sr 'history.git'
            New-Item -ItemType Directory -Force -Path (Join-Path $gd 'refs\heads'), (Join-Path $gd 'objects') | Out-Null
            Set-Content -LiteralPath (Join-Path $gd 'HEAD') -Value 'ref: refs/heads/main'
            foreach ($f in @('index.lock', 'refs\heads\main.lock')) {
                $p = Join-Path $gd $f
                Set-Content -LiteralPath $p -Value ''
                (Get-Item -LiteralPath $p).LastWriteTimeUtc = [datetime]::UtcNow.AddMinutes(-$AgeMinutes)
            }
            return $sr
        }
        $script:noGit = { @() }
    }
    It 'removes git''s lock files when NO git.exe of this user is alive, and records it' {
        $sr = script:New-StoreGitDir 'gl1'
        Set-Content -LiteralPath (Join-Path $sr 'history.git\objects\pack-x.lock') -Value ''   # not a git lock name: never touched
        (Get-Item -LiteralPath (Join-Path $sr 'history.git\objects\pack-x.lock')).LastWriteTimeUtc = [datetime]::UtcNow.AddMinutes(-5)
        $rec = Invoke-AmGitLockRecovery -StateRoot $sr -GetGitProcesses $script:noGit -Trigger 'test'
        @($rec.removed).Count | Should -Be 2
        Test-Path -LiteralPath (Join-Path $sr 'history.git\index.lock') | Should -BeFalse
        Test-Path -LiteralPath (Join-Path $sr 'history.git\objects\pack-x.lock') | Should -BeTrue -Because 'only index/HEAD/config/packed-refs/shallow.lock and refs/**/*.lock are git locks'
        $v = Get-AmGitLockVerdict -Record (Read-AmJsonFile -Path (Join-Path $sr 'git-lock-recovery.json'))
        $v.Status | Should -Be 'WARN'
        $v.Detail | Should -Match 'index\.lock'
    }
    It 'keeps every lock while ANY git.exe of this user is alive - whatever its command line says' {
        $sr = script:New-StoreGitDir 'gl2'
        # a cwd-only / GIT_DIR-env invocation names no git dir at all; it must still protect the lock
        $rec = Invoke-AmGitLockRecovery -StateRoot $sr -GetGitProcesses { @([pscustomobject]@{ ProcessId = 77; CommandLine = 'git.exe gc --auto' }) }
        @($rec.removed).Count | Should -Be 0
        @($rec.kept).Count | Should -Be 2
        ($rec.kept[0].reason) | Should -Match '77'
        Test-Path -LiteralPath (Join-Path $sr 'history.git\index.lock') | Should -BeTrue
    }
    It 're-queries the git processes before EACH delete (a git that starts mid-sweep keeps the rest)' {
        $sr = script:New-StoreGitDir 'gl3'
        $script:q = 0
        $rec = Invoke-AmGitLockRecovery -StateRoot $sr -GetGitProcesses { $script:q++; if ($script:q -ge 2) { @([pscustomobject]@{ ProcessId = 88; CommandLine = 'git.exe commit' }) } else { @() } }
        @($rec.removed).Count | Should -Be 1 -Because 'the first delete saw no git; the second saw pid 88 start'
        @($rec.kept).Count | Should -Be 1
        $script:q | Should -BeGreaterOrEqual 2
    }
    It 'leaves a lock younger than the grace alone (a just-started git is not raced), and waits for it when asked' {
        $sr = script:New-StoreGitDir 'gl4' -AgeMinutes 0
        $rec = Invoke-AmGitLockRecovery -StateRoot $sr -GetGitProcesses $script:noGit
        @($rec.removed).Count | Should -Be 0
        $rec.kept[0].reason | Should -Match 'younger than'
        $script:slept = 0
        $rec2 = Invoke-AmGitLockRecovery -StateRoot $sr -GetGitProcesses $script:noGit -WaitForGrace -NowUtc ([datetime]::UtcNow) `
            -Sleep { param($ms) $script:slept += $ms; foreach ($l in @(Get-ChildItem -LiteralPath (Join-Path $sr 'history.git') -Recurse -Filter '*.lock')) { $l.LastWriteTimeUtc = [datetime]::UtcNow.AddMinutes(-3) } }
        $script:slept | Should -BeGreaterThan 0
        @($rec2.removed).Count | Should -Be 2
    }
    It 'never follows a junction: a git dir or a refs subtree that is a reparse point is skipped' {
        $sr = script:New-StoreGitDir 'gl5'
        $outside = script:New-StoreGitDir 'outside-target'
        $j = Join-Path $sr 'evil.git'
        New-Item -ItemType Junction -Path $j -Target (Join-Path $outside 'history.git') | Out-Null
        $j2 = Join-Path $sr 'history.git\refs\linked'
        New-Item -ItemType Junction -Path $j2 -Target (Join-Path $outside 'history.git\refs\heads') | Out-Null
        $rec = Invoke-AmGitLockRecovery -StateRoot $sr -GetGitProcesses $script:noGit
        Test-Path -LiteralPath (Join-Path $outside 'history.git\index.lock') | Should -BeTrue -Because 'a junctioned git dir is never entered'
        Test-Path -LiteralPath (Join-Path $outside 'history.git\refs\heads\main.lock') | Should -BeTrue -Because 'a junctioned refs subtree is never entered'
        @($rec.removed | Where-Object { $_ -like '*outside-target*' -or $_ -like '*evil.git*' -or $_ -like '*linked*' }).Count | Should -Be 0
        @($rec.removed).Count | Should -Be 2
    }
    It 'Test-AmPathInsideRoot refuses a path outside the canonical root or through a reparse point' {
        $sr = script:New-StoreGitDir 'gl6'
        $outside = script:New-StoreGitDir 'gl6-out'
        $j = Join-Path $sr 'via'
        New-Item -ItemType Junction -Path $j -Target $outside | Out-Null
        Test-AmPathInsideRoot -Path (Join-Path $sr 'history.git\index.lock') -Root $sr | Should -BeTrue
        Test-AmPathInsideRoot -Path (Join-Path $outside 'history.git\index.lock') -Root $sr | Should -BeFalse
        Test-AmPathInsideRoot -Path (Join-Path $j 'history.git\index.lock') -Root $sr | Should -BeFalse
        Test-AmPathInsideRoot -Path (Join-Path $sr '..\gl6-out\history.git\index.lock') -Root $sr | Should -BeFalse
    }
    It 'a directory with HEAD but no objects/ or refs/ is not a git dir' {
        $sr = Join-Path $TestDrive 'gl7'
        New-Item -ItemType Directory -Force -Path (Join-Path $sr 'fake.git') | Out-Null
        Set-Content -LiteralPath (Join-Path $sr 'fake.git\HEAD') -Value 'x'
        Set-Content -LiteralPath (Join-Path $sr 'fake.git\index.lock') -Value ''
        @(Get-AmStoreGitDirs -StateRoot $sr).Count | Should -Be 0
    }
}

Describe 'the git-lock self-test row judges an old lock by whether any git is alive' {
    It 'old lock and no git alive = FAIL; old lock with a git alive = WARN naming the pid; fresh lock = OK' {
        $sr = Join-Path $TestDrive 'glv'
        $gd = Join-Path $sr 'history.git'
        New-Item -ItemType Directory -Force -Path (Join-Path $gd 'refs'), (Join-Path $gd 'objects') | Out-Null
        Set-Content -LiteralPath (Join-Path $gd 'HEAD') -Value 'ref: refs/heads/main'
        Set-Content -LiteralPath (Join-Path $gd 'index.lock') -Value ''
        @(Get-AmStaleGitLocks -StateRoot $sr).Count | Should -Be 0 -Because 'a fresh lock is a sync in progress'
        (Get-AmGitLockVerdict -Record $null -StaleLocks @(Get-AmStaleGitLocks -StateRoot $sr) -GitProcesses @()).Status | Should -Be 'OK'
        (Get-Item -LiteralPath (Join-Path $gd 'index.lock')).LastWriteTimeUtc = [datetime]::UtcNow.AddMinutes(-30)
        $stale = @(Get-AmStaleGitLocks -StateRoot $sr)
        $stale.Count | Should -Be 1
        (Get-AmGitLockVerdict -Record $null -StaleLocks $stale -GitProcesses @()).Status | Should -Be 'FAIL'
        $w = Get-AmGitLockVerdict -Record $null -StaleLocks $stale -GitProcesses @([pscustomobject]@{ ProcessId = 4242; CommandLine = 'git.exe gc' })
        $w.Status | Should -Be 'WARN' -Because 'a long gc or a slow push holds its lock legitimately'
        $w.Detail | Should -Match '4242'
    }
}
