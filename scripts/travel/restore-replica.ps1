#Requires -PSEdition Core
<#
.SYNOPSIS
  Restore the newest your-machine snapshot into the laptop's LOCAL mem0 + Qdrant as a read-only replica.

.NOTES
  Called by travel-mode.ps1 on. Idempotent — re-running refreshes the replica to a newer snapshot.

  Qdrant is restored via the SNAPSHOT UPLOAD API, not by copying the storage directory. A raw
  directory copy is version-coupled and silently corrupts across Qdrant versions; the upload API
  is the supported path.
#>
param(
    # No default ON PURPOSE: the old 'P:\memory-backups\your-machine' default pointed at pCloud's STREAMING
    # drive, which cannot serve a restore offline. travel-mode.ps1 always passes the resolved
    # (local-first) dir; a direct caller must choose deliberately.
    [Parameter(Mandatory)][string]$BackupDir,
    [Parameter(Mandatory)][string]$Stamp,
    [string]$Distro = $(if ($env:MEM0_WSL_DISTRO) { $env:MEM0_WSL_DISTRO } else { 'Ubuntu' }),
    [string]$Collection = 'mem0_egemma_768'
)
$ErrorActionPreference = 'Stop'
function Wsl([string]$cmd) { wsl.exe -d $Distro -e bash -lc $cmd }

$epi  = "$BackupDir\episodic-$Stamp.db"
$hist = "$BackupDir\history-$Stamp.db"
$snap = "$BackupDir\qdrant-$Stamp.snapshot"
foreach ($f in @($epi, $hist, $snap)) { if (-not (Test-Path $f)) { throw "missing backup artifact: $f" } }

# Preconditions the hard way: jq is required by the snapshot chain (AMS issue #17), and a
# missing embedder means the replica can answer nothing.
if (-not (Wsl "command -v jq >/dev/null && echo ok")) { throw "jq is not installed in WSL ($Distro). Run: sudo apt-get install -y jq" }
if (-not (Wsl "curl -sf -m 5 http://127.0.0.1:11436/v1/models >/dev/null && echo ok")) {
    throw "The local embedder (llama-swap :11436) is not serving. The replica needs EmbeddingGemma@768 locally or recall returns nothing."
}

Write-Host "    stopping local mem0; starting qdrant (needed for the snapshot upload)"
Wsl "systemctl --user stop mem0.service 2>/dev/null; systemctl --user start qdrant.service 2>/dev/null; true" | Out-Null

# --- SQLite ledgers: straight copy (mem0 is stopped) ---
Write-Host "    restoring episodic + history ledgers"
$epiW  = "$(Wsl "wslpath '$($epi -replace '\\','/')'")".Trim()
$histW = "$(Wsl "wslpath '$($hist -replace '\\','/')'")".Trim()
Wsl "mkdir -p ~/.mem0 && rm -f ~/.mem0/episodic.db-shm ~/.mem0/episodic.db-wal ~/.mem0/history.db-shm ~/.mem0/history.db-wal && cp '$epiW' ~/.mem0/episodic.db && cp '$histW' ~/.mem0/history.db && echo ok" | Out-Null

# --- Qdrant collection: snapshot UPLOAD (version-safe) ---
Write-Host "    restoring Qdrant collection '$Collection' via snapshot upload"
$snapW = "$(Wsl "wslpath '$($snap -replace '\\','/')'")".Trim()
# Bounded wait (2 min) — an `until` loop with no cap hangs travel-mode 'on' forever if qdrant is broken
Wsl 'for i in $(seq 1 60); do curl -sf -m 3 http://127.0.0.1:6333/healthz >/dev/null && exit 0; sleep 2; done; echo QDRANT_TIMEOUT; exit 1' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "local qdrant did not come up within 2 minutes — check: wsl -d $Distro systemctl --user status qdrant.service" }
$out = Wsl "curl -s -m 900 -X POST 'http://127.0.0.1:6333/collections/$Collection/snapshots/upload?priority=snapshot' -H 'Content-Type: multipart/form-data' -F 'snapshot=@$snapW'"
if ($out -notmatch '"status"\s*:\s*"ok"') { throw "Qdrant snapshot upload failed: $out" }

Write-Host "    starting mem0"
Wsl "systemctl --user start mem0.service && sleep 4" | Out-Null

# --- Verify: the replica must actually answer ---
$health = Wsl "curl -sf -m 10 http://127.0.0.1:18791/health"
if ($health -notmatch '"ok"\s*:\s*true') { throw "replica mem0 did not come up healthy: $health" }
$pts = Wsl "curl -sf -m 10 'http://127.0.0.1:6333/collections/$Collection' | jq -r '.result.points_count'"
Write-Host "    replica live: $($pts.Trim()) memories restored" -ForegroundColor Green
