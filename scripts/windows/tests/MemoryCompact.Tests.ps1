# MemoryCompact.Tests.ps1 - behavioural tests for memory-compact.ps1, one per SAFETY GUARD.
# (Part 1 of 2; store-shape robustness, ghost handling, history and dry-run live in
# MemoryCompactRobustness.Tests.ps1. Shared sandbox: MemoryCompact.Fixture.ps1.)
#
# Seam: the compactor dot-sources memory-common.ps1 from its OWN directory, so each scenario
# runs a copy of the real compactor + real memory-store-lib.ps1 beside a STUB memory-common.ps1
# that scripts Codex, mem0 and the throttle. Nothing is mocked inside the compactor itself -
# the logic under test is the shipped logic. $env:USERPROFILE is redirected so no real store or
# state directory is ever touched.
BeforeAll {
    . (Join-Path $PSScriptRoot 'MemoryCompact.Fixture.ps1')
}

Describe 'trigger + hysteresis' {
    It 'does nothing when every store is below the trigger' {
        $sb = New-Sandbox
        Add-SandboxStore -Sandbox $sb -Workspace 'small' -IndexLines @('- [A](a.md) ' + $script:EmDash + ' hook') -Facts @{ 'a.md' = (New-FactFile 'a' 'd') } | Out-Null
        $idx = Join-Path $sb.Projects 'small\memory\MEMORY.md'
        $before = Get-Bytes $idx
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts.Count | Should -Be 0
        (Get-Bytes $idx) | Should -Be $before
        Test-Path (Join-Path $sb.Home '.claude\state\throttle-marked') | Should -BeTrue -Because 'a clean sweep is a productive run'
    }
}

Describe 'GUARD 1: liveness gate' {
    It 'skips a store whose workspace has a recently-written transcript' {
        $sb = New-Sandbox
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts (@{} + (1..60 | ForEach-Object -Begin { $h = @{} } -Process { $h["fact$_.md"] = (New-FactFile "fact$_" 'd') } -End { $h })) | Out-Null
        Set-Content -Path (Join-Path $sb.Projects 'ws\live.jsonl') -Value '{}'
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $before = Get-Bytes $idx
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'skipped-live-session'
        (Get-Bytes $idx) | Should -Be $before
    }
}

Describe 'GUARD 2: compare-and-swap' {
    It 'aborts without writing when the index changes while the model is deciding' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $plan = '{"plan":[{"slug":"fact1.md","action":"SHORTEN","hook":"detail number 1 short"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $sb | Add-Member -NotePropertyName MutateTarget -NotePropertyValue $idx
        $sb.Mutate = '- [Appended by a live session](fact1.md) ' + $script:EmDash + ' written mid-run'
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'aborted-concurrent-write'
        (Get-Content -LiteralPath $idx -Raw) | Should -Match 'Appended by a live session' -Because 'the concurrent write must survive untouched'
    }
}

Describe 'GUARD 3: doctrine is untouchable' {
    It 'ignores a plan that tries to shorten or migrate a feedback-typed entry' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $facts['fact7.md'] = (New-FactFile 'fact7' 'a standing rule' 'feedback')
        $plan = '{"plan":[{"slug":"fact7.md","action":"SHORTEN","hook":"tiny"},{"slug":"fact8.md","action":"MIGRATE"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $idxText = Get-Content -LiteralPath (Join-Path $sb.Projects 'ws\memory\MEMORY.md') -Raw
        $idxText | Should -Match 'detail number 7 detail number 7' -Because 'the doctrine line must be byte-identical'
        $r.Receipts[0].shortened | Should -Be 0
        $prompt = Get-Content -LiteralPath (Join-Path $sb.Home '.claude\state\last-codex-prompt.txt') -Raw
        $prompt | Should -Not -Match 'slug: fact7\.md' -Because 'doctrine must never even be offered to the model'
    }
}

Describe 'GUARD 4: strict decrease, anchors, seal, blast cap' {
    It 'applies a genuine shortening and shrinks the index' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $plan = '{"plan":[{"slug":"fact1.md","action":"SHORTEN","hook":"detail number 1 kept short"},{"slug":"fact2.md","action":"SHORTEN","hook":"detail number 2 kept short"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $before = Get-Bytes $idx
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'applied'
        $r.Receipts[0].shortened | Should -Be 2
        (Get-Bytes $idx) | Should -BeLessThan $before
        (Get-Content -LiteralPath $idx -Raw) | Should -Match 'detail number 1 kept short'
    }

    It 'rejects a hook that is not shorter, and one that drops every anchor token' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $long = 'x' * 400
        $plan = '{"plan":[{"slug":"fact1.md","action":"SHORTEN","hook":"' + $long + '"},{"slug":"fact2.md","action":"SHORTEN","hook":"generic label with no anchors at all"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].shortened | Should -Be 0
        $r.Receipts[0].status | Should -Be 'no-op'
    }

    It 'seals a shortened line so a second run never re-sends it to the model' {
        # The shortened hook must stay OVER the 130 B line cap, or the byte filter alone would
        # keep it out of the second prompt and this test would pass with the seal deleted -
        # the seam-test trap. It shrinks (so the rewrite applies) but stays a candidate, so
        # ONLY the seal can exclude it.
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $stillLong = 'detail number 1 ' + ('kept but still long ' * 9)
        $plan = '{"plan":[{"slug":"fact1.md","action":"SHORTEN","hook":"' + $stillLong + '"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r1 = Invoke-Compactor -Sandbox $sb
        $r1.Receipts[0].shortened | Should -Be 1 -Because 'the rewrite must actually apply for the seal to mean anything'
        $line1 = (Get-Content -LiteralPath (Join-Path $sb.Projects 'ws\memory\MEMORY.md') | Where-Object { $_ -match '\(fact1\.md\)' })
        ([System.Text.Encoding]::UTF8.GetByteCount($line1)) | Should -BeGreaterThan 130 -Because 'the sealed line must remain a byte-cap candidate, else the byte filter (not the seal) is what excludes it'
        Invoke-Compactor -Sandbox $sb | Out-Null
        $prompt = Get-Content -LiteralPath (Join-Path $sb.Home '.claude\state\last-codex-prompt.txt') -Raw
        # A sealed line may still appear as a MIGRATE candidate; what must never recur is its
        # offer as a SHORTEN candidate (the "| <n> B" form). One judge rewrite per line, ever.
        $prompt | Should -Not -Match 'slug: fact1\.md \| type: [a-z]+ \| \d+ B' -Because 'one judge rewrite per line, ever - this is what stops nightly drift'
        $prompt | Should -Match 'slug: fact2\.md \| type: [a-z]+ \| \d+ B' -Because 'an unsealed long line is still offered'
    }

    It 'rejects a hook containing a markdown link (it would inject a permanent ghost)' {
        # A second link to a non-existent file is a ghost hygiene cannot remove: the invariant
        # check would fail, the index would be restored, and the run discarded - every night.
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $plan = '{"plan":[{"slug":"fact1.md","action":"SHORTEN","hook":"detail number 1 see [other](ghost.md)"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].shortened | Should -Be 0
        (Get-Content -LiteralPath (Join-Path $sb.Projects 'ws\memory\MEMORY.md') -Raw) | Should -Not -Match 'ghost\.md'
    }

    It 'keeps an anchor containing regex/wildcard characters (no false accept)' {
        # `cfg[0].name` is a character class to -like: the guard both rejected good hooks and
        # accepted hooks that had dropped the anchor entirely.
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $lines = New-BigIndexLines
        $lines += '- [Brackety](bracket.md) ' + $script:EmDash + ' the `cfg[0].name` knob ' + ('matters a great deal here ' * 5)
        $facts['bracket.md'] = (New-FactFile 'bracket' 'd')
        # hook DROPS the anchor -> must be rejected
        $plan = '{"plan":[{"slug":"bracket.md","action":"SHORTEN","hook":"the cfg0.name knob matters"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].shortened | Should -Be 0 -Because 'the rewritten hook no longer contains the anchor cfg[0].name'
    }
}

Describe 'GUARD 5: write-then-verify migration' {
    It 'removes the line and file only after a byte-equal read-back by id' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $plan = '{"plan":[{"slug":"fact3.md","action":"MIGRATE"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].migrated | Should -Be 1
        Test-Path (Join-Path $sb.Projects 'ws\memory\fact3.md') | Should -BeFalse
        (Get-Content -LiteralPath (Join-Path $sb.Projects 'ws\memory\MEMORY.md') -Raw) | Should -Not -Match 'fact3\.md'
        ($r.Receipts[0].mem0 -join ' ') | Should -Match 'stub-id-0001' -Because 'the receipt must carry the id and the original line'
        (Get-Content -LiteralPath (Join-Path $sb.Home '.claude\state\mem0-posts.txt') -Raw) | Should -Match 'automemory:ws/fact3.md' -Because 'the source tag is the A-to-B bridge and the dedup exemption key'
    }

    It 'keeps the line when mem0 returns no id (unverifiable)' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexPlanJson '{"plan":[{"slug":"fact3.md","action":"MIGRATE"}]}' -Mem0Mode 'noid'
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].migrated | Should -Be 0
        Test-Path (Join-Path $sb.Projects 'ws\memory\fact3.md') | Should -BeTrue
    }

    It 'keeps the line when the read-back text does not match byte-for-byte' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexPlanJson '{"plan":[{"slug":"fact3.md","action":"MIGRATE"}]}' -Mem0Mode 'mismatch'
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].migrated | Should -Be 0
        Test-Path (Join-Path $sb.Projects 'ws\memory\fact3.md') | Should -BeTrue
    }
}

Describe 'no-local-fallback' {
    It 'still applies deterministic hygiene when Codex is unreachable, and does not mark the throttle' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $facts['orphan.md'] = (New-FactFile 'orphan' 'an unindexed fact')
        $lines = New-BigIndexLines
        $lines += '- [Gone](missing-file.md) ' + $script:EmDash + ' dangling'
        $sb = New-Sandbox -CodexThrows
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].dedangled | Should -Be 1
        $r.Receipts[0].reindexed | Should -Be 1
        $r.Receipts[0].shortened | Should -Be 0
        $idxText = Get-Content -LiteralPath (Join-Path $sb.Projects 'ws\memory\MEMORY.md') -Raw
        $idxText | Should -Not -Match 'missing-file\.md'
        $idxText | Should -Match 'orphan\.md'
        Test-Path (Join-Path $sb.Home '.claude\state\throttle-marked') | Should -BeTrue -Because 'the run still reached a decision; the codex-only work is what waits'
    }
}

Describe 'feasibility: protected-set overflow fails loud' {
    It 'refuses to act when doctrine lines alone exceed the target budget' {
        $lines = @('# Index', '')
        $facts = @{}
        for ($i = 1; $i -le 150; $i++) {
            $lines += ('- [Rule ' + $i + '](rule' + $i + '.md) ' + $script:EmDash + ' ' + ('standing rule text ' + $i + ' ') * 8)
            $facts["rule$i.md"] = (New-FactFile "rule$i" 'a rule' 'feedback')
        }
        $sb = New-Sandbox
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $before = Get-Bytes $idx
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'protected-set-overflow'
        (Get-Bytes $idx) | Should -Be $before -Because 'it must fail loud, never loosen the hard rule'
    }
}

