# PS51Compat.Tests.ps1 — every production script here is launched by the Claude Code hooks
# (Stop/PreCompact/UserPromptSubmit/PreToolUse) and by Windows Task Scheduler via
# `powershell.exe` = Windows PowerShell 5.1. PS7-only syntax (the null-coalescing `??`/`??=`,
# the null-conditional `?.`/`?[`, and the ternary `a ? b : c`) is a PARSE ERROR under 5.1 that
# silently kills the ENTIRE script before line 1 executes — no log, no error, just nothing.
#
# That is exactly what froze L1a capture on 2026-06-16: `Get-BrandFromTranscriptPath -Path
# ($TranscriptPath ?? '')` made l1a-extract.ps1 un-parseable under 5.1, so the Stop hook spawned
# a worker that never ran, and the system stored zero new memories for 8 days while every "audit"
# checked wiring instead of output.
#
# This test parses each *.ps1 under the REAL 5.1 parser (shelling to powershell.exe) and fails on
# any parse error, so that class of regression cannot silently ship again.

# Scripts that DECLARE `#Requires -Version 7` are pwsh-only by design (dev/test tools run manually,
# never by a hook or scheduled task). The #Requires gives them a clean error under 5.1; this guard
# skips them and checks every OTHER script — i.e. everything the hooks / Task Scheduler run under 5.1.
$ps1Files = Get-ChildItem -Path (Split-Path -Parent $PSScriptRoot) -Filter '*.ps1' -File |
    Where-Object { -not ((Get-Content $_.FullName -TotalCount 5 -ErrorAction SilentlyContinue) -match '#Requires\s+-Version\s+[7-9]') } |
    ForEach-Object { @{ Name = $_.Name; Path = $_.FullName } }

Describe 'Windows PowerShell 5.1 parse-compat (scripts run via powershell.exe)' {
    It '<Name> parses clean under Windows PowerShell 5.1' -ForEach $ps1Files {
        $cmd = "`$e=`$null; [void][System.Management.Automation.Language.Parser]::ParseFile('" + $Path + "',[ref]`$null,[ref]`$e); if(`$e){`$e|%{'L'+`$_.Extent.StartLineNumber+': '+`$_.Message}}"
        $errOut = (& powershell.exe -NoProfile -NonInteractive -Command $cmd 2>&1 | Out-String).Trim()
        $errOut | Should -BeNullOrEmpty -Because "PS7-only syntax parse-errors under 5.1 and silently kills the whole script (the 2026-06-16 L1a outage). Parse errors: $errOut"
    }
}
