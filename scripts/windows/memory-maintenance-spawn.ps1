# memory-maintenance-spawn.ps1 - SessionStart hook entry point for the Step-3 robustness
# jobs. Detach-spawns dream-catchup.ps1 (debt-based dream catch-up) and
# memory-index-refresh.ps1 (MEMORY.md index, decoupled from the dream) hidden, then exits
# immediately so the hook never holds the session. Same ProcessStartInfo pattern as
# mem0-hook-daemon-spawn.ps1. Each child carries its own throttle, so a burst of session
# starts costs two no-op spawns at worst. Fail-open: any error here is swallowed.
$ErrorActionPreference = 'SilentlyContinue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

foreach ($child in 'dream-catchup.ps1', 'memory-index-refresh.ps1') {
    try {
        $target = Join-Path $ScriptDir $child
        if (-not (Test-Path $target)) { continue }
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $env:SystemRoot + '\System32\WindowsPowerShell\v1.0\powershell.exe'
        $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $target + '"'
        $psi.UseShellExecute = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $proc = [System.Diagnostics.Process]::Start($psi)
        if ($proc) { $proc.Dispose() }
    } catch {}
}
exit 0
