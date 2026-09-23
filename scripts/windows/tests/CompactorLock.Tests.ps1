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
