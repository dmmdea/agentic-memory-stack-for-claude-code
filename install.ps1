# install.ps1 - top-level orchestrator
# Runs all 4 install phases. Idempotent: safe to re-run.
#
# Usage from a fresh PowerShell session:
#   cd $env:USERPROFILE\agentic-memory-stack
#   .\install.ps1
#
# Or non-interactive (skip prompts, log to file):
#   .\install.ps1 -NonInteractive -LogFile install.log

param(
    [switch]$NonInteractive,
    [string]$LogFile = '',
    # v1.0 Phase 7A: operator-agnostic install. The WSL distro is auto-detected
    # (default distro from `wsl -l -q`) but can be overridden for multi-distro boxes.
    [string]$Distro = ''
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

# v1.0 Phase 7A: resolve the WSL distro (never hardcode 'Ubuntu'). `wsl -l -q`
# emits UTF-16 — read it with the right console encoding or names arrive
# space-padded. Default = the first (default) installed distro.
if (-not $Distro) {
    $prevEnc = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::Unicode
        $Distro = (wsl.exe -l -q | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
    } finally { [Console]::OutputEncoding = $prevEnc }
}
if (-not $Distro) { throw "No WSL distro found. Install one (wsl --install -d Ubuntu) or pass -Distro <name> (see: wsl -l -q)." }

function Write-Phase {
    param([string]$Title)
    Write-Host ""
    Write-Host "=================================================================" -ForegroundColor Cyan
    Write-Host " $Title" -ForegroundColor Cyan
    Write-Host "=================================================================" -ForegroundColor Cyan
}

if ($LogFile) { Start-Transcript -Path $LogFile -Append | Out-Null }

try {
    Write-Phase "Agentic Memory Stack - Install"
    Write-Host "Repo root: $RepoRoot"
    Write-Host "Windows user: $env:USERNAME"
    Write-Host "WSL distro: $Distro"
    $wslUser = (wsl.exe -d $Distro -e whoami).Trim()
    Write-Host "WSL user: $wslUser"
    Write-Host ""

    Write-Phase "[0/4] Prerequisites check"
    & "$RepoRoot\install\0-prereqs.ps1" -Distro $Distro
    if ($LASTEXITCODE -ne 0) { throw "Prerequisites check failed - resolve issues above and re-run." }

    Write-Phase "[1/4] WSL services (mem0, Qdrant, l10-audit, llama-swap)"
    # wslpath needs forward slashes (backslashes are stripped by the wsl.exe arg pass);
    # fall back to manual /mnt/<drive>/... computation.
    $rrFwd = $RepoRoot -replace '\\', '/'
    $repoWsl = (wsl.exe -d $Distro wslpath -u "$rrFwd" 2>$null)
    if ($repoWsl) { $repoWsl = ([string]$repoWsl).Trim() }
    if (-not $repoWsl) { $repoWsl = "/mnt/" + $RepoRoot.Substring(0,1).ToLower() + "/" + ($RepoRoot.Substring(3) -replace '\\', '/') }
    wsl.exe -d $Distro -e bash "$repoWsl/install/1-wsl-services.sh" "$wslUser" "$env:USERNAME" "$Distro"
    if ($LASTEXITCODE -ne 0) { throw "WSL services install failed." }

    Write-Phase "[2/4] Windows config (hooks, Task Scheduler, MCP registrations, CLAUDE.md patch)"
    & "$RepoRoot\install\2-windows-config.ps1" -WslUser $wslUser -Distro $Distro
    if ($LASTEXITCODE -ne 0) { throw "Windows config failed." }

    Write-Phase "[3/4] Verify (end-to-end smoke test)"
    & "$RepoRoot\install\3-verify.ps1" -WslUser $wslUser -Distro $Distro
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Verify reported issues - check output above. Stack may still be partially functional." -ForegroundColor Yellow
    }

    Write-Phase "DONE"
    Write-Host "Restart VS Code / Claude Code to pick up new hooks + MCP servers." -ForegroundColor Green
    Write-Host "First C1 consolidation fires daily at 03:00 (Windows Task Scheduler with -WakeToRun)." -ForegroundColor Green
    Write-Host "L1a extraction fires on every Stop/PreCompact hook (10-minute throttle)." -ForegroundColor Green

} finally {
    if ($LogFile) { Stop-Transcript | Out-Null }
}
