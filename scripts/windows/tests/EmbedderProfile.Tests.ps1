# EmbedderProfile.Tests.ps1 - the PowerShell consumers of the embedding space.
#
# mem0-server/embedder_profile.py is the one definition of the embedding space (model alias,
# collections, cosine thresholds). PowerShell cannot import it, so four consumers carry a small
# mirror of the fields they need, and these tests are what stops a mirror from drifting:
#   scripts/windows/autopromote-lib.ps1   collection + sibling cosine per profile
#   scripts/windows/Test-MemoryStack.ps1  stock llama-swap alias per profile (replica fallback)
#   install/0-prereqs.ps1                 llama.cpp build floor per profile
# and the document prefix the two verifiers probe with. Each mirror is parsed out of the shipped
# file and compared with embedder_profile.py, so a new profile, a recalibrated threshold or a renamed
# collection fails here until the mirror follows.
#
# The behaviour tests run the shipped functions (extracted by AST) against mocked Qdrant, llama-swap
# and WSL: nothing here touches a live service, this box's stack.env or its install receipt.

BeforeAll {
    $script:winDir   = Split-Path -Parent $PSScriptRoot
    $script:repoRoot = (Resolve-Path (Join-Path $script:winDir '..\..')).Path
    $script:libPath  = Join-Path $script:winDir 'autopromote-lib.ps1'
    $script:tmsPath  = Join-Path $script:winDir 'Test-MemoryStack.ps1'
    $script:prePath  = Join-Path $script:repoRoot 'install\0-prereqs.ps1'
    $script:verPath  = Join-Path $script:repoRoot 'install\3-verify.ps1'
    $script:profPath = Join-Path $script:repoRoot 'mem0-server\embedder_profile.py'

    function script:Get-CodeText {
        param([string]$Path)   # the file without its comment lines, so prose can never satisfy a pin
        ((Get-Content -LiteralPath $Path -Raw -Encoding UTF8) -split "`r?`n" |
            Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
    }
    function script:Get-FunctionText {
        param([string]$Path, [string]$Name)
        $errs = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$errs)
        $errs | Should -BeNullOrEmpty -Because "$Path must parse"
        $fn = $ast.Find({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $Name }, $true)
        $fn | Should -Not -BeNullOrEmpty -Because "$Name must exist in $Path"
        return $fn.Extent.Text
    }
    function script:Get-AssignedValue {
        param([string]$Path, [string]$Variable)   # evaluates the right-hand side of `$Variable = ...`
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$null)
        $as = $ast.Find({ param($a) $a -is [System.Management.Automation.Language.AssignmentStatementAst] -and
                                    $a.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
                                    $a.Left.VariablePath.UserPath -eq $Variable }, $true)
        $as | Should -Not -BeNullOrEmpty -Because "$Variable must be assigned in $Path"
        return (& ([scriptblock]::Create($as.Right.Extent.Text)))
    }

    # embedder_profile.py -> { name -> @{ Memories; Model; Dims; Sibling } }, plus the default and the prefix.
    $py = Get-Content -LiteralPath $script:profPath -Raw -Encoding UTF8
    $start = $py.IndexOf('PROFILES = {')
    $end   = $py.IndexOf('DEFAULT_PROFILE =')
    $start | Should -BeGreaterThan 0 -Because 'embedder_profile.py must define PROFILES'
    $end   | Should -BeGreaterThan $start
    $block = (($py.Substring($start, $end - $start)) -split "`r?`n" | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
    $script:pyProfiles = @{}
    foreach ($part in @($block -split 'EmbedProfile\(' | Select-Object -Skip 1)) {
        $name = [regex]::Match($part, '\bname="([^"]+)"').Groups[1].Value
        $name | Should -Not -BeNullOrEmpty -Because 'every EmbedProfile(...) names itself'
        $script:pyProfiles[$name] = @{
            Memories = [regex]::Match($part, '\bmemories="([^"]+)"').Groups[1].Value
            Model    = [regex]::Match($part, '(?<![A-Za-z_])model="([^"]+)"').Groups[1].Value
            Dims     = [int][regex]::Match($part, '\bdims=(\d+)').Groups[1].Value
            Sibling  = [double][regex]::Match($part, '\bsibling=([0-9.]+)').Groups[1].Value
        }
    }
    $script:pyDefault = [regex]::Match($py, '(?m)^DEFAULT_PROFILE\s*=\s*"([^"]+)"').Groups[1].Value
    $script:pyDocPrefix = [regex]::Match($py, '(?m)^_EG_DOC\s*=\s*"([^"]*)"').Groups[1].Value
    $script:pySource = $py

    . $script:libPath
}

Describe 'embedder_profile.py is parsed correctly (the parity tests below are not vacuous)' {
    It 'yields both known profiles, each with every field the mirrors compare' {
        $script:pyProfiles.Keys | Should -Contain 'egemma-300m'
        $script:pyProfiles.Keys | Should -Contain 'egemma2'
        foreach ($k in $script:pyProfiles.Keys) {
            $script:pyProfiles[$k].Memories | Should -Not -BeNullOrEmpty -Because "$k memories collection"
            $script:pyProfiles[$k].Model    | Should -Not -BeNullOrEmpty -Because "$k llama-swap alias"
            $script:pyProfiles[$k].Dims     | Should -BeGreaterThan 0 -Because "$k dims"
            $script:pyProfiles[$k].Sibling  | Should -BeGreaterThan 0 -Because "$k sibling cosine"
        }
        $script:pyProfiles['egemma-300m'].Memories | Should -Be 'mem0_egemma_768'
        $script:pyProfiles['egemma2'].Memories     | Should -Be 'mem0_eg2_768'
        $script:pyProfiles['egemma2'].Model        | Should -Be 'embeddinggemma2'
        $script:pyDefault   | Should -Be 'egemma-300m'
        $script:pyDocPrefix | Should -Be 'title: none | text: '
    }
}

Describe 'autopromote-lib.ps1 mirrors embedder_profile.py (collection + sibling cosine)' {
    It 'lists exactly the profiles embedder_profile.py defines' {
        (@($script:AmEmbedProfiles.Keys | Sort-Object) -join ',') | Should -BeExactly (@($script:pyProfiles.Keys | Sort-Object) -join ',') -Because 'a profile missing here would throw at the nightly gate; an extra one would bind a space that does not exist'
    }
    It 'carries the same memories collection and sibling cosine for every profile' {
        foreach ($k in $script:pyProfiles.Keys) {
            $script:AmEmbedProfiles[$k].Collection       | Should -BeExactly $script:pyProfiles[$k].Memories -Because "$k collection"
            $script:AmEmbedProfiles[$k].SiblingThreshold | Should -Be $script:pyProfiles[$k].Sibling -Because "$k sibling cosine"
        }
    }
    It 'defaults to the profile embedder_profile.py defaults to' {
        $script:AmDefaultEmbedProfile | Should -BeExactly $script:pyDefault
    }
    It 'names a collection or a cosine nowhere but in that table' {
        $code = script:Get-CodeText $script:libPath
        $code | Should -Not -Match '\[double\]\$\w+\s*=\s*0\.\d' -Because 'a cosine default in a signature is a literal that ignores the profile'
        $code | Should -Not -Match "\[string\]\`$Collection\s*=\s*'" -Because 'a collection default in a signature is a literal that ignores the profile'
        ([regex]::Matches($code, 'mem0_egemma_768')).Count | Should -Be 1 -Because 'the table is the only place the lib names a collection'
    }
}

Describe 'Read-AmStackEnv / Get-AmEmbedProfile' {
    BeforeEach {
        $script:envKeys = 'MEM0_EMBED_PROFILE', 'MEM0_QDRANT_COLLECTION', 'MEM0_COLLECTION'
        $script:envWas = @{}
        foreach ($k in $script:envKeys) { $script:envWas[$k] = [System.Environment]::GetEnvironmentVariable($k); [System.Environment]::SetEnvironmentVariable($k, $null) }
        $script:stackEnv = Join-Path $TestDrive 'stack.env'
    }
    AfterEach {
        foreach ($k in $script:envKeys) { [System.Environment]::SetEnvironmentVariable($k, $script:envWas[$k]) }
    }

    It 'with nothing configured stays in the space existing stores were built in' {
        $p = Get-AmEmbedProfile -StackEnvPath (Join-Path $TestDrive 'absent.env')
        $p.Name | Should -Be 'egemma-300m'
        $p.Collection | Should -Be 'mem0_egemma_768'
        $p.SiblingThreshold | Should -Be 0.6
    }
    It 'reads the profile from stack.env, the first occurrence of a key winning and comments ignored' {
        Set-Content -LiteralPath $script:stackEnv -Value "# MEM0_EMBED_PROFILE=egemma-300m`n  MEM0_EMBED_PROFILE = egemma2 `nMEM0_EMBED_PROFILE=egemma-300m`nOTHER=1"
        $p = Get-AmEmbedProfile -StackEnvPath $script:stackEnv
        $p.Name | Should -Be 'egemma2'
        $p.Collection | Should -Be 'mem0_eg2_768'
        $p.SiblingThreshold | Should -Be 0.825
    }
    It 'lets the environment beat stack.env' {
        Set-Content -LiteralPath $script:stackEnv -Value 'MEM0_EMBED_PROFILE=egemma2'
        $env:MEM0_EMBED_PROFILE = 'egemma-300m'
        (Get-AmEmbedProfile -StackEnvPath $script:stackEnv).Name | Should -Be 'egemma-300m'
    }
    It 'honours the same collection overrides as embedder_profile.collection(): MEM0_QDRANT_COLLECTION, then MEM0_COLLECTION, environment before stack.env' {
        Set-Content -LiteralPath $script:stackEnv -Value "MEM0_EMBED_PROFILE=egemma2`nMEM0_COLLECTION=legacy_named"
        (Get-AmEmbedProfile -StackEnvPath $script:stackEnv).Collection | Should -Be 'legacy_named'
        $env:MEM0_QDRANT_COLLECTION = 'env_named'
        (Get-AmEmbedProfile -StackEnvPath $script:stackEnv).Collection | Should -Be 'env_named'
    }
    It 'throws on an unknown profile (case-sensitive) instead of falling back to a space the store was not built in' {
        $env:MEM0_EMBED_PROFILE = 'egemma3'
        { Get-AmEmbedProfile -StackEnvPath (Join-Path $TestDrive 'absent.env') } | Should -Throw '*not a known embedding profile*'
        $env:MEM0_EMBED_PROFILE = 'EGEMMA2'
        { Get-AmEmbedProfile -StackEnvPath (Join-Path $TestDrive 'absent.env') } | Should -Throw '*not a known embedding profile*'
    }
    It 'Get-CorroborationCount defaults its cosine to the profile: a 0.7 sibling counts on 300m, not on EmbeddingGemma-2' {
        $env:MEM0_EMBED_PROFILE = 'egemma-300m'
        Get-CorroborationCount -SiblingScores @(0.7, 0.9) | Should -Be 3
        $env:MEM0_EMBED_PROFILE = 'egemma2'
        Get-CorroborationCount -SiblingScores @(0.7, 0.9) | Should -Be 2
        Get-CorroborationCount -SiblingScores @(0.7, 0.9) -Threshold 0.6 | Should -Be 3 -Because 'an explicit threshold still wins'
    }
}

Describe 'Get-PromotionGateVerdict reads the collection and the sibling cosine of the active embedding space' {
    BeforeAll {
        function Invoke-CodexSubagent { param($Prompt, $ReasoningEffort, $TimeoutSeconds, $Model) }
        function Get-CodexResponseText { param($RawOutput) }
        function Parse-CodexTokenUsage { param($RawOutput) }
        function Parse-CodexHeader { param($RawOutput) return @{ Model = 'stub-model'; Effort = 'stub-effort' } }
        function Write-CodexUsageLog { param($Component, $TokensUsed, $DurationMs, $Status, $FactsPosted, $ModelRequested, $EffortRequested, $ModelResolved, $EffortResolved, $Outcome) }
        function script:Body-Text($Body) { if ($Body -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($Body) } else { [string]$Body } }
    }
    BeforeEach {
        $script:envKeys = 'MEM0_EMBED_PROFILE', 'MEM0_QDRANT_COLLECTION', 'MEM0_COLLECTION'
        $script:envWas = @{}
        foreach ($k in $script:envKeys) { $script:envWas[$k] = [System.Environment]::GetEnvironmentVariable($k); [System.Environment]::SetEnvironmentVariable($k, $null) }
        $script:uris = [System.Collections.Generic.List[string]]::new()
        Mock Invoke-RestMethod {
            $script:uris.Add([string]$Uri)
            if ($Uri -match '/points$') { return [pscustomobject]@{ result = @([pscustomobject]@{ payload = [pscustomobject]@{ user_id = 'u'; created_at = '2026-06-01T00:00:00'; updated_at = '2026-06-01T00:00:00' } }) } }
            if ((script:Body-Text $Body) -match '"with_payload":false') {
                return [pscustomobject]@{ result = [pscustomobject]@{ points = @(0.7, 0.9 | ForEach-Object { [pscustomobject]@{ score = $_ } }) } }
            }
            return [pscustomobject]@{ result = [pscustomobject]@{ points = @() } }
        }
        Mock Invoke-CodexSubagent { 'raw' }
        Mock Get-CodexResponseText { '{"contradicts": false}' }
        Mock Parse-CodexTokenUsage { 0 }
    }
    AfterEach {
        foreach ($k in $script:envKeys) { [System.Environment]::SetEnvironmentVariable($k, $script:envWas[$k]) }
    }

    It 'queries the 300m collection with its 0.6 cosine on the default profile' {
        $env:MEM0_EMBED_PROFILE = 'egemma-300m'
        $r = Get-PromotionGateVerdict -MemoryId 'm1' -CandidateText 'a fact' -EvidenceRecord ([pscustomobject]@{ metadata = [pscustomobject]@{ source = 'l1a-extractor' } })
        $r.embedProfile | Should -Be 'egemma-300m'
        $r.collection | Should -Be 'mem0_egemma_768'
        $r.siblingThreshold | Should -Be 0.6
        $r.siblingCount | Should -Be 2
        @($script:uris | Where-Object { $_ -like '*/collections/mem0_egemma_768/points*' }).Count | Should -BeGreaterThan 0
        @($script:uris | Where-Object { $_ -like '*mem0_eg2_768*' }).Count | Should -Be 0
    }
    It 'queries the EmbeddingGemma-2 collection with its 0.825 cosine when that profile is active' {
        $env:MEM0_EMBED_PROFILE = 'egemma2'
        $r = Get-PromotionGateVerdict -MemoryId 'm1' -CandidateText 'a fact' -EvidenceRecord ([pscustomobject]@{ metadata = [pscustomobject]@{ source = 'l1a-extractor' } })
        $r.embedProfile | Should -Be 'egemma2'
        $r.collection | Should -Be 'mem0_eg2_768'
        $r.siblingThreshold | Should -Be 0.825
        $r.siblingCount | Should -Be 1 -Because 'the 0.7 sibling is noise on the new scale; only the 0.9 one corroborates'
        @($script:uris | Where-Object { $_ -like '*/collections/mem0_eg2_768/points*' }).Count | Should -BeGreaterThan 0
        @($script:uris | Where-Object { $_ -like '*mem0_egemma_768*' }).Count | Should -Be 0
    }
    It 'an explicit collection and cosine win, and then the profile is never consulted' {
        $env:MEM0_EMBED_PROFILE = 'not-a-profile'
        $r = Get-PromotionGateVerdict -MemoryId 'm1' -CandidateText 'a fact' -EvidenceRecord ([pscustomobject]@{ metadata = [pscustomobject]@{ source = 'l1a-extractor' } }) `
                -SiblingThreshold 0.5 -Collection 'restore_copy'
        $r.collection | Should -Be 'restore_copy'
        $r.siblingThreshold | Should -Be 0.5
        $r.embedProfile | Should -BeNullOrEmpty
    }
    It 'throws on an unknown profile when it has to resolve one (the dream treats a throw as a gate error: fail-safe BLOCK in enforce)' {
        $env:MEM0_EMBED_PROFILE = 'not-a-profile'
        { Get-PromotionGateVerdict -MemoryId 'm1' -CandidateText 'a fact' -EvidenceRecord ([pscustomobject]@{ metadata = [pscustomobject]@{ source = 'x' } }) } | Should -Throw '*not a known embedding profile*'
    }
}

Describe 'Test-MemoryStack.ps1: the L4 embedder probe and the collection fallback name no model or collection' {
    BeforeAll {
        $script:tmsCode = script:Get-CodeText $script:tmsPath
    }
    It 'carries no collection literal and no bare width check' {
        $script:tmsCode | Should -Not -Match 'mem0_egemma_768|mem0_eg2_768' -Because 'the bound collection comes from /health/deep or the server venv'
        $script:tmsCode | Should -Not -Match '\$dim -eq 768' -Because 'a width cannot tell two EmbeddingGemma generations apart'
        $script:tmsCode | Should -Match "Add-Check 'LIVENESS' 'EmbeddingGemma :11436'" -Because 'the row keeps its name'
        $script:tmsCode | Should -Match "Add-Check 'LIVENESS' 'embedder identity' 'WARN'" -Because 'a profile / alias disagreement must surface as its own row'
    }
    It 'resolves the I13 bound collection from /health/deep, then the profile it reports, then the server venv' {
        $script:tmsCode | Should -Match '\$psColl = if \(\$hd -and \$hd\.collection\) \{ \$hd\.collection \}\s+elseif \(\$hd -and \$hd\.embed_profile\.collections\.memories\) \{ \$hd\.embed_profile\.collections\.memories \}\s+else \{ \(Get-TmsEmbedProfilePy\)\.Collection \}'
        $script:tmsCode | Should -Match "throw 'probe skipped: the bound collection could not be resolved" -Because 'an unresolvable collection skips the probe before it writes anything'
        $script:tmsCode | Should -Match '(?s)finally\s*\{\s*if \(\$psMid\)' -Because 'the PUT-survival probe must still delete its record on every path'
    }
    It 'probes the document prefix both EmbeddingGemma generations use' {
        $script:tmsCode.Contains("input='$($script:pyDocPrefix)ping'") | Should -BeTrue
    }
    It 'its stock-alias fallback mirrors embedder_profile.py for every profile' {
        $stock = script:Get-AssignedValue $script:tmsPath 'TmsProfileStockModel'
        (@($stock.Keys | Sort-Object) -join ',') | Should -BeExactly (@($script:pyProfiles.Keys | Sort-Object) -join ',')
        foreach ($k in $script:pyProfiles.Keys) { $stock[$k] | Should -BeExactly $script:pyProfiles[$k].Model -Because "$k stock alias" }
    }
    It 'every profile has a nonzero width the verifiers may assume (both generations are 768-d)' {
        foreach ($k in $script:pyProfiles.Keys) { $script:pyProfiles[$k].Dims | Should -Be 768 }
    }
}

Describe 'Test-MemoryStack.ps1: Get-TmsEmbedderExpectation' {
    BeforeAll {
        $script:TmsProfileStockModel = script:Get-AssignedValue $script:tmsPath 'TmsProfileStockModel'
        $script:TmsWslUser = 'tester'; $script:TmsDistro = 'test-distro'
        . ([scriptblock]::Create((script:Get-FunctionText $script:tmsPath 'Get-TmsEmbedProfilePy')))
        . ([scriptblock]::Create((script:Get-FunctionText $script:tmsPath 'Get-TmsEmbedderExpectation')))
        function script:New-Hd {
            param([string]$Profile = 'egemma2', [string]$Model = 'embeddinggemma2', [string]$Collection = 'mem0_eg2_768',
                  [string]$ProbedModel = $Model, [string]$ProfileCollection = $Collection, [int]$Dim = 768, [bool]$EmbOk = $true)
            [pscustomobject]@{
                collection = $Collection
                embed_profile = [pscustomobject]@{ profile = $Profile; model = $Model; template_version = 'eg-search-v1'
                                                    collections = [pscustomobject]@{ memories = $ProfileCollection } }
                checks = [pscustomobject]@{ embedder = [pscustomobject]@{ ok = $EmbOk; dim = $Dim; model = $ProbedModel } }
            }
        }
    }
    It 'on the brain probes the alias the authority reports, with its width, and asks python nothing' {
        Mock Get-TmsEmbedProfilePy { $null }
        $x = Get-TmsEmbedderExpectation -Hd (New-Hd -Profile 'egemma2' -Model 'embeddinggemma2-ams') -Remote $false
        $x.Profile | Should -Be 'egemma2'
        $x.Model | Should -Be 'embeddinggemma2-ams'
        $x.Dim | Should -Be 768
        $x.Template | Should -Be 'eg-search-v1'
        $x.Collection | Should -Be 'mem0_eg2_768'
        $x.Source | Should -Be 'authority /health/deep'
        $x.Notes.Count | Should -Be 0
        Should -Invoke Get-TmsEmbedProfilePy -Times 0 -Exactly
    }
    It 'on a replica probes the alias THIS box serves for the authority profile, not the authority private alias' {
        Mock Get-TmsEmbedProfilePy { [pscustomobject]@{ Profile = 'egemma2'; Model = 'embeddinggemma2-local'; Collection = 'mem0_eg2_768' } }
        $x = Get-TmsEmbedderExpectation -Hd (New-Hd -Profile 'egemma2' -Model 'embeddinggemma2-ams') -Remote $true
        $x.Model | Should -Be 'embeddinggemma2-local' -Because 'the alias this box resolves for the profile (its stack.env override) beats both the authority alias and the stock alias'
        $x.Notes.Count | Should -Be 0
        Should -Invoke Get-TmsEmbedProfilePy -Times 1 -Exactly -ParameterFilter { $Profile -eq 'egemma2' }
    }
    It 'warns when a replica is itself bound to another profile than its authority' {
        Mock Get-TmsEmbedProfilePy {
            if ($Profile) { [pscustomobject]@{ Profile = $Profile; Model = 'embeddinggemma2'; Collection = 'x' } }
            else { [pscustomobject]@{ Profile = 'egemma-300m'; Model = 'embeddinggemma'; Collection = 'mem0_egemma_768' } }
        }
        $x = Get-TmsEmbedderExpectation -Hd (New-Hd) -Remote $true
        $x.Model | Should -Be 'embeddinggemma2'
        $x.Notes.Count | Should -Be 1
        $x.Notes[0] | Should -Match "own embedder profile is 'egemma-300m'.*authority is bound to 'egemma2'"
    }
    It 'falls back to the profile stock alias when the replica has no embedder_profile module to ask' {
        Mock Get-TmsEmbedProfilePy { $null }
        (Get-TmsEmbedderExpectation -Hd (New-Hd -Profile 'egemma2' -Model 'embeddinggemma2-ams') -Remote $true).Model | Should -Be 'embeddinggemma2'
        (Get-TmsEmbedderExpectation -Hd (New-Hd -Profile 'egemma-300m' -Model 'embeddinggemma-ams' -Collection 'mem0_egemma_768') -Remote $true).Model | Should -Be 'embeddinggemma'
        (Get-TmsEmbedderExpectation -Hd (New-Hd -Profile 'future-profile' -Model 'future-alias') -Remote $true).Model | Should -Be 'future-alias' -Because 'an unknown profile falls back to the authority alias'
    }
    It 'reports a disagreement between the authority probe and its profile, and a collection outside the profile' {
        Mock Get-TmsEmbedProfilePy { $null }
        $x = Get-TmsEmbedderExpectation -Hd (New-Hd -ProbedModel 'embeddinggemma' -Collection 'mem0_egemma_768' -ProfileCollection 'mem0_eg2_768') -Remote $false
        $x.Notes.Count | Should -Be 2
        ($x.Notes -join ' ') | Should -Match "probed its embedder as 'embeddinggemma' but its profile names 'embeddinggemma2'"
        ($x.Notes -join ' ') | Should -Match "bound to collection 'mem0_egemma_768' but its profile 'egemma2' names 'mem0_eg2_768'"
    }
    It 'asks the local server venv when the authority reports no embed_profile, and uses what it says' {
        Mock Get-TmsEmbedProfilePy { [pscustomobject]@{ Profile = 'egemma2'; Model = 'embeddinggemma2'; Collection = 'mem0_eg2_768' } }
        $old = [pscustomobject]@{ collection = 'mem0_eg2_768'; checks = [pscustomobject]@{ embedder = [pscustomobject]@{ ok = $true; dim = 768 } } }
        $x = Get-TmsEmbedderExpectation -Hd $old -Remote $false
        $x.Profile | Should -Be 'egemma2'
        $x.Model | Should -Be 'embeddinggemma2'
        $x.Source | Should -Match 'server venv'
        (Get-TmsEmbedderExpectation -Hd $null -Remote $false).Model | Should -Be 'embeddinggemma2'
    }
    It 'with no profile anywhere sends the request this verifier sent before profiles existed' {
        Mock Get-TmsEmbedProfilePy { $null }
        foreach ($hd in @($null, [pscustomobject]@{ collection = 'mem0_egemma_768' })) {
            $x = Get-TmsEmbedderExpectation -Hd $hd -Remote $false
            $x.Profile | Should -BeNullOrEmpty
            $x.Model | Should -Be 'embeddinggemma'
            $x.Dim | Should -Be 768
            $x.Source | Should -Be 'pre-profile literal'
        }
    }
    It 'uses the width the authority embedder reported only when that embedder was ok' {
        Mock Get-TmsEmbedProfilePy { $null }
        (Get-TmsEmbedderExpectation -Hd (New-Hd -Dim 512) -Remote $false).Dim | Should -Be 512
        (Get-TmsEmbedderExpectation -Hd (New-Hd -Dim 0 -EmbOk $false) -Remote $false).Dim | Should -Be 768
    }
}

Describe 'Test-MemoryStack.ps1: Get-TmsEmbedProfilePy talks to the server venv safely' {
    BeforeAll {
        $script:TmsWslUser = 'tester'; $script:TmsDistro = 'test-distro'
        . ([scriptblock]::Create((script:Get-FunctionText $script:tmsPath 'Get-TmsEmbedProfilePy')))
        $script:wslLines = @()
        $script:wslCalls = [System.Collections.Generic.List[string]]::new()
        function script:wsl.exe { $script:wslCalls.Add(($args -join ' ')); $script:wslLines }
    }
    BeforeEach { $script:wslCalls.Clear() }
    It 'parses the answer line out of login-shell noise' {
        $script:wslLines = @('motd noise', 'TMS-EMBED|egemma2|embeddinggemma2|mem0_eg2_768', 'trailing')
        $r = Get-TmsEmbedProfilePy -Profile 'egemma2'
        $r.Profile | Should -Be 'egemma2'; $r.Model | Should -Be 'embeddinggemma2'; $r.Collection | Should -Be 'mem0_eg2_768'
        $script:wslCalls[0] | Should -Match "MEM0_EMBED_PROFILE=egemma2 /home/$($script:TmsWslUser)/apps/mem0-server/\.venv/bin/python"
    }
    It 'asks for the active profile (no env prefix) when no profile is named' {
        $script:wslLines = @('TMS-EMBED|egemma-300m|embeddinggemma|mem0_egemma_768')
        (Get-TmsEmbedProfilePy).Profile | Should -Be 'egemma-300m'
        $script:wslCalls[0] | Should -Not -Match 'MEM0_EMBED_PROFILE='
    }
    It 'returns $null when the venv prints nothing usable' {
        $script:wslLines = @('Traceback...', '')
        Get-TmsEmbedProfilePy | Should -BeNullOrEmpty
    }
    It 'never interpolates an odd profile name into the shell line' {
        $script:wslLines = @('TMS-EMBED|x|y|z')
        Get-TmsEmbedProfilePy -Profile 'egemma2; rm -rf ~' | Should -BeNullOrEmpty
        $script:wslCalls.Count | Should -Be 0
    }
}

Describe 'Test-MemoryStack.ps1: the L4 row, run against a mocked llama-swap' {
    BeforeAll {
        $src = Get-Content -LiteralPath $script:tmsPath -Raw -Encoding UTF8
        $m = [regex]::Match($src, '(?s)(\$embExp = Get-TmsEmbedderExpectation.*?)(?=\r?\n# L5:)')
        $m.Success | Should -BeTrue -Because 'the L4 probe block must be findable'
        $script:l4Block = $m.Groups[1].Value
        $script:TmsProfileStockModel = script:Get-AssignedValue $script:tmsPath 'TmsProfileStockModel'
        $script:TmsWslUser = 'tester'; $script:TmsDistro = 'test-distro'
        . ([scriptblock]::Create((script:Get-FunctionText $script:tmsPath 'Get-ProbeFailure')))
        . ([scriptblock]::Create((script:Get-FunctionText $script:tmsPath 'Get-TmsEmbedProfilePy')))
        . ([scriptblock]::Create((script:Get-FunctionText $script:tmsPath 'Get-TmsEmbedderExpectation')))
        function script:Add-Check { param([string]$Dimension, [string]$Component, [string]$Status, [string]$Detail = '') $script:rows.Add([pscustomobject]@{ Component = $Component; Status = $Status; Detail = $Detail }) }
        function script:Run-L4 {
            param($Hd, [bool]$Replica = $false, [bool]$Loopback = $true)
            $script:rows = [System.Collections.Generic.List[object]]::new()
            $hd = $Hd; $TmsIsReplica = $Replica; $TmsAuthorityIsLoopback = $Loopback; $probeTimeoutSec = 90
            . ([scriptblock]::Create($script:l4Block))
            return @($script:rows | Where-Object { $_.Component -eq 'EmbeddingGemma :11436' })[0]
        }
        function script:New-Hd2 { param([string]$Profile = 'egemma2', [string]$Model = 'embeddinggemma2')
            [pscustomobject]@{ collection = 'mem0_eg2_768'
                embed_profile = [pscustomobject]@{ profile = $Profile; model = $Model; template_version = 'eg-search-v1'; collections = [pscustomobject]@{ memories = 'mem0_eg2_768' } }
                checks = [pscustomobject]@{ embedder = [pscustomobject]@{ ok = $true; dim = 768; model = $Model } } } }
    }
    BeforeEach {
        $script:sentModel = $null
        $script:vecLen = 768
        $script:rest = 'ok'
        Mock Get-TmsEmbedProfilePy { $null }
        Mock Invoke-RestMethod {
            $script:sentModel = ([System.Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json).model
            if ($script:rest -eq 'http') { throw 'HTTP 404: model not found' }
            if ($script:rest -eq 'timeout') { throw 'The request was canceled due to the configured timeout' }
            [pscustomobject]@{ data = @([pscustomobject]@{ embedding = @(1..$script:vecLen) }) }
        }
    }
    It 'EmbeddingGemma-2 authority: probes its alias and reports profile + alias + template' {
        $r = Run-L4 -Hd (New-Hd2)
        $script:sentModel | Should -Be 'embeddinggemma2'
        $r.Status | Should -Be 'OK'
        $r.Detail | Should -Be 'egemma2/embeddinggemma2 live, dim=768 [profile+alias from authority /health/deep, template eg-search-v1]'
    }
    It 'a width that is not the authority width is a WARN naming the profile' {
        $script:vecLen = 384
        $r = Run-L4 -Hd (New-Hd2)
        $r.Status | Should -Be 'WARN'
        $r.Detail | Should -Match 'egemma2/embeddinggemma2 responded but dim=384 \(expected 768\)'
    }
    It 'an alias the local llama-swap rejects is a FAIL that names the alias and the profile' {
        $script:rest = 'http'
        $r = Run-L4 -Hd (New-Hd2)
        $r.Status | Should -Be 'FAIL'
        $r.Detail | Should -Match "probe as alias 'embeddinggemma2' failed.*must serve it for profile egemma2.*model not found"
    }
    It 'a cold model (timeout) stays a WARN, not a FAIL' {
        $script:rest = 'timeout'
        (Run-L4 -Hd (New-Hd2)).Status | Should -Be 'WARN'
    }
    It 'a disagreement inside the authority binding adds an identity WARN row beside the OK row' {
        $hd = New-Hd2; $hd.checks.embedder.model = 'embeddinggemma'
        $r = Run-L4 -Hd $hd
        $r.Status | Should -Be 'OK'
        @($script:rows | Where-Object { $_.Component -eq 'embedder identity' -and $_.Status -eq 'WARN' }).Count | Should -Be 1
    }
    It 'authority without embed_profile and no venv answer: the request and the row text are exactly the pre-profile ones' {
        $r = Run-L4 -Hd ([pscustomobject]@{ collection = 'mem0_egemma_768'; checks = [pscustomobject]@{ embedder = [pscustomobject]@{ ok = $true; dim = 768 } } })
        $script:sentModel | Should -Be 'embeddinggemma'
        $r.Status | Should -Be 'OK'
        $r.Detail | Should -Be 'embeddinggemma live, dim=768'
        @($script:rows | Where-Object { $_.Component -eq 'embedder identity' }).Count | Should -Be 0
    }
    It 'authority unreachable (no /health/deep at all) still probes, and a failure is the plain pre-profile FAIL' {
        $script:rest = 'http'
        $r = Run-L4 -Hd $null
        $script:sentModel | Should -Be 'embeddinggemma'
        $r.Status | Should -Be 'FAIL'
        $r.Detail | Should -Be 'HTTP 404: model not found'
    }
    It 'a replica probes the alias this box resolves for the authority profile' {
        Mock Get-TmsEmbedProfilePy { [pscustomobject]@{ Profile = 'egemma2'; Model = 'embeddinggemma2'; Collection = 'mem0_eg2_768' } }
        $r = Run-L4 -Hd (New-Hd2 -Model 'embeddinggemma2-ams') -Replica $true -Loopback $false
        $script:sentModel | Should -Be 'embeddinggemma2'
        $r.Status | Should -Be 'OK'
    }
}

Describe 'install/0-prereqs.ps1: the llama.cpp floor follows the embedding profile' {
    BeforeAll {
        . ([scriptblock]::Create((script:Get-FunctionText $script:prePath 'Get-LlamaBuildRequirement')))
    }
    It 'knows every profile embedder_profile.py defines' {
        foreach ($k in $script:pyProfiles.Keys) { (Get-LlamaBuildRequirement -Profile $k).Known | Should -BeTrue -Because "$k needs a llama.cpp floor decision" }
    }
    It 'keeps b6384 for EmbeddingGemma-300m (and for no profile) and raises EmbeddingGemma-2 to b11452' {
        (Get-LlamaBuildRequirement -Profile 'egemma-300m').Floor | Should -Be 'b6384'
        (Get-LlamaBuildRequirement).Floor | Should -Be 'b6384'
        $eg2 = Get-LlamaBuildRequirement -Profile 'egemma2'
        $eg2.Floor | Should -Be 'b11452'
        $eg2.Arch | Should -Be 'gemma-embedding2'
        $script:pySource | Should -Match 'b11452' -Because 'embedder_profile.py documents the floor the 0-prereqs table repeats'
    }
    It 'gives a profile it does not list the newest floor rather than the oldest' {
        $u = Get-LlamaBuildRequirement -Profile 'future-profile'
        $u.Known | Should -BeFalse
        $u.Floor | Should -Be 'b11452'
    }
    It 'keeps the original hint wording for the 300m and runs under Windows PowerShell 5.1 syntax' {
        $pre = Get-Content -LiteralPath $script:prePath -Raw -Encoding UTF8
        $pre | Should -Match 'build llama\.cpp >= \$llamaFloor, download the two GGUFs, config \+ systemd unit \+ verify'
        $pre | Should -Not -Match '\?\?|\?\.' -Because 'phase 0 runs before pwsh is guaranteed'
    }
}

Describe 'install/3-verify.ps1: the embedder check follows the authority embedding profile' {
    BeforeAll {
        $script:verCode = script:Get-CodeText $script:verPath
        . ([scriptblock]::Create((script:Get-FunctionText $script:verPath 'Get-VerifyEmbedProfilePy')))
        . ([scriptblock]::Create((script:Get-FunctionText $script:verPath 'Get-VerifyEmbedder')))
        function script:Probe-Url { param([string]$Uri, [int]$Attempts = 2, [int]$TimeoutSec = 10) $script:hd }
        $script:wslLines = @()
        $script:wslCalls = [System.Collections.Generic.List[string]]::new()
        function script:wsl.exe { $script:wslCalls.Add(($args -join ' ')); $script:wslLines }
    }
    BeforeEach { $script:wslCalls.Clear(); $script:hd = $null; $script:wslLines = @() }

    It 'carries no model alias, collection or bare width in the check' {
        $script:verCode | Should -Not -Match "model='embeddinggemma'" -Because 'the alias comes from the authority binding'
        $script:verCode | Should -Not -Match '\.Count -eq 768' -Because 'the width comes from the authority binding'
        $script:verCode | Should -Match 'Check "EmbeddingGemma :11436 \(\$embedWho\)"'
    }
    It 'on the brain checks the alias the authority server embeds with, at the width it reports' {
        $script:hd = [pscustomobject]@{ embed_profile = [pscustomobject]@{ profile = 'egemma2'; model = 'embeddinggemma2-ams' }
                                        checks = [pscustomobject]@{ embedder = [pscustomobject]@{ ok = $true; dim = 768 } } }
        $script:wslLines = @('VERIFY-EMBED|egemma2|embeddinggemma2|768')
        $b = Get-VerifyEmbedder -AuthorityUrl 'http://authority.invalid:18791' -Role 'brain' -Distro 'd' -WslUser 'u'
        $b.Profile | Should -Be 'egemma2'; $b.Alias | Should -Be 'embeddinggemma2-ams'; $b.Dim | Should -Be 768
    }
    It 'on a replica checks the alias THIS box resolves for the authority profile' {
        $script:hd = [pscustomobject]@{ embed_profile = [pscustomobject]@{ profile = 'egemma2'; model = 'embeddinggemma2-ams' }
                                        checks = [pscustomobject]@{ embedder = [pscustomobject]@{ ok = $true; dim = 768 } } }
        $script:wslLines = @('VERIFY-EMBED|egemma2|embeddinggemma2|768')
        $b = Get-VerifyEmbedder -AuthorityUrl 'http://authority.invalid:18791' -Role 'replica' -Distro 'd' -WslUser 'u'
        $b.Alias | Should -Be 'embeddinggemma2'
        $script:wslCalls[0] | Should -Match 'MEM0_EMBED_PROFILE=egemma2 '
    }
    It 'fails to resolve (instead of guessing an alias) when a replica cannot ask its own venv' {
        $script:hd = [pscustomobject]@{ embed_profile = [pscustomobject]@{ profile = 'egemma2'; model = 'embeddinggemma2-ams' }
                                        checks = [pscustomobject]@{ embedder = [pscustomobject]@{ ok = $true; dim = 768 } } }
        Get-VerifyEmbedder -AuthorityUrl 'http://authority.invalid:18791' -Role 'replica' -Distro 'd' -WslUser 'u' | Should -BeNullOrEmpty
    }
    It 'takes the width from the venv when the authority embedder was not ok' {
        $script:hd = [pscustomobject]@{ embed_profile = [pscustomobject]@{ profile = 'egemma-300m'; model = 'embeddinggemma' }
                                        checks = [pscustomobject]@{ embedder = [pscustomobject]@{ ok = $false; dim = 0 } } }
        $script:wslLines = @('VERIFY-EMBED|egemma-300m|embeddinggemma|768')
        (Get-VerifyEmbedder -AuthorityUrl 'u' -Role 'brain' -Distro 'd' -WslUser 'u').Dim | Should -Be 768
    }
    It 'asks this box venv when the authority reports no binding, and resolves nothing when that fails too' {
        $script:hd = [pscustomobject]@{ checks = [pscustomobject]@{ embedder = [pscustomobject]@{ ok = $true; dim = 768 } } }
        $script:wslLines = @('VERIFY-EMBED|egemma-300m|embeddinggemma|768')
        (Get-VerifyEmbedder -AuthorityUrl 'u' -Role 'brain' -Distro 'd' -WslUser 'u').Alias | Should -Be 'embeddinggemma'
        $script:wslLines = @()
        Get-VerifyEmbedder -AuthorityUrl 'u' -Role 'brain' -Distro 'd' -WslUser 'u' | Should -BeNullOrEmpty
    }
    It 'never interpolates an odd profile name into the shell line' {
        Get-VerifyEmbedProfilePy -Distro 'd' -WslUser 'u' -Profile 'x; reboot' | Should -BeNullOrEmpty
        $script:wslCalls.Count | Should -Be 0
    }
}
