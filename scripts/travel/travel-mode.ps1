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

  HARD GUARD: refuses to run 'on' on the authority machine itself ($AuthorityHost) — restoring
  a snapshot there would overwrite the live brain with day-old data.

.EXAMPLE
  travel-mode.ps1 status
  travel-mode.ps1 on
  travel-mode.ps1 off
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('on', 'off', 'status')]
    [string]$Mode = 'status',

    # Snapshot source. LEAVE EMPTY — 'on' resolves it: a COMPLETE set on local disk wins, else the
    # pCloud copy with a loud warning. P: is pCloud's VIRTUAL drive: it STREAMS files from the
    # cloud, so restoring from it fails in the one scenario travel mode exists for (found
    # 2026-07-14 — the first end-to-end test only passed because the laptop still had internet).
    [string]$BackupDir = '',
    [string]$LocalBackupDir = 'D:\memory-backups\your-machine',
    [string]$CloudBackupDir = 'P:\memory-backups\your-machine',

    [string]$Authority = 'http://your-machine:18791',      # Tailscale MagicDNS — resolves home and away
    [string]$Replica   = 'http://127.0.0.1:18791',
    [string]$AuthorityHost = 'your-machine',               # machine that OWNS the live brain — on/off refuse to run there
    [switch]$Force,
    # Pre-flight: resolve + seed the snapshot source and print what WOULD happen, touching no
    # services. Use before a trip to answer "will travel mode work if I lose internet right now?"
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$Distro = $env:MEM0_WSL_DISTRO; if (-not $Distro) { $Distro = 'Ubuntu' }   # laptop=Ubuntu, your-machine=Ubuntu-ML

# A snapshot SET is only usable if all four artifacts share a timestamp — P: regularly holds
# orphans (a manifest whose qdrant snapshot never finished uploading). Everything below reasons
# about complete SETS, never about "does the directory exist" or "how fresh are the files":
# an existing-but-empty local dir must not shadow a good cloud set, and a 30-day-old local set
# must not shadow a 4-day-old cloud one.
function Get-SetFiles([string]$dir, [string]$stamp) {
    return @("manifest-$stamp.json", "episodic-$stamp.db", "history-$stamp.db", "qdrant-$stamp.snapshot") |
           ForEach-Object { Join-Path $dir $_ }
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

  'status' { Show-Status; break }

  'on' {
      # HARD GUARD — never on the authority machine: the restore would stop the live mem0 and
      # overwrite the real brain (67k+ memories) with a day-old snapshot.
      if ($env:COMPUTERNAME -ieq $AuthorityHost) {
          throw "REFUSED: this machine ($env:COMPUTERNAME) IS the memory authority. Travel mode is for the laptop. (Override only by editing -AuthorityHost, deliberately.)"
      }
      Write-Host "==> Travel mode ON"

      # 0. Seed/refresh the LOCAL cache from pCloud while we still have internet. Gated on the
      #    authority health check, not on probing P: — pCloud keeps its drive letter mounted with
      #    no network, so a filesystem probe can stall; Test-Endpoint is a fast, honest "am I
      #    online" signal. Copies only the newest COMPLETE cloud set (not every file in the
      #    folder), and only when it is newer than what we already hold.
      if (-not $BackupDir) {
          $online = Test-Endpoint $Authority
          if ($online) {
              try {
                  $cloudStamp = Get-NewestCompleteSet $CloudBackupDir
                  $localStamp = Get-NewestCompleteSet $LocalBackupDir
                  if ($cloudStamp -and $cloudStamp -gt "$localStamp") {
                      New-Item -ItemType Directory -Force -Path $LocalBackupDir | Out-Null
                      Write-Host "    seeding local snapshot cache from pCloud: $cloudStamp"
                      foreach ($src in (Get-SetFiles $CloudBackupDir $cloudStamp)) {
                          $dst = Join-Path $LocalBackupDir (Split-Path $src -Leaf)
                          if ((Test-Path $dst) -and (Get-Item $dst).Length -eq (Get-Item $src).Length) { continue }
                          # .part + atomic same-volume rename: a torn 686MB copy (sleep, Ctrl-C,
                          # pCloud stall) must never masquerade as a complete set on the plane.
                          Copy-Item $src "$dst.part" -Force -ErrorAction Stop
                          Move-Item "$dst.part" $dst -Force -ErrorAction Stop
                      }
                      # Retention: keep the newest 3 complete sets (~721MB each), prune the rest.
                      $keep = @(Get-ChildItem (Join-Path $LocalBackupDir 'manifest-*.json') -File |
                                Sort-Object Name -Descending | Select-Object -First 3 |
                                ForEach-Object { $_.BaseName -replace '^manifest-', '' })
                      Get-ChildItem (Join-Path $LocalBackupDir 'manifest-*.json') -File |
                          ForEach-Object { $_.BaseName -replace '^manifest-', '' } |
                          Where-Object { $keep -notcontains $_ } |
                          ForEach-Object { Get-SetFiles $LocalBackupDir $_ | Where-Object { Test-Path $_ } | Remove-Item -Force }
                  }
              } catch {
                  Write-Host "    (could not refresh from pCloud: $($_.Exception.Message) — using the local cache as-is)" -ForegroundColor DarkYellow
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

      # 4. Flip: travel.json is what the shim actually reads (per call, host-pinned).
      $state = @{ mode = 'travel'; host = $env:COMPUTERNAME; replica_url = $Replica
                  since = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz'); snapshot = $stamp } | ConvertTo-Json -Compress
      Wsl "cat > ~/.mem0/travel.json <<'EOF'
$state
EOF"
      # Windows-side env for the PowerShell hooks (reads hit the replica; their episodic
      # checkpoints are ephemeral session banners and are NOT replayed — accepted loss).
      [Environment]::SetEnvironmentVariable('MEM0_URL', $Replica, 'User')
      $env:MEM0_URL = $Replica

      Write-Host "    shim -> replica reads + outbox writes (live for ALL sessions, no restart needed)"
      Write-Host "    NOTE: PowerShell memory hooks in already-running sessions keep the old URL until those sessions restart."
      Write-Host "==> Travel mode ON. Recall works offline. Run 'travel-mode.ps1 off' when you're back online." -ForegroundColor Green
      Show-Status
      break
  }

  'off' {
      # HARD GUARD — 'off' stops+disables mem0/qdrant in WSL. On the authority machine those ARE
      # the live brain, so this must refuse there just as 'on' does (they share the same Wsl calls).
      if ($env:COMPUTERNAME -ieq $AuthorityHost) {
          throw "REFUSED: this machine ($env:COMPUTERNAME) IS the memory authority — 'off' would stop and disable the LIVE mem0/qdrant. Travel mode is for the laptop."
      }
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

      # Point back at the authority: remove the state file the shim reads, restore hook env.
      Wsl 'rm -f ~/.mem0/travel.json' | Out-Null
      [Environment]::SetEnvironmentVariable('MEM0_URL', $Authority, 'User')
      $env:MEM0_URL = $Authority

      # Stop + disable the local replica — one live brain, always
      Wsl 'systemctl --user stop mem0.service qdrant.service 2>/dev/null; systemctl --user disable mem0.service qdrant.service 2>/dev/null; true' | Out-Null
      Write-Host "    local replica stopped + disabled (one-brain rule)"
      Write-Host "==> Travel mode OFF. Memory is back on the your-machine authority." -ForegroundColor Green
      Show-Status
      break
  }
}
