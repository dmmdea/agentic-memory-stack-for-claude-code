# wiki-index-catchup.ps1 - a replica PC refreshes the wiki index itself when it is due.
#
# Problem: the brain's nightly step pulls the vault from a PC at 03:00, and a workstation that is
# asleep or off at 03:00 leaves the index aging (docs/systems/wiki-index.md). Nothing retried once
# the PC was back. This child, spawned DETACHED from SessionStart by memory-maintenance-spawn.ps1
# (so its HTTP call never sits on the session's hot path), runs the session-side refresh
# (wiki-index-refresh.sh -> wiki-index.sh snapshot + build) when either is true:
#   - the authority reports the index older than 20 h (`wiki.fresh_age_h` in /health/maintenance;
#     the newer of the brain's last pull and last build, so a refresh from any PC counts), or
#   - any wiki/*.md page in the vault is newer than this PC's own refresh stamp
#     (~/.claude/state/last-wiki-refresh, written by wiki-index-refresh.sh on success).
# Attempts are throttled to one per 6 h (the launch opens the window; a check that launched
# nothing does not, so a page edited right after a check is still caught at the next start).
# No new scheduled task: this rides the existing SessionStart hook.
#
# Only a replica runs it (the brain's own nightly chain owns the index there), and only when the
# operator has told this box where the vault is: WIKI_VAULT, else the first line of
# ~\.mem0\wiki-vault (a per-box operator value, like the brain alias). The drive letter of a
# cloud-synced folder moves, so a configured path that is gone is retried on the other letters.
# Fail-open everywhere: a failure here must never touch the session.
param([switch]$DefineOnly)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$script:CommonOk = $true
try {
    . (Join-Path $ScriptDir 'memory-common.ps1')
    Initialize-MemoryEnv
} catch { $script:CommonOk = $false }

$script:WikiFreshLimitH = 20
$script:WikiThrottleSeconds = 21600

function Get-WikiVaultDir {
    # The vault directory on THIS box, or $null. WIKI_VAULT wins over the config file.
    param([string[]]$Letters = @('G', 'D', 'E', 'F', 'H'))
    $configured = "$($env:WIKI_VAULT)".Trim()
    if (-not $configured) {
        try {
            $f = Join-Path (Get-AmsHomeDir) (Join-Path '.mem0' 'wiki-vault')
            if (Test-Path -LiteralPath $f) {
                foreach ($line in @([System.IO.File]::ReadAllLines($f))) {
                    $t = "$line".Trim()
                    if ($t -and -not $t.StartsWith('#')) { $configured = $t; break }
                }
            }
        } catch {}
    }
    if (-not $configured) { return $null }
    $configured = $configured.TrimEnd('\', '/')
    if (Test-Path -LiteralPath $configured) { return $configured }
    if ($configured -match '^[A-Za-z]:[\\/]') {
        $rest = $configured.Substring(2)
        foreach ($l in $Letters) {
            $candidate = $l + ':' + $rest
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    return $null
}

function Get-WikiFreshAgeH {
    # The authority's index freshness in hours: [double], or $null when it does not say (an
    # authority older than the `wiki` block, or no such value). Throws when it is unreachable.
    param([Parameter(Mandatory)][string]$AuthorityUrl)
    $h = Invoke-RestMethod -Uri ($AuthorityUrl.TrimEnd('/') + '/health/maintenance') -TimeoutSec 3 -ErrorAction Stop
    if ($null -eq $h -or $null -eq $h.wiki) { return $null }
    $v = $h.wiki.fresh_age_h
    if ($null -eq $v) { return $null }
    try { return [double]$v } catch { return $null }
}

function Find-GitBash {
    # Git Bash by its install location. NOT the first `bash` on PATH: System32 carries the WSL
    # launcher under that name, which cannot run the refresh driver.
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA 'Programs'))
    foreach ($r in $roots) {
        if (-not $r) { continue }
        $c = Join-Path $r 'Git\bin\bash.exe'
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return $null
}

function Start-DetachedProcess {
    param([string]$FilePath, [string]$ArgumentList, [hashtable]$Environment)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = $ArgumentList
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    foreach ($k in $Environment.Keys) { $psi.EnvironmentVariables[$k] = [string]$Environment[$k] }
    $proc = [System.Diagnostics.Process]::Start($psi)
    if ($proc) { $proc.Dispose() }
}

function Start-WikiRefresh {
    # Launch the deployed refresh driver, hidden and detached. $true when it was started.
    param([Parameter(Mandatory)][string]$VaultDir, [Parameter(Mandatory)][string]$ScriptDir)
    # The vault path is operator config that reaches an environment block, so it is whitelisted.
    if ($VaultDir -match '["`$;&|<>%^]') {
        Write-MemoryLog -Component 'wiki-index-catchup' -Message 'vault path has characters that are not allowed; refresh not started'
        return $false
    }
    $driver = Join-Path $ScriptDir 'wiki-index-refresh.sh'
    if (-not (Test-Path -LiteralPath $driver)) {
        Write-MemoryLog -Component 'wiki-index-catchup' -Message 'wiki-index-refresh.sh is not deployed beside this script; refresh not started'
        return $false
    }
    $bash = Find-GitBash
    if (-not $bash) {
        Write-MemoryLog -Component 'wiki-index-catchup' -Message 'Git Bash not found; refresh not started'
        return $false
    }
    $envBlock = @{ WIKI_VAULT = $VaultDir }
    if ($script:Mem0WslDistro) { $envBlock['WSL_DISTRO'] = $script:Mem0WslDistro }
    try {
        Start-DetachedProcess -FilePath $bash -ArgumentList ('"' + ($driver -replace '\\', '/') + '"') -Environment $envBlock
    } catch {
        Write-MemoryLog -Component 'wiki-index-catchup' -Message "refresh could not be started (non-fatal): $_"
        return $false
    }
    return $true
}

function Get-WikiRefreshReason {
    # Why a refresh is due, or $null. Pure: the age (or $null when unknown), the vault, the stamp.
    param($FreshAgeH, [string]$VaultDir, [string]$StampPath)
    if ($null -ne $FreshAgeH -and [double]$FreshAgeH -gt $script:WikiFreshLimitH) {
        return ('index is {0:N1} h old' -f [double]$FreshAgeH)
    }
    $wikiDir = Join-Path $VaultDir 'wiki'
    if (-not (Test-Path -LiteralPath $wikiDir)) { return $null }
    $since = [datetime]::MinValue
    if (Test-Path -LiteralPath $StampPath) { $since = (Get-Item -LiteralPath $StampPath).LastWriteTimeUtc }
    $newer = Get-ChildItem -LiteralPath $wikiDir -Recurse -File -Filter '*.md' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTimeUtc -gt $since } | Select-Object -First 1
    if ($newer) { return 'a vault page is newer than the last refresh' }
    return $null
}

function Invoke-WikiCatchup {
    param([Parameter(Mandatory)][string]$ScriptDir)
    try {
        if ((Get-Mem0Role) -eq 'brain') { return }
        if (-not (Test-Throttle -Name 'wiki-catchup' -MinIntervalSeconds $script:WikiThrottleSeconds)) { return }
        $vault = Get-WikiVaultDir
        if (-not $vault) { return }
        try {
            $ageH = Get-WikiFreshAgeH -AuthorityUrl (Get-Mem0AuthorityUrl)
        } catch {
            # The refresh tunnels to the same brain, so there is nothing to attempt; retry next start.
            Write-MemoryLog -Component 'wiki-index-catchup' -Message "authority unreachable; no refresh attempted ($($_.Exception.Message))"
            return
        }
        $stamp = Join-Path $script:StateDir 'last-wiki-refresh'
        $reason = Get-WikiRefreshReason -FreshAgeH $ageH -VaultDir $vault -StampPath $stamp
        if (-not $reason) { return }
        Write-MemoryLog -Component 'wiki-index-catchup' -Message "refresh due ($reason); starting wiki-index-refresh.sh"
        if (Start-WikiRefresh -VaultDir $vault -ScriptDir $ScriptDir) {
            Mark-Throttle -Name 'wiki-catchup'
        }
    } catch {
        try { Write-MemoryLog -Component 'wiki-index-catchup' -Message "catch-up aborted (non-fatal): $_" } catch {}
    }
}

if ($DefineOnly) { return }
if ($script:CommonOk) { Invoke-WikiCatchup -ScriptDir $ScriptDir }
exit 0
