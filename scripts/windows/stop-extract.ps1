# Stop / PreCompact hook dispatcher - Windows-native PowerShell
# Replaces stop-extract.sh (which depended on `flock` not present in Git Bash).
# Fire-and-forget: spawns the actual L1a extractor in a detached hidden PS process and exits 0
# immediately so Claude Code's session-close path is never blocked.
#
# Claude Code hook contract (2024+): event data arrives via stdin as JSON with fields
# transcript_path and hook_event_name. Env vars CLAUDE_TRANSCRIPT_PATH / CLAUDE_HOOK_EVENT
# are NOT set by Claude Code - kept below as dead fallbacks only.

$ErrorActionPreference = 'SilentlyContinue'

# v0.19 L13: hook contract version — stamped into fixture FILENAMES (no payload
# mutation). '17.0' is intentional; bump only IF the stdin contract changes.
$HookContractVersion = '17.0'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Worker = Join-Path $ScriptDir 'l1a-extract.ps1'

if (-not (Test-Path $Worker)) { exit 0 }

# Primary: read hook event JSON from stdin (Claude Code's actual delivery mechanism)
$trans = $null
$evt = $null
try {
    $stdinRaw = [Console]::In.ReadToEnd()
    if ($stdinRaw) {
        $hookEvent = $stdinRaw | ConvertFrom-Json -ErrorAction Stop
        $trans = $hookEvent.transcript_path
        $evt   = $hookEvent.hook_event_name
        # v0.17 F.3.3: save payload fixture for hook contract regression corpus (v0.18 MED-14: 1-in-10 sampling)
        # v0.19 L13: write $stdinRaw bytes VERBATIM (no JSON round-trip that normalizes
        # key order/escapes and silently truncates at -Depth 16; no Out-File BOM) —
        # the corpus must be byte-faithful to detect wire-format drift. Contract
        # version lives in the FILENAME (...-contract<ver>.json), not the payload.
        try {
            $fixtureDir = Join-Path $env:USERPROFILE '.claude\state\hook-fixtures'
            if (-not (Test-Path $fixtureDir)) { New-Item -ItemType Directory -Path $fixtureDir -Force | Out-Null }
            if ((Get-Random -Minimum 0 -Maximum 10) -eq 0) {
                # v0.18 LOW-2: sub-second timestamp avoids 1-second filename collisions
                $ts = (Get-Date).ToString('yyyyMMdd-HHmmss-fff')
                [System.IO.File]::WriteAllText((Join-Path $fixtureDir "Stop-$ts-contract$HookContractVersion.json"), $stdinRaw)
                $existing = Get-ChildItem -Path $fixtureDir -Filter 'Stop-*.json' | Sort-Object LastWriteTime -Descending
                if ($existing.Count -gt 20) { $existing | Select-Object -Skip 20 | Remove-Item -Force -ErrorAction SilentlyContinue }
            }
        } catch {}
    }
} catch { }

# Fallback: env vars (never populated by Claude Code but kept for manual/test invocations)
if (-not $trans) { $trans = $env:CLAUDE_TRANSCRIPT_PATH }
if (-not $evt)   { $evt   = $env:CLAUDE_HOOK_EVENT }
if (-not $evt)   { $evt   = 'Stop' }

# For PreCompact, snapshot the transcript before it gets mutated by compaction
if ($evt -eq 'PreCompact' -and $trans -and (Test-Path $trans)) {
    $snap = Join-Path $env:TEMP "precompact-snap-$PID.jsonl"
    Copy-Item -Path $trans -Destination $snap -ErrorAction SilentlyContinue
    if (Test-Path $snap) { $trans = $snap }
}

# Spawn detached, hidden - claude.cmd auth works in this context (verified)
try {
    Start-Process -FilePath 'pwsh.exe' `
        -ArgumentList '-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',$Worker,'-TranscriptPath',"`"$trans`"",'-EventName',$evt `
        -WindowStyle Hidden `
        -ErrorAction SilentlyContinue | Out-Null
} catch {
    # If pwsh.exe missing, fall back to powershell.exe (Windows PS 5.1)
    try {
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList '-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',$Worker,'-TranscriptPath',"`"$trans`"",'-EventName',$evt `
            -WindowStyle Hidden `
            -ErrorAction SilentlyContinue | Out-Null
    } catch { }
}

exit 0
