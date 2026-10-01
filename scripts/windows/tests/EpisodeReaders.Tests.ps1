#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# EpisodeReaders.Tests.ps1 - 1.32.4: the PowerShell readers of the episode list look at FINISHED episodes.
#
# An unfinished episode (in_progress, or abandoned after it was never finalized) has no goal and the newest
# ended_at in the table, and every prompt's checkpoint moves that ended_at. Two readers were fooled by it:
#   - dream-consolidate.ps1 Get-RecentEpisodes: a plain "last 7" window filled with unfinished rows, each a
#     blank line. It now asks for state=complete (an older server ignores the parameter) and skips any row
#     that has no goal either way.
#   - Test-MemoryStack.ps1, the 'episodic.db :v0.15' row: "last episode N hours ago" read the checkpoint
#     clock, so it could not see the Stop-hook extraction stopping. It now reads last_complete_ended_at
#     (the newest FINISHED episode) when the server reports it.
#
# Both are read out of their scripts and run with the REST call mocked: the scripts have top-level side
# effects, so they cannot be dot-sourced, and no live authority is contacted.

BeforeAll {
    $script:winDir = Split-Path -Parent $PSScriptRoot
    $tokens = $null; $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $script:winDir 'dream-consolidate.ps1'), [ref]$tokens, [ref]$errs)
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-RecentEpisodes' }, $true)
    $fn | Should -Not -BeNullOrEmpty
    . ([scriptblock]::Create($fn.Extent.Text))
    function Write-MemoryLog { param($Component, $Message) }
    function Add-Check { param($Layer, $Name, $Status, $Detail) }

    # the episodic.db health row is an inline block between two comment headers
    $tms = Get-Content -Raw -Encoding UTF8 (Join-Path $script:winDir 'Test-MemoryStack.ps1')
    $a = $tms.IndexOf('# R5: episodic.db health (v0.15)')
    $b = $tms.IndexOf('# R6: goals health (v0.16)')
    ($a -ge 0 -and $b -gt $a) | Should -BeTrue
    $script:r5Block = $tms.Substring($a, $b - $a)
}

Describe 'dream Get-RecentEpisodes shows finished episodes only' {
    BeforeEach {
        $script:uris = @()
        Mock Get-Content { return "test-key`n" }
    }

    It 'asks the authority for state=complete' {
        Mock Invoke-RestMethod { $script:uris += $Uri; return @() }
        Get-RecentEpisodes -Limit 7 | Should -BeNullOrEmpty
        $script:uris.Count | Should -Be 1
        $script:uris[0] | Should -Match '/v1/episodes\?recent=7&state=complete$'
    }

    It 'skips a row that has no goal (an unfinished episode from an older server) and keeps the finished ones' {
        Mock Invoke-RestMethod {
            return @(
                [pscustomobject]@{ goal_text = '';             summary_text = '<task-notification>b0c1</task-notification> | Another Claude session sent a message:'; ended_at = '2026-10-01T09:00:00+00:00'; brand = 'acme'; state = 'in_progress' },
                [pscustomobject]@{ goal_text = $null;          summary_text = 'typed prompts so far';                                                                    ended_at = '2026-10-01T08:00:00+00:00'; brand = 'acme'; state = 'abandoned' },
                [pscustomobject]@{ goal_text = 'Ship the fix'; summary_text = 'Shipped it.';                                                                             ended_at = '2026-09-30T10:00:00+00:00'; brand = 'acme'; state = 'complete' }
            )
        }
        $out = Get-RecentEpisodes -Limit 7
        $out | Should -Be '- [2026-09-30] acme: Ship the fix'
        $out | Should -Not -Match 'task-notification'
        $out | Should -Not -Match 'typed prompts'
    }

    It 'returns nothing when every row is unfinished' {
        Mock Invoke-RestMethod { return @([pscustomobject]@{ goal_text = ''; summary_text = 'x'; ended_at = '2026-10-01T09:00:00+00:00'; brand = $null; state = 'in_progress' }) }
        Get-RecentEpisodes -Limit 7 | Should -BeNullOrEmpty
    }
}

Describe "Test-MemoryStack 'episodic.db :v0.15' row reads the finished-only clock" {
    BeforeAll {
        function script:Run-R5 {
            param($Response)
            $script:checks = @()
            $script:resp = $Response
            $key = 'k'; $TmsAuthorityUrl = 'http://authority.invalid:1'
            Invoke-Expression $script:r5Block
            return @($script:checks)
        }
        $script:now = Get-Date
        function script:Iso { param([double]$HoursAgo) return $script:now.AddHours(-$HoursAgo).ToString('o') }
    }
    BeforeEach {
        Mock Add-Check { $script:checks += [pscustomobject]@{ Status = $Status; Detail = $Detail } }
        Mock Invoke-RestMethod { return $script:resp }
    }

    It 'a server that reports last_complete_ended_at: a recent finish is OK even though checkpoints are newer' {
        $c = script:Run-R5 ([pscustomobject]@{ count = 9; last_ended_at = (script:Iso 0); last_complete_ended_at = (script:Iso 2) })
        $c.Count | Should -Be 1
        $c[0].Status | Should -Be 'OK'
        $c[0].Detail | Should -Be '9 episodes; last finished 2h ago'
    }

    It 'a server that reports it: no finish for over a week WARNs although a checkpoint landed a moment ago' {
        $c = script:Run-R5 ([pscustomobject]@{ count = 9; last_ended_at = (script:Iso 0); last_complete_ended_at = (script:Iso 300) })
        $c[0].Status | Should -Be 'WARN'
        $c[0].Detail | Should -Match '^9 episodes but last finished 300h ago \(stale'
    }

    It 'a server that reports it as null: episodes exist but none was ever finished -> WARN, never the checkpoint clock' {
        $c = script:Run-R5 ([pscustomobject]@{ count = 5; last_ended_at = (script:Iso 0); last_complete_ended_at = $null })
        $c[0].Status | Should -Be 'WARN'
        $c[0].Detail | Should -Be '5 episodes but none finished (L1a Stop hook may be failing)'
    }

    It 'an older server (no last_complete_ended_at) falls back to last_ended_at, as before' {
        $c = script:Run-R5 ([pscustomobject]@{ count = 9; last_ended_at = (script:Iso 2) })
        $c[0].Status | Should -Be 'OK'
        $c[0].Detail | Should -Be '9 episodes; last 2h ago'
        $stale = script:Run-R5 ([pscustomobject]@{ count = 9; last_ended_at = (script:Iso 300) })
        $stale[0].Status | Should -Be 'WARN'
        $stale[0].Detail | Should -Match '^9 episodes but last 300h ago \(stale'
    }

    It 'an empty ledger still reads as empty' {
        $c = script:Run-R5 ([pscustomobject]@{ count = 0; last_ended_at = $null; last_complete_ended_at = $null })
        $c[0].Status | Should -Be 'WARN'
        $c[0].Detail | Should -Match '^empty'
    }
}
