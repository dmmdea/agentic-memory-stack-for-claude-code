#Requires -PSEdition Core
<#
.SYNOPSIS
  Travel mode — offline memory on the laptop, with write-outbox re-sync to the your-machine authority.

.DESCRIPTION
  The memory authority lives on your-machine (WSL: Qdrant + mem0 :18791). Over Tailscale it is reachable
  from anywhere with internet, so travel mode is the OFFLINE-ONLY fallback: planes, cabins, dead
  zones — not "away from home".

  ON   restore the newest nightly snapshot into the laptop's LOCAL mem0/Qdrant (read-only
       replica). The mem0 MCP shim needs no flag: when the authority is connect-unreachable
       it fails over to the replica automatically for reads, and queues ALL mutations
       (op-typed records) to ~/.mem0/outbox.jsonl. ~/.mem0/travel.json is still written, but
       only so this script's own 'status' verb can report ON/OFF — the shim never reads it.
  OFF  replay the outbox to the your-machine authority, remove travel.json, stop the local services.

  The one-brain rule holds because the shim never writes to the replica — new memories go to
  the outbox and are applied by the authority on reconnect.

  HARD GUARD: refuses to run 'on'/'off' on the authority machine — restoring a snapshot there
  would overwrite the live brain with day-old data, and 'off' would stop the live services.
  The guard reads LOCAL receipts (~/.mem0/role and the authority URL), never a hostname
  parameter: the previous parameter defaulted to an operator-neutral placeholder, so it never
  fired anywhere, and deriving the hostname from the authority URL is equally vacuous on the
  brain (its authority is loopback). Fail-closed: an undeterminable role refuses too.

  REFRESH (1.35.2) seed the local snapshot cache from pCloud while the authority is reachable, and
       touch nothing else. The mem0-replica-cache-refresh task runs it when the offline watcher
       finds a refresh due, so a takeover restores last night's set rather than whatever an
       operator last copied down. It writes ~\.claude\state\replica-cache-refresh.json and exits 1 when
       the cache is not left holding pCloud's newest restorable set; 0 when it is, or when another refresh
       already holds the lock.

.EXAMPLE
  travel-mode.ps1 status
  travel-mode.ps1 on
  travel-mode.ps1 off
  travel-mode.ps1 refresh
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('on', 'off', 'status', 'refresh')]
    [string]$Mode = 'status',

    # Snapshot source. LEAVE EMPTY — 'on' resolves it: a COMPLETE set on local disk wins, else the
    # pCloud copy with a loud warning. P: is pCloud's VIRTUAL drive: it STREAMS files from the
    # cloud, so restoring from it fails in the one scenario travel mode exists for (found
    # 2026-07-14 — the first end-to-end test only passed because the laptop still had internet).
    # AMS-11 (W4): the placeholder defaults below are LAST-RESORT only. Unset
    # params are resolved at RUNTIME from receipts that already exist on every
    # installed box (see Resolve-TravelDefaults). The operator-neutral
    # 'your-machine' literals made travel mode resolve to directories that
    # exist nowhere, so the replica restore has never once worked.
    [string]$BackupDir = '',
    [string]$LocalBackupDir = '',
    [string]$CloudBackupDir = '',

    [string]$Authority = '',                               # Tailscale MagicDNS — resolves home and away
    [string]$Replica   = 'http://127.0.0.1:18791',
    [switch]$Force,
    # Pre-flight: resolve + seed the snapshot source and print what WOULD happen, touching no
    # services. Use before a trip to answer "will travel mode work if I lose internet right now?"
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$Distro = $env:MEM0_WSL_DISTRO
if (-not $Distro) {
    # AMS-11 (W4): the installer receipt knows the distro; the old hardcoded
    # 'Ubuntu' was wrong on any box using a different distro name.
    try {
        $cfgP = Join-Path $env:USERPROFILE '.claude\scripts\mem0-stack.config.psd1'
        if (Test-Path $cfgP) { $Distro = (Import-PowerShellDataFile $cfgP).Distro }
    } catch {}
}
if (-not $Distro) { $Distro = 'Ubuntu' }

# ── AMS-11 (W4): runtime resolution + the ONE-BRAIN GUARD ────────────────────
# The guard that stops a replica-restore from destroying the live brain must
# key on facts that are AUTHORITATIVE ON THIS BOX. It previously compared
# $env:COMPUTERNAME against an -AuthorityHost parameter whose default was the
# operator-neutral literal 'your-machine' — so it never fired anywhere.
#
# Deriving that hostname from the authority URL (the obvious fix) is ALSO
# vacuous: on the brain the authority URL is loopback, so the derived host is
# '127.0.0.1', which never equals the machine name — the guard would look
# alive and still never fire on the one machine it exists to protect.
#
# Two independent local facts decide it instead:
#   1. ~/.mem0/role == 'brain'      (written by the installer; W3 made the
#                                    server read the same receipt)
#   2. the authority URL is LOCAL   (a box whose authority is itself IS the
#                                    authority — the offline-watcher already
#                                    treats a local URL as "not a replica")
function Test-IsLocalAuthorityUrl([string]$url) {
    if (-not $url) { return $false }
    try { $h = ([System.Uri]$url).Host } catch { return $false }
    return ($h -in @('127.0.0.1', 'localhost', '::1', '[::1]')) -or ($h -ieq $env:COMPUTERNAME)
}

function Get-Mem0RoleLocal {
    # ~/.mem0/role inside WSL; empty when the receipt is absent (older install).
    try {
        $r = Wsl "cat ~/.mem0/role 2>/dev/null"
        return ("$($r | Select-Object -First 1)".Trim().ToLowerInvariant())
    } catch { return '' }
}

function Get-AuthorityUrlLocal {
    # Receipt first (Windows side), then the per-host file the MCP shim reads.
    try {
        $cfgP = Join-Path $env:USERPROFILE '.claude\scripts\mem0-stack.config.psd1'
        if (Test-Path $cfgP) {
            $u = (Import-PowerShellDataFile $cfgP).AuthorityUrl
            if ($u) { return [string]$u }
        }
    } catch {}
    try {
        $u = Wsl "cat ~/.mem0/authority-url 2>/dev/null"
        return ("$($u | Select-Object -First 1)".Trim())
    } catch { return '' }
}

function Assert-NotTheAuthority([string]$verb) {
    $role = Get-Mem0RoleLocal
    $auth = Get-AuthorityUrlLocal
    $isLocalAuth = Test-IsLocalAuthorityUrl $auth
    if ($role -eq 'brain' -or $isLocalAuth) {
        $why = if ($role -eq 'brain') { "~/.mem0/role says 'brain'" } else { "the authority URL ($auth) is this machine" }
        $harm = switch ($verb) {
            'on'      { 'overwrite the live brain with a day-old snapshot' }
            'refresh' { 'fill a replica snapshot cache on the machine those snapshots are taken from' }
            default   { 'stop and disable the LIVE mem0/qdrant' }
        }
        throw ("REFUSED: this machine ($env:COMPUTERNAME) IS the memory authority — $why. " +
               "'$verb' would $harm. " +
               'Travel mode is for a replica box. (If this box was genuinely demoted to a replica, fix ~/.mem0/role and the authority URL first.)')
    }
    if (-not $role -and -not $auth) {
        throw "REFUSED: cannot determine this box's role (no ~/.mem0/role and no authority URL). Refusing '$verb' fail-closed rather than risk running on the authority."
    }
}

function Resolve-TravelDefaults {
    # Backup dirs are named after the AUTHORITY host by convention
    # (P:\memory-backups\<host> is the live layout). Derive it, normalised, and
    # refuse IP literals — 'D:\memory-backups\127.0.0.1' is not a real path.
    $auth = Get-AuthorityUrlLocal
    if (-not $script:Authority) { $script:Authority = $auth }
    $host_ = ''
    if ($script:Authority) {
        try { $host_ = ([System.Uri]$script:Authority).Host } catch { $host_ = '' }
    }
    $host_ = ($host_ -split '\.')[0].ToLowerInvariant()   # strip domain, normalise
    if ($host_ -match '^\d+$' -or $host_ -in @('127', 'localhost', '')) { $host_ = '' }
    if (-not $script:LocalBackupDir) {
        $script:LocalBackupDir = if ($host_) { "D:\memory-backups\$host_" } else { '' }
    }
    if (-not $script:CloudBackupDir) {
        $script:CloudBackupDir = if ($host_) { "P:\memory-backups\$host_" } else { '' }
    }
    if (-not $script:LocalBackupDir -or -not $script:CloudBackupDir) {
        throw ("REFUSED: cannot derive the backup directories — the authority URL " +
               "('$($script:Authority)') yields no usable host name. Pass -LocalBackupDir and " +
               '-CloudBackupDir explicitly, or fix ~/.mem0/authority-url to name the authority by host.')
    }
}

# A snapshot SET is only usable if all four artifacts share a timestamp — P: regularly holds
# orphans (a manifest whose qdrant snapshot never finished uploading). Everything below reasons
# about complete SETS, never about "does the directory exist" or "how fresh are the files":
# an existing-but-empty local dir must not shadow a good cloud set, and a 30-day-old local set
# must not shadow a 4-day-old cloud one.
function Get-SetFiles([string]$dir, [string]$stamp, [switch]$WithMedia) {
    $files = @("manifest-$stamp.json", "episodic-$stamp.db", "history-$stamp.db", "qdrant-$stamp.snapshot") |
             ForEach-Object { Join-Path $dir $_ }
    # -WithMedia adds the set's media tar, for the callers that COPY or PRUNE a set. Completeness never asks
    # for it: it is optional (absent from sets made before 1.35.0 and from sets made while no media memory existed).
    if ($WithMedia) { $files = @($files) + @(Get-SetMediaFile $dir $stamp) }
    return $files
}

function Get-SetMediaFile([string]$dir, [string]$stamp) {
    # The media tar (media-<ts>.tar) the set's manifest names as files.media, when it is also in the directory.
    # The name comes from the manifest, so only a plain file name is trusted; anything else is no file.
    try {
        $m = Get-Content -Raw -LiteralPath (Join-Path $dir "manifest-$stamp.json") -ErrorAction Stop | ConvertFrom-Json
        if ($null -eq $m) { throw 'the manifest is empty' }
        $name = $m.files.media
    } catch {
        # A manifest that cannot be read cannot say what the set holds, but stack-backup.sh always names the tar
        # media-<ts>.tar. Falling back to that name keeps retention from skipping the set's tar for good (the
        # prune is driven by the manifests that are there, and an unreadable one would leave it behind forever).
        $f = Join-Path $dir "media-$stamp.tar"
        if (Test-Path -LiteralPath $f) { return @($f) }
        return @()
    }
    if ($name -is [string] -and $name -match '^[A-Za-z0-9][A-Za-z0-9._-]*\.tar$') {
        $f = Join-Path $dir $name
        if (Test-Path -LiteralPath $f) { return @($f) }
    }
    return @()
}

function Test-SetMediaMissing([string]$cloudDir, [string]$localDir, [string]$stamp) {
    # True when the cloud copy of this set has its media tar and the local copy does not hold it whole (absent, or
    # another size: a torn copy). Completeness never asks for the tar, so a set whose tar copy failed, or that a
    # travel-mode from before 1.35.1 seeded without it, looks complete, and "is the cloud stamp newer" alone
    # would never fetch it.
    foreach ($src in @(Get-SetMediaFile $cloudDir $stamp)) {
        $dst = Join-Path $localDir (Split-Path $src -Leaf)
        if (-not (Test-Path -LiteralPath $dst)) { return $true }
        if ((Get-Item -LiteralPath $dst).Length -ne (Get-Item -LiteralPath $src).Length) { return $true }
    }
    return $false
}

function Get-NewestCompleteSet([string]$dir) {
    # Returns the newest stamp whose four artifacts are ALL present, or $null. Never throws:
    # a missing/dead drive is just "no sets here".
    try {
        if (-not (Test-Path $dir)) { return $null }
        $manifests = @(Get-ChildItem (Join-Path $dir 'manifest-*.json') -File -ErrorAction Stop | Sort-Object Name -Descending)
    } catch { return $null }
    foreach ($m in $manifests) {
        $s = $m.BaseName -replace '^manifest-', ''
        if (-not (Get-SetFiles $dir $s | Where-Object { -not (Test-Path $_) })) { return $s }
    }
    return $null
}

function Get-CompleteStamps([string]$dir) {
    # Every stamp in $dir whose four required files are present, newest first. Never throws.
    try {
        @(Get-ChildItem (Join-Path $dir 'manifest-*.json') -File -ErrorAction Stop | Sort-Object Name -Descending |
          ForEach-Object { $_.BaseName -replace '^manifest-', '' } |
          Where-Object { -not (Get-SetFiles $dir $_ | Where-Object { -not (Test-Path $_) }) })
    } catch { @() }
}

function Invoke-CacheRetention([string]$LocalBackupDir, [int]$Keep, [string]$Protect = '') {
    # Keep the newest $Keep COMPLETE sets plus $Protect (a set being copied, whose finished files a retry
    # must not fetch again); remove every other set's files, an incomplete set's included. Counting only
    # complete sets means a torn copy can never push a restorable set out of the cache.
    # ($keepStamps, not $keep: PowerShell names are case-insensitive, and $keep IS the [int] $Keep parameter.)
    $keepStamps = @(Get-CompleteStamps $LocalBackupDir | Select-Object -First $Keep) + @($Protect | Where-Object { $_ })
    Get-ChildItem (Join-Path $LocalBackupDir 'manifest-*.json') -File -ErrorAction SilentlyContinue |
        ForEach-Object { $_.BaseName -replace '^manifest-', '' } |
        Where-Object { $keepStamps -notcontains $_ } |
        ForEach-Object { Get-SetFiles $LocalBackupDir $_ -WithMedia | Where-Object { Test-Path $_ } | Remove-Item -Force }
}

function Get-SetProfile([string]$dir, [string]$stamp, [string]$LegacyProfile) {
    # The embedding profile a set was made in: the manifest's embed_profile, else the legacy space (a set
    # from before profiles existed), exactly as restore-replica.ps1 reads it.
    try {
        $m = Get-Content -Raw -LiteralPath (Join-Path $dir "manifest-$stamp.json") -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($m.embed_profile) { return "$($m.embed_profile)".Trim() }
    } catch {}
    return $LegacyProfile
}

function Update-LocalCache([string]$CloudBackupDir, [string]$LocalBackupDir, [string]$BoxProfile = '', [string]$LegacyProfile = '') {
    # Seed/refresh the LOCAL cache from pCloud. The caller decides we are online (Test-Endpoint on the
    # authority, not a probe of P:: pCloud keeps its drive letter mounted with no network, so a
    # filesystem probe can stall). Copies only the newest COMPLETE cloud set (not every file in the
    # folder), and only when it is newer than what we already hold. Never throws: returns what it
    # found and did, with the error text when something failed. A failed copy leaves no .part behind and
    # never removes a complete set it was not replacing.
    # 1.35.2: $BoxProfile (the embedding profile this box restores, when known) gates the copy. A set made
    # in another space is neither copied nor allowed to push the restorable sets out: restore-replica.ps1
    # refuses such a set, and a cache filled with them would leave the takeover nothing to restore.
    $r = [ordered]@{ cloud_stamp = $null; local_stamp = $null; local_set_epoch = $null; seeded = $false; error = $null }
    try {
        if (Test-Path -LiteralPath $LocalBackupDir) {
            # A copy killed mid-file (task time limit, reboot, full disk) leaves <file>.part, which no
            # retention by set name ever matches. One refresh runs at a time (the lock), so any .part is dead.
            Get-ChildItem -LiteralPath $LocalBackupDir -Filter '*.part' -File -ErrorAction SilentlyContinue | Remove-Item -Force
        }
        $cloudStamp = Get-NewestCompleteSet $CloudBackupDir
        $localStamp = Get-NewestCompleteSet $LocalBackupDir
        $r.cloud_stamp = $cloudStamp
        $setProfile = if ($cloudStamp -and $BoxProfile) { Get-SetProfile $CloudBackupDir $cloudStamp $LegacyProfile } else { '' }
        if ($cloudStamp -and $BoxProfile -and $setProfile -and $setProfile -ne $BoxProfile) {
            throw ("pCloud's newest set $cloudStamp is in embedding profile '$setProfile' but this box restores '$BoxProfile': " +
                   "not copied, and the cache keeps the sets it can restore. Switch this replica first: install.ps1 -Role replica -EmbedProfile $setProfile")
        }
        # A newer cloud set, or the same set whose media tar the local copy lacks (the tar is copied
        # last and is not part of "complete", so a failed copy leaves the set looking whole). The copy
        # loop below skips every file already held at the right size, so the second case fetches
        # only the tar.
        if ($cloudStamp -and (($cloudStamp -gt "$localStamp") -or
                              ($cloudStamp -eq "$localStamp" -and (Test-SetMediaMissing $CloudBackupDir $LocalBackupDir $cloudStamp)))) {
            New-Item -ItemType Directory -Force -Path $LocalBackupDir | Out-Null
            # Make room FIRST: keep the newest 2 complete sets (3 with the incoming one). Pruning only after
            # the copy wedged a disk with room for 3 sets but not 4: every copy failed, so nothing was pruned.
            Invoke-CacheRetention $LocalBackupDir 2 -Protect $cloudStamp
            Write-Host "    seeding local snapshot cache from pCloud: $cloudStamp"
            foreach ($src in (Get-SetFiles $CloudBackupDir $cloudStamp -WithMedia)) {
                $dst = Join-Path $LocalBackupDir (Split-Path $src -Leaf)
                if ((Test-Path $dst) -and (Get-Item $dst).Length -eq (Get-Item $src).Length) { continue }
                # .part + atomic same-volume rename: a torn 686MB copy (sleep, Ctrl-C,
                # pCloud stall) must never masquerade as a complete set on the plane.
                try {
                    Copy-Item $src "$dst.part" -Force -ErrorAction Stop
                    Move-Item "$dst.part" $dst -Force -ErrorAction Stop
                } catch {
                    Remove-Item -LiteralPath "$dst.part" -Force -ErrorAction SilentlyContinue
                    throw
                }
            }
            $r.seeded = $true
        }
        # Retention on every pass (not only after a copy), so a prune that failed once is retried:
        # keep the newest 3 complete sets (~721MB each).
        if (Test-Path -LiteralPath $LocalBackupDir) { Invoke-CacheRetention $LocalBackupDir 3 }
    } catch {
        $r.error = $_.Exception.Message
    }
    $r.local_stamp = Get-NewestCompleteSet $LocalBackupDir
    if ($r.local_stamp) {
        try { $r.local_set_epoch = [DateTimeOffset]::new((Get-Item -LiteralPath (Join-Path $LocalBackupDir "manifest-$($r.local_stamp).json")).LastWriteTimeUtc).ToUnixTimeSeconds() } catch {}
    }
    [pscustomobject]$r
}

function Get-RefreshRecordPath { Join-Path $env:USERPROFILE '.claude\state\replica-cache-refresh.json' }

function Read-RefreshRecord([string]$Path) {
    # $null when absent or unreadable: a torn record must never stop the next refresh.
    try { if (Test-Path -LiteralPath $Path) { return (Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json -ErrorAction Stop) } } catch {}
    return $null
}

function Write-RefreshRecord([string]$Path, $Record) {
    # tmp + rename, so the watcher never reads half a record. The rename is retried: it fails while a
    # reader (the watcher tick, Test-MemoryStack) has the file open, and a lost final write would leave
    # a 'running' record behind a refresh that finished.
    New-Item -ItemType Directory -Force -Path (Split-Path $Path) | Out-Null
    $tmp = "$Path.$PID.tmp"
    ([pscustomobject]$Record | ConvertTo-Json -Compress) | Set-Content -LiteralPath $tmp -Encoding utf8
    for ($i = 1; ; $i++) {
        try { [IO.File]::Move($tmp, $Path, $true); return }
        catch { if ($i -ge 20) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue; throw }; Start-Sleep -Milliseconds 100 }
    }
}

function New-RunningRecord {
    # Written when a refresh starts. It keeps the previous result's fields (ok, stamps, error), so the
    # health row and the backoff still see how the last refresh ended while this one runs, or if it dies.
    param($Prior, [int64]$StartedEpoch)
    $rec = [ordered]@{ state = 'running'; started_epoch = $StartedEpoch; pid = $PID }
    if ($Prior) {
        foreach ($k in 'ok', 'finished_epoch', 'finished', 'cloud_stamp', 'local_stamp', 'local_set_epoch', 'local_dir', 'error') {
            if ($Prior.PSObject.Properties[$k]) { $rec[$k] = $Prior.$k }
        }
    }
    $rec
}

function New-RefreshResult {
    # The record a finished refresh writes. ok means the local cache now holds the newest complete set
    # pCloud has. local_stamp is what a takeover would restore; it comes from the disk after the attempt
    # (errors or not), or from the previous record when this run never reached the cache.
    param($Seed, $Prior, [int64]$StartedEpoch, [int64]$NowEpoch, [string]$LocalDir, [string]$CloudDir, [string]$ErrorText = '',
          [string]$FallbackLocalStamp = '')
    $err = if ($ErrorText) { $ErrorText } elseif ($Seed -and $Seed.error) { [string]$Seed.error } else { $null }
    $cloud = if ($Seed) { $Seed.cloud_stamp } else { $null }
    $local = if ($Seed) { $Seed.local_stamp } elseif ($FallbackLocalStamp) { $FallbackLocalStamp } elseif ($Prior) { $Prior.local_stamp } else { $null }
    $setEpoch = if ($Seed) { $Seed.local_set_epoch } elseif ($Prior -and "$local" -eq "$($Prior.local_stamp)") { $Prior.local_set_epoch } else { $null }
    if (-not $err -and -not $cloud) { $err = "no complete snapshot set in $CloudDir" }
    $ok = (-not $err) -and [bool]$cloud -and ("$local" -eq "$cloud")
    if (-not $err -and -not $ok) { $err = "the local cache holds '$local' but pCloud's newest complete set is '$cloud'" }
    [ordered]@{
        state           = 'done'
        ok              = $ok
        started_epoch   = $StartedEpoch
        finished_epoch  = $NowEpoch
        finished        = [DateTimeOffset]::FromUnixTimeSeconds($NowEpoch).ToString('o')
        cloud_stamp     = $cloud
        local_stamp     = $local
        local_set_epoch = $setEpoch
        seeded          = [bool]($Seed -and $Seed.seeded)
        local_dir       = $LocalDir
        error           = $err
    }
}

function Get-BoxEmbedProfile {
    # The embedding profile this box restores (ep.active(), as restore-replica.ps1 asks it) and the legacy
    # profile name, from the deployed server inside WSL. Both '' when WSL or the module does not answer:
    # the refresh then copies without the profile gate, as before 1.35.2.
    # On stdin, as restore-replica.ps1 sends its probe: no quoting through wsl.exe's command line.
    $py = "import os, sys`nsys.path.insert(0, os.path.expanduser('~/apps/mem0-server'))`nimport embedder_profile as ep`nprint(ep.active().name + ' ' + ep.LEGACY_PROFILE)`n"
    try {
        $out = "$($py | wsl.exe -d $Distro -e python3 - 2>$null | Select-Object -First 1)".Trim()
        if ($out -match '^([a-z0-9][a-z0-9-]*) ([a-z0-9][a-z0-9-]*)$') { return @($Matches[1], $Matches[2]) }
    } catch {}
    return @('', '')
}

function Wsl([string]$cmd) { wsl.exe -d $Distro -e bash -lc $cmd }

function Test-Endpoint([string]$url) {
    try { $r = Invoke-RestMethod "$url/health" -TimeoutSec 5; return [bool]$r.ok } catch { return $false }
}

function Get-OutboxCount {
    $n = Wsl 'test -f ~/.mem0/outbox.jsonl && wc -l < ~/.mem0/outbox.jsonl || echo 0'
    return [int]("$($n | Select-Object -First 1)".Trim())
}

function Get-TravelState {
    $raw = Wsl 'cat ~/.mem0/travel.json 2>/dev/null || true'
    if (-not "$raw".Trim()) { return $null }
    try { return ("$raw" | ConvertFrom-Json) } catch { return $null }
}

function Show-Status {
    $t = Get-TravelState
    Write-Host ""
    Write-Host "  travel mode : $(if ($t) { "ON since $($t.since) (replica + outbox)" } else { 'OFF (live authority)' })"
    Write-Host "  authority   : $Authority  -> $(if (Test-Endpoint $Authority) { 'reachable' } else { 'UNREACHABLE' })"
    Write-Host "  replica     : $Replica  -> $(if (Test-Endpoint $Replica) { 'running' } else { 'stopped' })"
    Write-Host "  outbox      : $(Get-OutboxCount) queued memories"
    Write-Host ""
}

switch ($Mode) {

  'status' {
      # 'status' is the DIAGNOSTIC verb: it must work on every box, including
      # the authority (where the guard refuses the mutating verbs) and a box
      # whose backup dirs cannot be derived. So resolve only the authority URL
      # here — never Resolve-TravelDefaults, which throws when the host is an
      # IP literal. Without this the row printed 'authority: -> UNREACHABLE'
      # everywhere once the placeholder default was removed.
      if (-not $Authority) { $Authority = Get-AuthorityUrlLocal }
      Show-Status
      break
  }

  'on' {
      # HARD GUARD — never on the authority machine: the restore would stop the live mem0 and
      # overwrite the real brain with a day-old snapshot. Keyed on local receipts (AMS-11/W4).
      Assert-NotTheAuthority 'on'
      Resolve-TravelDefaults
      Write-Host "==> Travel mode ON"

      # 0. Seed/refresh the LOCAL cache from pCloud while we still have internet. Gated on the
      #    authority health check, not on probing P: — pCloud keeps its drive letter mounted with
      #    no network, so a filesystem probe can stall; Test-Endpoint is a fast, honest "am I
      #    online" signal. Copies only the newest COMPLETE cloud set (not every file in the
      #    folder), and only when it is newer than what we already hold.
      if (-not $BackupDir) {
          $online = Test-Endpoint $Authority
          if ($online) {
              $bp = Get-BoxEmbedProfile
              $seed = Update-LocalCache $CloudBackupDir $LocalBackupDir -BoxProfile $bp[0] -LegacyProfile $bp[1]
              if ($seed.error) {
                  Write-Host "    (could not refresh from pCloud: $($seed.error) — using the local cache as-is)" -ForegroundColor DarkYellow
              }
          }
      }

      # 1. Resolve the snapshot SOURCE: a local COMPLETE set wins; else fall back to the pCloud
      #    copy with a loud warning (that path cannot work offline — which is the whole point).
      if (-not $BackupDir) {
          if (Get-NewestCompleteSet $LocalBackupDir) {
              $BackupDir = $LocalBackupDir
          } elseif (Get-NewestCompleteSet $CloudBackupDir) {
              $BackupDir = $CloudBackupDir
          } else {
              throw "No COMPLETE snapshot set in $LocalBackupDir or $CloudBackupDir (need manifest + episodic + history + qdrant sharing a timestamp). Is the your-machine nightly backup running?"
          }
      }
      # Warn on the RESOLVED path, so an explicit -BackupDir P:\... is not silently trusted either.
      if ([IO.Path]::GetFullPath($BackupDir).TrimEnd('\') -ieq [IO.Path]::GetFullPath($CloudBackupDir).TrimEnd('\')) {
          Write-Host "    WARNING: restoring from $CloudBackupDir — that is pCloud's STREAMING drive." -ForegroundColor Yellow
          Write-Host "             It works now (you are online) but WILL FAIL with no internet." -ForegroundColor Yellow
          Write-Host "             Seed the offline cache before you travel: copy the newest complete set to $LocalBackupDir" -ForegroundColor Yellow
      }

      $stamp = Get-NewestCompleteSet $BackupDir
      if (-not $stamp) { throw "No COMPLETE snapshot set in $BackupDir." }
      $age = [math]::Round(((Get-Date) - (Get-Item (Join-Path $BackupDir "manifest-$stamp.json")).LastWriteTime).TotalHours, 1)
      Write-Host "    snapshot: $stamp from $BackupDir  (replica will be $age h behind the authority)"
      if ($age -gt 72) { Write-Host "    WARNING: that snapshot is $([math]::Round($age/24,1)) DAYS old — recent memories will be missing offline." -ForegroundColor Yellow }

      if ($DryRun) {
          $offlineSafe = [IO.Path]::GetFullPath($BackupDir).TrimEnd('\') -ine [IO.Path]::GetFullPath($CloudBackupDir).TrimEnd('\')
          Write-Host "    DRY RUN — nothing was changed."
          Write-Host "    offline-safe: $(if ($offlineSafe) { 'YES — the snapshot is on local disk; travel mode will work with no internet' } else { 'NO — would restore from the streaming cloud drive' })" -ForegroundColor $(if ($offlineSafe) { 'Green' } else { 'Yellow' })
          break
      }

      # 2. Drain the outbox FIRST if the authority is still reachable — never strand writes
      if ((Get-OutboxCount) -gt 0 -and (Test-Endpoint $Authority)) {
          Write-Host "    outbox has queued memories and the authority is still reachable — replaying before going offline"
          & "$PSScriptRoot\replay-outbox.ps1" -Authority $Authority -Distro $Distro
      }

      # 3. Restore into the LOCAL stack
      & "$PSScriptRoot\restore-replica.ps1" -BackupDir $BackupDir -Stamp $stamp -Distro $Distro

      # 1.35.2: the restore marker names the SET it restored ("<time> <stamp>"), written here, after a
      # restore that did not throw, by every caller (the watcher's takeover and an operator alike). The
      # watcher restores again at the next takeover whenever the cache's newest complete set is newer than
      # this stamp. Comparing clock times instead (marker time against the refresh's finish time) missed a
      # set seeded while a restore ran, and every set seeded by hand.
      $marker = Join-Path $env:USERPROFILE '.claude\state\replica-restored.txt'
      New-Item -ItemType Directory -Force -Path (Split-Path $marker) | Out-Null
      Set-Content -LiteralPath $marker -Value "$((Get-Date).ToUniversalTime().ToString('o')) $stamp" -Encoding utf8

      # 4. Flip: travel.json is what the shim actually reads (per call, host-pinned).
      $state = @{ mode = 'travel'; host = $env:COMPUTERNAME; replica_url = $Replica
                  since = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz'); snapshot = $stamp } | ConvertTo-Json -Compress
      Wsl "cat > ~/.mem0/travel.json <<'EOF'
$state
EOF"
      # v1.23 P2-7 (spec §7 defect 2): the Windows hooks read ~\.mem0\authority-url and it stays
      # pointed at the AUTHORITY. Reads fail over to the replica inside the hooks
      # (source=local-replica); failed writes queue to the Outbox. Nothing rewrites MEM0_URL any
      # more, so no hook can ever post into the disposable store.
      Write-Host "    hooks keep the authority in ~\.mem0\authority-url; reads fail over to the replica, writes queue to the Outbox"

      Write-Host "    shim -> replica reads + outbox writes (live for ALL sessions, no restart needed)"
      Write-Host "    NOTE: PowerShell memory hooks in already-running sessions keep the old URL until those sessions restart."
      Write-Host "==> Travel mode ON. Recall works offline. Run 'travel-mode.ps1 off' when you're back online." -ForegroundColor Green
      Show-Status
      break
  }

  'refresh' {
      # 1.35.2: keep the takeover snapshot current. Before this, the cache was refreshed only by 'on'
      # (at a takeover, when the authority is down and the refresh is skipped) or by an operator running
      # 'on -DryRun', so a replica took over with whatever set was last copied by hand: on 2026-10-11
      # both PC replicas held a set two days old and nothing would have refreshed it. The
      # mem0-replica-cache-refresh task runs this when the offline watcher finds a refresh due; it
      # touches no service and no model, only the files under the local cache folder.
      $recPath = Get-RefreshRecordPath
      $lockPath = [IO.Path]::ChangeExtension($recPath, '.lock')
      New-Item -ItemType Directory -Force -Path (Split-Path $recPath) | Out-Null
      # One refresh at a time. The OS releases the lock if this process dies, so it can never go stale.
      # A second run while one holds it exits 0 and writes nothing: the running one owns the record.
      # (The task's IgnoreNew already stops a second task instance; this covers a run by hand.)
      $lock = $null; $refreshFailed = $false
      try { $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
      catch { Write-Host "==> replica cache refresh already running (lock held: $lockPath); nothing to do"; exit 0 }
      try {
          $prior = Read-RefreshRecord $recPath
          $started = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
          # A 'running' record first: the watcher waits on it instead of starting another refresh, and a
          # run that dies leaves the record that says when it started.
          Write-RefreshRecord $recPath (New-RunningRecord -Prior $prior -StartedEpoch $started)
          $seed = $null; $failure = ''
          try {
              # The guard runs inside the recorded section on purpose: a refusal (or a WSL that does not
              # answer the role question) still writes a failed record, so the watcher waits $RetryHours
              # instead of starting the task again every two minutes.
              Assert-NotTheAuthority 'refresh'
              Resolve-TravelDefaults
              Write-Host "==> replica cache refresh: $CloudBackupDir -> $LocalBackupDir"
              # Only while the authority answers: with no network pCloud's drive can stall instead of failing.
              if (Test-Endpoint $Authority) {
                  $bp = Get-BoxEmbedProfile
                  $seed = Update-LocalCache $CloudBackupDir $LocalBackupDir -BoxProfile $bp[0] -LegacyProfile $bp[1]
              }
              else { $failure = "the authority $Authority is unreachable; a refresh runs only while online" }
          } catch { $failure = $_.Exception.Message }
          # A run that never reached the cache still says what a takeover would restore, read from the disk.
          $onDisk = if (-not $seed -and $LocalBackupDir) { "$(Get-NewestCompleteSet $LocalBackupDir)" } else { '' }
          $result = New-RefreshResult -Seed $seed -Prior $prior -StartedEpoch $started -NowEpoch ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) `
                                      -LocalDir $LocalBackupDir -CloudDir $CloudBackupDir -ErrorText $failure -FallbackLocalStamp $onDisk
          Write-RefreshRecord $recPath $result
          if ($result.ok) {
              Write-Host "    cache holds $($result.local_stamp), the newest complete set$(if ($result.seeded) { ' (copied now)' } else { ' (already held)' })" -ForegroundColor Green
          } else {
              Write-Host "    NOT refreshed: $($result.error)" -ForegroundColor Yellow
              $refreshFailed = $true
          }
      } finally { if ($lock) { $lock.Dispose() } }
      # The exit code is the task's LastTaskResult. Both outcomes are explicit: falling off the end would
      # leave whatever the last native call returned (a wsl.exe probe), not the refresh's own result.
      if ($refreshFailed) { exit 1 }
      exit 0
  }

  'off' {
      # HARD GUARD — 'off' stops+disables mem0/qdrant in WSL. On the authority machine those ARE
      # the live brain, so this must refuse there just as 'on' does (they share the same Wsl calls).
      Assert-NotTheAuthority 'off'
      Resolve-TravelDefaults
      Write-Host "==> Travel mode OFF"

      if (-not (Test-Endpoint $Authority)) {
          if (-not $Force) { throw "Authority $Authority is UNREACHABLE. Staying in travel mode so the outbox isn't stranded. (Is your-machine awake? Is Tailscale up?) Use -Force to flip anyway and replay later." }
          Write-Host "    -Force: flipping without replay; the outbox stays queued" -ForegroundColor Yellow
      } else {
          $n = Get-OutboxCount
          if ($n -gt 0) {
              Write-Host "    replaying $n queued memories to the authority"
              & "$PSScriptRoot\replay-outbox.ps1" -Authority $Authority -Distro $Distro
          } else {
              Write-Host "    outbox empty — nothing to replay"
          }
      }

      # Point back at the authority: remove the state file the shim reads. (v1.23: the hooks'
      # authority file was never changed, so there is no env var to restore.)
      Wsl 'rm -f ~/.mem0/travel.json' | Out-Null

      # Stop + disable the local replica — one live brain, always
      Wsl 'systemctl --user stop mem0.service qdrant.service 2>/dev/null; systemctl --user disable mem0.service qdrant.service 2>/dev/null; true' | Out-Null
      Write-Host "    local replica stopped + disabled (one-brain rule)"
      Write-Host "==> Travel mode OFF. Memory is back on the your-machine authority." -ForegroundColor Green
      Show-Status
      break
  }
}
