# sessionstart-capture.ps1 - SessionStart hook: capture the most-recent PRIOR session's transcript.
#
# WHY THIS EXISTS: in the Claude Code VSCode-extension / Agent-SDK runtime, the PER-TURN hooks
# (Stop / UserPromptSubmit / PreToolUse) do NOT fire (verified 2026-06-24 with an unconditional
# fire-marker probe: a tool call produced no marker; the session id never appears in any per-turn
# hook log). So Stop-driven capture is dead in that runtime, which is why the corpus froze on
# 2026-06-16 when the operator switched runtimes. The LIFECYCLE hooks (SessionStart, PreCompact)
# DO fire (the SessionStart resume banner appears every session).
#
# PreCompact already runs the extractor mid-session (covers long sessions that compact). This hook
# covers session BOUNDARIES: at each new session start it runs the L1a extractor on the most-recently
# modified OTHER transcript (the session that just ended), so every session's durable facts + episode
# land in mem0 even with the per-turn hooks dead. No scheduler, no per-turn dependency, no <24h timer.
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
$projects = Join-Path $env:USERPROFILE '.claude\projects'
if (-not (Test-Path $projects)) { exit 0 }
$prior = $null
try {
    $prior = Get-ChildItem -Path (Join-Path $projects '*\*.jsonl') -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne $curTrans -and $_.BaseName -ne $curSid -and $_.Length -gt 0 } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
} catch {}
if (-not $prior) { exit 0 }

# Watermark: skip if this exact transcript@mtime was already captured by a previous SessionStart.
$stateDir = Join-Path $env:USERPROFILE '.claude\state'
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
    Start-Process -FilePath 'pwsh.exe' `
        -ArgumentList '-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',$Worker,'-TranscriptPath',"`"$($prior.FullName)`"",'-EventName','SessionStart' `
        -WindowStyle Hidden -ErrorAction Stop | Out-Null
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
