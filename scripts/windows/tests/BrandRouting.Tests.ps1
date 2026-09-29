#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# BrandRouting.Tests.ps1 - the PowerShell side of the C3 brand map (docs/systems/brands.md).
# The resolver runs the SAME corpus as the Python and Go resolvers
# (tests/fixtures/brand-routing-cases.jsonl); user-prompt-lib.ps1 and memory-common.ps1 carry one
# pinned copy each because they do not dot-source each other.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester <repo>\scripts\windows\tests\BrandRouting.Tests.ps1 -Output Detailed"

BeforeDiscovery {
    $fx = Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))) 'tests\fixtures\brand-routing-cases.jsonl'
    $script:corpus = @()
    $i = 0
    foreach ($line in @(Get-Content -LiteralPath $fx -Encoding UTF8)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $c = $line | ConvertFrom-Json
        $script:corpus += @{ i = $i; map = $c.map; path = [string]$c.path; text = [string]$c.text; expect = $c.expect }
        $i++
    }
}

BeforeAll {
    $script:winDir = Split-Path -Parent $PSScriptRoot
    . (Join-Path $script:winDir 'user-prompt-lib.ps1')
    . (Join-Path $script:winDir 'memory-common.ps1')
    function script:Get-CodeLines {
        param([string]$Path)
        ((Get-Content $Path -Raw) -split "`r?`n" | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
    }
    function script:Get-FunctionBody {
        param([string]$Path, [string]$Name)
        $m = [regex]::Match((script:Get-CodeLines $Path), "(?ms)^function $Name \{.*?^\}")
        if (-not $m.Success) { return '' }
        return ($m.Value -replace '\s+', ' ').Trim()
    }
}

Describe 'Resolve-BrandFromMap over the shared corpus' {
    It 'case <i>: <path> -> <expect>' -ForEach $script:corpus {
        $got = Resolve-BrandFromMap -Map $map -Path $path -Text $text
        if ($null -eq $expect) { $got | Should -BeNullOrEmpty } else { $got | Should -BeExactly $expect }
    }
    It 'the corpus is loaded (a silent empty -ForEach would pass vacuously)' {
        $fx = Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))) 'tests/fixtures/brand-routing-cases.jsonl'
        @(Get-Content -LiteralPath $fx | Where-Object { $_.Trim() }).Count | Should -BeGreaterThan 15
    }
    It 'never throws on a null map, null path or a rule that is not an object' {
        { Resolve-BrandFromMap -Map $null -Path 'x' -Text 'y' } | Should -Not -Throw
        Resolve-BrandFromMap -Map $null -Path 'x' | Should -BeNullOrEmpty
        Resolve-BrandFromMap -Map ([pscustomobject]@{ rules = @('junk', 3, $null) }) -Path 'x' | Should -BeNullOrEmpty
    }
}

Describe 'the two library copies are one resolver' {
    It 'keeps <fn> byte-identical (comment-stripped) in user-prompt-lib.ps1 and memory-common.ps1' -ForEach @(
        @{ fn = 'ConvertTo-BrandPath' }, @{ fn = 'ConvertTo-BrandPattern' }, @{ fn = 'Test-BrandPattern' },
        @{ fn = 'Get-BrandMapList' }, @{ fn = 'Resolve-BrandFromMap' }, @{ fn = 'Get-BrandMap' },
        @{ fn = 'Get-SharedBrands' }, @{ fn = 'Get-BrandFromTranscriptPath' }
    ) {
        $a = script:Get-FunctionBody (Join-Path $script:winDir 'user-prompt-lib.ps1') $fn
        $b = script:Get-FunctionBody (Join-Path $script:winDir 'memory-common.ps1') $fn
        $a | Should -Not -BeNullOrEmpty -Because "$fn must exist in user-prompt-lib.ps1"
        $b | Should -Be $a -Because "$fn in memory-common.ps1 must be the same function (edit both)"
    }
    It 'Get-InferredBrandFromPath delegates to Get-BrandFromTranscriptPath' {
        Mock Get-BrandFromTranscriptPath { return @{ brand = 'delegated'; workspace = $null; project = $null } }
        Get-InferredBrandFromPath -Path 'C:\x\projects\d--any-slug\s.jsonl' | Should -Be 'delegated'
        Should -Invoke Get-BrandFromTranscriptPath -Exactly 1
    }
}

Describe 'Get-BrandFromTranscriptPath' {
    BeforeAll {
        $script:map = [pscustomobject]@{
            rules = @([pscustomobject]@{ pattern = 'projects/clienta'; brand = 'brand-a' })
            content_rule_workspaces = @('projects-mixed')
            content_rules = @([pscustomobject]@{ pattern = 'alpha-store'; brand = 'brand-a' }, [pscustomobject]@{ pattern = 'beta-shop'; brand = 'brand-b' })
        }
    }
    It 'returns the routed brand and the transcript-dir slug as workspace and project' {
        $r = Get-BrandFromTranscriptPath -Path 'C:\h\.claude\projects\g--My-Drive-Projects-ClientA\s.jsonl' -Map $script:map
        $r.brand | Should -Be 'brand-a'
        $r.workspace | Should -Be 'g--My-Drive-Projects-ClientA'
        $r.project | Should -Be 'g--My-Drive-Projects-ClientA'
    }
    It 'classifies by the fact text in a content-rule workspace' {
        $p = 'C:\h\.claude\projects\g--My-Drive-Projects-Mixed\s.jsonl'
        (Get-BrandFromTranscriptPath -Path $p -Text 'the beta-shop checkout' -Map $script:map).brand | Should -Be 'brand-b'
        (Get-BrandFromTranscriptPath -Path $p -Text 'alpha-store and beta-shop' -Map $script:map).brand | Should -BeNullOrEmpty
        (Get-BrandFromTranscriptPath -Path $p -Map $script:map).brand | Should -BeNullOrEmpty
    }
    It 'an unrouted path has no brand but still names its workspace (never a constant)' {
        $r = Get-BrandFromTranscriptPath -Path 'C:\h\.claude\projects\g--My-Drive-Elsewhere\s.jsonl' -Map $script:map
        $r.brand | Should -BeNullOrEmpty
        $r.workspace | Should -Be 'g--My-Drive-Elsewhere'
    }
    It 'the workspace serializes under Windows PowerShell 5.1 (a Split-Path result carries provider properties the hook JavaScriptSerializer rejects)' {
        $ps51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        if (-not (Test-Path -LiteralPath $ps51)) { Set-ItResult -Skipped -Because 'Windows PowerShell 5.1 is not present'; return }
        $lib = Join-Path $script:winDir 'user-prompt-lib.ps1'
        $cmd = ". '$lib'; [void][System.Reflection.Assembly]::LoadWithPartialName('System.Web.Extensions'); " +
               '$j = [Activator]::CreateInstance([System.Web.Script.Serialization.JavaScriptSerializer]); ' +
               '$r = Get-BrandFromTranscriptPath -Path ''C:\x\agentic-memory-stack\s.jsonl''; ' +
               '$j.Serialize(@{ brand = $r.brand; workspace = $r.workspace; project = $r.project })'
        $out = (& $ps51 -NoProfile -ExecutionPolicy Bypass -Command $cmd 2>&1 | Out-String)
        $out | Should -Match '"workspace":"agentic-memory-stack"'
    }
    It 'an empty path yields nothing' {
        $r = Get-BrandFromTranscriptPath -Path '' -Map $script:map
        $r.brand | Should -BeNullOrEmpty
        $r.workspace | Should -BeNullOrEmpty
    }
}

Describe 'Get-BrandMap / Get-SharedBrands' {
    BeforeEach { Remove-Item Env:MEM0_SHARED_BRANDS -ErrorAction SilentlyContinue }
    AfterAll { Remove-Item Env:MEM0_SHARED_BRANDS -ErrorAction SilentlyContinue }
    It 'keeps the stack default rule when the file is missing, malformed or has no rules (behavior as before)' {
        foreach ($body in @($null, '{ not json', '{"rules": []}', '[1,2]')) {
            $f = Join-Path $TestDrive ('brands-' + [guid]::NewGuid().ToString('N') + '.json')
            if ($null -ne $body) { Set-Content -LiteralPath $f -Value $body -Encoding ASCII }
            $m = Get-BrandMap -Path $f
            Resolve-BrandFromMap -Map $m -Path 'd--My-Drive-AI-Ecosystem' | Should -Be 'ai-ecosystem'
            Resolve-BrandFromMap -Map $m -Path 'd--My-Drive-Elsewhere' | Should -BeNullOrEmpty
        }
    }
    It 'reads every C3 key from a valid file' {
        $f = Join-Path $TestDrive 'brands-valid.json'
        Set-Content -LiteralPath $f -Encoding ASCII -Value '{"rules":[{"pattern":"clienta","brand":"brand-a"}],"shared_brands":["Shared-A"],"content_rule_workspaces":["mixed"],"content_rules":[{"pattern":"beta-shop","brand":"brand-b"}]}'
        $m = Get-BrandMap -Path $f
        Resolve-BrandFromMap -Map $m -Path 'x-clienta' | Should -Be 'brand-a'
        Resolve-BrandFromMap -Map $m -Path 'x-mixed' -Text 'beta-shop' | Should -Be 'brand-b'
        @(Get-SharedBrands -Map $m) | Should -Be @('shared-a')
    }
    It 'Get-SharedBrands unions the map and MEM0_SHARED_BRANDS, lower-cased' {
        $env:MEM0_SHARED_BRANDS = 'shared-b, Shared-C,,'
        $got = @(Get-SharedBrands -Map ([pscustomobject]@{ shared_brands = @('Shared-A') })) | Sort-Object
        $got | Should -Be @('shared-a', 'shared-b', 'shared-c')
        @(Get-SharedBrands -Map $null).Count | Should -Be 2
    }
}

Describe 'no hook hard-codes a brand or workspace constant (C3)' {
    It '<f> names no literal brand label in code (comments excluded)' -ForEach @(
        @{ f = 'user-prompt-extract.ps1' }, @{ f = 'mem0-hook-daemon.ps1' }, @{ f = 'l1a-extract.ps1' }
    ) {
        $code = script:Get-CodeLines (Join-Path $script:winDir $f)
        $code | Should -Not -Match "'ai-ecosystem'" -Because "$f must take brand and workspace from Get-BrandFromTranscriptPath, never a constant"
    }
}

Describe 'Select-AdmittedMemoryResults treats shared brands as neutral (C3 client backstop)' {
    BeforeAll {
        function script:New-Hit([string]$Id, $Brand) {
            return [pscustomobject]@{ id = $Id; memory = "memory $Id"; metadata = [pscustomobject]@{ tier = 'evidence'; brand = $Brand } }
        }
    }
    BeforeEach { $env:MEM0_SHARED_BRANDS = 'shared-a'; $script:audit = Join-Path $TestDrive ('adm-' + [guid]::NewGuid().ToString('N') + '.jsonl') }
    AfterAll { Remove-Item Env:MEM0_SHARED_BRANDS -ErrorAction SilentlyContinue }

    It 'a brandless session sees neutral and shared-brand memories, never another brand''s' {
        $hits = @((New-Hit 'n' $null), (New-Hit 's' 'shared-a'), (New-Hit 'b' 'brand-a'))
        $ids = @(Select-AdmittedMemoryResults -Hits $hits -Brand '' -AuditPath $script:audit | ForEach-Object { $_.id })
        $ids | Should -Be @('n', 's')
    }
    It 'a brand-scoped session sees its own, neutral and shared-brand memories, never another brand''s' {
        $hits = @((New-Hit 'n' $null), (New-Hit 's' 'Shared-A'), (New-Hit 'own' 'brand-b'), (New-Hit 'other' 'brand-a'))
        $ids = @(Select-AdmittedMemoryResults -Hits $hits -Brand 'brand-b' -AuditPath $script:audit | ForEach-Object { $_.id })
        $ids | Should -Be @('n', 's', 'own')
    }
    It 'with no shared brands configured the fail-closed behavior is unchanged' {
        Remove-Item Env:MEM0_SHARED_BRANDS -ErrorAction SilentlyContinue
        $hits = @((New-Hit 'n' $null), (New-Hit 's' 'shared-a'))
        @(Select-AdmittedMemoryResults -Hits $hits -Brand '' -AuditPath $script:audit | ForEach-Object { $_.id }) | Should -Be @('n')
    }
}
