#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# WindowsReplicaProfile.Tests.ps1 - 1.35.1: a Windows PC replica can change its embedding profile, and its
# restore knows the 1.35.0 set (media memories, the profile the set was made in).
#
# A PC replica is install.ps1 / 2-windows-config.ps1 -Role replica plus install/1-wsl-services.sh inside a WSL
# distro. Its profile lives only in the distro's ~/.mem0/stack.env, and 1.35.0 gave it no way to change: the
# installers took no profile input, and the command the restore error named (install/linux-replica.sh) is the
# native-Linux installer, which on a WSL distro would overwrite MEM0_DISTRO and MEM0_WIN_USER and register a
# second (Linux) watcher. This suite pins the Windows path:
#   install.ps1 -EmbedProfile <p>          validated, forwarded to the WSL phase as MEM0_SET_EMBED_PROFILE
#   restore-replica.ps1                    names that command (never linux-replica.sh), extracts the set's media
#                                          tar additively, and checks /health/deep after mem0 starts
#   travel-mode.ps1                        seeds the media tar into the local cache with the rest of the set
# The restore script is RUN here against a scripted wsl.exe (nothing touches a real distro); the installer's
# wsl.exe line is evaluated, not just matched. The WSL side of the switch (install/1-wsl-services.sh) is
# tested by mem0-server/tests/test_stack_env_writers.py.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester <repo>/scripts/windows/tests/WindowsReplicaProfile.Tests.ps1 -Output Detailed"

BeforeAll {
    $script:winDir     = Split-Path -Parent $PSScriptRoot
    $script:repoRoot   = (Resolve-Path (Join-Path $script:winDir '..\..')).Path
    $script:installPs1 = Join-Path $script:repoRoot 'install.ps1'
    $script:restorePs1 = Join-Path $script:repoRoot 'scripts\travel\restore-replica.ps1'
    $script:restoreSh  = Join-Path $script:repoRoot 'scripts\travel\restore-replica.sh'
    $script:travelPs1  = Join-Path $script:repoRoot 'scripts\travel\travel-mode.ps1'

    function script:Get-CodeText {
        param([string]$Path)   # the file without its comment lines, so prose can never satisfy a pin
        ((Get-Content -LiteralPath $Path -Raw -Encoding UTF8) -split "`r?`n" |
            Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
    }
    function script:Get-Ast {
        param([string]$Path)
        $errs = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$errs)
        $errs | Should -BeNullOrEmpty -Because "$Path must parse"
        $ast
    }
}

Describe 'install.ps1 -EmbedProfile' {
    BeforeAll {
        $script:ast = script:Get-Ast $script:installPs1
        $script:param = $script:ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'EmbedProfile' }
        $script:wslCalls = @($script:ast.FindAll({ param($n)
            $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'wsl.exe' -and
            $n.Extent.Text -match '1-wsl-services\.sh' }, $true))
    }

    It 'is a string parameter, empty by default, so an omitted one changes nothing' {
        $script:param | Should -Not -BeNullOrEmpty
        $script:param.StaticType | Should -Be ([string])
        $script:param.DefaultValue.Value | Should -Be ''
    }

    It 'admits only lower-case letters, digits and hyphens (a profile name), and is case-sensitive' {
        $attr = $script:param.Attributes | Where-Object { $_.TypeName.Name -eq 'ValidatePattern' }
        $attr | Should -Not -BeNullOrEmpty
        $attr.PositionalArguments[0].Value | Should -Be '^[a-z0-9][a-z0-9-]*$'
        # ValidatePattern ignores case unless told not to: 'EGEMMA2' would otherwise reach the WSL phase
        ($attr.NamedArguments | Where-Object { $_.ArgumentName -eq 'Options' }).Argument.Value | Should -Be 'None'
    }

    It 'leaves the existing parameters alone' {
        $names = @($script:ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
        $names | Should -Be @('NonInteractive', 'LogFile', 'Distro', 'Role', 'AuthorityUrl', 'AuthoritySsh', 'EmbedProfile', 'MediaEmbedder')
        $role = $script:ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'Role' }
        $role.Extent.Text | Should -Match "ValidateSet\('brain','replica'\)"
    }

    It 'forwards MEM0_SET_EMBED_PROFILE on the same wsl.exe line as MEM0_ROLE, and only when given' {
        $script:wslCalls.Count | Should -Be 3 -Because 'role + profile, profile alone, neither'
        $withRole = @($script:wslCalls | Where-Object { $_.Extent.Text -match "MEM0_ROLE='\`$Role'" })
        $withRole.Count | Should -Be 1
        $withRole[0].Extent.Text | Should -Match 'if \(\$EmbedProfile\) \{ "MEM0_SET_EMBED_PROFILE=''\$EmbedProfile'' " \}'
        $alone = @($script:wslCalls | Where-Object { $_.Extent.Text -match 'MEM0_SET_EMBED_PROFILE' -and $_.Extent.Text -notmatch 'MEM0_ROLE' })
        $alone.Count | Should -Be 1
        $alone[0].Extent.Text | Should -Match 'if \(\$EmbedProfile\) \{ "MEM0_SET_EMBED_PROFILE=''\$EmbedProfile'' " \}'
        $plain = @($script:wslCalls | Where-Object { $_.Extent.Text -notmatch 'MEM0_ROLE|MEM0_SET_EMBED_PROFILE' })
        $plain.Count | Should -Be 1 -Because 'no -Role and no -EmbedProfile is the 1.35.0 call, unchanged'
        # the profile-alone call sits under an elseif on the parameter itself
        (Get-Content -Raw $script:installPs1) | Should -Match "(?s)ContainsKey\('Role'\)\) \{\s*wsl\.exe[^\n]*\s*\}\s*elseif \(\`$EmbedProfile -or \`$MediaEmbedder\) \{\s*wsl\.exe[^\n]*MEM0_SET_EMBED_PROFILE"
    }

    It 'builds exactly the 1.35.0 command line without -EmbedProfile, and adds the variable with it' -ForEach @(
        @{ Profile = '';        Expect = "MEM0_ROLE='replica' exec bash '/mnt/r/install/1-wsl-services.sh' 'wu' 'wn' 'Dist'" }
        @{ Profile = 'egemma2'; Expect = "MEM0_ROLE='replica' MEM0_SET_EMBED_PROFILE='egemma2' exec bash '/mnt/r/install/1-wsl-services.sh' 'wu' 'wn' 'Dist'" }
        @{ Profile = 'egemma2'; Media = 'off'; Expect = "MEM0_ROLE='replica' MEM0_SET_EMBED_PROFILE='egemma2' MEM0_SET_MEDIA_EMBEDDER='off' exec bash '/mnt/r/install/1-wsl-services.sh' 'wu' 'wn' 'Dist'" }
    ) {
        # evaluate the shipped -c argument of the explicit-Role call with the installer's own variables
        $call = @($script:wslCalls | Where-Object { $_.Extent.Text -match "MEM0_ROLE='\`$Role'" })[0]
        $arg = $call.CommandElements[-1]
        $arg | Should -BeOfType ([System.Management.Automation.Language.ExpandableStringExpressionAst])
        $sb = [scriptblock]::Create($arg.Extent.Text)
        $got = & {
            $Role = 'replica'; $EmbedProfile = $Profile; $MediaEmbedder = $Media; $repoWsl = '/mnt/r'; $wslUser = 'wu'; $Distro = 'Dist'
            $env:USERNAME = 'wn'
            & $sb
        }
        $got | Should -Be $Expect
    }

    It '-MediaEmbedder admits on or off only, empty by default (omitted keeps the recorded value)' {
        $media = $script:ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'MediaEmbedder' }
        $media | Should -Not -BeNullOrEmpty
        $media.DefaultValue.Value | Should -Be ''
        $set = $media.Attributes | Where-Object { $_.TypeName.Name -eq 'ValidateSet' }
        @($set.PositionalArguments | ForEach-Object { $_.Value }) | Should -Be @('', 'on', 'off')
        # ValidateSet ignores case and does not normalise, so 'ON' would reach the WSL phase, which accepts on|off only
        $ic = $set.NamedArguments | Where-Object { $_.ArgumentName -eq 'IgnoreCase' }
        $ic | Should -Not -BeNullOrEmpty
        $ic.Argument.Extent.Text | Should -Be '$false'
    }

    It 'forwards -MediaEmbedder without -Role on the elseif call, and only what was given' -ForEach @(
        @{ Profile = '';        Media = 'off'; Expect = "MEM0_SET_MEDIA_EMBEDDER='off' exec bash '/mnt/r/install/1-wsl-services.sh' 'wu' 'wn' 'Dist'" }
        @{ Profile = 'egemma2'; Media = '';    Expect = "MEM0_SET_EMBED_PROFILE='egemma2' exec bash '/mnt/r/install/1-wsl-services.sh' 'wu' 'wn' 'Dist'" }
    ) {
        $call = @($script:wslCalls | Where-Object { $_.Extent.Text -match 'MEM0_SET_MEDIA_EMBEDDER' -and $_.Extent.Text -notmatch 'MEM0_ROLE' })[0]
        $sb = [scriptblock]::Create($call.CommandElements[-1].Extent.Text)
        $got = & {
            $EmbedProfile = $Profile; $MediaEmbedder = $Media; $repoWsl = '/mnt/r'; $wslUser = 'wu'; $Distro = 'Dist'
            $env:USERNAME = 'wn'
            & $sb
        }
        $got | Should -Be $Expect
    }

    It 'does not remove the offline watcher restore marker itself: the WSL phase does, where it knows a switch was made' {
        # It used to clear the marker before the WSL phase whenever -EmbedProfile was given on a replica, even when
        # that phase then made no switch (the profile already recorded, or the name refused): a stale marker at
        # go_offline forces a restore that can throw, and the watcher then starts nothing.
        $code = script:Get-CodeText $script:installPs1
        $code | Should -Not -Match 'replica-restored'
        $code | Should -Not -Match 'restoredMarker'
        $code | Should -Not -Match 'Remove-Item'
    }

    It 'leaves the clearing to install/1-wsl-services.sh, which does it after the stack.env write, for a switched replica' {
        $wsl = Get-Content -Raw -LiteralPath (Join-Path $script:repoRoot 'install\1-wsl-services.sh')
        $wslCode = (($wsl -split "`r?`n") | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
        $wslCode | Should -Match '(?s)if \[ -n "\$EP_SWITCHED" \] && \[ "\$MEM0_ROLE" = "replica" \]; then\s*RESTORED_DIR=.*?/\.claude/state".*?rm -f "\$RESTORED_DIR/replica-restored\.txt"'
        $wslCode.IndexOf('stack.env written') | Should -BeLessThan $wslCode.IndexOf('rm -f "$RESTORED_DIR/replica-restored.txt"')
        # the marker the offline watcher reads is the file that script removes
        (Get-Content -Raw (Join-Path $script:repoRoot 'scripts\travel\offline-watcher.ps1')) | Should -Match "'\.claude\\state\\replica-restored\.txt'"
    }

    It 'tells a replica what the restore a cleared marker forces needs: a new-space set already in the offline cache' {
        $code = script:Get-CodeText $script:installPs1
        # after the WSL phase succeeded, so it is said only when the switch was made
        $afterWsl = $code.Substring($code.IndexOf('WSL services install failed'))
        $afterWsl | Should -Match "(?s)if \(\`$EmbedProfile -and \`$Role -eq 'replica'\) \{.*travel-mode\.ps1 on -DryRun"
        $afterWsl | Should -Match 'cannot fetch a set once the brain is unreachable'
        $afterWsl | Should -Match "'snapshot:' stamp"
    }

    Context 'run for real in a sandbox (PATH holds nothing but pwsh, so a run that reaches WSL fails there)' {
        BeforeAll {
            $script:pwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue | Select-Object -First 1).Source
            function script:Invoke-Install {
                param([string[]]$InstallArgs)
                $p = Join-Path $TestDrive ('p' + [guid]::NewGuid().ToString('N'))
                New-Item -ItemType Directory -Force -Path (Join-Path $p '.mem0') | Out-Null
                [System.IO.File]::WriteAllText((Join-Path $p '.mem0\role'), "replica`n")
                $psi = New-Object System.Diagnostics.ProcessStartInfo
                $psi.FileName = $script:pwshExe
                # parameter names go through bare (quoted, they would bind positionally); every value is single-quoted
                $quoted = ($InstallArgs | ForEach-Object { if ($_ -match '^-[A-Za-z]+$') { $_ } else { "'" + $_.Replace("'", "''") + "'" } }) -join ' '
                $inner = "try { & '" + $script:installPs1.Replace("'", "''") + "' $quoted; exit `$LASTEXITCODE } catch { [Console]::Error.WriteLine('INSTALLER-THREW: ' + `$_.Exception.Message); exit 1 }"
                $psi.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + $inner.Replace('"', '\"') + '"'
                $psi.UseShellExecute = $false
                $psi.RedirectStandardOutput = $true
                $psi.RedirectStandardError = $true
                $psi.CreateNoWindow = $true
                $root = [System.IO.Path]::GetPathRoot($p)
                $psi.EnvironmentVariables['USERPROFILE'] = $p
                $psi.EnvironmentVariables['HOME'] = $p
                $psi.EnvironmentVariables['HOMEDRIVE'] = $root.TrimEnd('\')
                $psi.EnvironmentVariables['HOMEPATH'] = '\' + $p.Substring($root.Length)
                $psi.EnvironmentVariables['PATH'] = (Split-Path -Parent $script:pwshExe)
                $proc = [System.Diagnostics.Process]::Start($psi)
                $so = $proc.StandardOutput.ReadToEndAsync(); $se = $proc.StandardError.ReadToEndAsync()
                $proc.WaitForExit(120000) | Should -BeTrue
                $proc.WaitForExit()
                [pscustomobject]@{ ExitCode = $proc.ExitCode; Text = ($so.Result + "`n" + $se.Result) }
            }
        }

        It 'refuses <Name> before it reaches WSL' -Skip:(-not (Get-Command pwsh -ErrorAction SilentlyContinue)) -ForEach @(
            @{ Name = 'a name with an upper-case letter'; Value = 'EGEMMA2' }
            @{ Name = 'a name with a space';              Value = 'egemma 2' }
            @{ Name = 'a leading hyphen';                 Value = '-egemma2' }
            @{ Name = 'shell metacharacters';             Value = "egemma2';id;'" }
            @{ Name = 'an empty name';                    Value = '' }
        ) {
            $r = script:Invoke-Install -InstallArgs @('-EmbedProfile', $Value)
            $r.ExitCode | Should -Not -Be 0
            $r.Text | Should -Match 'EmbedProfile'
            $r.Text | Should -Not -Match 'wsl\.exe' -Because 'the parameter is validated before anything reaches WSL'
        }

        It 'refuses -MediaEmbedder <Value> before it reaches WSL (on and off are lower-case only)' -Skip:(-not (Get-Command pwsh -ErrorAction SilentlyContinue)) -ForEach @(
            @{ Value = 'ON' }, @{ Value = 'Off' }, @{ Value = 'yes' }
        ) {
            $r = script:Invoke-Install -InstallArgs @('-MediaEmbedder', $Value)
            $r.ExitCode | Should -Not -Be 0
            $r.Text | Should -Match 'MediaEmbedder'
            $r.Text | Should -Not -Match 'wsl\.exe' -Because 'the parameter is validated before anything reaches WSL'
        }

        It 'lets -MediaEmbedder off through to the WSL phase' -Skip:(-not (Get-Command pwsh -ErrorAction SilentlyContinue)) {
            $r = script:Invoke-Install -InstallArgs @('-MediaEmbedder', 'off')
            $r.ExitCode | Should -Not -Be 0
            $r.Text | Should -Match 'wsl\.exe' -Because 'past validation the first thing it needs is WSL, which the sandbox does not have'
            $r.Text | Should -Not -Match 'Cannot validate argument'
        }

        It 'lets a valid name through to the WSL phase' -Skip:(-not (Get-Command pwsh -ErrorAction SilentlyContinue)) {
            $r = script:Invoke-Install -InstallArgs @('-EmbedProfile', 'egemma2')
            $r.ExitCode | Should -Not -Be 0
            $r.Text | Should -Match 'wsl\.exe' -Because 'past validation the first thing it needs is WSL, which the sandbox does not have'
            $r.Text | Should -Not -Match 'Cannot validate argument'
        }
    }
}

Describe 'restore-replica.ps1 names the Windows way to change profile' {
    BeforeAll {
        $script:ps1Raw = Get-Content -LiteralPath $script:restorePs1 -Raw -Encoding UTF8
        $script:ps1Code = script:Get-CodeText $script:restorePs1
    }
    It 'never names the native-Linux replica installer, in its error text or its header' {
        $script:ps1Raw | Should -Not -Match 'linux-replica'
    }
    It 'tells the operator to serve the alias, then run install.ps1 -EmbedProfile (or the WSL form)' {
        $script:ps1Code | Should -Match 'install\.ps1 -EmbedProfile \$\(\$space\.profile\)'
        $script:ps1Code | Should -Match 'MEM0_SET_EMBED_PROFILE=\$\(\$space\.profile\) bash install/1-wsl-services\.sh <wsluser> <winuser> <distro>'
        $script:ps1Code | Should -Match "Serve '\`$\(\`$space\.alias\)' on the local llama-swap"
        # the header says the same, and why the Linux installer is not the answer
        $script:ps1Raw | Should -Match '(?s)\.NOTES.*install\.ps1 -EmbedProfile <profile>.*MEM0_SET_EMBED_PROFILE=<profile>'
    }
    It 'restore-replica.sh (the Linux path) still names linux-replica.sh, and says it is the Linux one' {
        $sh = Get-Content -LiteralPath $script:restoreSh -Raw -Encoding UTF8
        $sh | Should -Match 'on this native-Linux replica re-run: bash install/linux-replica\.sh --embed-profile \$MP_PROFILE'
        $sh | Should -Match 'a Windows PC replica uses install\.ps1 -EmbedProfile \$MP_PROFILE'
        # what llama-swap must serve is the profile's ALIAS (embeddinggemma2), not its name (egemma2)
        $sh | Should -Match "Serve '\`$SET_ALIAS' \(profile '\`$MP_PROFILE'\) on llama-swap :11436"
    }
    It 'tells the operator that install.ps1 records the profile and stages the model files, then to serve the alias' {
        $script:ps1Code | Should -Match 'which records the profile and stages the model files its alias needs\. Serve '
        $script:ps1Raw | Should -Match 'travel-mode\.ps1 on -DryRun, while online, seeds the cache with one'
    }
}

Describe 'restore-replica.ps1 run against a scripted distro' {
    BeforeAll {
        $script:Stamp = '20261008T030000Z'
        $global:WrpCalls = [System.Collections.Generic.List[string]]::new()
        $global:WrpFake = @{}

        function script:New-Set {
            # a backup directory holding the artifacts the restore insists on, plus what the case adds
            param([hashtable]$Manifest, [string]$Media = '')
            $dir = Join-Path $TestDrive ('set' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            foreach ($f in "episodic-$($script:Stamp).db", "history-$($script:Stamp).db", "qdrant-$($script:Stamp).snapshot") {
                [System.IO.File]::WriteAllText((Join-Path $dir $f), 'x')
            }
            if ($Media) { [System.IO.File]::WriteAllText((Join-Path $dir $Media), 'tar') }
            ($Manifest | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath (Join-Path $dir "manifest-$($script:Stamp).json") -Encoding UTF8
            $dir
        }
        function script:New-Manifest {
            param($Media = "media-$($script:Stamp).tar", $Profile = 'egemma2', $Collection = 'mem0_eg2_768')
            $files = @{ qdrant_snapshot = "qdrant-$($script:Stamp).snapshot"; episodic_db = "episodic-$($script:Stamp).db"; history_db = "history-$($script:Stamp).db" }
            if ($null -ne $Media) { $files.media = $Media }
            $m = @{ files = $files; counts = @{ qdrant_points = 5 } }
            if ($Profile) { $m.embed_profile = $Profile }
            if ($Collection) { $m.collections = @{ memories = $Collection } }
            $m
        }
        function script:Set-Fake {
            param([hashtable]$Over = @{})
            $global:WrpCalls.Clear()
            $global:WrpFake = @{
                ep = '{"profile":"egemma2","local":"egemma2","alias":"embeddinggemma2","collection":"mem0_eg2_768","base_url":"http://localhost:11436/v1"}'
                aliasServed = $true
                deep = '{"ok":true,"collection":"mem0_eg2_768","embed_profile":{"profile":"egemma2"}}'
                tarOut = 'media-ok 3'
                tarErr = ''
                mediaReadable = $true
                # what `tar -tvf` prints for a set stack-backup.sh made: relative names, regular files and directories
                tarList = @(
                    'drwxr-xr-x 1000/1000           0 2026-10-08 03:00 ./'
                    'drwxr-xr-x 1000/1000           0 2026-10-08 03:00 ./ab/'
                    '-rw-r--r-- 1000/1000        1234 2026-10-08 03:00 ./ab/ab00.png'
                )
                tarListRc = 0
            }
            foreach ($k in $Over.Keys) { $global:WrpFake[$k] = $Over[$k] }
        }
        # a stand-in for wsl.exe: answers by what the command does, and keeps the commands in order
        function script:wsl.exe {
            $global:LASTEXITCODE = 0
            $cmd = [string]($args[-1])
            if ($args -contains 'python3') { $null = $input; return $global:WrpFake.ep }
            $global:WrpCalls.Add($cmd)
            switch -Regex ($cmd) {
                'command -v jq'                      { return 'ok' }
                "grep -q"                            { if ($global:WrpFake.aliasServed) { return 'ok' } else { return '' } }
                'wslpath'                            { $p = [regex]::Match($cmd, "wslpath '([^']+)'").Groups[1].Value; return '/mnt/' + ($p -replace '^([A-Za-z]):', '$1') }
                'test -r'                            { if ($cmd -match 'media-' -and -not $global:WrpFake.mediaReadable) { return '' } else { return 'ok' } }
                'snapshots/upload'                   { return '{"status":"ok","time":0.1}' }
                'tar -tvf'                           { $global:LASTEXITCODE = [int]$global:WrpFake.tarListRc; return $global:WrpFake.tarList }
                'tar -C'                             {
                    # stdout is what the restore sees; tar's stderr reaches it only when the command folds it in.
                    # Several lines come back as an array, the way wsl.exe's output reaches the caller (a login-shell
                    # banner before media-ok is the case this exists for).
                    $o = @($global:WrpFake.tarOut)
                    if ($global:WrpFake.tarErr -and $cmd -match '\}\s*2>&1') { $o = @($o) + @($global:WrpFake.tarErr) }
                    return @($o | Where-Object { $_ })
                }
                '18791/health/deep'                  { return $global:WrpFake.deep }
                '18791/health'                       { return '{"ok":true}' }
                'points_count'                       { return '5' }
                '&& echo ok'                         { return 'ok' }
                default                              { return '' }
            }
        }
        function script:Invoke-Restore {
            param([string]$Dir, [string]$Collection = '')
            $w = $null
            $more = @{}
            if ($Collection) { $more.Collection = $Collection }
            $null = & $script:restorePs1 -BackupDir $Dir -Stamp $script:Stamp -Distro 'Test' @more -WarningVariable w -WarningAction SilentlyContinue 6>$null
            ,@($w)
        }
        function script:Invoke-RestoreFull {
            # the same run, with what it printed (Write-Host reaches the information stream) as well as its warnings
            param([string]$Dir, [string]$Collection = '')
            $w = $null
            $more = @{}
            if ($Collection) { $more.Collection = $Collection }
            $out = & $script:restorePs1 -BackupDir $Dir -Stamp $script:Stamp -Distro 'Test' @more -WarningVariable w -WarningAction SilentlyContinue 6>&1
            [pscustomobject]@{
                Warnings = @($w)
                Info = @($out | Where-Object { $_ -is [System.Management.Automation.InformationRecord] } | ForEach-Object { "$($_.MessageData)" })
            }
        }
        function script:Get-CallIndex { param([string]$Pattern) for ($i = 0; $i -lt $global:WrpCalls.Count; $i++) { if ($global:WrpCalls[$i] -match $Pattern) { return $i } } -1 }
    }

    AfterAll { Remove-Variable WrpCalls, WrpFake -Scope Global -ErrorAction SilentlyContinue }

    It 'refuses a set in another profile and names install.ps1 -EmbedProfile, never linux-replica.sh' {
        script:Set-Fake @{ ep = '{"profile":"egemma2","local":"egemma-300m","alias":"embeddinggemma2","collection":"mem0_eg2_768","base_url":"http://localhost:11436/v1"}' }
        $dir = script:New-Set (script:New-Manifest)
        $err = $null
        try { script:Invoke-Restore $dir | Out-Null } catch { $err = $_.Exception.Message }
        $err | Should -Not -BeNullOrEmpty
        $err | Should -Match "set $($script:Stamp) is in embedding profile 'egemma2' but WSL \(Test\) is configured for 'egemma-300m'"
        $err | Should -Match 'install\.ps1 -EmbedProfile egemma2'
        $err | Should -Match 'MEM0_SET_EMBED_PROFILE=egemma2 bash install/1-wsl-services\.sh <wsluser> <winuser> <distro>'
        $err | Should -Match "Serve 'embeddinggemma2' on the local llama-swap"
        $err | Should -Not -Match 'linux-replica'
        (script:Get-CallIndex 'snapshots/upload') | Should -Be -1 -Because 'nothing is restored into the wrong space'
    }

    It 'restores a set with media: extracts it additively after the snapshot upload and before mem0 starts' {
        script:Set-Fake
        $dir = script:New-Set (script:New-Manifest) -Media "media-$($script:Stamp).tar"
        $w = script:Invoke-Restore $dir
        $w.Count | Should -Be 0
        $upload = script:Get-CallIndex 'snapshots/upload'
        $tar = script:Get-CallIndex 'tar -C'
        $start = script:Get-CallIndex 'systemctl --user start mem0\.service'
        $upload | Should -BeGreaterOrEqual 0
        $tar | Should -BeGreaterThan $upload
        $start | Should -BeGreaterThan $tar
        $global:WrpCalls[$tar] | Should -Match '--skip-old-files'
        $global:WrpCalls[$tar] | Should -Match '--no-same-owner'
        $global:WrpCalls[$tar] | Should -Match "media-$($script:Stamp)\.tar"
        $global:WrpCalls[$tar] | Should -Match 'MEM0_MEDIA_DIR'
        $global:WrpCalls[$tar] | Should -Match '\.mem0/media' -Because 'the default is the server default'
    }

    It 'finds the media directory the way the server gets it: the mem0 unit environment, never stack.env' {
        # mem0-server/media.py reads MEM0_MEDIA_DIR from the process environment only, and no installer writes it to
        # stack.env (which would drop it on the next run), so a relocated directory can only be a unit drop-in
        script:Set-Fake
        script:Invoke-Restore (script:New-Set (script:New-Manifest) -Media "media-$($script:Stamp).tar") | Out-Null
        $cmd = $global:WrpCalls[(script:Get-CallIndex 'tar -C')]
        $cmd | Should -Match 'systemctl --user show mem0\.service -p Environment --value'
        $cmd | Should -Not -Match 'stack\.env'
        $cmd | Should -Match 'MEM0_MEDIA_DIR:-\$HOME/\.mem0/media' -Because 'then the login shell, then the server default'
        (Get-Content -LiteralPath $script:restorePs1 -Raw -Encoding UTF8) | Should -Not -Match 'MEM0_MEDIA_DIR from ~/\.mem0/stack\.env'
    }

    It 'leaves mem0 and Qdrant running (travel mode needs them)' {
        script:Set-Fake
        script:Invoke-Restore (script:New-Set (script:New-Manifest)) | Out-Null
        (script:Get-CallIndex 'systemctl --user stop (mem0|qdrant)') | Should -BeGreaterOrEqual 0 -Because 'mem0 is stopped before the ledgers are replaced'
        $stops = @(0..($global:WrpCalls.Count - 1) | Where-Object { $global:WrpCalls[$_] -match 'systemctl --user stop' })
        $stops.Count | Should -Be 1
        $stops[0] | Should -BeLessThan (script:Get-CallIndex 'snapshots/upload')
    }

    It 'asks for no media when the manifest lists none' {
        script:Set-Fake
        $w = script:Invoke-Restore (script:New-Set (script:New-Manifest -Media $null))
        $w.Count | Should -Be 0
        (script:Get-CallIndex 'tar -C') | Should -Be -1
    }

    It 'warns, and still restores, when the listed media tar is not in the backup directory' {
        script:Set-Fake
        $w = script:Invoke-Restore (script:New-Set (script:New-Manifest))
        (script:Get-CallIndex 'tar -C') | Should -Be -1
        ($w -join "`n") | Should -Match "lists media-$($script:Stamp)\.tar but it is not in"
    }

    It 'warns, and still restores, when WSL cannot read the media tar' {
        script:Set-Fake @{ mediaReadable = $false }
        $w = script:Invoke-Restore (script:New-Set (script:New-Manifest) -Media "media-$($script:Stamp).tar")
        (script:Get-CallIndex 'tar -C') | Should -Be -1
        ($w -join "`n") | Should -Match 'not readable from WSL'
    }

    It 'a failed extraction is a WARNING, not a failed restore (the memories keep their captions)' {
        # tar says why on STDERR and the chain's `&& echo media-ok` never runs: the command must fold stderr in
        script:Set-Fake @{ tarOut = ''; tarErr = 'tar: Unexpected EOF in archive' }
        $w = script:Invoke-Restore (script:New-Set (script:New-Manifest) -Media "media-$($script:Stamp).tar")
        ($w -join "`n") | Should -Match 'could not extract media-.*their files are missing'
        ($w -join "`n") | Should -Match 'directory \(tar: Unexpected EOF in archive\)' -Because 'the reason reaches the operator, not an empty pair of parentheses'
        $global:WrpCalls[(script:Get-CallIndex 'tar -C')] | Should -Match '\{ d=.*\} 2>&1'
        (script:Get-CallIndex 'systemctl --user start mem0\.service') | Should -BeGreaterThan (script:Get-CallIndex 'tar -C') -Because 'the restore carried on'
    }

    It 'finds media-ok when a line comes before it (the output lines are joined by newlines, not spaces)' {
        # wsl.exe hands the output back as one string per line; "$(...)" joined them with a space, so ^media-ok only
        # matched as the FIRST line and a banner or tar warning ahead of it turned a good extraction into a warning
        script:Set-Fake @{ tarOut = @('Welcome to the distro (a login-shell banner)', 'tar: a warning that is not an error', 'media-ok 3') }
        $r = script:Invoke-RestoreFull (script:New-Set (script:New-Manifest) -Media "media-$($script:Stamp).tar")
        $r.Warnings.Count | Should -Be 0
        ($r.Info -join "`n") | Should -Match '    media: 3 file\(s\) in the distro''s media directory'
    }

    Context 'the media tar is listed before it is extracted' {
        It 'lists it (tar -tvf) first, then extracts a tar of regular files and directories' {
            script:Set-Fake
            $w = script:Invoke-Restore (script:New-Set (script:New-Manifest) -Media "media-$($script:Stamp).tar")
            $w.Count | Should -Be 0
            $list = script:Get-CallIndex 'tar -tvf'
            $list | Should -BeGreaterOrEqual 0
            $list | Should -BeLessThan (script:Get-CallIndex 'tar -C')
            $global:WrpCalls[$list] | Should -Match "tar -tvf '[^']*media-$($script:Stamp)\.tar'"
        }

        It 'does not mistake tar''s own diagnostics for entries' {
            script:Set-Fake @{ tarList = @('tar: Ignoring unknown extended header keyword', 'drwxr-xr-x 1000/1000 0 2026-10-08 03:00 ./', '-rw-r--r-- 1000/1000 5 2026-10-08 03:00:09 ./ab/x.png') }
            $w = script:Invoke-Restore (script:New-Set (script:New-Manifest) -Media "media-$($script:Stamp).tar")
            $w.Count | Should -Be 0
            (script:Get-CallIndex 'tar -C') | Should -BeGreaterOrEqual 0
        }

        It 'refuses to extract a tar with <Name>, with a warning that says why, and still restores the rest' -ForEach @(
            @{ Name = 'a symlink entry'; Why = 'not a regular file or directory'
               Entry = 'lrwxrwxrwx 1000/1000           0 2026-10-08 03:00 ./ab/escape -> /home' }
            @{ Name = 'a hard link'; Why = 'not a regular file or directory'
               Entry = 'hrw-r--r-- 1000/1000           0 2026-10-08 03:00 ./ab/other link to ./ab/ab00.png' }
            @{ Name = 'a device node'; Why = 'not a regular file or directory'
               Entry = 'crw-rw-rw- 0/0               1,3 2026-10-08 03:00 ./ab/null' }
            @{ Name = 'an absolute name'; Why = "absolute or '\.\.' name"
               Entry = '-rw-r--r-- 1000/1000           5 2026-10-08 03:00 /etc/cron.d/evil' }
            @{ Name = 'a ".." component'; Why = "absolute or '\.\.' name"
               Entry = '-rw-r--r-- 1000/1000           5 2026-10-08 03:00 ./ab/../../evil' }
            @{ Name = 'a bare ".." directory'; Why = "absolute or '\.\.' name"
               Entry = 'drwxr-xr-x 1000/1000           0 2026-10-08 03:00 ../' }
            @{ Name = 'a line it cannot read'; Why = 'cannot read'
               Entry = '-rw-r--r-- not a listing line' }
        ) {
            $clean = @('drwxr-xr-x 1000/1000           0 2026-10-08 03:00 ./', '-rw-r--r-- 1000/1000        1234 2026-10-08 03:00 ./ab/ab00.png')
            script:Set-Fake @{ tarList = @($clean + $Entry) }
            $w = script:Invoke-Restore (script:New-Set (script:New-Manifest) -Media "media-$($script:Stamp).tar")
            (script:Get-CallIndex 'tar -C') | Should -Be -1 -Because 'nothing is extracted from a tar that holds that'
            ($w -join "`n") | Should -Match "not extracting media-$($script:Stamp)\.tar"
            ($w -join "`n") | Should -Match $Why
            ($w -join "`n") | Should -Match 'keep their captions but their files are missing'
            (script:Get-CallIndex 'systemctl --user start mem0\.service') | Should -BeGreaterThan (script:Get-CallIndex 'snapshots/upload') -Because 'the restore carried on'
        }

        It 'refuses, and says what tar said, when tar cannot list it' {
            script:Set-Fake @{ tarList = @('tar: This does not look like a tar archive', 'tar: Exiting with failure status due to previous errors'); tarListRc = 2 }
            $w = script:Invoke-Restore (script:New-Set (script:New-Manifest) -Media "media-$($script:Stamp).tar")
            (script:Get-CallIndex 'tar -C') | Should -Be -1
            ($w -join "`n") | Should -Match 'tar could not list it \(tar: This does not look like a tar archive'
        }
    }

    Context 'an explicit -Collection (restore under a name you will bind yourself)' {
        It 'is not compared with the server''s binding afterwards: a note instead of a throw' {
            # the server is still bound to the profile's own collection, which is exactly why the operator chose a
            # name of their own; comparing it with that name threw AFTER the upload
            script:Set-Fake @{ deep = '{"ok":true,"collection":"mem0_eg2_768","embed_profile":{"profile":"egemma2"}}' }
            $r = script:Invoke-RestoreFull (script:New-Set (script:New-Manifest)) -Collection 'my_own_collection'   # a throw fails the test
            $global:WrpCalls[(script:Get-CallIndex 'snapshots/upload')] | Should -Match 'collections/my_own_collection/snapshots/upload'
            ($r.Info -join "`n") | Should -Match "note: restored under the -Collection name 'my_own_collection'; the server's binding \('mem0_eg2_768'\) was not checked against it"
        }
        It 'still compares the profile' {
            script:Set-Fake @{ deep = '{"ok":true,"collection":"mem0_eg2_768","embed_profile":{"profile":"egemma-300m"}}' }
            { script:Invoke-Restore (script:New-Set (script:New-Manifest)) -Collection 'my_own_collection' } |
                Should -Throw "*bound to embedding profile 'egemma-300m', but the restored set is 'egemma2'*"
        }
        It 'says nothing about it, and still throws on a mismatch, when no -Collection was given' {
            script:Set-Fake @{ deep = '{"ok":true,"collection":"mem0_egemma_768","embed_profile":{"profile":"egemma2"}}' }
            { script:Invoke-Restore (script:New-Set (script:New-Manifest)) } | Should -Throw "*bound to collection 'mem0_egemma_768', but the set was restored into 'mem0_eg2_768'*"
        }
    }

    It 'never lets a manifest-supplied media name into a shell command unless it is a plain file name' -ForEach @(
        @{ Name = "x.tar'; rm -rf ~; echo '.tar" }
        @{ Name = '..\..\evil.tar' }
        @{ Name = 'media 1.tar' }
        @{ Name = 'media-1.tgz' }
    ) {
        script:Set-Fake
        $w = script:Invoke-Restore (script:New-Set (script:New-Manifest -Media $Name))
        (script:Get-CallIndex 'tar -C') | Should -Be -1
        ($w -join "`n") | Should -Match 'names an odd media file'
    }

    Context '/health/deep after mem0 starts' {
        It 'passes when it reports the restored profile and collection' {
            script:Set-Fake
            { script:Invoke-Restore (script:New-Set (script:New-Manifest)) } | Should -Not -Throw
        }
        It 'refuses a server bound to another profile' {
            script:Set-Fake @{ deep = '{"ok":true,"collection":"mem0_eg2_768","embed_profile":{"profile":"egemma-300m"}}' }
            { script:Invoke-Restore (script:New-Set (script:New-Manifest)) } |
                Should -Throw "*bound to embedding profile 'egemma-300m', but the restored set is 'egemma2'*"
        }
        It 'refuses a server bound to another collection' {
            script:Set-Fake @{ deep = '{"ok":true,"collection":"mem0_egemma_768","embed_profile":{"profile":"egemma2"}}' }
            { script:Invoke-Restore (script:New-Set (script:New-Manifest)) } |
                Should -Throw "*bound to collection 'mem0_egemma_768', but the set was restored into 'mem0_eg2_768'*"
        }
        It 'reads the binding from a degraded answer too (503 with its JSON)' {
            script:Set-Fake @{ deep = '{"ok":false,"collection":"mem0_egemma_768","embed_profile":{"profile":"egemma2"}}' }
            { script:Invoke-Restore (script:New-Set (script:New-Manifest)) } | Should -Throw "*bound to collection*"
        }
        It 'does not second-guess a server that reports neither (one from before the profile report)' {
            script:Set-Fake @{ deep = '{"ok":true}' }
            { script:Invoke-Restore (script:New-Set (script:New-Manifest)) } | Should -Not -Throw
        }
        It 'warns, without failing, when it answers no readable JSON' {
            script:Set-Fake @{ deep = '<html>502</html>' }
            $w = script:Invoke-Restore (script:New-Set (script:New-Manifest))
            ($w -join "`n") | Should -Match 'did not answer readable JSON'
        }
        It 'checks a set from before profiles against the legacy collection it holds' {
            script:Set-Fake @{
                ep = '{"profile":"egemma-300m","local":"egemma-300m","alias":"embeddinggemma","collection":"mem0_egemma_768","base_url":"http://localhost:11436/v1"}'
                deep = '{"ok":true,"collection":"mem0_egemma_768","embed_profile":{"profile":"egemma-300m"}}'
            }
            { script:Invoke-Restore (script:New-Set (script:New-Manifest -Media $null -Profile '' -Collection '')) } | Should -Not -Throw
        }
    }
}

Describe 'travel-mode.ps1 seeds the media tar with the rest of the set' {
    BeforeAll {
        $ast = script:Get-Ast $script:travelPs1
        foreach ($n in 'Get-SetFiles', 'Get-SetMediaFile', 'Test-SetMediaMissing', 'Get-NewestCompleteSet') {
            $fn = $ast.Find({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true)
            $fn | Should -Not -BeNullOrEmpty -Because "$n must exist in travel-mode.ps1"
            . ([scriptblock]::Create($fn.Extent.Text))
        }
        $script:Stamp = '20261008T030000Z'
        function script:New-CacheDir {
            param($Media = "media-$($script:Stamp).tar", [bool]$MediaPresent = $true, [bool]$Complete = $true)
            $dir = Join-Path $TestDrive ('c' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            $s = $script:Stamp
            $files = @("manifest-$s.json", "episodic-$s.db", "history-$s.db", "qdrant-$s.snapshot")
            if (-not $Complete) { $files = $files[0..2] }
            foreach ($f in $files) { [System.IO.File]::WriteAllText((Join-Path $dir $f), 'x') }
            $m = @{ files = @{ qdrant_snapshot = "qdrant-$s.snapshot" } }
            if ($null -ne $Media) { $m.files.media = $Media }
            ($m | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath (Join-Path $dir "manifest-$s.json") -Encoding UTF8
            if ($Media -and $MediaPresent -and $Media -match '^[A-Za-z0-9][A-Za-z0-9._-]*\.tar$') { [System.IO.File]::WriteAllText((Join-Path $dir $Media), 'tar') }
            $dir
        }
    }

    It 'includes media-<ts>.tar in the copy/prune list when the manifest lists it and it is there' {
        $dir = script:New-CacheDir
        $names = @(Get-SetFiles $dir $script:Stamp -WithMedia | ForEach-Object { Split-Path $_ -Leaf })
        $names | Should -Contain "media-$($script:Stamp).tar"
        $names.Count | Should -Be 5
    }
    It 'is the plain four-file list without -WithMedia: media is never part of a COMPLETE set' {
        $dir = script:New-CacheDir
        @(Get-SetFiles $dir $script:Stamp).Count | Should -Be 4
        Get-NewestCompleteSet $dir | Should -Be $script:Stamp
        # a set whose tar is listed but missing is still complete, and still copyable
        $noTar = script:New-CacheDir -MediaPresent $false
        Get-NewestCompleteSet $noTar | Should -Be $script:Stamp
        @(Get-SetFiles $noTar $script:Stamp -WithMedia).Count | Should -Be 4 -Because 'an optional file that is absent is simply not copied'
    }
    It 'adds nothing for a set from before 1.35.0 (a manifest that reads and has no files.media), stray tar or not' {
        @(Get-SetFiles (script:New-CacheDir -Media $null) $script:Stamp -WithMedia).Count | Should -Be 4
        $stray = script:New-CacheDir -Media $null
        [System.IO.File]::WriteAllText((Join-Path $stray "media-$($script:Stamp).tar"), 'tar')
        @(Get-SetFiles $stray $script:Stamp -WithMedia).Count | Should -Be 4 -Because 'a manifest that reads and lists no media is not overruled by a file lying there'
    }
    # Changed in 1.35.1: this case used to assert that a set whose manifest cannot be read adds nothing, which
    # pinned the defect. Retention prunes by the manifests that exist, so a set whose manifest is torn kept its
    # media tar for ever. The tar's name is fixed (stack-backup.sh writes media-<ts>.tar), so it is the fallback.
    It 'falls back to media-<ts>.tar when the manifest cannot be read, so retention still prunes that set''s tar' -ForEach @(
        @{ Name = 'unparseable'; Body = '{ not json' }
        @{ Name = 'empty';       Body = '' }
    ) {
        $bad = script:New-CacheDir
        [System.IO.File]::WriteAllText((Join-Path $bad "manifest-$($script:Stamp).json"), $Body)
        $names = @(Get-SetFiles $bad $script:Stamp -WithMedia | ForEach-Object { Split-Path $_ -Leaf })
        $names | Should -Contain "media-$($script:Stamp).tar"
        $names.Count | Should -Be 5
    }
    It 'adds nothing for an unreadable manifest when there is no tar to fall back to' {
        $bad = script:New-CacheDir -MediaPresent $false
        Set-Content -LiteralPath (Join-Path $bad "manifest-$($script:Stamp).json") -Value '{ not json'
        @(Get-SetFiles $bad $script:Stamp -WithMedia).Count | Should -Be 4
    }
    It 'prunes the tar of a set whose manifest cannot be read, through the same retention line travel-mode.ps1 runs' {
        $bad = script:New-CacheDir
        [System.IO.File]::WriteAllText((Join-Path $bad "manifest-$($script:Stamp).json"), '{ not json')
        Get-SetFiles $bad $script:Stamp -WithMedia | Where-Object { Test-Path $_ } | Remove-Item -Force
        @(Get-ChildItem -LiteralPath $bad -File).Count | Should -Be 0 -Because 'the tar goes with the set'
    }
    It 'trusts only a plain file name from the manifest' -ForEach @(
        @{ Name = '..\outside.tar' }, @{ Name = 'sub/dir.tar' }, @{ Name = 'a b.tar' }, @{ Name = 'media.zip' }
    ) {
        $dir = script:New-CacheDir -Media $Name
        @(Get-SetFiles $dir $script:Stamp -WithMedia).Count | Should -Be 4
    }
    Context 'a set that looks complete without its media tar' {
        It 'is seen when the local copy lacks the tar the cloud copy has (a torn copy, or a cache 1.35.0 seeded)' {
            $cloud = script:New-CacheDir
            $local = script:New-CacheDir -MediaPresent $false
            Get-NewestCompleteSet $local | Should -Be $script:Stamp -Because 'it is complete: the tar is optional'
            Test-SetMediaMissing $cloud $local $script:Stamp | Should -BeTrue
        }
        It 'is seen when the local tar is another size than the cloud one' {
            $cloud = script:New-CacheDir
            $local = script:New-CacheDir
            [System.IO.File]::WriteAllText((Join-Path $local "media-$($script:Stamp).tar"), 'torn-halfway-and-longer')
            Test-SetMediaMissing $cloud $local $script:Stamp | Should -BeTrue
        }
        It 'is quiet when the local copy holds the tar whole' {
            Test-SetMediaMissing (script:New-CacheDir) (script:New-CacheDir) $script:Stamp | Should -BeFalse
        }
        It 'is quiet when the cloud copy has no tar to fetch (a set from before 1.35.0, or the file absent there)' {
            $local = script:New-CacheDir -MediaPresent $false
            Test-SetMediaMissing (script:New-CacheDir -Media $null) $local $script:Stamp | Should -BeFalse
            Test-SetMediaMissing (script:New-CacheDir -MediaPresent $false) $local $script:Stamp | Should -BeFalse
        }
        It 'never follows a manifest-supplied name that is not a plain file name' -ForEach @(
            @{ Name = '..\outside.tar' }, @{ Name = 'sub/dir.tar' }
        ) {
            Test-SetMediaMissing (script:New-CacheDir -Media $Name) (script:New-CacheDir -MediaPresent $false) $script:Stamp | Should -BeFalse
        }
        It 'makes the seeding run for the SAME stamp too, and only fetches what is missing' {
            $code = script:Get-CodeText $script:travelPs1
            $code | Should -Match '\(\$cloudStamp -gt "\$localStamp"\) -or\s*\(\$cloudStamp -eq "\$localStamp" -and \(Test-SetMediaMissing \$CloudBackupDir \$LocalBackupDir \$cloudStamp\)\)'
            # the copy loop skips every file already held at the right size, so an equal stamp costs one file
            $code | Should -Match 'if \(\(Test-Path \$dst\) -and \(Get-Item \$dst\)\.Length -eq \(Get-Item \$src\)\.Length\) \{ continue \}'
        }
    }
    It 'seeds from pCloud and prunes the local cache with the media-aware list, guarded by the source' {
        $code = script:Get-CodeText $script:travelPs1
        $code | Should -Match 'foreach \(\$src in \(Get-SetFiles \$CloudBackupDir \$cloudStamp -WithMedia\)\)'
        $code | Should -Match 'Get-SetFiles \$LocalBackupDir \$_ -WithMedia \| Where-Object \{ Test-Path \$_ \} \| Remove-Item -Force'
        # completeness is still the four required files
        $code | Should -Match 'if \(-not \(Get-SetFiles \$dir \$s \| Where-Object \{ -not \(Test-Path \$_\) \}\)\) \{ return \$s \}'
    }
}
