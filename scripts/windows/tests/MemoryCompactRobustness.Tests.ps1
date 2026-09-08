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

Describe 'receipt fidelity' {
    It 'a re-indexed orphan that is then migrated carries its constructed line in the receipt' {
        # The receipt row is "id | slug | original line". A line the job itself constructed has
        # no "original", so it must carry the constructed one - never an empty string, which
        # would leave the audit trail unable to say what the index looked like before removal.
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $facts['orphan.md'] = (New-FactFile 'orphan' 'an unindexed reference fact' 'reference')
        $sb = New-Sandbox -CodexPlanJson '{"plan":[{"slug":"orphan.md","action":"MIGRATE"}]}'
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts[0].reindexed | Should -Be 1
        $r.Receipts[0].migrated | Should -Be 1
        $row = ($r.Receipts[0].mem0 -join "`n")
        $row | Should -Match 'orphan\.md \| - \[orphan\]\(orphan\.md\)' -Because 'the receipt must show the line as it stood in the index'
        $row | Should -Not -Match 'orphan\.md \| \s*$'
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

Describe 'convergence floor (2026-09-03): the index leaves every run under the sync limit, or the run says so' {
    BeforeAll {
        . (Join-Path $PSScriptRoot 'MemoryCompact.Fixture.ps1')
        # 80 lines x ~360 B = ~29 KB: over the 25,000 B sync limit, every line over the cap.
        function New-OverLimitStore {
            param($Sandbox, [int]$Count = 80, [string]$Type = 'project', [switch]$HugeTitles)
            $lines = @('# Memory Index', '')
            $facts = @{}
            for ($i = 1; $i -le $Count; $i++) {
                $title = if ($HugeTitles) { ('T' * 140) + $i } else { 'Fact ' + $i }
                $lines += ('- [' + $title + '](fact' + $i + '.md) ' + $script:EmDash + ' ' + ('detail number ' + $i + ' ') * 22)
                $facts["fact$i.md"] = New-FactFile "fact$i" 'd' $Type
            }
            return (Add-SandboxStore -Sandbox $Sandbox -Workspace 'ws' -IndexLines $lines -Facts $facts)
        }
    }

    It 'floors an over-limit index deterministically when the judge keeps everything, and reports applied' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        $dir = New-OverLimitStore -Sandbox $sb
        $idx = Join-Path $dir 'MEMORY.md'
        (Get-Bytes $idx) | Should -BeGreaterThan 25000 -Because 'the fixture must start over the sync limit or the floor is never exercised'
        $r = Invoke-Compactor -Sandbox $sb -ExtraArgs @('-Workspace', 'ws')
        $r.Receipts[0].status | Should -Be 'applied'
        $r.Receipts[0].floored | Should -BeGreaterThan 0
        $r.Receipts[0].after_bytes | Should -BeLessThan 20000 -Because 'the floor stops below the TRIGGER (hysteresis), not merely below the sync limit'
        (Get-Bytes $idx) | Should -BeLessThan 25000
        $r.ExitCode | Should -Be 0
        # every floored line still round-trips as the same entry (slug intact) and is under the cap
        # The floor stops at the TRIGGER (hysteresis), not at zero long lines: everything past that
        # point is left for the judge, which keeps first crack at hook quality. So the assertion is
        # "fewer long lines than before", never "none".
        $long = @((Get-Content $idx) | Where-Object { $_ -match '^- \[' -and ([Text.Encoding]::UTF8.GetByteCount($_)) -gt 130 })
        $long.Count | Should -BeLessThan 80 -Because 'the floor must have truncated at least some lines (all 80 started over the cap)'
        $long.Count | Should -BeGreaterThan 0 -Because 'the floor stops at the trigger and leaves the remainder to the judge (hysteresis, not a blanket rewrite)'
    }

    It 'still floors when the codex lock is held: the judge is skipped, hygiene and the floor are not' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}' -LockHeld
        $dir = New-OverLimitStore -Sandbox $sb
        $idx = Join-Path $dir 'MEMORY.md'
        $r = Invoke-Compactor -Sandbox $sb -ExtraArgs @('-Workspace', 'ws')
        $r.Receipts[0].status | Should -Be 'applied'
        (Get-Bytes $idx) | Should -BeLessThan 25000 -Because 'the lock guards the JUDGE, not the deterministic floor - a store must not stay unloadable because dedup ran first'
        Test-Path (Join-Path $sb.Home '.claude\state\last-codex-prompt.txt') | Should -BeFalse -Because 'no judge call may happen while another worker holds the mutex'
        (Get-Content (Join-Path $sb.Home '.claude\logs\compact-test.log') -Raw) | Should -Match 'codex lock held'
    }

    It 'reports unconverged and exits 1 when nothing can be floored (titles alone eat the cap)' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        $dir = New-OverLimitStore -Sandbox $sb -HugeTitles
        $idx = Join-Path $dir 'MEMORY.md'
        $before = Get-Bytes $idx
        $r = Invoke-Compactor -Sandbox $sb -ExtraArgs @('-Workspace', 'ws')
        $r.Receipts[0].status | Should -BeIn @('unconverged', 'applied-unconverged')
        $r.Receipts[0].note | Should -Match 'UNCONVERGED'
        $r.ExitCode | Should -Be 1 -Because 'a store still over the sync limit is a failed run and the scheduled task must record it'
        Test-Path (Join-Path $sb.Home '.claude\state\throttle-marked') | Should -BeFalse -Because 'an unconverged run must be retried, not counted as done'
    }

    It 'never offers or applies a migration for a body over the server storage cap' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[{"slug":"big.md","action":"MIGRATE"}]}'
        $lines = New-BigIndexLines 60
        $lines += ('- [Big](big.md) ' + $script:EmDash + ' a pullable lookup with an enormous body')
        $facts = @{}
        1..60 | ForEach-Object { $facts["fact$_.md"] = New-FactFile "fact$_" 'd' }
        $facts['big.md'] = New-FactFile 'big' 'd' 'project' (('lorem ipsum ') * 500)   # ~6,000 chars > 4,000 cap
        $dir = Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines $lines -Facts $facts
        $r = Invoke-Compactor -Sandbox $sb -ExtraArgs @('-Workspace', 'ws')
        $r.Receipts[0].migrated | Should -Be 0 -Because 'the server would 413 it; retrying nightly forever is the defect'
        Test-Path (Join-Path $dir 'big.md') | Should -BeTrue
        (Get-Content (Join-Path $dir 'MEMORY.md') -Raw) | Should -Match '\(big\.md\)'
        $prompt = Get-Content (Join-Path $sb.Home '.claude\state\last-codex-prompt.txt') -Raw
        $prompt | Should -Not -Match 'slug: big\.md \| type: project \| migrate-candidate' -Because 'an unmigratable body must not even be offered'
        Test-Path (Join-Path $sb.Home '.claude\state\mem0-posts.txt') | Should -BeFalse -Because 'nothing may be POSTed for it'
    }
}

Describe 'hygiene runs on every store (2026-09-03): a small store is not exempt from correctness' {
    BeforeAll { . (Join-Path $PSScriptRoot 'MemoryCompact.Fixture.ps1') }

    It 're-indexes an orphan in a below-trigger store without calling the judge, and receipts it' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        $facts = @{ 'a.md' = (New-FactFile 'a' 'd'); 'orphan.md' = (New-FactFile 'orphan' 'never indexed') }
        $dir = Add-SandboxStore -Sandbox $sb -Workspace 'small' -IndexLines @('# Memory Index', '', ('- [A](a.md) ' + $script:EmDash + ' hook')) -Facts $facts
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts.Count | Should -Be 1 -Because 'one receipt for the store that changed'
        $r.Receipts[0].status | Should -Be 'applied'
        $r.Receipts[0].reindexed | Should -Be 1
        (Get-Content (Join-Path $dir 'MEMORY.md') -Raw) | Should -Match '\(orphan\.md\)' -Because 'the orphan must be reachable from the index again'
        Test-Path (Join-Path $sb.Home '.claude\state\last-codex-prompt.txt') | Should -BeFalse -Because 'below the trigger there is nothing for the judge'
        $r.ExitCode | Should -Be 0
    }

    It 'writes no receipt and no log line for a clean below-trigger store (nightly common case)' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        Add-SandboxStore -Sandbox $sb -Workspace 'clean' -IndexLines @('# Memory Index', '', ('- [A](a.md) ' + $script:EmDash + ' hook')) -Facts @{ 'a.md' = (New-FactFile 'a' 'd') } | Out-Null
        $r = Invoke-Compactor -Sandbox $sb
        $r.Receipts.Count | Should -Be 0
        (Get-Content (Join-Path $sb.Home '.claude\logs\compact-test.log') -Raw) | Should -Not -Match 'clean: no-op'
        Test-Path (Join-Path $sb.Home '.claude\state\throttle-marked') | Should -BeTrue -Because 'a clean pass is a real decision; the throttle is marked'
    }
}

Describe 'line floor, catch-up and synthesized hooks (2026-09-06)' {
    BeforeAll { . (Join-Path $PSScriptRoot 'MemoryCompact.Fixture.ps1') }

    It 'migrates the OLDEST pullable facts until the store is back at its line target, never doctrine, when the judge keeps everything' {
        # 172 short entries (+2 header lines = 174 index lines): ~8 KB, under the byte trigger but over the 160-line trigger.
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        $lines = @('# Memory Index', ''); $facts = @{}
        for ($i = 1; $i -le 169; $i++) {
            $lines += ('- [Fact ' + $i + '](fact' + $i + '.md) ' + $script:EmDash + ' hook ' + $i)
            $facts['fact' + $i + '.md'] = (New-FactFile ('fact' + $i) ('desc ' + $i) 'reference' ('body ' + $i))
        }
        for ($i = 1; $i -le 3; $i++) {
            $lines += ('- [Rule ' + $i + '](rule' + $i + '.md) ' + $script:EmDash + ' Daniel: never do thing ' + $i)
            $facts['rule' + $i + '.md'] = (New-FactFile ('rule' + $i) ('Daniel: never do thing ' + $i) 'feedback' ('the rule ' + $i))
        }
        $dir = Add-SandboxStore -Sandbox $sb -Workspace 'tall' -IndexLines $lines -Facts $facts
        # age: fact1..fact40 are the oldest (100 days back, ascending), the rest are fresh
        for ($i = 1; $i -le 40; $i++) { (Get-Item (Join-Path $dir ('fact' + $i + '.md'))).LastWriteTime = (Get-Date).AddDays(-100 + $i) }
        $r = Invoke-Compactor -Sandbox $sb
        $rc = $r.Receipts | Where-Object { $_.workspace -eq 'tall' } | Select-Object -Last 1
        $rc.status | Should -Be 'applied'
        $rc.line_floored | Should -Be 34 -Because '174 index lines -> 140 target = 34 migrations, all from the floor (the judge kept everything); the blast cap is floor(0.2*172) = 34'
        $rc.migrated | Should -Be 34
        $rc.after_lines | Should -BeLessOrEqual 142
        for ($i = 1; $i -le 34; $i++) { Test-Path (Join-Path $dir ('fact' + $i + '.md')) | Should -BeFalse -Because "fact$i is among the 34 oldest" }
        Test-Path (Join-Path $dir 'fact35.md') | Should -BeTrue
        Test-Path (Join-Path $dir 'fact169.md') | Should -BeTrue
        for ($i = 1; $i -le 3; $i++) { Test-Path (Join-Path $dir ('rule' + $i + '.md')) | Should -BeTrue -Because 'doctrine is never migrated by the floor' }
        (Get-Content (Join-Path $dir 'MEMORY.md') -Raw) | Should -Match '\(rule1\.md\)'
        $r.ExitCode | Should -Be 0
    }

    It '-CatchUp exits silently while the throttle stamp is fresh (a quiet night writes no receipt), and runs once it is stale' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        Add-SandboxStore -Sandbox $sb -Workspace 'cu' -IndexLines @('# Memory Index', '', ('- [A](a.md) ' + $script:EmDash + ' hook')) -Facts @{ 'a.md' = (New-FactFile 'a' 'd'); 'orphan.md' = (New-FactFile 'orphan' 'x') } | Out-Null
        $marker = Join-Path $sb.Home '.claude\state\throttle-fresh'
        Set-Content -LiteralPath $marker -Value '1'
        $r = Invoke-Compactor -Sandbox $sb -ExtraArgs @('-CatchUp')
        $r.Receipts.Count | Should -Be 0 -Because 'a fresh throttle stamp means last night ran (receipt or not); a session start must not re-run it'
        (Get-Content (Join-Path $sb.Home '.claude\logs\compact-test.log') -Raw -ErrorAction SilentlyContinue) | Should -Not -Match 'catch-up'
        Remove-Item -LiteralPath $marker -Force
        $r2 = Invoke-Compactor -Sandbox $sb -ExtraArgs @('-CatchUp')
        $r2.Receipts.Count | Should -BeGreaterOrEqual 1 -Because 'a stale stamp = the nightly was missed; catch-up runs it'
        (Get-Content (Join-Path $sb.Home '.claude\logs\compact-test.log') -Raw) | Should -Match 'catch-up: no productive run'
        ($r2.Receipts | Where-Object { $_.workspace -eq 'cu' } | Select-Object -Last 1).reindexed | Should -Be 1
    }

    It 'the line floor never migrates an ATTRIBUTED statement, even when it is typed project' {
        # The live run migrated "Owner: X's box = first-class, ABSOLUTE" - no imperative verb, type
        # project. An attributed statement is doctrine whatever verb follows.
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        $lines = @('# Memory Index', ''); $facts = @{}
        for ($i = 1; $i -le 165; $i++) {
            $lines += ('- [Fact ' + $i + '](fact' + $i + '.md) ' + $script:EmDash + ' hook ' + $i)
            $facts['fact' + $i + '.md'] = (New-FactFile ('fact' + $i) ('desc ' + $i) 'reference' ('body ' + $i))
        }
        $lines += ('- [Owner priority](owner-priority.md) ' + $script:EmDash + " Owner: the small box = first-class, ABSOLUTE #1 queue priority")
        $facts['owner-priority.md'] = (New-FactFile 'owner-priority' "Owner 2026-07-23: the small box = first-class, ABSOLUTE #1 priority" 'project' 'the standing order')
        $dir = Add-SandboxStore -Sandbox $sb -Workspace 'attr' -IndexLines $lines -Facts $facts
        (Get-Item (Join-Path $dir 'owner-priority.md')).LastWriteTime = (Get-Date).AddDays(-400)   # the oldest file of all
        $r = Invoke-Compactor -Sandbox $sb
        $rc = $r.Receipts | Where-Object { $_.workspace -eq 'attr' } | Select-Object -Last 1
        $rc.status | Should -Be 'applied'
        $rc.line_floored | Should -BeGreaterThan 0
        Test-Path (Join-Path $dir 'owner-priority.md') | Should -BeTrue -Because 'the oldest file is an attributed standing order; the floor must skip it'
        (Get-Content (Join-Path $dir 'MEMORY.md') -Raw) | Should -Match '\(owner-priority\.md\)'
    }

    It 're-indexes a frontmatter-less orphan with a hook made from its first line of prose' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        $addendum = "**2026-09-03 addendum " + $script:EmDash + " orphan engine cores.** vLLM engine cores are setproctitle'd; see [old](x.md).`n`nmore text`n"
        $dir = Add-SandboxStore -Sandbox $sb -Workspace 'fl' -IndexLines @('# Memory Index', '', ('- [A](a.md) ' + $script:EmDash + ' hook')) -Facts @{ 'a.md' = (New-FactFile 'a' 'd'); 'wsl2-traps.md' = $addendum }
        $r = Invoke-Compactor -Sandbox $sb
        ($r.Receipts | Where-Object { $_.workspace -eq 'fl' } | Select-Object -Last 1).reindexed | Should -Be 1
        $idx = Get-Content (Join-Path $dir 'MEMORY.md') -Raw
        $idx | Should -Match '\(wsl2-traps\.md\) .* 2026-09-03 addendum'
        $idx | Should -Not -Match 'no description'
        $idx | Should -Not -Match '\(x\.md\)' -Because 'a link inside the prose must not become a second slug on the line'
    }
}

Describe 'usage ledger completeness (2026-09-07 silent-failure review)' {
    It 'writes an outcome=empty row when the judge SUCCEEDS but returns nothing' {
        # '' is falsy in PowerShell, so `if ($raw)` skipped the ledger write entirely and this
        # outcome was invisible to codex-usage-report's `failed` column. A failure the ledger
        # cannot count is a failure nobody fixes.
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexEmpty
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        $null = Invoke-Compactor -Sandbox $sb
        $rowsFile = Join-Path $sb.Home '.claude\state\usage-rows.jsonl'
        Test-Path -LiteralPath $rowsFile | Should -BeTrue -Because 'the empty-success path wrote no row at all before this fix'
        $rows = @(Get-Content -LiteralPath $rowsFile | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
        @($rows | Where-Object { $_.component -eq 'compact' -and $_.outcome -eq 'empty' }).Count |
            Should -BeGreaterThan 0 -Because 'a successful call that returned nothing is an outcome, not a non-event'
    }
}

Describe 'starvation (2026-09-08): a live session must not starve a store past the sync limit, and starvation is reported' {
    # The live incident: skipped as "live" two nights running (legitimately), 19,032 -> 26,424 B
    # with no pass between, harness stopped syncing, another session loaded a partial index.
    # Nothing said so. These pin every part of the fix and the exact boundaries it must respect.
    BeforeAll {
        $script:LintSrc = Join-Path (Split-Path -Parent $PSScriptRoot) 'memory-lint.ps1'
        function script:Seed-Receipts {
            param($Sandbox, [string]$Workspace, [string[]]$Statuses, [double]$HoursAgo = 30)
            $d = Join-Path $Sandbox.Home '.claude\state\automemory'
            [System.IO.Directory]::CreateDirectory($d) | Out-Null
            $i = 0
            foreach ($st in $Statuses) {
                $ts = (Get-Date).ToUniversalTime().AddHours(-$HoursAgo + $i).ToString('o'); $i++
                Add-Content -LiteralPath (Join-Path $d 'compact-receipts.jsonl') -Value ('{"ts":"' + $ts + '","workspace":"' + $Workspace + '","dry_run":false,"status":"' + $st + '"}')
            }
        }
        function script:Make-Live {
            # a transcript in the workspace dir is what Test-AmWorkspaceLive probes
            param($Sandbox, [string]$Workspace, [int]$MinutesAgo = 0)
            $wd = Join-Path $Sandbox.Projects $Workspace
            [System.IO.Directory]::CreateDirectory($wd) | Out-Null
            $t = Join-Path $wd 'session.jsonl'
            Set-Content -LiteralPath $t -Value '{"type":"user"}'
            (Get-Item -LiteralPath $t).LastWriteTime = (Get-Date).AddMinutes(-1 * $MinutesAgo)
        }
        function script:New-OverLimitStore {
            # 180 pullable facts with ~170 B lines: ~30 KB, comfortably over the 25,000 B limit
            param($Sandbox, [string]$Workspace)
            $lines = @('# Memory Index', ''); $facts = @{}
            for ($i = 1; $i -le 180; $i++) {
                $lines += ('- [Fact ' + $i + '](fact' + $i + '.md) ' + $script:EmDash + ' ' + ('detail ' * 20) + $i)
                $facts['fact' + $i + '.md'] = (New-FactFile ('fact' + $i) ('desc ' + $i) 'reference' ('body ' + $i))
            }
            return (Add-SandboxStore -Sandbox $Sandbox -Workspace $Workspace -IndexLines $lines -Facts $facts)
        }
    }

    It 'UNDER the sync limit a live session still skips - and the receipt now counts the streak' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        Add-SandboxStore -Sandbox $sb -Workspace 'ws' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        script:Seed-Receipts -Sandbox $sb -Workspace 'ws' -Statuses @('skipped-live-session')
        script:Make-Live -Sandbox $sb -Workspace 'ws'
        $rc = (Invoke-Compactor -Sandbox $sb).Receipts | Where-Object { $_.workspace -eq 'ws' } | Select-Object -Last 1
        $rc.status | Should -Be 'skipped-live-session' -Because 'under the limit the lost-update guard is unchanged'
        $rc.skip_streak | Should -Be 2
        $rc.liveness_override | Should -BeFalse
    }

    It 'AT the sync limit after two skips, a live session no longer blocks: the run proceeds and the receipt says why' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        $dir = script:New-OverLimitStore -Sandbox $sb -Workspace 'ws'
        (Get-Item (Join-Path $dir 'MEMORY.md')).Length | Should -BeGreaterOrEqual 25000
        script:Seed-Receipts -Sandbox $sb -Workspace 'ws' -Statuses @('skipped-live-session', 'skipped-live-session')
        script:Make-Live -Sandbox $sb -Workspace 'ws'
        $rc = (Invoke-Compactor -Sandbox $sb).Receipts | Where-Object { $_.workspace -eq 'ws' } | Select-Object -Last 1
        $rc.liveness_override | Should -BeTrue
        $rc.note | Should -Match 'LIVENESS OVERRIDDEN'
        $rc.status | Should -Be 'applied'
        $rc.after_bytes | Should -BeLessThan 25000 -Because 'the whole point is to get the harness syncing it again'
    }

    It 'AT the sync limit with no prior skip, a session quiet for 10 min is not mid-write: the run proceeds' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        script:New-OverLimitStore -Sandbox $sb -Workspace 'ws' | Out-Null
        script:Make-Live -Sandbox $sb -Workspace 'ws' -MinutesAgo 10
        $rc = (Invoke-Compactor -Sandbox $sb).Receipts | Where-Object { $_.workspace -eq 'ws' } | Select-Object -Last 1
        $rc.liveness_override | Should -BeTrue
        $rc.note | Should -Match 'no transcript write in the last 5 min'
        $rc.status | Should -Be 'applied'
    }

    It 'AT the sync limit with no prior skip, a session that wrote 1 min ago STILL skips (the override is a boundary, not a blanket)' {
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        script:New-OverLimitStore -Sandbox $sb -Workspace 'ws' | Out-Null
        script:Make-Live -Sandbox $sb -Workspace 'ws' -MinutesAgo 1
        $rc = (Invoke-Compactor -Sandbox $sb).Receipts | Where-Object { $_.workspace -eq 'ws' } | Select-Object -Last 1
        $rc.status | Should -Be 'skipped-live-session'
        $rc.skip_streak | Should -Be 1
        $rc.liveness_override | Should -BeFalse
    }

    It '-CatchUp runs a store starved by live-session skips even though the RUN stamp is fresh' {
        # The stamp is per run: another store''s no-op marked it while this one was skipped, so
        # the catch-up used to exit here at every session start and the store starved for days.
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        Add-SandboxStore -Sandbox $sb -Workspace 'st' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        Set-Content -LiteralPath (Join-Path $sb.Home '.claude\state\throttle-fresh') -Value '1'
        script:Seed-Receipts -Sandbox $sb -Workspace 'st' -Statuses @('skipped-live-session', 'skipped-live-session') -HoursAgo 30
        $r = Invoke-Compactor -Sandbox $sb -ExtraArgs @('-CatchUp')
        $rc = $r.Receipts | Where-Object { $_.workspace -eq 'st' } | Select-Object -Last 1
        $rc.status | Should -BeIn @('applied', 'no-op') -Because 'reaching a DECISION is what ends starvation; this store has nothing to fix, so no-op is that decision'
        $rc.ts | Should -Not -BeNullOrEmpty
        (Get-Content (Join-Path $sb.Home '.claude\logs\compact-test.log') -Raw) | Should -Match 'starved by live-session skips'
    }

    It '-CatchUp still exits silently when the fresh stamp is backed by a recent decision on the store above trigger' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        Add-SandboxStore -Sandbox $sb -Workspace 'st' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        Set-Content -LiteralPath (Join-Path $sb.Home '.claude\state\throttle-fresh') -Value '1'
        script:Seed-Receipts -Sandbox $sb -Workspace 'st' -Statuses @('applied') -HoursAgo 1
        $r = Invoke-Compactor -Sandbox $sb -ExtraArgs @('-CatchUp')
        @($r.Receipts | Where-Object { $_.workspace -eq 'st' }).Count | Should -Be 1 -Because 'only the seeded receipt: a store that reached a decision an hour ago is not starved'
        (Get-Content (Join-Path $sb.Home '.claude\logs\compact-test.log') -Raw -ErrorAction SilentlyContinue) | Should -Not -Match 'catch-up'
    }

    It 'lint raises compactor-starved (actionable) on two consecutive skips above trigger, and the store row carries the streak' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        Add-SandboxStore -Sandbox $sb -Workspace 'lt' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        script:Seed-Receipts -Sandbox $sb -Workspace 'lt' -Statuses @('skipped-live-session', 'skipped-live-session')
        Copy-Item $script:LintSrc $sb.Bin
        $cmd = '$env:USERPROFILE=' + "'" + $sb.Home + "'; & '" + (Join-Path $sb.Bin 'memory-lint.ps1') + "' -Quiet"
        & pwsh -NoProfile -NonInteractive -Command $cmd 2>&1 | Out-Null
        $j = Get-Content -LiteralPath (Join-Path $sb.Home '.claude\state\automemory\lint-summary.json') -Raw | ConvertFrom-Json
        ($j.stores | Where-Object { $_.workspace -eq 'lt' }).skip_streak | Should -Be 2
        @($j.findings | Where-Object { $_.kind -eq 'compactor-starved' }).Count | Should -Be 1
        $j.counts.actionable | Should -BeGreaterOrEqual 1 -Because 'a finding missing from the actionable list reaches no surface at all'
    }

    It 'lint does NOT raise compactor-starved for a single skip under the limit (one live night is normal)' {
        $facts = @{}; 1..60 | ForEach-Object { $facts["fact$_.md"] = (New-FactFile "fact$_" 'd') }
        $sb = New-Sandbox -CodexPlanJson '{"plan":[]}'
        Add-SandboxStore -Sandbox $sb -Workspace 'lt' -IndexLines (New-BigIndexLines) -Facts $facts | Out-Null
        script:Seed-Receipts -Sandbox $sb -Workspace 'lt' -Statuses @('applied', 'skipped-live-session')
        Copy-Item $script:LintSrc $sb.Bin
        $cmd = '$env:USERPROFILE=' + "'" + $sb.Home + "'; & '" + (Join-Path $sb.Bin 'memory-lint.ps1') + "' -Quiet"
        & pwsh -NoProfile -NonInteractive -Command $cmd 2>&1 | Out-Null
        $j = Get-Content -LiteralPath (Join-Path $sb.Home '.claude\state\automemory\lint-summary.json') -Raw | ConvertFrom-Json
        @($j.findings | Where-Object { $_.kind -eq 'compactor-starved' }).Count | Should -Be 0
    }
}
