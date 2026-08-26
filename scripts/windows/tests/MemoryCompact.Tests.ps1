# MemoryCompact.Tests.ps1 - behavioural tests for memory-compact.ps1, one per SAFETY GUARD.
#
# Seam: the compactor dot-sources memory-common.ps1 from its OWN directory, so each scenario
# runs a copy of the real compactor + real memory-store-lib.ps1 beside a STUB memory-common.ps1
# that scripts Codex, mem0 and the throttle. Nothing is mocked inside the compactor itself -
# the logic under test is the shipped logic. $env:USERPROFILE is redirected so no real store or
# state directory is ever touched.
BeforeAll {
    $script:WinDir = Split-Path -Parent $PSScriptRoot
    $script:EmDash = [string][char]0x2014

    function New-Sandbox {
        param(
            [string]$CodexPlanJson = '{"plan":[]}',
            [switch]$CodexThrows,
            [string]$Mem0Mode = 'ok',        # ok | noid | mismatch | fail
            [string]$MutateIndexDuringCodex  # text appended to the index while "Codex thinks" (CAS)
        )
        $root = Join-Path $TestDrive ([guid]::NewGuid().ToString('N').Substring(0, 8))
        $home_ = Join-Path $root 'home'
        $bin = Join-Path $root 'bin'
        foreach ($d in @($home_, $bin, (Join-Path $home_ '.claude\projects'), (Join-Path $home_ '.claude\state'), (Join-Path $home_ '.claude\logs'))) {
            [System.IO.Directory]::CreateDirectory($d) | Out-Null
        }
        Copy-Item (Join-Path $script:WinDir 'memory-compact.ps1') $bin
        Copy-Item (Join-Path $script:WinDir 'memory-store-lib.ps1') $bin
        $stub = @'
# stub memory-common.ps1 (test double)
$script:StubDir = Split-Path -Parent $MyInvocation.MyCommand.Path
function Initialize-MemoryEnv { }
function Write-MemoryLog { param($Component, $Message) Add-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\logs\compact-test.log') -Value $Message }
function Test-Throttle { param($Name, $MinIntervalSeconds) return $true }
function Mark-Throttle { param($Name) Set-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\throttle-marked') -Value '1' }
function Acquire-CodexLock { param($Owner, $MaxAgeMinutes) return $true }
function Release-CodexLock { }
function Get-Mem0Key { return 'test-key' }
function Invoke-CodexSubagent {
    param($Prompt, $ReasoningEffort, $TimeoutSeconds)
    Set-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\last-codex-prompt.txt') -Value $Prompt
    if ($env:STUB_CODEX_THROWS -eq '1') { throw 'codex.cmd not found (stub)' }
    if ($env:STUB_MUTATE_INDEX) {
        Add-Content -LiteralPath $env:STUB_MUTATE_TARGET -Value $env:STUB_MUTATE_INDEX
    }
    return ("header`ncodex`n" + $env:STUB_CODEX_PLAN + "`ntokens used`n42")
}
function Get-CodexResponseText {
    param($RawOutput)
    $lines = $RawOutput -split "`r?`n"
    $s = -1; $e = $lines.Length
    for ($i = $lines.Length - 1; $i -ge 0; $i--) {
        if ($lines[$i].Trim() -eq 'tokens used') { $e = $i }
        if ($lines[$i].Trim() -eq 'codex') { $s = $i + 1; break }
    }
    if ($s -lt 0) { return $RawOutput }
    return (($lines[$s..($e - 1)] -join "`n").Trim())
}
function Extract-JsonFromText {
    param($Text, $ExpectedKey)
    try { $o = $Text | ConvertFrom-Json } catch { return $null }
    if ($o.PSObject.Properties.Name -contains $ExpectedKey) { return $o }
    return $null
}
function Add-Mem0Memory {
    param($Text, $Source, $Metadata)
    Add-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\mem0-posts.txt') -Value ($Source + '||' + $Text)
    switch ($env:STUB_MEM0_MODE) {
        'noid'     { return $true }
        'fail'     { return $false }
        default    { Set-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\mem0-last.txt') -Value $Text; return 'stub-id-0001' }
    }
}
function Invoke-RestMethod {
    # shadows the cmdlet for the by-id read-back AND the undo delete
    param($Uri, $Headers, $TimeoutSec, $Method, $Body, $ContentType)
    if ($Method -eq 'Delete') {
        Add-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\mem0-deleted.txt') -Value ([string]$Uri)
        return [pscustomobject]@{ ok = $true }
    }
    if ($env:STUB_MEM0_MODE -eq 'mismatch') { return [pscustomobject]@{ memory = 'SOMETHING ELSE'; retrievable = $true } }
    $t = Get-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\mem0-last.txt') -Raw
    return [pscustomobject]@{ memory = $t.TrimEnd("`r", "`n"); retrievable = $true }
}
'@
        Set-Content -LiteralPath (Join-Path $bin 'memory-common.ps1') -Value $stub -Encoding UTF8
        return [pscustomobject]@{
            Root = $root; Home = $home_; Bin = $bin
            Projects = (Join-Path $home_ '.claude\projects')
            Plan = $CodexPlanJson; Throws = [bool]$CodexThrows; Mem0 = $Mem0Mode; Mutate = $MutateIndexDuringCodex
        }
    }

    function Add-SandboxStore {
        param($Sandbox, [string]$Workspace, [string[]]$IndexLines, [hashtable]$Facts)
        $dir = Join-Path (Join-Path $Sandbox.Projects $Workspace) 'memory'
        [System.IO.Directory]::CreateDirectory($dir) | Out-Null
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText((Join-Path $dir 'MEMORY.md'), (($IndexLines -join "`n") + "`n"), $utf8)
        foreach ($k in $Facts.Keys) { [System.IO.File]::WriteAllText((Join-Path $dir $k), $Facts[$k], $utf8) }
        return $dir
    }

    function New-FactFile {
        param([string]$Name, [string]$Desc, [string]$Type = 'project', [string]$Body = 'the body of the fact')
        return ("---`nname: $Name`ndescription: `"$Desc`"`nmetadata: `n  type: $Type`n  modified: 2026-07-01`n---`n`n$Body`n")
    }

    function Invoke-Compactor {
        param($Sandbox, [string[]]$ExtraArgs = @())
        $envAssign = @(
            '$env:USERPROFILE=' + "'" + $Sandbox.Home + "'"
            '$env:STUB_CODEX_PLAN=' + "'" + ($Sandbox.Plan -replace "'", "''") + "'"
            '$env:STUB_CODEX_THROWS=' + "'" + $(if ($Sandbox.Throws) { '1' } else { '0' }) + "'"
            '$env:STUB_MEM0_MODE=' + "'" + $Sandbox.Mem0 + "'"
        )
        if ($Sandbox.Mutate) {
            $envAssign += '$env:STUB_MUTATE_INDEX=' + "'" + $Sandbox.Mutate + "'"
            $envAssign += '$env:STUB_MUTATE_TARGET=' + "'" + $Sandbox.MutateTarget + "'"
        }
        $argLine = if ($ExtraArgs.Count) { ' ' + ($ExtraArgs -join ' ') } else { '' }
        $cmd = ($envAssign -join '; ') + '; & "' + (Join-Path $Sandbox.Bin 'memory-compact.ps1') + '"' + $argLine
        $out = & pwsh -NoProfile -NonInteractive -Command $cmd 2>&1
        $receiptPath = Join-Path $Sandbox.Home '.claude\state\automemory\compact-receipts.jsonl'
        $receipts = @()
        if (Test-Path -LiteralPath $receiptPath) {
            foreach ($l in (Get-Content -LiteralPath $receiptPath)) { if ($l.Trim()) { $receipts += ($l | ConvertFrom-Json) } }
        }
        return [pscustomobject]@{ Output = ($out | Out-String); Receipts = $receipts }
    }

    function Get-Bytes { param([string]$P) return ([System.IO.File]::ReadAllBytes($P)).Length }

    # A big index: 60 long lines (over the 130 B hook cap) => over the 20,000 B trigger.
    function New-BigIndexLines {
        param([int]$Count = 60, [string]$Prefix = 'fact')
        $lines = @('# Memory Index', '')
        for ($i = 1; $i -le $Count; $i++) {
            $lines += ('- [Fact ' + $i + '](' + $Prefix + $i + '.md) ' + $script:EmDash + ' ' + ('detail number ' + $i + ' ') * 22)
        }
        return $lines
    }
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
