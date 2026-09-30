# The mem0 add+search round-trip in install/3-verify.ps1 removes the smoke point it wrote.
#
# Every verify run used to leave one permanent 'smoke-test memory' (user verify-test) in the
# authority's store - 42 piled up - because the round-trip never deleted its own point and nothing
# failed. The check block is extracted from the real script by its AST and run against a mocked
# authority, so the assertions bind to the shipped code, not a copy of it.

BeforeAll {
    $script:repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
    $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $script:repoRoot 'install\3-verify.ps1'), [ref]$null, [ref]$errs)
    $errs | Should -BeNullOrEmpty -Because '3-verify.ps1 must parse'
    $cmd = $ast.FindAll({
            param($n)
            $n -is [System.Management.Automation.Language.CommandAst] -and
            $n.GetCommandName() -eq 'Check' -and
            $n.CommandElements.Count -gt 1 -and
            $n.CommandElements[1].Extent.Text -like '*mem0 add+search round-trip*'
        }, $true) | Select-Object -First 1
    $cmd | Should -Not -BeNullOrEmpty -Because 'the round-trip Check must exist'
    $blockAst = $cmd.CommandElements |
        Where-Object { $_ -is [System.Management.Automation.Language.ScriptBlockExpressionAst] } |
        Select-Object -First 1
    $script:smoke = $blockAst.ScriptBlock.GetScriptBlock()

    # the block shells out to wsl.exe for the api key; a function outranks the executable
    function script:wsl.exe { 'test-key' }
}

Describe 'install/3-verify.ps1 mem0 add+search round-trip' {

    BeforeEach {
        $Distro = 'test-distro'; $WslUser = 'tester'; $authorityUrl = 'http://authority.invalid:18791'
        $script:addResponse = [pscustomobject]@{ results = @([pscustomobject]@{ id = 'smoke-id-1' }) }
        $script:searchResults = @([pscustomobject]@{ id = 'smoke-id-1' })
        $script:deleteFails = $false
        Mock Start-Sleep { }
        Mock Invoke-RestMethod {
            if ($Method -eq 'Delete') {
                if ($script:deleteFails) { throw 'HTTP 403' }
                return [pscustomobject]@{ deleted = $true }
            }
            if ($Uri -like '*/v1/memories/search') { return [pscustomobject]@{ results = $script:searchResults } }
            return $script:addResponse
        }
    }

    It 'passes, and deletes exactly the point it added, by id' {
        (& $script:smoke) | Should -BeTrue
        Should -Invoke Invoke-RestMethod -Times 1 -Exactly -ParameterFilter { $Method -eq 'Delete' -and $Uri -like '*/v1/memories/smoke-id-1*' }
    }

    It 'fails the check when the delete fails' {
        $script:deleteFails = $true
        (& $script:smoke) | Should -Not -BeTrue
        Should -Invoke Invoke-RestMethod -Times 1 -Exactly -ParameterFilter { $Method -eq 'Delete' }
    }

    It 'still removes the point when the search never finds it, and fails' {
        $script:searchResults = @()
        (& $script:smoke) | Should -Not -BeTrue
        Should -Invoke Invoke-RestMethod -Times 1 -Exactly -ParameterFilter { $Method -eq 'Delete' -and $Uri -like '*smoke-id-1*' }
    }

    It 'fails, with no delete attempted, when the add returned no id to clean up' {
        $script:addResponse = [pscustomobject]@{ results = @() }
        (& $script:smoke) | Should -Not -BeTrue
        Should -Invoke Invoke-RestMethod -Times 0 -Exactly -ParameterFilter { $Method -eq 'Delete' }
    }
}
