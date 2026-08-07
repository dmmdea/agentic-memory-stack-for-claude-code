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

Describe 'Deploy parity covers the LIVE LAUNCH PATH (audit 2026-08-07: AMS-02/03/04)' {
    # The MCP registration launches the WINDOWS copy of the shim; deploy.sh only ever
    # writes the WSL copy. R9 printed "deployed scripts SHA256-match repo" for 12 days
    # while that shim was stale and two shipped fixes (503-queueing, deep memory_health)
    # were dead in production on BOTH boxes. These guards close the hole in the guard.
    BeforeAll {
        # tests -> windows -> scripts -> repo root (three levels; an off-by-one here makes
        # every assertion below fail on a missing file instead of on the thing under test)
        $script:winDir        = Split-Path -Parent $PSScriptRoot
        $script:repoRoot      = Split-Path -Parent (Split-Path -Parent $script:winDir)
        $script:tmsPath       = Join-Path $script:winDir 'Test-MemoryStack.ps1'
        $script:installerPath = Join-Path $script:repoRoot 'install\2-windows-config.ps1'
        $script:tmsText       = Get-Content $script:tmsPath -Raw
        $script:installerTxt  = Get-Content $script:installerPath -Raw
        $script:deployText    = Get-Content (Join-Path $script:repoRoot 'scripts\wsl\deploy.sh') -Raw

        # Parse a string-array literal out of a PowerShell file via the AST. Text-grepping
        # for a quoted name is not equivalent: the same literal can appear in a comment or
        # in a second array, which is precisely how the first version of the membership
        # guard below passed while the audited hole was reopened.
        function script:Get-PsArrayStrings {
            param([string]$Path, [string]$VarName)
            $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$null)
            $assign = $ast.FindAll({
                param($n)
                $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
                $n.Left.VariablePath.UserPath -eq $VarName
            }, $true) | Select-Object -First 1
            if (-not $assign) { return @() }
            return @($assign.Right.FindAll({
                param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst]
            }, $true) | ForEach-Object { $_.Value })
        }
    }

    It 'R9 hashes every file the installer deploys to the launch path' {
        # PARSE THE ARRAYS, do not grep the file. A whole-file substring check was
        # mutation-proven VACUOUS: the trio names also appear in Test-MemoryStack's
        # $wslSourced routing list, so deleting a name from $hookNames -- reopening the
        # exact audited hole -- left the whole suite green. Membership must be asserted
        # against the parsed $hookNames array, nothing else.
        $deployed = @(script:Get-PsArrayStrings -Path $script:installerPath -VarName 'wslScripts')
        $tracked  = @(script:Get-PsArrayStrings -Path $script:tmsPath       -VarName 'hookNames')
        $routed   = @(script:Get-PsArrayStrings -Path $script:tmsPath       -VarName 'wslSourced')

        $deployed.Count | Should -BeGreaterThan 0 -Because 'the installer must declare $wslScripts (an empty lookup would make every assertion below vacuous)'
        $tracked.Count  | Should -BeGreaterThan 0 -Because 'R9 must declare $hookNames'

        $missing = @($deployed | Where-Object { $tracked -notcontains $_ })
        $missing | Should -BeNullOrEmpty -Because (
            "R9 must hash every file the installer deploys to the launch path; missing: $($missing -join ', ')")

        # ...and each must be ROUTED to scripts\wsl for its repo source, or R9 compares it
        # against scripts\windows, reports 'no-repo-copy', and stays blind to real drift.
        $misrouted = @($deployed | Where-Object { $routed -notcontains $_ })
        $misrouted | Should -BeNullOrEmpty -Because (
            "every launch-path file must be routed to its scripts\wsl source; misrouted: $($misrouted -join ', ')")
    }

    It 'deploy.sh actually enforces launch-path parity (not just mentions it)' {
        # Prose is not a gate. Asserting on the raw text was mutation-proven vacuous:
        # replacing `exit 3` with `true` gutted enforcement entirely and the test stayed
        # green, because the words still appeared in comments and echo strings.
        $code = ($script:deployText -split "`r?`n" | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
        $code | Should -Match 'diff -q'   -Because 'it must actually compare the deployed copy against the repo source'
        $code | Should -Match 'launch_stale' -Because 'the comparison result must be collected'
        $code | Should -Match 'exit 3'    -Because 'a stale launch path must ABORT the deploy, not merely print'
        $code | Should -Match '2-windows-config\.ps1' -Because 'the failure must name the exact command that fixes it'
    }

    It 'the launch-path gate is PRE-FLIGHT, before anything is written' {
        # An earlier revision ran after the rsync steps, so a stale launch path aborted
        # mid-deploy: new modules on disk, import smoke + restart + health gate skipped,
        # and the next reboot activates ungated bytes.
        # Index into CODE, not prose: the file header describes the rsync steps in a
        # comment long before any command runs, so indexing the raw text compared the
        # gate against a comment and failed for the wrong reason.
        $code     = ($script:deployText -split "`r?`n" | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
        $gateIdx  = $code.IndexOf('LAUNCH_SCRIPTS=')
        $rsyncIdx = $code.IndexOf('rsync ')
        $gateIdx  | Should -BeGreaterThan 0 -Because 'the gate must exist in executable code'
        $rsyncIdx | Should -BeGreaterThan 0 -Because 'the rsync steps must exist in executable code'
        $gateIdx  | Should -BeLessThan $rsyncIdx -Because 'aborting after a partial write leaves the divergence this script exists to prevent'
    }

    It 'R9 names the baseline its parity verdict was computed against' {
        # A green "deployed matches repo" is meaningless if the repo side is a checkout
        # 8 PRs behind origin/main -- which is what the audit found.
        #
        # ASSERT ON CODE, NOT PROSE. The first version of this guard grepped the whole file
        # for 'origin/main' and 'BASELINE' and was VACUOUS: -Match is case-insensitive, so
        # the word "baseline" in this fix's own explanatory COMMENT satisfied it, and
        # 'origin/main' appears in that comment too. Mutating the real logic left it green.
        # Stripping comment lines first is what makes the assertion bite.
        $code = ($script:tmsText -split "`r?`n" | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"

        $code | Should -Match "rev-list\s+--count\s+'HEAD\.\.origin/main'" -Because 'the baseline must be measured against origin/main, not asserted in prose'
        $code | Should -Match 'BASELINE STALE' -Because 'a stale baseline must be named in the row an operator reads'
        # and the note must actually reach the verdict: computed-but-unused is the same defect
        $code | Should -Match '\$parityStatus\s*=.*\$baselineNote' -Because 'a stale baseline must downgrade the row, not just be computed'
        $code | Should -Match 'Add-Check .RECOVERY. .deployed hooks freshness. \$parityStatus \$parityMsg' -Because 'the OK branch must publish the computed status and message'
        # The note must LEAD the message: the summary table truncates long details with an
        # ellipsis, and the first live run proved a tail-appended warning is invisible.
        $code | Should -Match '\$parityMsg\s*=\s*if\s*\(\$baselineNote\)' -Because 'the message must branch on the baseline'
        $code | Should -Match '"\$baselineNote -- ' -Because 'the caveat must come first so truncation cannot eat it'
    }
}

Describe 'W2 stop-the-bleeding guards stay wired (audit 2026-08-07: AMS-01/09/10)' {
    # Each fix in this wave ships with its own liveness surface. These pins assert
    # the WIRING of those surfaces in code (comment-stripped), because every one of
    # them protects against a silent-removal failure mode the audit already found
    # once. Behavior is proven elsewhere (headless pytest, the live canaries, and
    # the mutation ledger for this wave); these guards make sure the wiring cannot
    # quietly disappear in a refactor.
    BeforeAll {
        $script:winDir   = Split-Path -Parent $PSScriptRoot
        $script:repoRoot = Split-Path -Parent (Split-Path -Parent $script:winDir)
        function script:Get-CodeLines {
            param([string]$Path)
            # strip comment lines (# leading) so prose can never satisfy a pin —
            # the W1 mutation ledger proved whole-file greps vacuous three times.
            ((Get-Content $Path -Raw) -split "`r?`n" |
                Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
        }
        $script:appCode  = script:Get-CodeLines (Join-Path $script:repoRoot 'mem0-server\app.py')
        $script:instCode = script:Get-CodeLines (Join-Path $script:repoRoot 'install\1-wsl-services.sh')
        $script:tmsCode  = script:Get-CodeLines (Join-Path $script:winDir 'Test-MemoryStack.ps1')
        $script:ciText   = Get-Content (Join-Path $script:repoRoot '.github\workflows\ci.yml') -Raw
    }

    It 'AMS-01: the PUT handler passes the carry-over INTO mem.update' {
        # The defect was precisely that this call site sent no metadata and only
        # tier was restored afterward. If the metadata= argument disappears, the
        # wipe is back with every test below it still green (the headless suite
        # pins the helper, not this call site — this pin owns the wiring).
        $script:appCode | Should -Match 'compute_carryover\(_pre_payload\)' -Because 'the pre-read payload must feed the carry-over'
        $script:appCode | Should -Match 'metadata=\(_carryover or None\)'   -Because 'preservation must be atomic in the upsert, not post-hoc'
        $script:appCode | Should -Match 'raise HTTPException\(\s*503'       -Because 'a failed pre-read must refuse the PUT (fail-closed) — proceeding blind IS the wipe'
    }

    It 'AMS-09: fastembed is installed by BOTH installer branches and proven loadable' {
        # The leg died because a venv rebuild took the refresh branch, which never
        # installed fastembed. Every pip line that installs the server deps must
        # carry it, and the post-condition must LOAD the encoder, not pip-list it.
        $pipLines = @($script:instCode -split "`n" | Where-Object { $_ -match 'pip.? install' -and ($_ -match 'mem0ai' -or $_ -match 'starlette') })
        $pipLines.Count | Should -BeGreaterOrEqual 2 -Because 'fresh and refresh pip lines must both exist (an empty lookup would make this pin vacuous)'
        $bare = @($pipLines | Where-Object { $_ -notmatch 'fastembed' })
        $bare | Should -BeNullOrEmpty -Because "every server pip line must install fastembed; missing from: $($bare -join ' || ')"
        $script:instCode | Should -Match 'SparseTextEmbedding\(model_name="Qdrant/bm25"\)' -Because 'the post-condition must instantiate the encoder (pip-listed but unloadable = the same dead leg)'
    }

    It 'AMS-09: /health/deep sparse_leg check exists and GATES ok' {
        $script:appCode | Should -Match 'out\["checks"\]\["sparse_leg"\]' -Because 'the leg must report on /health/deep'
        # the gating flip must be the sparse_leg result, adjacent to its assignment
        $script:appCode | Should -Match '(?s)out\["checks"\]\["sparse_leg"\]\s*=\s*_sl\s*.{0,120}if not _sl\.get\("ok"\).{0,40}out\["ok"\]\s*=\s*False' -Because 'a dead lexical leg must flip ok=false so the deploy gate blocks (informational-only was how it stayed dead 33 days)'
    }

    It 'W2 verifier rows exist for all three findings' {
        $script:tmsCode | Should -Match "Add-Check 'LIVENESS' 'bm25 sparse leg'"        -Because 'AMS-09 needs a human-visible row (L8)'
        $script:tmsCode | Should -Match "Add-Check 'INVARIANTS' 'PUT payload survival'" -Because 'AMS-01 needs the live PUT canary row (I13)'
        $script:tmsCode | Should -Match "Add-Check 'RECOVERY' 'cp437 mojibake'"         -Because 'AMS-10 needs the corpus tripwire row (R10)'
        # the PUT canary must clean up even when assertions fail — the pre-fix live
        # pytest suite leaked 2 damaged records into the production store.
        $script:tmsCode | Should -Match '(?s)finally\s*\{\s*if \(\$psMid\)' -Because 'the PUT-survival probe must delete its record on every path'
    }

    It 'the new headless suites actually gate CI (silent-not-gating is the W1 failure class)' {
        foreach ($t in 'test_payload_carryover.py', 'test_sparse_health.py', 'test_mojibake_check.py',
                       'test_capabilities.py', 'test_job_liveness.py') {
            $script:ciText | Should -Match ([regex]::Escape("mem0-server/tests/$t")) -Because "a headless test file absent from ci.yml's explicit list never runs — $t must gate"
            Test-Path (Join-Path $script:repoRoot "mem0-server\tests\$t") | Should -BeTrue -Because "$t must exist where ci.yml points"
        }
    }
}

Describe 'W3 alarm-mouths guards stay wired (audit 2026-08-07: AMS-05/06/08)' {
    BeforeAll {
        $script:winDir   = Split-Path -Parent $PSScriptRoot
        $script:repoRoot = Split-Path -Parent (Split-Path -Parent $script:winDir)
        function script:Get-CodeLines {
            param([string]$Path)
            ((Get-Content $Path -Raw) -split "`r?`n" |
                Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
        }
        $script:dreamCode = script:Get-CodeLines (Join-Path $script:winDir 'dream-consolidate.ps1')
        $script:tmsCode   = script:Get-CodeLines (Join-Path $script:winDir 'Test-MemoryStack.ps1')
        $script:bannerRaw = Get-Content (Join-Path $script:repoRoot 'claude-config\storage-cap-check.sh') -Raw
        $script:bannerCode = ($script:bannerRaw -split "`r?`n" |
            Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
    }

    It 'AMS-06: the dedup-mutex skip does NOT mark the throttle' {
        # The wake-collision burned TWO nights per occurrence because this branch
        # stamped last-dream on a skip; dream-catchup then read "fresh". Extract
        # the mutex block from executable code and assert the mark is gone.
        $m = [regex]::Match($script:dreamCode, '(?s)\$dedupLock\s*=.*?\$driftBefore\s*=')
        $m.Success | Should -BeTrue -Because 'the mutex block must exist between the dedup.lock check and the drift BEFORE snapshot'
        $m.Value | Should -Not -Match 'Mark-Throttle' -Because 'a skipped consolidation must leave the throttle open so the next 03:00 recovers same-day'
    }

    It 'AMS-06: a DryRun cannot stamp the live throttle or the consolidation receipt' {
        # The zero-signal branch marked the throttle unconditionally — a dry run
        # reaching it burned that night's real 03:00 (F4). And prune.json is the
        # consolidation-completed receipt: a dry-run write spoofs the probe (F5).
        $script:dreamCode | Should -Match 'if \(-not \$DryRun\) \{ Mark-Throttle -Name \$ThrottleName \}' -Because 'the zero-signal mark must be DryRun-guarded'
        $m = [regex]::Match($script:dreamCode, "(?s)if \(-not \`$DryRun\) \{\s*Save-PhaseState -Phase 'prune'")
        $m.Success | Should -BeTrue -Because 'the prune receipt must only be written by a REAL run'
    }

    It 'AMS-08: the guard death and drift alarms are typed records with a compat probe' {
        $script:dreamCode.Contains("Test-DriftStateSupport") | Should -BeTrue -Because 'the ps1 must probe the deployed python for --state support (either repo may deploy first)'
        $script:dreamCode.Contains("-match '--state'") | Should -BeTrue -Because 'the probe must grep the help text, not assume'
        $script:dreamCode.Contains("Add-DriftRecord -Kind 'guard-dead'") | Should -BeTrue -Because 'two consecutive snapshot failures must append a guard-dead record (the 07-21 fail-open signature)'
        $script:dreamCode.Contains("Add-DriftRecord -Kind 'drift'") | Should -BeTrue -Because 'the classic alarm must carry its kind so readers can filter'
        $script:dreamCode.Contains("Write-DriftHeartbeat -Event 'snapshot-failure'") | Should -BeTrue -Because 'a failed snapshot must beat the heartbeat, not skip silently'
    }

    It 'W3 verifier rows exist and the drift row filters kinds' {
        $script:tmsCode | Should -Match "Add-Check 'LIVENESS' 'dream cycle'" -Because 'AMS-06 needs the artifact-based cycle row (L9)'
        $script:tmsCode | Should -Match "Add-Check 'RECOVERY' 'drift guard liveness'" -Because 'AMS-08 needs the guard-liveness row (R11)'
        $script:tmsCode | Should -Match "Add-Check 'LIVENESS' 'capability manifest'" -Because 'the liveness contract needs its verdict row (L10)'
        $script:tmsCode.Contains("-not `$_.kind -or `$_.kind -eq 'drift'") | Should -BeTrue -Because 'legacy kind-absent records must read as drift (F14) and guard-dead must not be mislabeled a drift alarm'
        $script:tmsCode.Contains("`$TmsRole -eq 'replica'") | Should -BeTrue -Because 'brain-only rows must be role-gated (F3) or a replica FAILs forever'
    }

    It 'AMS-07: the Codex shim spawn is NOT gated on the flag nothing creates' {
        # The shim's spawn required ~/.claude/state/codex-shim.enabled — a flag
        # whose only documented creator was a manual "enable the NLI write-gate"
        # step never performed, and which nothing in either repo creates. That
        # one check kept a P1 judgment capability dead for its entire life.
        # (Found by the W4 mutation ledger: re-gating it turned NOTHING red.)
        $spawn = script:Get-CodeLines (Join-Path $script:winDir 'codex-shim-spawn.ps1')
        $spawn | Should -Not -Match 'codex-shim\.enabled' -Because 'an opt-IN flag nothing creates is why the judgment leg never once ran'
        $spawn.Contains('codex-shim.disabled') | Should -BeTrue -Because 'the gate must be opt-OUT: both consumers are always registered'
    }

    It 'AMS-07: the weekly sweep spawns the shim on demand' {
        # A session-spawned shim self-shuts after ~240 idle minutes, so the Sunday
        # 05:00 timer would still find it down and log its eighth consecutive
        # no-op. Without this ExecStartPre the R6c streak escalation below would
        # be a permanently-red guard (plan review F16).
        $unit = Get-Content (Join-Path $script:repoRoot 'systemd\contradiction-sweep.service') -Raw
        $unitCode = ($unit -split "`r?`n" | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
        # ABSOLUTE interpreter path: `env powershell.exe` exits 127 from a
        # systemd --user unit (no /mnt/c on its PATH) and the '-' prefix hides
        # it. The first version of this pin matched `ExecStartPre=-.*spawn.ps1`,
        # which the DEAD form satisfied — that is why mutation M6 passed against
        # a mechanism that could never run.
        $unitCode | Should -Match 'ExecStartPre=-/mnt/c/.*powershell\.exe.*codex-shim-spawn\.ps1' -Because 'the env form cannot resolve from a systemd user unit; only an absolute interpreter path runs'
        $unitCode | Should -Match 'ExecStartPre=-/bin/sh -c .*18792/health' -Because 'the preflight is a single 3s attempt with no retry, so the spawn needs a bounded readiness wait or ExecStart loses the race'
        $unitCode | Should -Match 'judge codex' -Because 'the sweep must never fall back to a local judge (78% false positives measured)'
    }

    It 'AMS-07: a streak of no-op sweeps FAILs, a single skipped week WARNs' {
        # 7 consecutive no-ops went unnoticed for a month because the row only
        # ever read the LAST run. A skipped week is the documented accepted
        # trade; a streak means the leg is dead.
        $script:tmsCode | Should -Match "Add-Check 'RECOVERY' 'contradiction sweep' 'FAIL'" -Because 'a dead judgment leg must FAIL, not WARN forever'
        $script:tmsCode | Should -Match '\$streak\s*-ge\s*3' -Because 'the escalation must key on a consecutive-run streak'
    }

    It 'AMS-11: the one-brain guard keys on local receipts, never a hostname parameter' {
        # The guard compared $env:COMPUTERNAME against -AuthorityHost, whose
        # default was the literal 'your-machine' — so it never fired anywhere.
        # Deriving that host from the authority URL is EQUALLY vacuous on the
        # brain (loopback authority -> host 127.0.0.1 != COMPUTERNAME).
        $trav = script:Get-CodeLines (Join-Path $script:repoRoot 'scripts\travel\travel-mode.ps1')
        $trav | Should -Not -Match '\$AuthorityHost' -Because 'a hostname parameter default is what made this guard vacuous for its whole life'
        $trav.Contains("-eq 'brain'") | Should -BeTrue -Because 'the role receipt is authoritative on the box being protected'
        $trav.Contains('Test-IsLocalAuthorityUrl') | Should -BeTrue -Because 'a box whose authority is itself IS the authority'
        $trav | Should -Match 'Assert-NotTheAuthority ''on''' -Because "the 'on' verb must invoke the guard"
        $trav | Should -Match 'Assert-NotTheAuthority ''off''' -Because "the 'off' verb stops the live services and must invoke it too"
    }

    It 'the SessionStart banner emits the heartbeat digest from files, never /health/deep' {
        $script:bannerCode.Contains('[heartbeat]') | Should -BeTrue -Because 'the digest line is the load-bearing mouth (morning-summary has no other reader)'
        $script:bannerCode.Contains('retrieval-drift-state.json') | Should -BeTrue -Because 'drift alarm/guard-dead must reach the banner'
        $script:bannerCode | Should -Not -Match 'health/deep' -Because 'the banner must never call the expensive endpoint inline (the 1s cold-morning guard exists for a reason)'
    }
}
