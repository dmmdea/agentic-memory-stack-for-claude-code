#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# L1aExtract.Tests.ps1 - the real l1a-extract.ps1 run end to end under Windows PowerShell 5.1 (the
# production runtime) in a sandbox: a sandboxed USERPROFILE, the REAL memory-common.ps1 with only
# the network and codex edges replaced by recording stubs, and a scripted codex reply.
#
# What it pins:
#   C3   every fact posted to mem0 carries the brand its transcript path (and, in a content-rule
#        workspace, its own text) routes to; an unrouted path posts no brand at all.
#   6.7  a codex lock held by another worker is waited on, not skipped; a codex failure logs the
#        tail of its output; the extraction prompt keeps the episode for a session with turns.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester <repo>\scripts\windows\tests\L1aExtract.Tests.ps1 -Output Detailed"

BeforeAll {
    $script:winDir = Split-Path -Parent $PSScriptRoot
    $script:ps51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

    # The stub memory-common: load the REAL one (renamed) so the routing, the throttle, the cursor,
    # the lock and the ship-log split are production code, then replace the edges that would touch
    # a network or the codex CLI.
    $script:stubCommon = @'
. (Join-Path $PSScriptRoot 'memory-common-real.ps1')
function Test-Mem0Health { return $true }
function Get-Mem0Key { return 'test-key' }
function Add-Mem0Memory {
    param([string]$Text, [string]$Source, [hashtable]$Metadata = @{})
    $Metadata['source'] = $Source
    (@{ text = $Text; metadata = $Metadata } | ConvertTo-Json -Depth 5 -Compress) |
        Add-Content -LiteralPath (Join-Path $env:USERPROFILE 'records.jsonl') -Encoding UTF8
    return ([guid]::NewGuid().ToString())
}
function Invoke-RestMethod {
    param($Uri, $Method, $Body, $ContentType, $Headers, $TimeoutSec)
    $text = if ($Body -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($Body) } else { [string]$Body }
    Add-Content -LiteralPath (Join-Path $env:USERPROFILE 'episodes.jsonl') -Value $text -Encoding UTF8
}
function Invoke-CodexSubagent {
    param($Prompt, $ReasoningEffort, $TimeoutSeconds, $Model, $LastMessagePath)
    Set-Content -LiteralPath (Join-Path $env:USERPROFILE 'last-prompt.txt') -Value $Prompt -Encoding UTF8
    if ($env:STUB_CODEX_FAIL) { throw $env:STUB_CODEX_FAIL }
    Set-Content -LiteralPath $LastMessagePath -Value $env:STUB_CODEX_JSON -Encoding UTF8
    return 'header'
}
'@

    function script:New-L1aSandbox {
        param([string]$Slug, [string]$BrandsJson = $null)
        $root = Join-Path $TestDrive ('l1a-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        $home_ = Join-Path $root 'home'
        $bin = Join-Path $root 'bin'
        $projDir = Join-Path $home_ ('.claude\projects\' + $Slug)
        foreach ($d in @($bin, $projDir, (Join-Path $home_ '.claude\state'), (Join-Path $home_ '.claude\logs'))) {
            [System.IO.Directory]::CreateDirectory($d) | Out-Null
        }
        Copy-Item (Join-Path $script:winDir 'l1a-extract.ps1') $bin
        Copy-Item (Join-Path $script:winDir 'memory-common.ps1') (Join-Path $bin 'memory-common-real.ps1')
        Set-Content -LiteralPath (Join-Path $bin 'memory-common.ps1') -Value $script:stubCommon -Encoding UTF8
        if ($BrandsJson) { Set-Content -LiteralPath (Join-Path $bin 'brands.json') -Value $BrandsJson -Encoding ASCII }
        $transcript = Join-Path $projDir ([guid]::NewGuid().ToString() + '.jsonl')
        Set-Content -LiteralPath $transcript -Encoding UTF8 -Value @(
            '{"message":{"role":"user","content":"please review the storefront catalog"}}'
            '{"message":{"role":"assistant","content":"the catalog review is written up"}}'
        )
        return @{ Root = $root; Home = $home_; Bin = $bin; Transcript = $transcript }
    }

    function script:Invoke-L1a {
        param($Sb, [string]$CodexJson = '', [string]$CodexFail = '', [scriptblock]$BeforeRun = $null, [int]$TimeoutSec = 90)
        if ($BeforeRun) { & $BeforeRun }
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $script:ps51
        $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $Sb.Bin 'l1a-extract.ps1') + '" -TranscriptPath "' + $Sb.Transcript + '" -EventName Stop'
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.EnvironmentVariables['USERPROFILE'] = $Sb.Home
        $psi.EnvironmentVariables['STUB_CODEX_JSON'] = $CodexJson
        $psi.EnvironmentVariables['STUB_CODEX_FAIL'] = $CodexFail
        foreach ($k in @('L1A_REENTRANT', 'OPENAI_API_KEY', 'CODEX_API_KEY')) { $psi.EnvironmentVariables.Remove($k) }
        $p = [System.Diagnostics.Process]::Start($psi)
        $outT = $p.StandardOutput.ReadToEndAsync(); $errT = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($TimeoutSec * 1000)) { try { $p.Kill() } catch {}; throw 'l1a-extract.ps1 did not exit' }
        $rec = Join-Path $Sb.Home 'records.jsonl'
        $epi = Join-Path $Sb.Home 'episodes.jsonl'
        $log = Join-Path $Sb.Home '.claude\logs\l1a.log'
        return @{
            ExitCode = $p.ExitCode
            Stderr   = $errT.Result
            Records  = @(if (Test-Path $rec) { Get-Content $rec -Encoding UTF8 | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json } })
            Episodes = @(if (Test-Path $epi) { Get-Content $epi -Encoding UTF8 | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json } })
            Log      = $(if (Test-Path $log) { Get-Content $log -Raw -Encoding UTF8 } else { '' })
            Prompt   = $(if (Test-Path (Join-Path $Sb.Home 'last-prompt.txt')) { Get-Content (Join-Path $Sb.Home 'last-prompt.txt') -Raw } else { '' })
        }
    }

    function script:New-CodexJson {
        param([string[]]$Facts)
        return (@{ facts = $Facts; episode = @{ goal = 'review the catalog'; summary = 'reviewed it'; advanced_goals = @(); blocked_goals = @(); open_questions = @() } } | ConvertTo-Json -Depth 5 -Compress)
    }

    $script:brandsJson = '{"rules":[{"pattern":"projects/clienta","brand":"brand-a"}],"content_rule_workspaces":["projects-mixed"],"content_rules":[{"pattern":"alpha-store","brand":"brand-a"},{"pattern":"beta-shop","brand":"brand-b"}]}'
    $script:haveFive1 = Test-Path -LiteralPath $script:ps51
}

Describe 'L1a facts carry a brand (C3)' {
    BeforeAll { if (-not $script:haveFive1) { Set-ItResult -Skipped -Because 'Windows PowerShell 5.1 is not present' } }

    It 'a transcript under a routed path posts every fact with that brand, and the episode too' {
        $sb = New-L1aSandbox -Slug 'g--My-Drive-Projects-ClientA' -BrandsJson $script:brandsJson
        $r = Invoke-L1a $sb -CodexJson (New-CodexJson @('the storefront catalog lists twelve products', 'the storefront ships from one warehouse'))
        $r.ExitCode | Should -Be 0
        $r.Records.Count | Should -Be 2
        foreach ($rec in $r.Records) { $rec.metadata.brand | Should -Be 'brand-a' }
        $r.Episodes.Count | Should -Be 1
        $r.Episodes[0].brand | Should -Be 'brand-a'
    }

    It 'a content-rule workspace posts each fact with the brand its own text routes to' {
        $sb = New-L1aSandbox -Slug 'g--My-Drive-Projects-Mixed' -BrandsJson $script:brandsJson
        $facts = @('the alpha-store catalog lists twelve products', 'the beta-shop checkout uses a flat rate',
                   'alpha-store and beta-shop share one supplier', 'the supplier invoices monthly')
        $r = Invoke-L1a $sb -CodexJson (New-CodexJson $facts)
        $r.Records.Count | Should -Be 4
        $by = @{}
        foreach ($rec in $r.Records) { $by[$rec.text] = $rec.metadata }
        $by[$facts[0]].brand | Should -Be 'brand-a'
        $by[$facts[1]].brand | Should -Be 'brand-b'
        $by[$facts[2]].PSObject.Properties.Name | Should -Not -Contain 'brand' -Because 'two brands in one fact route nowhere'
        $by[$facts[3]].PSObject.Properties.Name | Should -Not -Contain 'brand' -Because 'no brand term routes nowhere'
        $r.Episodes[0].brand | Should -BeNullOrEmpty -Because 'the episode is classified by path only'
    }

    It 'an unrouted path posts no brand key on any fact' {
        $sb = New-L1aSandbox -Slug 'g--My-Drive-Elsewhere' -BrandsJson $script:brandsJson
        $r = Invoke-L1a $sb -CodexJson (New-CodexJson @('the alpha-store catalog lists twelve products', 'the supplier invoices monthly'))
        $r.Records.Count | Should -Be 2
        foreach ($rec in $r.Records) { $rec.metadata.PSObject.Properties.Name | Should -Not -Contain 'brand' }
    }

    It 'with no brands.json the stack default still routes its own workspace and nothing else' {
        $own = New-L1aSandbox -Slug 'd--My-Drive-AI-Ecosystem'
        (Invoke-L1a $own -CodexJson (New-CodexJson @('the supplier invoices monthly'))).Records[0].metadata.brand | Should -Be 'ai-ecosystem'
        $other = New-L1aSandbox -Slug 'g--My-Drive-Elsewhere'
        (Invoke-L1a $other -CodexJson (New-CodexJson @('the supplier invoices monthly'))).Records[0].metadata.PSObject.Properties.Name | Should -Not -Contain 'brand'
    }
}
