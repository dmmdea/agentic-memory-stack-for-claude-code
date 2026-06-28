# codex-shim-spawn.ps1 — v0.27.1 R5: best-effort SessionStart launcher for the Codex HTTP shim.
#
# Registered as an async SessionStart hook so the shim is warm during a session
# IF — and only if — a consumer needs it. The shim is only used when the NLI
# write-gate is enabled (or a Codex contradiction re-judge is running), so spawning
# it every session by default would be pure waste. Gate the spawn on a marker file:
#
#   ~/.claude/state/codex-shim.enabled    (created by the write-gate increment)
#
# Absent  -> no-op (default; the shim never spawns, zero overhead).
# Present -> spawn the shim if it is not already listening.
#
# Self-contained on purpose (no lib dot-source for the launch path): a broken lib
# deploy must not break SessionStart, and the shim's named mutex makes a duplicate
# spawn a silent no-op. Failure here is invisible by design.

$ErrorActionPreference = 'SilentlyContinue'

# Consume the SessionStart stdin payload so the hook never blocks Claude Code's read.
try { [void][Console]::In.ReadToEnd() } catch {}

$dir = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path)
$flag = [System.IO.Path]::Combine($env:USERPROFILE, '.claude', 'state', 'codex-shim.enabled')
if (-not [System.IO.File]::Exists($flag)) { exit 0 }

# Resolve the port (env override, else the reserved default 18792).
$port = 18792
if ($env:MEM0_CODEX_SHIM_PORT) {
    $p = 0
    if ([int]::TryParse($env:MEM0_CODEX_SHIM_PORT, [ref]$p) -and $p -gt 0 -and $p -lt 65536) { $port = $p }
}

# Already listening? (fast loopback TCP probe — avoids spawning a mutex-no-op process.)
$listening = $false
try {
    $tcp = [System.Net.Sockets.TcpClient]::new()
    $iar = $tcp.BeginConnect('127.0.0.1', $port, $null, $null)
    if ($iar.AsyncWaitHandle.WaitOne(300)) {
        try { $tcp.EndConnect($iar); $listening = $true } catch { $listening = $false }
    }
    $tcp.Close()
} catch { $listening = $false }

if (-not $listening) {
    $shim = [System.IO.Path]::Combine($dir, 'codex-shim.ps1')
    if ([System.IO.File]::Exists($shim)) {
        try {
            # UseShellExecute=$true: no inherited std handles, so the detached shim
            # cannot hold this hook's stdout open and stall SessionStart.
            $psi = [System.Diagnostics.ProcessStartInfo]::new()
            $psi.FileName = $env:SystemRoot + '\System32\WindowsPowerShell\v1.0\powershell.exe'
            $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $shim + '"'
            $psi.UseShellExecute = $true
            $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
            $proc = [System.Diagnostics.Process]::Start($psi)
            if ($proc) { $proc.Dispose() }
        } catch {}
    }
}
exit 0
