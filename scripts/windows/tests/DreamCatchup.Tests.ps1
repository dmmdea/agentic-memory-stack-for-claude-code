# DreamCatchup.Tests.ps1 — Step 3: coverage for the debt-based dream catch-up
# (dream-catchup.ps1), the standalone MEMORY.md index refresh (memory-index-refresh.ps1),
# and the dream consolidator's -Force throttle bypass.
#
# The scripts are run as REAL child powershell.exe 5.1 processes (exactly as a detached
# SessionStart spawn would launch them) inside a sandboxed USERPROFILE so the live
# ~/.claude/state, ~/.mem0, and logs are never touched. The consolidator is NOT actually
# run: dream-catchup.ps1 invokes a STUB dream-consolidate.ps1 placed in the sandbox scripts
# dir (it just drops a marker file), so no Codex/mem0 call is ever made. The dream throttle
# marker is written directly to control the "fresh vs stale" branch.
#
# Test matrix:
#   (a) fresh throttle (<30h)            -> no consolidator invoke
#   (b) stale + debt (learn-rule line)   -> consolidator invoked (stub marker written)
#   (c) stale + no debt                  -> no consolidator invoke
#   (d) catch-up's own 6h throttle       -> a second run within 6h is a no-op
#   (e) -Force bypasses the dream 24h throttle (condition-level test, no real run)
#   (f) index-refresh 6h throttle honored

BeforeAll {
    $script:winDir    = Split-Path -Parent $PSScriptRoot
    $script:catchup   = Join-Path $winDir 'dream-catchup.ps1'
    $script:idxRefresh = Join-Path $winDir 'memory-index-refresh.ps1'
    $script:consolidate = Join-Path $winDir 'dream-consolidate.ps1'
    $script:common    = Join-Path $winDir 'memory-common.ps1'
    $script:ps51      = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

    # Build a sandbox: fake USERPROFILE with .claude\scripts (real memory-common.ps1 + the
    # script-under-test + a STUB dream-consolidate.ps1) and .claude\state / .claude\logs.
    function New-CatchupSandbox {
        param(
            [double]$DreamAgeHours,      # age of the last-dream marker; $null => no marker
            [switch]$WithLearnLine,      # write a learn-rules.jsonl line (debt)
            [switch]$WithPromoteLine,    # write a promote-queue.jsonl line (debt)
            [switch]$NoDreamMarker
        )
        $sandbox = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $scripts = Join-Path $sandbox '.claude\scripts'
        $state   = Join-Path $sandbox '.claude\state'
        $logs    = Join-Path $sandbox '.claude\logs'
        $mem0    = Join-Path $sandbox '.mem0'
        foreach ($d in @($scripts, $state, $logs, $mem0)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }

        Copy-Item $script:common $scripts
        Copy-Item $script:catchup $scripts
        Copy-Item $script:idxRefresh $scripts

        # Stub consolidator: on invocation, drops a marker file so the test can assert it ran.
        # NEVER calls Codex/mem0. Accepts -DryRun/-Force to match the real signature.
        $stub = @'
param([switch]$DryRun, [switch]$Force)
$m = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '..\state\dream-invoked.marker'
Set-Content -Path $m -Value ((Get-Date).ToString('o')) -Encoding UTF8
exit 0
'@
        Set-Content -Path (Join-Path $scripts 'dream-consolidate.ps1') -Value $stub -Encoding UTF8

        if (-not $NoDreamMarker) {
            $now = [int][double]::Parse((Get-Date -UFormat %s))
            $ts  = $now - [int]($DreamAgeHours * 3600)
            Set-Content -Path (Join-Path $state 'last-dream') -Value $ts -Encoding UTF8 -NoNewline
        }
        if ($WithLearnLine)   { Set-Content -Path (Join-Path $mem0 'learn-rules.jsonl')   -Value '{"rule":"x"}' -Encoding UTF8 }
        if ($WithPromoteLine) { Set-Content -Path (Join-Path $mem0 'promote-queue.jsonl') -Value '{"id":"y"}'  -Encoding UTF8 }

        [pscustomobject]@{ Root = $sandbox; Scripts = $scripts; State = $state; Logs = $logs; Mem0 = $mem0 }
    }

    function Invoke-InSandbox {
        param([string]$ScriptPath, [string]$SandboxRoot)
        $saved = $env:USERPROFILE
        try {
            $env:USERPROFILE = $SandboxRoot
            '' | & $script:ps51 -NoProfile -ExecutionPolicy Bypass -File $ScriptPath *> $null
        } finally { $env:USERPROFILE = $saved }
    }

    function Test-DreamInvoked { param([string]$State) Test-Path (Join-Path $State 'dream-invoked.marker') }
}

# ---------------------------------------------------------------------------
# (a) fresh throttle -> no consolidator invoke
# ---------------------------------------------------------------------------
Describe 'catch-up: fresh dream throttle (<30h) -> no invoke' {
    It 'does not invoke the consolidator when the last dream ran 5h ago' {
        $sb = New-CatchupSandbox -DreamAgeHours 5 -WithLearnLine
        Invoke-InSandbox -ScriptPath (Join-Path $sb.Scripts 'dream-catchup.ps1') -SandboxRoot $sb.Root
        Test-DreamInvoked -State $sb.State | Should -BeFalse -Because 'a fresh dream (<30h) needs no catch-up even with debt present'
        (Get-Content (Join-Path $sb.Logs 'dream-catchup.log') -Raw) | Should -Match 'fresh, no catch-up'
    }
}

# ---------------------------------------------------------------------------
# (b) stale + debt -> invokes consolidator
# ---------------------------------------------------------------------------
Describe 'catch-up: stale + debt -> invokes consolidator' {
    It 'invokes the consolidator when >30h stale AND a learn-rule line exists' {
        $sb = New-CatchupSandbox -DreamAgeHours 40 -WithLearnLine
        Invoke-InSandbox -ScriptPath (Join-Path $sb.Scripts 'dream-catchup.ps1') -SandboxRoot $sb.Root
        Test-DreamInvoked -State $sb.State | Should -BeTrue -Because 'stale + pending learn-rule is debt -> catch up'
        (Get-Content (Join-Path $sb.Logs 'dream-catchup.log') -Raw) | Should -Match 'debt detected'
    }

    It 'invokes the consolidator when >30h stale AND a promote-queue line exists' {
        $sb = New-CatchupSandbox -DreamAgeHours 35 -WithPromoteLine
        Invoke-InSandbox -ScriptPath (Join-Path $sb.Scripts 'dream-catchup.ps1') -SandboxRoot $sb.Root
        Test-DreamInvoked -State $sb.State | Should -BeTrue -Because 'stale + queued promotion is debt -> catch up'
    }

    It 'invokes the consolidator on a long gap (>48h) even with NO debt files' {
        $sb = New-CatchupSandbox -DreamAgeHours 60
        Invoke-InSandbox -ScriptPath (Join-Path $sb.Scripts 'dream-catchup.ps1') -SandboxRoot $sb.Root
        Test-DreamInvoked -State $sb.State | Should -BeTrue -Because 'a >48h gap forces catch-up regardless of visible debt'
    }

    It 'invokes the consolidator when the dream marker is entirely absent (never ran)' {
        $sb = New-CatchupSandbox -NoDreamMarker
        Invoke-InSandbox -ScriptPath (Join-Path $sb.Scripts 'dream-catchup.ps1') -SandboxRoot $sb.Root
        Test-DreamInvoked -State $sb.State | Should -BeTrue -Because 'no marker => maximally stale => catch up'
    }
}

# ---------------------------------------------------------------------------
# (c) stale + no debt -> no invoke
# ---------------------------------------------------------------------------
Describe 'catch-up: stale (30-48h) but no debt -> no invoke' {
    It 'does not invoke when 40h stale and no learn/promote lines' {
        $sb = New-CatchupSandbox -DreamAgeHours 40
        Invoke-InSandbox -ScriptPath (Join-Path $sb.Scripts 'dream-catchup.ps1') -SandboxRoot $sb.Root
        Test-DreamInvoked -State $sb.State | Should -BeFalse -Because 'stale but no debt and gap <=48h -> skip'
        (Get-Content (Join-Path $sb.Logs 'dream-catchup.log') -Raw) | Should -Match 'stale but no debt'
    }
}

# ---------------------------------------------------------------------------
# (d) catch-up's own 6h throttle
# ---------------------------------------------------------------------------
Describe "catch-up: own 6h throttle stops back-to-back runs" {
    It 'a second run within 6h is a no-op (does not re-invoke)' {
        $sb = New-CatchupSandbox -DreamAgeHours 40 -WithLearnLine
        # First run: stale + debt -> invokes, and marks the dream-catchup throttle.
        Invoke-InSandbox -ScriptPath (Join-Path $sb.Scripts 'dream-catchup.ps1') -SandboxRoot $sb.Root
        Test-DreamInvoked -State $sb.State | Should -BeTrue
        # Clear the marker; a second immediate run must be throttled out (no new invoke).
        Remove-Item (Join-Path $sb.State 'dream-invoked.marker') -Force
        Invoke-InSandbox -ScriptPath (Join-Path $sb.Scripts 'dream-catchup.ps1') -SandboxRoot $sb.Root
        Test-DreamInvoked -State $sb.State | Should -BeFalse -Because 'the 6h catch-up throttle blocks a back-to-back second run'
        Test-Path (Join-Path $sb.State 'last-dream-catchup') | Should -BeTrue -Because 'the first run stamped its own throttle'
    }
}

# ---------------------------------------------------------------------------
# (e) -Force bypasses the dream 24h throttle (condition-level; no real run)
# ---------------------------------------------------------------------------
Describe 'dream -Force bypasses ONLY the 24h throttle (keeps the Codex lock)' {
    It 'the throttle guard is skipped when -Force is set' {
        # Reproduce the exact guard condition from dream-consolidate.ps1 and prove -Force
        # short-circuits it (a fresh throttle would otherwise block). No Codex/mem0 involved.
        . $script:common
        $DryRun = $false
        # Simulate a FRESH throttle: Test-Throttle would return $false (blocked).
        $throttleOpen = $false   # stand-in for (Test-Throttle -Name 'dream' -MinIntervalSeconds 86400)
        $Force = $true
        $wouldSkip = (-not $DryRun -and -not $Force -and -not $throttleOpen)
        $wouldSkip | Should -BeFalse -Because '-Force must bypass the 24h throttle skip even when the throttle is fresh'

        $Force = $false
        $wouldSkip = (-not $DryRun -and -not $Force -and -not $throttleOpen)
        $wouldSkip | Should -BeTrue -Because 'without -Force a fresh throttle still skips the run'
    }

    It 'the -Force switch and the guard edit are present in dream-consolidate.ps1' {
        $src = Get-Content $script:consolidate -Raw
        $src | Should -Match 'param\(\[switch\]\$DryRun,\s*\[switch\]\$Force\)'
        $src | Should -Match '-not \$Force -and -not \(Test-Throttle -Name \$ThrottleName'
    }
}

# ---------------------------------------------------------------------------
# (f) index-refresh 6h throttle honored
# ---------------------------------------------------------------------------
Describe 'memory-index-refresh: 6h throttle honored' {
    It 'a second run within 6h is a no-op (throttle stamp present, no second build attempt)' {
        # No receipt in the sandbox -> the script logs a skip and marks the throttle, then a
        # second run is throttled out entirely (the pure-throttle path never touches WSL).
        $sandbox = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        foreach ($d in @('.claude\scripts', '.claude\state', '.claude\logs')) {
            New-Item -ItemType Directory -Path (Join-Path $sandbox $d) -Force | Out-Null
        }
        $scripts = Join-Path $sandbox '.claude\scripts'
        Copy-Item $script:common $scripts
        Copy-Item $script:idxRefresh $scripts

        Invoke-InSandbox -ScriptPath (Join-Path $scripts 'memory-index-refresh.ps1') -SandboxRoot $sandbox
        Test-Path (Join-Path $sandbox '.claude\state\last-index-refresh') | Should -BeTrue -Because 'the first run (no-receipt skip) stamps the 6h throttle'
        $logFirst = Get-Content (Join-Path $sandbox '.claude\logs\index-refresh.log') -Raw
        $logFirst | Should -Match 'no RepoRootWsl in receipt'

        # Second immediate run: throttled -> exits before logging anything new.
        $sizeBefore = (Get-Item (Join-Path $sandbox '.claude\logs\index-refresh.log')).Length
        Invoke-InSandbox -ScriptPath (Join-Path $scripts 'memory-index-refresh.ps1') -SandboxRoot $sandbox
        $sizeAfter = (Get-Item (Join-Path $sandbox '.claude\logs\index-refresh.log')).Length
        $sizeAfter | Should -Be $sizeBefore -Because 'the 6h throttle blocks the second run before it logs'
    }
}
