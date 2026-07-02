# build-hook-client.ps1 — v0.20 A.6: compile + smoke-gate + install the
# compiled thin UserPromptSubmit client (mem0-hook-client.cs -> .exe).
#
# WHY A GATE: settings.json points the UserPromptSubmit hook at ONE command —
# the exe. If a broken exe ever lands at the registered path, hooks break HARD
# (no PowerShell wrapper to fail open). So this script:
#   1. refreshes the deployed .cs copy (R9 SHA256-compares it against the repo;
#      the exe's freshness is measured against this file's LastWriteTime),
#   2. compiles with the ALWAYS-PRESENT .NET Framework csc (no new toolchain)
#      to a TEMP candidate — never directly onto the live registered path,
#   3. SMOKE-GATES the candidate (must start, eat stdin, exit 0) — a corrupted
#      or non-functional exe is discarded and the existing exe/registration
#      stays untouched,
#   4. installs the candidate and re-smokes it from the deployed dir.
#
# Deploy flow (full): edit repo .cs (and/or user-prompt-lib.ps1 /
# mem0-hook-daemon.ps1) -> run THIS script -> (only on first install) point
# settings.json UserPromptSubmit at the exe. v0.22 review L6: this script now
# also deploys the edited lib + daemon (step 1b) so a lib/daemon edit ships with
# the rebuild — previously only the .cs/.exe shipped and lib/daemon edits stayed
# inert (deployed copies still hashed/served the old logic, no mismatch).
# ROLLBACK (any time, one line in settings.json): set the UserPromptSubmit
# command back to:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:/Users/<your-windows-user>/.claude/scripts/user-prompt-extract.ps1
# Full rollback additionally removes the SessionStart hook entry running
# mem0-hook-daemon-spawn.ps1 (daemon self-terminates after 2h idle, or kill the
# hidden powershell running mem0-hook-daemon.ps1).
#
# -SmokeOnly <path>: run ONLY the smoke gate against an arbitrary exe and exit
# 0/1 (used by Pester to prove a broken exe is refused without touching the
# live deployment).

param(
    [string]$DeployDir = (Join-Path $env:USERPROFILE '.claude\scripts'),
    [string]$SmokeOnly,        # smoke an arbitrary exe, change nothing
    [string]$OutExe,           # override final exe path (tests); default <DeployDir>\mem0-hook-client.exe
    [switch]$NoDeployCs        # tests: compile the repo .cs as-is, do not refresh the deployed copy
)

$ErrorActionPreference = 'Stop'

function Test-HookClientSmoke {
    # The candidate must behave like a hook: start, consume stdin, exit 0
    # within the timeout. The payload is hook-shaped but NOT a UserPromptSubmit
    # event, so neither the daemon nor the PS fallback performs any HTTP work
    # (no checkpoint debris); a corrupted exe fails at Process.Start or returns
    # non-zero. Returns $true/$false, never throws.
    param([string]$ExePath, [int]$TimeoutMs = 20000)
    try {
        if (-not (Test-Path -LiteralPath $ExePath)) { return $false }
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $ExePath
        $psi.UseShellExecute = $false
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $p.StandardInput.Write('{"hook_event_name":"build-smoke"}')
        $p.StandardInput.Close()
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($TimeoutMs)) {
            try { $p.Kill() } catch {}
            return $false
        }
        $null = $outTask.Result; $null = $errTask.Result
        return ($p.ExitCode -eq 0)
    } catch { return $false }
}

if ($SmokeOnly) {
    if (Test-HookClientSmoke -ExePath $SmokeOnly) { Write-Host "SMOKE PASS: $SmokeOnly"; exit 0 }
    Write-Host "SMOKE FAIL: $SmokeOnly is not a working hook client (would NOT be installed/registered)"
    exit 1
}

$repoCs = Join-Path $PSScriptRoot 'mem0-hook-client.cs'
if (-not (Test-Path $repoCs)) { Write-Host "FAIL: source not found: $repoCs"; exit 1 }

# .NET Framework 4.x compiler — present on every Win10/11 install, no toolchain
# to manage. Compiles the C#5-level source in <1s.
$csc = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { Write-Host "FAIL: framework csc not found at $csc"; exit 1 }

if (-not $OutExe) { $OutExe = Join-Path $DeployDir 'mem0-hook-client.exe' }

# 1. Refresh the deployed .cs (R9 hashes repo-vs-deployed; exe freshness =
#    exe.LastWriteTime >= deployed .cs LastWriteTime). Tests skip this.
#    v0.20 Final: when THIS script runs from the deployed dir itself (repo-less
#    DR restore — the SessionStart self-heal in mem0-hook-daemon-spawn.ps1),
#    $repoCs IS the deployed copy; Copy-Item onto itself throws under
#    ErrorActionPreference=Stop, so skip the no-op copy.
$srcCs = $repoCs
if (-not $NoDeployCs) {
    $deployedCs = Join-Path $DeployDir 'mem0-hook-client.cs'
    if ([System.IO.Path]::GetFullPath($repoCs) -ne [System.IO.Path]::GetFullPath($deployedCs)) {
        Copy-Item -LiteralPath $repoCs -Destination $deployedCs -Force
    }
    $srcCs = $deployedCs

    # v0.22 review L6: the combined handshake digest is computed by all three
    # sides (exe, daemon at startup, inline lib) from the DEPLOYED lib + daemon
    # files. Editing user-prompt-lib.ps1 or mem0-hook-daemon.ps1 in the repo and
    # running ONLY this script (without deploying them) left the deployed copies
    # stale: clients hashed the OLD files, matched the OLD daemon, and the edit
    # had ZERO effect (no error, no mismatch). Deploy the whole hot-path bundle
    # here so "run build-hook-client.ps1" ships a lib/daemon edit atomically with
    # the exe rebuild. Same self-copy guard as the .cs (the DR-restore self-heal
    # runs from the deployed dir, where $PSScriptRoot IS $DeployDir).
    # v1.0 Phase 7A: deploy the hot-path bundle WITH operator-sentinel resolution.
    # The deployed copy must carry real values (not __WSL_USER__/__WIN_USER__/
    # __WSL_DISTRO__), or R9 repo-vs-deployed parity drifts (the deploy loop in
    # 2-windows-config substitutes; this re-deploy must too). Resolve from the
    # install receipt beside the deployed scripts; copy verbatim if it's absent.
    $bhcCfgPath = Join-Path $DeployDir 'mem0-stack.config.psd1'
    $bhcCfg = $null
    try { if (Test-Path -LiteralPath $bhcCfgPath) { $bhcCfg = Import-PowerShellDataFile $bhcCfgPath } } catch { $bhcCfg = $null }
    $bhcUtf8NoBom = New-Object System.Text.UTF8Encoding($false)
    # Step 3: dream-catchup.ps1 + memory-index-refresh.ps1 ride the same deploy so a
    # build-hook-client.ps1 run ships them to ~/.claude/scripts (they carry no operator
    # sentinels, so the substitution branch is skipped and they copy verbatim).
    foreach ($hot in @('user-prompt-lib.ps1', 'mem0-hook-daemon.ps1', 'user-prompt-extract.ps1', 'dream-catchup.ps1', 'memory-index-refresh.ps1', 'memory-maintenance-spawn.ps1')) {
        $repoHot = Join-Path $PSScriptRoot $hot
        $depHot  = Join-Path $DeployDir $hot
        if ((Test-Path -LiteralPath $repoHot) -and
            ([System.IO.Path]::GetFullPath($repoHot) -ne [System.IO.Path]::GetFullPath($depHot))) {
            $hotText = [System.IO.File]::ReadAllText($repoHot)
            if ($bhcCfg -and ($hotText -match '__WSL_USER__|__WIN_USER__|__WSL_DISTRO__')) {
                $hotText = $hotText.Replace('__WSL_USER__', [string]$bhcCfg.WslUser).Replace('__WIN_USER__', [string]$bhcCfg.WinUser).Replace('__WSL_DISTRO__', [string]$bhcCfg.Distro)
                [System.IO.File]::WriteAllText($depHot, $hotText, $bhcUtf8NoBom)
            } else {
                Copy-Item -LiteralPath $repoHot -Destination $depHot -Force
            }
        }
    }
}

# 2. Compile to a temp candidate.
$tmpExe = Join-Path $env:TEMP ('mem0-hook-client-' + [guid]::NewGuid().ToString('N') + '.exe')
& $csc /nologo /optimize+ /target:exe /platform:anycpu "/out:$tmpExe" "$srcCs"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $tmpExe)) {
    Write-Host 'FAIL: compile failed - existing exe and registration untouched'
    exit 1
}

# 3. Smoke gate — a candidate that cannot pass never reaches the registered path.
if (-not (Test-HookClientSmoke -ExePath $tmpExe)) {
    Remove-Item -LiteralPath $tmpExe -Force -ErrorAction SilentlyContinue
    Write-Host 'FAIL: smoke check failed - candidate DISCARDED, existing exe and registration untouched'
    exit 1
}

# 4. Install (brief retry: the live exe may be mid-execution serving a hook).
$installed = $false
for ($i = 0; $i -lt 5 -and -not $installed; $i++) {
    try { Copy-Item -LiteralPath $tmpExe -Destination $OutExe -Force; $installed = $true }
    catch { Start-Sleep -Milliseconds 400 }
}
Remove-Item -LiteralPath $tmpExe -Force -ErrorAction SilentlyContinue
if (-not $installed) { Write-Host "FAIL: could not install to $OutExe (in use?) - registration untouched"; exit 1 }
(Get-Item -LiteralPath $OutExe).LastWriteTime = Get-Date   # freshness anchor vs deployed .cs

# 5. Post-install smoke from the deployed dir (full environment: lib + scripts
#    beside the exe). Failure here is LOUD — do not switch/keep registration on
#    a failing exe.
if (-not (Test-HookClientSmoke -ExePath $OutExe)) {
    Write-Host "FAIL: installed exe FAILED the deployed-dir smoke at $OutExe - do NOT register it; rollback line below"
    Write-Host "ROLLBACK: settings.json UserPromptSubmit command -> powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:/Users/$env:USERNAME/.claude/scripts/user-prompt-extract.ps1"
    Write-Host 'ROLLBACK (full): also remove the SessionStart hook entry running mem0-hook-daemon-spawn.ps1 from settings.json (daemon self-terminates after 2h idle, or kill the hidden powershell running mem0-hook-daemon.ps1)'
    exit 1
}

# v0.21 Phase B (L3): write an install-time SHA256 sidecar so R9 has a CONTENT
# anchor for the one artifact it cannot SHA-compare against the repo (no
# committed binary). R9 flags a drift between the installed exe and this record
# (accidental/out-of-band same-user replacement); re-running this build refreshes
# the record. Written AFTER the final post-install smoke so it pins the exact
# bytes that passed the gate.
(Get-FileHash -LiteralPath $OutExe -Algorithm SHA256).Hash | Set-Content -LiteralPath ($OutExe + '.sha256') -NoNewline

Write-Host "OK: compiled + smoke-gated + installed $OutExe"
Write-Host ('Registration (settings.json UserPromptSubmit command): "C:/Users/' + $env:USERNAME + '/.claude/scripts/mem0-hook-client.exe"')
Write-Host "ROLLBACK: settings.json UserPromptSubmit command -> powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:/Users/$env:USERNAME/.claude/scripts/user-prompt-extract.ps1"
Write-Host 'ROLLBACK (full): also remove the SessionStart hook entry running mem0-hook-daemon-spawn.ps1 from settings.json (daemon self-terminates after 2h idle, or kill the hidden powershell running mem0-hook-daemon.ps1)'
exit 0
