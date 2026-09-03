# memory-store-lib.ps1 - shared primitives for maintaining Claude Code's native per-workspace
# auto-memory stores ("System A": ~/.claude/projects/<workspace>/memory/MEMORY.md + one fact
# file per index line). Dot-sourced by memory-lint.ps1 (SessionStart child, Windows PowerShell
# 5.1), memory-compact.ps1 (Task Scheduler, pwsh 7) and Test-MemoryStack.ps1.
#
# Design rules this file enforces (see docs/systems/auto-memory-maintenance.md):
#   - ASCII-only source: PS 5.1 reads a BOM-less file as ANSI, so a single em-dash in this
#     file would be tokenised as a smart quote. Non-ASCII runtime characters are built from
#     code points ([char]0x2014) never typed literally.
#   - Byte-exact I/O: index and fact files are read/written as UTF-8 WITHOUT a BOM and keep
#     their original line ending. Set-Content/Add-Content -Encoding UTF8 are never used on a
#     store file (PS 5.1 prepends a BOM and rewrites the newline).
#   - Maintainer state lives OUTSIDE the store (~/.claude/state/automemory/). The store is
#     synced by the harness and globbed by agents, so nothing but MEMORY.md and fact files may
#     ever be written inside it.
#   - Doctrine is untouchable: a fact whose frontmatter carries `metadata.type: feedback`
#     (NESTED - the top-level form matches zero real files) or whose text is an imperative
#     standing order is never dropped, merged or migrated by any job.
#   - History is git with the git-dir OUTSIDE the tree and no remote, ever.

$script:AmLineByteCap      = 130     # index line hook budget ("- [Title](file.md) - hook")
$script:AmTriggerBytes     = 20000   # compactor fires at/above either trigger ...
$script:AmTriggerLines     = 160
$script:AmTargetBytes      = 17000   # ... and aims at/below both targets (hysteresis)
$script:AmTargetLines      = 140
$script:AmSyncLimitBytes   = 25000   # harness: per-file sync limit (refuses to sync above)
$script:AmInjectLimitLines = 200     # harness: index lines injected into context
$script:AmOversizedFactBytes = 10000 # a single fact body this large is flagged (never edited)
$script:AmMem0MaxChars      = 4000  # mem0-server MAX_MEMORY_CHARS default: a body over this 413s on migration, forever
$script:AmEmDash           = [string][char]0x2014

function Get-AmUtf8 { return (New-Object System.Text.UTF8Encoding($false)) }

function Get-AmProjectsRoot {
    return (Join-Path $env:USERPROFILE '.claude\projects')
}

function Get-AmStateRoot {
    $p = Join-Path $env:USERPROFILE '.claude\state\automemory'
    if (-not (Test-Path -LiteralPath $p)) { [System.IO.Directory]::CreateDirectory($p) | Out-Null }
    return $p
}

function Get-AmWorkspaceStateDir {
    param([Parameter(Mandatory)][string]$Workspace)
    $p = Join-Path (Get-AmStateRoot) $Workspace
    if (-not (Test-Path -LiteralPath $p)) { [System.IO.Directory]::CreateDirectory($p) | Out-Null }
    return $p
}

# ---------------------------------------------------------------- byte-exact text I/O

function Read-AmText {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.File]::ReadAllText($Path, (Get-AmUtf8))
}

function Get-AmByteCount {
    param([AllowEmptyString()][string]$Text)
    if ($null -eq $Text) { return 0 }
    return (Get-AmUtf8).GetByteCount($Text)
}

function Get-AmNewline {
    # The store's PREVAILING line ending; new files default to LF (what the harness writes).
    # Majority, not "contains": a mostly-LF index with one stray CRLF would otherwise be
    # rewritten wholly to CRLF, changing every line the job never touched.
    param([AllowEmptyString()][string]$Text)
    if (-not $Text) { return "`n" }
    $crlf = ([regex]::Matches($Text, "`r`n")).Count
    if ($crlf -eq 0) { return "`n" }
    $lf = ([regex]::Matches($Text, "`n")).Count
    if ($crlf * 2 -ge $lf) { return "`r`n" }
    return "`n"
}

function Write-AmTextAtomic {
    # Write UTF-8 (no BOM) to a temp file beside the target, then swap it in. A crash mid-write
    # leaves the original untouched; a reader never sees a torn file.
    param([Parameter(Mandatory)][string]$Path, [AllowEmptyString()][string]$Text)
    $tmp = $Path + '.am-tmp'
    [System.IO.File]::WriteAllText($tmp, $Text, (Get-AmUtf8))
    try {
        if (Test-Path -LiteralPath $Path) {
            # $null binds as "" on a .NET string parameter; NullString is the real null.
            [System.IO.File]::Replace($tmp, $Path, [NullString]::Value)
        } else {
            [System.IO.File]::Move($tmp, $Path)
        }
    } catch {
        # The swap failed (sharing violation, ACL, cross-volume). Never leave a full copy of
        # the index behind in the store - that directory is synced and globbed.
        try { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } catch {}
        throw
    }
}

function Get-AmContentHash {
    param([Parameter(Mandatory)][string]$Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

# ---------------------------------------------------------------- store enumeration

function Resolve-AmReparseTarget {
    # A workspace dir may be a junction/symlink alias of another (observed: the same store
    # reachable under a spaces-vs-dashes name). Returns the canonical directory path, or $null
    # when the link cannot be resolved.
    #
    # Returning $Dir on failure would be fail-OPEN: an unresolvable junction would be crowned
    # its own canonical store and the same physical store compacted TWICE in one run, the
    # second pass reading the first pass's output and defeating both the seal and the blast cap.
    param([Parameter(Mandatory)][string]$Dir)
    try {
        $item = Get-Item -LiteralPath $Dir -ErrorAction Stop
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            $t = $null
            try { $t = $item.Target } catch { $t = $null }
            if ($t) { return ([string]@($t)[0]).TrimEnd('\') }
            return $null   # it IS a link and we could not read where it points
        }
    } catch { return $null }
    return $Dir.TrimEnd('\')
}

function Get-AmStores {
    # Every populated store under the projects root, deduplicated by canonical path so an
    # alias is never processed twice (mutating through an alias = mutating the real store).
    # Returns objects: Workspace, Dir (memory dir), IndexPath, CanonicalDir, IsAlias.
    param([string]$ProjectsRoot = (Get-AmProjectsRoot))
    $out = @()
    if (-not (Test-Path -LiteralPath $ProjectsRoot)) { return @() }
    $seen = @{}
    # Real directories first, then reparse points, so the physical store is always the
    # canonical one and an alias can never be crowned by sort order.
    $dirs = @(Get-ChildItem -LiteralPath $ProjectsRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object @{ Expression = { [bool]($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) } }, Name)
    $rows = @()
    foreach ($d in $dirs) {
        $memDir = Join-Path $d.FullName 'memory'
        $idx = Join-Path $memDir 'MEMORY.md'
        if (-not (Test-Path -LiteralPath $idx)) { continue }
        $target = Resolve-AmReparseTarget -Dir $d.FullName
        if ($null -eq $target) {
            # Unresolvable link: treat as an alias of nothing, i.e. never mutate through it.
            $rows += [pscustomobject]@{
                Workspace = $d.Name; Dir = $memDir; IndexPath = $idx
                CanonicalDir = $null; IsAlias = $true; AliasOf = '(unresolved link)'; WorkspaceDir = $d.FullName
            }
            continue
        }
        $canon = (Join-Path $target 'memory').ToLowerInvariant()
        $isAlias = $seen.ContainsKey($canon)
        if (-not $isAlias) { $seen[$canon] = $d.Name }
        $rows += [pscustomobject]@{
            Workspace    = $d.Name
            Dir          = $memDir
            IndexPath    = $idx
            CanonicalDir = $canon
            IsAlias      = $isAlias
            AliasOf      = $(if ($isAlias) { $seen[$canon] } else { $null })
            WorkspaceDir = $d.FullName
        }
    }
    # Give every canonical store the list of workspace directories that reach it. A session
    # running under an ALIAS path writes its transcripts into the alias directory, so a liveness
    # probe that only looks at the canonical name would miss it entirely - and the liveness gate
    # is the guard that exists because a live session was observed writing mid-run.
    foreach ($r in $rows) {
        $dirsForStore = @($r.WorkspaceDir)
        if (-not $r.IsAlias -and $r.CanonicalDir) {
            foreach ($o in $rows) {
                if ($o.IsAlias -and $o.CanonicalDir -eq $r.CanonicalDir) { $dirsForStore += $o.WorkspaceDir }
            }
        }
        $r | Add-Member -NotePropertyName ProbeDirs -NotePropertyValue @($dirsForStore) -Force
        $out += $r
    }
    # Plain return: callers wrap in @(). A `return ,$out` here hands back ONE element that IS
    # the array (observed: every store concatenated into a single object under 5.1 and 7).
    return $out
}

function Get-AmFactFiles {
    # Fact files = every *.md in the store except the index. Never recurses: subdirectories
    # are not part of the harness contract and must never be created by a maintainer.
    #
    # THROWS on an enumeration failure, deliberately. With -ErrorAction SilentlyContinue an
    # unreadable directory and an empty one both returned @(), and a caller comparing the index
    # against that empty set concludes every line is dangling - then every downstream guard
    # agrees, because they all compare against the same empty set, and the whole index is wiped
    # with a receipt reporting success. "Could not read" must never be spellable as "nothing
    # there".
    param([Parameter(Mandatory)][string]$Dir)
    return @(Get-ChildItem -LiteralPath $Dir -Filter '*.md' -File -ErrorAction Stop |
        Where-Object { $_.Name -ne 'MEMORY.md' } | Sort-Object Name)
}

function Clear-AmStoreTempFiles {
    # A crashed atomic write can leave a full copy of the index as <name>.am-tmp INSIDE the
    # store, where the harness syncs it and agents glob it. Sweep them; a leftover is evidence
    # of a previously failed write, so the caller logs what it removed.
    # Returns @{ Removed; Failed } - a temp file that could NOT be removed (locked) is a full
    # copy of the index sitting in a synced, globbed directory: the case to shout about, never
    # one to swallow.
    param([Parameter(Mandatory)][string]$Dir)
    $removed = @(); $failed = @()
    foreach ($f in @(Get-ChildItem -LiteralPath $Dir -Filter '*.am-tmp' -File -ErrorAction SilentlyContinue)) {
        try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop; $removed += $f.Name }
        catch { $failed += ($f.Name + ': ' + $_.Exception.Message) }
    }
    return [pscustomobject]@{ Removed = @($removed); Failed = @($failed) }
}

# ---------------------------------------------------------------- index parsing

# Lazy title so a title containing "]" still parses; leading whitespace so an indented pointer
# is an entry rather than opaque text. Both shapes previously fell through to Kind='other'.
$script:AmEntryRegex = [regex]'^(?<indent>\s*)- \[(?<title>.*?)\]\((?<slug>[^)\s]+\.md)\)(?<rest>.*)$'
$script:AmLinkRegex  = [regex]'\(([^)\s]+\.md)\)'

function Read-AmIndex {
    # Parses the index into records. Every line is preserved verbatim in Raw (blank lines and
    # headings included) so the regenerator can reproduce untouched lines byte-for-byte.
    param([Parameter(Mandatory)][string]$Text)
    $nl = Get-AmNewline -Text $Text
    # [regex]::Split keeps the trailing empty element for a newline-terminated file, so the
    # regenerator reproduces the final newline. (-split's count argument means something else.)
    $lines = [regex]::Split($Text, "\r?\n")
    $records = New-Object System.Collections.ArrayList
    $i = 0
    $inFence = $false
    foreach ($ln in $lines) {
        # A list item inside a ``` code fence is text, not a pointer. Without fence tracking a
        # fenced example line would be treated as an entry and its (example) target "dangling".
        if ($ln -match '^\s*(```|~~~)') { $inFence = -not $inFence }
        $m = $null
        if (-not $inFence) { $m = $script:AmEntryRegex.Match($ln) }
        if ($m -and $m.Success) {
            $rest = $m.Groups['rest'].Value
            # summary = rest with the leading separator (em-dash / hyphen / colon) stripped
            $summary = ($rest -replace ('^\s*(?:' + $script:AmEmDash + '|--?|:)\s*'), '').TrimEnd()
            $extra = @()
            foreach ($lm in $script:AmLinkRegex.Matches($rest)) { $extra += $lm.Groups[1].Value }
            [void]$records.Add([pscustomobject]@{
                Index   = $i
                Kind    = 'entry'
                Raw     = $ln
                Indent  = $m.Groups['indent'].Value
                Title   = $m.Groups['title'].Value
                Slug    = $m.Groups['slug'].Value
                Summary = $summary
                ExtraSlugs = $extra
                Bytes   = Get-AmByteCount -Text $ln
            })
        } else {
            [void]$records.Add([pscustomobject]@{
                Index = $i; Kind = 'other'; Raw = $ln; Indent = ''; Title = $null; Slug = $null
                Summary = $null; ExtraSlugs = @(); Bytes = (Get-AmByteCount -Text $ln)
            })
        }
        $i++
    }
    return [pscustomobject]@{ Newline = $nl; Records = $records; Lines = $lines.Count }
}

function New-AmEntryLine {
    param(
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][string]$Slug,
        [AllowEmptyString()][string]$Summary,
        [AllowEmptyString()][string]$Indent = ''
    )
    $line = $Indent + '- [' + $Title + '](' + $Slug + ')'
    if ($Summary) { $line += ' ' + $script:AmEmDash + ' ' + $Summary }
    return $line
}

function ConvertTo-AmIndexText {
    # Regenerates the index from records. Entries flagged Dirty are rebuilt from their fields;
    # everything else is emitted from Raw so a line the job never touched cannot change.
    param([Parameter(Mandatory)]$Records, [Parameter(Mandatory)][string]$Newline)
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($r in $Records) {
        if ($r.Kind -eq 'entry' -and $r.PSObject.Properties['Dirty'] -and $r.Dirty) {
            $ind = ''
            if ($r.PSObject.Properties['Indent'] -and $r.Indent) { $ind = $r.Indent }
            $out.Add((New-AmEntryLine -Title $r.Title -Slug $r.Slug -Summary $r.Summary -Indent $ind))
        } else {
            $out.Add($r.Raw)
        }
    }
    return ($out -join $Newline)
}

function Get-AmIndexLinkedSlugs {
    # Every fact file the index references, from ANY line - parsed entry or not.
    #
    # Deliberately independent of entry parsing. A line whose title contains "]" or that is
    # indented does not match the entry regex, so treating only entries as "linked" reported its
    # file as an ORPHAN; the compactor then appended a SECOND pointer to a file that was already
    # indexed, growing a store it exists to shrink (reproduced in review: 24,014 -> 24,101 B on
    # a store already at 96% of the sync limit). Reachability must be decided by "is there a
    # link to it anywhere", never by "did my regex like the line".
    param([Parameter(Mandatory)]$Records)
    $set = @{}
    foreach ($r in $Records) {
        if ($r.Kind -eq 'entry') {
            $set[$r.Slug] = $true
            foreach ($e in $r.ExtraSlugs) { $set[$e] = $true }
            continue
        }
        foreach ($m in $script:AmLinkRegex.Matches([string]$r.Raw)) { $set[$m.Groups[1].Value] = $true }
    }
    return $set
}

# ---------------------------------------------------------------- frontmatter + doctrine

function Read-AmFrontmatter {
    # Lenient by design: real files carry trailing spaces after keys, em-dashes and inner quotes
    # in descriptions, and `type` NESTED under `metadata:`. The first `type:` key at ANY
    # indentation wins. Returns $null when the file has no leading --- block at all.
    param([Parameter(Mandatory)][string]$Path)
    $text = $null
    try { $text = Read-AmText -Path $Path } catch { return $null }
    if (-not $text.StartsWith('---')) { return $null }
    $m = [regex]::Match($text, '^---\r?\n(?<fm>.*?)\r?\n---(?:\r?\n|$)', 'Singleline')
    if (-not $m.Success) { return $null }
    $fm = $m.Groups['fm'].Value
    $body = $text.Substring($m.Length)
    $get = {
        param($key)
        $mm = [regex]::Match($fm, '(?m)^\s*' + $key + ':\s*(?<v>.*)$')
        if (-not $mm.Success) { return $null }
        $v = $mm.Groups['v'].Value.Trim()
        if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[-1] -eq '"') -or ($v[0] -eq "'" -and $v[-1] -eq "'"))) { $v = $v.Substring(1, $v.Length - 2) }
        return $v
    }
    return [pscustomobject]@{
        Name        = (& $get 'name')
        Description = (& $get 'description')
        Type        = (& $get 'type')
        Modified    = (& $get 'modified')
        Body        = $body
        BodyBytes   = (Get-AmByteCount -Text $body)
        FileBytes   = (Get-AmByteCount -Text $text)
    }
}

# Port of mem0-server/imperative_canary.py::is_imperative_canonical (kept in lock-step by
# a Pester fixture that mirrors its documented examples). A sentence-level test: standing
# orders open with MUST/NEVER/ALWAYS/SHALL/DO NOT/DON'T/RULE:, or say "you must" anywhere.
$script:AmImperativeRegex = [regex]"(?ix)(?:^\s*(?:MUST|NEVER|ALWAYS|SHALL)\b|^\s*(?:DO\s+NOT|DON'T)\b|^\s*RULE\s*:|\byou\s+must\b)"

function Test-AmImperative {
    param([AllowEmptyString()][AllowNull()][string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $false }
    foreach ($s in ($Text -split '[.!?\n]+')) {
        if ($s.Trim() -and $script:AmImperativeRegex.IsMatch($s)) { return $true }
    }
    return $false
}

function Test-AmDoctrine {
    # The hard rule every job honours before any LLM sees a line.
    param([Parameter(Mandatory)]$Record, $Frontmatter)
    if ($Frontmatter -and $Frontmatter.Type -and ($Frontmatter.Type.ToLowerInvariant() -eq 'feedback')) { return $true }
    if (Test-AmImperative -Text $Record.Summary) { return $true }
    if ($Frontmatter -and (Test-AmImperative -Text $Frontmatter.Description)) { return $true }
    return $false
}

function Get-AmAnchorTokens {
    # Tokens a shortened line must keep at least one of: numbers, paths/ports/URLs, backticked
    # identifiers, ALL-CAPS words (>= 2 letters). A rewrite that keeps none has lost the detail
    # that told the reader WHEN to open the file.
    param([AllowEmptyString()][string]$Text)
    $set = @{}
    if (-not $Text) { return $set }
    foreach ($m in [regex]::Matches($Text, '`[^`]+`|\b\d[\d.,:/-]*\b|[A-Za-z]:\\[^\s,;)]+|/[A-Za-z0-9_./-]{3,}|https?://\S+|\b[A-Z][A-Z0-9_-]{1,}\b')) {
        $set[$m.Value.Trim('`')] = $true
    }
    return $set
}

# ---------------------------------------------------------------- store stats + lint

function Get-AmStoreStats {
    param([Parameter(Mandatory)]$Store)
    $text = Read-AmText -Path $Store.IndexPath
    $idx = Read-AmIndex -Text $text
    $entries = @($idx.Records | Where-Object { $_.Kind -eq 'entry' })
    $lineCount = $idx.Lines
    if ($idx.Lines -gt 0 -and $idx.Records[$idx.Lines - 1].Raw -eq '') { $lineCount = $idx.Lines - 1 }
    return [pscustomobject]@{
        Bytes   = (Get-AmByteCount -Text $text)
        Lines   = $lineCount
        Entries = $entries.Count
        Files   = (Get-AmFactFiles -Dir $Store.Dir).Count
        OverTrigger = (((Get-AmByteCount -Text $text) -ge $script:AmTriggerBytes) -or ($lineCount -ge $script:AmTriggerLines))
    }
}

function Get-AmLintFindings {
    # Stateless: recomputed from disk every time (a store has <= ~200 files). Finding kinds:
    #   orphan, dangling, dup-slug, long-line, oversized-file, no-frontmatter, near-budget,
    #   over-sync-limit, over-inject-limit.
    param([Parameter(Mandatory)]$Store)
    $findings = New-Object System.Collections.ArrayList
    $add = { param($kind, $file, $detail) [void]$findings.Add([pscustomobject]@{ store = $Store.Workspace; kind = $kind; file = $file; detail = $detail }) }
    $text = Read-AmText -Path $Store.IndexPath
    $idx = Read-AmIndex -Text $text
    $entries = @($idx.Records | Where-Object { $_.Kind -eq 'entry' })
    $files = @(Get-AmFactFiles -Dir $Store.Dir)
    $onDisk = @{}
    foreach ($f in $files) { $onDisk[$f.Name] = $f }
    $linked = Get-AmIndexLinkedSlugs -Records $idx.Records
    $seen = @{}
    foreach ($e in $entries) {
        if ($seen.ContainsKey($e.Slug)) { & $add 'dup-slug' $e.Slug ('linked again at line ' + ($e.Index + 1)) } else { $seen[$e.Slug] = $true }
        if (-not $onDisk.ContainsKey($e.Slug)) { & $add 'dangling' $e.Slug ('index line ' + ($e.Index + 1) + ' points at a missing file') }
        if ($e.Bytes -gt $script:AmLineByteCap) { & $add 'long-line' $e.Slug ($e.Bytes.ToString() + ' B (cap ' + $script:AmLineByteCap + ')') }
    }
    foreach ($f in $files) {
        if (-not $linked.ContainsKey($f.Name)) { & $add 'orphan' $f.Name 'on disk but not indexed (never loaded)' }
        if ($f.Length -ge $script:AmOversizedFactBytes) { & $add 'oversized-file' $f.Name ($f.Length.ToString() + ' B (flag only; bodies are never edited)') }
        if (-not (Read-AmFrontmatter -Path $f.FullName)) { & $add 'no-frontmatter' $f.Name 'no leading --- block' }
    }
    $bytes = Get-AmByteCount -Text $text
    $lineCount = $idx.Lines
    if ($idx.Lines -gt 0 -and $idx.Records[$idx.Lines - 1].Raw -eq '') { $lineCount-- }
    if ($bytes -ge $script:AmSyncLimitBytes) { & $add 'over-sync-limit' 'MEMORY.md' ($bytes.ToString() + ' B >= ' + $script:AmSyncLimitBytes + ' (harness refuses to sync)') }
    elseif ($bytes -ge $script:AmTriggerBytes) { & $add 'near-budget' 'MEMORY.md' ($bytes.ToString() + ' B >= trigger ' + $script:AmTriggerBytes) }
    if ($lineCount -ge $script:AmInjectLimitLines) { & $add 'over-inject-limit' 'MEMORY.md' ($lineCount.ToString() + ' lines >= ' + $script:AmInjectLimitLines + ' (tail not injected)') }
    elseif ($lineCount -ge $script:AmTriggerLines) { & $add 'near-budget' 'MEMORY.md' ($lineCount.ToString() + ' lines >= trigger ' + $script:AmTriggerLines) }
    return @($findings)
}

# ---------------------------------------------------------------- session liveness

function Test-AmWorkspaceLive {
    # A session in this workspace wrote its transcript recently. The harness holds an in-context
    # copy of the index and writes it back whole, so mutating under a live session loses one
    # side's changes. Transcripts are the top-level *.jsonl files of the workspace dir.
    #
    # FAILS CLOSED: an enumeration error reports LIVE. "I could not tell" must mean "do not
    # touch it", never "go ahead". Pass -Dirs (a store's ProbeDirs) so alias paths are covered.
    param(
        [string]$Workspace,
        [string[]]$Dirs,
        [int]$WithinMinutes = 30,
        [string]$ProjectsRoot = (Get-AmProjectsRoot)
    )
    $probe = @()
    if ($Dirs -and @($Dirs).Count -gt 0) { $probe = @($Dirs) }
    elseif ($Workspace) { $probe = @((Join-Path $ProjectsRoot $Workspace)) }
    else { return $true }
    $cutoff = [DateTime]::UtcNow.AddMinutes(-1 * $WithinMinutes)
    foreach ($d in $probe) {
        if (-not (Test-Path -LiteralPath $d)) { continue }
        try {
            $recent = @(Get-ChildItem -LiteralPath $d -Filter '*.jsonl' -File -ErrorAction Stop |
                Where-Object { $_.LastWriteTimeUtc -ge $cutoff })
            if ($recent.Count -gt 0) { return $true }
        } catch { return $true }
    }
    return $false
}

function Test-AmLineRoundTrips {
    # A line the job CONSTRUCTS must parse back to exactly the entry it intended: same slug, no
    # extra links, and recognised as an entry at all.
    #
    # Without this, two constructed lines can poison the store permanently. A model-written hook
    # containing something like "(see other.md)" injects a second link to a file that may not
    # exist - a ghost the hygiene pass never removes, so the post-write invariant fails every
    # night, the index is restored every night, and the run is discarded forever. A title
    # containing "]" makes the regenerated line unparseable, so its file reads back as an orphan
    # on the next run - same endless loop.
    param(
        [Parameter(Mandatory)][string]$Line,
        [Parameter(Mandatory)][string]$ExpectedSlug,
        [string[]]$ExpectedExtras = @()
    )
    $rt = Read-AmIndex -Text $Line
    $recs = @($rt.Records | Where-Object { $_.Kind -eq 'entry' })
    if (@($recs).Count -ne 1) { return $false }
    if ($recs[0].Slug -ne $ExpectedSlug) { return $false }
    # Exactly the expected extra links (none by default): a repair that drops a DEAD extra link
    # must keep a LIVE one, or the repair is rejected and the dead link stays forever.
    $got = @($recs[0].ExtraSlugs | Sort-Object)
    $want = @($ExpectedExtras | Sort-Object)
    if (@($got).Count -ne @($want).Count) { return $false }
    for ($i = 0; $i -lt @($got).Count; $i++) { if ($got[$i] -ne $want[$i]) { return $false } }
    return $true
}

function Get-AmEntryGhosts {
    # Links that ENTRY lines carry (primary slug or extra links) to files that do not exist.
    # Only entries count: the compactor never rewrites a non-entry line, so a "(see notes.md)"
    # in a heading or a prose note must not be a ghost it is expected to repair - that would
    # fail the post-write invariant every night for a condition hygiene cannot touch. Non-entry
    # links still count for REACHABILITY (Get-AmIndexLinkedSlugs), just never as ghosts.
    param([Parameter(Mandatory)]$Records, [Parameter(Mandatory)][hashtable]$OnDisk)
    $ghosts = @{}
    foreach ($r in $Records) {
        if ($r.Kind -ne 'entry') { continue }
        # An ambiguous shape ("- [x] task, see [notes](missing.md)" - a checkbox, not a pointer)
        # is never removed by hygiene, so it must not be a ghost either, or a single checkbox
        # line would abort the store's compaction forever.
        if ($r.Title -match '\]') { continue }
        if (-not $OnDisk.ContainsKey($r.Slug)) { $ghosts[$r.Slug] = $true }
        foreach ($e in $r.ExtraSlugs) { if (-not $OnDisk.ContainsKey($e)) { $ghosts[$e] = $true } }
    }
    return @($ghosts.Keys | Sort-Object)
}

function Get-AmTruncatedToBytes {
    # Truncate to a BYTE budget (not a character count) on a word boundary where possible.
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text, [int]$MaxBytes = 100)
    if ((Get-AmByteCount -Text $Text) -le $MaxBytes) { return $Text }
    $s = $Text
    while ($s.Length -gt 0 -and (Get-AmByteCount -Text $s) -gt $MaxBytes) {
        $s = $s.Substring(0, $s.Length - 1)
        # Never cut between the halves of a surrogate pair: a lone high surrogate encodes as
        # EF BF BD (the replacement character) and the index gains a garbage byte sequence.
        if ($s.Length -gt 0 -and [char]::IsHighSurrogate($s[$s.Length - 1])) { $s = $s.Substring(0, $s.Length - 1) }
    }
    $cut = $s.LastIndexOf(' ')
    if ($cut -gt [int]($s.Length * 0.6)) { $s = $s.Substring(0, $cut) }
    return $s.TrimEnd()
}

function Invoke-AmConvergenceFloor {
    # 2026-09-03: the deterministic BACKSTOP shared by the nightly compactor and the write-time
    # gate. The judge-driven shortening ("KEEP is the safe default") converged ~13 lines a night
    # while live sessions added more, and the compactor still stamped 'applied' at 25,219 B - over
    # the 25,000 B sync limit, where the harness silently loads only part of the index. Above the
    # sync limit, convergence beats hook fidelity: every byte past the limit is a whole entry the
    # next session never sees. Longest non-doctrine lines are truncated on a word boundary to the
    # line cap until the index is below $StopBelowBytes (the compactor trigger, so the store leaves
    # the nightly candidate set and the judge keeps first crack at everything else). Doctrine lines
    # are NEVER touched (the hard rule) - a doctrine-only overflow is reported, not "fixed".
    # Mutates the records in place (Dirty=$true) exactly like a judge SHORTEN; returns the count and
    # the projected byte size so the caller decides converged vs unconverged.
    param(
        [Parameter(Mandatory)]$Records,
        [Parameter(Mandatory)][string]$StoreDir,
        [Parameter(Mandatory)][string]$Newline,
        [hashtable]$Meta = $null,
        [int]$StopBelowBytes = $script:AmTriggerBytes
    )
    $projected = Get-AmByteCount -Text (ConvertTo-AmIndexText -Records @($Records) -Newline $Newline)
    $floored = 0
    if ($projected -lt $script:AmSyncLimitBytes) { return [pscustomobject]@{ Floored = 0; Bytes = $projected } }
    $long = @($Records | Where-Object { $_.Kind -eq 'entry' -and $_.Bytes -gt $script:AmLineByteCap } | Sort-Object -Property Bytes -Descending)
    foreach ($rec in $long) {
        if ($projected -lt $StopBelowBytes) { break }
        $doctrine = $false
        if ($Meta -and $Meta.ContainsKey($rec.Slug)) {
            $doctrine = [bool]$Meta[$rec.Slug].Doctrine
        } else {
            $fp = Join-Path $StoreDir $rec.Slug
            $fm = $null
            if (Test-Path -LiteralPath $fp) { $fm = Read-AmFrontmatter -Path $fp }
            $doctrine = Test-AmDoctrine -Record $rec -Frontmatter $fm
        }
        if ($doctrine) { continue }
        $ind = ''
        if ($rec.PSObject.Properties['Indent'] -and $rec.Indent) { $ind = $rec.Indent }
        # overhead of "- [Title](slug) - " measured with a 1-byte placeholder hook
        $overhead = (Get-AmByteCount -Text (New-AmEntryLine -Title $rec.Title -Slug $rec.Slug -Summary 'x' -Indent $ind)) - 1
        $budget = $script:AmLineByteCap - $overhead
        if ($budget -lt 24) { continue }   # a title that alone eats the cap cannot be floored
        $hook = Get-AmTruncatedToBytes -Text $rec.Summary -MaxBytes $budget
        if (-not $hook) { continue }
        $cand = New-AmEntryLine -Title $rec.Title -Slug $rec.Slug -Summary $hook -Indent $ind
        $nb = Get-AmByteCount -Text $cand
        if ($nb -ge $rec.Bytes) { continue }
        if (-not (Test-AmLineRoundTrips -Line $cand -ExpectedSlug $rec.Slug)) { continue }
        $projected -= ($rec.Bytes - $nb)
        $rec.Summary = $hook
        $rec.Bytes = $nb
        $rec | Add-Member -NotePropertyName Dirty -NotePropertyValue $true -Force
        $floored++
    }
    return [pscustomobject]@{ Floored = $floored; Bytes = $projected }
}

# ---------------------------------------------------------------- git history (out of tree)

function Get-AmHistoryGitDir {
    return (Join-Path (Get-AmStateRoot) 'history.git')
}

function Invoke-AmGit {
    # git with the metadata OUTSIDE the tree: no .git inside any store, nothing for the harness
    # to sync or an agent to glob. Returns @{ Code; Out }.
    param([Parameter(Mandatory)][string[]]$GitArgs, [string]$GitDir = (Get-AmHistoryGitDir), [string]$WorkTree = (Get-AmProjectsRoot))
    # Each element parenthesised: the comma binds tighter than +, so an unparenthesised
    # 'a' + $x, 'b' + $y concatenates the ARRAY and fuses both flags into one argument.
    $all = @(('--git-dir=' + $GitDir), ('--work-tree=' + $WorkTree)) + $GitArgs
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'git'
    if ($psi.PSObject.Properties['ArgumentList']) {
        foreach ($a in $all) { [void]$psi.ArgumentList.Add($a) }
    } else {
        # .NET Framework (PS 5.1) has no ArgumentList: quote each arg ourselves
        # (CommandLineToArgv rules: escape embedded quotes; no arg ends in a backslash).
        $psi.Arguments = (($all | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }) -join ' ')
    }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = (Get-AmUtf8)
    $psi.StandardErrorEncoding = (Get-AmUtf8)
    $p = [System.Diagnostics.Process]::Start($psi)
    # Read stdout asynchronously while draining stderr: two sequential ReadToEnd calls deadlock
    # if the child fills the pipe we are not reading yet.
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $err = $p.StandardError.ReadToEnd()
    $out = $outTask.GetAwaiter().GetResult()
    $p.WaitForExit()
    return [pscustomobject]@{ Code = $p.ExitCode; Out = $out; Err = $err }
}

function Initialize-AmHistory {
    # Idempotent. Creates the out-of-tree repo, pins an identity (commits need one; the
    # operator's global config may demand signing), forces byte-exact handling, and excludes
    # everything by default - stores are added with an explicit forced pathspec.
    param([string]$GitDir = (Get-AmHistoryGitDir), [string]$WorkTree = (Get-AmProjectsRoot))
    if (-not (Test-Path -LiteralPath (Join-Path $GitDir 'HEAD'))) {
        $parent = Split-Path -Parent $GitDir
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { [System.IO.Directory]::CreateDirectory($parent) | Out-Null }
        $r = Invoke-AmGit -GitArgs @('init', '-q') -GitDir $GitDir -WorkTree $WorkTree
        if ($r.Code -ne 0) { throw ('git init failed: ' + $r.Err) }
    }
    foreach ($kv in @(@('user.name', 'automemory'), @('user.email', 'automemory@localhost'), @('commit.gpgsign', 'false'), @('core.autocrlf', 'false'), @('core.safecrlf', 'false'), @('core.quotepath', 'false'))) {
        [void](Invoke-AmGit -GitArgs @('config', $kv[0], $kv[1]) -GitDir $GitDir -WorkTree $WorkTree)
    }
    $info = Join-Path $GitDir 'info'
    if (-not (Test-Path -LiteralPath $info)) { [System.IO.Directory]::CreateDirectory($info) | Out-Null }
    [System.IO.File]::WriteAllText((Join-Path $info 'exclude'), "*`n", (Get-AmUtf8))
    return $GitDir
}

function Test-AmHistoryHasRemote {
    param([string]$GitDir = (Get-AmHistoryGitDir))
    if (-not (Test-Path -LiteralPath (Join-Path $GitDir 'HEAD'))) { return $false }
    $r = Invoke-AmGit -GitArgs @('remote') -GitDir $GitDir
    return (-not [string]::IsNullOrWhiteSpace($r.Out))
}

function Get-AmStoreRelPath {
    param([Parameter(Mandatory)]$Store, [string]$WorkTree = (Get-AmProjectsRoot))
    return ($Store.Workspace + '/memory')
}

function Save-AmHistorySnapshot {
    # Stages the store (adds, modifications AND deletions) and commits. Returns the commit sha,
    # or $null when nothing changed (git's diff is the no-op guard).
    param([Parameter(Mandatory)]$Store, [Parameter(Mandatory)][string]$Message, [string]$GitDir = (Get-AmHistoryGitDir), [string]$WorkTree = (Get-AmProjectsRoot))
    $rel = Get-AmStoreRelPath -Store $Store
    $r = Invoke-AmGit -GitArgs @('add', '-A', '-f', '--', $rel) -GitDir $GitDir -WorkTree $WorkTree
    if ($r.Code -ne 0) { throw ('git add failed: ' + $r.Err) }
    $q = Invoke-AmGit -GitArgs @('diff', '--cached', '--quiet', '--', $rel) -GitDir $GitDir -WorkTree $WorkTree
    if ($q.Code -eq 0) { return $null }
    $c = Invoke-AmGit -GitArgs @('commit', '-q', '-m', $Message, '--', $rel) -GitDir $GitDir -WorkTree $WorkTree
    if ($c.Code -ne 0) { throw ('git commit failed: ' + $c.Err) }
    $h = Invoke-AmGit -GitArgs @('rev-parse', 'HEAD') -GitDir $GitDir -WorkTree $WorkTree
    return $h.Out.Trim()
}

function Test-AmHistoryHasFile {
    param([Parameter(Mandatory)][string]$Sha, [Parameter(Mandatory)][string]$RelPath, [string]$GitDir = (Get-AmHistoryGitDir))
    $r = Invoke-AmGit -GitArgs @('cat-file', '-e', ($Sha + ':' + $RelPath)) -GitDir $GitDir
    return ($r.Code -eq 0)
}

function Restore-AmHistoryFile {
    # Per-FILE restore from a snapshot. Never a directory: a directory restore would revert
    # files a live session touched in the meantime.
    param([Parameter(Mandatory)][string]$Sha, [Parameter(Mandatory)][string]$RelPath, [string]$GitDir = (Get-AmHistoryGitDir), [string]$WorkTree = (Get-AmProjectsRoot))
    $r = Invoke-AmGit -GitArgs @('checkout', $Sha, '--', $RelPath) -GitDir $GitDir -WorkTree $WorkTree
    return ($r.Code -eq 0)
}

function Get-AmHistoryDiff {
    param([Parameter(Mandatory)][string]$FromSha, [Parameter(Mandatory)][string]$ToSha, [Parameter(Mandatory)][string]$RelPath, [string]$GitDir = (Get-AmHistoryGitDir), [string]$WorkTree = (Get-AmProjectsRoot))
    $r = Invoke-AmGit -GitArgs @('diff', '--no-color', $FromSha, $ToSha, '--', $RelPath) -GitDir $GitDir -WorkTree $WorkTree
    return $r.Out
}

# ---------------------------------------------------------------- small JSON helpers

function Write-AmJsonFile {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Object)
    $json = $Object | ConvertTo-Json -Depth 8
    Write-AmTextAtomic -Path $Path -Text $json
}

function Read-AmJsonFile {
    # Returns the parsed object, or $null when the file does not exist.
    # THROWS when the file exists but does not parse - "absent" and "corrupt" must not collapse
    # into the same answer. A corrupt seal file silently read as "no seals" would re-arm the
    # model on every already-shortened line, which is exactly the drift the seal prevents.
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $raw = Read-AmText -Path $Path
    # An EMPTY file is present-but-unparseable, not absent: '' | ConvertFrom-Json yields $null
    # on pwsh 7, which read a truncated seal file as "no seals" - the same disarm as corruption.
    if ([string]::IsNullOrWhiteSpace($raw)) { throw ('state file is empty (truncated write?): ' + $Path) }
    return ($raw | ConvertFrom-Json)
}
