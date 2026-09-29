# WikiIndexCatchup.Tests.ps1 - WP-8 task 8.2: a replica PC refreshes the wiki index itself when
# it is due (wiki-index-catchup.ps1, spawned by memory-maintenance-spawn.ps1 at SessionStart).
#
# The decision runs in-process against a sandboxed USERPROFILE: HTTP is mocked (Invoke-RestMethod),
# vault page times and the local refresh stamp are real files with set LastWriteTime, and the
# launch of the refresh driver is mocked (Start-WikiRefresh), so nothing here touches the live
# authority, the live vault or WSL.
#
# Matrix: role gate; freshness age > 20 h; a vault page newer than the refresh stamp; neither;
# the boundary (exactly 20 h); an authority that does not report `wiki` yet; an unreachable
# authority; the 6 h attempt throttle; no vault configured; the vault drive letter moving; the
# launch contract; and the wiring (spawn list, installer list).

BeforeAll {
    $script:winDir  = Split-Path -Parent $PSScriptRoot
    $script:catchup = Join-Path $script:winDir 'wiki-index-catchup.ps1'

    function New-WikiSandbox {
        param([string]$Role = 'replica', [switch]$NoVault)
        $sb = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        foreach ($d in @('.claude\state', '.claude\logs', '.claude\scripts', '.mem0', 'vault\wiki\entities')) {
            New-Item -ItemType Directory -Path (Join-Path $sb $d) -Force | Out-Null
        }
        if ($Role) { Set-Content -Path (Join-Path $sb '.mem0\role') -Value $Role -Encoding ASCII -NoNewline }
        if (-not $NoVault) { Set-Content -Path (Join-Path $sb '.mem0\wiki-vault') -Value (Join-Path $sb 'vault') -Encoding ASCII }
        Set-Content -Path (Join-Path $sb 'vault\wiki\entities\Page.md') -Value "# Page`n`ntext" -Encoding UTF8
        [pscustomobject]@{
            Root  = $sb
            Vault = (Join-Path $sb 'vault')
            Page  = (Join-Path $sb 'vault\wiki\entities\Page.md')
            Stamp = (Join-Path $sb '.claude\state\last-wiki-refresh')
            State = (Join-Path $sb '.claude\state')
            Scripts = (Join-Path $sb '.claude\scripts')
        }
    }

    # A refresh stamp whose time is `AgeMinutes` old, and the vault page relative to it.
    function Set-FileAge {
        param([string]$Path, [double]$AgeMinutes)
        if (-not (Test-Path -LiteralPath $Path)) { Set-Content -Path $Path -Value '1' -Encoding ASCII }
        (Get-Item -LiteralPath $Path).LastWriteTimeUtc = [datetime]::UtcNow.AddMinutes(-$AgeMinutes)
    }

    function Enter-Sandbox {
        param($Sb)
        $script:savedProfile = $env:USERPROFILE
        $script:savedVault = $env:WIKI_VAULT
        $env:USERPROFILE = $Sb.Root
        $env:WIKI_VAULT = $null
    }
}

Describe 'wiki-index catch-up: when a replica refreshes' {
    AfterEach {
        if ($null -ne $script:savedProfile) { $env:USERPROFILE = $script:savedProfile }
        $env:WIKI_VAULT = $script:savedVault
    }
    BeforeEach {
        $script:sb = New-WikiSandbox
        Enter-Sandbox $script:sb
        . $script:catchup -DefineOnly
        Mock Start-WikiRefresh { $true }
        # the page is OLDER than the refresh stamp unless a test says otherwise
        Set-FileAge $script:sb.Page 600
        Set-FileAge $script:sb.Stamp 60
    }

    It 'refreshes when the index freshness is older than 20 h' {
        Mock Invoke-RestMethod { [pscustomobject]@{ ok = $true; wiki = [pscustomobject]@{ fresh_age_h = 30.5 } } }
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 1 -Exactly
    }

    It 'does nothing at exactly 20 h with no newer page (the rule is strictly greater)' {
        Mock Invoke-RestMethod { [pscustomobject]@{ wiki = [pscustomobject]@{ fresh_age_h = 20 } } }
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 0 -Exactly
    }

    It 'does nothing when the index is fresh and no page is newer than the refresh stamp' {
        Mock Invoke-RestMethod { [pscustomobject]@{ wiki = [pscustomobject]@{ fresh_age_h = 3 } } }
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 0 -Exactly
        Test-Path (Join-Path $script:sb.State 'last-wiki-catchup') | Should -BeFalse -Because 'a check that launched nothing must not burn the 6 h window'
    }

    It 'refreshes when a vault page is newer than the refresh stamp, however fresh the index reads' {
        Set-FileAge $script:sb.Page 5      # edited 5 min ago; the stamp is 60 min old
        Mock Invoke-RestMethod { [pscustomobject]@{ wiki = [pscustomobject]@{ fresh_age_h = 0.2 } } }
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 1 -Exactly
    }

    It 'refreshes for a newer page when the authority does not report wiki freshness yet' {
        Set-FileAge $script:sb.Page 5
        Mock Invoke-RestMethod { [pscustomobject]@{ ok = $true; stale_steps = @() } }
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 1 -Exactly
    }

    It 'does nothing when the authority reports no wiki key and no page is newer' {
        Mock Invoke-RestMethod { [pscustomobject]@{ ok = $true } }
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 0 -Exactly
    }

    It 'treats a missing refresh stamp as older than every page' {
        Remove-Item $script:sb.Stamp -Force
        Mock Invoke-RestMethod { [pscustomobject]@{ wiki = [pscustomobject]@{ fresh_age_h = 1 } } }
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 1 -Exactly
    }

    It 'does nothing and keeps the window open when the authority is unreachable' {
        Set-FileAge $script:sb.Page 5
        Mock Invoke-RestMethod { throw 'connection refused' }
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 0 -Exactly
        Test-Path (Join-Path $script:sb.State 'last-wiki-catchup') | Should -BeFalse
    }

    It 'a second due check inside 6 h is throttled out' {
        Mock Invoke-RestMethod { [pscustomobject]@{ wiki = [pscustomobject]@{ fresh_age_h = 30 } } }
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 1 -Exactly
        Test-Path (Join-Path $script:sb.State 'last-wiki-catchup') | Should -BeTrue
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 1 -Exactly -Because 'the 6 h throttle stops a burst of session starts'
        # ... and it reopens after 6 h
        Set-FileAge (Join-Path $script:sb.State 'last-wiki-catchup') 0
        $old = [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - 7 * 3600
        Set-Content -Path (Join-Path $script:sb.State 'last-wiki-catchup') -Value $old -Encoding ASCII -NoNewline
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 2 -Exactly
    }

    It 'a launch that could not start does not burn the window' {
        Mock Invoke-RestMethod { [pscustomobject]@{ wiki = [pscustomobject]@{ fresh_age_h = 30 } } }
        Mock Start-WikiRefresh { $false }
        Invoke-WikiCatchup -ScriptDir $script:sb.Scripts
        Test-Path (Join-Path $script:sb.State 'last-wiki-catchup') | Should -BeFalse
    }
}

Describe 'wiki-index catch-up: gates' {
    AfterEach {
        if ($null -ne $script:savedProfile) { $env:USERPROFILE = $script:savedProfile }
        $env:WIKI_VAULT = $script:savedVault
    }
    It 'does nothing on the brain role (its own nightly chain owns the index)' {
        $sb = New-WikiSandbox -Role 'brain'
        Enter-Sandbox $sb
        . $script:catchup -DefineOnly
        Mock Start-WikiRefresh { $true }
        Mock Invoke-RestMethod { [pscustomobject]@{ wiki = [pscustomobject]@{ fresh_age_h = 99 } } }
        Invoke-WikiCatchup -ScriptDir $sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 0 -Exactly
        Should -Invoke Invoke-RestMethod -Times 0 -Exactly
    }

    It 'does nothing, and makes no HTTP call, when no vault is configured' {
        $sb = New-WikiSandbox -NoVault
        Enter-Sandbox $sb
        . $script:catchup -DefineOnly
        Mock Start-WikiRefresh { $true }
        Mock Invoke-RestMethod { [pscustomobject]@{ wiki = [pscustomobject]@{ fresh_age_h = 99 } } }
        Invoke-WikiCatchup -ScriptDir $sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 0 -Exactly
        Should -Invoke Invoke-RestMethod -Times 0 -Exactly
    }

    It 'the WIKI_VAULT environment variable stands in for the config file' {
        $sb = New-WikiSandbox -NoVault
        Enter-Sandbox $sb
        $env:WIKI_VAULT = $sb.Vault
        . $script:catchup -DefineOnly
        Set-FileAge $sb.Page 600; Set-FileAge $sb.Stamp 60
        Mock Start-WikiRefresh { $true }
        Mock Invoke-RestMethod { [pscustomobject]@{ wiki = [pscustomobject]@{ fresh_age_h = 30 } } }
        Invoke-WikiCatchup -ScriptDir $sb.Scripts
        Should -Invoke Start-WikiRefresh -Times 1 -Exactly
    }
}

Describe 'wiki-index catch-up: the vault drive letter moves' {
    AfterEach {
        if ($null -ne $script:savedProfile) { $env:USERPROFILE = $script:savedProfile }
        $env:WIKI_VAULT = $script:savedVault
    }
    It 'finds the vault on another drive letter when the configured one is gone' {
        $sb = New-WikiSandbox -NoVault
        Enter-Sandbox $sb
        . $script:catchup -DefineOnly
        $real = $sb.Vault                                   # e.g. C:\Users\...\vault
        $stale = 'Q:' + $real.Substring(2)                  # a letter that is not mounted
        Set-Content -Path (Join-Path $sb.Root '.mem0\wiki-vault') -Value $stale -Encoding ASCII
        (Get-WikiVaultDir -Letters @($real.Substring(0, 1))) | Should -Be $real
    }

    It 'returns nothing when no letter has the vault' {
        $sb = New-WikiSandbox -NoVault
        Enter-Sandbox $sb
        . $script:catchup -DefineOnly
        Set-Content -Path (Join-Path $sb.Root '.mem0\wiki-vault') -Value 'Q:\nowhere\vault' -Encoding ASCII
        (Get-WikiVaultDir -Letters @('R', 'S')) | Should -BeNullOrEmpty
    }
}

Describe 'wiki-index catch-up: the launch' {
    AfterEach {
        if ($null -ne $script:savedProfile) { $env:USERPROFILE = $script:savedProfile }
        $env:WIKI_VAULT = $script:savedVault
    }
    BeforeEach {
        $script:sb = New-WikiSandbox
        Enter-Sandbox $script:sb
        . $script:catchup -DefineOnly
    }

    It 'runs the deployed refresh driver under Git Bash with the vault in the environment' {
        Set-Content -Path (Join-Path $script:sb.Scripts 'wiki-index-refresh.sh') -Value '#!/usr/bin/env bash' -Encoding ASCII
        Mock Find-GitBash { 'C:\fake\Git\bin\bash.exe' }
        Mock Start-DetachedProcess {}
        (Start-WikiRefresh -VaultDir $script:sb.Vault -ScriptDir $script:sb.Scripts) | Should -BeTrue
        Should -Invoke Start-DetachedProcess -Times 1 -Exactly -ParameterFilter {
            $FilePath -eq 'C:\fake\Git\bin\bash.exe' -and
            $ArgumentList -match 'wiki-index-refresh\.sh' -and
            $Environment['WIKI_VAULT'] -eq $script:sb.Vault
        }
    }

    It 'reports failure, launching nothing, when the driver is not deployed' {
        Mock Find-GitBash { 'C:\fake\Git\bin\bash.exe' }
        Mock Start-DetachedProcess {}
        (Start-WikiRefresh -VaultDir $script:sb.Vault -ScriptDir $script:sb.Scripts) | Should -BeFalse
        Should -Invoke Start-DetachedProcess -Times 0 -Exactly
    }

    It 'reports failure, launching nothing, when Git Bash is not installed' {
        Set-Content -Path (Join-Path $script:sb.Scripts 'wiki-index-refresh.sh') -Value '#!/usr/bin/env bash' -Encoding ASCII
        Mock Find-GitBash { $null }
        Mock Start-DetachedProcess {}
        (Start-WikiRefresh -VaultDir $script:sb.Vault -ScriptDir $script:sb.Scripts) | Should -BeFalse
        Should -Invoke Start-DetachedProcess -Times 0 -Exactly
    }

    It 'refuses a vault path that would break the command line' {
        Set-Content -Path (Join-Path $script:sb.Scripts 'wiki-index-refresh.sh') -Value '#!/usr/bin/env bash' -Encoding ASCII
        Mock Find-GitBash { 'C:\fake\Git\bin\bash.exe' }
        Mock Start-DetachedProcess {}
        (Start-WikiRefresh -VaultDir 'C:\vault"; calc; "' -ScriptDir $script:sb.Scripts) | Should -BeFalse
        Should -Invoke Start-DetachedProcess -Times 0 -Exactly
    }
}

Describe 'wiki-index catch-up: wiring' {
    It 'the SessionStart spawn detaches the catch-up child' {
        $spawn = Get-Content (Join-Path $script:winDir 'memory-maintenance-spawn.ps1') -Raw
        $spawn | Should -Match "'wiki-index-catchup\.ps1'"
    }

    It 'the installer deploys the child and the refresh driver' {
        $repo = Split-Path -Parent (Split-Path -Parent $script:winDir)
        $inst = Get-Content (Join-Path $repo 'install\2-windows-config.ps1') -Raw
        $inst | Should -Match "'wiki-index-catchup\.ps1'"
        $inst | Should -Match 'claude-config\\wiki-index-refresh\.sh'
    }

    It 'the child is Windows PowerShell 5.1 safe (no ?. ?? or ternary)' {
        $code = Get-Content $script:catchup -Raw
        $code | Should -Not -Match '\?\.'
        $code | Should -Not -Match '\?\?'
        $code | Should -Not -Match '\s\?\s.+\s:\s'
    }
}

# WP-8 fix round 1: the installer must not silently replace an operator's own session refresh script.
Describe 'installer: wiki-index-refresh.sh deploy (Install-AmsWikiRefreshDriver)' {
    BeforeAll {
        $repo = Split-Path -Parent (Split-Path -Parent $script:winDir)
        $script:repoSrc = Join-Path $repo 'claude-config\wiki-index-refresh.sh'
        $tokens = $null; $errs = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'install\2-windows-config.ps1'), [ref]$tokens, [ref]$errs)
        $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Install-AmsWikiRefreshDriver' }, $true)
        $script:fnText = if ($fn) { $fn.Extent.Text } else { $null }
        function New-DeploySandbox {
            $d = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path (Join-Path $d 'scripts'), (Join-Path $d 'home\.mem0') -Force | Out-Null
            [pscustomobject]@{ Dst = (Join-Path $d 'scripts\wiki-index-refresh.sh'); Home = (Join-Path $d 'home') }
        }
        function Invoke-Deploy($sb) {
            . ([scriptblock]::Create($script:fnText))
            $out = Install-AmsWikiRefreshDriver -Src $script:repoSrc -Dst $sb.Dst -HomeDir $sb.Home 6>&1 | Out-String
            $out
        }
    }

    It 'the repo driver carries the ams-managed marker' {
        (Get-Content $script:repoSrc -Raw) | Should -Match '(?m)^# ams-managed: wiki-index-refresh'
    }

    It 'the installer defines the function and calls it' {
        $script:fnText | Should -Not -BeNullOrEmpty
        $repo = Split-Path -Parent (Split-Path -Parent $script:winDir)
        (Get-Content (Join-Path $repo 'install\2-windows-config.ps1') -Raw) | Should -Match 'Install-AmsWikiRefreshDriver\s+-Src'
    }

    It 'a first install copies the driver and says the vault is still to configure' {
        $sb = New-DeploySandbox
        $out = Invoke-Deploy $sb
        (Get-Content $sb.Dst -Raw) | Should -Be (Get-Content $script:repoSrc -Raw)
        $out | Should -Match 'wiki-vault'
    }

    It 'a first install with a vault already configured prints no configure notice' {
        $sb = New-DeploySandbox
        Set-Content -Path (Join-Path $sb.Home '.mem0\wiki-vault') -Value 'X:\vault' -Encoding ASCII
        $out = Invoke-Deploy $sb
        (Test-Path $sb.Dst) | Should -BeTrue
        $out | Should -Not -Match 'NOTICE'
    }

    It 'a re-install refreshes a copy that carries the marker' {
        $sb = New-DeploySandbox
        Set-Content -Path $sb.Dst -Value "#!/usr/bin/env bash`n# ams-managed: wiki-index-refresh`n# old version`n" -Encoding ASCII
        Invoke-Deploy $sb | Out-Null
        (Get-Content $sb.Dst -Raw) | Should -Be (Get-Content $script:repoSrc -Raw)
    }

    It 'an operator-owned script (no marker) is kept byte-for-byte and the notice names the migration' {
        $sb = New-DeploySandbox
        $own = "#!/usr/bin/env bash`n# my own refresh, vault baked in`ntar -C /g/vault -cf - wiki | wsl.exe -e true`n"
        [System.IO.File]::WriteAllText($sb.Dst, $own)
        $before = (Get-FileHash $sb.Dst -Algorithm SHA256).Hash
        $out = Invoke-Deploy $sb
        (Get-FileHash $sb.Dst -Algorithm SHA256).Hash | Should -Be $before
        $out | Should -Match 'NOTICE'
        $out | Should -Match 'kept'
        $out | Should -Match 'wiki-index-refresh\.sh'
    }
}
