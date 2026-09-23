#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# AmsStoreInstall.Tests.ps1 - the installer's ams-store block (register P4-1a, session-7 plan
# Q-C / Q-E / Q-F): the pieces that decide whether a PC is cut over run here for real against a
# TestDrive, extracted from the installer's text. The installer itself is top-level code that
# would touch the real box if dot-sourced, which is the same reason InstallerParity parses it.
#
# Run: pwsh -NoProfile -File scripts\windows\Run-PesterTests.ps1 -Path scripts\windows\tests\AmsStoreInstall.Tests.ps1 -Detailed

BeforeAll {
    $script:winDir    = Split-Path -Parent $PSScriptRoot
    $script:repoRoot  = Split-Path -Parent (Split-Path -Parent $script:winDir)
    $script:installer = Join-Path $script:repoRoot 'install\2-windows-config.ps1'
    $script:prereqs   = Join-Path $script:repoRoot 'install\0-prereqs.ps1'

    function script:Get-FunctionText {
        # The full text of ONE function definition in a script: from `function Name {` at column 0
        # through the first `}` at column 0 (every brace inside the body is indented).
        param([string]$Path, [string]$Name)
        $src = Get-Content -LiteralPath $Path -Raw
        $m = [regex]::Match($src, "(?ms)^function $([regex]::Escape($Name)) \{.*?^\}")
        if (-not $m.Success) { throw "function $Name not found in $Path" }
        $m.Value
    }
    # The installer dot-sources memory-store-lib.ps1 for the shared hub-path predicate
    # (Get-AmHubHostKeyLines / Get-AmHubPathGaps); the extracted functions need it too.
    . (Join-Path $script:winDir 'memory-store-lib.ps1')
    foreach ($n in 'Get-AmsFileSha256', 'Read-AmsSumsHash', 'Set-AmsHubSshConfig', 'Initialize-AmsKnownHosts', 'Initialize-AmsHistoryRemote', 'Get-AmsStoreVersionToken', 'Install-AmsStoreBinary', 'Get-AmsArgv0', 'Select-AmsStoreProcessesForStore', 'Stop-AmsStoreProcessesForStore', 'Confirm-AmsWatcherAlive') {
        . ([scriptblock]::Create((script:Get-FunctionText -Path $script:installer -Name $n)))
    }
    . ([scriptblock]::Create((script:Get-FunctionText -Path $script:prereqs -Name 'Test-GitVersionAtLeast')))
}

Describe 'Read-AmsSumsHash (the release SHA256SUMS parser)' {
    It 'reads the sha256sum text-mode and binary-mode shapes and lowercases the digest' {
        $hexA = 'A' * 64; $hexB = 'b' * 64
        $sums = "$hexA  ams-store-windows-amd64.exe`n$hexB *ams-store-linux-amd64`n"
        Read-AmsSumsHash -SumsText $sums -Asset 'ams-store-windows-amd64.exe' | Should -Be ('a' * 64)
        Read-AmsSumsHash -SumsText $sums -Asset 'ams-store-linux-amd64' | Should -Be $hexB
    }
    It 'returns null for an asset the file does not list, and for a line that names it without a 64-hex digest' {
        Read-AmsSumsHash -SumsText ("{0}  other`n" -f ('c' * 64)) -Asset 'ams-store-windows-amd64.exe' | Should -BeNullOrEmpty
        Read-AmsSumsHash -SumsText "deadbeef  ams-store-windows-amd64.exe`n" -Asset 'ams-store-windows-amd64.exe' | Should -BeNullOrEmpty
        Read-AmsSumsHash -SumsText '' -Asset 'ams-store-windows-amd64.exe' | Should -BeNullOrEmpty
    }
}

Describe 'Get-AmsFileSha256' {
    It 'is the lowercase hex SHA-256 of the file content (the empty file gives the well-known digest)' {
        $p = Join-Path $TestDrive 'empty.bin'
        [System.IO.File]::WriteAllBytes($p, [byte[]]@())
        Get-AmsFileSha256 -Path $p | Should -Be 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    }
}

Describe 'Set-AmsHubSshConfig (idempotent Match block)' {
    It 'writes the block into an absent config, and a second call changes nothing' {
        $cfg = Join-Path $TestDrive 'ssh1\config'
        Set-AmsHubSshConfig -ConfigPath $cfg -HubHost 'hub.test' -HubUser 'ams-hub' -IdentityFile '~/.ssh/id_ed25519_ams_hub' | Should -BeTrue
        $first = Get-Content -LiteralPath $cfg -Raw
        $first | Should -Match '(?m)^Match host hub\.test user ams-hub$'
        $first | Should -Match '(?m)^    IdentityFile ~/\.ssh/id_ed25519_ams_hub$'
        $first | Should -Match '(?m)^    IdentitiesOnly yes$'
        Set-AmsHubSshConfig -ConfigPath $cfg -HubHost 'hub.test' -HubUser 'ams-hub' -IdentityFile '~/.ssh/id_ed25519_ams_hub' | Should -BeFalse
        (Get-Content -LiteralPath $cfg -Raw) | Should -Be $first
        ([regex]::Matches($first, 'Match host')).Count | Should -Be 1
    }
    It 'replaces a stale block in place and preserves everything around it' {
        $cfg = Join-Path $TestDrive 'ssh2\config'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $cfg) | Out-Null
        $before = "Host other`n    User me`n`n# >>> ams-store hub (managed by 2-windows-config.ps1)`nMatch host old.test user ams-hub`n    IdentityFile ~/.ssh/old`n# <<< ams-store hub`n`nHost after`n    Port 2222`n"
        [System.IO.File]::WriteAllText($cfg, $before)
        Set-AmsHubSshConfig -ConfigPath $cfg -HubHost 'new.test' -HubUser 'ams-hub' -IdentityFile '~/.ssh/id_ed25519_ams_hub' | Should -BeTrue
        $after = Get-Content -LiteralPath $cfg -Raw
        $after | Should -Match '(?m)^Host other$'
        $after | Should -Match '(?m)^Host after$'
        $after | Should -Match '(?m)^    Port 2222$'
        $after | Should -Match '(?m)^Match host new\.test user ams-hub$'
        $after | Should -Not -Match 'old\.test'
        ([regex]::Matches($after, '# >>> ams-store hub')).Count | Should -Be 1
        $after.IndexOf('Host other') | Should -BeLessThan $after.IndexOf('Match host new.test')
        $after.IndexOf('Match host new.test') | Should -BeLessThan $after.IndexOf('Host after')
    }
}

Describe 'Initialize-AmsKnownHosts (the binary''s own known_hosts)' {
    BeforeAll { $script:haveKeygen = [bool](Get-Command ssh-keygen -ErrorAction SilentlyContinue) }
    It 'seeds nothing and reports 0 when the user has never accepted the hub''s host key' {
        if (-not $script:haveKeygen) { Set-ItResult -Skipped -Because 'ssh-keygen is not on PATH'; return }
        $user = Join-Path $TestDrive 'kh1\known_hosts'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $user) | Out-Null
        "other.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGQwZ2h4a2xtbm9wcXJzdHV2d3h5ejAxMjM0NTY3ODkw`n" | Set-Content -LiteralPath $user -NoNewline
        $state = Join-Path $TestDrive 'kh1\state\known_hosts'
        Initialize-AmsKnownHosts -HubHost 'hub.test' -UserKnownHosts $user -StateKnownHosts $state | Should -Be 0
        Test-Path $state | Should -BeFalse
        Initialize-AmsKnownHosts -HubHost 'hub.test' -UserKnownHosts (Join-Path $TestDrive 'kh1\absent') -StateKnownHosts $state | Should -Be 0
    }
    It 'copies exactly the hub''s key lines into the state root' {
        if (-not $script:haveKeygen) { Set-ItResult -Skipped -Because 'ssh-keygen is not on PATH'; return }
        $dir = Join-Path $TestDrive 'kh2'
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        & ssh-keygen -q -t ed25519 -N '' -f (Join-Path $dir 'k') | Out-Null
        $pub = (Get-Content -LiteralPath (Join-Path $dir 'k.pub') -Raw).Trim()
        $user = Join-Path $dir 'known_hosts'
        "other.test $pub`nhub.test $pub`n" | Set-Content -LiteralPath $user -NoNewline
        $state = Join-Path $dir 'state\known_hosts'
        Initialize-AmsKnownHosts -HubHost 'hub.test' -UserKnownHosts $user -StateKnownHosts $state | Should -Be 1
        $lines = @(Get-Content -LiteralPath $state)
        $lines.Count | Should -Be 1
        $lines[0] | Should -Match '^hub\.test ssh-ed25519 '
    }
}

Describe 'Initialize-AmsHistoryRemote (branch main, one remote named hub)' {
    It 'creates a missing history repo on main and adds the hub remote in the user@host:repo form' {
        $root = Join-Path $TestDrive 'h1'; $gd = Join-Path $root 'history.git'; $wt = Join-Path $root 'projects'
        New-Item -ItemType Directory -Force -Path $wt | Out-Null
        $url = Initialize-AmsHistoryRemote -GitDir $gd -WorkTree $wt -HubHost 'hub.test' -HubUser 'ams-hub' -HubRepo 'ams-store.git'
        $url | Should -Be 'ams-hub@hub.test:ams-store.git'
        (git --git-dir $gd --work-tree $wt symbolic-ref --short HEAD) | Should -Be 'main'
        (git --git-dir $gd --work-tree $wt remote get-url hub) | Should -Be 'ams-hub@hub.test:ams-store.git'
        @(git --git-dir $gd --work-tree $wt remote).Count | Should -Be 1
    }
    It 'renames a pre-binary master branch to main, corrects a drifted hub URL and prunes any other remote, keeping the history' {
        $root = Join-Path $TestDrive 'h2'; $gd = Join-Path $root 'history.git'; $wt = Join-Path $root 'projects'
        New-Item -ItemType Directory -Force -Path $wt | Out-Null
        git --git-dir $gd --work-tree $wt init -q -b master
        git --git-dir $gd --work-tree $wt -c user.name=t -c user.email=t@t.test commit -q --allow-empty -m seed
        git --git-dir $gd --work-tree $wt remote add hub 'ams-hub@wrong.test:ams-store.git'
        git --git-dir $gd --work-tree $wt remote add origin 'someone@elsewhere.test:other.git'
        Initialize-AmsHistoryRemote -GitDir $gd -WorkTree $wt -HubHost 'hub.test' -HubUser 'ams-hub' -HubRepo 'ams-store.git' | Out-Null
        (git --git-dir $gd --work-tree $wt symbolic-ref --short HEAD) | Should -Be 'main'
        (git --git-dir $gd --work-tree $wt remote get-url hub) | Should -Be 'ams-hub@hub.test:ams-store.git'
        @(git --git-dir $gd --work-tree $wt remote) | Should -Be @('hub') -Because 'the remote policy allows exactly one remote, named hub; every other one makes sync refuse'
        (git --git-dir $gd --work-tree $wt log --oneline).Count | Should -Be 1 -Because 'the rename keeps the history'
    }
}

Describe 'Test-GitVersionAtLeast (0-prereqs: git >= 2.38 for merge-tree --write-tree)' {
    It 'accepts 2.55.0.windows.2 and 2.38.0, refuses 2.37.1 and garbage' {
        Test-GitVersionAtLeast -VersionText 'git version 2.55.0.windows.2' -Major 2 -Minor 38 | Should -BeTrue
        Test-GitVersionAtLeast -VersionText 'git version 2.38.0' -Major 2 -Minor 38 | Should -BeTrue
        Test-GitVersionAtLeast -VersionText 'git version 2.37.1' -Major 2 -Minor 38 | Should -BeFalse
        Test-GitVersionAtLeast -VersionText '' -Major 2 -Minor 38 | Should -BeFalse
        Test-GitVersionAtLeast -VersionText 'git: command not found' -Major 2 -Minor 38 | Should -BeFalse
    }
}

Describe 'Q-F: the PowerShell receipt reader tolerates the Go binary''s receipt rows' {
    # During the overlap both writers append to compact-receipts.jsonl. The Go row is a superset
    # of the PowerShell row (harvested, migrated_stamped, over_inject_limit, protected_set_overflow,
    # unconverged, changed; mem0 as an array; commit/snapshot as null). The PS lint's per-store
    # history must read straight through it, and a dry_run row must stay invisible.
    It 'Get-AmStoreRunHistory reads a Go row as the newest outcome without throwing' {
        . (Join-Path $script:winDir 'memory-store-lib.ps1')
        $p = Join-Path $TestDrive 'mixed.jsonl'
        @(
            '{"ts":"2026-09-14T05:00:00.0000000Z","tier":"opus","origin_slug":"x","workspace":"ws","status":"applied","after_bytes":100,"after_lines":5,"migrated":1,"floored":0,"line_floored":0,"mem0_orphan":[],"mem0":[],"note":"","commit":"abc","shortened":0,"reindexed":0,"dedangled":0,"dedup_slug":0,"diff":null,"judge_called":true}'
            '{"ts":"2026-09-15T17:32:46.6866291Z","workspace":"ws","dry_run":false,"before_bytes":387,"before_lines":5,"status":"no-op","shortened":0,"migrated":0,"reindexed":0,"dedangled":0,"dedup_slug":0,"floored":0,"line_floored":0,"mem0":[],"mem0_orphan":[],"after_bytes":387,"after_lines":5,"commit":null,"snapshot":null,"note":"","skip_streak":0,"liveness_override":false,"judge_called":false,"harvested":3,"migrated_stamped":0,"over_inject_limit":0,"protected_set_overflow":false,"unconverged":false,"changed":false}'
            '{"ts":"2026-09-16T01:00:00.0000000Z","workspace":"ws","dry_run":true,"status":"dry-run","harvested":0}'
        ) | Set-Content -LiteralPath $p
        $h = Get-AmStoreRunHistory -ReceiptPath $p -Workspace 'ws'
        $h.LastStatus | Should -Be 'no-op' -Because 'the dry_run row is skipped and the Go no-op row is the newest real outcome'
        $h.SkipStreak | Should -Be 0
        $h.LastJudgeUtc | Should -Be ([DateTime]::Parse('2026-09-14T05:00:00.0000000Z', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal))
    }
}

Describe 'store hub path predicate (Get-AmHubPathGaps) - one answer for the installer and the self-test' {
    BeforeAll {
        $script:haveKeygen = [bool](Get-Command ssh-keygen -ErrorAction SilentlyContinue)
        function script:New-HubSshDir {
            # An ssh dir holding (optionally) the hub identity and a known_hosts that (optionally)
            # carries a real key line for the hub host.
            param([string]$Name, [switch]$Identity, [switch]$HostKey)
            $dir = Join-Path $TestDrive $Name
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            & ssh-keygen -q -t ed25519 -N '' -f (Join-Path $dir 'k') | Out-Null
            $pub = (Get-Content -LiteralPath (Join-Path $dir 'k.pub') -Raw).Trim()
            if ($Identity) { Copy-Item -LiteralPath (Join-Path $dir 'k') -Destination (Join-Path $dir 'id_ed25519_ams_hub') }
            $kh = "other.test $pub`n"
            if ($HostKey) { $kh += "hub.test $pub`n" }
            [System.IO.File]::WriteAllText((Join-Path $dir 'known_hosts'), $kh)
            return $dir
        }
    }
    It 'no hub host is a gap on its own (the installer keeps the legacy nightly)' {
        @(Get-AmHubPathGaps -HubHost '' -SshDir (Join-Path $TestDrive 'nothing')).Count | Should -Be 1
        (Get-AmHubPathGaps -HubHost '' -SshDir (Join-Path $TestDrive 'nothing')) | Should -Match 'HubHost'
    }
    It 'is proven only with the identity key AND the hub host key in the user known_hosts' {
        if (-not $script:haveKeygen) { Set-ItResult -Skipped -Because 'ssh-keygen is not on PATH'; return }
        @(Get-AmHubPathGaps -HubHost 'hub.test' -SshDir (script:New-HubSshDir -Name 'hp-ok' -Identity -HostKey)).Count | Should -Be 0
        $noId = @(Get-AmHubPathGaps -HubHost 'hub.test' -SshDir (script:New-HubSshDir -Name 'hp-noid' -HostKey))
        $noId.Count | Should -Be 1
        $noId[0] | Should -Match 'identity key'
        $noKh = @(Get-AmHubPathGaps -HubHost 'hub.test' -SshDir (script:New-HubSshDir -Name 'hp-nokh' -Identity))
        $noKh.Count | Should -Be 1
        $noKh[0] | Should -Match 'host key'
        @(Get-AmHubPathGaps -HubHost 'hub.test' -SshDir (script:New-HubSshDir -Name 'hp-none')).Count | Should -Be 2
    }
    It 'Get-AmHubHostKeyLines returns only the hub host''s lines (the count the installer seeds)' {
        if (-not $script:haveKeygen) { Set-ItResult -Skipped -Because 'ssh-keygen is not on PATH'; return }
        $d = script:New-HubSshDir -Name 'hp-lines' -Identity -HostKey
        @(Get-AmHubHostKeyLines -HubHost 'hub.test' -UserKnownHosts (Join-Path $d 'known_hosts')).Count | Should -Be 1
        @(Get-AmHubHostKeyLines -HubHost 'absent.test' -UserKnownHosts (Join-Path $d 'known_hosts')).Count | Should -Be 0
        @(Get-AmHubHostKeyLines -HubHost 'hub.test' -UserKnownHosts (Join-Path $d 'no-such-file')).Count | Should -Be 0
    }
}

Describe 'compactor task verdict (Get-AmCompactorTaskVerdict) mirrors installer step 1d' {
    It 'absent + hub proven = OK: retired on purpose (the 2026-09-22 false FAIL on a replica)' {
        $v = Get-AmCompactorTaskVerdict -Present $false -HubGaps @()
        $v.Status | Should -Be 'OK'
        $v.Detail | Should -Match 'retired'
    }
    It 'absent + hub NOT proven = FAIL, naming what is missing' {
        $v = Get-AmCompactorTaskVerdict -Present $false -HubGaps @('identity key X is absent')
        $v.Status | Should -Be 'FAIL'
        $v.Detail | Should -Match 'identity key X is absent'
    }
    It 'present = the action-shape checks, whatever the hub state' {
        (Get-AmCompactorTaskVerdict -Present $true -TaskArgs '-File C:\Stack\scripts\memory-compact.ps1' -TaskState 'Ready' -HubGaps @('g')).Status | Should -Be 'OK'
        (Get-AmCompactorTaskVerdict -Present $true -TaskArgs '-File D:\Dev\repo\scripts\windows\memory-compact.ps1' -HubGaps @()).Status | Should -Be 'FAIL'
        (Get-AmCompactorTaskVerdict -Present $true -TaskArgs '-File C:\x\other.ps1' -HubGaps @()).Status | Should -Be 'WARN'
    }
}

Describe 'the binary swap stops this store''s ams-store.exe first (1.31.3 mixed-version window)' {
    BeforeAll {
        $script:exe  = 'C:\Profiles\op\.claude\scripts\ams-store.exe'
        $script:root = 'C:\Profiles\op\.claude\state\automemory'
        $script:fake = @(
            [pscustomobject]@{ ProcessId = 11; ExecutablePath = ($script:exe + '.prev'); CommandLine = '"C:\Profiles\op\.claude\scripts\ams-store.exe" sync --watch --hub-host hub'; Owner = 'PC\op'; StartUnix = 1011 }
            [pscustomobject]@{ ProcessId = 12; ExecutablePath = $script:exe; CommandLine = 'ams-store.exe sync --once --hub-host hub'; Owner = 'PC\op'; StartUnix = 1012 }
            [pscustomobject]@{ ProcessId = 13; ExecutablePath = $script:exe; CommandLine = 'ams-store.exe sync --watch --state-root "C:\Temp\scratch\state"'; Owner = 'PC\op'; StartUnix = 1013 }
            [pscustomobject]@{ ProcessId = 14; ExecutablePath = 'C:\Profiles\other\.claude\scripts\ams-store.exe'; CommandLine = 'ams-store.exe sync --watch'; Owner = 'PC\other'; StartUnix = 1014 }
            [pscustomobject]@{ ProcessId = 15; ExecutablePath = $script:exe; CommandLine = 'ams-store.exe sync --watch'; Owner = 'PC\other'; StartUnix = 1015 }
            [pscustomobject]@{ ProcessId = 16; ExecutablePath = 'C:\Temp\go-build1\lock.test.exe'; CommandLine = 'lock.test.exe'; Owner = 'PC\op'; StartUnix = 1016 }
            [pscustomobject]@{ ProcessId = 17; ExecutablePath = $script:exe; CommandLine = ('ams-store.exe gate --state-root=' + $script:root); Owner = 'PC\op'; StartUnix = 1017 }
            # no image path from WMI: argv[0] identifies it
            [pscustomobject]@{ ProcessId = 18; ExecutablePath = ''; CommandLine = ('"' + $script:exe + '" derive'); Owner = 'PC\op'; StartUnix = 1018 }
            # neither an image path nor a command line: seen, not identifiable
            [pscustomobject]@{ ProcessId = 19; ExecutablePath = ''; CommandLine = ''; Owner = 'PC\op'; StartUnix = 1019 }
        )
        function script:Pick { Select-AmsStoreProcessesForStore -Processes $script:fake -ImagePaths @($script:exe, ($script:exe + '.prev')) -StateRoot $script:root -DefaultStateRoot $script:root -Owner 'PC\op' }
    }
    It 'selects only this user''s image serving this state root, falls back to argv[0], and reports the unidentifiable' {
        $pick = script:Pick
        @($pick.Selected.ProcessId) | Should -Be @(11, 12, 17, 18)
        ($pick.Selected | Where-Object ProcessId -eq 11).Kind | Should -Be 'watch'
        ($pick.Selected | Where-Object ProcessId -eq 12).Kind | Should -Be 'pass'
        @($pick.Unidentified) | Should -Be @(19)
    }
    It 'asks the watcher to stop, lets a pass finish, tree-kills a pass that does not, and logs each' {
        $pick = script:Pick
        $script:kills = @(); $script:polls = 0; $script:stopReq = $null
        $r = Stop-AmsStoreProcessesForStore -Selected $pick.Selected -StateRoot $script:root -WatcherGraceSeconds 1 -PassWaitSeconds 1 `
            -HasExited { param($id) if ($id -eq 11) { return ($null -ne $script:stopReq) }; if ($id -eq 12) { $script:polls++; return ($script:polls -gt 2) }; return ($script:kills -contains $id) } `
            -GetStartUnix { param($id) [int64](1000 + $id) } `
            -KillTree { param($id) $script:kills += $id } -RequestWatcherStop { param($root) $script:stopReq = $root } -ClearWatcherStop { param($root) } -Sleep { param($ms) }
        $script:stopReq | Should -Be $script:root -Because 'the watcher is asked to stop through watch.stop first'
        $script:kills | Should -Be @(17, 18) -Because 'the watcher stopped cooperatively and pass 12 finished; 17 and 18 did not'
        ($r.Results | Where-Object ProcessId -eq 11).Action | Should -Be 'exited'
        ($r.Results | Where-Object ProcessId -eq 12).Action | Should -Be 'exited'
        $r.Forced | Should -BeTrue
    }
    It 'never kills a pid whose start time no longer matches the snapshot (a recycled pid), nor one it cannot verify' {
        $pick = script:Pick
        $script:kills = @()
        $r = Stop-AmsStoreProcessesForStore -Selected @($pick.Selected | Where-Object ProcessId -in 12, 17) -StateRoot $script:root -PassWaitSeconds 0 `
            -HasExited { param($id) $false } -GetStartUnix { param($id) if ($id -eq 12) { [int64]999 } else { [int64]0 } } `
            -KillTree { param($id) $script:kills += $id } -Sleep { param($ms) }
        @($script:kills).Count | Should -Be 0
        @($r.Results.Action | Select-Object -Unique) | Should -Be @('not-killed-identity')
        $r.Forced | Should -BeFalse
    }
    It 'the default kill is a TREE kill (taskkill /T /F), so a git child mid-commit is not orphaned' {
        $src = Get-Content -LiteralPath $script:installer -Raw
        $src | Should -Match "taskkill\.exe'\) /PID \`$id /T /F"
        $src | Should -Not -Match 'Stop-Process -Id \$id -Force'
    }
    It 'Install-AmsStoreBinary runs the stop step BEFORE the old image is renamed, and not at all when nothing changes' {
        $d = Join-Path $TestDrive 'swap'; New-Item -ItemType Directory -Force -Path $d | Out-Null
        $dest = Join-Path $d 'ams-store.exe'
        [System.IO.File]::WriteAllText($dest, 'old image')
        $oldHash = Get-AmsFileSha256 $dest
        $drop = Join-Path $d 'drop.exe'
        Copy-Item -LiteralPath (Join-Path $env:SystemRoot 'System32\where.exe') -Destination $drop
        $script:seen = $null
        try {
            Install-AmsStoreBinary -Tag 'v0.0.0' -Asset 'ams-store-windows-amd64.exe' -Dest $dest -ReleaseRepo 'x/y' -BinaryPath $drop -BeforeSwap { $script:seen = Get-AmsFileSha256 $dest } 6>$null | Out-Null
        } catch {}   # where.exe does not answer --version; the swap has already happened
        $script:seen | Should -Be $oldHash -Because 'the running store processes must be stopped while the old image is still in place'
        (Get-AmsFileSha256 $dest) | Should -Be (Get-AmsFileSha256 $drop)
        $script:seen = 'not called'
        try { Install-AmsStoreBinary -Tag 'v0.0.0' -Asset 'a' -Dest $dest -ReleaseRepo 'x/y' -BinaryPath $drop -BeforeSwap { $script:seen = 'called' } 6>$null | Out-Null } catch {}
        $script:seen | Should -Be 'not called' -Because 'an unchanged binary is not swapped, so nothing is stopped'
    }
    It 'Confirm-AmsWatcherAlive tells alive, a quiet exit and a refusal apart' {
        $alive = [pscustomobject]@{ Id = 7; ExitCode = $null } | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $false } -PassThru
        (Confirm-AmsWatcherAlive -Process $alive -WaitSeconds 0).Alive | Should -BeTrue
        $quiet = [pscustomobject]@{ Id = 8; ExitCode = 0 } | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $true } -PassThru
        (Confirm-AmsWatcherAlive -Process $quiet -WaitSeconds 0).Message | Should -Match 'code 0 \(no live session'
        $log = Join-Path $TestDrive 'watch-refused.log'
        Set-Content -LiteralPath $log -Value @('old line', '2026-09-23T08:00:00Z ams-store sync --watch REFUSED: an older watcher')
        $refused = [pscustomobject]@{ Id = 9; ExitCode = 3 } | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $true } -PassThru
        $m = (Confirm-AmsWatcherAlive -Process $refused -RefusedLog $log -WaitSeconds 0).Message
        $m | Should -Match 'EXITED with code 3'
        $m | Should -Match 'REFUSED: an older watcher'
        $m | Should -Not -Match 'restarted'
    }
    It 'the installer wires the stop step, the unidentified line, the lock recovery and the liveness check' {
        $src = Get-Content -LiteralPath $script:installer -Raw
        $src | Should -Match 'Install-AmsStoreBinary -Tag \$amsStoreTag .*-BeforeSwap \$amsStopForSwap'
        $src | Should -Match 'Select-AmsStoreProcessesForStore -Processes \(Get-AmsStoreProcesses\)'
        $src | Should -Match 'Stop-AmsStoreProcessesForStore -Selected \$pick\.Selected -StateRoot \$amsSr'
        $src | Should -Match 'process\(es\) seen but could not be identified'
        $src | Should -Match "if \(\`$stop\.Forced\) \{"
        $src | Should -Match 'Invoke-AmGitLockRecovery -StateRoot \$amsSr -GitProcesses \(Get-AmGitProcesses\)'
        $src | Should -Match 'Confirm-AmsWatcherAlive -Process \$wp'
        $src | Should -Not -Match 'watcher restarted from the new image \(pid \$\(\$wp\.Id\)\)"' -Because 'a respawn is confirmed alive, never claimed'
    }
}
