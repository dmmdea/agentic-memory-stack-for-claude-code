# RegressionGuards.Tests.ps1 — pins the fixes from the 2026-07 outage week.
#
# Every guard here exists because a real defect shipped, ran silently for days, and left NO
# signal in any log it owned. Each was found by hand; none had a test. A silent revert of any
# one of them would be equally invisible, which is the whole reason this file exists.
#
# WRITING GUARDS THAT CAN ACTUALLY FAIL. The first draft of this file had four vacuous
# assertions — an adversarial review reverted each fix and watched the guard stay GREEN:
#   * a regex that matched the explanatory COMMENT, which survives a revert of the code;
#   * a pattern requiring `$x = ...` that missed the `return ...` form;
#   * an unescaped `$` (a regex end-anchor) that could never match anything;
#   * a check for a helper's EXISTENCE rather than the string SHAPE it emits.
# So: assert executable code, not comments; assert emitted shape, not presence; and prefer a
# literal .Contains() over a regex whenever the needle contains regex metacharacters.

BeforeAll {
    $script:winDir = Split-Path -Parent $PSScriptRoot
    # winDir = <repo>/scripts/windows, so the repo root is TWO levels up, not one.
    $script:repoRoot  = Split-Path -Parent (Split-Path -Parent $script:winDir)
    $script:dream     = Join-Path $script:winDir 'dream-consolidate.ps1'
    $script:common    = Join-Path $script:winDir 'memory-common.ps1'
    $script:installer = Join-Path $script:repoRoot 'install\2-windows-config.ps1'

    # Executable lines only — comments explain the bug and survive a revert of the fix, so
    # matching them proves nothing.
    function script:CodeOf([string]$Path) {
        (Get-Content $Path | Where-Object { $_ -notmatch '^\s*#' }) -join "`n"
    }
}

Describe 'Dream nightly throttle (2026-07-24)' {
    # A strict 86400s window against a fixed 03:00 trigger made the next night's run ineligible
    # by ~43 seconds, because the stamp is written at cycle COMPLETION. The dream silently ran
    # every OTHER night. The pre-existing throttle suite passed without reading the constant.
    It 'uses 82800s (23h), not 86400s' {
        $code = script:CodeOf $script:dream
        $code | Should -Match 'MinIntervalSeconds\s+82800'
        $code | Should -Not -Match 'MinIntervalSeconds\s+86400'
    }

    It 'opens after 23h+ and stays closed before it' {
        . $script:common
        $sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("throttle-" + [guid]::NewGuid())
        New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
        try {
            $script:StateDir = $sandbox
            $now = Get-UnixEpoch
            $stamp = Join-Path $sandbox 'last-dreamguard'

            Set-Content -Path $stamp -Value ($now - 22 * 3600) -Encoding UTF8 -NoNewline
            Test-Throttle -Name 'dreamguard' -MinIntervalSeconds 82800 | Should -BeFalse -Because '22h < 23h must stay throttled'

            Set-Content -Path $stamp -Value ($now - 24 * 3600) -Encoding UTF8 -NoNewline
            Test-Throttle -Name 'dreamguard' -MinIntervalSeconds 82800 | Should -BeTrue -Because '24h > 23h must open'

            # The exact regression: a nightly trigger arriving a minute shy of a full 24h. Under
            # the old 86400s window this returned False and skipped the night.
            Set-Content -Path $stamp -Value ($now - (86400 - 60)) -Encoding UTF8 -NoNewline
            Test-Throttle -Name 'dreamguard' -MinIntervalSeconds 82800 | Should -BeTrue -Because 'the every-other-night skip must not come back'
            Test-Throttle -Name 'dreamguard' -MinIntervalSeconds 86400 | Should -BeFalse -Because 'and 86400 is exactly what used to break it'
        } finally {
            Remove-Item $sandbox -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Shell-independent epoch (2026-07-24)' {
    # PowerShell 5.1's `Get-Date -UFormat %s` is offset by the machine's UTC offset; pwsh 7 is
    # correct. Scheduled tasks run under pwsh 7 and hooks under 5.1, and both write the SAME
    # ~/.claude/state/last-* stamps — so elapsed time came out wrong by the UTC offset in
    # whichever direction. A 41.4h gap was reported as 36.4h.
    It 'the Get-UnixEpoch BODY uses UtcNow and not -UFormat' {
        . $script:common
        $body = (Get-Command Get-UnixEpoch).ScriptBlock.ToString()
        $code = ($body -split "`n" | Where-Object { $_ -notmatch '^\s*#' }) -join "`n"
        $code | Should -Match 'DateTimeOffset\]::UtcNow'
        $code | Should -Not -Match 'UFormat'
    }

    It 'no production script computes epoch via -UFormat %s' {
        # Any executable use, assignment OR return.
        $offenders = Get-ChildItem (Join-Path $script:winDir '*.ps1') -File | Where-Object {
            (script:CodeOf $_.FullName) -match 'Get-Date\s+-UFormat\s+%s'
        } | ForEach-Object { $_.Name }
        $offenders | Should -BeNullOrEmpty -Because 'that expression is 5.1/7-dependent; use Get-UnixEpoch'
    }

    It 'returns the true epoch under the CURRENT shell edition' {
        . $script:common
        $truth = [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        [Math]::Abs($truth - (Get-UnixEpoch)) | Should -BeLessThan 5
    }
}

Describe 'Installer receipt: EvalRootWsl inherits (2026-07-24)' {
    # A plain re-run reset EvalRootWsl to '', silently reverting the dream's retrieval-drift
    # canary to its graceful skip — a degrade-not-alarm path, so nothing reported the loss.
    It 'inherits the prior receipt value when the flag is omitted' {
        $code = script:CodeOf $script:installer
        $code | Should -Match '\$EvalRootWsl\s*=\s*\$prevEval'
        $code | Should -Match 'Import-PowerShellDataFile \$receiptPath'
    }

    It 'writes EvalRootWsl into the receipt' {
        (script:CodeOf $script:installer) | Should -Match 'EvalRootWsl\s*=\s*'
    }
}

Describe 'Hook commands are bash-safe (2026-07-22 / 2026-07-24)' {
    # Claude Code passes a hook command with no `args` array to Git Bash, where an unquoted
    # backslash is an escape character. That silently shredded the command and killed the hook
    # exit 127 — twice: the .ps1 hooks (9-day episodic-capture outage) and then the bare-exe
    # hook (~1000 failed prompts, memory injection reaching nothing).
    It 'emits .ps1 hook commands with a DOUBLE-QUOTED forward-slash path' {
        $code = script:CodeOf $script:installer
        $code | Should -Match 'function New-HookCommand'
        # the emitted shape must open a double quote before the path, not just reference the var
        $code | Should -Match '-File \"'
        $code | Should -Match "hookScriptsFwd = 'C:/Users/"
    }

    It 'emits bare-exe hook commands through New-HookExeCommand, quoted' {
        $code = script:CodeOf $script:installer
        $code | Should -Match 'function New-HookExeCommand'
        $code | Should -Match "New-HookExeCommand 'mem0-hook-client.exe'"
    }

    It 'no hook command is assembled from a raw backslash user path' {
        # Literal substring, NOT a regex: an earlier version left `$env` unescaped, where `$` is
        # a regex end-anchor, so the pattern could never match and the guard was vacuous.
        $src = Get-Content $script:installer -Raw
        $badShape = "'C:\Users\' + " + '$env:USERNAME' + " + '\.claude\scripts"
        $src.Contains($badShape) | Should -BeFalse -Because 'that concatenation is the shape Git Bash shreds'
    }
}
