#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# SessionStartPrewarm.Tests.ps1 - the SessionStart pre-warm warms the whole support tier, not just
# the embedder. Both models unload after 5 idle minutes; a reranker that is still cold when the
# first deliberate search arrives pays its load inside the caller's timeout. The pre-warm therefore
# asks the authority for `?warm=rerank`, and its hidden child is given the time a cold load needs
# (embed up to 10 s, then the one-document rerank up to 20 s).
#
# Run: pwsh -NoProfile -Command "Invoke-Pester <repo>\scripts\windows\tests\SessionStartPrewarm.Tests.ps1 -Output Detailed"

BeforeAll {
    $script:winDir = Split-Path -Parent $PSScriptRoot
    $script:code = ((Get-Content (Join-Path $script:winDir 'sessionstart-capture.ps1') -Raw) -split "`r?`n" |
        Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
    $script:cmdLine = ($script:code -split "`n" | Where-Object { $_ -match '\$prewarmCmd\s*=' } | Select-Object -First 1)
}

Describe 'SessionStart pre-warm' {
    It 'builds one pre-warm command' {
        $script:cmdLine | Should -Not -BeNullOrEmpty
    }
    It 'asks the authority to warm the reranker too' {
        $script:cmdLine | Should -Match '/health/embedder\?warm=rerank'
    }
    It 'gives the child longer than a cold embed plus a cold rerank (30 s)' {
        $m = [regex]::Match($script:cmdLine, '-TimeoutSec (\d+)')
        $m.Success | Should -BeTrue
        [int]$m.Groups[1].Value | Should -BeGreaterThan 30
    }
}
