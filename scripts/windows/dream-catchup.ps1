# dream-catchup.ps1 — debt-based catch-up trigger for the nightly dream consolidator.
#
# Problem: the dream fires nightly 3am via Task Scheduler. If the PC is off/asleep/
# travelling at 3am, that night's consolidation is simply MISSED — the 24h throttle
# and the scheduler don't reschedule it, so the store silently falls behind.
#
# This script is spawned DETACHED from a SessionStart hook (like mem0-hook-daemon-spawn.ps1).
# It's already off the prompt hot path, so it does the (cheap) debt checks in-process and
# only when there is real work invokes dream-consolidate.ps1 to catch up. Fail-open
# everywhere: a failure here must NEVER block a session. The dream's OWN 24h throttle +
# Codex lock prevent a double-run, so we pass nothing special — we only DECIDE whether to
# nudge it. Its own 6h throttle stops multiple session-starts in a morning from hammering.
#
# Debt = anything the dream would consolidate that has piled up:
#   - >=1 pending learn-rule (~/.mem0/learn-rules.jsonl), OR
#   - >=1 queued promotion (~/.mem0/promote-queue.jsonl), OR
#   - last dream run >48h ago (long gap: catch up regardless of visible debt).
# If the dream ran <30h ago it's fresh — no catch-up. Stale (30-48h) but no debt -> skip.

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Dot-source the shared helpers (Test-Throttle / Mark-Throttle / Write-MemoryLog /
# Initialize-MemoryEnv). A broken lib deploy must not break SessionStart, so guard it —
# if the lib is unreadable we simply exit 0 (fail-open, no catch-up this session).
try {
    . (Join-Path $ScriptDir 'memory-common.ps1')
    Initialize-MemoryEnv
} catch { exit 0 }

# Own 6h throttle so a burst of session starts (a busy morning) fires at most one catch-up
# check. Test-Throttle is a pure read; Mark-Throttle is only stamped once we actually run a
# check-to-completion below (so a crash before the decision doesn't burn the window).
if (-not (Test-Throttle -Name 'dream-catchup' -MinIntervalSeconds 21600)) {
    exit 0
}

try {
    # (a) Read the dream throttle's last-run marker DIRECTLY — same file Test-Throttle 'dream'
    # uses (~/.claude/state/last-dream). We only READ it here; we never consume/update it, so
    # the dream's own throttle accounting stays intact.
    $dreamMarker = Join-Path $env:USERPROFILE '.claude\state\last-dream'
    $lastDream = $null
    if (Test-Path $dreamMarker) {
        try { $lastDream = [int](Get-Content $dreamMarker -Raw).Trim() } catch { $lastDream = $null }
    }
    $now = [int][double]::Parse((Get-Date -UFormat %s))
    # No marker at all -> the dream has never recorded a run: treat as maximally stale (force).
    $ageHours = if ($null -ne $lastDream) { ($now - $lastDream) / 3600.0 } else { [double]::PositiveInfinity }

    Mark-Throttle -Name 'dream-catchup'   # 6h window opens now — the check ran to a decision

    # (b) Fresh: a dream ran within the last 30h — nothing to catch up.
    if ($ageHours -lt 30) {
        Write-MemoryLog -Component 'dream-catchup' -Message ("fresh, no catch-up (last dream {0:N1}h ago)" -f $ageHours)
        exit 0
    }

    # (c) Debt check: pending learn-rules OR queued promotions OR a long (>48h) gap.
    function Test-JsonlHasLine {
        param([string]$Path)
        if (-not (Test-Path -LiteralPath $Path)) { return $false }
        try {
            foreach ($ln in [System.IO.File]::ReadLines($Path)) {
                if (-not [string]::IsNullOrWhiteSpace($ln)) { return $true }
            }
        } catch { return $false }
        return $false
    }
    $learnPath   = Join-Path $env:USERPROFILE '.mem0\learn-rules.jsonl'
    $promotePath = Join-Path $env:USERPROFILE '.mem0\promote-queue.jsonl'
    $hasLearn   = Test-JsonlHasLine -Path $learnPath
    $hasPromote = Test-JsonlHasLine -Path $promotePath
    $longGap    = ($ageHours -gt 48)

    if (-not ($hasLearn -or $hasPromote -or $longGap)) {
        Write-MemoryLog -Component 'dream-catchup' -Message ("stale but no debt (last dream {0:N1}h ago); skipping catch-up" -f $ageHours)
        exit 0
    }

    Write-MemoryLog -Component 'dream-catchup' -Message ("debt detected (last dream {0:N1}h ago, learn={1} promote={2} longGap={3}); invoking dream-consolidate.ps1" -f $ageHours, $hasLearn, $hasPromote, $longGap)

    # Invoke the real consolidator. Its OWN 24h throttle + Codex lock already prevent a
    # double-run against the nightly scheduled task or a concurrent session, so we pass
    # nothing special. Run in-process (we're already detached) and just log the outcome.
    $dreamScript = Join-Path $ScriptDir 'dream-consolidate.ps1'
    if (Test-Path $dreamScript) {
        try {
            & $dreamScript
            Write-MemoryLog -Component 'dream-catchup' -Message "dream-consolidate.ps1 returned (its own throttle/lock govern whether it actually ran)"
        } catch {
            Write-MemoryLog -Component 'dream-catchup' -Message "dream-consolidate.ps1 threw (non-fatal): $_"
        }
    } else {
        Write-MemoryLog -Component 'dream-catchup' -Message "dream-consolidate.ps1 not found beside this script; nothing to invoke"
    }
} catch {
    # Fail-open: never let a catch-up failure surface to the session.
    try { Write-MemoryLog -Component 'dream-catchup' -Message "catch-up aborted (non-fatal): $_" } catch {}
}
exit 0
