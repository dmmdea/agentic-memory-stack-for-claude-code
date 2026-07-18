#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# UserPromptExtract.Tests.ps1 — v0.18 MED-20/21: Pester 5 tests for the
# UserPromptSubmit hook logic (user-prompt-extract.ps1).
#
# The hook script executes its pipeline on load, so the testable logic lives in
# scripts/windows/user-prompt-lib.ps1 (dot-sourceable, no side effects at load):
#   - Test-DecisionLikePrompt        (Phase 0.B decision-capture predicate)
#   - Select-AdmittedMemoryResults   (Phase 0.D proactive-injection admission)
#
# Run: pwsh -NoProfile -Command "Invoke-Pester D:\repos\agentic-memory-stack\scripts\windows\tests\ -Output Detailed"

BeforeAll {
    $libPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'user-prompt-lib.ps1'
    if (-not (Test-Path $libPath)) { throw "user-prompt-lib.ps1 not found at $libPath" }
    . $libPath
}

Describe 'Step 1 (2026-06-30) correction-capture' {

    It 'Test-CorrectionLikePrompt matches correction "<_>"' -ForEach @(
        "no, that's wrong",
        "you forgot to run the tests",
        "revert that",
        "that's not what I asked",
        "wrong file - use the other one",
        "nope, undo it",
        "actually, I meant the reranker not the embedder",
        "don't do that again",
        "roll back the last change"
    ) {
        Test-CorrectionLikePrompt -Prompt $_ | Should -BeTrue
    }

    It 'Test-CorrectionLikePrompt does NOT match ordinary prompt "<_>"' -ForEach @(
        'add a login page',
        'run the tests please',
        'what is the memory stack for',
        'yes',
        'no worries, take your time',
        'stop the server gracefully',
        'wait for the build to finish',
        'the wrong-answer rate improved'
    ) {
        Test-CorrectionLikePrompt -Prompt $_ | Should -BeFalse
    }

    It 'Add-LearnRuleCapture appends a well-formed record and skips blank prompts (fail-open)' {
        $tmp = Join-Path $env:TEMP ("learn-rules-test-" + [guid]::NewGuid().ToString('N') + ".jsonl")
        try {
            (Add-LearnRuleCapture -Prompt "no that's wrong, use bge-reranker-v2-m3" -SessionId 'sid-x' -Brand 'ai-ecosystem' -QueuePath $tmp) | Should -BeTrue
            (Add-LearnRuleCapture -Prompt '   ' -QueuePath $tmp) | Should -BeFalse
            $lines = @(Get-Content -LiteralPath $tmp)
            $lines.Count   | Should -Be 1
            $rec = $lines[0] | ConvertFrom-Json
            $rec.kind       | Should -Be 'correction'
            $rec.status     | Should -Be 'pending'
            $rec.brand      | Should -Be 'ai-ecosystem'
            $rec.correction | Should -Match 'bge-reranker'
        } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
}

Describe 'Phase 0.B decision-capture predicate (Test-DecisionLikePrompt)' {

    It 'matches decision-like prompt "<_>"' -ForEach @(
        'yes',
        'no',
        '1 and 2',
        'go ahead',
        '1',
        '2',
        'lets do 2 and 3 for the deploy please'   # mid-sentence "N and M" decision
    ) {
        Test-DecisionLikePrompt -Prompt $_ | Should -BeTrue
    }

    It 'does NOT match non-decision prompt "<_>"' -ForEach @(
        'thanks',
        'ok cool'
    ) {
        Test-DecisionLikePrompt -Prompt $_ | Should -BeFalse
    }

    It 'rejects long prompts even if they contain a decision pattern (>25 words)' {
        $long = (1..24 -join ' ') + ' choose 1 and 2 maybe but this sentence rambles on'
        Test-DecisionLikePrompt -Prompt $long | Should -BeFalse
    }

    It 'rejects empty / whitespace prompts' {
        Test-DecisionLikePrompt -Prompt ''   | Should -BeFalse
        Test-DecisionLikePrompt -Prompt '  ' | Should -BeFalse
    }
}

Describe 'Phase 0.D proactive-injection admission (Select-AdmittedMemoryResults)' {

    BeforeEach {
        # Crafted mem0 search response (shape of POST /v1/memories/search .results),
        # mixing canonical-tier, cross-brand, evidence/stable, and legacy null entries.
        # Unique audit file per test: TestDrive contents persist across Its within
        # a Describe in Pester 5, and the audit log is append-only.
        $script:auditPath = Join-Path $TestDrive ("admission-rejected-{0}.jsonl" -f ([guid]::NewGuid().ToString('N')))
        $script:hits = @(
            [pscustomobject]@{ id = 'm-canonical';  memory = 'canonical directive — must never surface via injection'
                               metadata = [pscustomobject]@{ tier = 'canonical'; brand = 'ai-ecosystem' } }
            [pscustomobject]@{ id = 'm-crossbrand'; memory = 'brand-a fact — wrong brand for this prompt'
                               metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'brand-a' } }
            [pscustomobject]@{ id = 'm-evidence';   memory = 'evidence fact — brand match'
                               metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
            [pscustomobject]@{ id = 'm-stable';     memory = 'stable fact — null brand (legacy, accepted)'
                               metadata = [pscustomobject]@{ tier = 'stable'; brand = $null } }
            [pscustomobject]@{ id = 'm-nulltier';   memory = 'legacy null-tier fact (admitted per Phase C semantics)'
                               metadata = [pscustomobject]@{ tier = $null; brand = $null } }
        )
        # Simulate the hook's call path: results come back from a (mocked) mem0 search
        Mock Invoke-RestMethod { [pscustomobject]@{ results = $script:hits } }
        $script:searchResponse = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories/search' -Method Post
    }

    It 'surfaces only evidence/stable + brand-matching (or null-brand) results' {
        $admitted = @(Select-AdmittedMemoryResults -Hits @($script:searchResponse.results) -Brand 'ai-ecosystem' -AuditPath $script:auditPath)
        @($admitted | ForEach-Object id) | Should -Be @('m-evidence', 'm-stable', 'm-nulltier')
    }

    It 'filters canonical out' {
        $admitted = @(Select-AdmittedMemoryResults -Hits @($script:searchResponse.results) -Brand 'ai-ecosystem' -AuditPath $script:auditPath)
        @($admitted | ForEach-Object id) | Should -Not -Contain 'm-canonical'
    }

    It 'filters cross-brand out when a brand is inferred' {
        $admitted = @(Select-AdmittedMemoryResults -Hits @($script:searchResponse.results) -Brand 'ai-ecosystem' -AuditPath $script:auditPath)
        @($admitted | ForEach-Object id) | Should -Not -Contain 'm-crossbrand'
    }

    It 'writes admission-rejected.jsonl entries with the right reasons' {
        $null = Select-AdmittedMemoryResults -Hits @($script:searchResponse.results) -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        Test-Path $script:auditPath | Should -BeTrue
        $entries = Get-Content $script:auditPath | ForEach-Object { $_ | ConvertFrom-Json }
        @($entries).Count | Should -Be 2
        @($entries | ForEach-Object memory_id) | Should -Be @('m-canonical', 'm-crossbrand')
        ($entries | Where-Object memory_id -eq 'm-canonical').reason  | Should -Be 'tier_disallowed:canonical'
        ($entries | Where-Object memory_id -eq 'm-crossbrand').reason | Should -Be 'brand_mismatch:brand-a_vs_ai-ecosystem'
        @($entries | ForEach-Object layer) | Should -Be @('phase-0d-client', 'phase-0d-client')
        # v0.19 L6: client entries carry the same schema_version as the server writer
        @($entries | ForEach-Object schema_version) | Should -Be @('v18', 'v18')
    }

    It 'rotates the audit log at 10MB to a single .1 backup (v0.19 L5)' {
        # Pre-create an oversized audit file (sparse — instant, no real 10MB write)
        $fs = [System.IO.File]::Create($script:auditPath)
        $fs.SetLength(10MB + 1)
        $fs.Close()
        $null = Select-AdmittedMemoryResults -Hits @($script:searchResponse.results) -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        Test-Path "$($script:auditPath).1" | Should -BeTrue
        (Get-Item "$($script:auditPath).1").Length | Should -BeGreaterThan 10MB
        # Fresh post-rotation file holds only this call's rejections
        $entries = Get-Content $script:auditPath | ForEach-Object { $_ | ConvertFrom-Json }
        @($entries).Count | Should -Be 2
        @($entries | ForEach-Object schema_version) | Should -Be @('v18', 'v18')
    }

    It 'caps surfaced results at 3 and logs overflow as truncated_by_topN' {
        $extra = @(1..3 | ForEach-Object {
            [pscustomobject]@{ id = "m-extra-$_"; memory = "extra evidence fact $_"
                               metadata = [pscustomobject]@{ tier = 'evidence'; brand = $null } }
        })
        $admitted = @(Select-AdmittedMemoryResults -Hits (@($script:hits) + $extra) -Brand 'ai-ecosystem' -AuditPath $script:auditPath)
        $admitted.Count | Should -Be 3
        $entries = Get-Content $script:auditPath | ForEach-Object { $_ | ConvertFrom-Json }
        @($entries | Where-Object reason -eq 'truncated_by_topN').Count | Should -Be 3
    }

    It 'truncates memories longer than 200 chars' {
        $longHit = [pscustomobject]@{ id = 'm-long'; memory = ('x' * 400)
                                      metadata = [pscustomobject]@{ tier = 'evidence'; brand = $null } }
        $admitted = @(Select-AdmittedMemoryResults -Hits @($longHit) -Brand 'ai-ecosystem' -AuditPath $script:auditPath)
        $admitted[0].memory.Length | Should -BeLessOrEqual (200 + '... [truncated by v0.18 admission policy]'.Length)
        $admitted[0].memory | Should -BeLike '*`[truncated by v0.18 admission policy`]'
    }

    It 'admits only brand-neutral memories when no brand is inferred (v0.19 fail-closed)' {
        $admitted = @(Select-AdmittedMemoryResults -Hits @($script:searchResponse.results) -AuditPath $script:auditPath)
        # v0.19 M5/M14/L10: unknown-brand session -> brand-tagged memories never
        # surface; only null-brand (brand-neutral) records pass Layer 2.
        @($admitted | ForEach-Object id) | Should -Be @('m-stable', 'm-nulltier')
    }

    It 'logs brand-related rejection reasons when brand inference failed (v0.19 fail-closed audit)' {
        $null = Select-AdmittedMemoryResults -Hits @($script:searchResponse.results) -AuditPath $script:auditPath
        $entries = Get-Content $script:auditPath | ForEach-Object { $_ | ConvertFrom-Json }
        ($entries | Where-Object memory_id -eq 'm-canonical').reason  | Should -Be 'tier_disallowed:canonical'
        ($entries | Where-Object memory_id -eq 'm-crossbrand').reason | Should -Be 'brand_unknown_session:brand-a'
        ($entries | Where-Object memory_id -eq 'm-evidence').reason   | Should -Be 'brand_unknown_session:ai-ecosystem'
    }

    It 'isolation negative test: failed inference + cross-brand evidence -> only null-brand surfaces' {
        # v0.19 M5/M14/L10 end-to-end shape: transcript path outside every known
        # brand -> Get-InferredBrandFromPath yields $null -> Layer 2 fails closed.
        $inferredBrand = Get-InferredBrandFromPath -Path 'C:\Users\youruser\.claude\projects\C--Users-youruser-some-client-folder\abc.jsonl'
        $inferredBrand | Should -BeNullOrEmpty
        $isoHits = @(
            [pscustomobject]@{ id = 'm-iso-crossbrand'; memory = 'brand-a evidence — must not leak into unknown-brand session'
                               metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'brand-a' } }
            [pscustomobject]@{ id = 'm-iso-neutral';    memory = 'brand-neutral fact — safe to surface anywhere'
                               metadata = [pscustomobject]@{ tier = 'evidence'; brand = $null } }
        )
        $admitted = @(Select-AdmittedMemoryResults -Hits $isoHits -Brand $inferredBrand -AuditPath $script:auditPath)
        @($admitted | ForEach-Object id) | Should -Be @('m-iso-neutral')
        $entries = Get-Content $script:auditPath | ForEach-Object { $_ | ConvertFrom-Json }
        ($entries | Where-Object memory_id -eq 'm-iso-crossbrand').reason | Should -Be 'brand_unknown_session:brand-a'
        ($entries | Where-Object memory_id -eq 'm-iso-crossbrand').layer  | Should -Be 'phase-0d-client'
    }

    It 'rejects cross-brand evidence under a KNOWN brand with brand_mismatch (M14 brand-c)' {
        $hit = @([pscustomobject]@{ id = 'm-rp'; memory = 'brand-a evidence'
                                    metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'brand-a' } })
        $admitted = @(Select-AdmittedMemoryResults -Hits $hit -Brand 'brand-c' -AuditPath $script:auditPath)
        $admitted.Count | Should -Be 0
        $entries = Get-Content $script:auditPath | ForEach-Object { $_ | ConvertFrom-Json }
        ($entries | Where-Object memory_id -eq 'm-rp').reason | Should -Be 'brand_mismatch:brand-a_vs_brand-c'
    }

    It 'treats empty-string memory brand as legacy/null on both guard branches (M14)' {
        $hits = @([pscustomobject]@{ id = 'm-emptybrand'; memory = 'empty-string brand fact'
                                     metadata = [pscustomobject]@{ tier = 'evidence'; brand = '' } })
        @((Select-AdmittedMemoryResults -Hits $hits -Brand 'ai-ecosystem' -AuditPath $script:auditPath) | ForEach-Object id) |
            Should -Be @('m-emptybrand')
        @((Select-AdmittedMemoryResults -Hits $hits -AuditPath $script:auditPath) | ForEach-Object id) |
            Should -Be @('m-emptybrand')
    }

    It 'treats whitespace-only memory brand as legacy/null on both guard branches (v0.20 Phase F M14)' {
        # v0.20 M14: IsNullOrWhiteSpace on the client mirrors the server gate's
        # strip-before-falsiness — '  ' behaves exactly like '' on both layers.
        $hits = @([pscustomobject]@{ id = 'm-wsbrand'; memory = 'whitespace-only brand fact'
                                     metadata = [pscustomobject]@{ tier = 'evidence'; brand = '  ' } })
        @((Select-AdmittedMemoryResults -Hits $hits -Brand 'ai-ecosystem' -AuditPath $script:auditPath) | ForEach-Object id) |
            Should -Be @('m-wsbrand')
        @((Select-AdmittedMemoryResults -Hits $hits -AuditPath $script:auditPath) | ForEach-Object id) |
            Should -Be @('m-wsbrand')
    }

    It 'returns an empty set for empty input without writing an audit file' {
        $admitted = @(Select-AdmittedMemoryResults -Hits @() -Brand 'ai-ecosystem' -AuditPath $script:auditPath)
        $admitted.Count | Should -Be 0
        Test-Path $script:auditPath | Should -BeFalse
    }
}

Describe 'v0.20 A.3 bundle rendering (Format-MemoryContextBlock)' {

    BeforeEach {
        $script:auditPath = Join-Path $TestDrive ("bundle-admission-{0}.jsonl" -f ([guid]::NewGuid().ToString('N')))
        # Shape of ONE POST /v1/context/bundle response (the hook's only call now)
        $script:bundle = [pscustomobject]@{
            ok        = $true
            checkpoint = [pscustomobject]@{ ok = $true; episode_id = 42; action = 'updated'; state = 'in_progress' }
            memories  = @(
                [pscustomobject]@{ id = 'm-canonical';  memory = 'canonical directive - must never surface via injection'
                                   metadata = [pscustomobject]@{ tier = 'canonical'; brand = 'ai-ecosystem' } }
                [pscustomobject]@{ id = 'm-crossbrand'; memory = 'brand-a fact - wrong brand'
                                   metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'brand-a' } }
                [pscustomobject]@{ id = 'm-evidence';   memory = 'evidence fact - brand match'
                                   metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
            )
            goals = @(
                [pscustomobject]@{ id = 1; title = 'Ship v0.20 memory stack'; priority = 2; status = 'open' }
                [pscustomobject]@{ id = 2; title = ('long goal title ' * 10); priority = 3; status = 'open' }
            )
            open_questions = @(
                [pscustomobject]@{ id = 7; question_text = 'Should the daemon stretch be built?' }
            )
        }
    }

    It 'renders the identical [MEMORY CONTEXT] block from a single mocked bundle response' {
        $block = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $block | Should -Not -BeNullOrEmpty
        $lines = $block -split "`n"
        $lines[0] | Should -Be '[MEMORY CONTEXT - auto-surfaced by user-prompt-extract.ps1 v0.17 Phase 0.D]'
        $block | Should -Match 'Top 1 relevant memories:'
        $block | Should -Match '\[evidence\|ai-ecosystem\] evidence fact - brand match'
        $block | Should -Match 'Open goals \(2 shown\):'
        $block | Should -Match '\[P2 OPEN\] Ship v0\.20 memory stack'
        $block | Should -Match 'Open frontier questions:'
        $block | Should -Match 'Should the daemon stretch be built\?'
    }

    It 'v1.0 R6 placement: memory section renders LAST (after goals/OQs) with the most-relevant memory adjacent to the prompt' {
        $b = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true }
            memories = @(
                [pscustomobject]@{ id = 'm-best'; memory = 'BEST most-relevant fact'
                                   metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
                [pscustomobject]@{ id = 'm-2nd';  memory = 'SECOND less-relevant fact'
                                   metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
            )
            goals = @([pscustomobject]@{ id = 1; title = 'a goal'; priority = 2; status = 'open' })
            open_questions = @([pscustomobject]@{ id = 7; question_text = 'an open question?' })
        }
        $block = Format-MemoryContextBlock -Bundle $b -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        # the LITERAL last line (RAW, not blank-stripped) is the single most-relevant memory — this
        # pins "no trailing blank after the memory section" so it is adjacent to the prompt (R6).
        $rawLines = $block -split "`n"
        $rawLines[-1] | Should -Match 'BEST most-relevant fact'
        $rawLines[-1] | Should -Not -Match '^\s*$'   # not a trailing blank
        # the memory section comes AFTER goals + open-questions (they sit in the low-attention middle)
        $block.IndexOf('Top 2 relevant memories:') | Should -BeGreaterThan $block.IndexOf('Open goals')
        $block.IndexOf('Top 2 relevant memories:') | Should -BeGreaterThan $block.IndexOf('Open frontier questions:')
        # the 2nd-best memory precedes the best (best is reversed to last)
        $block.IndexOf('SECOND less-relevant fact') | Should -BeLessThan $block.IndexOf('BEST most-relevant fact')
    }

    It 'still applies client-side admission: canonical + cross-brand never rendered' {
        $block = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $block | Should -Not -Match 'canonical directive'
        $block | Should -Not -Match 'brand-a fact'
    }

    It 'rejection logging still works (admission-rejected.jsonl entries from bundle memories)' {
        $null = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        Test-Path $script:auditPath | Should -BeTrue
        $entries = Get-Content $script:auditPath | ForEach-Object { $_ | ConvertFrom-Json }
        @($entries | ForEach-Object memory_id) | Should -Be @('m-canonical', 'm-crossbrand')
        ($entries | Where-Object memory_id -eq 'm-canonical').reason  | Should -Be 'tier_disallowed:canonical'
        ($entries | Where-Object memory_id -eq 'm-crossbrand').reason | Should -Be 'brand_mismatch:brand-a_vs_ai-ecosystem'
        @($entries | ForEach-Object layer) | Should -Be @('phase-0d-client', 'phase-0d-client')
    }

    It 'truncates goal titles at 100 chars and question text at 120 chars' {
        $block = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $goalLine = ($block -split "`n") | Where-Object { $_ -match 'long goal title' }
        $goalLine | Should -Match '\.\.\.$'
    }

    It 'returns $null when the bundle has no substantive content' {
        $empty = [pscustomobject]@{ ok = $true; checkpoint = @{ ok = $true }; memories = @(); goals = @(); open_questions = @() }
        Format-MemoryContextBlock -Bundle $empty -Brand 'ai-ecosystem' -AuditPath $script:auditPath | Should -BeNullOrEmpty
    }

    It 'returns $null for a null bundle (failed call)' {
        Format-MemoryContextBlock -Bundle $null -Brand 'ai-ecosystem' -AuditPath $script:auditPath | Should -BeNullOrEmpty
    }

    # --- v1.0 Phase 3 / R2: abstention-first block-level gating ---
    # The [MEMORY CONTEXT] block now renders ONLY when >=1 memory clears the
    # relevance gate. Open goals / open frontier questions no longer
    # static-prepend on their own — that was the paper's #2 anti-pattern (static
    # 63.82% premature). When the bundle's memories are empty/all-rejected, the
    # WHOLE block abstains (NOOP), goals/OQ notwithstanding.
    It 'v1.0 R2 abstention: returns $null when NO memory clears, even with open goals + OQ present' {
        $goalsOnly = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true }
            memories = @()
            goals = @([pscustomobject]@{ id = 1; title = 'Ship v0.26 memory stack'; priority = 2; status = 'open' })
            open_questions = @([pscustomobject]@{ id = 7; question_text = 'pick the abstain threshold?' })
        }
        Format-MemoryContextBlock -Bundle $goalsOnly -Brand 'ai-ecosystem' -AuditPath $script:auditPath | Should -BeNullOrEmpty
    }

    It 'v1.0 R2 abstention: abstains when every bundle memory is admission-rejected (canonical/cross-brand), goals notwithstanding' {
        $rejected = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true }
            memories = @(
                [pscustomobject]@{ id = 'm-can'; memory = 'canonical directive - never via injection'; metadata = [pscustomobject]@{ tier = 'canonical'; brand = 'ai-ecosystem' } }
                [pscustomobject]@{ id = 'm-rp';  memory = 'brand-a fact - wrong brand'; metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'brand-a' } }
            )
            goals = @([pscustomobject]@{ id = 1; title = 'Ship v0.26 memory stack'; priority = 2; status = 'open' })
            open_questions = @()
        }
        Format-MemoryContextBlock -Bundle $rejected -Brand 'ai-ecosystem' -AuditPath $script:auditPath | Should -BeNullOrEmpty
    }

    It 'v1.0 R2: renders the block (memory + goals + OQ) when >=1 memory is admitted' {
        $oneMem = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true }
            memories = @([pscustomobject]@{ id = 'm-ev'; memory = 'evidence fact - relevant'; metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } })
            goals = @([pscustomobject]@{ id = 1; title = 'Ship v0.26 memory stack'; priority = 2; status = 'open' })
            open_questions = @([pscustomobject]@{ id = 7; question_text = 'pick the abstain threshold?' })
        }
        $block = Format-MemoryContextBlock -Bundle $oneMem -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $block | Should -Not -BeNullOrEmpty
        $block | Should -Match 'Top 1 relevant memories:'
        $block | Should -Match 'evidence fact - relevant'
        $block | Should -Match 'Open goals \(1 shown\):'
        $block | Should -Match 'Open frontier questions:'
    }

    # --- v1.0 Phase 6 / R4: low-confidence raw-trace fallback rendering ---
    # When NO condensed memory clears the gate, the server may surface ONE strict-
    # match past episode (raw_fallback). The hook renders it as a single advisory
    # line INSTEAD OF abstaining — but ONLY that line (R2-faithful: goals/OQ never
    # piggyback on a no-memory turn), and only when the brand gate passes.
    It 'v1.0 R4 raw-trace fallback: renders the episode snippet when NO memory clears but raw_fallback is present' {
        $rf = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true }
            memories = @()
            goals = @(); open_questions = @()
            raw_fallback = [pscustomobject]@{ episode_id = 42; brand = $null; snippet = 'Investigate the deploy pipeline failure — traced the crash to a missing env var.' }
        }
        $block = Format-MemoryContextBlock -Bundle $rf -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $block | Should -Not -BeNullOrEmpty
        $block | Should -Match 'Related past work \(episode 42\):'
        $block | Should -Match 'deploy pipeline failure'
    }

    It 'v1.0 R4: raw-trace fallback does NOT piggyback goals/OQ (R2-faithful — only the snippet line renders)' {
        $rf = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true }
            memories = @()
            goals = @([pscustomobject]@{ id = 1; title = 'Ship v0.29 R4'; priority = 2; status = 'open' })
            open_questions = @([pscustomobject]@{ id = 7; question_text = 'pick the rank floor?' })
            raw_fallback = [pscustomobject]@{ episode_id = 9; brand = $null; snippet = 'Past episode about the extraction gate.' }
        }
        $block = Format-MemoryContextBlock -Bundle $rf -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $block | Should -Match 'Related past work \(episode 9\):'
        $block | Should -Not -Match 'Open goals'
        $block | Should -Not -Match 'Open frontier questions'
    }

    It 'v1.0 R4: when a memory clears, raw_fallback is ignored (real condensed memories preferred)' {
        $rf = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true }
            memories = @([pscustomobject]@{ id = 'm-ev'; memory = 'evidence fact - relevant'; metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } })
            goals = @(); open_questions = @()
            raw_fallback = [pscustomobject]@{ episode_id = 99; brand = $null; snippet = 'Should not appear because a memory cleared.' }
        }
        $block = Format-MemoryContextBlock -Bundle $rf -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $block | Should -Match 'evidence fact - relevant'
        $block | Should -Not -Match 'Related past work'
    }

    It 'v1.0 R4: raw-trace fallback respects the brand gate — a branded episode never renders in an unknown-brand session' {
        $rf = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true }
            memories = @(); goals = @(); open_questions = @()
            raw_fallback = [pscustomobject]@{ episode_id = 5; brand = 'brand-a'; snippet = 'Brand-A-specific past work that must not leak.' }
        }
        # Unknown-brand session ($Brand = $null): the client backstop drops the branded fallback.
        Format-MemoryContextBlock -Bundle $rf -Brand $null -AuditPath $script:auditPath | Should -BeNullOrEmpty
    }

    It 'v1.0 R4: an empty/whitespace fallback snippet does not render the block' {
        $rf = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true }
            memories = @(); goals = @(); open_questions = @()
            raw_fallback = [pscustomobject]@{ episode_id = 3; brand = $null; snippet = '   ' }
        }
        Format-MemoryContextBlock -Bundle $rf -Brand 'ai-ecosystem' -AuditPath $script:auditPath | Should -BeNullOrEmpty
    }

    It 'v1.0 R2: abstention gate and brand gate compose — admitted same-brand memory fires the block; wrong-brand memory AND goal are filtered independently' {
        # The abstention gate keys on the ADMITTED set (post brand+tier filtering),
        # and the per-row brand gate still drops wrong-brand memories/goals. So a
        # same-brand memory fires the block, while a cross-brand memory and a
        # cross-brand goal in the same bundle are filtered out — no leak, no
        # spurious abstain.
        $mixed = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true }
            memories = @(
                [pscustomobject]@{ id = 'm-ai'; memory = 'ai-ecosystem evidence fact'; metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
                [pscustomobject]@{ id = 'm-rp'; memory = 'brand-a cross-brand fact';  metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'brand-a' } }
            )
            goals = @(
                [pscustomobject]@{ id = 1; title = 'ai-ecosystem goal'; priority = 2; status = 'open'; brand = 'ai-ecosystem' }
                [pscustomobject]@{ id = 2; title = 'brand-a goal';     priority = 2; status = 'open'; brand = 'brand-a' }
            )
            open_questions = @()
        }
        $block = Format-MemoryContextBlock -Bundle $mixed -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $block | Should -Not -BeNullOrEmpty
        $block | Should -Match 'ai-ecosystem evidence fact'
        $block | Should -Not -Match 'brand-a cross-brand fact'
        $block | Should -Match 'ai-ecosystem goal'
        $block | Should -Not -Match 'brand-a goal'
    }

    It 'renders from dictionary-shaped wire data (the hook parses JSON to dictionaries, not PSObjects)' {
        # v0.20 A.3: the 5.1 hook deserializes via JavaScriptSerializer ->
        # Dictionary<string,object>. ConvertFrom-Json -AsHashtable is the
        # closest pwsh-7 proxy for that shape; property-style access on
        # dictionaries must keep working through the whole render+admission path.
        $json = '{"ok":true,"checkpoint":{"ok":true,"episode_id":9,"action":"created"},' +
                '"memories":[{"id":"m-d1","memory":"dict-shaped evidence fact","metadata":{"tier":"evidence","brand":null}},' +
                '{"id":"m-d2","memory":"dict-shaped canonical","metadata":{"tier":"canonical","brand":null}}],' +
                '"goals":[{"id":3,"title":"dict goal","priority":1,"status":"open"}],' +
                '"open_questions":[{"id":4,"question_text":"dict question?"}]}'
        $dictBundle = $json | ConvertFrom-Json -AsHashtable
        $block = Format-MemoryContextBlock -Bundle $dictBundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $block | Should -Match 'dict-shaped evidence fact'
        $block | Should -Not -Match 'dict-shaped canonical'
        $block | Should -Match '\[P1 OPEN\] dict goal'
        $block | Should -Match 'dict question\?'
        $entries = Get-Content $script:auditPath | ForEach-Object { $_ | ConvertFrom-Json }
        ($entries | Where-Object memory_id -eq 'm-d2').reason | Should -Be 'tier_disallowed:canonical'
    }

    It 'fail-closed brand guard holds for bundle memories in an unknown-brand session' {
        $block = Format-MemoryContextBlock -Bundle $script:bundle -Brand $null -AuditPath $script:auditPath
        # Only null-brand memories may surface; this bundle has none -> no memories section
        if ($block) { $block | Should -Not -Match 'relevant memories:' }
    }

    It 'v0.21 M2 client defense: drops brand-tagged goals/OQ in an unknown-brand session, keeps brand-neutral' {
        # Bundle carries a brand-tagged + a brand-neutral goal and the same for OQ.
        # v1.0 R2: a brand-NEUTRAL evidence memory is added so the block fires
        # under abstention-first gating (unknown-brand session surfaces only
        # neutral memories); the goal/OQ brand gate is what's under test here.
        $branded = [pscustomobject]@{
            ok = $true
            checkpoint = [pscustomobject]@{ ok = $true; episode_id = 1; action = 'created' }
            memories = @(
                [pscustomobject]@{ id = 'm-neutral'; memory = 'brand-neutral evidence fact'; metadata = [pscustomobject]@{ tier = 'evidence'; brand = $null } }
            )
            goals = @(
                [pscustomobject]@{ id = 10; title = 'Brand-tagged brand-a goal'; priority = 2; status = 'open'; brand = 'brand-a' }
                [pscustomobject]@{ id = 11; title = 'Brand-neutral goal';         priority = 2; status = 'open'; brand = $null }
            )
            open_questions = @(
                [pscustomobject]@{ id = 20; question_text = 'Brand-tagged brand-a question?'; brand = 'brand-a' }
                [pscustomobject]@{ id = 21; question_text = 'Brand-neutral question?';         brand = $null }
            )
        }
        # Unknown-brand session ($Brand $null) -> only brand-neutral rows survive
        $block = Format-MemoryContextBlock -Bundle $branded -Brand $null -AuditPath $script:auditPath
        $block | Should -Not -BeNullOrEmpty
        $block | Should -Not -Match 'Brand-tagged brand-a goal'
        $block | Should -Match 'Brand-neutral goal'
        $block | Should -Not -Match 'Brand-tagged brand-a question\?'
        $block | Should -Match 'Brand-neutral question\?'
        $block | Should -Match 'Open goals \(1 shown\):'
    }

    It 'v0.21 M2 client defense: with a known brand, keeps same-brand + neutral goals/OQ, drops other-brand' {
        # v1.0 R2: an ai-ecosystem evidence memory is added so the block fires
        # under abstention-first gating; the goal brand gate is what's under test.
        $branded = [pscustomobject]@{
            ok = $true
            checkpoint = [pscustomobject]@{ ok = $true; episode_id = 1; action = 'created' }
            memories = @(
                [pscustomobject]@{ id = 'm-ai'; memory = 'ai-ecosystem evidence fact'; metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
            )
            goals = @(
                [pscustomobject]@{ id = 10; title = 'brand-a goal';     priority = 2; status = 'open'; brand = 'brand-a' }
                [pscustomobject]@{ id = 11; title = 'ai-ecosystem goal';  priority = 2; status = 'open'; brand = 'ai-ecosystem' }
                [pscustomobject]@{ id = 12; title = 'neutral goal';       priority = 2; status = 'open'; brand = $null }
            )
            open_questions = @()
        }
        $block = Format-MemoryContextBlock -Bundle $branded -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $block | Should -Match 'ai-ecosystem goal'
        $block | Should -Match 'neutral goal'
        $block | Should -Not -Match 'brand-a goal'
        $block | Should -Match 'Open goals \(2 shown\):'
    }
}

Describe 'v0.22 Phase D tier-aware rendering (Format-MemoryContextBlock -Tier)' {

    BeforeEach {
        $script:auditPath = Join-Path $TestDrive ("tier-admission-{0}.jsonl" -f ([guid]::NewGuid().ToString('N')))
        # Memories spanning tiers so highest-tier-first ordering + flat tag drop are observable.
        $script:bundle = [pscustomobject]@{
            ok        = $true
            checkpoint = [pscustomobject]@{ ok = $true; episode_id = 1; action = 'updated' }
            memories  = @(
                [pscustomobject]@{ id = 'm-ev';     memory = 'evidence advisory note'
                                   metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
                [pscustomobject]@{ id = 'm-stable'; memory = 'stable trusted fact'
                                   metadata = [pscustomobject]@{ tier = 'stable'; brand = 'ai-ecosystem' } }
            )
            goals = @(
                [pscustomobject]@{ id = 1; title = 'Ship the small-tier format'; priority = 2; status = 'open' }
            )
            open_questions = @(
                [pscustomobject]@{ id = 7; question_text = 'Does the flat format read clean?' }
            )
        }
        $script:Legend = 'Memory tiers: [canonical]=locked truth · [insight]/[stable]=trusted · [evidence]/[temporal]=advisory (verify before risky actions) · prefer higher-tier on conflict.'
    }

    It 'frontier tier is byte-identical to the default (no -Tier) full format' {
        $default  = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $frontier = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath -Tier 'frontier'
        $frontier | Should -Be $default
    }

    It 'a legacy/removed "mid" tier string still fails open to the full format (stale sidecars safe)' {
        # v0.23 removed the 'mid' tier — Sonnet is now frontier-class (1M flagship). A
        # sidecar still cached with tier='mid' must render the FULL block, never an empty
        # or trimmed one. Format treats any non-'small' tier as full, so this holds.
        $default = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $mid     = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath -Tier 'mid'
        $mid | Should -Be $default
    }

    It 'unknown tier fails open to the full (frontier) format' {
        $default = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $bogus   = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath -Tier 'bogus-xyz'
        $bogus | Should -Be $default
    }

    It 'small tier prepends the one-line tier legend' {
        $block = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath -Tier 'small'
        $lines = $block -split "`n"
        $lines[0] | Should -Be '[MEMORY CONTEXT - auto-surfaced by user-prompt-extract.ps1 v0.17 Phase 0.D]'
        ($block -split "`n") | Where-Object { $_ -eq $script:Legend } | Should -Not -BeNullOrEmpty
    }

    It 'small tier drops the redundant [brand] tag when the brand is known' {
        $block = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath -Tier 'small'
        # flat: tier-only tag, no |brand suffix
        $block | Should -Match '\[stable\] stable trusted fact'
        $block | Should -Not -Match '\[stable\|ai-ecosystem\]'
    }

    It 'small tier keeps the brand tag when the brand is unknown (no brand to drop)' {
        # null-brand memories so they survive the unknown-brand admission gate
        $neutral = [pscustomobject]@{
            ok = $true
            checkpoint = [pscustomobject]@{ ok = $true; episode_id = 2; action = 'created' }
            memories = @(
                [pscustomobject]@{ id = 'm-n'; memory = 'neutral evidence fact'
                                   metadata = [pscustomobject]@{ tier = 'evidence'; brand = $null } }
            )
            goals = @(); open_questions = @()
        }
        $block = Format-MemoryContextBlock -Bundle $neutral -Brand $null -AuditPath $script:auditPath -Tier 'small'
        $block | Should -Match '\[evidence\|cross-brand\] neutral evidence fact'
    }

    It 'small tier orders memories highest-tier LAST (v1.0 R6: most-trusted at the recency peak, adjacent to the prompt)' {
        # v1.0 R6 (placement): the importance order (small = tier-rank) is REVERSED at render so
        # the most-trusted memory is the final line, immediately above the user's prompt.
        $block = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath -Tier 'small'
        $lines = $block -split "`n"
        $stableIdx   = [array]::FindIndex($lines, [Predicate[string]]{ param($l) $l -match 'stable trusted fact' })
        $evidenceIdx = [array]::FindIndex($lines, [Predicate[string]]{ param($l) $l -match 'evidence advisory note' })
        $stableIdx | Should -BeGreaterThan -1
        $evidenceIdx | Should -BeGreaterThan -1
        $stableIdx | Should -BeGreaterThan $evidenceIdx
    }

    It 'small tier still applies client-side admission (canonical never surfaces)' {
        $withCanon = [pscustomobject]@{
            ok = $true
            checkpoint = [pscustomobject]@{ ok = $true; episode_id = 3; action = 'created' }
            memories = @(
                [pscustomobject]@{ id = 'm-c'; memory = 'canonical directive must not surface'
                                   metadata = [pscustomobject]@{ tier = 'canonical'; brand = 'ai-ecosystem' } }
                [pscustomobject]@{ id = 'm-e'; memory = 'evidence ok to surface'
                                   metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
            )
            goals = @(); open_questions = @()
        }
        $block = Format-MemoryContextBlock -Bundle $withCanon -Brand 'ai-ecosystem' -AuditPath $script:auditPath -Tier 'small'
        $block | Should -Not -Match 'canonical directive'
        $block | Should -Match 'evidence ok to surface'
    }
}

Describe 'v0.22 H1 — small-tier render under Windows PowerShell 5.1 (production runtime)' {
    # The production UserPromptSubmit path (mem0-hook-daemon-spawn.ps1 / mem0-hook-client.cs)
    # spawns System32\WindowsPowerShell\v1.0\powershell.exe (PS 5.1), NOT pwsh 7. Pester
    # runs under pwsh 7, so the rest of this file CANNOT catch a PS7-only construct
    # regressing the 5.1 render (the original -Stable bug). This leg shells out to the
    # REAL 5.1 interpreter, dot-sources the deployed lib, and renders a small-tier
    # bundle with >=2 memories — proving the block is non-empty ("Top 2", not "Top 0")
    # under the interpreter production actually uses.
    BeforeAll {
        $script:ps51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $script:libPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'user-prompt-lib.ps1'
    }

    It 'renders >=2 memories (not "Top 0") with $ErrorActionPreference=SilentlyContinue under PS 5.1' {
        if (-not (Test-Path $script:ps51)) {
            Set-ItResult -Skipped -Because 'Windows PowerShell 5.1 not present (non-Windows or removed)'
            return
        }
        # Render snippet executed by the 5.1 interpreter. Mirrors the production
        # daemon/extract environment ($ErrorActionPreference='SilentlyContinue' is the
        # exact setting that previously swallowed the -Stable error into an empty array).
        $snippet = @"
`$ErrorActionPreference = 'SilentlyContinue'
. '$($script:libPath)'
`$bundle = [pscustomobject]@{
  ok = `$true
  checkpoint = [pscustomobject]@{ ok = `$true; episode_id = 1; action = 'updated' }
  memories = @(
    [pscustomobject]@{ id = 'm-ev';     memory = 'evidence advisory note'; metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
    [pscustomobject]@{ id = 'm-stable'; memory = 'stable trusted fact';    metadata = [pscustomobject]@{ tier = 'stable';   brand = 'ai-ecosystem' } }
  )
  goals = @(); open_questions = @()
}
Format-MemoryContextBlock -Bundle `$bundle -Brand 'ai-ecosystem' -Tier 'small'
"@
        $snippetFile = Join-Path $TestDrive ("h1-render-{0}.ps1" -f ([guid]::NewGuid().ToString('N')))
        Set-Content -Path $snippetFile -Value $snippet -Encoding UTF8
        $block = & $script:ps51 -NoProfile -ExecutionPolicy Bypass -File $snippetFile 2>&1 | Out-String

        $block | Should -Match 'Top 2 relevant memories:'
        $block | Should -Not -Match 'Top 0 relevant memories:'
        $block | Should -Match '\[stable\] stable trusted fact'
        $block | Should -Match '\[evidence\] evidence advisory note'
        # v1.0 R6: highest-tier rendered LAST (recency peak) under 5.1 — the reverse + the
        # decorate-sort both hold on the production runtime (no "Top 0" collapse).
        $stablePos   = $block.IndexOf('stable trusted fact')
        $evidencePos = $block.IndexOf('evidence advisory note')
        $stablePos   | Should -BeGreaterThan -1
        $stablePos   | Should -BeGreaterThan $evidencePos
    }

    It 'v1.0 R2 abstention holds under PS 5.1: goals-only -> empty, memory present -> block' {
        if (-not (Test-Path $script:ps51)) {
            Set-ItResult -Skipped -Because 'Windows PowerShell 5.1 not present (non-Windows or removed)'
            return
        }
        # The block-level abstention ($anyMemoryAdmitted gate) must behave identically
        # on the production 5.1 interpreter as it does under pwsh 7. Exercise BOTH
        # arms in one 5.1 process: a goals-only bundle (must NOOP) then a
        # memory-present bundle (must render), with sentinels so the parse is robust.
        $snippet = @"
`$ErrorActionPreference = 'SilentlyContinue'
. '$($script:libPath)'
`$goalsOnly = [pscustomobject]@{
  ok = `$true; checkpoint = [pscustomobject]@{ ok = `$true }
  memories = @()
  goals = @([pscustomobject]@{ id = 1; title = 'Ship v0.26'; priority = 2; status = 'open' })
  open_questions = @([pscustomobject]@{ id = 7; question_text = 'threshold?' })
}
`$a = Format-MemoryContextBlock -Bundle `$goalsOnly -Brand 'ai-ecosystem' -Tier 'frontier'
if ([string]::IsNullOrEmpty(`$a)) { 'ABSTAIN_OK' } else { 'ABSTAIN_FAIL' }
`$withMem = [pscustomobject]@{
  ok = `$true; checkpoint = [pscustomobject]@{ ok = `$true }
  memories = @([pscustomobject]@{ id = 'm-ev'; memory = 'r2 evidence fact'; metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } })
  goals = @([pscustomobject]@{ id = 1; title = 'Ship v0.26'; priority = 2; status = 'open' })
  open_questions = @()
}
`$b = Format-MemoryContextBlock -Bundle `$withMem -Brand 'ai-ecosystem' -Tier 'frontier'
if (`$b -match 'r2 evidence fact') { 'RENDER_OK' } else { 'RENDER_FAIL' }
"@
        $snippetFile = Join-Path $TestDrive ("r2-abstain-51-{0}.ps1" -f ([guid]::NewGuid().ToString('N')))
        Set-Content -Path $snippetFile -Value $snippet -Encoding UTF8
        $out = & $script:ps51 -NoProfile -ExecutionPolicy Bypass -File $snippetFile 2>&1 | Out-String
        $out | Should -Match 'ABSTAIN_OK'
        $out | Should -Not -Match 'ABSTAIN_FAIL'
        $out | Should -Match 'RENDER_OK'
        $out | Should -Not -Match 'RENDER_FAIL'
    }
}

Describe 'v1.0 P7B brand inference (Get-InferredBrandFromPath — config-driven, operator-agnostic)' {

    BeforeAll {
        # Generic operator rules (no private brand names) — exercises the mechanism
        # the same way a real brands.json would, via the -Rules DI param.
        $script:testRules = @(
            [pscustomobject]@{ pattern = 'acme|acme-platform'; brand = 'acme' }
            [pscustomobject]@{ pattern = 'beta-co';            brand = 'beta' }
            [pscustomobject]@{ pattern = 'agentic-memory|ai-ecosystem|mem0'; brand = 'ai-ecosystem' }
        )
    }

    It 'infers <expected> from a path matching the operator rules' -ForEach @(
        @{ token = 'd--repos-acme-platform';        expected = 'acme' }
        @{ token = 'd--repos-beta-co-site';         expected = 'beta' }
        @{ token = 'd--repos-agentic-memory-stack'; expected = 'ai-ecosystem' }
        @{ token = 'd--My-Drive-AI-Ecosystem';      expected = 'ai-ecosystem' }
    ) {
        $p = "C:\Users\u\.claude\projects\$token\11111111-2222-3333-4444-555555555555.jsonl"
        Get-InferredBrandFromPath -Path $p -Rules $script:testRules | Should -Be $expected
    }

    It 'returns $null for paths matching no rule (fail-closed contract)' {
        Get-InferredBrandFromPath -Path 'C:\x\projects\C--Users-someone\x.jsonl' -Rules $script:testRules | Should -BeNullOrEmpty
        Get-InferredBrandFromPath -Path 'C:\x\projects\d--My-Drive-Tools\x.jsonl' -Rules $script:testRules | Should -BeNullOrEmpty
    }

    It 'returns $null for empty/whitespace paths' {
        Get-InferredBrandFromPath -Path ''   -Rules $script:testRules | Should -BeNullOrEmpty
        Get-InferredBrandFromPath -Path '  ' -Rules $script:testRules | Should -BeNullOrEmpty
    }

    It 'falls back to the neutral default (ai-ecosystem only) when no rules are injected and no brands.json is present' {
        Get-InferredBrandFromPath -Path 'C:\x\projects\d--My-Drive-AI-Ecosystem\x.jsonl' | Should -Be 'ai-ecosystem'
        Get-InferredBrandFromPath -Path 'C:\x\projects\d--repos-acme-platform\x.jsonl'   | Should -BeNullOrEmpty
    }

    It 'Get-StackBrandRules returns the neutral default when no brands.json is beside the lib' {
        $rules = Get-StackBrandRules
        @($rules).Count | Should -BeGreaterThan 0
        ($rules | Where-Object { $_.brand -eq 'ai-ecosystem' }) | Should -Not -BeNullOrEmpty
        # No operator-private brand names may leak into the default. The private
        # name list itself lives OUTSIDE the repo: one regex per line in
        # tests\pii-patterns.local.txt (gitignored; see the .example file).
        $piiFile = Join-Path $PSScriptRoot 'pii-patterns.local.txt'
        if (Test-Path -LiteralPath $piiFile) {
            $piiPatterns = @(Get-Content -LiteralPath $piiFile |
                Where-Object { $_ -and $_.Trim() -and -not $_.Trim().StartsWith('#') } |
                ForEach-Object { $_.Trim() })
            foreach ($pat in $piiPatterns) {
                ($rules | Where-Object { $_.pattern -match $pat }) | Should -BeNullOrEmpty -Because "default brand rules must not contain the operator-private pattern '$pat'"
            }
        }
    }
}

Describe 'v0.22 Pillar 1 session initiative (Get-SessionInitiative)' {

    It 'returns the git repo-root leaf for a path inside the agentic-memory-stack repo' {
        # The repo root is two levels above this tests dir
        # (scripts/windows/tests -> repo root).
        $repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
        # Sanity: only assert the git-leaf behavior when this really is a git repo
        # (CI checkouts always are; a tarball export would not be).
        $isGit = Test-Path (Join-Path $repoRoot '.git')
        if (-not $isGit) { Set-ItResult -Skipped -Because 'not a git checkout'; return }
        # Dynamic leaf: the checkout dir name varies (clone name, CI checkout) —
        # the contract is "repo-root leaf", not a specific repo name.
        Get-SessionInitiative -Cwd $repoRoot | Should -Be (Split-Path -Leaf $repoRoot)
    }

    It 'returns the git repo-root leaf from a SUBDIR of the repo (not the subdir leaf)' {
        $repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
        $isGit = Test-Path (Join-Path $repoRoot '.git')
        if (-not $isGit) { Set-ItResult -Skipped -Because 'not a git checkout'; return }
        Get-SessionInitiative -Cwd $PSScriptRoot | Should -Be (Split-Path -Leaf $repoRoot)
    }

    It 'falls back to the cwd leaf for a non-git temp directory' {
        $nonRepo = Join-Path $TestDrive ('initiative-leaf-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $nonRepo -Force | Out-Null
        Get-SessionInitiative -Cwd $nonRepo | Should -Be (Split-Path -Leaf $nonRepo)
    }

    It 'trims a trailing separator before taking the leaf (non-git path)' {
        $nonRepo = Join-Path $TestDrive ('initiative-trail-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $nonRepo -Force | Out-Null
        Get-SessionInitiative -Cwd ($nonRepo + '\') | Should -Be (Split-Path -Leaf $nonRepo)
    }

    It 'returns $null for empty/whitespace cwd' {
        Get-SessionInitiative -Cwd ''   | Should -BeNullOrEmpty
        Get-SessionInitiative -Cwd '  ' | Should -BeNullOrEmpty
    }
}

Describe 'v0.20 Phase F (L9) rate-limit decision + sweep (Get-RateLimitDecision / Invoke-RateLimitStateSweep)' {

    BeforeEach {
        $script:stateDir = Join-Path $TestDrive ("rl-state-{0}" -f ([guid]::NewGuid().ToString('N')))
        $script:sid = '11111111-2222-3333-4444-555555555555'
        $script:statePath = Join-Path $script:stateDir "user-prompt-rate-limit-$($script:sid)"
    }

    It 'cooldown boundary: 999ms since last fire -> limited; 1001ms -> not limited' {
        $last = [System.DateTime]::Now.ToFileTimeUtc()
        New-Item -ItemType Directory -Path $script:stateDir -Force | Out-Null
        Set-Content -Path $script:statePath -Value ([string]$last) -NoNewline
        $at999 = Get-RateLimitDecision -StateDir $script:stateDir -SessionId $script:sid `
            -NowFileTimeUtc ($last + 999L * 10000L) -CooldownMs 1000
        $at999.RateLimited | Should -BeTrue
        $at1001 = Get-RateLimitDecision -StateDir $script:stateDir -SessionId $script:sid `
            -NowFileTimeUtc ($last + 1001L * 10000L) -CooldownMs 1000
        $at1001.RateLimited | Should -BeFalse
        # StatePath is the consume-on-fire target the caller writes to
        $at999.StatePath | Should -Be $script:statePath
    }

    It 'per-session isolation: a fresh token for session A never limits session B' {
        $last = [System.DateTime]::Now.ToFileTimeUtc()
        New-Item -ItemType Directory -Path $script:stateDir -Force | Out-Null
        Set-Content -Path $script:statePath -Value ([string]$last) -NoNewline
        $otherSid = '99999999-8888-7777-6666-555555555555'
        $dA = Get-RateLimitDecision -StateDir $script:stateDir -SessionId $script:sid -NowFileTimeUtc $last -CooldownMs 1000
        $dB = Get-RateLimitDecision -StateDir $script:stateDir -SessionId $otherSid -NowFileTimeUtc $last -CooldownMs 1000
        $dA.RateLimited | Should -BeTrue
        $dB.RateLimited | Should -BeFalse
        $dB.StatePath   | Should -Not -Be $dA.StatePath
    }

    It 'corrupt state file fails open (not limited, no throw)' {
        New-Item -ItemType Directory -Path $script:stateDir -Force | Out-Null
        Set-Content -Path $script:statePath -Value 'not-a-filetime !!' -NoNewline
        $d = Get-RateLimitDecision -StateDir $script:stateDir -SessionId $script:sid `
            -NowFileTimeUtc ([System.DateTime]::Now.ToFileTimeUtc()) -CooldownMs 1000
        $d.RateLimited | Should -BeFalse
    }

    It 'missing state file/dir -> not limited, dir created for the consume-on-fire write' {
        $d = Get-RateLimitDecision -StateDir $script:stateDir -SessionId $script:sid `
            -NowFileTimeUtc ([System.DateTime]::Now.ToFileTimeUtc()) -CooldownMs 1000
        $d.RateLimited | Should -BeFalse
        Test-Path $script:stateDir | Should -BeTrue
    }

    It 'sweep deletes >1h state files including the legacy global file, spares fresh ones' {
        New-Item -ItemType Directory -Path $script:stateDir -Force | Out-Null
        $stale  = Join-Path $script:stateDir 'user-prompt-rate-limit-aaaaaaaa-old'
        $legacy = Join-Path $script:stateDir 'user-prompt-rate-limit'      # pre-v0.19 global file
        $fresh  = Join-Path $script:stateDir 'user-prompt-rate-limit-bbbbbbbb-new'
        $other  = Join-Path $script:stateDir 'unrelated-state-file'
        foreach ($f in @($stale, $legacy, $fresh, $other)) { Set-Content -Path $f -Value 'x' -NoNewline }
        (Get-Item $stale).LastWriteTime  = (Get-Date).AddHours(-2)
        (Get-Item $legacy).LastWriteTime = (Get-Date).AddHours(-2)
        (Get-Item $other).LastWriteTime  = (Get-Date).AddHours(-2)
        Invoke-RateLimitStateSweep -StateDir $script:stateDir -MaxAgeHours 1
        Test-Path $stale  | Should -BeFalse
        Test-Path $legacy | Should -BeFalse
        Test-Path $fresh  | Should -BeTrue
        Test-Path $other  | Should -BeTrue   # only rate-limit files are swept
    }
}

Describe 'v0.20 Phase F (L9) fixture writer (Save-HookFixture)' {

    BeforeEach {
        $script:fixDir = Join-Path $TestDrive ("fixtures-{0}" -f ([guid]::NewGuid().ToString('N')))
    }

    It 'writes raw stdin byte-faithfully: no BOM, key order preserved, no trailing bytes' {
        # deliberately non-canonical key order + escaped char + NO trailing newline
        $raw = '{"zeta":1,"alpha":"a\"b","mid":{"y":2,"x":1}}'
        $path = Save-HookFixture -FixtureDir $script:fixDir -EventName 'UserPromptSubmit' `
            -ContractVersion '20.0' -RawBytes $raw -SampleRoll $true
        $path | Should -Not -BeNullOrEmpty
        $path | Should -BeLike '*UserPromptSubmit-*-contract20.0.json'   # contract ver in FILENAME
        $bytes    = [System.IO.File]::ReadAllBytes($path)
        $expected = [System.Text.Encoding]::UTF8.GetBytes($raw)
        $bytes.Length | Should -Be $expected.Length    # no BOM, no trailing newline
        [Convert]::ToBase64String($bytes) | Should -Be ([Convert]::ToBase64String($expected))
    }

    It 'SampleRoll=$false writes nothing and returns $null' {
        $r = Save-HookFixture -FixtureDir $script:fixDir -EventName 'UserPromptSubmit' `
            -ContractVersion '20.0' -RawBytes '{"x":1}' -SampleRoll $false
        $r | Should -BeNullOrEmpty
        Test-Path $script:fixDir | Should -BeFalse
    }

    It 'prunes to the 20 newest fixtures of the SAME event, leaving other events alone' {
        New-Item -ItemType Directory -Path $script:fixDir -Force | Out-Null
        # 22 pre-existing UserPromptSubmit fixtures with staggered ages (oldest first)
        $pre = @()
        for ($i = 0; $i -lt 22; $i++) {
            $f = Join-Path $script:fixDir ("UserPromptSubmit-202601010000{0:00}-000-contract20.0.json" -f $i)
            Set-Content -Path $f -Value "{`"n`":$i}" -NoNewline
            (Get-Item $f).LastWriteTime = (Get-Date).AddMinutes(-60 + $i)
            $pre += $f
        }
        $otherEvent = Join-Path $script:fixDir 'PreToolUse-20260101000000-000-contract17.0.json'
        Set-Content -Path $otherEvent -Value '{}' -NoNewline
        (Get-Item $otherEvent).LastWriteTime = (Get-Date).AddHours(-9)
        $newPath = Save-HookFixture -FixtureDir $script:fixDir -EventName 'UserPromptSubmit' `
            -ContractVersion '20.0' -RawBytes '{"new":true}' -SampleRoll $true
        $newPath | Should -Not -BeNullOrEmpty
        $remaining = @(Get-ChildItem $script:fixDir -Filter 'UserPromptSubmit-*.json')
        $remaining.Count | Should -Be 20
        $remaining.FullName | Should -Contain $newPath          # newest survives
        $remaining.FullName | Should -Not -Contain $pre[0]      # oldest pruned
        $remaining.FullName | Should -Not -Contain $pre[1]
        $remaining.FullName | Should -Not -Contain $pre[2]
        Test-Path $otherEvent | Should -BeTrue                  # other event untouched
    }
}

Describe 'v0.22 Pillar 2 model-tier resolution (Resolve-ModelTier)' {

    BeforeAll {
        # The deployed-side default is model-tiers.json beside the lib; the
        # repo copy lives in claude-config/. Point the tests at the repo copy
        # explicitly so the suite passes BEFORE the installer/deploy step.
        $script:tiersConfig = Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))) 'claude-config\model-tiers.json'
        if (-not (Test-Path $script:tiersConfig)) { throw "model-tiers.json not found at $script:tiersConfig" }
    }

    It 'resolves "<model>" -> "<tier>" (case-insensitive substring match against tiers[*].match)' -ForEach @(
        @{ model = 'claude-fable-5';    tier = 'frontier' }
        @{ model = 'claude-opus-4-8';   tier = 'frontier' }
        @{ model = 'claude-opus-4-7';   tier = 'frontier' }   # v0.23: full portfolio (1M flagship)
        @{ model = 'claude-opus-4-6';   tier = 'frontier' }   # v0.23: full portfolio (1M flagship)
        @{ model = 'claude-sonnet-4-6'; tier = 'frontier' }   # v0.23: 1M flagship -> frontier (was 'mid')
        @{ model = 'claude-haiku-4-5';  tier = 'small' }
        @{ model = 'claude-unknown-9';  tier = 'frontier' }   # no match -> default_tier
        @{ model = 'CLAUDE-SONNET-4-6'; tier = 'frontier' }   # case-insensitive
        @{ model = 'CLAUDE-HAIKU-4-5';  tier = 'small' }       # case-insensitive
    ) {
        Resolve-ModelTier -Model $model -ConfigPath $script:tiersConfig | Should -Be $tier
    }

    It 'returns the default tier (frontier) for $null / empty / whitespace model' {
        Resolve-ModelTier -Model $null -ConfigPath $script:tiersConfig | Should -Be 'frontier'
        Resolve-ModelTier -Model ''    -ConfigPath $script:tiersConfig | Should -Be 'frontier'
        Resolve-ModelTier -Model '  '  -ConfigPath $script:tiersConfig | Should -Be 'frontier'
    }

    It 'falls back to frontier when the config file is missing (fail-open)' {
        $missing = Join-Path $TestDrive ('no-such-tiers-{0}.json' -f ([guid]::NewGuid().ToString('N')))
        Resolve-ModelTier -Model 'claude-haiku-4-5' -ConfigPath $missing | Should -Be 'frontier'
    }
}

Describe 'v0.22 Pillar 2 session-tier resolution (Get-SessionTier)' {

    BeforeAll {
        $script:tiersConfig = Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))) 'claude-config\model-tiers.json'
    }

    BeforeEach {
        $script:sid     = [guid]::NewGuid().ToString()
        $script:tierDir = Join-Path $TestDrive ("session-tier-{0}" -f ([guid]::NewGuid().ToString('N')))
        New-Item -ItemType Directory -Path $script:tierDir -Force | Out-Null
    }

    It 'returns the sidecar tier when ~/.mem0/session-tier/<id>.json exists (no transcript scan)' {
        $sidecar = Join-Path $script:tierDir ($script:sid + '.json')
        Set-Content -Path $sidecar -Value '{"model":"claude-haiku-4-5","tier":"small","ts":"2026-06-13T00:00:00"}' -NoNewline
        Get-SessionTier -SessionId $script:sid -TranscriptPath 'C:\nonexistent.jsonl' `
            -TierDir $script:tierDir -ConfigPath $script:tiersConfig | Should -Be 'small'
    }

    It 'parses the LAST assistant line .message.model from the transcript when no sidecar' {
        $transcript = Join-Path $script:tierDir 'transcript.jsonl'
        # An earlier frontier assistant line, then a later haiku assistant line:
        # resolution must use the LAST assistant model, not the first.
        $lines = @(
            '{"type":"user","message":{"role":"user","content":"hi"}}'
            '{"type":"assistant","message":{"role":"assistant","model":"claude-opus-4-8","content":[{"type":"text","text":"a"}]}}'
            '{"type":"user","message":{"role":"user","content":"again"}}'
            '{"type":"assistant","message":{"role":"assistant","model":"claude-haiku-4-5","content":[{"type":"text","text":"b"}]}}'
        )
        Set-Content -Path $transcript -Value ($lines -join "`n")
        Get-SessionTier -SessionId $script:sid -TranscriptPath $transcript `
            -TierDir $script:tierDir -ConfigPath $script:tiersConfig | Should -Be 'small'
    }

    It 'writes the resolved tier back to the sidecar after a transcript resolution (cache)' {
        $transcript = Join-Path $script:tierDir 'transcript.jsonl'
        Set-Content -Path $transcript -Value '{"type":"assistant","message":{"role":"assistant","model":"claude-haiku-4-5","content":[{"type":"text","text":"b"}]}}'
        $sidecar = Join-Path $script:tierDir ($script:sid + '.json')
        Test-Path $sidecar | Should -BeFalse
        Get-SessionTier -SessionId $script:sid -TranscriptPath $transcript `
            -TierDir $script:tierDir -ConfigPath $script:tiersConfig | Should -Be 'small'
        Test-Path $sidecar | Should -BeTrue -Because 'a transcript resolution caches the tier in the sidecar'
        (Get-Content $sidecar -Raw) | Should -Match '"tier"\s*:\s*"small"'
    }

    It 'returns frontier when there is no sidecar and no transcript (fail-open default)' {
        Get-SessionTier -SessionId $script:sid -TranscriptPath $null `
            -TierDir $script:tierDir -ConfigPath $script:tiersConfig | Should -Be 'frontier'
    }

    It 'returns frontier when the transcript has no assistant line with a model' {
        $transcript = Join-Path $script:tierDir 'transcript-nomodel.jsonl'
        Set-Content -Path $transcript -Value '{"type":"user","message":{"role":"user","content":"hi"}}'
        Get-SessionTier -SessionId $script:sid -TranscriptPath $transcript `
            -TierDir $script:tierDir -ConfigPath $script:tiersConfig | Should -Be 'frontier'
    }
}

Describe 'v0.22 Pillar 2 / B latency fix: sidecar read (Get-SessionSidecar)' {

    BeforeEach {
        $script:sid     = [guid]::NewGuid().ToString()
        $script:tierDir = Join-Path $TestDrive ("sidecar-{0}" -f ([guid]::NewGuid().ToString('N')))
        New-Item -ItemType Directory -Path $script:tierDir -Force | Out-Null
    }

    It 'reads tier + initiative + model from the sidecar (no git spawn on the prompt path)' {
        $sidecar = Join-Path $script:tierDir ($script:sid + '.json')
        Set-Content -Path $sidecar -Value '{"model":"claude-haiku-4-5","tier":"small","initiative":"agentic-memory-stack","ts":"2026-06-13T00:00:00"}' -NoNewline
        $sc = Get-SessionSidecar -SessionId $script:sid -TierDir $script:tierDir
        $sc.tier       | Should -Be 'small'
        $sc.initiative | Should -Be 'agentic-memory-stack'
        $sc.model      | Should -Be 'claude-haiku-4-5'
    }

    It 'normalizes an empty-string initiative to $null (explicitly unscoped session)' {
        $sidecar = Join-Path $script:tierDir ($script:sid + '.json')
        Set-Content -Path $sidecar -Value '{"model":"claude-opus-4-8","tier":"frontier","initiative":"","ts":"x"}' -NoNewline
        $sc = Get-SessionSidecar -SessionId $script:sid -TierDir $script:tierDir
        $sc.initiative | Should -BeNullOrEmpty
        $sc.tier       | Should -Be 'frontier'
    }

    It 'returns $null on sidecar miss (caller falls back to git spawn + transcript scan)' {
        Get-SessionSidecar -SessionId $script:sid -TierDir $script:tierDir | Should -BeNullOrEmpty
    }

    It 'returns $null for empty session id' {
        Get-SessionSidecar -SessionId '' -TierDir $script:tierDir | Should -BeNullOrEmpty
    }
}

Describe 'v1.0 R6 frontier placement guard: Format-MemoryContextBlock renders the memory section LAST' {

    It 'frontier block keeps the v0.21 header + [tier|brand] bullet shapes (no legend) but renders memories LAST (R6 placement; recency peak)' {
        # v1.0 R6 supersedes the v0.22 "byte-identical to v0.21" premise: the frontier block keeps
        # the same header + full [tier|brand] bullet shapes + no small-tier legend, BUT the memory
        # section now renders LAST (after Open goals + Open frontier questions) so the most-relevant
        # memory sits at the recency peak immediately above the user's prompt. This guard pins both
        # the shape (presence) AND the R6 ordering / literal-last-line invariant.
        $auditPath = Join-Path $TestDrive ("d5-audit-{0}.jsonl" -f ([guid]::NewGuid().ToString('N')))
        $bundle = [pscustomobject]@{
            memories = @(
                [pscustomobject]@{ id = 'm1'; memory = 'evidence fact one'
                                   metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } }
            )
            goals = @([pscustomobject]@{ title = 'ship v0.22'; priority = 2; brand = 'ai-ecosystem' })
            open_questions = @([pscustomobject]@{ question_text = 'does tier plumbing stay frontier-safe?'; brand = $null })
        }
        $block = Format-MemoryContextBlock -Bundle $bundle -Brand 'ai-ecosystem' -AuditPath $auditPath
        # Shape (unchanged): header, full [tier|brand] bullets, goals/OQ headers, NO small-tier legend.
        $block | Should -Match '\[MEMORY CONTEXT - auto-surfaced by user-prompt-extract\.ps1 v0\.17 Phase 0\.D\]'
        $block | Should -Match 'Top 1 relevant memories:'
        $block | Should -Match '  - \[evidence\|ai-ecosystem\] evidence fact one'
        $block | Should -Match 'Open goals \(1 shown\):'
        $block | Should -Match '  - \[P2 OPEN\] ship v0\.22'
        $block | Should -Match 'Open frontier questions:'
        $block | Should -Not -Match 'Memory tiers:'
        # R6 ORDERING: the memory section comes AFTER goals + open-questions.
        $block.IndexOf('Top 1 relevant memories:') | Should -BeGreaterThan $block.IndexOf('Open goals')
        $block.IndexOf('Top 1 relevant memories:') | Should -BeGreaterThan $block.IndexOf('Open frontier questions:')
        # R6 RECENCY PEAK: the memory bullet is the LITERAL last line (RAW, not blank-stripped) —
        # pins "no trailing blank after the memory section" so the memory is adjacent to the prompt.
        $rawLines = $block -split "`n"
        $rawLines[-1] | Should -Be '  - [evidence|ai-ecosystem] evidence fact one'
    }
}

Describe 'v0.22 Phase E R-offload invariant (Test-OffloadNoBlockInvariant)' {

    BeforeAll {
        # A valid, production-shaped hooks object: UserPromptSubmit -> the compiled
        # human-prompt client; PreToolUse gates Bash|Edit|MultiEdit|Write (no mcp__).
        $script:goodHooks = [pscustomobject]@{
            UserPromptSubmit = @(
                [pscustomobject]@{ hooks = @(
                    [pscustomobject]@{ type = 'command'; command = 'C:/Users/youruser/.claude/scripts/mem0-hook-client.exe' }
                ) }
            )
            PreToolUse = @(
                [pscustomobject]@{ matcher = 'Bash|Edit|Write|MultiEdit'; hooks = @(
                    [pscustomobject]@{ type = 'command'; command = 'powershell.exe -File C:/Users/youruser/.claude/scripts/pre-tool-check.ps1' }
                ) }
                [pscustomobject]@{ matcher = 'Write|Edit'; hooks = @(
                    [pscustomobject]@{ type = 'command'; command = 'node gsd-prompt-guard.js' }
                ) }
            )
        }
    }

    It 'passes (OK) for a production-shaped config: human-client UserPromptSubmit + no mcp__ PreToolUse matcher' {
        $r = Test-OffloadNoBlockInvariant -Hooks $script:goodHooks
        $r.Status | Should -Be 'OK'
        $r.Ok     | Should -BeTrue
        $r.Detail | Should -Match "matcher='Bash\|Edit\|Write\|MultiEdit'"
    }

    It 'also accepts the powershell-fallback UserPromptSubmit command (user-prompt-extract.ps1)' {
        $h = [pscustomobject]@{
            UserPromptSubmit = @([pscustomobject]@{ hooks = @(
                [pscustomobject]@{ type = 'command'; command = 'powershell.exe -File C:/Users/youruser/.claude/scripts/user-prompt-extract.ps1' }) })
            PreToolUse = @([pscustomobject]@{ matcher = 'Bash|Edit|Write|MultiEdit'; hooks = @(
                [pscustomobject]@{ command = 'pre-tool-check.ps1' }) })
        }
        (Test-OffloadNoBlockInvariant -Hooks $h).Status | Should -Be 'OK'
    }

    It 'FAILS when a PreToolUse matcher FIRES for an offload call (2026-07-14 audited semantics)' {
        # The invariant is evaluated the way Claude Code evaluates matchers — as a
        # regex against the tool name — not by substring-searching the matcher text.
        $bad = [pscustomobject]@{
            UserPromptSubmit = $script:goodHooks.UserPromptSubmit
            PreToolUse = @(
                [pscustomobject]@{ matcher = 'Bash|Edit|mcp__local-offload__.*'; hooks = @(
                    [pscustomobject]@{ command = 'pre-tool-check.ps1' }) }
            )
        }
        $r = Test-OffloadNoBlockInvariant -Hooks $bad
        $r.Status | Should -Be 'FAIL'
        $r.Ok     | Should -BeFalse
        $r.Detail | Should -Match 'fires for the offload harness'
    }

    It 'stays OK when a matcher names an UNRELATED MCP tool (precision fix — gating other MCP tools is legitimate)' {
        # 2026-07-14 audit: the old rule condemned ANY mcp__ substring, which would
        # have flagged a legitimate secret-scanner gate on desktop-commander writes.
        # Only matchers that actually FIRE on mcp__local-offload__* violate INV-2.
        $ok = [pscustomobject]@{
            UserPromptSubmit = $script:goodHooks.UserPromptSubmit
            PreToolUse = @(
                $script:goodHooks.PreToolUse[0]
                [pscustomobject]@{ matcher = 'mcp__desktop-commander__write_file|mcp__desktop-commander__edit_block'; hooks = @(
                    [pscustomobject]@{ command = 'node secret-scanner.js' }) }
            )
        }
        (Test-OffloadNoBlockInvariant -Hooks $ok).Status | Should -Be 'OK'
    }

    It 'FAILS when a UserPromptSubmit command references mcp__ (offload exposure)' {
        $bad = [pscustomobject]@{
            UserPromptSubmit = @([pscustomobject]@{ hooks = @(
                [pscustomobject]@{ command = 'mcp__local-offload__offload_summarize' }) })
            PreToolUse = $script:goodHooks.PreToolUse
        }
        $r = Test-OffloadNoBlockInvariant -Hooks $bad
        $r.Status | Should -Be 'FAIL'
        $r.Detail | Should -Match 'UserPromptSubmit command references mcp__'
    }

    It 'FAILS when UserPromptSubmit carries an mcp__ matcher' {
        $bad = [pscustomobject]@{
            UserPromptSubmit = @([pscustomobject]@{ matcher = 'mcp__local-offload__offload_triage'; hooks = @(
                [pscustomobject]@{ command = 'C:/Users/youruser/.claude/scripts/mem0-hook-client.exe' }) })
            PreToolUse = $script:goodHooks.PreToolUse
        }
        (Test-OffloadNoBlockInvariant -Hooks $bad).Status | Should -Be 'FAIL'
    }

    It 'fails OPEN (WARN) when the hooks object is null (config not found)' {
        $r = Test-OffloadNoBlockInvariant -Hooks $null
        $r.Status | Should -Be 'WARN'
        $r.Ok     | Should -BeTrue   # fail-open never flips exit code
    }

    It 'WARNs (not FAIL) when UserPromptSubmit routes to an unrecognized command' {
        $h = [pscustomobject]@{
            UserPromptSubmit = @([pscustomobject]@{ hooks = @(
                [pscustomobject]@{ command = 'some-other-thing.exe' }) })
            PreToolUse = $script:goodHooks.PreToolUse
        }
        $r = Test-OffloadNoBlockInvariant -Hooks $h
        $r.Status | Should -Be 'WARN'
        $r.Ok     | Should -BeTrue
    }

    It 'matches the LIVE deployed settings.json hooks (OK in this environment)' {
        $settings = Join-Path $env:USERPROFILE '.claude\settings.json'
        if (-not (Test-Path $settings)) { Set-ItResult -Skipped -Because 'no deployed settings.json'; return }
        $hooks = (Get-Content $settings -Raw | ConvertFrom-Json).hooks
        $r = Test-OffloadNoBlockInvariant -Hooks $hooks
        # In the operator's deployed config this is OK; at minimum it must never FAIL.
        $r.Status | Should -Not -Be 'FAIL'
    }
}

Describe 'v0.22 Phase E R-budget (Measure-MemoryContextBudget)' {

    BeforeAll {
        $script:tiersConfig = Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))) 'claude-config\model-tiers.json'
        if (-not (Test-Path $script:tiersConfig)) { throw "model-tiers.json not found at $script:tiersConfig" }
    }

    BeforeEach {
        $script:auditPath = Join-Path $TestDrive ("rbudget-{0}.jsonl" -f ([guid]::NewGuid().ToString('N')))
    }

    It 'every tier renders a worst-case block within its cap-derived char ceiling' -ForEach @(
        @{ tier = 'frontier' }, @{ tier = 'small' }
    ) {
        $m = Measure-MemoryContextBudget -Tier $tier -ConfigPath $script:tiersConfig -AuditPath $script:auditPath
        $m.Chars        | Should -BeGreaterThan 0
        $m.Ceiling      | Should -BeGreaterThan 0
        $m.WithinBudget | Should -BeTrue -Because "tier $tier rendered $($m.Chars) chars, ceiling $($m.Ceiling)"
    }

    It 'the small tier has a TIGHTER ceiling than frontier (caps 3/3/2 vs 5/5/3)' {
        $small    = Measure-MemoryContextBudget -Tier 'small'    -ConfigPath $script:tiersConfig -AuditPath $script:auditPath
        $frontier = Measure-MemoryContextBudget -Tier 'frontier' -ConfigPath $script:tiersConfig -AuditPath $script:auditPath
        $small.Ceiling | Should -BeLessThan $frontier.Ceiling
    }

    It 'returns the rendered block so the count_tokens leg can measure it' {
        $m = Measure-MemoryContextBudget -Tier 'small' -ConfigPath $script:tiersConfig -AuditPath $script:auditPath
        $m.Block | Should -Not -BeNullOrEmpty
        $m.Block | Should -Match '\[MEMORY CONTEXT'
    }

    It 'fails open (WithinBudget=$true, Chars=0) when the config is missing' {
        $missing = Join-Path $TestDrive ('no-tiers-{0}.json' -f ([guid]::NewGuid().ToString('N')))
        # Missing config -> default frontier-ish caps; render still succeeds, so
        # assert it does not throw and reports a within-budget result.
        $m = Measure-MemoryContextBudget -Tier 'small' -ConfigPath $missing -AuditPath $script:auditPath
        $m.WithinBudget | Should -BeTrue
    }
}
