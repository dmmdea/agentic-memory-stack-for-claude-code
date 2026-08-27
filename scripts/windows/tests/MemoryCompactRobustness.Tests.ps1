# MemoryCompactRobustness.Tests.ps1 - part 2 of the compactor suite: store-shape robustness
# (the review-reproduced defects), ghost-link handling, history/receipts and dry-run purity.
# Shared sandbox: MemoryCompact.Fixture.ps1. See MemoryCompact.Tests.ps1 for the guard tests.
BeforeAll {
    . (Join-Path $PSScriptRoot 'MemoryCompact.Fixture.ps1')
}

Describe 'store-shape robustness (review-reproduced defects)' {
    It 'never duplicates a pointer whose line the entry regex cannot parse, and never grows the index' {
        # Reproduced in review: a bracketed title and an indented pointer both parsed as
        # non-entries, their files were called orphans, and a SECOND pointer was appended to
        # each - growing a store that was already at 96% of the sync limit.
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $facts['bracket.md'] = (New-FactFile 'bracket' 'desc for bracket')
        $facts['nested.md']  = (New-FactFile 'nested' 'desc for nested')
        $lines = New-BigIndexLines
        $lines += '- [Title [with] brackets](bracket.md) ' + $script:EmDash + ' a hook'
        $lines += '  - [Nested](nested.md) ' + $script:EmDash + ' a nested pointer'
        $sb = New-Sandbox
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $before = Get-Bytes $idx
        $r = Invoke-Compactor -Sandbox $sb
        $text = Get-Content -LiteralPath $idx -Raw
        ([regex]::Matches($text, [regex]::Escape('(bracket.md)'))).Count | Should -Be 1 -Because 'the file is already indexed; a second pointer is duplication'
        ([regex]::Matches($text, [regex]::Escape('(nested.md)'))).Count | Should -Be 1
        $r.Receipts[0].reindexed | Should -Be 0
        (Get-Bytes $idx) | Should -BeLessOrEqual $before -Because 'the maintainer must never grow a store'
    }

    It 'aborts instead of wiping the index when the store enumerates no fact files' {
        # If the fact directory cannot be read, every line looks dangling - and every downstream
        # guard agrees, because they all compare against the same empty set.
        $sb = New-Sandbox
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts @{} | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $before = Get-Bytes $idx
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'aborted-no-fact-files'
        (Get-Bytes $idx) | Should -Be $before
    }

    It 'caps hygiene removals: a mass-dangling index is reported, not silently gutted' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $lines = New-BigIndexLines
        # 30 dangling lines out of 90 entries - far over the 20% blast cap.
        for ($i = 1; $i -le 30; $i++) { $lines += ('- [Gone ' + $i + '](missing' + $i + '.md) ' + $script:EmDash + ' dangling') }
        $sb = New-Sandbox
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $before = Get-Bytes $idx
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'aborted-blast-cap'
        (Get-Bytes $idx) | Should -Be $before
    }

    It 'a concurrent write aborts with the fact file still on disk (nothing deleted, not just nothing written)' {
        # The delete used to happen before the CAS, so an abort left the store with a dangling
        # link while the receipt claimed "no write performed".
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $plan = '{"plan":[{"slug":"fact3.md","action":"MIGRATE"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $sb | Add-Member -NotePropertyName MutateTarget -NotePropertyValue $idx
        $sb.Mutate = '- [Appended by a live session](fact1.md) ' + $script:EmDash + ' written mid-run'
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'aborted-concurrent-write'
        Test-Path (Join-Path $sb.Projects 'ws\memory\fact3.md') | Should -BeTrue -Because 'an abort must leave the store exactly as it was'
        (Get-Content -LiteralPath $idx -Raw) | Should -Match 'fact3\.md'
    }

    It 'reports skipped-judge-unavailable (not no-op) and does not mark the throttle' {
        # A store over trigger with nothing for hygiene to fix and no judge: the run accomplished
        # nothing, so it must be retried rather than counted as done.
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexThrows
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'skipped-judge-unavailable'
        Test-Path (Join-Path $sb.Home '.claude\state\throttle-marked') | Should -BeFalse -Because 'no local fallback: the judge-only work waits and is retried'
    }

    It 'never deletes a DEDUPLICATED id on read-back failure (it is someone else''s record)' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexPlanJson '{"plan":[{"slug":"fact3.md","action":"MIGRATE"}]}' -Mem0Mode 'dedup-mismatch'
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].migrated | Should -Be 0
        Test-Path (Join-Path $sb.Projects 'ws\memory\fact3.md') | Should -BeTrue
        Test-Path (Join-Path $sb.Home '.claude\state\mem0-deleted.txt') | Should -BeFalse -Because 'a dedup hit is a PRE-EXISTING record; deleting it would destroy an L1a fact or an earlier migration'
        ($r.Receipts[0].mem0_orphan -join ' ') | Should -Match 'pre-existing'
    }

    It 'a concurrent-write abort after a verified migration UNDOES the corpus write' {
        # Without the undo, the abort leaves a verified, exemption-protected record that no
        # receipt names and that only heals if the judge happens to say MIGRATE again.
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexPlanJson '{"plan":[{"slug":"fact3.md","action":"MIGRATE"}]}'
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $sb | Add-Member -NotePropertyName MutateTarget -NotePropertyValue $idx
        $sb.Mutate = '- [Appended by a live session](fact1.md) ' + $script:EmDash + ' written mid-run'
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'aborted-concurrent-write'
        $r.Receipts[0].migrated | Should -Be 0
        (Get-Content -LiteralPath (Join-Path $sb.Home '.claude\state\mem0-deleted.txt') -Raw) | Should -Match 'stub-id-0001'
        Test-Path (Join-Path $sb.Projects 'ws\memory\fact3.md') | Should -BeTrue
    }

    It 'undoes an unverifiable migration write instead of leaving an orphan record' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexPlanJson '{"plan":[{"slug":"fact3.md","action":"MIGRATE"}]}' -Mem0Mode 'mismatch'
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].migrated | Should -Be 0
        Test-Path (Join-Path $sb.Projects 'ws\memory\fact3.md') | Should -BeTrue
        (Get-Content -LiteralPath (Join-Path $sb.Home '.claude\state\mem0-deleted.txt') -Raw -ErrorAction SilentlyContinue) | Should -Match 'stub-id-0001' -Because 'the unverified record must be removed, not left dangling in the corpus'
    }
}

Describe 'ghost links: decided before the judge, nothing deleted, nothing posted' {
    It 'an unrepairable entry ghost aborts BEFORE any write or migration (the reviewed nightly-loop)' {
        # Reproduced in review: a permanent ghost + a migration plan = files deleted every
        # night, index restored every night, receipt saying lost=0. Now: abort up front.
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $lines = New-BigIndexLines
        # The dead link's text contains "]", so the repair regex cannot remove it: a genuinely
        # unrepairable entry ghost.
        $facts['ninety.md'] = (New-FactFile 'ninety' 'd')
        $lines += '- [Ninety](ninety.md) ' + $script:EmDash + ' see [odd ] name](ghost-a.md) for detail'
        $plan = '{"plan":[{"slug":"fact3.md","action":"MIGRATE"},{"slug":"fact4.md","action":"MIGRATE"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $before = [System.IO.File]::ReadAllBytes($idx)
        $r1 = Invoke-Compactor -Sandbox $sb
        $r2 = Invoke-Compactor -Sandbox $sb
        foreach ($r in @($r1, $r2)) {
            $r.Receipts[-1].status | Should -Be 'aborted-ghost-links'
            $r.Receipts[-1].note | Should -Match 'ghost-a\.md'
        }
        Test-Path (Join-Path $sb.Projects 'ws\memory\fact3.md') | Should -BeTrue
        Test-Path (Join-Path $sb.Projects 'ws\memory\fact4.md') | Should -BeTrue
        Test-Path (Join-Path $sb.Home '.claude\state\mem0-posts.txt') | Should -BeFalse -Because 'nothing may be posted for a store that cannot be written'
        [System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($idx)) | Should -Be ([System.BitConverter]::ToString($before))
    }

    It 'a non-entry line mentioning a missing file is NOT a ghost (reachability only)' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $lines = @('# Index (older notes were moved to (archive.md))', '') + (New-BigIndexLines | Select-Object -Skip 2)
        $plan = '{"plan":[{"slug":"fact1.md","action":"SHORTEN","hook":"detail number 1 kept short"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'applied' -Because 'the compactor never rewrites a heading, so it must not be held to it'
    }

    It 'repairs a dead extra link while KEEPING a live extra link on the same line' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $lines = New-BigIndexLines
        $facts['merged.md'] = (New-FactFile 'merged' 'd')
        $lines += '- [Merged](merged.md) ' + $script:EmDash + ' see also [two](fact5.md) and [gone](ghost.md)'
        $sb = New-Sandbox
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].status | Should -Be 'applied'
        $text = Get-Content -LiteralPath (Join-Path $sb.Projects 'ws\memory\MEMORY.md') -Raw
        $text | Should -Not -Match 'ghost\.md'
        $text | Should -Match '\[two\]\(fact5\.md\)' -Because 'the live extra link must survive the repair'
        $text | Should -Not -Match 'see also \[two\]\(fact5\.md\) and\s*$' -Because 'dangling conjunction should be tidied'
    }

    It 'a checkbox line with a link is never removed as dangling, and a fenced list item is not an entry' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $lines = New-BigIndexLines
        $lines += '- [x] task done, see [notes](missing-notes.md)'
        $lines += '```'
        $lines += '- [Example](example-only.md) - an illustration inside a code fence'
        $lines += '```'
        $sb = New-Sandbox
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $text = Get-Content -LiteralPath (Join-Path $sb.Projects 'ws\memory\MEMORY.md') -Raw
        $text | Should -Match 'missing-notes\.md' -Because 'an ambiguous checkbox line is left alone, never deleted'
        $text | Should -Match 'example-only\.md' -Because 'a fenced example is text, not a pointer'
        $r.Receipts[0].dedangled | Should -Be 0
    }

    It 'blast cap boundary: 23 dangling over 90 live aborts, 22 applies (cap = floor(0.2 * total entries))' {
        # total entries = 90 live + n dangling; cap = floor(0.2 * (90+n)); abort iff n > cap.
        # n=23 -> cap 22 -> abort; n=22 -> cap 22 -> apply. A regression that loosened the
        # constant to 0.25 would flip the first case.
        foreach ($case in @(@{ n = 23; want = 'aborted-blast-cap' }, @{ n = 22; want = 'applied' })) {
            $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
            $lines = New-BigIndexLines
            for ($i = 1; $i -le 30; $i++) { $lines += ('- [Live ' + $i + '](live' + $i + '.md) ' + $script:EmDash + ' ok'); $facts["live$i.md"] = (New-FactFile "live$i" 'd') }
            for ($i = 1; $i -le $case.n; $i++) { $lines += ('- [Gone ' + $i + '](missing' + $i + '.md) ' + $script:EmDash + ' dangling') }
            $sb = New-Sandbox
            Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts | Out-Null
            $r = Invoke-Compactor -Sandbox $sb
            $r.Receipts[0].status | Should -Be $case.want -Because "$($case.n) dangling of $(90 + $case.n) entries"
        }
    }
}

Describe 'history and receipts' {
    It 'commits to an out-of-tree repo with no remote, writes a diff, and never creates .git in a store' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $plan = '{"plan":[{"slug":"fact1.md","action":"SHORTEN","hook":"detail number 1 kept short"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].commit | Should -Match '^[0-9a-f]{40}$'
        Test-Path (Join-Path $sb.Home '.claude\state\automemory\history.git\HEAD') | Should -BeTrue
        Test-Path (Join-Path $sb.Projects 'ws\memory\.git') | Should -BeFalse
        Test-Path (Join-Path $sb.Projects '.git') | Should -BeFalse
        $diff = Get-ChildItem (Join-Path $sb.Home '.claude\state\automemory\ws') -Filter 'index-*.diff'
        @($diff).Count | Should -Be 1
        $diffText = Get-Content -LiteralPath $diff[0].FullName -Raw
        $diffText | Should -Match 'diff --git a/ws/memory/MEMORY\.md' -Because 'the receipt diff is the 30-second audit artifact'
        $diffText | Should -Match '(?m)^\+.*detail number 1 kept short' -Because 'the diff must show what the night actually changed'
    }

    It 'is idempotent: a second run over a compacted store changes nothing' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $plan = '{"plan":[{"slug":"fact1.md","action":"SHORTEN","hook":"detail number 1 kept short"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        Invoke-Compactor -Sandbox $sb | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $mid = [System.IO.File]::ReadAllBytes($idx)
        $r2 = Invoke-Compactor -Sandbox $sb
        $after = [System.IO.File]::ReadAllBytes($idx)
        [System.BitConverter]::ToString($after) | Should -Be ([System.BitConverter]::ToString($mid))
        $r2.Receipts[-1].status | Should -BeIn @('no-op', 'applied')
    }
}

Describe 'dry run' {
    It 'reports what it would do and writes nothing' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $plan = '{"plan":[{"slug":"fact1.md","action":"SHORTEN","hook":"detail number 1 kept short"},{"slug":"fact3.md","action":"MIGRATE"}]}'
        $sb = New-Sandbox -CodexPlanJson $plan
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $idx = Join-Path $sb.Projects 'ws\memory\MEMORY.md'
        $before = [System.IO.File]::ReadAllBytes($idx)
        $r = Invoke-Compactor -Sandbox $sb -ExtraArgs @('-DryRun')
        $r.Receipts[0].status | Should -Be 'dry-run'
        $r.Receipts[0].after_bytes | Should -BeLessThan $r.Receipts[0].before_bytes
        # A dry run PROJECTS its migrations, so the reported line count and `migrated` describe
        # the same world; before/after must also be counted identically or the receipt misreports.
        $r.Receipts[0].migrated | Should -Be 1
        $r.Receipts[0].after_lines | Should -Be ($r.Receipts[0].before_lines - $r.Receipts[0].migrated)
        [System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($idx)) | Should -Be ([System.BitConverter]::ToString($before))
        Test-Path (Join-Path $sb.Home '.claude\state\mem0-posts.txt') | Should -BeFalse -Because 'a dry run must not post to mem0'
    }
}
