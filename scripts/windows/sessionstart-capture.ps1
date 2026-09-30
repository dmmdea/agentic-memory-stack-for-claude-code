# sessionstart-capture.ps1 - SessionStart hook: capture the most-recent PRIOR session's transcript.
#
# WHY THIS EXISTS: the Stop hook covers a clean session end and PreCompact covers a long session
# that compacts, but a session that ends without either (a killed window, a crash, a closed laptop)
# is never extracted. A new session's start is the one moment the previous one is certainly over.
# (Written 2026-06-24, when the PER-TURN hooks (Stop / UserPromptSubmit / PreToolUse) were seen
# silent in the Claude Code VSCode-extension / Agent-SDK runtime: an unconditional fire-marker probe
# left no marker, and the corpus had frozen on 2026-06-16. That outage was the hook command form,
# fixed in 1.18.0, and the per-turn hooks fire today; this capture stays as the backstop.)
#
# PreCompact already runs the extractor mid-session (covers long sessions that compact). This hook
# covers session BOUNDARIES: at each new session start it runs the L1a extractor on the most-recently
# modified OTHER transcript (the session that just ended), so every session's durable facts + episode
# land in mem0 even when that session's Stop never ran. No scheduler, no per-turn dependency, no
# <24h timer.
#
# Fire-and-forget: spawns the worker DETACHED and exits 0 immediately so session start never blocks.
# A per-transcript watermark prevents re-capturing the same prior session on repeated starts; the
# extractor's own 10-min throttle + mem0 dedup bound cost further.
#
# Claude Code SessionStart payload (stdin JSON): { session_id, transcript_path, cwd, source,
# hook_event_name }; source in {startup, resume, clear, compact}.
# PS5.1-safe (no ?? / ternary / ?. ) - enforced by tests/PS51Compat.Tests.ps1.

$ErrorActionPreference = 'SilentlyContinue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# P1-6: pre-warm the memory authority's embedder. It unloads after 5 idle minutes and takes
# ~3.4 s to come back, so the first prompt's bundle used to pay the cold start (or get a
# cold-embedder 503). A hidden child calls GET /health/embedder?warm=rerank (one active embed, then a
# one-document rerank: the reranker unloads after 5 idle minutes too, and its cold load otherwise
# lands inside the first deliberate search) and exits. -TimeoutSec 45 = embed 10 s + rerank 20 s + margin.
# Fire-and-forget: nothing here blocks, and nothing is logged unless the spawn itself throws.
# This runs BEFORE the capture early-exits below on purpose: a repeated start on the same prior
# transcript (the watermark case) still needs a warm embedder.
# Authority precedence: ~\.mem0\authority-url (first non-empty line) > $env:MEM0_URL > the
# daemon's own default (loopback :18791 - on a replica that is a swallowed connection-refused).
# Host kind (P4-3): the same two lines the other spawner carries, for the same reason.
$IsUnixHost = ($PSVersionTable.Platform -eq 'Unix')
$HomeDirPath = if ($IsUnixHost) { $HOME } else { $env:USERPROFILE }

$prewarmUrl = ''
try {
    $authFile = Join-Path $HomeDirPath (Join-Path '.mem0' 'authority-url')
    if (Test-Path -LiteralPath $authFile) {
        foreach ($line in @(Get-Content -LiteralPath $authFile -ErrorAction SilentlyContinue)) {
            if ($line -and $line.Trim()) { $prewarmUrl = $line.Trim(); break }
        }
    }
} catch { $prewarmUrl = '' }
if (-not $prewarmUrl -and $env:MEM0_URL) { $prewarmUrl = $env:MEM0_URL.Trim() }
if (-not $prewarmUrl) { $prewarmUrl = 'http://127.0.0.1:18791' }
# Only a plain http(s) URL is ever interpolated into the child command line.
if ($prewarmUrl -notmatch '^https?://[A-Za-z0-9._:\[\]/-]+$') { $prewarmUrl = '' }
if ($prewarmUrl) {
    $prewarmCmd = "try { Invoke-RestMethod '" + $prewarmUrl.TrimEnd('/') + "/health/embedder?warm=rerank' -TimeoutSec 45 | Out-Null } catch {}"
    try {
        if ($IsUnixHost) {
            Start-Process -FilePath 'pwsh' `
                -ArgumentList '-NoProfile','-Command',$prewarmCmd `
                -ErrorAction Stop | Out-Null
        } else {
            Start-Process -FilePath 'pwsh.exe' `
                -ArgumentList '-NoProfile','-WindowStyle','Hidden','-Command',"`"$prewarmCmd`"" `
                -WindowStyle Hidden -ErrorAction Stop | Out-Null
        }
    } catch {
        try {
            Start-Process -FilePath 'powershell.exe' `
                -ArgumentList '-NoProfile','-WindowStyle','Hidden','-Command',"`"$prewarmCmd`"" `
                -WindowStyle Hidden -ErrorAction Stop | Out-Null
        } catch {
            try {
                $prewarmLogDir = Join-Path $HomeDirPath (Join-Path '.claude' 'logs')
                if (-not (Test-Path $prewarmLogDir)) { New-Item -ItemType Directory -Path $prewarmLogDir -Force | Out-Null }
                Add-Content -Path (Join-Path $prewarmLogDir 'sessionstart-capture.log') -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' embedder pre-warm spawn failed: ' + $_.Exception.Message)
            } catch {}
        }
    }
}

$Worker = Join-Path $ScriptDir 'l1a-extract.ps1'
if (-not (Test-Path $Worker)) { exit 0 }

# Current session - EXCLUDE it (at SessionStart its own transcript is new / near-empty).
$curTrans = $null
$curSid = $null
try {
    $raw = [Console]::In.ReadToEnd()
    if ($raw) {
        $p = $raw | ConvertFrom-Json -ErrorAction Stop
        $curTrans = [string]$p.transcript_path
        $curSid = [string]$p.session_id
    }
} catch {}

# Find the most-recently-modified transcript that is NOT the current session (2-level glob, no -Recurse).
$projects = Join-Path $HomeDirPath (Join-Path '.claude' 'projects')
if (-not (Test-Path $projects)) { exit 0 }
$prior = $null
try {
    $prior = Get-ChildItem -Path (Join-Path $projects (Join-Path '*' '*.jsonl')) -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne $curTrans -and $_.BaseName -ne $curSid -and $_.Length -gt 0 } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
} catch {}
if (-not $prior) { exit 0 }

$stateDir = Join-Path $HomeDirPath (Join-Path '.claude' 'state')

# Per-session same-second guard. 19% of SessionStart spawns were exact duplicates: two hooks fire
# for one session in the same second and both pass the watermark below (neither has written it
# yet), so both spawn an extractor and race for the codex lock. The first start creates a marker
# atomically (CreateNew); a start that finds a marker younger than $dupWindowSeconds is the
# duplicate and exits. An older marker is a genuine later start of the same session (a resume):
# it is refreshed and goes ahead. Fail-open: any error here just skips the guard.
$dupWindowSeconds = 3
$dupKey = $curSid
if (-not $dupKey) { $dupKey = $prior.BaseName }
$dupKey = ($dupKey -replace '[^A-Za-z0-9._-]', '_')
$spawnMarker = Join-Path $stateDir ('sessionstart-spawn-' + $dupKey)
try {
    if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
    $mfs = [System.IO.File]::Open($spawnMarker, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $mfs.Close()
} catch [System.IO.IOException] {
    $markerAge = $dupWindowSeconds + 1
    try { $markerAge = ((Get-Date) - (Get-Item -LiteralPath $spawnMarker).LastWriteTime).TotalSeconds } catch {}
    if ($markerAge -lt $dupWindowSeconds) { exit 0 }
    try { (Get-Item -LiteralPath $spawnMarker).LastWriteTime = Get-Date } catch {}
} catch {}
# housekeeping: markers are one tiny file per session start; drop the ones older than a day
try {
    $dayAgo = (Get-Date).AddDays(-1)
    Get-ChildItem -Path $stateDir -Filter 'sessionstart-spawn-*' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $dayAgo } | Remove-Item -Force -ErrorAction SilentlyContinue
} catch {}

# Watermark: skip if this exact transcript@mtime was already captured by a previous SessionStart.
$wm = Join-Path $stateDir 'last-sessionstart-capture'
$sig = $prior.FullName + '|' + $prior.LastWriteTimeUtc.Ticks
try {
    if (Test-Path $wm) {
        $prev = (Get-Content -Path $wm -Raw -ErrorAction SilentlyContinue)
        if ($prev) { $prev = $prev.Trim() }
        if ($prev -eq $sig) { exit 0 }
    }
} catch {}

# Spawn the extractor DETACHED (pwsh.exe first; powershell.exe 5.1 fallback) - never block startup.
$spawned = $false
try {
    if ($IsUnixHost) {
        Start-Process -FilePath 'pwsh' `
            -ArgumentList '-NoProfile','-File',$Worker,'-TranscriptPath',$prior.FullName,'-EventName','SessionStart' `
            -ErrorAction Stop | Out-Null
    } else {
        Start-Process -FilePath 'pwsh.exe' `
            -ArgumentList '-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',$Worker,'-TranscriptPath',"`"$($prior.FullName)`"",'-EventName','SessionStart' `
            -WindowStyle Hidden -ErrorAction Stop | Out-Null
    }
    $spawned = $true
} catch {
    try {
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList '-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',$Worker,'-TranscriptPath',"`"$($prior.FullName)`"",'-EventName','SessionStart' `
            -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
        $spawned = $true
    } catch {}
}

# Mark the watermark so the same prior transcript is not re-captured on the next start.
if ($spawned) {
    try {
        if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
        Set-Content -Path $wm -Value $sig -Encoding UTF8
    } catch {}
}
exit 0
