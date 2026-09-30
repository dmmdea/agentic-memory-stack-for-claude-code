#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# InstallRole.Tests.ps1 - which role an install run applies (install/role-lib.ps1).
#
# install.ps1 declared `-Role` with the default 'brain' and passed it to 2-windows-config.ps1
# unconditionally, and the phase script writes it to the receipt, to both ~/.mem0/role files and
# to the brain gate that registers the nightly dream/dedup tasks. So a plain re-run of
# `.\install.ps1` on a replica turned the replica into a brain. (Phase 1 already keeps a recorded
# role when -Role is omitted; phase 2 did not.) An omitted -Role now keeps the recorded role.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester <repo>/scripts/windows/tests/InstallRole.Tests.ps1 -Output Detailed"

BeforeAll {
    $script:winDir   = Split-Path -Parent $PSScriptRoot
    $script:repoRoot = Split-Path -Parent (Split-Path -Parent $script:winDir)
    $script:libPath  = Join-Path $script:repoRoot 'install\role-lib.ps1'
    $script:orchPath = Join-Path $script:repoRoot 'install.ps1'
    $script:phase2   = Join-Path $script:repoRoot 'install\2-windows-config.ps1'
    if (Test-Path $script:libPath) { . $script:libPath }

    function script:New-Profile {
        # A fake %USERPROFILE%: -RoleFile is the raw text of .mem0\role, -ReceiptRole the Role in the receipt.
        param([string]$RoleFile = $null, [string]$ReceiptRole = $null)
        $p = Join-Path $TestDrive ('p' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path (Join-Path $p '.mem0'), (Join-Path $p '.claude\scripts') | Out-Null
        if ($null -ne $RoleFile) { [System.IO.File]::WriteAllText((Join-Path $p '.mem0\role'), $RoleFile) }
        if ($ReceiptRole) {
            [System.IO.File]::WriteAllText((Join-Path $p '.claude\scripts\mem0-stack.config.psd1'),
                "@{`n    WslUser = 'u'`n    Role        = '$ReceiptRole'`n    AuthorityUrl = 'http://192.0.2.7:18791'`n}`n")
        }
        $p
    }
}

Describe 'Resolve-InstallRole' {
    It 'the library exists and defines the resolver' {
        Test-Path $script:libPath | Should -BeTrue
        Get-Command Resolve-InstallRole -ErrorAction SilentlyContinue | Should -Not -BeNullOrEmpty
    }

    It 'a replica re-run without -Role stays a replica (the role file the installer wrote)' {
        $p = script:New-Profile -RoleFile "replica`n"
        $r = Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p
        $r.Role | Should -Be 'replica'
        $r.Source | Should -Match 'role'
    }

    It 'an explicit -Role wins over the recorded role, both ways' {
        $p = script:New-Profile -RoleFile "replica`n" -ReceiptRole 'replica'
        (Resolve-InstallRole -Explicit $true -Requested 'brain' -ProfileDir $p).Role | Should -Be 'brain'
        $q = script:New-Profile -RoleFile "brain`n" -ReceiptRole 'brain'
        (Resolve-InstallRole -Explicit $true -Requested 'replica' -ProfileDir $q).Role | Should -Be 'replica'
    }

    It 'nothing recorded is the first-install default: brain' {
        $p = script:New-Profile
        $r = Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p
        $r.Role | Should -Be 'brain'
        $r.Source | Should -Match 'default'
    }

    It 'a profile directory that does not exist at all is a first install too' {
        (Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir (Join-Path $TestDrive 'nope')).Role | Should -Be 'brain'
    }

    It 'the receipt stands in when the role file is absent (an install older than the role file)' {
        $p = script:New-Profile -ReceiptRole 'replica'
        $r = Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p
        $r.Role | Should -Be 'replica'
        $r.Source | Should -Match 'receipt'
    }

    It 'the role file is read before the receipt' {
        $p = script:New-Profile -RoleFile "brain`n" -ReceiptRole 'replica'
        (Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p).Role | Should -Be 'brain'
    }

    It 'tolerates the shapes a role file arrives in: CRLF, case, spaces, a BOM' {
        foreach ($raw in @("replica`r`n", "Replica`n", "  REPLICA  `n", ([string][char]0xFEFF + "replica`n"))) {
            $p = script:New-Profile -RoleFile $raw
            # -BeExactly: the role reaches bash tests that compare case-sensitively ([ "$role" = replica ])
            (Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p).Role | Should -BeExactly 'replica' -Because ("raw: " + ($raw -replace "[\r\n]", '~'))
        }
    }

    It 'a role file that holds neither role is ignored, never passed on to the phase script' {
        $p = script:New-Profile -RoleFile "observer`n"
        (Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p -WarningAction SilentlyContinue).Role | Should -Be 'brain'
        $q = script:New-Profile -RoleFile "observer`n" -ReceiptRole 'replica'
        (Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $q -WarningAction SilentlyContinue).Role | Should -Be 'replica'
    }

    It 'an empty role file falls through to the receipt' {
        $p = script:New-Profile -RoleFile '' -ReceiptRole 'replica'
        (Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p).Role | Should -Be 'replica'
    }
}

Describe 'the installers use it' {
    BeforeAll {
        function script:Get-Ast { param([string]$Path)
            $t = $null; $e = $null
            $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$t, [ref]$e)
            if ($e -and $e.Count) { throw "parse errors in ${Path}: $($e[0].Message)" }
            $ast
        }
    }

    It 'the library is plain ASCII, so it parses the same under every host encoding' {
        $bytes = [System.IO.File]::ReadAllBytes($script:libPath)
        @($bytes | Where-Object { $_ -gt 127 }).Count | Should -Be 0
    }

    It 'install.ps1 resolves the role from the library before it prints it or hands it to phase 2' {
        $ast = script:Get-Ast $script:orchPath
        $dot = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and
                           $n.InvocationOperator -eq 'Dot' -and $n.Extent.Text -match 'role-lib\.ps1' }, $true)
        $dot | Should -Not -BeNullOrEmpty -Because 'install.ps1 must dot-source the resolver'
        $call = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and
                            $n.GetCommandName() -eq 'Resolve-InstallRole' }, $true)
        $call | Should -Not -BeNullOrEmpty -Because '$Role must come from Resolve-InstallRole when -Role is not given'
        $call.Extent.Text | Should -Match "ContainsKey\('Role'\)" -Because 'only an explicit -Role may override the recorded role'
        $assign = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                              $n.Left.Extent.Text -eq '$Role' -and $n.Right.Extent.Text -match '\.Role$' }, $true)
        $assign | Should -Not -BeNullOrEmpty -Because 'the resolved role replaces $Role'
        $phase2 = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.Extent.Text -match '2-windows-config\.ps1' }, $true)
        $phase2 | Should -Not -BeNullOrEmpty
        $phase2.Extent.Text | Should -Match '-Role \$Role'
        $call.Extent.StartOffset | Should -BeLessThan $assign.Extent.StartOffset
        $assign.Extent.StartOffset | Should -BeLessThan $phase2.Extent.StartOffset -Because 'the role is resolved before phase 2 runs'
        $print = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.Extent.Text -match 'Memory role:' }, $true)
        $assign.Extent.StartOffset | Should -BeLessThan $print.Extent.StartOffset -Because 'the printed role is the applied role'
    }

    It 'install.ps1 still hands the WSL phase MEM0_ROLE only for an explicit -Role (phase 1 inherits on its own)' {
        $src = Get-Content $script:orchPath -Raw
        $src | Should -Match "if \(\`$PSBoundParameters\.ContainsKey\('Role'\)\) \{\s*wsl\.exe[^\n]*MEM0_ROLE='\`$Role'"
    }

    It '2-windows-config.ps1 resolves an omitted -Role the same way, so the direct run is as safe' {
        $ast = script:Get-Ast $script:phase2
        $dot = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and
                           $n.InvocationOperator -eq 'Dot' -and $n.Extent.Text -match 'role-lib\.ps1' }, $true)
        $dot | Should -Not -BeNullOrEmpty -Because '2-windows-config.ps1 must dot-source the resolver'
        $guard = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and
                             $n.Extent.Text -match "-not \`$PSBoundParameters\.ContainsKey\('Role'\)" -and $n.Extent.Text -match 'Resolve-InstallRole' -and
                             $n.Extent.Text -match '\$Role = ' }, $true)
        $guard | Should -Not -BeNullOrEmpty -Because 'the resolution must be conditional on -Role not being bound'
        # and before the first use of the role: the receipt is written from $eRole
        $use = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$eRole' }, $true)
        $guard.Extent.StartOffset | Should -BeLessThan $use.Extent.StartOffset
    }

    It 'every script that records a role and every one that resolves it parse cleanly' {
        foreach ($p in @($script:libPath, $script:orchPath, $script:phase2)) {
            { script:Get-Ast $p } | Should -Not -Throw
        }
    }
}
