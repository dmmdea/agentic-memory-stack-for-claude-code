# DreamGateVerdict.Tests.ps1 — unit coverage for Get-PromotionGateVerdict, the 4C live
# orchestration (autopromote-lib.ps1), with Qdrant (Invoke-RestMethod) and the Codex
# helpers mocked. Closes the E-audit MED ("Get-PromotionGateVerdict has zero coverage").
#
# Mock discriminators (match the production calls):
#   Uri .../points        -> candidate payload (user_id + re-observation signal)
#   Uri .../points/query  + body "with_payload":false -> sibling corroboration query
#   Uri .../points/query  + body "with_payload":true  -> nearest-canonical query
# The Codex helpers (Invoke-CodexSubagent / Get-CodexResponseText / Parse-CodexTokenUsage)
# come from memory-common.ps1 at dream runtime; here they are stubbed then mocked.

BeforeAll {
    . (Join-Path (Split-Path -Parent $PSScriptRoot) 'autopromote-lib.ps1')
    function Invoke-CodexSubagent { param($Prompt, $ReasoningEffort, $TimeoutSeconds) }
    function Get-CodexResponseText { param($RawOutput) }
    function Parse-CodexTokenUsage { param($RawOutput) }

    # v1.12 F1 sends every Qdrant -Body as UTF-8 BYTES (PS 5.1 Latin-1 fix). A mock
    # discriminator doing `$Body -match '...'` therefore never matches (regex vs byte[]).
    # Decode first — this is what broke 3 of these tests silently after v1.12.
    function _bodyText($Body) {
        if ($Body -is [byte[]]) { return [System.Text.Encoding]::UTF8.GetString($Body) }
        return [string]$Body
    }

    function _ev([string]$source) {
        $md = if ($source) { [pscustomobject]@{ source = $source } } else { [pscustomobject]@{} }
        [pscustomobject]@{ metadata = $md }
    }
    function _candResp {
        [pscustomobject]@{ result = @([pscustomobject]@{ payload = [pscustomobject]@{
            user_id = 'youruser'; created_at = '2026-06-01T00:00:00'; updated_at = '2026-06-01T00:00:00' } }) }
    }
    function _siblings([double[]]$scores) {
        [pscustomobject]@{ result = [pscustomobject]@{ points = @($scores | ForEach-Object { [pscustomobject]@{ score = $_ } }) } }
    }
    function _canon([string[]]$texts) {
        [pscustomobject]@{ result = [pscustomobject]@{ points = @($texts | ForEach-Object { [pscustomobject]@{ payload = [pscustomobject]@{ data = $_ } } }) } }
    }
}

Describe 'Get-PromotionGateVerdict -- contradiction blocks (Codex says contradicts)' {
    It 'BLOCKS when the contradiction judge returns contradicts=true' {
        Mock Invoke-RestMethod {
            if ($Uri -match '/points$') { return _candResp }
            if ((_bodyText $Body) -match '"with_payload":false') { return _siblings @(0.8) }
            return _canon @('reserved ports are 9090')
        }
        Mock Invoke-CodexSubagent { 'raw' }
        Mock Get-CodexResponseText { '{"contradicts": true, "canonical": "reserved ports are 9090"}' }
        Mock Parse-CodexTokenUsage { 0 }
        $r = Get-PromotionGateVerdict -MemoryId 'm1' -CandidateText 'reserved ports are 8080' -EvidenceRecord (_ev 'l1a-extractor')
        $r.contradicts | Should -BeTrue
        $r.gate.promote | Should -BeFalse
        $r.gate.gateClass | Should -Be 'contradiction'
    }
}

Describe 'Get-PromotionGateVerdict -- corroborated untrusted promotes (no contradiction)' {
    It 'PROMOTES an untrusted fact with N>=2 siblings and no contradiction' {
        Mock Invoke-RestMethod {
            if ($Uri -match '/points$') { return _candResp }
            if ((_bodyText $Body) -match '"with_payload":false') { return _siblings @(0.8, 0.75) }  # 2 siblings >=0.6 -> N=3
            return _canon @('some canonical fact')
        }
        Mock Invoke-CodexSubagent { 'raw' }
        Mock Get-CodexResponseText { '{"contradicts": false}' }
        Mock Parse-CodexTokenUsage { 0 }
        $r = Get-PromotionGateVerdict -MemoryId 'm2' -CandidateText 'an inferred fact' -EvidenceRecord (_ev 'l1a-extractor')
        $r.contradicts | Should -BeFalse
        $r.corroborationCount | Should -BeGreaterOrEqual 2
        $r.gate.promote | Should -BeTrue
        $r.gate.gateClass | Should -Be 'corroborated'
    }
}

Describe 'Get-PromotionGateVerdict -- trusted source fast-tracks (no corroboration needed)' {
    It 'PROMOTES a trusted (operator) fact with zero siblings, no contradiction' {
        Mock Invoke-RestMethod {
            if ($Uri -match '/points$') { return _candResp }
            if ((_bodyText $Body) -match '"with_payload":false') { return _siblings @() }  # N=1
            return _canon @('a canonical fact')
        }
        Mock Invoke-CodexSubagent { 'raw' }
        Mock Get-CodexResponseText { '{"contradicts": false}' }
        Mock Parse-CodexTokenUsage { 0 }
        $r = Get-PromotionGateVerdict -MemoryId 'm3' -CandidateText 'operator asserted fact' -EvidenceRecord (_ev 'operator-decision')
        $r.sourceClass | Should -Be 'trusted'
        $r.gate.promote | Should -BeTrue
        $r.gate.gateClass | Should -Be 'trusted-source'
    }
}

Describe 'Get-PromotionGateVerdict -- uncorroborated untrusted blocks' {
    It 'BLOCKS an untrusted fact with N=1 and no contradiction' {
        Mock Invoke-RestMethod {
            if ($Uri -match '/points$') { return _candResp }
            if ((_bodyText $Body) -match '"with_payload":false') { return _siblings @() }  # N=1
            return _canon @('a canonical fact')
        }
        Mock Invoke-CodexSubagent { 'raw' }
        Mock Get-CodexResponseText { '{"contradicts": false}' }
        Mock Parse-CodexTokenUsage { 0 }
        $r = Get-PromotionGateVerdict -MemoryId 'm4' -CandidateText 'a lone inferred fact' -EvidenceRecord (_ev 'l1a-extractor')
        $r.gate.promote | Should -BeFalse
        $r.gate.gateClass | Should -Be 'uncorroborated'
    }
}

Describe 'Get-PromotionGateVerdict -- canonical-fetch ERROR fails safe (contradicts=true)' {
    It 'forces contradicts=true/parsed=false when the near-canonical query throws' {
        Mock Invoke-RestMethod {
            if ($Uri -match '/points$') { return _candResp }
            if ((_bodyText $Body) -match '"with_payload":false') { return _siblings @(0.9, 0.9) }  # would be corroborated
            throw 'qdrant canonical fetch failed'
        }
        Mock Invoke-CodexSubagent { 'raw' }
        Mock Get-CodexResponseText { '{"contradicts": false}' }
        Mock Parse-CodexTokenUsage { 0 }
        $r = Get-PromotionGateVerdict -MemoryId 'm5' -CandidateText 'a fact' -EvidenceRecord (_ev 'l1a-extractor')
        $r.contradicts | Should -BeTrue -Because 'a failed canonical fetch must not be read as no-conflict'
        $r.contradictionParsed | Should -BeFalse
        $r.gate.promote | Should -BeFalse
        Should -Invoke Invoke-CodexSubagent -Times 0 -Because 'no canonical texts were fetched, so the judge is not called'
    }
}

Describe 'Get-PromotionGateVerdict -- no nearby canonicals => no contradiction, judge not called' {
    It 'treats zero near-canonicals as no-contradiction (promotes if corroborated)' {
        Mock Invoke-RestMethod {
            if ($Uri -match '/points$') { return _candResp }
            if ((_bodyText $Body) -match '"with_payload":false') { return _siblings @(0.8, 0.8) }
            return _canon @()   # canon fetch OK, but no canonicals near
        }
        Mock Invoke-CodexSubagent { 'raw' }
        Mock Get-CodexResponseText { '{"contradicts": true}' }  # should NOT be consulted
        Mock Parse-CodexTokenUsage { 0 }
        $r = Get-PromotionGateVerdict -MemoryId 'm6' -CandidateText 'a fact with no canonical neighbor' -EvidenceRecord (_ev 'l1a-extractor')
        $r.contradicts | Should -BeFalse
        $r.gate.promote | Should -BeTrue
        Should -Invoke Invoke-CodexSubagent -Times 0
    }
}

Describe 'Get-PromotionGateVerdict -- retry-once on an unparseable verdict (over-block fix)' {
    It 'RECOVERS when the first judge reply is unparseable but the retry parses' {
        $script:gcrt = 0
        Mock Invoke-RestMethod {
            if ($Uri -match '/points$') { return _candResp }
            if ((_bodyText $Body) -match '"with_payload":false') { return _siblings @(0.8, 0.8) }
            return _canon @('a canonical fact')
        }
        Mock Invoke-CodexSubagent { 'raw' }
        Mock Get-CodexResponseText { $script:gcrt++; if ($script:gcrt -eq 1) { 'sorry, no json' } else { '{"contradicts": false}' } }
        Mock Parse-CodexTokenUsage { 0 }
        $r = Get-PromotionGateVerdict -MemoryId 'm7' -CandidateText 'a fact' -EvidenceRecord (_ev 'l1a-extractor')
        $r.contradictionParsed | Should -BeTrue
        $r.contradicts | Should -BeFalse
        $r.gate.promote | Should -BeTrue
        Should -Invoke Invoke-CodexSubagent -Times 2 -Because 'an unparseable first verdict triggers exactly one retry'
    }
    It 'stays FAIL-SAFE (block) when both the first verdict and the retry are unparseable' {
        Mock Invoke-RestMethod {
            if ($Uri -match '/points$') { return _candResp }
            if ((_bodyText $Body) -match '"with_payload":false') { return _siblings @(0.8, 0.8) }
            return _canon @('a canonical fact')
        }
        Mock Invoke-CodexSubagent { 'raw' }
        Mock Get-CodexResponseText { 'still no json' }
        Mock Parse-CodexTokenUsage { 0 }
        $r = Get-PromotionGateVerdict -MemoryId 'm8' -CandidateText 'a fact' -EvidenceRecord (_ev 'operator-decision')
        $r.contradicts | Should -BeTrue
        $r.contradictionParsed | Should -BeFalse
        $r.gate.promote | Should -BeFalse -Because 'an unverifiable candidate is blocked even for a trusted source'
    }
}
