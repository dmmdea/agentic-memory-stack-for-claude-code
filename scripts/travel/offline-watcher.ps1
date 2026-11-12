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

function Test-IsPlaceholderUrl {
    # $true for the documentation placeholder host (your-machine): it resolves to nothing, so it is never
    # an authority to probe. Operator-neutral text in config templates must not become one.
    param([string]$Url)
    $uri = $null
    if (-not [System.Uri]::TryCreate("$Url".Trim(), [System.UriKind]::Absolute, [ref]$uri)) { return $false }
    return ($uri.Host -ieq 'your-machine')
}

function Get-FirstUrlLine {
    # First non-blank, non-comment line of a ~/.mem0/authority-url style file's lines, trimmed, no trailing '/'.
    param($Lines)
    foreach ($l in @($Lines)) {
        $t = "$l" -replace "`0", ''
        $t = $t.Trim()
        if ($t -and -not $t.StartsWith('#')) { return $t.TrimEnd('/') }
    }
    return ''
}

function Resolve-WatcherAuthority {
    # The authority this box probes, or '' when none can be told. Order: $env:MEM0_URL when it is not
    # local; the Windows-side ~\.mem0\authority-url (a plain file read: it works while wsl.exe is busy and
    # travel-mode.ps1 never writes it, so it keeps naming the authority while the replica runs) when it is
    # not local; the WSL copy of the same file. A local, unspecified, malformed or placeholder URL is never
    # taken. There is no default host: an unresolved authority is "cannot tell".
    param([string]$Distro)
    $usable = { param($u) $u -and -not (Test-IsLocalUrl $u) -and -not (Test-IsPlaceholderUrl $u) }
    $e = "$env:MEM0_URL".Trim().TrimEnd('/')
    if (& $usable $e) { return $e }
    try {
        $f = Join-Path $env:USERPROFILE '.mem0\authority-url'
        if (Test-Path -LiteralPath $f) {
            $w = Get-FirstUrlLine ([System.IO.File]::ReadAllLines($f))
            if (& $usable $w) { return $w }
        }
    } catch {}
    if ($Distro) {
        try {
            $env:WSL_UTF8 = '1'
            $w = Get-FirstUrlLine (wsl.exe -d $Distro -e bash -lc 'cat ~/.mem0/authority-url 2>/dev/null' 2>$null)
            if (& $usable $w) { return $w }
        } catch {}
    }
    return ''
}

function Test-CacheRefreshDue {
    # 1.35.2: is it time to refresh the local snapshot cache (travel-mode.ps1 refresh, through the
    # mem0-replica-cache-refresh task)? $Record is replica-cache-refresh.json (or $null). Due when there is
    # no record, $OkHours after a refresh that left the cache current (the authority writes one set a
    # night, so 12 h picks each one up the same day), $RetryHours after one that did not, and $StaleRunHours
    # after a 'running' record (a refresh that died mid-copy: the task's 2 h limit killed it, or a reboot;
    # the same 6 h as a failure, so a refresh that keeps dying is not restarted back to back). Epoch
    # seconds, never parsed dates: pwsh's ConvertFrom-Json turns ISO strings into DateTimes of whatever kind
    # it guesses. A stamp in the future (a clock moved back) is treated as due, so a bad clock can never
    # stop the refresh for good.
    param($Record, [int64]$NowEpoch, [double]$OkHours = 12, [double]$RetryHours = 6, [double]$StaleRunHours = 6)
    if (-not $Record) { return $true }
    $state = [string]$Record.state
    $at = if ($state -eq 'running') { $Record.started_epoch } else { $Record.finished_epoch }
    $t = 0L
    if ($null -eq $at -or -not [int64]::TryParse("$at", [ref]$t) -or $t -le 0) { return $true }
    $ageH = ($NowEpoch - $t) / 3600.0
    if ($ageH -lt 0) { return $true }
    if ($state -eq 'running') { return $ageH -ge $StaleRunHours }
    if ($Record.ok -eq $true) { return $ageH -ge $OkHours }
    return $ageH -ge $RetryHours
}

function Read-RestoreMarker {
    # replica-restored.txt: "<ISO time> <set stamp>" since 1.35.2 (travel-mode.ps1 on writes it after a
    # restore), a bare time before. Returns Epoch (file mtime, UTC) and Stamp ('' for an old marker), or $null.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $stamp = ''
    try {
        $tok = @("$(Get-Content -Raw -LiteralPath $Path)".Trim() -split '\s+')
        if ($tok.Count -ge 2 -and $tok[1] -match '^[0-9]{8}-[0-9]{6}$') { $stamp = $tok[1] }
    } catch {}
    [pscustomobject]@{ Epoch = [DateTimeOffset]::new((Get-Item -LiteralPath $Path).LastWriteTimeUtc).ToUnixTimeSeconds(); Stamp = $stamp }
}

function Get-CacheNewestStamp {
    # The newest COMPLETE set in the local cache: the four files sharing a stamp, as travel-mode.ps1's
    # Get-NewestCompleteSet decides it (ReplicaTakeover.Tests.ps1 runs both on the same folders). '' when none.
    param([string]$Dir)
    try {
        if (-not $Dir -or -not (Test-Path -LiteralPath $Dir)) { return '' }
        foreach ($m in @(Get-ChildItem (Join-Path $Dir 'manifest-*.json') -File -ErrorAction Stop | Sort-Object Name -Descending)) {
            $s = $m.BaseName -replace '^manifest-', ''
            $need = @("episodic-$s.db", "history-$s.db", "qdrant-$s.snapshot") | ForEach-Object { Join-Path $Dir $_ }
            if (-not ($need | Where-Object { -not (Test-Path -LiteralPath $_) })) { return $s }
        }
    } catch {}
    return ''
}

function Get-LocalCacheDir {
    # Where the takeover snapshot cache is: the folder the last refresh recorded, else travel-mode.ps1's
    # convention, D:\memory-backups\<authority host> (no folder for an IP literal or a local host).
    param($Record, [string]$Authority)
    if ($Record -and $Record.local_dir) { return [string]$Record.local_dir }
    $h = ''
    try { $h = ([System.Uri]$Authority).Host } catch {}
    $h = ($h -split '\.')[0].ToLowerInvariant()
    if (-not $h -or $h -match '^\d+$' -or $h -in @('127', 'localhost')) { return '' }
    return "D:\memory-backups\$h"
}

function Test-ReplicaRestoreStale {
    # Must a takeover restore again (travel-mode.ps1 on), or may it restart the replica it last restored?
    # Restore again when there is no restore marker, when it is older than $MaxHours, when it does not name
    # the set it restored (written before 1.35.2), or when the cache now holds a newer complete set than
    # that one: the replica would otherwise come up with the older set while the newer one sits beside it.
    # Set stamps, not clock times: a set seeded while a restore ran, or seeded by hand, is still seen.
    param($Marker, [string]$CacheStamp, [int64]$NowEpoch, [double]$MaxHours = 24)
    if (-not $Marker -or $Marker.Epoch -le 0) { return $true }
    if ((($NowEpoch - $Marker.Epoch) / 3600.0) -gt $MaxHours) { return $true }
    if (-not $Marker.Stamp) { return $true }
    if ($CacheStamp -and $CacheStamp -gt $Marker.Stamp) { return $true }
    return $false
}

function Write-WatcherLog {
    # The tick runs windowless, so Write-Host goes nowhere: transitions and failures also go here
    # (~\.claude\state\offline-watcher.log, trimmed to its last 400 lines past 256 KB).
    param([string]$Line)
    try {
        $p = Join-Path $env:USERPROFILE '.claude\state\offline-watcher.log'
        Add-Content -LiteralPath $p -Value "$((Get-Date).ToString('s')) $Line" -Encoding utf8
        if ((Get-Item -LiteralPath $p).Length -gt 256KB) { Set-Content -LiteralPath $p -Value (Get-Content -LiteralPath $p -Tail 400) -Encoding utf8 }
    } catch {}
}

if ($DefineOnly) { return }
# --- tick body added in Task 7 ---
$ErrorActionPreference = 'Continue'
# Authority resolution (Resolve-WatcherAuthority): explicit -Authority wins (always honored, even
# loopback — a deliberate operator choice, but never the placeholder host); else $env:MEM0_URL only when
# Test-IsLocalUrl rejects it as neither local, unspecified, nor malformed; else the Windows-side
# ~\.mem0\authority-url; else the WSL copy of that file; else the tick stops ("cannot tell", no probe,
# no state change). A busy wsl.exe used to drop the watcher onto the placeholder host, which resolves to
# nothing, and three such ticks put the box offline while the authority was up. An older travel-mode.ps1 on set
# the user-scope MEM0_URL to the LOCAL replica — each scheduled-task tick is a fresh process
# that would otherwise inherit the replica as its "authority", probe it as healthy, and
# go_online would replay the outbox INTO the disposable local store (one-brain violation).
# The true authority is never a loopback/unspecified host on the machine this watcher runs
# on (it is a companion to travel-mode.ps1, which refuses on/off on the authority itself).
$Distro    = if ($env:MEM0_WSL_DISTRO) { $env:MEM0_WSL_DISTRO } else {
    # wsl.exe emits UTF-16LE by default -> NUL-interleaved capture; WSL_UTF8=1 fixes the
    # encoding at the source, NUL-strip is belt-and-braces for older wsl.exe builds.
    # A busy or absent wsl.exe leaves the name empty (the WSL read below is then skipped).
    $env:WSL_UTF8 = '1'
    try { "$(((wsl.exe -l -q 2>$null) -replace "`0", '' | Where-Object { "$_".Trim() } | Select-Object -First 1))".Trim() } catch { '' } }
if ($Authority -and (Test-IsPlaceholderUrl $Authority)) { $Authority = '' }
if (-not $Authority) { $Authority = Resolve-WatcherAuthority -Distro $Distro }
if (-not $Authority) {
    # "Cannot tell" is not "offline": with no authority to probe the tick changes nothing, reads and
    # writes no state, and never probes the placeholder host (a reachable authority would otherwise
    # look down three ticks running and push this box into travel mode).
    Write-WatcherLog 'authority unresolved (no -Authority, no non-local MEM0_URL, no non-local ~\.mem0\authority-url, WSL file unreadable or local); skipping this tick without probing'
    # Non-zero on purpose: the task's last result is what the verifier's offline-watcher row reads, so a
    # watcher that cannot see its authority shows there instead of staying quiet through a real outage.
    exit 3
}
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
$RefreshRecordPath = Join-Path $env:USERPROFILE '.claude\state\replica-cache-refresh.json'
$refreshRecord = $null
try { if (Test-Path -LiteralPath $RefreshRecordPath) { $refreshRecord = Get-Content -Raw -LiteralPath $RefreshRecordPath | ConvertFrom-Json -ErrorAction Stop } } catch { $refreshRecord = $null }
$nowEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

if ($next.transition -eq 'go_offline') {
    # Restore again if the last restore is stale (no marker, >24 h, or older than the newest complete set
    # in the cache), then bring the replica up read-only.
    $lastRestore = Join-Path $env:USERPROFILE '.claude\state\replica-restored.txt'
    $marker = Read-RestoreMarker $lastRestore
    $cacheStamp = Get-CacheNewestStamp (Get-LocalCacheDir -Record $refreshRecord -Authority $Authority)
    $stale = Test-ReplicaRestoreStale -Marker $marker -CacheStamp $cacheStamp -NowEpoch $nowEpoch
    Write-WatcherLog "go_offline: $Authority unreachable x3; cache newest=$(if ($cacheStamp) { $cacheStamp } else { 'none' }) restored=$(if ($marker -and $marker.Stamp) { $marker.Stamp } elseif ($marker) { '(unnamed)' } else { 'never' }) -> $(if ($stale) { 'restore' } else { 'start the existing restore' })"
    if ($stale) {
        # travel-mode.ps1 on writes the marker itself, after a restore that did not throw, naming the set
        # it restored; a failed or partial restore leaves the old marker, so the next takeover tries again.
        $global:LASTEXITCODE = 0
        $tmOk = $false; $tmErr = ''
        try { & "$PSScriptRoot\travel-mode.ps1" on | Out-Null; $tmOk = $? } catch { $tmOk = $false; $tmErr = $_.Exception.Message }
        if ($tmOk -and $LASTEXITCODE -eq 0) { Write-WatcherLog "go_offline: restored and started the replica" }
        else {
            Write-WatcherLog "go_offline: travel-mode.ps1 on FAILED: $tmErr"
            if ($marker) {
                # A failed re-restore must not leave the box with no memory when a previous restore is on
                # disk: start that one (older, but it answers). Nothing is stamped, so the next takeover
                # tries the newer set again.
                Wsl "systemctl --user start qdrant.service mem0.service 2>/dev/null; true" | Out-Null
                Write-WatcherLog "go_offline: started the previous restore ($(if ($marker.Stamp) { $marker.Stamp } else { 'unnamed' })) instead"
            }
        }
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
        # AMS-48 (2026-08-08): the drain's output used to go to Out-Null, so a
        # replay that abandoned writes to mutation-conflicts.jsonl left NO trace
        # anywhere — the one moment the outbox's whole purpose is on the line
        # was the one moment nothing was recorded. Tee the stats (with a ts) to
        # ~/.mem0/outbox-drain.log, which job_liveness already ages.
        # Also run the DEPLOYED replay-ops (~/apps/mem0-scripts), not the
        # checkout copy: a watcher firing against a stale/absent worktree was
        # the same checkout-as-deployment class as the dedup task (AMS-31/50).
        $drainOut = Wsl "MEM0_URL='$Authority' ~/apps/mem0-server/.venv/bin/python ~/apps/mem0-scripts/replay-ops.py 2>&1"
        $drainTs  = (Get-Date).ToUniversalTime().ToString('o')
        $drainLine = ('{{"ts":"{0}","event":"outbox-drain","result":{1}}}' -f `
            $drainTs, (($drainOut | Out-String).Trim() | ForEach-Object { if ($_ -match '^\s*\{') { $_ } else { ($_ | ConvertTo-Json -Compress) } }))
        Wsl "printf '%s\n' '$($drainLine -replace "'", "''")' >> ~/.mem0/outbox-drain.log" | Out-Null
        Write-Host "offline-watcher: outbox drain -> $drainOut"
        Write-WatcherLog "go_online: $Authority back; outbox drain -> $((($drainOut | Out-String).Trim() -replace '\s+', ' '))"
    } else {
        # transient WSL failure: skip replay rather than run a garbage path — the outbox
        # persists on disk, so the drain happens on the next offline->online cycle
        Write-Host "offline-watcher: could not resolve repo root to a WSL path; skipping replay this cycle"
    }
    Wsl "systemctl --user stop mem0.service qdrant.service 2>/dev/null; true" | Out-Null
    # the redesigned shim no longer reads travel.json; clear the flag travel-mode.ps1 on
    # wrote so 'travel-mode.ps1 status' doesn't report travel mode forever (mirrors 'off')
    Wsl 'rm -f ~/.mem0/travel.json' | Out-Null
    # v1.23 P2-7: the Windows hooks resolve the authority from ~\.mem0\authority-url, which travel
    # mode never changes any more — nothing to restore on reconnect.
}
elseif ($next.mode -eq 'online' -and $reachable -and (Test-CacheRefreshDue -Record $refreshRecord -NowEpoch $nowEpoch)) {
    # 1.35.2: while the authority answers, keep the takeover snapshot current. The refresh copies a ~700 MB
    # set from pCloud, so it runs as its own task (install-offline-watcher.ps1 registers it): this tick
    # returns at once and keeps probing, the task has its own time limit, and its LastTaskResult is
    # readable. The task ignores a start while one is already running.
    try { Start-ScheduledTask -TaskName 'mem0-replica-cache-refresh' -ErrorAction Stop }
    catch { Write-WatcherLog "could not start mem0-replica-cache-refresh ($($_.Exception.Message)); re-run install-offline-watcher.ps1" }
}

# 5. persist
New-Item -ItemType Directory -Force -Path (Split-Path $StateFile) | Out-Null
($next | Select-Object mode,consecutive_down,consecutive_up,transition | ConvertTo-Json -Compress) |
    Set-Content -LiteralPath $StateFile -Encoding utf8
