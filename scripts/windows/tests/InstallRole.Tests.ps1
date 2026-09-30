#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# InstallRole.Tests.ps1 - which role an install run applies (install/role-lib.ps1).
#
# install.ps1 declared `-Role` with the default 'brain' and passed it to 2-windows-config.ps1
# unconditionally, and the phase script writes it to the receipt, to both ~/.mem0/role files and
# to the brain gate that registers the nightly dream/dedup tasks. So a plain re-run of
# `.\install.ps1` on a replica turned the replica into a brain. (Phase 1 already keeps a recorded
# role when -Role is omitted; phase 2 did not.) An omitted -Role now keeps the recorded role.
#
# The other half of the same rule: a record that EXISTS but yields no role (a torn or empty role
# file, a receipt with a bad or empty Role, a receipt that will not parse) must never end at the
# 'brain' default. The first cut ignored such a record and fell through to brain, which is the
# replica-to-brain flip again for exactly the boxes whose records are damaged. It is an error now,
# thrown before either installer writes anything; only "nothing recorded" (or a receipt from
# before roles existed, with no role file) is a first install.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester <repo>/scripts/windows/tests/InstallRole.Tests.ps1 -Output Detailed"
# It also runs under Windows PowerShell 5.1 (Import-Module the Pester 5 manifest by path first): the
# resolver is dot-sourced by scripts that must parse there, and one test below runs it under 5.1.

BeforeAll {
    $script:winDir   = Split-Path -Parent $PSScriptRoot
    $script:repoRoot = Split-Path -Parent (Split-Path -Parent $script:winDir)
    $script:libPath  = Join-Path $script:repoRoot 'install\role-lib.ps1'
    $script:orchPath = Join-Path $script:repoRoot 'install.ps1'
    $script:phase2   = Join-Path $script:repoRoot 'install\2-windows-config.ps1'
    if (Test-Path $script:libPath) { . $script:libPath }

    function script:New-ReceiptText {
        # Shaped like the receipt 2-windows-config.ps1 writes. $RoleLiteral is what follows `Role = `
        # ("'replica'", "''", '$null', "@('x')"); $null leaves the line out (a receipt from before roles).
        param($RoleLiteral)
        $lines = @('@{', "    WslUser     = 'u'")
        if ($null -ne $RoleLiteral) { $lines += "    Role        = $RoleLiteral" }
        $lines += "    AuthorityUrl = 'http://192.0.2.7:18791'"
        $lines += '}'
        ($lines -join "`n") + "`n"
    }

    function script:New-Profile {
        # A fake %USERPROFILE%. ONLY what is passed exists: with no arguments the box has recorded
        # nothing. (The parameters are untyped on purpose: a [string] parameter turns a $null default
        # into '', and the old helper therefore wrote an EMPTY role file into every profile that was
        # meant to have none.)
        param(
            $RoleFile,                 # the text of .mem0\role
            [byte[]]$RoleBytes,        # the bytes of .mem0\role (NULs, UTF-16 without a BOM)
            [switch]$RoleIsDirectory,  # .mem0\role is a directory, so it cannot be read as a file
            $ReceiptRole,              # a normal receipt whose Role is this plain value
            $ReceiptLiteral,           # ... or whose Role line reads `Role = <this literal>`
            $ReceiptRaw,               # ... or the receipt's exact text (unparseable, empty ...)
            [switch]$LegacyReceipt     # a receipt with no Role key at all (from before roles existed)
        )
        $p = Join-Path $TestDrive ('p' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path (Join-Path $p '.mem0'), (Join-Path $p '.claude\scripts') | Out-Null
        $rf = Join-Path $p '.mem0\role'
        if ($RoleIsDirectory) { New-Item -ItemType Directory -Path $rf | Out-Null }
        elseif ($PSBoundParameters.ContainsKey('RoleBytes')) { [System.IO.File]::WriteAllBytes($rf, $RoleBytes) }
        elseif ($PSBoundParameters.ContainsKey('RoleFile')) { [System.IO.File]::WriteAllText($rf, [string]$RoleFile) }
        $rc = Join-Path $p '.claude\scripts\mem0-stack.config.psd1'
        if ($PSBoundParameters.ContainsKey('ReceiptRaw')) { [System.IO.File]::WriteAllText($rc, [string]$ReceiptRaw) }
        elseif ($PSBoundParameters.ContainsKey('ReceiptLiteral')) { [System.IO.File]::WriteAllText($rc, (script:New-ReceiptText $ReceiptLiteral)) }
        elseif ($PSBoundParameters.ContainsKey('ReceiptRole')) { [System.IO.File]::WriteAllText($rc, (script:New-ReceiptText "'$ReceiptRole'")) }
        elseif ($LegacyReceipt) { [System.IO.File]::WriteAllText($rc, (script:New-ReceiptText $null)) }
        $p
    }

    function script:Get-Tree {
        # Every directory and file under $Root with its size, write time and hash: two snapshots are
        # equal only when nothing was created, changed or rewritten in between.
        # -IgnorePowerShellHostCache: a child pwsh whose USERPROFILE is redirected writes its OWN startup
        # cache, AppData\Local\Microsoft\PowerShell\StartupProfileData-<mode>, under that profile when it
        # exits. That file is the PowerShell runtime's, not the installer's, so exactly it (and the empty
        # directories that only hold it) is left out; any other file under AppData still counts.
        param([string]$Root, [switch]$IgnorePowerShellHostCache)
        $full = (Resolve-Path -LiteralPath $Root).ProviderPath.TrimEnd('\')
        $lines = @(Get-ChildItem -LiteralPath $full -Recurse -Force | Sort-Object FullName | ForEach-Object {
            $rel = $_.FullName.Substring($full.Length)
            if ($_.PSIsContainer) { "D $rel" }
            else { 'F {0} {1} {2} {3}' -f $rel, $_.Length, $_.LastWriteTimeUtc.Ticks, (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
        })
        if ($IgnorePowerShellHostCache) {
            $lines = @($lines | Where-Object { $_ -notmatch '^[DF] \\AppData(\\Local(\\Microsoft(\\PowerShell(\\StartupProfileData-[^ ]*)?)?)?)?( |$)' })
        }
        $lines -join "`n"
    }

    function script:Get-ThrownMessage {
        param([scriptblock]$Block)
        try { & $Block | Out-Null } catch { return $_.Exception.Message }
        return $null
    }
}

Describe 'Resolve-InstallRole' {
    It 'the library exists and defines the resolver' {
        Test-Path $script:libPath | Should -BeTrue
        Get-Command Resolve-InstallRole -ErrorAction SilentlyContinue | Should -Not -BeNullOrEmpty
    }

    It 'the fixture helper records nothing unless it is told to (the old one wrote an empty role file into every profile)' {
        $p = script:New-Profile
        Test-Path (Join-Path $p '.mem0\role') | Should -BeFalse
        Test-Path (Join-Path $p '.claude\scripts\mem0-stack.config.psd1') | Should -BeFalse
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

    It 'an empty profile path is a first install too' {
        (Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir '').Role | Should -Be 'brain'
    }

    It 'the receipt stands in when the role file is absent (an install older than the role file)' {
        $p = script:New-Profile -ReceiptRole 'replica'
        $r = Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p
        $r.Role | Should -Be 'replica'
        $r.Source | Should -Match 'receipt'
    }

    It 'a receipt from before roles existed (no Role key) and no role file is a first install: brain' {
        $p = script:New-Profile -LegacyReceipt
        $r = Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p
        $r.Role | Should -Be 'brain'
        $r.Source | Should -Match 'default'
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

    It 'returns the literal role, never the text it was read from' {
        $p = script:New-Profile -RoleFile "  REPLICA  `r`n"
        $r = (Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p).Role
        $r.Length | Should -Be 7
        [int[]][char[]]$r | Should -Be ([int[]][char[]]'replica')
    }
}

Describe 'Resolve-InstallRole: a record it cannot use is an error, never a silent brain' {
    # Every way a record can exist and still say nothing usable. `Setup` is splatted into New-Profile;
    # `Names` are the records the error must name.
    It 'stops on <Name>: names the record, asks for an explicit -Role, writes nothing' -ForEach @(
        @{ Name = 'a role file holding another word';         Setup = @{ RoleFile = "observer`n" };                     Names = @('role') }
        @{ Name = 'a role file with a trailing note';         Setup = @{ RoleFile = "replica # note`n" };               Names = @('role') }
        @{ Name = 'a role file with two lines';               Setup = @{ RoleFile = "replica`nbrain`n" };               Names = @('role') }
        @{ Name = 'a role file of NUL bytes';                 Setup = @{ RoleBytes = [byte[]](0, 0, 0, 0, 0, 0) };      Names = @('role') }
        @{ Name = 'a role file in UTF-16 without a BOM';      Setup = @{ RoleBytes = [byte[]][System.Text.Encoding]::Unicode.GetBytes("replica`n") }; Names = @('role') }
        # no trailing newline: read as UTF-8 this is "r<NUL>e<NUL>...a<NUL>", which a culture-aware -eq equates with 'replica'
        @{ Name = 'a UTF-16 role file with no newline';       Setup = @{ RoleBytes = [byte[]][System.Text.Encoding]::Unicode.GetBytes('replica') };   Names = @('role') }
        @{ Name = 'an empty role file (a torn write)';        Setup = @{ RoleFile = '' };                               Names = @('role') }
        @{ Name = 'a whitespace-only role file';              Setup = @{ RoleFile = "  `r`n" };                         Names = @('role') }
        @{ Name = 'a role that is a directory';               Setup = @{ RoleIsDirectory = $true };                     Names = @('role') }
        @{ Name = 'a receipt whose Role is another word';     Setup = @{ ReceiptRole = 'Replica2' };                    Names = @('receipt') }
        @{ Name = 'a receipt with an empty Role';             Setup = @{ ReceiptLiteral = "''" };                       Names = @('receipt') }
        @{ Name = 'a receipt with a null Role';               Setup = @{ ReceiptLiteral = '$null' };                    Names = @('receipt') }
        @{ Name = 'a receipt whose Role is not text';         Setup = @{ ReceiptLiteral = "@('replica')" };             Names = @('receipt') }
        @{ Name = 'a receipt that will not parse';           Setup = @{ ReceiptRaw = '@{ Role = ' };                   Names = @('receipt') }
        @{ Name = 'an empty receipt';                         Setup = @{ ReceiptRaw = '' };                             Names = @('receipt') }
        @{ Name = 'a receipt that is not a data file';        Setup = @{ ReceiptRaw = "'just a string'" };              Names = @('receipt') }
        @{ Name = 'a garbage role file and a receipt with no Role key'; Setup = @{ RoleFile = "observer`n"; LegacyReceipt = $true }; Names = @('role') }
        @{ Name = 'a garbage role file and a garbage receipt'; Setup = @{ RoleFile = "observer`n"; ReceiptRole = 'x' };  Names = @('role', 'receipt') }
        @{ Name = 'an empty role file and an empty receipt';   Setup = @{ RoleFile = ''; ReceiptLiteral = "''" };        Names = @('role', 'receipt') }
    ) {
        $p = script:New-Profile @Setup
        $before = script:Get-Tree $p
        $msg = script:Get-ThrownMessage { Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p -WarningAction SilentlyContinue }
        $msg | Should -Not -BeNullOrEmpty -Because 'a record the resolver cannot use must stop the run, not fall through to brain'
        $msg | Should -Match '(?i)pass -Role brain or -Role replica explicitly'
        if ($Names -contains 'role')    { $msg.Contains((Join-Path $p '.mem0\role')) | Should -BeTrue -Because "the message names the role file: $msg" }
        if ($Names -contains 'receipt') { $msg.Contains((Join-Path $p '.claude\scripts\mem0-stack.config.psd1')) | Should -BeTrue -Because "the message names the receipt: $msg" }
        $msg | Should -Not -Match '[\x00-\x08\x0B\x0C\x0E-\x1F]' -Because 'a NUL or control character from a damaged file must not reach the console'
        script:Get-Tree $p | Should -BeExactly $before -Because 'the resolver reads; it must not create, change or rewrite anything under the profile'
    }

    It 'a very long junk value is cut, not echoed whole' {
        $p = script:New-Profile -RoleFile ('x' * 5000)
        $msg = script:Get-ThrownMessage { Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p }
        $msg | Should -Not -BeNullOrEmpty
        $msg.Length | Should -BeLessThan 1500
    }

    It 'still throws for an unparseable receipt when the caller runs with ErrorActionPreference Continue' {
        # Import-PowerShellDataFile reports a bad file as a NON-terminating error unless asked otherwise;
        # a caller that is not install.ps1 must not turn "cannot parse" into "nothing recorded".
        $p = script:New-Profile -ReceiptRaw '@{ Role = '
        $old = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { $msg = script:Get-ThrownMessage { Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p 2>$null } }
        finally { $ErrorActionPreference = $old }
        $msg | Should -Match '(?i)pass -Role brain or -Role replica explicitly'
        $msg | Should -Match 'could not be parsed as a PowerShell Data File' -Because 'the message must carry the real reason, not a follow-on error from a null result'
    }

    It 'ConvertTo-InstallRole compares ordinally and returns the literal, so NUL padding is not a role' {
        ConvertTo-InstallRole ("r`0e`0p`0l`0i`0c`0a`0") | Should -BeNullOrEmpty
        ConvertTo-InstallRole ("b`0r`0a`0i`0n`0") | Should -BeNullOrEmpty
        ConvertTo-InstallRole "  REPLICA `r`n" | Should -BeExactly 'replica'
        ConvertTo-InstallRole ([string][char]0xFEFF + "Brain") | Should -BeExactly 'brain'
        ConvertTo-InstallRole 'replica2' | Should -BeNullOrEmpty
        ConvertTo-InstallRole '' | Should -BeNullOrEmpty
    }

    It 'an explicit -Role still wins over <Name> (the operator names the role; nothing is read)' -ForEach @(
        @{ Name = 'a role file holding another word'; Setup = @{ RoleFile = "observer`n" } }
        @{ Name = 'an empty role file';               Setup = @{ RoleFile = '' } }
        @{ Name = 'a receipt that will not parse';    Setup = @{ ReceiptRaw = '@{ Role = ' } }
    ) {
        $p = script:New-Profile @Setup
        $r = Resolve-InstallRole -Explicit $true -Requested 'replica' -ProfileDir $p -WarningVariable w -WarningAction SilentlyContinue
        $r.Role | Should -Be 'replica'
        @($w).Count | Should -Be 0
    }

    It 'a usable role in the OTHER record still wins, with a warning naming the one it skipped: <Name>' -ForEach @(
        @{ Name = 'a garbage role file and a valid receipt'; Setup = @{ RoleFile = "observer`n"; ReceiptRole = 'replica' }; Expect = 'replica'; Skipped = 'role' }
        @{ Name = 'an empty role file and a valid receipt';  Setup = @{ RoleFile = '';           ReceiptRole = 'replica' }; Expect = 'replica'; Skipped = 'role' }
        @{ Name = 'a garbage role file and a brain receipt'; Setup = @{ RoleFile = "observer`n"; ReceiptRole = 'brain' };   Expect = 'brain';   Skipped = 'role' }
    ) {
        $p = script:New-Profile @Setup
        $r = Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p -WarningVariable w -WarningAction SilentlyContinue
        $r.Role | Should -Be $Expect
        $r.Source | Should -Match 'receipt'
        @($w).Count | Should -Be 1
        ([string]$w[0]).Contains((Join-Path $p '.mem0\role')) | Should -BeTrue -Because 'the warning names the record that was skipped'
    }

    It 'a valid role file is not undone by a damaged receipt (the role file is read first, as Get-Mem0Role does)' {
        $p = script:New-Profile -RoleFile "replica`n" -ReceiptRaw '@{ Role = '
        (Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $p).Role | Should -Be 'replica'
    }
}

Describe 'an unusable role record stops both installers before anything is written' {
    BeforeAll {
        $script:pwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue | Select-Object -First 1).Source

        function script:Invoke-Installer {
            # Runs a real installer script in a child pwsh whose profile is the sandbox and whose PATH holds
            # nothing but pwsh's own directory: past the resolver the script would call wsl.exe, which is not
            # reachable, so a run that gets that far fails there instead of touching a real WSL distro.
            param([string]$ScriptPath, [string[]]$ScriptArgs, [string]$ProfileDir)
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $script:pwshExe
            $argLine = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $ScriptPath + '"'
            foreach ($a in $ScriptArgs) { $argLine += ' ' + $a }
            $psi.Arguments = $argLine
            $psi.UseShellExecute = $false
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.CreateNoWindow = $true
            $root = [System.IO.Path]::GetPathRoot($ProfileDir)
            $psi.EnvironmentVariables['USERPROFILE'] = $ProfileDir
            $psi.EnvironmentVariables['HOME'] = $ProfileDir
            $psi.EnvironmentVariables['HOMEDRIVE'] = $root.TrimEnd('\')
            $psi.EnvironmentVariables['HOMEPATH'] = '\' + $ProfileDir.Substring($root.Length)
            $psi.EnvironmentVariables['PATH'] = (Split-Path -Parent $script:pwshExe)
            if ($psi.EnvironmentVariables.ContainsKey('PSModulePath')) { $psi.EnvironmentVariables.Remove('PSModulePath') }
            $proc = [System.Diagnostics.Process]::Start($psi)
            $so = $proc.StandardOutput.ReadToEndAsync()
            $se = $proc.StandardError.ReadToEndAsync()
            if (-not $proc.WaitForExit(180000)) { try { $proc.Kill() } catch { $null = $_ }; throw "installer child timed out: $ScriptPath" }
            $proc.WaitForExit()
            [pscustomobject]@{ ExitCode = $proc.ExitCode; Text = (($so.Result + "`n" + $se.Result) -replace '\s+', ' ') }
        }
    }

    It '<Script> stops on <Name> with the resolver''s error and leaves the profile byte-identical' -Skip:(-not (Get-Command pwsh -ErrorAction SilentlyContinue)) -ForEach @(
        @{ Script = 'install.ps1';                   ScriptArgs = @();                 Name = 'a role file holding another word'; Setup = @{ RoleFile = "observer`n" } }
        @{ Script = 'install.ps1';                   ScriptArgs = @();                 Name = 'an empty role file';               Setup = @{ RoleFile = '' } }
        @{ Script = 'install.ps1';                   ScriptArgs = @();                 Name = 'a receipt that will not parse';    Setup = @{ ReceiptRaw = '@{ Role = ' } }
        @{ Script = 'install\2-windows-config.ps1';  ScriptArgs = @('-WslUser', 'sbx'); Name = 'a role file holding another word'; Setup = @{ RoleFile = "observer`n" } }
        @{ Script = 'install\2-windows-config.ps1';  ScriptArgs = @('-WslUser', 'sbx'); Name = 'an empty role file';               Setup = @{ RoleFile = '' } }
        @{ Script = 'install\2-windows-config.ps1';  ScriptArgs = @('-WslUser', 'sbx'); Name = 'a receipt that will not parse';    Setup = @{ ReceiptRaw = '@{ Role = ' } }
    ) {
        $p = script:New-Profile @Setup
        $before = script:Get-Tree $p -IgnorePowerShellHostCache
        $r = script:Invoke-Installer -ScriptPath (Join-Path $script:repoRoot $Script) -ScriptArgs $ScriptArgs -ProfileDir $p
        $r.ExitCode | Should -Not -Be 0
        $r.Text | Should -Match 'Cannot tell which role this box has'
        $r.Text | Should -Match '(?i)Pass -Role brain or -Role replica explicitly'
        $r.Text | Should -Not -Match 'Memory role:|role \(no -Role given\)|\[0/4\]|not recognized' -Because 'the run must end at the resolver, not at a later step'
        script:Get-Tree $p -IgnorePowerShellHostCache | Should -BeExactly $before -Because 'nothing under the profile may be created, changed or rewritten'
    }

    It 'control: the snapshot sees a write a child makes under the profile, and only the PowerShell host cache is ignored' -Skip:(-not (Get-Command pwsh -ErrorAction SilentlyContinue)) {
        # Without this the byte-identical assertions above could pass because the comparison is blind.
        $p = script:New-Profile -RoleFile "observer`n"
        $before = script:Get-Tree $p -IgnorePowerShellHostCache
        $writer = Join-Path $TestDrive 'writer.ps1'
        [System.IO.File]::WriteAllText($writer, "Set-Content -LiteralPath (Join-Path `$env:USERPROFILE '.mem0\stray') -Value x`n")
        $r = script:Invoke-Installer -ScriptPath $writer -ScriptArgs @() -ProfileDir $p
        $r.ExitCode | Should -Be 0
        script:Get-Tree $p -IgnorePowerShellHostCache | Should -Not -BeExactly $before -Because 'a file written under .mem0 must show up in the snapshot'
    }

    It '<Script> control: a usable record gets past the resolver and stops at the missing wsl.exe (the sandbox cannot reach a real WSL)' -Skip:(-not (Get-Command pwsh -ErrorAction SilentlyContinue)) -ForEach @(
        @{ Script = 'install.ps1';                   ScriptArgs = @() }
        @{ Script = 'install\2-windows-config.ps1';  ScriptArgs = @('-WslUser', 'sbx') }
    ) {
        $p = script:New-Profile -RoleFile "replica`n"
        $r = script:Invoke-Installer -ScriptPath (Join-Path $script:repoRoot $Script) -ScriptArgs $ScriptArgs -ProfileDir $p
        $r.ExitCode | Should -Not -Be 0
        $r.Text | Should -Not -Match 'Cannot tell which role'
        $r.Text | Should -Match 'wsl\.exe'
    }
}

Describe 'the resolver under Windows PowerShell 5.1' {
    BeforeAll {
        $script:ps51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    }

    It 'parses and gives the same answers under powershell.exe: a usable record resolves, an unusable one throws' -Skip:(-not (Test-Path (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'))) {
        $good = script:New-Profile -RoleFile "replica`n"
        $bad  = script:New-Profile -RoleFile "observer`n"
        $empty = script:New-Profile -RoleFile ''
        $probe = Join-Path $TestDrive 'probe51.ps1'
        $body = @'
$ErrorActionPreference = 'Stop'
. 'LIB'
$o = [ordered]@{ edition = ('{0} {1}' -f $PSVersionTable.PSEdition, $PSVersionTable.PSVersion.Major) }
$o.good = (Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir 'GOOD').Role
foreach ($k in 'BAD', 'EMPTY') {
    $dir = if ($k -eq 'BAD') { 'BADDIR' } else { 'EMPTYDIR' }
    try { $null = Resolve-InstallRole -Explicit $false -Requested 'brain' -ProfileDir $dir; $o[$k] = 'no throw' }
    catch { $o[$k] = 'threw: ' + $_.Exception.Message }
}
$o | ConvertTo-Json -Compress
'@
        $body = $body.Replace('LIB', $script:libPath).Replace('GOOD', $good).Replace('BADDIR', $bad).Replace('EMPTYDIR', $empty)
        [System.IO.File]::WriteAllText($probe, $body, (New-Object System.Text.UTF8Encoding($true)))
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $script:ps51
        $psi.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $probe + '"'
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        $so = $proc.StandardOutput.ReadToEndAsync()
        $se = $proc.StandardError.ReadToEndAsync()
        $proc.WaitForExit(120000) | Should -BeTrue
        $proc.WaitForExit()
        $proc.ExitCode | Should -Be 0 -Because ("stderr: " + $se.Result)
        # the probe's JSON is its last line; a stray warning above it must not break the parse
        $j = (@($so.Result -split "`n" | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() }) | Select-Object -Last 1) | ConvertFrom-Json
        $j.edition | Should -Be 'Desktop 5'
        $j.good | Should -Be 'replica'
        $j.BAD | Should -Match '^threw: .*(?i)Pass -Role brain or -Role replica explicitly'
        $j.EMPTY | Should -Match '^threw: .*is empty'
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

        function script:Get-SideEffects {
            # Statements outside any function that write, register, launch or reach WSL: what must not
            # run before the role is settled.
            param($Ast)
            $names = @('wsl.exe', 'wsl', 'Start-Transcript', 'Write-StackFile', 'New-Item', 'Set-Content', 'Add-Content', 'Out-File',
                       'Copy-Item', 'Move-Item', 'Remove-Item', 'Rename-Item', 'Register-ScheduledTask', 'Unregister-ScheduledTask',
                       'Set-ItemProperty', 'New-ItemProperty', 'Remove-ItemProperty', 'Start-Process', 'Invoke-WebRequest', 'Invoke-RestMethod')
            $found = $Ast.FindAll({ param($n)
                if ($n -is [System.Management.Automation.Language.CommandAst]) {
                    return (($names -contains $n.GetCommandName()) -or ($n.InvocationOperator -eq 'Ampersand'))
                }
                if ($n -is [System.Management.Automation.Language.InvokeMemberExpressionAst]) {
                    return ($n.Extent.Text -match '^\[(System\.)?(IO\.File|IO\.Directory|Environment)\]::(Write|Append|Create|Delete|Copy|Move|SetEnvironmentVariable)')
                }
                return $false
            }, $true)
            @($found | Where-Object {
                $up = $_.Parent; $inFunction = $false
                while ($up) { if ($up -is [System.Management.Automation.Language.FunctionDefinitionAst]) { $inFunction = $true; break }; $up = $up.Parent }
                -not $inFunction
            })
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

    It '<Script> resolves the role before the first statement that writes, registers, launches or reaches WSL' -ForEach @(
        @{ Script = 'install.ps1' }
        @{ Script = 'install\2-windows-config.ps1' }
    ) {
        # The resolver may now throw. That leaves the box untouched only if it runs first.
        $ast = script:Get-Ast (Join-Path $script:repoRoot $Script)
        $call = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Resolve-InstallRole' }, $true)
        $call | Should -Not -BeNullOrEmpty
        $effects = @(script:Get-SideEffects $ast)
        $effects.Count | Should -BeGreaterThan 0 -Because 'the scan must find the script''s side effects, or it proves nothing'
        $first = $effects | Sort-Object { $_.Extent.StartOffset } | Select-Object -First 1
        $call.Extent.StartOffset | Should -BeLessThan $first.Extent.StartOffset -Because "the resolver must precede '$($first.Extent.Text)' (line $($first.Extent.StartLineNumber))"
    }

    It 'every script that records a role and every one that resolves it parse cleanly' {
        foreach ($p in @($script:libPath, $script:orchPath, $script:phase2)) {
            { script:Get-Ast $p } | Should -Not -Throw
        }
    }
}
