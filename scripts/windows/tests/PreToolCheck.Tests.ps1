#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# PreToolCheck.Tests.ps1 — v0.20 Phase A.2: Pester 5 tests for the PreToolUse
# fast path (pre-tool-check.ps1) and the api-key local cache
# (user-prompt-lib.ps1 Get-Mem0ApiKeyCached).
#
# pre-tool-check.ps1 is dot-sourced with $env:PRETOOL_TEST_MODE='1' so it only
# defines functions (Test-CanonicalAssertionCandidate, Save-PreToolFixture,
# Invoke-PreToolCheck) without executing the pipeline or reading stdin.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester C:\path\to\agentic-memory-stack-for-claude-code\scripts\windows\tests\ -Output Detailed"

BeforeAll {
    $scriptsDir = Split-Path -Parent $PSScriptRoot
    $hookPath = Join-Path $scriptsDir 'pre-tool-check.ps1'
    $libPath  = Join-Path $scriptsDir 'user-prompt-lib.ps1'
    if (-not (Test-Path $hookPath)) { throw "pre-tool-check.ps1 not found at $hookPath" }
    if (-not (Test-Path $libPath))  { throw "user-prompt-lib.ps1 not found at $libPath" }
    $env:PRETOOL_TEST_MODE = '1'
    . $hookPath
    . $libPath
}

AfterAll {
    Remove-Item Env:\PRETOOL_TEST_MODE -ErrorAction SilentlyContinue
}

Describe 'v0.20 A.2 pattern gate (Test-CanonicalAssertionCandidate)' {

    It 'matches canonical-assertion candidate "<_>"' -ForEach @(
        'llama-server --port 18791',
        'curl http://127.0.0.1:18791/health',
        'ssh nodeuser@192.0.2.21 sudo ss -tlpn',
        'bind the listener to localhost',
        'this decision was locked as canonical',
        'Postiz is retired and forbidden',
        'never bind port 3000',
        'C:\path\to\x\config.yaml port: 8081'
    ) {
        Test-CanonicalAssertionCandidate -Text $_ | Should -BeTrue
    }

    It 'does NOT match typical non-canonical tool input "<_>"' -ForEach @(
        'git status --short',
        'npm run build',
        'ls -la C:\path\to\some-project\src',
        'Get-ChildItem .\components -Filter *.tsx',
        'sed -n 1,50p README.md'
    ) {
        Test-CanonicalAssertionCandidate -Text $_ | Should -BeFalse
    }

    It 'returns false for empty / whitespace input' {
        Test-CanonicalAssertionCandidate -Text ''   | Should -BeFalse
        Test-CanonicalAssertionCandidate -Text '  ' | Should -BeFalse
        Test-CanonicalAssertionCandidate -Text $null | Should -BeFalse
    }
}

Describe 'v0.20 A.2 PreToolUse fast path (Invoke-PreToolCheck)' {

    BeforeEach {
        # No fixture writes during tests (mock the sampler), no real HTTP.
        Mock Save-PreToolFixture {}
        Mock Invoke-RestMethod { [pscustomobject]@{ results = @() } }
        Mock Get-Mem0ApiKeyCached { 'pester-test-key' }
        # Redirect the warn file away from the real UNC path
        $script:WarnFile = Join-Path $TestDrive 'pre-tool-warnings.jsonl'
    }

    It 'non-matching tool input exits BEFORE any HTTP call (zero Invoke-RestMethod)' {
        $payload = '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"git status --short"},"session_id":"s1"}'
        Invoke-PreToolCheck -StdinRaw $payload
        Should -Invoke Invoke-RestMethod -Times 0 -Exactly
        Should -Invoke Get-Mem0ApiKeyCached -Times 0 -Exactly
    }

    It 'non-matching input never reads the api key (no UNC, no cache)' {
        $payload = '{"hook_event_name":"PreToolUse","tool_name":"Edit","tool_input":{"file_path":"C:\\path\\to\\web\\src\\Button.tsx","old_string":"className=\"btn\""},"session_id":"s1"}'
        Invoke-PreToolCheck -StdinRaw $payload
        Should -Invoke Get-Mem0ApiKeyCached -Times 0 -Exactly
        Should -Invoke Invoke-RestMethod -Times 0 -Exactly
    }

    It 'matching tool input still reaches the HTTP search (exactly one call)' {
        $payload = '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"llama-server --port 18791 bound to 127.0.0.1"},"session_id":"s1"}'
        Invoke-PreToolCheck -StdinRaw $payload
        Should -Invoke Invoke-RestMethod -Times 1 -Exactly
        Should -Invoke Get-Mem0ApiKeyCached -Times 1 -Exactly
    }

    It 'raw-text match on a payload field but non-matching extracted query exits without HTTP (precision re-gate)' {
        # transcript_path contains "port" lookalike via cwd; the Bash command itself is clean
        $payload = '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"git log --oneline"},"session_id":"s1","cwd":"C:\\path\\to\\port-directory-tools"}'
        Invoke-PreToolCheck -StdinRaw $payload
        Should -Invoke Invoke-RestMethod -Times 0 -Exactly
    }

    It 'irrelevant tool (Read) exits without HTTP even when patterns match' {
        $payload = '{"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"file_path":"C:\\path\\to\\port-directory\\your-machine-ports.md"},"session_id":"s1"}'
        Invoke-PreToolCheck -StdinRaw $payload
        Should -Invoke Invoke-RestMethod -Times 0 -Exactly
    }

    It 'canonical hit writes a pre-tool-warnings.jsonl line' {
        Mock Invoke-RestMethod {
            [pscustomobject]@{ results = @(
                [pscustomobject]@{ id = 'm1'; memory = 'llama-swap serves on :11436 (canonical)'; score = 0.91
                                   metadata = [pscustomobject]@{ tier = 'canonical' } }
            ) }
        }
        $payload = '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"llama-server --port 11436"},"session_id":"warn-test"}'
        Invoke-PreToolCheck -StdinRaw $payload
        Should -Invoke Invoke-RestMethod -Times 1 -Exactly
        Test-Path $script:WarnFile | Should -BeTrue
        $line = (Get-Content $script:WarnFile -Raw) | ConvertFrom-Json
        $line.session_id | Should -Be 'warn-test'
        $line.matched_canonical.Count | Should -Be 1
    }

    It 'empty stdin exits silently with no calls' {
        Invoke-PreToolCheck -StdinRaw ''
        Should -Invoke Invoke-RestMethod -Times 0 -Exactly
    }
}

Describe 'v0.20 A.2 api-key local cache (Get-Mem0ApiKeyCached)' {

    BeforeEach {
        $script:uncStub   = Join-Path $TestDrive 'unc-api-key'
        $script:cacheFile = Join-Path $TestDrive ("api-key-{0}.cache" -f ([guid]::NewGuid().ToString('N')))
        Set-Content -Path $script:uncStub -Value 'key-from-unc' -NoNewline
    }

    It 'uses a FRESH cache without touching the UNC path' {
        Set-Content -Path $script:cacheFile -Value 'key-from-cache' -NoNewline
        (Get-Item $script:cacheFile).LastWriteTime = Get-Date
        # UNC path deliberately nonexistent: a fresh cache must be enough
        $key = Get-Mem0ApiKeyCached -UncPath (Join-Path $TestDrive 'no-such-unc') -CachePath $script:cacheFile
        $key | Should -Be 'key-from-cache'
    }

    It 'refreshes a STALE cache from the UNC original' {
        Set-Content -Path $script:cacheFile -Value 'stale-old-key' -NoNewline
        (Get-Item $script:cacheFile).LastWriteTime = (Get-Date).AddHours(-2)
        $key = Get-Mem0ApiKeyCached -UncPath $script:uncStub -CachePath $script:cacheFile
        $key | Should -Be 'key-from-unc'
        # Cache file rewritten with the fresh key + fresh mtime
        ([System.IO.File]::ReadAllText($script:cacheFile)).Trim() | Should -Be 'key-from-unc'
        ((Get-Date) - (Get-Item $script:cacheFile).LastWriteTime).TotalMinutes | Should -BeLessThan 1
    }

    It 'populates the cache on first use (cache missing -> UNC read + write-through)' {
        $key = Get-Mem0ApiKeyCached -UncPath $script:uncStub -CachePath $script:cacheFile
        $key | Should -Be 'key-from-unc'
        Test-Path $script:cacheFile | Should -BeTrue
    }

    It 'falls back to a STALE cache when the UNC path is unreadable (WSL asleep)' {
        Set-Content -Path $script:cacheFile -Value 'stale-but-usable' -NoNewline
        (Get-Item $script:cacheFile).LastWriteTime = (Get-Date).AddHours(-5)
        $key = Get-Mem0ApiKeyCached -UncPath (Join-Path $TestDrive 'no-such-unc') -CachePath $script:cacheFile
        $key | Should -Be 'stale-but-usable'
    }

    It 'returns $null when neither cache nor UNC is available' {
        $key = Get-Mem0ApiKeyCached -UncPath (Join-Path $TestDrive 'no-such-unc') -CachePath $script:cacheFile
        $key | Should -BeNullOrEmpty
    }

    It 'ignores an empty cache file and reads the UNC original' {
        Set-Content -Path $script:cacheFile -Value '' -NoNewline
        (Get-Item $script:cacheFile).LastWriteTime = Get-Date
        $key = Get-Mem0ApiKeyCached -UncPath $script:uncStub -CachePath $script:cacheFile
        $key | Should -Be 'key-from-unc'
    }

    # v0.21 L2: fail-closed ACL + bounded stale fallback
    It 'after a refresh the cache ACL is owner-only protected (one ACE for current user)' {
        $key = Get-Mem0ApiKeyCached -UncPath $script:uncStub -CachePath $script:cacheFile
        $key | Should -Be 'key-from-unc'
        $acl = Get-Acl -LiteralPath $script:cacheFile
        $acl.AreAccessRulesProtected | Should -BeTrue
        $rules = @($acl.Access)
        $rules.Count | Should -Be 1
        $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $rules[0].IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value | Should -Be $me.Value
    }

    It 'returns $null (does NOT serve) when the cache is older than MaxStaleFallbackHours and UNC is unreachable' {
        Set-Content -Path $script:cacheFile -Value 'too-stale-key' -NoNewline
        (Get-Item $script:cacheFile).LastWriteTime = (Get-Date).AddHours(-25)
        $key = Get-Mem0ApiKeyCached -UncPath (Join-Path $TestDrive 'no-such-unc') -CachePath $script:cacheFile -MaxStaleFallbackHours 24
        $key | Should -BeNullOrEmpty
    }

    # v0.21 review fix-pass: the atomic temp-file + protected-ACL + rename refresh
    # must keep the cache owner-only protected even under a concurrent same-user
    # refresh race (the old in-place WriteAllText('')->ACL->WriteAllText($key)
    # trio could, under a sharing-violation-induced Delete race, leave the secret
    # on a fresh inode with inherited ACLs). Two real processes hammer the SAME
    # CachePath; afterwards the live cache must still be protected with one ACE.
    It 'keeps the cache owner-only protected after a CONCURRENT same-user refresh race' {
        $libForJob = $libPath
        $unc       = $script:uncStub
        $cache     = $script:cacheFile
        # Each iteration: tiny jitter, then refresh against the shared CachePath,
        # repeated so the create/rename windows of the two processes overlap.
        $sb = {
            param($lib, $uncPath, $cachePath, $seed)
            . $lib
            $rng = [System.Random]::new($seed)
            for ($i = 0; $i -lt 40; $i++) {
                # force a refresh each loop by ageing the cache past MaxAgeMinutes
                try { if ([System.IO.File]::Exists($cachePath)) { [System.IO.File]::SetLastWriteTime($cachePath, [System.DateTime]::Now.AddHours(-2)) } } catch {}
                [void](Get-Mem0ApiKeyCached -UncPath $uncPath -CachePath $cachePath -MaxAgeMinutes 60)
                [System.Threading.Thread]::Sleep($rng.Next(0, 3))
            }
        }
        $jA = Start-Job -ScriptBlock $sb -ArgumentList $libForJob, $unc, $cache, 1
        $jB = Start-Job -ScriptBlock $sb -ArgumentList $libForJob, $unc, $cache, 2
        $null = Wait-Job -Job $jA, $jB -Timeout 120
        Receive-Job -Job $jA, $jB -ErrorAction SilentlyContinue | Out-Null
        Remove-Job -Job $jA, $jB -Force

        # The live cache survives the race with the correct content and a
        # protected, single-ACE, owner-only DACL — never an inherited-ACL inode.
        [System.IO.File]::Exists($cache) | Should -BeTrue
        ([System.IO.File]::ReadAllText($cache)).Trim() | Should -Be 'key-from-unc'
        $acl = Get-Acl -LiteralPath $cache
        $acl.AreAccessRulesProtected | Should -BeTrue
        $rules = @($acl.Access)
        $rules.Count | Should -Be 1
        $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $rules[0].IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value | Should -Be $me.Value
        # no orphaned temp files left behind by the race
        @(Get-ChildItem -Path (Split-Path -Parent $cache) -Filter ((Split-Path -Leaf $cache) + '.*.tmp') -ErrorAction SilentlyContinue).Count | Should -Be 0
    }

    # v0.22 review L7: the fresh-read path must NOT serve a cache whose ACL is not
    # owner-only-protected (a pre-v0.21 file written secret-first with inherited
    # ACEs). It must fall through to refresh and re-secure the file ACL-first.
    It 're-secures a fresh cache that has a non-protected (inherited-ACE) ACL on the read path' {
        # Seed a FRESH (within MaxAgeMinutes) cache file with a deliberately weak ACL:
        # inheritance ON + a second ACE for a well-known group.
        Set-Content -Path $script:cacheFile -Value 'pre-v021-weak-acl-key' -NoNewline
        (Get-Item $script:cacheFile).LastWriteTime = Get-Date
        $weak = Get-Acl -LiteralPath $script:cacheFile
        $weak.SetAccessRuleProtection($false, $true)   # inheritance ON (not protected)
        $users = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-545')  # BUILTIN\Users
        $weak.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($users, 'Read', 'Allow')))
        Set-Acl -LiteralPath $script:cacheFile -AclObject $weak
        (Get-Acl -LiteralPath $script:cacheFile).AreAccessRulesProtected | Should -BeFalse  # precondition

        # The function must NOT trust the weak cache; it re-reads the UNC + rewrites.
        $key = Get-Mem0ApiKeyCached -UncPath $script:uncStub -CachePath $script:cacheFile
        $key | Should -Be 'key-from-unc'   # served the authoritative UNC value, not the weak cache
        $acl = Get-Acl -LiteralPath $script:cacheFile
        $acl.AreAccessRulesProtected | Should -BeTrue
        $rules = @($acl.Access)
        $rules.Count | Should -Be 1
        $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $rules[0].IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value | Should -Be $me.Value
    }

    # v0.22 review L10: the PS7 (.NET Core) Pester run exercises ONLY the
    # [FileSystemAclExtensions] else-branch of the ACL selector. The PRODUCTION
    # hook runtime is PS5.1 / .NET Framework, which takes the
    # [System.IO.File]::SetAccessControl if-branch — never run by the pwsh suite.
    # This leg drives the real Framework branch by spawning Windows PowerShell
    # 5.1 to run the actual Get-Mem0ApiKeyCached refresh, then asserts the cache
    # lands owner-only-protected. Skips cleanly where powershell.exe is absent.
    It 'applies an owner-only protected ACL on the PRODUCTION PS5.1/.NET-Framework branch' -Skip:(-not (Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'))) {
        $ps51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $cache51 = Join-Path $TestDrive ("ps51-{0}.cache" -f ([guid]::NewGuid().ToString('N')))
        # Run the REAL function under PS5.1; emit a single PROTECTED:<bool>|COUNT:<n>|SID:<sid> line.
        $inner = @"
. '$libPath'
`$null = Get-Mem0ApiKeyCached -UncPath '$($script:uncStub)' -CachePath '$cache51'
`$acl = Get-Acl -LiteralPath '$cache51'
`$me  = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
`$rules = @(`$acl.Access)
`$sid = if (`$rules.Count -ge 1) { `$rules[0].IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } else { '' }
Write-Output ("PROTECTED:{0}|COUNT:{1}|SID:{2}|ME:{3}" -f `$acl.AreAccessRulesProtected, `$rules.Count, `$sid, `$me.Value)
"@
        $out = & $ps51 -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $inner 2>&1
        $line = ($out | Where-Object { $_ -match '^PROTECTED:' } | Select-Object -First 1)
        $line | Should -Not -BeNullOrEmpty -Because "PS5.1 run produced no result line. Output: $out"
        $line | Should -Match 'PROTECTED:True'
        $line | Should -Match 'COUNT:1'
        $m = [regex]::Match($line, 'SID:([^|]*)\|ME:(.*)$')
        $m.Groups[1].Value | Should -Be $m.Groups[2].Value  # the one ACE is the current user
    }
}
