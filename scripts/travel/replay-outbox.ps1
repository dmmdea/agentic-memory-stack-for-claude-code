#Requires -PSEdition Core
<#
.SYNOPSIS
  Replay the offline operation-outbox to the your-machine memory authority.

.DESCRIPTION
  2026-07-15: superseded by scripts/wsl/replay-ops.py (handles every queued op — adds,
  updates, deletes, tier changes, goal/open-question mutations — not just adds). This
  wrapper is kept as the callable name travel-mode.ps1 uses: it verifies the API key and
  authority health, then invokes the generalized replayer inside WSL.

  Idempotency, adds-first ordering, the replayed-key ledger (~/.mem0/outbox.replayed.jsonl),
  conflict logging (~/.mem0/mutation-conflicts.jsonl), and the atomic outbox rotation all
  live in replay-ops.py.
#>
param(
    [string]$Authority = 'http://your-machine:18791',
    [string]$Distro = $(if ($env:MEM0_WSL_DISTRO) { $env:MEM0_WSL_DISTRO } else { 'Ubuntu' })
)
$ErrorActionPreference = 'Stop'
function Wsl([string]$cmd) { wsl.exe -d $Distro -e bash -lc $cmd }

# The API key lives in WSL (mode 600) — never printed, never copied to Windows.
$key = "$(Wsl 'cat ~/.mem0/api-key 2>/dev/null')".Trim()
if (-not $key) { throw "no mem0 api-key found in WSL ($Distro)" }

try { $h = Invoke-RestMethod "$Authority/health" -TimeoutSec 8 } catch { throw "authority $Authority unreachable — refusing to drain the outbox" }
if (-not $h.ok) { throw "authority $Authority unhealthy" }

# 2026-07-15: superseded by replay-ops.py (handles every op, not just adds). Kept as the callable
# name travel-mode.ps1 uses. Drains the operation-outbox to the authority; idempotent.
# Repo root derived from this script's location (scripts/travel/ -> two levels up) so the
# wrapper works on any machine regardless of where the repo is checked out.
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..' '..')).Path
$repoWsl = "$(Wsl "wslpath '$($repoRoot -replace '\\','/')'")".Trim()
if (-not $repoWsl) { throw "could not resolve repo root '$repoRoot' to a WSL path" }
$py = "$(Wsl 'command -v ~/apps/mem0-server/.venv/bin/python || command -v python3')".Trim()
if (-not $py) { throw "no python found in WSL ($Distro)" }
$out = Wsl "MEM0_URL='$Authority' '$py' '$repoWsl/scripts/wsl/replay-ops.py'"
Write-Host "    replay-ops: $out"
