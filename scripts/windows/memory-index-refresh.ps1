# memory-index-refresh.ps1 — standalone MEMORY.md index refresh, DECOUPLED from the dream.
#
# The nightly dream rebuilds MEMORY.md as its phase-4 step. That couples the index to the
# whole consolidation: if the dream is down (PC off, Codex hung, catch-up not yet run) the
# index freezes too, so a session starts against a stale MEMORY.md even though the underlying
# store moved on. This script runs the SAME index build the dream invokes, on its OWN cheap
# throttle — zero Codex, local Qdrant only. Spawned DETACHED from a SessionStart hook (like
# mem0-hook-daemon-spawn.ps1); it exits fast and fails open (a failure must NEVER block a
# session). The dream's own phase-4 index invocation stays as-is — this is additive.

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Operator receipt — resolve operator-specific paths (venv python + distro + repo root)
# exactly as dream-consolidate.ps1 does, so this refresh is operator-agnostic. Written by
# install/2-windows-config.ps1; live fallback if the receipt is absent. Mirrors the dream's
# $DcCfg block verbatim so the two invocations can never drift.
$IrCfgPath = Join-Path $env:USERPROFILE '.claude\scripts\mem0-stack.config.psd1'
$IrCfg = $null
try { if (Test-Path $IrCfgPath) { $IrCfg = Import-PowerShellDataFile $IrCfgPath } } catch { $IrCfg = $null }
$IrWslUser = if ($IrCfg -and $IrCfg.WslUser) { $IrCfg.WslUser } else { try { ([string](wsl.exe -e bash -lc 'printf %s "$USER"')).Trim() } catch { $env:USERNAME } }
$IrDistro  = if ($IrCfg -and $IrCfg.Distro)  { $IrCfg.Distro } else {
    $prevEnc = [Console]::OutputEncoding
    try { [Console]::OutputEncoding = [System.Text.Encoding]::Unicode; (wsl.exe -l -q | Where-Object { $_.Trim() } | Select-Object -First 1).Trim() } finally { [Console]::OutputEncoding = $prevEnc }
}
$IrRepoWsl = if ($IrCfg -and $IrCfg.RepoRootWsl) { $IrCfg.RepoRootWsl } else { '' }

# Dot-source the shared helpers (Test-Throttle / Mark-Throttle / Write-MemoryLog /
# Initialize-MemoryEnv). A broken lib deploy must not break SessionStart — guard it and
# exit 0 (fail-open, no refresh this session).
try {
    . (Join-Path $ScriptDir 'memory-common.ps1')
    Initialize-MemoryEnv
} catch { exit 0 }

# Own 6h throttle so a burst of session starts fires at most one refresh.
if (-not (Test-Throttle -Name 'index-refresh' -MinIntervalSeconds 21600)) {
    exit 0
}

try {
    # No RepoRootWsl in the receipt -> we cannot locate the index builder. Log + exit
    # (same guard the dream uses in phase 4).
    if ([string]::IsNullOrWhiteSpace($IrRepoWsl)) {
        Write-MemoryLog -Component 'index-refresh' -Message 'skip: no RepoRootWsl in receipt (run install to write ~/.claude/scripts/mem0-stack.config.psd1)'
        Mark-Throttle -Name 'index-refresh'
        exit 0
    }

    # Invoke memory-index-build.py EXACTLY as dream-consolidate.ps1 phase 4 does:
    # <venv python> $IrRepoWsl/scripts/wsl/memory-index-build.py, via wsl.exe -d <distro>.
    $indexResult = wsl.exe -d $IrDistro -e bash -c "/home/$IrWslUser/apps/mem0-server/.venv/bin/python $IrRepoWsl/scripts/wsl/memory-index-build.py 2>&1"
    $indexExit = $LASTEXITCODE
    Write-MemoryLog -Component 'index-refresh' -Message "  $indexResult"
    if ($indexExit -ne 0) {
        # Do NOT mark the throttle on failure — let the next session retry the refresh.
        Write-MemoryLog -Component 'index-refresh' -Message "index build failed (exit=$indexExit); throttle NOT marked"
        exit 0
    }
    Mark-Throttle -Name 'index-refresh'
    Write-MemoryLog -Component 'index-refresh' -Message 'MEMORY.md index refreshed (decoupled from dream)'
} catch {
    # Fail-open: never surface a refresh failure to the session.
    try { Write-MemoryLog -Component 'index-refresh' -Message "index refresh aborted (non-fatal): $_" } catch {}
}
exit 0
