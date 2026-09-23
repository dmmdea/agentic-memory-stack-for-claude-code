#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# InstallerParity.Tests.ps1 — v0.20 Final (adversarial-review HIGH x2):
#
# 1. Installer/verifier A.5/A.6 parity: install/2-windows-config.ps1 must
#    deploy every file Test-MemoryStack R9 hash-tracks (a clean install must
#    not self-report DEGRADED), build + register the compiled UserPromptSubmit
#    client with BOTH-shape dedupe markers (legacy wrapper + exe — a re-run
#    over a live exe-registered box must replace, never append), and register
#    the SessionStart daemon-spawn launcher; install/3-verify.ps1 must expect
#    that accelerated chain.
#
# 2. SessionStart exe self-heal: mem0-hook-daemon-spawn.ps1 must rebuild a
#    missing mem0-hook-client.exe from the deployed .cs via the smoke-gated
#    builder (DR restore / cross-PC sync of settings.json otherwise silently
#    kills the prompt pipeline). Functional test runs the REAL spawn script in
#    a sandboxed USERPROFILE so the real deployment is never touched.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester D:\repos\agentic-memory-stack\scripts\windows\tests\ -Output Detailed"

BeforeAll {
    $script:winDir        = Split-Path -Parent $PSScriptRoot
    $script:repoRoot      = Split-Path -Parent (Split-Path -Parent $script:winDir)
    $script:installerPath = Join-Path $script:repoRoot 'install\2-windows-config.ps1'
    $script:verifierPath  = Join-Path $script:repoRoot 'install\3-verify.ps1'
    $script:wslInstaller  = Join-Path $script:repoRoot 'install\1-wsl-services.sh'
    $script:goalsSweepSvc = Join-Path $script:repoRoot 'systemd\goals-stale-sweep.service'
    $script:tmsPath       = Join-Path $script:winDir 'Test-MemoryStack.ps1'
    $script:spawnPath     = Join-Path $script:winDir 'mem0-hook-daemon-spawn.ps1'
    $script:capCheckSrc   = Join-Path $script:repoRoot 'claude-config\storage-cap-check.sh'

    function Get-ParsedAst {
        param([string]$Path)
        $tokens = $null; $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
        if ($errors -and $errors.Count -gt 0) {
            throw "parse errors in ${Path}: $($errors[0].Message)"
        }
        $ast
    }

    function Get-AstArrayStrings {
        # All string constants on the right-hand side of `$<VarName> = @(...)`.
        param([string]$Path, [string]$VarName)
        $ast = Get-ParsedAst -Path $Path
        $assign = $ast.FindAll({
            param($a)
            $a -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $a.Left.Extent.Text -eq ('$' + $VarName)
        }, $true) | Select-Object -First 1
        if (-not $assign) { throw "no assignment to `$$VarName found in $Path" }
        @($assign.Right.FindAll({
            param($a) $a -is [System.Management.Automation.Language.StringConstantExpressionAst]
        }, $true) | ForEach-Object { $_.Value })
    }
}

Describe 'installer/verifier ship the A.5/A.6 chain (v0.20 Final)' {

    It 'installer and verifier parse cleanly' {
        { Get-ParsedAst -Path $installerPath } | Should -Not -Throw
        { Get-ParsedAst -Path $verifierPath }  | Should -Not -Throw
    }

    It 'the installer deploys every file Test-MemoryStack R9 hash-tracks' {
        # The invariant is "R9-tracked => deployed by the installer", NOT "=> listed in
        # $winScripts". The installer has TWO deploy lists into the same scripts dir:
        # $winScripts (sources from scripts\windows\) and $wslScripts (sources from
        # scripts\wsl\ -- the mem0 MCP shim and its siblings, which the MCP actually
        # launches). Audit 2026-08-07 (AMS-03) added the $wslScripts trio to R9 because
        # its absence let R9 report "all deployed scripts match" while the live shim was
        # 12 days stale; checking only $winScripts here would have forced that fix back
        # out and re-opened the hole. Union both lists.
        $win = Get-AstArrayStrings -Path $installerPath -VarName 'winScripts'
        $wsl = Get-AstArrayStrings -Path $installerPath -VarName 'wslScripts'
        $deployed = @($win) + @($wsl)
        $r9  = Get-AstArrayStrings -Path $tmsPath       -VarName 'hookNames'
        $missing = @($r9 | Where-Object { $deployed -notcontains $_ })
        $missing | Should -BeNullOrEmpty -Because "every R9-tracked deployed file the installer skips makes a clean install self-report DEGRADED: $($missing -join ', ')"
    }

    It 'both installer deploy lists are actually discoverable (the union above is not vacuous)' {
        # If a rename made either lookup return empty, the union test above would pass
        # trivially for the wrong reason. Pin that both lists exist and are non-empty.
        @(Get-AstArrayStrings -Path $installerPath -VarName 'winScripts') | Should -Not -BeNullOrEmpty
        @(Get-AstArrayStrings -Path $installerPath -VarName 'wslScripts') | Should -Not -BeNullOrEmpty
    }

    It 'installer deploys the daemon, spawn launcher, client source, and smoke-gated builder' {
        $win = Get-AstArrayStrings -Path $installerPath -VarName 'winScripts'
        foreach ($f in @('mem0-hook-daemon.ps1', 'mem0-hook-daemon-spawn.ps1', 'mem0-hook-client.cs', 'build-hook-client.ps1')) {
            $win | Should -Contain $f
        }
    }

    It 'installer runs the smoke-gated build and aborts BEFORE hook registration on failure' {
        $src = Get-Content $installerPath -Raw
        $buildIdx = $src.IndexOf("scripts\windows\build-hook-client.ps1")
        $regIdx   = $src.IndexOf('Registering hooks in settings.json')
        $buildIdx | Should -BeGreaterThan 0
        $regIdx   | Should -BeGreaterThan $buildIdx -Because 'a failed exe build must abort before settings.json ever points at a missing exe'
        # the abort gate sits between the build call and hook registration
        $between = $src.Substring($buildIdx, $regIdx - $buildIdx)
        $between | Should -Match '\$LASTEXITCODE\s+-ne\s+0'
        $between | Should -Match 'exit\s+1'
    }

    It 'installer registers UserPromptSubmit at the compiled exe' {
        # 2026-07-24: assert the BUILDER call, not a backslash path spelling. Hook command
        # strings run through Git Bash, which eats unquoted backslashes — the old literal form
        # this used to assert is precisely the shape that died exit 127 on every prompt.
        $src = Get-Content $installerPath -Raw
        $src | Should -Match ([regex]::Escape("New-HookExeCommand 'mem0-hook-client.exe'"))
        # the legacy wrapper must no longer be the registered command shape
        $src | Should -Not -Match ([regex]::Escape('-File C:\Users\') + ".*user-prompt-extract\.ps1")
    }

    It 'UserPromptSubmit dedupe markers match BOTH the legacy wrapper and the exe shape' {
        # Re-running the installer over a live exe-registered box must replace
        # the entry, not append a duplicate inline hook beside it.
        $ast = Get-ParsedAst -Path $installerPath
        $assign = $ast.FindAll({
            param($a)
            $a -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $a.Left.Extent.Text -eq '$hookEntries'
        }, $true) | Select-Object -First 1
        $assign | Should -Not -BeNullOrEmpty
        $upsLine = ($assign.Right.Extent.Text -split "`n" | Where-Object { $_ -match "'UserPromptSubmit'" }) -join "`n"
        $upsLine | Should -Match "user-prompt-extract\.ps1"
        $upsLine | Should -Match "mem0-hook-client"
    }

    It 'installer registers the SessionStart daemon-spawn launcher' {
        $ast = Get-ParsedAst -Path $installerPath
        $assign = $ast.FindAll({
            param($a)
            $a -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $a.Left.Extent.Text -eq '$hookEntries'
        }, $true) | Select-Object -First 1
        $sessionBlock = $assign.Right.Extent.Text
        $sessionBlock | Should -Match "mem0-hook-daemon-spawn\.ps1"
        # 2026-07-22: hook commands are built by New-HookCommand with FORWARD-slash paths —
        # hook command strings execute through Git Bash, where an unquoted backslash is an
        # escape character and silently destroys the command (root cause of the second
        # episodic-capture outage). Assert the builder call, not a path spelling.
        $src = Get-Content $installerPath -Raw
        $src | Should -Match ([regex]::Escape("New-HookCommand 'mem0-hook-daemon-spawn.ps1'"))
    }

    It 'installer registers the SessionStart codex-shim-spawn launcher (v0.27.1 R5)' {
        $ast = Get-ParsedAst -Path $installerPath
        $assign = $ast.FindAll({
            param($a)
            $a -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $a.Left.Extent.Text -eq '$hookEntries'
        }, $true) | Select-Object -First 1
        $sessionBlock = $assign.Right.Extent.Text
        $sessionBlock | Should -Match "codex-shim-spawn\.ps1" -Because 'if the launcher registration is dropped, deploy + R9 still pass but the shim never warms — silently disabling the Codex write-gate path'
        # Same forward-slash rationale as the daemon-spawn assertion above.
        $src = Get-Content $installerPath -Raw
        $src | Should -Match ([regex]::Escape("New-HookCommand 'codex-shim-spawn.ps1'"))
    }

    It 'shim scripts carry NO dmmdea token (R9 byte-identical deploy)' {
        foreach ($f in @('codex-shim.ps1', 'codex-shim-spawn.ps1')) {
            (Get-Content (Join-Path $winDir $f) -Raw) | Should -Not -Match 'dmmdea' -Because "$f is R9 SHA256-tracked; a 'dmmdea' token would make the installer substitute it and break the deployed-vs-repo hash match"
        }
    }

    It 'verifier expects the accelerated chain (exe present, exe registered exactly once, daemon-spawn registered)' {
        $src = Get-Content $verifierPath -Raw
        $src | Should -Match ([regex]::Escape('\.claude\scripts\mem0-hook-client.exe'))
        $src | Should -Match "mem0-hook-client\.exe"
        $src | Should -Match "mem0-hook-daemon-spawn\.ps1"
        # the exactly-one assertion (duplicate-hook regression guard)
        $src | Should -Match '\.Count\s+-eq\s+1'
    }
}

Describe 'installer deploys + registers the SessionStart storage-cap-check (v0.21 Phase C)' {

    It 'canonical source claude-config/storage-cap-check.sh exists' {
        Test-Path -LiteralPath $capCheckSrc | Should -BeTrue -Because 'the dedicated installer block copies the SessionStart cap-check from claude-config (NOT the retired scripts/wsl copy)'
    }

    It 'the retired scripts/wsl/storage-cap-check.sh copy is gone (no divergent source)' {
        Test-Path -LiteralPath (Join-Path $repoRoot 'scripts\wsl\storage-cap-check.sh') | Should -BeFalse -Because 'v0.14 retired it; two sources would let the SessionStart hook silently bind the wrong (dead v0.12) script'
    }

    It 'installer deploys storage-cap-check.sh from claude-config into ScriptsDir' {
        $src = Get-Content $installerPath -Raw
        # the dedicated copy block: source = claude-config\storage-cap-check.sh, dest = $ScriptsDir
        $src | Should -Match ([regex]::Escape('claude-config\storage-cap-check.sh')) -Because 'dropping this block makes the SessionStart marker resolve to a missing deployed file (the 2026-06-08 silent-fail bug)'
        $src | Should -Match "Join-Path\s+\`$ScriptsDir\s+'storage-cap-check\.sh'"
    }

    It 'SessionStart registers a storage-cap-check.sh entry whose command points at the deployed scripts dir' {
        $ast = Get-ParsedAst -Path $installerPath
        $assign = $ast.FindAll({
            param($a)
            $a -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $a.Left.Extent.Text -eq '$hookEntries'
        }, $true) | Select-Object -First 1
        $assign | Should -Not -BeNullOrEmpty
        $sessionBlock = $assign.Right.Extent.Text
        $sessionBlock | Should -Match 'storage-cap-check\.sh' -Because 'the SessionStart marker entry must exist'
        # the registered command ($bashCapCheck) must target the deployed filename
        $src = Get-Content $installerPath -Raw
        $src | Should -Match ([regex]::Escape('/.claude/scripts/storage-cap-check.sh')) -Because '$bashCapCheck must resolve to the deployed file, not a missing path'
    }

    It 'verifier checks storage-cap-check.sh is deployed' {
        (Get-Content $verifierPath -Raw) | Should -Match 'storage-cap-check\.sh' -Because '3-verify.ps1 must assert the deployed cap-check file is present after install'
    }
}

Describe 'WSL installer auto-enables the sweep timers in report-safe defaults (v0.22 Phase G)' {

    It 'WSL installer deploys both sweep .service + .timer units' {
        $src = Get-Content $wslInstaller -Raw
        foreach ($u in @('goals-stale-sweep.service', 'goals-stale-sweep.timer',
                         'contradiction-sweep.service', 'contradiction-sweep.timer')) {
            $src | Should -Match ([regex]::Escape($u)) -Because "the unit must be copied into ~/.config/systemd/user before enable --now"
        }
    }

    It 'WSL installer enable --now both sweep timers' {
        $src = Get-Content $wslInstaller -Raw
        $src | Should -Match 'enable --now[^\n]*goals-stale-sweep\.timer'
        $src | Should -Match 'enable --now[^\n]*contradiction-sweep\.timer'
    }

    It 'goals-stale-sweep runs the SCOPED standing auto-abandon (operator-approved 2026-08-09)' {
        # Supersedes the old report-only pin. The safety moved into the script:
        # abandon_exempt never touches manual goals and --abandon-days defaults
        # to the operator-set 90 (pinned in test_goal_redesign.py). The unit
        # must pass --auto-abandon and must NOT override the 90d window.
        $exec = (Get-Content $goalsSweepSvc | Where-Object { $_ -match '^\s*ExecStart=' })
        $exec | Should -Not -BeNullOrEmpty
        ($exec -join "`n") | Should -Match '--auto-abandon' -Because 'the standing scoped auto-abandon was operator-approved 2026-08-09'
        ($exec -join "`n") | Should -Not -Match '--abandon-days' -Because 'the 90d window is the script contract; the unit must not quietly shrink it'
    }
}

Describe 'WSL installer provisions EmbeddingGemma, not Ollama+nomic (v0.22 H3)' {

    It 'WSL installer stages the EmbeddingGemma GGUF to a stable models path' {
        $src = Get-Content $wslInstaller -Raw
        $src | Should -Match 'embeddinggemma-300M-Q8_0\.gguf' -Because 'a clean install must fetch the egemma embedder GGUF'
        $src | Should -Match 'ggml-org/embeddinggemma-300M-GGUF' -Because 'the GGUF source repo must be referenced'
        # staged to a flat (non-tmp) models dir (v0.22 L9)
        $src | Should -Match '/models/embeddinggemma-300M-Q8_0\.gguf'
        $src | Should -Not -Match 'egemma-tmp' -Because 'L9: the fragile tmp-named path is retired'
    }

    It 'WSL installer verifies the :11436 EmbeddingGemma embed endpoint (not an Ollama check)' {
        $src = Get-Content $wslInstaller -Raw
        $src | Should -Match '11436/v1/embeddings' -Because 'the installer must verify mem0 can embed via llama-swap'
        $src | Should -Match '"model":"embeddinggemma"'
    }

    It 'WSL installer MEM0_MODULES includes egemma_embedder.py (config.py lazy import — fresh-install crash guard, v1.0 P7A B1)' {
        # config.py:build_embedder lazily imports egemma_embedder; app.py calls it at
        # startup. The python-side test_config_import_closure.py walks the real closure;
        # this is the Windows-side guard so a re-introduced omission fails the Pester gate too.
        (Get-Content $wslInstaller -Raw) | Should -Match 'MEM0_MODULES="[^"]*\begemma_embedder\.py\b'
    }

    It 'WSL installer does NOT provision Ollama or pull nomic (decommissioned from mem0 path)' {
        $src = Get-Content $wslInstaller -Raw
        # No active Ollama provisioning: no `ollama serve`, no `ollama pull`, no nomic pull
        $src | Should -Not -Match 'ollama serve'
        $src | Should -Not -Match 'ollama pull'
        $src | Should -Not -Match 'pull nomic-embed-text'
        # mem0 venv must not pip-install the ollama python package
        $src | Should -Not -Match "pip install[^\n]*\bollama\b"
    }

    It 'WSL installer deploys the version-controlled rollback-prune units + script (v0.22 M5)' {
        $src = Get-Content $wslInstaller -Raw
        $src | Should -Match 'egemma-rollback-prune\.service'
        $src | Should -Match 'egemma-rollback-prune\.timer'
        $src | Should -Match 'egemma-rollback-prune\.sh' -Because 'the unit ExecStart points at the deployed script'
        # repo ships both units (version-controlled, no longer hand-placed-only)
        Test-Path (Join-Path $repoRoot 'systemd\egemma-rollback-prune.service') | Should -BeTrue
        Test-Path (Join-Path $repoRoot 'systemd\egemma-rollback-prune.timer')   | Should -BeTrue
    }

    It 'WSL installer does NOT auto-enable the rollback-prune timer (one-shot migration cleanup)' {
        $src = Get-Content $wslInstaller -Raw
        $src | Should -Not -Match 'enable[^\n]*egemma-rollback-prune\.timer' -Because 'the destructive one-shot must not be armed by a fresh install'
    }

    It '0-prereqs decodes wsl.exe output as UTF-16 and accepts the native Claude install (v1.23.5)' {
        # The first remote (ssh) install read "WSL2 installed MISSING" and "claude.cmd MISSING" on a
        # box that had both: wsl.exe prints UTF-16 and the native installer puts claude.exe in ~\.local\bin.
        $prereq = Get-Content (Join-Path $repoRoot 'install\0-prereqs.ps1') -Raw
        $prereq | Should -Match '(?s)Check "WSL2 installed".*?OutputEncoding\s*=\s*\[System\.Text\.Encoding\]::Unicode.*?wsl\.exe --status'
        $prereq | Should -Match '\.local\\bin\\claude\.exe'
        $prereq | Should -Match 'Get-Command claude -ErrorAction SilentlyContinue'
    }
    It '0-prereqs no longer requires Ollama; checks llama-swap instead' {
        $prereq = Get-Content (Join-Path $repoRoot 'install\0-prereqs.ps1') -Raw
        $prereq | Should -Not -Match 'Check "Ollama in WSL"' -Because 'Ollama is no longer a prerequisite (v0.22)'
        $prereq | Should -Match 'llama-swap :11436'
    }
}

Describe 'installer is operator-agnostic: distro detect + receipt + no raw dev identifiers (v1.0 Phase 7A)' {

    It 'install.ps1 + 2-windows-config resolve the WSL distro (no hardcoded -d Ubuntu)' {
        $orch = Get-Content (Join-Path $repoRoot 'install.ps1') -Raw
        $win  = Get-Content $installerPath -Raw
        # the MCP registration must NOT hardcode the distro
        $win  | Should -Not -Match "args = @\('-d', 'Ubuntu'" -Because 'the mem0 MCP launch must use the resolved distro, not a literal Ubuntu'
        # both accept a -Distro param and the orchestrator detects one
        $orch | Should -Match '\[string\]\$Distro' -Because 'the orchestrator must accept/resolve the distro'
        $win  | Should -Match '\[string\]\$Distro'
        $orch | Should -Match 'wsl\.exe -l -q' -Because 'distro auto-detection enumerates installed distros'
    }

    It 'ALL phase sub-scripts (0-prereqs, 2-windows-config, 3-verify) accept -Distro AND carry no hardcoded -d Ubuntu' {
        # Audit fix: install.ps1 passes -Distro to every phase; a sub-script missing
        # the param throws a binding error that aborts the install (3-verify.ps1 regression).
        foreach ($p in @((Join-Path $repoRoot 'install\0-prereqs.ps1'), $installerPath, $verifierPath)) {
            $src = Get-Content $p -Raw
            $src | Should -Match '\[string\]\$Distro' -Because "$(Split-Path -Leaf $p) is called with -Distro by install.ps1 and must accept it"
            $src | Should -Not -Match 'wsl(\.exe)? -d Ubuntu' -Because "$(Split-Path -Leaf $p) must use the resolved `$Distro, not a literal Ubuntu"
        }
        # install.ps1 must actually pass -Distro to the verifier
        (Get-Content (Join-Path $repoRoot 'install.ps1') -Raw) | Should -Match '3-verify\.ps1.*-Distro' -Because 'the orchestrator threads the resolved distro to the verify phase'
    }

    It '2-windows-config writes the operator receipt mem0-stack.config.psd1' {
        $win = Get-Content $installerPath -Raw
        $win | Should -Match 'mem0-stack\.config\.psd1' -Because 'deployed scripts resolve operator-specific paths from this receipt'
        foreach ($k in @('WslUser', 'WinUser', 'Distro', 'RepoRootWin', 'RepoRootWsl')) {
            $win | Should -Match $k -Because "the receipt must record $k"
        }
    }

    It '1-wsl-services writes the WSL receipt ~/.mem0/stack.env' {
        (Get-Content $wslInstaller -Raw) | Should -Match 'stack\.env' -Because 'WSL-side scripts resolve operator paths from this receipt'
    }

    It 'no deployed windows script hardcodes the developer repo path' {
        foreach ($f in (Get-AstArrayStrings -Path $installerPath -VarName 'winScripts')) {
            $p = Join-Path $winDir $f
            if (Test-Path $p) {
                (Get-Content $p -Raw) | Should -Not -Match 'D:\\repos\\agentic-memory-stack' -Because "$f must derive the repo root from the receipt, never hardcode D:\repos"
            }
        }
    }

    It 'no deployed windows script carries the raw WSL handle, distro UNC, or raw win/dev path (PII scrub + operator-agnostic)' {
        # Operator extension: private patterns (machine names, brand names, personal
        # names) live OUTSIDE the repo in tests\pii-patterns.local.txt — one regex
        # per line, '#' comments allowed (gitignored; see pii-patterns.local.txt.example).
        $piiFile = Join-Path $PSScriptRoot 'pii-patterns.local.txt'
        $piiPatterns = @()
        if (Test-Path -LiteralPath $piiFile) {
            $piiPatterns = @(Get-Content -LiteralPath $piiFile |
                Where-Object { $_ -and $_.Trim() -and -not $_.Trim().StartsWith('#') } |
                ForEach-Object { $_.Trim() })
        }
        foreach ($f in (Get-AstArrayStrings -Path $installerPath -VarName 'winScripts')) {
            $p = Join-Path $winDir $f
            if (Test-Path $p) {
                $t = Get-Content $p -Raw
                $t | Should -Not -Match 'dmmdea'                        -Because "$f must use the __WSL_USER__ sentinel (or receipt), not the raw handle"
                $t | Should -Not -Match '\\\\wsl\.localhost\\Ubuntu\\'  -Because "$f must use __WSL_DISTRO__ (or receipt) in UNC paths, not a literal Ubuntu"
                $t | Should -Not -Match 'Users[\\/]dmmde\b'             -Because "$f must use __WIN_USER__ (or `$env:USERNAME), not the raw Windows handle"
                $t | Should -Not -Match '/mnt/d/repos|D:\\repos'        -Because "$f must derive the repo path from the receipt, not hardcode it"
                foreach ($pat in $piiPatterns) {
                    $t | Should -Not -Match $pat -Because "$f must not leak the operator-private pattern '$pat' (from pii-patterns.local.txt)"
                }
            }
        }
    }
}

Describe 'v1.16 deploy-layer-skew hardening: fail-open PreCompact, distro-agnostic hooks, one-brain role gate' {

    It 'PreCompact capture command is FAIL-OPEN (a missing/erroring capture script can never block compaction)' {
        # 2026-07-17: deploy-layer skew wiped precompact_capture.py on the brain box; python3 exited 2 and
        # Claude Code treats PreCompact exit-2 as a HARD BLOCK -> live sessions deadlocked at the
        # context limit. Capture is best-effort by contract (h13-postcompact.js: "never blocks,
        # always exit 0") — the registered command must swallow the python exit code.
        $src = Get-Content $installerPath -Raw
        $pcLine = ($src -split "`n" | Where-Object { $_ -match '^\$bashPreCompactCapture\s*=' }) -join "`n"
        $pcLine | Should -Not -BeNullOrEmpty
        $pcLine | Should -Match ([regex]::Escape('precompact_capture.py || true')) -Because 'without || true, any future skew re-arms the exact compaction deadlock'
    }

    It 'hook wsl.exe commands are distro-agnostic when the AMS distro is the WSL default (no unconditional -d)' {
        # The shared/cross-machine settings.json must not be polluted with a machine-specific
        # -d <distro> (box A=Ubuntu, box B=Ubuntu-ML share one file). -d is emitted ONLY when
        # the AMS distro is not the box default.
        $src = Get-Content $installerPath -Raw
        $src | Should -Match '\$wslDistroArg' -Because 'hook commands must build on the conditional distro arg'
        foreach ($var in @('bashCapCheck', 'bashPreCompactCapture')) {
            $line = ($src -split "`n" | Where-Object { $_ -match ('^\$' + $var + '\s*=') }) -join "`n"
            $line | Should -Match ([regex]::Escape('$wslDistroArg')) -Because "`$$var must use the conditional distro arg, never an unconditional -d `$Distro"
            $line | Should -Not -Match ([regex]::Escape("'-d ' + `$Distro")) -Because "`$$var with a hardcoded -d makes the shared settings.json machine-specific"
        }
    }

    It 'installer + orchestrator accept -Role (brain|replica) and install.ps1 threads it through' {
        $win  = Get-Content $installerPath -Raw
        $orch = Get-Content (Join-Path $repoRoot 'install.ps1') -Raw
        $win  | Should -Match "ValidateSet\('brain','replica'\)" -Because 'the installer must accept the one-brain role'
        $orch | Should -Match "ValidateSet\('brain','replica'\)" -Because 'the orchestrator must accept the one-brain role'
        $orch | Should -Match '2-windows-config\.ps1.*-Role' -Because 'the orchestrator must thread the role to the windows-config phase'
    }

    It 'dream + dedup task registration is gated on Role=brain, and a replica removes stale tasks' {
        # Registering the nightly canonical-mutation tasks unconditionally let a read-replica
        # (a replica box) run a destructive dedup against the one shared brain — no cross-machine lock
        # exists. Both Register-ScheduledTask calls must sit inside the brain gate; the replica
        # path must unregister leftovers from pre-v1.16 installs.
        $src = Get-Content $installerPath -Raw
        $gateIdx  = $src.IndexOf("if (`$Role -ne 'brain')")
        $endIdx   = $src.IndexOf('} # end brain-role gate')
        $gateIdx | Should -BeGreaterThan 0 -Because 'the role gate must exist'
        $endIdx  | Should -BeGreaterThan $gateIdx -Because 'the gate must be closed'
        $reg1 = $src.IndexOf('Register-ScheduledTask `')
        $reg2 = $src.IndexOf('Register-ScheduledTask -TaskName $dedupTaskName')
        $reg1 | Should -BeGreaterThan $gateIdx
        $reg1 | Should -BeLessThan $endIdx -Because 'the dream registration must be inside the brain gate'
        $reg2 | Should -BeGreaterThan $gateIdx
        $reg2 | Should -BeLessThan $endIdx -Because 'the dedup registration must be inside the brain gate'
        # the replica branch removes stale tasks
        $replicaBranch = $src.Substring($gateIdx, $src.IndexOf('} else {', $gateIdx) - $gateIdx)
        $replicaBranch | Should -Match 'Unregister-ScheduledTask' -Because 'a replica must remove tasks a pre-v1.16 install registered'
    }

    It 'the auto-memory compactor task is RETIRED: unregistered behind the proven hub path, after the binary and before hooks, never registered (P4-1a)' {
        # 2026-09-16 (register P4-1): the store binary's gate, sync and the hub judge replace the
        # PowerShell nightly. Placement is asserted both ways: the removal must follow the binary
        # install (a store is never left without a gate) and precede hook registration (the
        # watcher spawner is registered after the last PowerShell writer is gone), and it must sit
        # behind $amsSyncReady so a box with no proven hub keeps its legacy nightly.
        $src = Get-Content $installerPath -Raw
        $src | Should -Not -Match '(?<!Un)Register-ScheduledTask -TaskName \$compactTaskName' -Because 'nothing registers the PowerShell nightly any more (the lookbehind keeps the Unregister call out of the match)'
        $un = $src.IndexOf('Unregister-ScheduledTask -TaskName $compactTaskName -Confirm:$false')
        $un | Should -BeGreaterThan 0 -Because 'the installer must remove the task it used to register'
        $un | Should -BeGreaterThan $src.IndexOf('Install-AmsStoreBinary -Tag') -Because 'the gate binary must be installed before the nightly goes'
        $un | Should -BeLessThan $src.IndexOf('Registering hooks in settings.json') -Because 'the watcher spawner is registered only after the nightly is gone'
        $src.LastIndexOf('if ($amsSyncReady) {', $un) | Should -BeGreaterThan $src.IndexOf('# 1d. Retire the nightly compactor') -Because 'a box with no proven hub path keeps its legacy nightly'
        $src.IndexOf('Register-ScheduledTask -TaskName $dedupTaskName') | Should -BeLessThan $src.IndexOf('} # end brain-role gate')
    }

    It 'the installer and Test-MemoryStack judge the retired compactor with ONE hub-path predicate (2026-09-22 drift)' {
        # A replica with HubHost got "compactor task: not registered" FAIL right after a clean
        # install: the installer retired the task (hub proven) while the self-test still demanded
        # it. Both now call Get-AmHubPathGaps from memory-store-lib.ps1; neither may re-derive it.
        $src = Get-Content $installerPath -Raw
        $src | Should -Match ([regex]::Escape(". (Join-Path `$RepoRoot 'scripts\windows\memory-store-lib.ps1')"))
        $src | Should -Match ([regex]::Escape('$missing = @(Get-AmHubPathGaps -HubHost $HubHost -SshDir $amsSshDir)'))
        $src.IndexOf('$missing = @(Get-AmHubPathGaps') | Should -BeLessThan $src.IndexOf('$amsSyncReady = $true') -Because 'the predicate decides $amsSyncReady, which step 1d acts on'
        $src | Should -Not -Match 'if \(\$seeded -lt 1\)' -Because 'the host-key half of the predicate lives in the lib only'
        $tms = Get-Content $script:tmsPath -Raw
        $tms | Should -Match 'Get-AmHubPathGaps -HubHost \$cHub'
        $tms | Should -Match 'Get-AmCompactorTaskVerdict -Present \$false -HubGaps \$cGaps'
        $tms | Should -Not -Match "'auto-memory compactor task' 'FAIL' 'not registered - re-run 2-windows-config\.ps1'" -Because 'an absent task is not a FAIL on a box whose hub path is proven'
    }

    It 'the write-time index gate is the store binary (ams-store gate) on PostToolUse, registered over both legacy markers' {
        # 2026-09-03: the bash advisory was ignored live; 2026-09-16 (P4-1a): the Go gate replaces
        # the PowerShell gate in place - both legacy markers stay so the previous registrations are
        # REPLACED, never duplicated, and the PowerShell gate is registered nowhere.
        $src = Get-Content $installerPath -Raw
        $src.Contains("`$amsGateCmd = (New-HookExeCommand 'ams-store.exe') + ' gate'") | Should -BeTrue -Because 'the hook command is the quoted forward-slash exe path plus the verb'
        $src | Should -Match "'PostToolUse'\s*=\s*@\(@\{ markers = @\('memory-index-write-lint\.sh', 'memory-index-write-gate\.ps1', 'ams-store'\); command = \`$amsGateCmd" -Because 'the old markers must remain so the previous registrations are REPLACED, not duplicated'
        $src | Should -Match "matcher = 'Write\|Edit'" -Because 'the hook must be matcher-scoped to write tools'
        $src | Should -Not -Match 'command = \$psIndexGate' -Because 'the PowerShell gate must not be registered anywhere any more'
    }

    It 'deploys the write-time lint script and the three auto-memory scripts' {
        $wins = Get-AstArrayStrings -Path $installerPath -VarName 'winScripts'
        foreach ($n in @('memory-store-lib.ps1', 'memory-lint.ps1', 'memory-compact.ps1', 'memory-index-write-gate.ps1', 'codex-usage-report.ps1')) {
            $wins | Should -Contain $n -Because "$n must be deployed or the SessionStart child and the nightly task launch nothing"
        }
        (Get-Content $installerPath -Raw) | Should -Match 'memory-index-write-lint\.sh' -Because 'the PostToolUse hook script must be deployed from claude-config'
        Test-Path (Join-Path $repoRoot 'claude-config\memory-index-write-lint.sh') | Should -BeTrue
    }

    It 'the SessionStart spawner launches the store lint (ams-store lint --summary-out), with memory-lint.ps1 only as the fallback while the binary is absent (P4-1c)' {
        $code = ((Get-Content (Join-Path $winDir 'memory-maintenance-spawn.ps1') -Raw) -split "`r?`n" | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
        $code | Should -Match "'lint --summary-out \`"'" -Because 'the Go lint writes the summary the banner reads, with the G7 clock the PowerShell lint leaves null'
        $code | Should -Match "'state\\automemory'" -Because 'the summary must land in the state root the banner and Test-MemoryStack read'
        $code | Should -Match "--hub-host" -Because 'the history-remote rule needs the hub name to accept the hub remote'
        $i = $code.IndexOf("elseif (Test-Path (Join-Path `$ScriptDir 'memory-lint.ps1'))")
        $i | Should -BeGreaterThan 0 -Because 'the PowerShell lint stays as the fallback so a box with an aborted install is never without a lint'
        $i | Should -BeGreaterThan $code.IndexOf('if (Test-Path $exe) {') -Because 'the fallback is the else branch, never the first choice'
    }

    It 'no comment sits between a line-continuation backtick and the next argument' {
        # 2026-09-07 outage: a comment block was inserted between "-Hidden `" and
        # "-ExecutionTimeLimit ...". PowerShell PARSES that cleanly - ParseFile and
        # PSScriptAnalyzer both stayed silent - but the continuation swallows the comment and
        # every following argument becomes a NEW statement. The installer aborted at
        # "Registering Task Scheduler entry" with "The term '-ExecutionTimeLimit' is not
        # recognized", so the 3am dream, 4:30am dedup and 5am compactor tasks were never
        # registered on a box that had just been deployed to. Syntax checks cannot catch this;
        # this lint can.
        $offenders = @()
        foreach ($f in (Get-ChildItem -Path (Join-Path $script:repoRoot 'install') -Filter '*.ps1' -File)) {
            $lines = Get-Content -LiteralPath $f.FullName
            for ($i = 0; $i -lt $lines.Count - 1; $i++) {
                $cur = $lines[$i]
                # a real continuation: ends with a backtick that is not itself escaped
                if ($cur -notmatch '`\s*$') { continue }
                if ($cur -match '^\s*#') { continue }
                $next = $lines[$i + 1]
                if ($next -match '^\s*#') {
                    $offenders += ('{0}:{1}: continuation followed by a comment -> "{2}"' -f $f.Name, ($i + 2), $next.Trim())
                }
            }
        }
        $offenders | Should -BeNullOrEmpty -Because 'a comment after a continuation silently truncates the argument list'
    }

    It '3-verify asserts the compactor task' {
        (Get-Content $verifierPath -Raw) | Should -Match 'ClaudeCode-MemoryCompactor-5am' -Because 'a task nobody verifies is a task that silently stops firing'
    }

    It 'every scheduled-task principal is the current identity name, never $env:USERDOMAIN' {
        # On a workgroup box $env:USERDOMAIN is the literal WORKGROUP; "WORKGROUP\user" has no
        # SID and Register-ScheduledTask fails with "No mapping between account names and
        # security IDs" (observed deploying to a replica). WindowsIdentity.GetCurrent().Name is
        # the resolvable form on domain, workgroup and Microsoft-account boxes alike.
        $src = Get-Content $installerPath -Raw
        $src | Should -Not -Match 'New-ScheduledTaskPrincipal[^\n]*USERDOMAIN' -Because 'a DOMAIN\user principal does not resolve on a workgroup box'
        $src | Should -Match '\$taskUserId\s*=\s*\[System\.Security\.Principal\.WindowsIdentity\]::GetCurrent\(\)\.Name' -Because 'the principal must come from the current identity'
        ([regex]::Matches($src, 'New-ScheduledTaskPrincipal -UserId \$taskUserId')).Count | Should -Be 2 -Because 'the dream and dedup registrations use the shared principal (the compactor registration is retired, P4-1a)'
        # v1.20.3 defined $taskUserId INSIDE the brain branch, so a replica registered the
        # compactor (all roles, after the gate) with a null UserId. The definition must precede
        # the gate so every role sees it.
        $defIdx  = $src.IndexOf('$taskUserId = [System.Security.Principal.WindowsIdentity]')
        $gateIdx = $src.IndexOf("if (`$Role -ne 'brain')")
        $defIdx  | Should -BeGreaterThan 0
        $defIdx  | Should -BeLessThan $gateIdx -Because 'a replica skips the brain branch; the principal must be defined before the role gate'
    }

    It 'the operator receipt records the Role' {
        (Get-Content $installerPath -Raw) | Should -Match "Role\s+= '\`$eRole'" -Because '3-verify and runtime scripts resolve the box role from the receipt (quote-escaped like every other receipt value)'
    }

    It '3-verify carries the deploy-layer skew guard and role-aware task checks' {
        $src = Get-Content $verifierPath -Raw
        $src | Should -Match 'skew guard' -Because 'settings.json advancing ahead of the local deploy layer must be detected, not silently deadlock'
        $src | Should -Match 'Import-PowerShellDataFile' -Because 'the verifier must read the role from the receipt'
        $src | Should -Match 'ClaudeCode-SemanticDedup-430am' -Because 'the replica branch must assert BOTH nightly tasks are absent'
    }
}

Describe 'installer is resumable / verify-as-you-go (v1.0 Phase 7B)' {

    It 'WSL installer has an ERR trap with an actionable, resume-safe message' {
        $src = Get-Content $wslInstaller -Raw
        $src | Should -Match "trap '.*FAILED at line" -Because 'a failed step must fail fast with an actionable message, not a silent abort'
        $src | Should -Match 're-run install\.ps1' -Because 'the message must tell the operator a re-run is safe (idempotent)'
    }

    It 'the expensive GGUF fetch is guarded by an existence check (resume == re-run, no re-download)' {
        $src = Get-Content $wslInstaller -Raw
        $src | Should -Match 'if \[ ! -f "\$EGEMMA_GGUF" \]' -Because 'a re-run after a mid-install failure must NOT re-fetch the ~334MB GGUF'
    }

    It 'the mem0 venv + Qdrant binary are existence-guarded (idempotent re-run)' {
        $src = Get-Content $wslInstaller -Raw
        $src | Should -Match 'if \[ ! -d "\$MEM0_DIR/\.venv" \]'
        $src | Should -Match 'if \[ ! -x "\$QDRANT_DIR/qdrant" \]'
    }
}

Describe 'ams-store into the Windows install (register P4-1a, 2026-09-16)' {
    BeforeAll {
        $script:ciPath     = Join-Path $script:repoRoot '.github\workflows\ci.yml'
        $script:prereqPath = Join-Path $script:repoRoot 'install\0-prereqs.ps1'
        $script:src  = Get-Content $script:installerPath -Raw
        $script:code = ($script:src -split "`r?`n" | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
        # the release job is the LAST job in ci.yml; everything from its key to EOF is its text
        $ci = Get-Content $script:ciPath -Raw
        $ci.IndexOf("`n  release-assets:") | Should -BeGreaterThan 0 -Because 'the release job must exist in ci.yml'
        $script:releaseJob = $ci.Substring($ci.IndexOf("`n  release-assets:"))
        $script:ciHead     = $ci.Substring(0, $ci.IndexOf("`n  release-assets:"))
    }

    It 'the release job runs only for a pushed v* tag, with contents: write, on ubuntu-latest, and every other job skips tags' {
        $script:ciHead | Should -Match "(?m)^\s+tags:\s*\['v\*'\]" -Because 'the release is tag-triggered'
        $script:releaseJob | Should -Match "if: startsWith\(github\.ref, 'refs/tags/v'\)"
        $script:releaseJob | Should -Match '(?m)^\s+permissions:\s*\r?\n\s+contents:\s*write' -Because 'GITHUB_TOKEN needs contents: write to create the release'
        $script:releaseJob | Should -Match 'runs-on: ubuntu-latest'
        $script:releaseJob | Should -Not -Match 'windows-latest' -Because 'Windows minutes bill 2x; the Windows binary is a cross-compile'
        # every pre-existing job carries the tag guard, so a release tag re-runs none of the matrix
        $jobs = @([regex]::Matches($script:ciHead, '(?m)^  ([a-z0-9-]+):\s*$') | ForEach-Object { $_.Groups[1].Value })
        $jobs.Count | Should -BeGreaterOrEqual 9
        foreach ($j in $jobs) {
            $block = $script:ciHead.Substring($script:ciHead.IndexOf("`n  ${j}:"))
            $block.Substring(0, [Math]::Min(400, $block.Length)) | Should -Match "if: github\.ref_type != 'tag'" -Because "job $j must not re-run on a release tag"
        }
    }

    It 'the release job builds exactly the asset the installer downloads, plus SHA256SUMS, with --version stamped from the tag' {
        $script:code | Should -Match "\`$amsStoreAsset\s*=\s*'ams-store-windows-amd64\.exe'"
        $script:releaseJob | Should -Match 'GOOS=windows GOARCH=amd64 go build .* -o dist/ams-store-windows-amd64\.exe'
        $script:releaseJob | Should -Match 'GOOS=linux\s+GOARCH=amd64 go build .* -o dist/ams-store-linux-amd64'
        $script:releaseJob | Should -Match 'sha256sum .* > SHA256SUMS'
        $script:releaseJob | Should -Match '-X main\.version=\$\{GITHUB_REF_NAME\}' -Because '--version must print the tag the installer verifies against'
        $script:releaseJob | Should -Match 'tr -d .* < \.\./VERSION' -Because 'a tag that disagrees with VERSION is refused before anything is built'
        $script:releaseJob | Should -Match 'gh release create "\$\{GITHUB_REF_NAME\}"'
        $script:releaseJob | Should -Match '--verify-tag'
        $script:releaseJob | Should -Match 'dist/SHA256SUMS'
        ([regex]::Matches($script:releaseJob, 'dist/ams-store-windows-amd64\.exe')).Count | Should -BeGreaterOrEqual 2 -Because 'built once, attached once'
    }

    It 'the installer verifies the asset against SHA256SUMS and aborts BEFORE the receipt and BEFORE hook registration' {
        $script:code | Should -Match 'releases/download/\$Tag'
        $script:code | Should -Match 'Read-AmsSumsHash \$sums \$Asset'
        $script:code | Should -Match 'checksum mismatch'
        $fatal = $script:src.IndexOf('FATAL: ams-store.exe not installed')
        $fatal | Should -BeGreaterThan 0
        $fatal | Should -BeLessThan $script:src.IndexOf('$receipt = @"') -Because 'a failed install must leave the previous receipt intact'
        $fatal | Should -BeLessThan $script:src.IndexOf('Registering hooks in settings.json') -Because 'settings.json must never point at a missing exe'
        $script:code | Should -Match '\[string\]\$BinaryPath' -Because 'an offline drop is the sanctioned alternative to the download'
        $script:code | Should -Match '\[string\]\$BinarySums' -Because 'a drop is verified against the SHA256SUMS it shipped with'
    }

    It 'the sync hooks (SessionStart async, SessionEnd) exist only behind the proven hub path and are stripped otherwise' {
        $script:src.Contains("`$amsSyncCmd = (New-HookExeCommand 'ams-store.exe') + ' sync --once --hub-host ' + `$HubHost") | Should -BeTrue
        $from = $script:src.IndexOf('if ($amsSyncReady) {', $script:src.IndexOf('$retiredHookMarkers = @{'))
        $from | Should -BeGreaterThan 0
        $block = $script:src.Substring($from)
        $block = $block.Substring(0, $block.IndexOf('foreach ($evt in $retiredHookMarkers.Keys)'))
        $block | Should -Match "\`$hookEntries\['SessionStart'\] \+= @\{ markers = @\('ams-store'\); command = \`$amsSyncCmd; async = \`$true"
        $block | Should -Match "\`$hookEntries\['SessionEnd'\]\s*=\s*@\(@\{ markers = @\('ams-store'\); command = \`$amsSyncCmd"
        $block | Should -Match "\`$retiredHookMarkers\['SessionStart'\] = @\('ams-store'\)"
        $block | Should -Match "\`$retiredHookMarkers\['SessionEnd'\]\s*=\s*@\('ams-store'\)"
    }

    It 'the SessionStart maintenance spawner is registered (it launches the resident watcher) and reads the hub from the receipt' {
        $script:src | Should -Match "markers = @\('memory-maintenance-spawn\.ps1'\);\s*command = \`$psMaintSpawn; async = \`$true"
        $spawn = ((Get-Content (Join-Path $script:winDir 'memory-maintenance-spawn.ps1') -Raw) -split "`r?`n" | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
        $spawn | Should -Match "'sync --watch --hub-host '"
        $spawn | Should -Match 'Import-PowerShellDataFile'
        $spawn | Should -Not -Match 'memory-compact\.ps1' -Because 'the compactor catch-up spawn is retired with the nightly'
    }

    It 'the receipt records the hub and the installed binary, and inherits -HubHost on a re-run' {
        foreach ($f in "HubHost     = '\`$eHubHost'", "AmsStoreTag    = '\`$eAmsTag'", "AmsStoreSha256 = '\`$eAmsSha'", "AmsStoreSource = '\`$eAmsSource'") {
            $script:src | Should -Match $f
        }
        $script:code | Should -Match '\(Import-PowerShellDataFile \$receiptPath\)\.HubHost'
    }

    It 'the hub transport is written by the installer: ssh Match block, seeded known_hosts, main branch, one hub remote' {
        $script:code | Should -Match 'Set-AmsHubSshConfig -ConfigPath'
        $script:code | Should -Match 'Initialize-AmsKnownHosts -HubHost'
        $script:code | Should -Match 'Initialize-AmsHistoryRemote -GitDir'
        $script:code | Should -Match 'REFUSED: sync hooks and the watcher are NOT registered' -Because 'a silent strict-checking failure at SessionStart must be refused loudly here'
        $script:code | Should -Match "StateKnownHosts \(Join-Path \`$amsStateRoot 'known_hosts'\)" -Because 'the binary pins UserKnownHostsFile=<state>/known_hosts'
    }

    It '0-prereqs parses git >= 2.38 and 3-verify checks --version, the sidecar, the hooks, the sync exit code and the retired task' {
        $pre = Get-Content $script:prereqPath -Raw
        $pre | Should -Match 'Test-GitVersionAtLeast -VersionText .* -Major 2 -Minor 38'
        $v = Get-Content $script:verifierPath -Raw
        $v | Should -Match "'\^ams-store\\s\+' \+ \[regex\]::Escape\(\`$amsTag\)"
        $v | Should -Match '\$amsExe\.sha256'
        $v | Should -Match "ams-store\.exe`" gate"
        $v | Should -Match 'sync --once --hub-host \$amsHubHost --json'
        $v | Should -Match 'sync --watch'
        $v | Should -Match "\`$null -eq \(Get-ScheduledTask -TaskName 'ClaudeCode-MemoryCompactor-5am'"
    }
}

Describe 'SessionStart self-heal rebuilds a missing mem0-hook-client.exe (v0.20 Final)' {

    It 'spawn script contains the pre-probe self-heal (exe missing + .cs + builder present -> run builder)' {
        $src = Get-Content $spawnPath -Raw
        $src | Should -Match "mem0-hook-client\.exe"
        $src | Should -Match "mem0-hook-client\.cs"
        $src | Should -Match "build-hook-client\.ps1"
        # heal must run BEFORE the daemon pipe probe (off the prompt hot path,
        # and a broken probe must not skip the heal)
        $healIdx  = $src.IndexOf('build-hook-client.ps1')
        $probeIdx = $src.IndexOf('EnumerateFiles')
        $healIdx  | Should -BeGreaterThan 0
        $probeIdx | Should -BeGreaterThan $healIdx
    }

    It 'functionally heals: a deploy dir with .cs + builder but NO exe gets a smoke-passed exe after one spawn run' {
        # Sandbox: fake USERPROFILE so the builder's default DeployDir resolves
        # inside TestDrive — the REAL deployment is never touched.
        $sandboxProfile = Join-Path $TestDrive 'profile'
        $deployDir = Join-Path $sandboxProfile '.claude\scripts'
        New-Item -ItemType Directory -Path $deployDir -Force | Out-Null
        Copy-Item (Join-Path $winDir 'mem0-hook-daemon-spawn.ps1') $deployDir
        Copy-Item (Join-Path $winDir 'mem0-hook-client.cs')        $deployDir
        Copy-Item (Join-Path $winDir 'build-hook-client.ps1')      $deployDir
        $exe = Join-Path $deployDir 'mem0-hook-client.exe'
        Test-Path $exe | Should -BeFalse

        $savedProfile = $env:USERPROFILE
        try {
            $env:USERPROFILE = $sandboxProfile
            # Run the REAL spawn script (powershell 5.1, like the registered
            # SessionStart hook). The daemon pipe may exist live — irrelevant:
            # the heal runs before the probe, and mem0-hook-daemon.ps1 is
            # absent from the sandbox so no daemon is spawned.
            # Pipe (and CLOSE) stdin so the launcher's [Console]::In.ReadToEnd() returns —
            # exactly as Claude Code's SessionStart hook delivers it. Without this the
            # script blocks forever waiting for stdin EOF (hangs the isolated test run).
            '' | & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $deployDir 'mem0-hook-daemon-spawn.ps1') *> $null
        } finally {
            $env:USERPROFILE = $savedProfile
        }

        Test-Path $exe | Should -BeTrue -Because 'the SessionStart self-heal must rebuild the registered exe from the deployed .cs'
        # the healed exe must pass the builder's own smoke gate
        & (Join-Path $winDir 'build-hook-client.ps1') -SmokeOnly $exe *> $null
        $LASTEXITCODE | Should -Be 0
    }

    It 'writes a session-tier sidecar (model/tier/initiative) from the SessionStart payload (v0.22 Pillar 2)' {
        # Sandbox USERPROFILE so the real ~/.mem0 is never touched. Deploy the
        # spawn script + the lib (Resolve-ModelTier / Get-SessionInitiative) into
        # the sandbox scripts dir; feed a SessionStart payload with a haiku model
        # and a repo cwd on stdin; assert the sidecar lands with tier=small and
        # the correct cwd-derived initiative.
        $sandboxProfile = Join-Path $TestDrive 'profile-sidecar'
        $deployDir = Join-Path $sandboxProfile '.claude\scripts'
        New-Item -ItemType Directory -Path $deployDir -Force | Out-Null
        Copy-Item (Join-Path $winDir 'mem0-hook-daemon-spawn.ps1') $deployDir
        Copy-Item (Join-Path $winDir 'user-prompt-lib.ps1')        $deployDir
        Copy-Item (Join-Path $repoRoot 'claude-config\model-tiers.json') $deployDir

        $sid = [guid]::NewGuid().ToString()
        $cwd = $repoRoot   # a git repo -> initiative = repo leaf
        $payload = (@{
            hook_event_name = 'SessionStart'
            session_id      = $sid
            model           = 'claude-haiku-4-5'
            cwd             = $cwd
        } | ConvertTo-Json -Compress)

        $savedProfile = $env:USERPROFILE
        try {
            $env:USERPROFILE = $sandboxProfile
            $payload | & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $deployDir 'mem0-hook-daemon-spawn.ps1') *> $null
        } finally {
            $env:USERPROFILE = $savedProfile
        }

        $sidecar = Join-Path $sandboxProfile ('.mem0\session-tier\' + $sid + '.json')
        Test-Path $sidecar | Should -BeTrue -Because 'SessionStart must cache the resolved tier for the prompt path'
        $sc = Get-Content $sidecar -Raw | ConvertFrom-Json
        $sc.tier       | Should -Be 'small'
        $sc.model      | Should -Be 'claude-haiku-4-5'
        $sc.initiative | Should -Be (Split-Path -Leaf $repoRoot)
    }

    It 'SessionStart sidecar write is fail-open: a payload with NO model still spawns and writes no broken sidecar' {
        $sandboxProfile = Join-Path $TestDrive 'profile-sidecar-nomodel'
        $deployDir = Join-Path $sandboxProfile '.claude\scripts'
        New-Item -ItemType Directory -Path $deployDir -Force | Out-Null
        Copy-Item (Join-Path $winDir 'mem0-hook-daemon-spawn.ps1') $deployDir
        Copy-Item (Join-Path $winDir 'user-prompt-lib.ps1')        $deployDir
        Copy-Item (Join-Path $repoRoot 'claude-config\model-tiers.json') $deployDir

        $sid = [guid]::NewGuid().ToString()
        $payload = (@{ hook_event_name = 'SessionStart'; session_id = $sid; cwd = $repoRoot } | ConvertTo-Json -Compress)
        $savedProfile = $env:USERPROFILE
        try {
            $env:USERPROFILE = $sandboxProfile
            $payload | & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $deployDir 'mem0-hook-daemon-spawn.ps1') *> $null
            $LASTEXITCODE | Should -Be 0
        } finally {
            $env:USERPROFILE = $savedProfile
        }
        # No model in the payload -> tier defaults to frontier; sidecar is still
        # written (model=null, tier=frontier) so the prompt path skips the git
        # spawn. It must be valid JSON with tier=frontier.
        $sidecar = Join-Path $sandboxProfile ('.mem0\session-tier\' + $sid + '.json')
        if (Test-Path $sidecar) {
            $sc = Get-Content $sidecar -Raw | ConvertFrom-Json
            $sc.tier | Should -Be 'frontier'
        }
    }

    It 'does not heal (and does not throw) when the builder is absent — a failed heal installs nothing' {
        $sandboxProfile = Join-Path $TestDrive 'profile-nobuilder'
        $deployDir = Join-Path $sandboxProfile '.claude\scripts'
        New-Item -ItemType Directory -Path $deployDir -Force | Out-Null
        Copy-Item (Join-Path $winDir 'mem0-hook-daemon-spawn.ps1') $deployDir
        Copy-Item (Join-Path $winDir 'mem0-hook-client.cs')        $deployDir
        $exe = Join-Path $deployDir 'mem0-hook-client.exe'

        $savedProfile = $env:USERPROFILE
        try {
            $env:USERPROFILE = $sandboxProfile
            # Pipe (and CLOSE) stdin so the launcher's [Console]::In.ReadToEnd() returns —
            # exactly as Claude Code's SessionStart hook delivers it. Without this the
            # script blocks forever waiting for stdin EOF (hangs the isolated test run).
            '' | & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $deployDir 'mem0-hook-daemon-spawn.ps1') *> $null
            $LASTEXITCODE | Should -Be 0
        } finally {
            $env:USERPROFILE = $savedProfile
        }
        Test-Path $exe | Should -BeFalse
    }
}

Describe 'W6 PR-A: semantic-dedup task action executes the DEPLOYED copy' {
    It 'builds $dedupCmd from ~/apps/mem0-scripts, never $RepoRootWsl (checkout-as-deployment guard)' {
        $cfg = Get-Content (Join-Path $PSScriptRoot '..\..\..\install\2-windows-config.ps1') -Raw
        $line = ($cfg -split "`n" | Where-Object { $_ -match '^\$dedupCmd = ' }) -join ''
        $line | Should -Match 'apps/mem0-scripts/semantic-dedup\.py'
        $line | Should -Not -Match 'RepoRootWsl'
    }
    It 'runs the destructive dedup THROUGH the durable queue (W6 review fix 5: reverting to direct exec was green everywhere)' {
        $cfg = Get-Content (Join-Path $PSScriptRoot '..\..\..\install\2-windows-config.ps1') -Raw
        $line = ($cfg -split "`n" | Where-Object { $_ -match '^\$dedupCmd = ' }) -join ''
        $line | Should -Match 'jobs\.py run semantic-dedup'
        $line | Should -Match '--receipt /home/\$WslUser/\.mem0/dedup-summary\.jsonl'
        $line | Should -Match '--stale-after \d+'
    }
    It '3-verify rejects worktree/repo action paths (the old substring check was vacuous on the path)' {
        $v = Get-Content (Join-Path $PSScriptRoot '..\..\..\install\3-verify.ps1') -Raw
        $v | Should -Match 'apps/mem0-scripts/semantic-dedup\\\.py'
        $v | Should -Match 'notmatch .{1,3}mnt/\[a-z\]/\(Dev\|repos\)'
    }
}
