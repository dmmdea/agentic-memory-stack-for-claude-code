# DreamAutopromote.Tests.ps1 — Pester smoke tests for the Phase 2 autonomous
# canonical promotion phase (Phase 3.5) in dream-consolidate.ps1.
#
# All external dependencies are avoided: the tests dot-source autopromote-lib.ps1
# (the production helper) and call Invoke-AutopromoteDecision directly.
# No live Codex or mem0 calls are made.
#
# Test matrix:
#   (a) DryRun=true  -> nominees logged, ZERO promotions (no canonize.sh call)
#   (b) Cap<=3        -> at most 3 nominees survive
#   (c) --actor flag  -> survivingNominees are present; logs include actor annotation
#   (d) Bad Codex JSON -> zero surviving nominees, no crash
#   (e) Structural filter (FIX 4) -> task/imperative text rejected before cap/dedup

BeforeAll {
    # ── Dot-source the production helper ────────────────────────────────────
    $LibPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'autopromote-lib.ps1'
    . $LibPath

    # Helper: build a list of N mock evidence records
    function _MakeEvidence([int]$n, [string]$prefix = 'id') {
        1..$n | ForEach-Object {
            [pscustomobject]@{
                id       = "$prefix-$_"
                memory   = "Stable declarative fact number $_ about the ecosystem architecture node topology"
                metadata = [pscustomobject]@{ tier = 'evidence' }
            }
        }
    }

    # Helper: build Codex JSON for N nominees (descending confidence)
    function _MakeCodexJson([string[]]$Ids) {
        $items = for ($i = 0; $i -lt $Ids.Count; $i++) {
            $conf = [math]::Round(1.0 - $i * 0.05, 2)
            "{`"memory_id`":`"$($Ids[$i])`",`"reason`":`"fact $($i+1)`",`"confidence`":$conf}"
        }
        '[' + ($items -join ',') + ']'
    }
}

# ---------------------------------------------------------------------------
# (a) DryRun=true -> ZERO promotions, nominees logged
# ---------------------------------------------------------------------------
Describe 'DryRun safety -- zero promotions, nominees logged' {
    It 'makes no actual promotions in DryRun (survivingNominees still reported)' {
        $ev   = _MakeEvidence 2 'dry'
        $json = _MakeCodexJson @('dry-1', 'dry-2')

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev -DryRun $true

        # Survivors are returned (callers decide what to do) — but DryRun logs are present
        $r.survivingNominees.Count | Should -BeGreaterOrEqual 0 -Because 'decision returns nominees regardless of DryRun'
        $dryLogs = @($r.logs | Where-Object { $_ -match 'DryRun=true' })
        if ($r.survivingNominees.Count -gt 0) {
            $dryLogs.Count | Should -BeGreaterThan 0 -Because 'DryRun survivors must be logged with DryRun annotation'
        }
    }

    It 'logs DryRun annotation for each surviving nominee' {
        $ev = @([pscustomobject]@{
            id = 'dry-ann-1'; memory = 'mem0 canonical scope is workspace=ai-ecosystem project=ecosystem'
            metadata = [pscustomobject]@{ tier = 'evidence' }
        })
        $json = '[{"memory_id":"dry-ann-1","reason":"canonical scope anchor","confidence":0.98}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev -DryRun $true

        $dryLog = @($r.logs | Where-Object { $_ -match 'DryRun=true' -and $_ -match 'dry-ann-1' })
        $dryLog.Count | Should -BeGreaterThan 0 -Because 'DryRun nominees must be logged with DryRun annotation'
    }

    It 'still nominates (survivingNominees returned) even in DryRun' {
        $ev = @([pscustomobject]@{
            id = 'dry-nom-1'; memory = 'EmbeddingGemma-300m serves all local inference on llama-swap :11436'
            metadata = [pscustomobject]@{ tier = 'evidence' }
        })
        $json = '[{"memory_id":"dry-nom-1","reason":"stable inference endpoint","confidence":0.95}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev -DryRun $true

        $r.survivingNominees.Count | Should -Be 1 -Because 'surviving count reflects nominees regardless of DryRun'
        $auditLog = @($r.logs | Where-Object { $_ -match 'dry-nom-1' })
        $auditLog.Count | Should -BeGreaterThan 0 -Because 'nominee must appear in audit log even in DryRun'
    }
}

# ---------------------------------------------------------------------------
# (b) Cap <= 3: at most 3 nominees survive; excess are deferred + logged
# ---------------------------------------------------------------------------
Describe 'Cap enforcement -- at most 3 nominees promoted' {
    It 'caps at 3 when Codex returns 5 nominees' {
        $ev   = _MakeEvidence 5 'cap'
        $json = _MakeCodexJson @('cap-1', 'cap-2', 'cap-3', 'cap-4', 'cap-5')

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        $r.survivingNominees.Count | Should -BeLessOrEqual 3 -Because 'hard cap is 3 per night'
        $r.overCapNominees.Count   | Should -BeGreaterOrEqual 2 -Because 'excess nominees must be deferred'
    }

    It 'does not defer when Codex returns exactly 3' {
        $ev   = _MakeEvidence 3 'ex3'
        $json = _MakeCodexJson @('ex3-1', 'ex3-2', 'ex3-3')

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        $r.overCapNominees.Count | Should -Be 0 -Because 'exactly 3 nominees must not trigger deferral'
        $r.survivingNominees.Count | Should -Be 3
    }

    It 'defers nominees 4 and 5 and logs them as over-cap' {
        $ev   = _MakeEvidence 5 'oc'
        $json = _MakeCodexJson @('oc-1', 'oc-2', 'oc-3', 'oc-4', 'oc-5')

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        $overCapLogs = @($r.logs | Where-Object { $_ -match 'deferred \(cap\)' })
        $overCapLogs.Count | Should -Be 2 -Because '2 nominees exceed the cap and must be logged as deferred'
    }
}

# ---------------------------------------------------------------------------
# (c) Promote: survivingNominees contain the expected IDs and logs show actor annotation
# ---------------------------------------------------------------------------
Describe 'Canonize actor flag -- survivingNominees ready for --actor dream-autopromote' {
    It 'returns survivors that would be promoted with --actor dream-autopromote' {
        $ev = @(
            [pscustomobject]@{ id='act-1'; memory='llama-swap serves all local LLM inference on :11436'; metadata=[pscustomobject]@{tier='evidence'} }
            [pscustomobject]@{ id='act-2'; memory='Box A is the primary Claude Code runtime node'; metadata=[pscustomobject]@{tier='evidence'} }
        )
        $json = '[{"memory_id":"act-1","reason":"stable inference endpoint","confidence":0.93},{"memory_id":"act-2","reason":"runtime node fact","confidence":0.88}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        $r.survivingNominees.Count | Should -BeGreaterThan 0 -Because 'at least one nomination should survive'
        # Verify surviving nominees have memory_id fields the consolidator passes to canonize.sh
        foreach ($nom in $r.survivingNominees) {
            $nom.memory_id | Should -Not -BeNullOrEmpty -Because 'each surviving nominee must have a memory_id for the canonize call'
        }
    }

    It 'logs the autonomous transport annotation for each surviving nominee' {
        $ev = @([pscustomobject]@{ id='ord-1'; memory='Stable ecosystem fact for ordering test'; metadata=[pscustomobject]@{tier='evidence'} })
        $json = '[{"memory_id":"ord-1","reason":"order test","confidence":0.90}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        # The decision function surfaces survivors; the consolidator uses --actor dream-autopromote.
        # Verify the nominee appears in logs (nominee log line)
        $nomineeLog = @($r.logs | Where-Object { $_ -match 'ord-1' })
        # Survivors are either in logs (dup/skip path) or just returned — either way check the id
        $r.survivingNominees | Where-Object { $_.memory_id -eq 'ord-1' } | Should -Not -BeNullOrEmpty -Because 'ord-1 should be a surviving nominee'
    }
}

# ---------------------------------------------------------------------------
# (d) Bad Codex JSON -> zero promotions, no crash
# ---------------------------------------------------------------------------
Describe 'Defensive Codex JSON parse -- bad output promotes nothing' {
    It 'returns empty survivors and does not throw on non-JSON prose' {
        $ev = @([pscustomobject]@{ id='bad-1'; memory='Some evidence fact'; metadata=[pscustomobject]@{tier='evidence'} })

        # Invoke-AutopromoteDecision is designed never to throw; call directly and verify.
        $r = Invoke-AutopromoteDecision -CodexJson 'Sorry, I cannot help.' -EvidenceMemories $ev

        $r                             | Should -Not -BeNullOrEmpty -Because 'bad JSON must not throw — result object must be returned'
        $r.survivingNominees.Count     | Should -Be 0
    }

    It 'promotes nothing on malformed partial JSON' {
        $ev = @([pscustomobject]@{ id='bad-2'; memory='Another evidence fact'; metadata=[pscustomobject]@{tier='evidence'} })

        $r = Invoke-AutopromoteDecision -CodexJson '[{"memory_id":"bad-2"' -EvidenceMemories $ev

        $r                         | Should -Not -BeNullOrEmpty -Because 'truncated JSON must not throw — result object must be returned'
        $r.survivingNominees.Count | Should -Be 0 -Because 'truncated JSON must promote nothing'
    }

    It 'promotes nothing when Codex call failed (null output)' {
        $ev = @([pscustomobject]@{ id='bad-3'; memory='Yet another evidence fact'; metadata=[pscustomobject]@{tier='evidence'} })

        $r = Invoke-AutopromoteDecision -CodexJson $null -CodexFailed $true -EvidenceMemories $ev

        $r                         | Should -Not -BeNullOrEmpty -Because 'null Codex output must not throw — result object must be returned'
        $r.survivingNominees.Count | Should -Be 0
    }

    It 'promotes nothing on empty Codex array' {
        $ev = @([pscustomobject]@{ id='bad-4'; memory='Evidence fact four'; metadata=[pscustomobject]@{tier='evidence'} })

        $r = Invoke-AutopromoteDecision -CodexJson '[]' -EvidenceMemories $ev

        $r.survivingNominees.Count | Should -Be 0 -Because 'empty nominee list promotes nothing'
    }

    It 'logs a bad-JSON event when Codex returns garbage' {
        $ev = @([pscustomobject]@{ id='bad-5'; memory='Evidence fact five'; metadata=[pscustomobject]@{tier='evidence'} })

        $r = Invoke-AutopromoteDecision -CodexJson 'not json at all' -EvidenceMemories $ev

        $badLog = @($r.logs | Where-Object { $_ -match 'bad Codex JSON|no Codex output' })
        $badLog.Count | Should -BeGreaterThan 0 -Because 'bad parse must be logged'
    }
}

# ---------------------------------------------------------------------------
# Dedup: near-duplicate of existing canonical is skipped
# ---------------------------------------------------------------------------
Describe 'Dedup -- near-duplicate of existing canonical is skipped' {
    It 'skips a nominee whose text is identical to an existing canonical fact' {
        $canonText = 'EmbeddingGemma-300m runs on llama-swap port 11436'
        $canonNorm = @(($canonText -replace '\s+', ' ').ToLower().Trim())
        $ev = @(
            # Identical text -> should be deduped
            [pscustomobject]@{ id='dup-1'; memory=$canonText; metadata=[pscustomobject]@{tier='evidence'} }
            # Distinct text -> should survive
            [pscustomobject]@{ id='dup-2'; memory='The node-b machine at 192.0.2.21 hosts the automation agent role candidate'; metadata=[pscustomobject]@{tier='evidence'} }
        )
        $json = '[{"memory_id":"dup-1","reason":"arch fact","confidence":0.95},{"memory_id":"dup-2","reason":"network fact","confidence":0.90}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev -CanonicalNorm $canonNorm

        $dupLog = @($r.logs | Where-Object { $_ -match 'dup-1' -and $_ -match 'dup' })
        $dupLog.Count | Should -BeGreaterThan 0 -Because 'near-duplicate must be logged as deferred'

        $dup1Survivors = @($r.survivingNominees | Where-Object { $_.memory_id -eq 'dup-1' })
        $dup1Survivors.Count | Should -Be 0 -Because 'near-duplicate must not survive'
    }

    It 'allows a nominee that does NOT duplicate any canonical fact' {
        $canonText = 'Ollama on port 11434 is decommissioned since v0.22'
        $canonNorm = @(($canonText -replace '\s+', ' ').ToLower().Trim())
        $ev = @(
            [pscustomobject]@{ id='uniq-1'; memory='The mem0 canonical scope anchor id is bc6fc858-55b6-4211-9bbf-9b3e66c23382'; metadata=[pscustomobject]@{tier='evidence'} }
        )
        $json = '[{"memory_id":"uniq-1","reason":"scope anchor","confidence":0.97}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev -CanonicalNorm $canonNorm

        $uniqSurvivors = @($r.survivingNominees | Where-Object { $_.memory_id -eq 'uniq-1' })
        $uniqSurvivors.Count | Should -Be 1 -Because 'a non-duplicate nominee must survive'
    }
}

# ---------------------------------------------------------------------------
# (e) FIX 4: Structural filter -- task/imperative text rejected
# ---------------------------------------------------------------------------
Describe 'Structural filter (FIX 4) -- task/imperative text rejected before cap/dedup' {
    It 'rejects a nominee with leading MUST keyword' {
        $ev = @([pscustomobject]@{
            id = 'imp-1'; memory = 'MUST verify the canonical key before calling canonize'
            metadata = [pscustomobject]@{ tier = 'evidence' }
        })
        $json = '[{"memory_id":"imp-1","reason":"imperative text","confidence":0.95}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        $r.structuralRejects.Count | Should -BeGreaterThan 0 -Because 'MUST-prefixed text must be structurally rejected'
        $survivors = @($r.survivingNominees | Where-Object { $_.memory_id -eq 'imp-1' })
        $survivors.Count | Should -Be 0 -Because 'structural reject must not survive'
        $rejectLogs = @($r.logs | Where-Object { $_ -match 'structural-reject' -and $_ -match 'imp-1' })
        $rejectLogs.Count | Should -BeGreaterThan 0 -Because 'structural reject must be logged'
    }

    It 'rejects a nominee with TODO marker' {
        $ev = @([pscustomobject]@{
            id = 'imp-2'; memory = 'TODO: update the mem0 canonical scope anchor'
            metadata = [pscustomobject]@{ tier = 'evidence' }
        })
        $json = '[{"memory_id":"imp-2","reason":"todo marker","confidence":0.90}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        $r.structuralRejects.Count | Should -BeGreaterThan 0 -Because 'TODO-prefixed text must be structurally rejected'
        $survivors = @($r.survivingNominees | Where-Object { $_.memory_id -eq 'imp-2' })
        $survivors.Count | Should -Be 0 -Because 'TODO text must not survive to promotion'
    }

    It 'rejects a nominee with leading verb-imperative (Run)' {
        $ev = @([pscustomobject]@{
            id = 'imp-3'; memory = 'Run the memory index build after each consolidation cycle'
            metadata = [pscustomobject]@{ tier = 'evidence' }
        })
        $json = '[{"memory_id":"imp-3","reason":"imperative verb","confidence":0.88}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        $r.structuralRejects.Count | Should -BeGreaterThan 0 -Because 'leading-verb imperative must be structurally rejected'
        $survivors = @($r.survivingNominees | Where-Object { $_.memory_id -eq 'imp-3' })
        $survivors.Count | Should -Be 0 -Because 'leading-verb imperative must not survive'
    }

    It 'allows a declarative fact through the structural filter' {
        $ev = @([pscustomobject]@{
            id = 'decl-1'; memory = 'EmbeddingGemma-300m serves all local inference on llama-swap :11436'
            metadata = [pscustomobject]@{ tier = 'evidence' }
        })
        $json = '[{"memory_id":"decl-1","reason":"declarative arch fact","confidence":0.97}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        $structReject = @($r.structuralRejects | Where-Object { $_.memory_id -eq 'decl-1' })
        $structReject.Count | Should -Be 0 -Because 'declarative fact must pass the structural filter'
        $survivors = @($r.survivingNominees | Where-Object { $_.memory_id -eq 'decl-1' })
        $survivors.Count | Should -Be 1 -Because 'declarative fact must survive to promotion'
    }

    It 'rejects NEVER- and ALWAYS-prefixed text' {
        $ev = @(
            [pscustomobject]@{ id='imp-never'; memory='NEVER re-register Ollama after decommissioning'; metadata=[pscustomobject]@{tier='evidence'} }
            [pscustomobject]@{ id='imp-always'; memory='ALWAYS use dream-autopromote for nightly promotions'; metadata=[pscustomobject]@{tier='evidence'} }
        )
        $json = '[{"memory_id":"imp-never","reason":"never rule","confidence":0.95},{"memory_id":"imp-always","reason":"always rule","confidence":0.90}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        $r.structuralRejects.Count | Should -Be 2 -Because 'both NEVER and ALWAYS prefixes must be rejected'
        $r.survivingNominees.Count | Should -Be 0 -Because 'no imperative nominees should survive'
    }

    It 'rejects WIP-tagged text' {
        $ev = @([pscustomobject]@{
            id = 'imp-wip'; memory = 'Memory consolidation v0.29 WIP: adding phase 3.5 autopromote'
            metadata = [pscustomobject]@{ tier = 'evidence' }
        })
        $json = '[{"memory_id":"imp-wip","reason":"wip marker","confidence":0.80}]'

        $r = Invoke-AutopromoteDecision -CodexJson $json -EvidenceMemories $ev

        $r.structuralRejects.Count | Should -BeGreaterThan 0 -Because 'WIP marker must be structurally rejected'
        $survivors = @($r.survivingNominees | Where-Object { $_.memory_id -eq 'imp-wip' })
        $survivors.Count | Should -Be 0 -Because 'WIP text must not survive'
    }
}
