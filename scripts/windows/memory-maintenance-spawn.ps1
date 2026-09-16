# memory-maintenance-spawn.ps1 - SessionStart hook entry point for the maintenance jobs.
# Detach-spawns dream-catchup.ps1 (debt-based dream catch-up), memory-index-refresh.ps1 (the
# mem0-side MEMORY.md index, decoupled from the dream), memory-lint.ps1 (read-only health scan
# of the harness-native per-workspace auto-memory stores) and - P4-1a, 2026-09-16 - the store
# binary's resident watcher, `ams-store sync --watch`: one per PC (it holds the
# Local\ams-store-watch singleton; a second instance exits 0 at once), wakes on the dirty marker
# the gate leaves, checks the hub only while a session is live, and exits by itself when none
# is. Every child is hidden, then this script exits immediately so the hook never holds the
# session. Same ProcessStartInfo pattern as mem0-hook-daemon-spawn.ps1. Each child carries its
# own throttle, debt gate or singleton, so a burst of session starts costs four no-op spawns at
# worst. Fail-open: any error here is swallowed.
#
# The compactor catch-up child (2026-09-06) is gone: the nightly PowerShell compactor is retired
# by the installer once a PC's hub path is proven (register P4-1, plan Q-A) - the gate keeps the
# index under the caps at write time, the watcher carries every local commit to the hub, and the
# hub's nightly judge replaces the local Codex judge.
#
# NOTE the two different MEMORY.md files: memory-index-refresh.ps1 rebuilds the mem0 corpus
# index (~/.mem0/MEMORY.md, "System B"); memory-lint.ps1 inspects the harness's own
# per-workspace stores (~/.claude/projects/<ws>/memory/, "System A") and never writes to them.
$ErrorActionPreference = 'SilentlyContinue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PowerShell51 = $env:SystemRoot + '\System32\WindowsPowerShell\v1.0\powershell.exe'

$children = @()
foreach ($f in @('dream-catchup.ps1', 'memory-index-refresh.ps1')) {
    $target = Join-Path $ScriptDir $f
    if (Test-Path $target) {
        $children += @{ Exe = $PowerShell51; Args = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $target + '"' }
    }
}
# The store binary and the hub's MagicDNS name. The hub comes from the installer's receipt beside
# this script (the same file the other deployed scripts read their install values from) and is
# re-validated here because it lands on a command line.
$exe = Join-Path $ScriptDir 'ams-store.exe'
$hub = ''
try {
    $receipt = Join-Path $ScriptDir 'mem0-stack.config.psd1'
    if (Test-Path $receipt) { $hub = [string](Import-PowerShellDataFile $receipt).HubHost }
} catch {}
if (-not ($hub -match '^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$')) { $hub = '' }
# P4-1c (2026-09-16): the store lint is the binary's. It writes the same summary file the
# PowerShell lint wrote, plus the G7 clock per store (over_trigger_hours, from the over-trigger
# stamp synced through the hub) that the session-start banner reports; the PowerShell lint
# writes null there. Its own 6 h throttle keeps a burst of session starts cheap. memory-lint.ps1
# is the fallback only while the binary is absent (an aborted install), so a box is never
# without a lint.
if (Test-Path $exe) {
    $stateRoot = Join-Path (Split-Path -Parent $ScriptDir) 'state\automemory'
    $lintArgs = 'lint --summary-out "' + (Join-Path $stateRoot 'lint-summary.json') + '"'
    if ($hub) { $lintArgs = $lintArgs + ' --hub-host ' + $hub }
    $children += @{ Exe = $exe; Args = $lintArgs }
} elseif (Test-Path (Join-Path $ScriptDir 'memory-lint.ps1')) {
    $children += @{ Exe = $PowerShell51; Args = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $ScriptDir 'memory-lint.ps1') + '"' }
}
# The watcher. No hub, no watcher: there is nothing to propagate to.
if ((Test-Path $exe) -and $hub) {
    $children += @{ Exe = $exe; Args = 'sync --watch --hub-host ' + $hub }
}

foreach ($child in $children) {
    try {
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $child.Exe
        $psi.Arguments = $child.Args
        $psi.UseShellExecute = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $proc = [System.Diagnostics.Process]::Start($psi)
        if ($proc) { $proc.Dispose() }
    } catch {}
}
exit 0
