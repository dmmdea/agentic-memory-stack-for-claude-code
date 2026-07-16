#Requires -PSEdition Core
param(
    [switch]$DefineOnly,     # -DefineOnly: dot-source for tests without running the tick
    [string]$Authority = ''  # explicit authority override; resolution below ignores loopback env
)

function Step-OfflineState {
    param([Parameter(Mandatory)]$State, [Parameter(Mandatory)][bool]$Reachable, [int]$N = 3, [int]$M = 2)
    $down = [int]$State.consecutive_down; $up = [int]$State.consecutive_up; $mode = [string]$State.mode
    $transition = 'none'
    if ($Reachable) { $up++; $down = 0 } else { $down++; $up = 0 }
    if ($mode -eq 'online'  -and $down -ge $N) { $mode = 'offline'; $transition = 'go_offline' }
    elseif ($mode -eq 'offline' -and $up -ge $M) { $mode = 'online'; $transition = 'go_online' }
    [pscustomobject]@{ mode = $mode; consecutive_down = $down; consecutive_up = $up; transition = $transition }
}

function Test-IsLocalUrl {
    # $true = local/unspecified/invalid — NEVER trust as the authority; $false = plausibly remote.
    # Robust, operator-agnostic: covers all of 127.0.0.0/8, ::1 (any IPv6 form), localhost and
    # *.localhost, and the unspecified hosts 0.0.0.0 / :: — not just two literals. A malformed
    # URL returns $true (fall through to the default), never throws: a bad env var must not
    # crash the tick or become the authority.
    param([string]$Url)
    if (-not "$Url".Trim()) { return $true }
    $uri = $null
    if (-not [System.Uri]::TryCreate($Url, [System.UriKind]::Absolute, [ref]$uri)) { return $true }
    $h = $uri.Host
    if (-not $h) { return $true }
    if ($h -ieq 'localhost' -or $h -ilike '*.localhost') { return $true }
    $ip = $null
    if ([System.Net.IPAddress]::TryParse($h.Trim('[', ']'), [ref]$ip)) {
        if ([System.Net.IPAddress]::IsLoopback($ip)) { return $true }
        if ($ip.Equals([System.Net.IPAddress]::Any) -or $ip.Equals([System.Net.IPAddress]::IPv6Any)) { return $true }
    }
    return $false   # non-IP hostnames other than localhost are remote (your-machine, any operator's host)
}

if ($DefineOnly) { return }
# --- tick body added in Task 7 ---
$ErrorActionPreference = 'Continue'
# Authority resolution: explicit -Authority wins (always honored, even loopback — a
# deliberate operator choice); else $env:MEM0_URL only when Test-IsLocalUrl rejects it as
# neither local, unspecified, nor malformed; else the your-machine default. travel-mode.ps1 on sets
# the user-scope MEM0_URL to the LOCAL replica — each scheduled-task tick is a fresh process
# that would otherwise inherit the replica as its "authority", probe it as healthy, and
# go_online would replay the outbox INTO the disposable local store (one-brain violation).
# The true authority is never a loopback/unspecified host on the machine this watcher runs
# on (it is a companion to travel-mode.ps1, which refuses on/off on the authority itself).
if (-not $Authority) {
    if ($env:MEM0_URL -and -not (Test-IsLocalUrl $env:MEM0_URL)) { $Authority = $env:MEM0_URL }
    else { $Authority = 'http://your-machine:18791' }
}
$Distro    = if ($env:MEM0_WSL_DISTRO) { $env:MEM0_WSL_DISTRO } else {
    # wsl.exe emits UTF-16LE by default -> NUL-interleaved capture; WSL_UTF8=1 fixes the
    # encoding at the source, NUL-strip is belt-and-braces for older wsl.exe builds.
    $env:WSL_UTF8 = '1'
    ((wsl.exe -l -q) -replace "`0", '' | Where-Object { $_.Trim() } | Select-Object -First 1).Trim() }
$StateFile = Join-Path $env:USERPROFILE '.claude\state\offline-mode.json'
function Wsl([string]$c) { wsl.exe -d $Distro -e bash -lc $c }

# 1. probe your-machine over Tailscale (short timeout)
$reachable = $false
try { $h = Invoke-RestMethod "$Authority/health" -TimeoutSec 4; $reachable = ($h.ok -eq $true) } catch { $reachable = $false }

# 2. load prior state (default online; a corrupt/torn state file must not wedge the watcher)
$prev = [pscustomobject]@{ mode='online'; consecutive_down=0; consecutive_up=0; transition='none' }
if (Test-Path $StateFile) {
    $loaded = $null
    try { $loaded = Get-Content $StateFile -Raw | ConvertFrom-Json -ErrorAction Stop } catch { $loaded = $null }
    if ($loaded -and $loaded.mode) { $prev = $loaded }
    else { Write-Host "offline-watcher: state file unreadable or missing 'mode'; resetting to default online state" }
}

# 3. step
$next = Step-OfflineState -State $prev -Reachable:$reachable

# 4. act on transitions
if ($next.transition -eq 'go_offline') {
    # refresh replica if stale (>24h), then bring it up read-only
    $lastRestore = Join-Path $env:USERPROFILE '.claude\state\replica-restored.txt'
    $stale = -not (Test-Path $lastRestore) -or ((Get-Date) - (Get-Item $lastRestore).LastWriteTime).TotalHours -gt 24
    if ($stale) {
        # stamp the marker ONLY when travel-mode actually succeeded, or later cycles
        # would treat a failed/partial restore as a fresh replica
        $global:LASTEXITCODE = 0
        $tmOk = $false
        try { & "$PSScriptRoot\travel-mode.ps1" on | Out-Null; $tmOk = $? } catch { $tmOk = $false }
        if ($tmOk -and $LASTEXITCODE -eq 0) { Set-Content $lastRestore (Get-Date).ToString('o') }
        else { Write-Host "offline-watcher: travel-mode.ps1 on failed; not stamping replica-restored marker" }
    } else {
        Wsl "systemctl --user start qdrant.service mem0.service 2>/dev/null; true" | Out-Null
    }
}
elseif ($next.transition -eq 'go_online') {
    # Repo root derived from this script's location (scripts/travel/ -> two levels up),
    # converted to a WSL path via wslpath — works on any machine, no hardcoded checkout path.
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..' '..')).Path
    $RepoWsl  = "$(Wsl "wslpath '$($repoRoot -replace '\\','/')'")".Trim()
    if ($RepoWsl) {
        Wsl "MEM0_URL='$Authority' ~/apps/mem0-server/.venv/bin/python $RepoWsl/scripts/wsl/replay-ops.py" | Out-Null
    } else {
        # transient WSL failure: skip replay rather than run a garbage path — the outbox
        # persists on disk, so the drain happens on the next offline->online cycle
        Write-Host "offline-watcher: could not resolve repo root to a WSL path; skipping replay this cycle"
    }
    Wsl "systemctl --user stop mem0.service qdrant.service 2>/dev/null; true" | Out-Null
    # the redesigned shim no longer reads travel.json; clear the flag travel-mode.ps1 on
    # wrote so 'travel-mode.ps1 status' doesn't report travel mode forever (mirrors 'off')
    Wsl 'rm -f ~/.mem0/travel.json' | Out-Null
    # travel-mode.ps1 on points the Windows-side hooks at the replica via a User-scope
    # MEM0_URL — that env side effect must be undone on auto-reconnect too, or every hook
    # keeps writing to a stopped replica forever (mirrors travel-mode.ps1 off's restore).
    [Environment]::SetEnvironmentVariable('MEM0_URL', $Authority, 'User')
    $env:MEM0_URL = $Authority
}

# 5. persist
New-Item -ItemType Directory -Force -Path (Split-Path $StateFile) | Out-Null
($next | Select-Object mode,consecutive_down,consecutive_up,transition | ConvertTo-Json -Compress) |
    Set-Content -LiteralPath $StateFile -Encoding utf8
