# MemoryStoreLib.Tests.ps1 - the auto-memory maintenance primitives (memory-store-lib.ps1).
# Every safety property the compactor relies on is proven here on synthetic stores under
# $TestDrive; no real store is ever touched (Get-AmProjectsRoot is overridden per test).
BeforeAll {
    . (Join-Path (Split-Path -Parent $PSScriptRoot) 'memory-store-lib.ps1')
    $script:EmDash = [string][char]0x2014

    function New-TestStore {
        param([string]$Root, [string]$Workspace, [string[]]$IndexLines, [hashtable]$Facts, [string]$Newline = "`n")
        $dir = Join-Path (Join-Path $Root $Workspace) 'memory'
        [System.IO.Directory]::CreateDirectory($dir) | Out-Null
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText((Join-Path $dir 'MEMORY.md'), (($IndexLines -join $Newline) + $Newline), $utf8)
        foreach ($k in $Facts.Keys) { [System.IO.File]::WriteAllText((Join-Path $dir $k), $Facts[$k], $utf8) }
        return $dir
    }
    function New-Fact {
        param([string]$Name, [string]$Desc, [string]$Type = 'project', [string]$Body = 'body text')
        return ("---`nname: $Name`ndescription: `"$Desc`"`nmetadata: `n  node_type: memory`n  type: $Type`n  modified: 2026-08-01`n---`n`n$Body`n")
    }
}

Describe 'byte-exact I/O' {
    It 'round-trips a BOM-less LF index containing em-dashes byte-for-byte' {
        $p = Join-Path $TestDrive 'idx.md'
        $line = '- [Title](t.md) ' + $script:EmDash + ' caf' + [char]0xE9 + ' rule'
        $bytesIn = (New-Object System.Text.UTF8Encoding($false)).GetBytes($line + "`n" + $line + "`n")
        [System.IO.File]::WriteAllBytes($p, $bytesIn)
        $text = Read-AmText -Path $p
        Write-AmTextAtomic -Path $p -Text $text
        $bytesOut = [System.IO.File]::ReadAllBytes($p)
        $bytesOut.Length | Should -Be $bytesIn.Length
        [System.BitConverter]::ToString($bytesOut) | Should -Be ([System.BitConverter]::ToString($bytesIn))
        $bytesOut[0] | Should -Be 0x2D -Because 'no BOM may be prepended'
        (Get-ChildItem $TestDrive -Filter '*.am-tmp').Count | Should -Be 0 -Because 'the temp file is swapped in, never left behind'
    }

    It 'counts UTF-8 BYTES, not characters (an em-dash is 3 bytes)' {
        (Get-AmByteCount -Text ('a' + $script:EmDash + 'b')) | Should -Be 5
    }

    It 'preserves CRLF when the store already uses it' {
        (Get-AmNewline -Text "a`r`nb") | Should -Be "`r`n"
        (Get-AmNewline -Text "a`nb") | Should -Be "`n"
    }
}

Describe 'index parsing and regeneration' {
    It 'parses entries, keeps other lines verbatim, and regenerates untouched lines byte-for-byte' {
        $text = "# Memory Index`n`n- [A title](a.md) " + $script:EmDash + " hook one`n- [B](b.md)`nnot an entry`n"
        $idx = Read-AmIndex -Text $text
        $entries = @($idx.Records | Where-Object Kind -eq 'entry')
        $entries.Count | Should -Be 2
        $entries[0].Slug | Should -Be 'a.md'
        $entries[0].Summary | Should -Be 'hook one'
        $entries[1].Summary | Should -Be ''
        (ConvertTo-AmIndexText -Records $idx.Records -Newline $idx.Newline) | Should -Be $text
    }

    It 'rebuilds only records marked Dirty' {
        $text = "- [A](a.md) " + $script:EmDash + " long long summary`n- [B](b.md) " + $script:EmDash + " keep`n"
        $idx = Read-AmIndex -Text $text
        $idx.Records[0] | Add-Member -NotePropertyName Dirty -NotePropertyValue $true
        $idx.Records[0].Summary = 'short'
        $out = ConvertTo-AmIndexText -Records $idx.Records -Newline $idx.Newline
        $out | Should -Be ("- [A](a.md) " + $script:EmDash + " short`n- [B](b.md) " + $script:EmDash + " keep`n")
    }

    It 'recognises extra links in a merged line for orphan accounting' {
        $idx = Read-AmIndex -Text ("- [A](a.md) " + $script:EmDash + " merged (also [B](b.md))`n")
        $linked = Get-AmIndexLinkedSlugs -Records $idx.Records
        $linked.ContainsKey('a.md') | Should -BeTrue
        $linked.ContainsKey('b.md') | Should -BeTrue
    }
}

Describe 'review-round primitives' {
    It 'ghosts come from ENTRY links only; non-entry mentions count for reachability, never as ghosts' {
        $text = "# Index (older notes moved to (archive.md))`n- [A](a.md) " + $script:EmDash + " see [B](b.md) and [dead](dead.md)`n"
        $idx = Read-AmIndex -Text $text
        $onDisk = @{ 'a.md' = $true; 'b.md' = $true }
        $g = @(Get-AmEntryGhosts -Records $idx.Records -OnDisk $onDisk)
        $g | Should -Be @('dead.md')
        (Get-AmIndexLinkedSlugs -Records $idx.Records).ContainsKey('archive.md') | Should -BeTrue -Because 'a file mentioned by a heading is reachable (not an orphan) even though it is not a ghost'
    }

    It 'round-trip check accepts exactly the expected extra links' {
        $line = '- [A](a.md) ' + $script:EmDash + ' see [B](b.md)'
        Test-AmLineRoundTrips -Line $line -ExpectedSlug 'a.md' | Should -BeFalse
        Test-AmLineRoundTrips -Line $line -ExpectedSlug 'a.md' -ExpectedExtras @('b.md') | Should -BeTrue
        Test-AmLineRoundTrips -Line $line -ExpectedSlug 'a.md' -ExpectedExtras @('c.md') | Should -BeFalse
    }

    It 'a list item inside a code fence is not an entry' {
        $text = "- [Real](real.md)`n" + '```' + "`n- [Example](example.md) - illustration`n" + '```' + "`n- [Also real](also.md)`n"
        $idx = Read-AmIndex -Text $text
        @($idx.Records | Where-Object Kind -eq 'entry').Slug | Should -Be @('real.md', 'also.md')
        (ConvertTo-AmIndexText -Records $idx.Records -Newline $idx.Newline) | Should -Be $text
    }

    It 'a bracketed title and an indented pointer parse as entries and round-trip byte-for-byte' {
        $text = "- [Title [with] brackets](b.md) " + $script:EmDash + " hook`n  - [Nested](n.md) " + $script:EmDash + " nested`n"
        $idx = Read-AmIndex -Text $text
        $e = @($idx.Records | Where-Object Kind -eq 'entry')
        $e.Count | Should -Be 2
        $e[0].Title | Should -Be 'Title [with] brackets'
        $e[1].Indent | Should -Be '  '
        $e[1] | Add-Member -NotePropertyName Dirty -NotePropertyValue $true
        (ConvertTo-AmIndexText -Records $idx.Records -Newline $idx.Newline) | Should -Be $text -Because 'a dirty indented entry must keep its indentation'
    }

    It 'an EMPTY state file throws instead of reading as "no state"' {
        $p = Join-Path $TestDrive 'empty.json'
        Set-Content -Path $p -Value ''
        { Read-AmJsonFile -Path $p } | Should -Throw
        Read-AmJsonFile -Path (Join-Path $TestDrive 'absent.json') | Should -BeNullOrEmpty
    }

    It 'byte truncation never splits a surrogate pair' {
        $emoji = [char]::ConvertFromUtf32(0x1F600)   # 4 bytes in UTF-8, 2 UTF-16 code units
        $t = ('x' * 97) + $emoji + 'tail'
        $out = Get-AmTruncatedToBytes -Text $t -MaxBytes 99
        $out | Should -Not -Match ([regex]::Escape([string][char]0xFFFD))
        foreach ($i in 0..($out.Length - 1)) { [char]::IsHighSurrogate($out[$i]) -and ($i -eq $out.Length - 1) | Should -BeFalse }
        (Get-AmByteCount -Text $out) | Should -BeLessOrEqual 99
    }

    It 'temp-file sweep reports what it removed and what it could not' {
        $d = Join-Path $TestDrive 'sweep'
        [System.IO.Directory]::CreateDirectory($d) | Out-Null
        Set-Content -Path (Join-Path $d 'MEMORY.md.am-tmp') -Value 'leftover'
        $r = Clear-AmStoreTempFiles -Dir $d
        @($r.Removed) | Should -Be @('MEMORY.md.am-tmp')
        @($r.Failed).Count | Should -Be 0
        $fs = [System.IO.File]::Open((Join-Path $d 'locked.am-tmp'), 'Create', 'ReadWrite', 'None')
        try {
            $r2 = Clear-AmStoreTempFiles -Dir $d
            @($r2.Failed).Count | Should -Be 1 -Because 'a locked temp file must be reported, not swallowed'
        } finally { $fs.Dispose() }
    }
}

Describe 'frontmatter and the doctrine hard rule' {
    It 'reads type NESTED under metadata (the shape every real fact file uses)' {
        $p = Join-Path $TestDrive 'fb.md'
        [System.IO.File]::WriteAllText($p, (New-Fact -Name 'x' -Desc ('Daniel ' + $script:EmDash + ' "quoted" inner') -Type 'feedback'), (New-Object System.Text.UTF8Encoding($false)))
        $fm = Read-AmFrontmatter -Path $p
        $fm.Type | Should -Be 'feedback'
        $fm.Name | Should -Be 'x'
        $fm.Description | Should -Match 'quoted'
        $fm.Modified | Should -Be '2026-08-01'
    }

    It 'returns $null for a file without a frontmatter block' {
        $p = Join-Path $TestDrive 'nofm.md'
        Set-Content -Path $p -Value 'just text'
        Read-AmFrontmatter -Path $p | Should -BeNullOrEmpty
    }

    It 'imperative test mirrors imperative_canary.py examples (fires)' {
        foreach ($t in @('You MUST use X for all calls', 'NEVER bind port 80', 'DO NOT redeploy without approval', 'RULE: do Y', "Ollama is decommissioned.`nNEVER re-register it.", 'It is retired. Always re-register it.')) {
            Test-AmImperative -Text $t | Should -BeTrue -Because $t
        }
    }

    It 'imperative test mirrors imperative_canary.py examples (declarative facts pass)' {
        foreach ($t in @('The reserved ports are 80 and 443', 'Ollama :11434 is decommissioned', 'Postiz is a retired, forbidden social scheduler', 'The operator must approve deploys', 'over-long inputs defer.')) {
            Test-AmImperative -Text $t | Should -BeFalse -Because $t
        }
    }

    It 'classifies a feedback-type file as doctrine even with a bland summary' {
        $rec = [pscustomobject]@{ Summary = 'on any key: write to the store now' }
        $fm = [pscustomobject]@{ Type = 'feedback'; Description = 'x' }
        Test-AmDoctrine -Record $rec -Frontmatter $fm | Should -BeTrue
    }

    It 'classifies an imperative summary as doctrine even when type=project' {
        $rec = [pscustomobject]@{ Summary = 'NEVER pin a launcher to a versioned path' }
        Test-AmDoctrine -Record $rec -Frontmatter ([pscustomobject]@{ Type = 'project'; Description = '' }) | Should -BeTrue
    }

    It 'leaves a plain location fact eligible' {
        $rec = [pscustomobject]@{ Summary = 'pricing PDFs live at the workspace root' }
        Test-AmDoctrine -Record $rec -Frontmatter ([pscustomobject]@{ Type = 'reference'; Description = 'where the PDFs are' }) | Should -BeFalse
    }

    It 'extracts anchor tokens (numbers, paths, backticks, ALL-CAPS)' {
        $a = Get-AmAnchorTokens -Text ('port 18791, C:\Users\x\.mem0, `Test-Throttle`, DPAPI phase 3')
        $a.ContainsKey('18791') | Should -BeTrue
        $a.ContainsKey('Test-Throttle') | Should -BeTrue
        $a.ContainsKey('DPAPI') | Should -BeTrue
        $a.Keys | Where-Object { $_ -like 'C:\Users*' } | Should -Not -BeNullOrEmpty
    }
}

Describe 'store enumeration' {
    It 'finds populated stores, skips empty scaffolds, and dedups a junction alias' {
        $root = Join-Path $TestDrive 'projects1'
        New-TestStore -Root $root -Workspace 'ws-a' -IndexLines @('- [A](a.md)') -Facts @{ 'a.md' = (New-Fact 'a' 'd') } | Out-Null
        [System.IO.Directory]::CreateDirectory((Join-Path $root 'empty\memory')) | Out-Null
        $target = Join-Path $root 'ws-a'
        $alias = Join-Path $root 'ws a alias'
        $made = $false
        try { New-Item -ItemType Junction -Path $alias -Target $target -ErrorAction Stop | Out-Null; $made = $true } catch { $made = $false }
        if (-not $made) { Set-ItResult -Skipped -Because 'this environment cannot create a junction (the alias half of the test is unverifiable here)' }
        $stores = @(Get-AmStores -ProjectsRoot $root)
        ($stores | Where-Object { -not $_.IsAlias }).Count | Should -Be 1
        ($stores | Where-Object { $_.Workspace -eq 'empty' }).Count | Should -Be 0
        ($stores | Where-Object IsAlias).Workspace | Should -Be 'ws a alias'
        ($stores | Where-Object IsAlias).AliasOf | Should -Be 'ws-a'
        # The canonical store must carry BOTH directories, so the liveness probe covers a
        # session running under the alias path.
        $canon = $stores | Where-Object { -not $_.IsAlias }
        @($canon.ProbeDirs).Count | Should -Be 2
    }

    It 'the physical directory is canonical even when the alias sorts first' {
        # Sorting by name alone once crowned the alias (spaces sort before dashes), so the job
        # would have mutated the store THROUGH the link and reported the real one as the alias.
        $root = Join-Path $TestDrive 'projects1b'
        New-TestStore -Root $root -Workspace 'zzz-real' -IndexLines @('- [A](a.md)') -Facts @{ 'a.md' = (New-Fact 'a' 'd') } | Out-Null
        $made = $false
        try { New-Item -ItemType Junction -Path (Join-Path $root 'aaa alias') -Target (Join-Path $root 'zzz-real') -ErrorAction Stop | Out-Null; $made = $true } catch {}
        if (-not $made) { Set-ItResult -Skipped -Because 'cannot create a junction here' }
        $stores = @(Get-AmStores -ProjectsRoot $root)
        ($stores | Where-Object { -not $_.IsAlias }).Workspace | Should -Be 'zzz-real'
    }
}

Describe 'Windows PowerShell 5.1 parity' {
    # The library is dot-sourced by memory-lint.ps1 under 5.1 and by memory-compact.ps1 under
    # pwsh 7. Everything above runs under 7; without this, the dual-edition contract the file's
    # header depends on is asserted by nothing.
    It 'loads under 5.1 and round-trips a multi-byte index byte-for-byte' {
        $ps51 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        if (-not (Test-Path $ps51)) { Set-ItResult -Skipped -Because 'Windows PowerShell 5.1 is not present' }
        $lib = Join-Path (Split-Path -Parent $PSScriptRoot) 'memory-store-lib.ps1'
        $work = Join-Path $TestDrive 'ps51'
        [System.IO.Directory]::CreateDirectory($work) | Out-Null
        $idx = Join-Path $work 'MEMORY.md'
        $line = '- [Caf' + [char]0xE9 + '](x.md) ' + $script:EmDash + ' hook with 3-byte dash'
        [System.IO.File]::WriteAllBytes($idx, (New-Object System.Text.UTF8Encoding($false)).GetBytes($line + "`n"))
        $script = @"
. '$lib'
`$t = Read-AmText -Path '$idx'
Write-AmTextAtomic -Path '$idx' -Text `$t
`$i = Read-AmIndex -Text `$t
`$e = @(`$i.Records | Where-Object { `$_.Kind -eq 'entry' })
Write-Output ('entries=' + `$e.Count + ' slug=' + `$e[0].Slug + ' bytes=' + (Get-AmByteCount -Text `$t) + ' imperative=' + (Test-AmImperative -Text 'NEVER bind port 80'))
"@
        $out = & $ps51 -NoProfile -ExecutionPolicy Bypass -Command $script 2>&1 | Out-String
        $out | Should -Match 'entries=1 slug=x\.md bytes=\d+ imperative=True' -Because "the library must behave identically under 5.1; got: $out"
        $bytes = [System.IO.File]::ReadAllBytes($idx)
        $bytes[0] | Should -Be 0x2D -Because '5.1 must not prepend a BOM when rewriting a store file'
        $bytes.Length | Should -Be ((New-Object System.Text.UTF8Encoding($false)).GetByteCount($line + "`n"))
    }
}

Describe 'lint findings (stateless)' {
    It 'reports orphan, dangling, dup-slug, long-line, oversized-file, no-frontmatter' {
        $root = Join-Path $TestDrive 'projects2'
        $long = '- [L](l.md) ' + $script:EmDash + ' ' + ('x' * 140)
        New-TestStore -Root $root -Workspace 'ws' -IndexLines @('- [A](a.md)', '- [A again](a.md)', '- [Gone](gone.md)', $long) -Facts @{
            'a.md' = (New-Fact 'a' 'd'); 'l.md' = (New-Fact 'l' 'd'); 'orphan.md' = (New-Fact 'o' 'd')
            'big.md' = (New-Fact 'big' 'd' 'project' ('z' * 12000)); 'raw.md' = 'no frontmatter here'
        } | Out-Null
        $store = @(Get-AmStores -ProjectsRoot $root)[0]
        $f = @(Get-AmLintFindings -Store $store)
        ($f | Where-Object kind -eq 'orphan').file | Should -Contain 'orphan.md'
        ($f | Where-Object kind -eq 'orphan').file | Should -Contain 'big.md'
        ($f | Where-Object kind -eq 'dangling').file | Should -Be 'gone.md'
        ($f | Where-Object kind -eq 'dup-slug').file | Should -Be 'a.md'
        ($f | Where-Object kind -eq 'long-line').file | Should -Be 'l.md'
        ($f | Where-Object kind -eq 'oversized-file').file | Should -Be 'big.md'
        ($f | Where-Object kind -eq 'no-frontmatter').file | Should -Be 'raw.md'
    }

    It 'a clean store yields zero findings and correct stats' {
        $root = Join-Path $TestDrive 'projects3'
        New-TestStore -Root $root -Workspace 'ws' -IndexLines @('# Index', '', ('- [A](a.md) ' + $script:EmDash + ' hook')) -Facts @{ 'a.md' = (New-Fact 'a' 'd') } | Out-Null
        $store = @(Get-AmStores -ProjectsRoot $root)[0]
        @(Get-AmLintFindings -Store $store).Count | Should -Be 0
        $s = Get-AmStoreStats -Store $store
        $s.Lines | Should -Be 3
        $s.Entries | Should -Be 1
        $s.Files | Should -Be 1
        $s.OverTrigger | Should -BeFalse
    }
}

Describe 'session liveness probe' {
    It 'is live when a transcript in that workspace was written recently, not otherwise' {
        $root = Join-Path $TestDrive 'projects4'
        $ws = Join-Path $root 'ws'
        [System.IO.Directory]::CreateDirectory($ws) | Out-Null
        $t = Join-Path $ws 'session.jsonl'
        Set-Content -Path $t -Value '{}'
        Test-AmWorkspaceLive -Workspace 'ws' -ProjectsRoot $root | Should -BeTrue
        (Get-Item $t).LastWriteTimeUtc = [DateTime]::UtcNow.AddHours(-3)
        Test-AmWorkspaceLive -Workspace 'ws' -ProjectsRoot $root | Should -BeFalse
        Test-AmWorkspaceLive -Workspace 'absent' -ProjectsRoot $root | Should -BeFalse
    }
}

Describe 'out-of-tree git history' {
    BeforeAll {
        $script:hRoot = Join-Path $TestDrive 'projects5'
        $script:hGit  = Join-Path $TestDrive 'state\history.git'
        New-TestStore -Root $script:hRoot -Workspace 'ws' -IndexLines @('- [A](a.md)', '- [B](b.md)') -Facts @{ 'a.md' = (New-Fact 'a' 'd'); 'b.md' = (New-Fact 'b' 'd') } | Out-Null
        # a transcript that must NEVER be tracked
        Set-Content -Path (Join-Path $script:hRoot 'ws\transcript.jsonl') -Value '{"secret":1}'
        Initialize-AmHistory -GitDir $script:hGit -WorkTree $script:hRoot | Out-Null
        $script:store = @(Get-AmStores -ProjectsRoot $script:hRoot)[0]
    }

    It 'creates no .git inside the tree and has no remote' {
        Test-Path (Join-Path $script:hRoot '.git') | Should -BeFalse
        Test-Path (Join-Path $script:hRoot 'ws\memory\.git') | Should -BeFalse
        Test-AmHistoryHasRemote -GitDir $script:hGit | Should -BeFalse
    }

    It 'snapshots the store only (transcripts excluded), and a second identical snapshot is a no-op' {
        $sha1 = Save-AmHistorySnapshot -Store $script:store -Message 'snap 1' -GitDir $script:hGit -WorkTree $script:hRoot
        $sha1 | Should -Match '^[0-9a-f]{40}$'
        (Invoke-AmGit -GitArgs @('ls-tree', '-r', '--name-only', $sha1) -GitDir $script:hGit -WorkTree $script:hRoot).Out | Should -Not -Match 'jsonl'
        Test-AmHistoryHasFile -Sha $sha1 -RelPath 'ws/memory/b.md' -GitDir $script:hGit | Should -BeTrue
        (Save-AmHistorySnapshot -Store $script:store -Message 'snap 1 again' -GitDir $script:hGit -WorkTree $script:hRoot) | Should -BeNullOrEmpty
    }

    It 'records a deletion and restores exactly that file, leaving newer files alone' {
        $sha1 = (Invoke-AmGit -GitArgs @('rev-parse', 'HEAD') -GitDir $script:hGit -WorkTree $script:hRoot).Out.Trim()
        Remove-Item (Join-Path $script:hRoot 'ws\memory\b.md')
        Set-Content -Path (Join-Path $script:hRoot 'ws\memory\new-by-session.md') -Value 'live session wrote this'
        $sha2 = Save-AmHistorySnapshot -Store $script:store -Message 'after delete' -GitDir $script:hGit -WorkTree $script:hRoot
        Test-AmHistoryHasFile -Sha $sha2 -RelPath 'ws/memory/b.md' -GitDir $script:hGit | Should -BeFalse
        (Restore-AmHistoryFile -Sha $sha1 -RelPath 'ws/memory/b.md' -GitDir $script:hGit -WorkTree $script:hRoot) | Should -BeTrue
        Test-Path (Join-Path $script:hRoot 'ws\memory\b.md') | Should -BeTrue
        Test-Path (Join-Path $script:hRoot 'ws\memory\new-by-session.md') | Should -BeTrue -Because 'per-file restore must never clobber a file the job did not touch'
        (Get-AmHistoryDiff -FromSha $sha1 -ToSha $sha2 -RelPath 'ws/memory' -GitDir $script:hGit -WorkTree $script:hRoot) | Should -Match 'b\.md'
    }
}
