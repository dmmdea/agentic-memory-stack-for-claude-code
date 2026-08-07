# codex-shim-spawn.ps1 — v0.27.1 R5: best-effort SessionStart launcher for the Codex HTTP shim.
#
# Registered as an async SessionStart hook so the shim is warm during a session.
#
# AMS-07 (W4, audit P1): this spawn used to require
# ~/.claude/state/codex-shim.enabled — a flag whose ONLY documented creator was
# the manual "turn the NLI write-gate on" step, which was never performed.
# NOTHING in either repo creates it. So the shim never spawned, and BOTH of its
# consumers died silently with it:
#   * the session-time contradiction RE-JUDGE (storage-cap-check.sh probes
#     :18792 and skips when it is down), and
#   * the weekly DISCOVERY sweep (7/7 receipts read no-op:codex-shim-unreachable).
# Coupling a general judgment service to an unrelated feature flag is what made
# a P1 capability dead-on-arrival for its whole life.
#
# The gate is now OPT-OUT. Both consumers are always registered, so the shim is
# always wanted; the flag file below exists for anyone who deliberately wants
# the old zero-listener posture:
#
#   ~/.claude/state/codex-shim.disabled   (present -> never spawn)
#
# POSTURE NOTE (deliberate, recorded in the program ledger): the shim now
# listens on loopback :18792 for up to its idle life (~4h) after each session
# start, where before it never listened at all. It is loopback-only, its /judge
# verb requires the mem0 API key under a constant-time compare, /health returns
# nothing sensitive, and a named singleton mutex means parallel sessions cannot
# accumulate processes. The exposure it adds is that a local process able to
# read ~/.mem0/api-key can spend the Codex subscription — unchanged in kind
# from every other stack component that holds that key.
#
# Self-contained on purpose (no lib dot-source for the launch path): a broken lib
# deploy must not break SessionStart, and the shim's named mutex makes a duplicate
# spawn a silent no-op. Failure here is invisible by design.

$ErrorActionPreference = 'SilentlyContinue'

# Consume the SessionStart stdin payload so the hook never blocks Claude Code's read.
try { [void][Console]::In.ReadToEnd() } catch {}

$dir = [System.IO.Path]::GetDirectoryName($MyInvocation.MyCommand.Path)
$optOut = [System.IO.Path]::Combine($env:USERPROFILE, '.claude', 'state', 'codex-shim.disabled')
if ([System.IO.File]::Exists($optOut)) { exit 0 }

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
