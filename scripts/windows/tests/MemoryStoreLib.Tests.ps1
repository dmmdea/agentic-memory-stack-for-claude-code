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
        $stores = @(Get-AmStores -ProjectsRoot $root)
        ($stores | Where-Object { -not $_.IsAlias }).Count | Should -Be 1
        ($stores | Where-Object { $_.Workspace -eq 'empty' }).Count | Should -Be 0
        if ($made) {
            ($stores | Where-Object IsAlias).Workspace | Should -Be 'ws a alias'
            ($stores | Where-Object IsAlias).AliasOf | Should -Be 'ws-a'
        }
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
