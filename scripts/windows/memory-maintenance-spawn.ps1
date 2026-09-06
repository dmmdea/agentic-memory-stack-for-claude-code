# memory-maintenance-spawn.ps1 - SessionStart hook entry point for the Step-3 robustness
# jobs. Detach-spawns dream-catchup.ps1 (debt-based dream catch-up),
# memory-index-refresh.ps1 (the mem0-side MEMORY.md index, decoupled from the dream),
# memory-lint.ps1 (read-only health scan of the harness-native per-workspace auto-memory
# stores) and memory-compact.ps1 -CatchUp (2026-09-06: runs the nightly compaction only when
# its newest receipt is older than 24h - the 5am task missed a night because the box was off
# and the scheduler's missed-start retry found no user logged on; nothing else re-ran it)
# hidden, then exits immediately so the hook never holds the session. Same ProcessStartInfo
# pattern as mem0-hook-daemon-spawn.ps1. Each child carries its own throttle or debt gate, so
# a burst of session starts costs four no-op spawns at worst. Fail-open: any error here is
# swallowed.
#
# NOTE the two different MEMORY.md files: memory-index-refresh.ps1 rebuilds the mem0 corpus
# index (~/.mem0/MEMORY.md, "System B"); memory-lint.ps1 inspects the harness's own
# per-workspace stores (~/.claude/projects/<ws>/memory/, "System A") and never writes to them.
$ErrorActionPreference = 'SilentlyContinue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$children = @(
    @{ File = 'dream-catchup.ps1';        Args = '' }
    @{ File = 'memory-index-refresh.ps1'; Args = '' }
    @{ File = 'memory-lint.ps1';          Args = '' }
    @{ File = 'memory-compact.ps1';       Args = ' -CatchUp' }
)
foreach ($child in $children) {
    try {
        $target = Join-Path $ScriptDir $child.File
        if (-not (Test-Path $target)) { continue }
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $env:SystemRoot + '\System32\WindowsPowerShell\v1.0\powershell.exe'
        $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $target + '"' + $child.Args
        $psi.UseShellExecute = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $proc = [System.Diagnostics.Process]::Start($psi)
        if ($proc) { $proc.Dispose() }
    } catch {}
}
exit 0
