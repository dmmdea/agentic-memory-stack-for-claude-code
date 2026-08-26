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
    # The store's existing line ending; new files default to LF (what the harness writes).
    param([AllowEmptyString()][string]$Text)
    if ($Text -and $Text.Contains("`r`n")) { return "`r`n" }
    return "`n"
}

function Write-AmTextAtomic {
    # Write UTF-8 (no BOM) to a temp file beside the target, then swap it in. A crash mid-write
    # leaves the original untouched; a reader never sees a torn file.
    param([Parameter(Mandatory)][string]$Path, [AllowEmptyString()][string]$Text)
    $tmp = $Path + '.am-tmp'
    [System.IO.File]::WriteAllText($tmp, $Text, (Get-AmUtf8))
    if (Test-Path -LiteralPath $Path) {
        # $null binds as "" on a .NET string parameter; NullString is the real null.
        [System.IO.File]::Replace($tmp, $Path, [NullString]::Value)
    } else {
        [System.IO.File]::Move($tmp, $Path)
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
    # reachable under a spaces-vs-dashes name). Returns the canonical directory path.
    param([Parameter(Mandatory)][string]$Dir)
    try {
        $item = Get-Item -LiteralPath $Dir -ErrorAction Stop
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            $t = $null
            try { $t = $item.Target } catch { $t = $null }
            if ($t) { return ([string]@($t)[0]).TrimEnd('\') }
        }
    } catch {}
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
    foreach ($d in $dirs) {
        $memDir = Join-Path $d.FullName 'memory'
        $idx = Join-Path $memDir 'MEMORY.md'
        if (-not (Test-Path -LiteralPath $idx)) { continue }
        $canon = (Join-Path (Resolve-AmReparseTarget -Dir $d.FullName) 'memory').ToLowerInvariant()
        $isAlias = $seen.ContainsKey($canon)
        if (-not $isAlias) { $seen[$canon] = $d.Name }
        $out += [pscustomobject]@{
            Workspace    = $d.Name
            Dir          = $memDir
            IndexPath    = $idx
            CanonicalDir = $canon
            IsAlias      = $isAlias
            AliasOf      = $(if ($isAlias) { $seen[$canon] } else { $null })
        }
    }
    # Plain return: callers wrap in @(). A `return ,$out` here hands back ONE element that IS
    # the array (observed: every store concatenated into a single object under 5.1 and 7).
    return $out
}

function Get-AmFactFiles {
    # Fact files = every *.md in the store except the index. Never recurses: subdirectories
    # are not part of the harness contract and must never be created by a maintainer.
    param([Parameter(Mandatory)][string]$Dir)
    return @(Get-ChildItem -LiteralPath $Dir -Filter '*.md' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'MEMORY.md' } | Sort-Object Name)
}

# ---------------------------------------------------------------- index parsing

$script:AmEntryRegex = [regex]'^- \[(?<title>[^\]]*)\]\((?<slug>[^)\s]+\.md)\)(?<rest>.*)$'
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
    foreach ($ln in $lines) {
        $m = $script:AmEntryRegex.Match($ln)
        if ($m.Success) {
            $rest = $m.Groups['rest'].Value
            # summary = rest with the leading separator (em-dash / hyphen / colon) stripped
            $summary = ($rest -replace ('^\s*(?:' + $script:AmEmDash + '|--?|:)\s*'), '').TrimEnd()
            $extra = @()
            foreach ($lm in $script:AmLinkRegex.Matches($rest)) { $extra += $lm.Groups[1].Value }
            [void]$records.Add([pscustomobject]@{
                Index   = $i
                Kind    = 'entry'
                Raw     = $ln
                Title   = $m.Groups['title'].Value
                Slug    = $m.Groups['slug'].Value
                Summary = $summary
                ExtraSlugs = $extra
                Bytes   = Get-AmByteCount -Text $ln
            })
        } else {
            [void]$records.Add([pscustomobject]@{
                Index = $i; Kind = 'other'; Raw = $ln; Title = $null; Slug = $null
                Summary = $null; ExtraSlugs = @(); Bytes = (Get-AmByteCount -Text $ln)
            })
        }
        $i++
    }
    return [pscustomobject]@{ Newline = $nl; Records = $records; Lines = $lines.Count }
}

function New-AmEntryLine {
    param([Parameter(Mandatory)][string]$Title, [Parameter(Mandatory)][string]$Slug, [AllowEmptyString()][string]$Summary)
    $line = '- [' + $Title + '](' + $Slug + ')'
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
            $out.Add((New-AmEntryLine -Title $r.Title -Slug $r.Slug -Summary $r.Summary))
        } else {
            $out.Add($r.Raw)
        }
    }
    return ($out -join $Newline)
}

function Get-AmIndexLinkedSlugs {
    # Every fact file the index references, primary or extra (a merged line may carry two).
    param([Parameter(Mandatory)]$Records)
    $set = @{}
    foreach ($r in $Records) {
        if ($r.Kind -ne 'entry') { continue }
        $set[$r.Slug] = $true
        foreach ($e in $r.ExtraSlugs) { $set[$e] = $true }
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
    # A session in THIS workspace wrote its transcript recently. The harness holds an in-context
    # copy of the index and writes it back whole, so mutating under a live session loses one
    # side's changes. Transcripts are the top-level *.jsonl files of the workspace dir.
    param([Parameter(Mandatory)][string]$Workspace, [int]$WithinMinutes = 30, [string]$ProjectsRoot = (Get-AmProjectsRoot))
    $wsDir = Join-Path $ProjectsRoot $Workspace
    if (-not (Test-Path -LiteralPath $wsDir)) { return $false }
    $cutoff = [DateTime]::UtcNow.AddMinutes(-1 * $WithinMinutes)
    $recent = @(Get-ChildItem -LiteralPath $wsDir -Filter '*.jsonl' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTimeUtc -ge $cutoff })
    return ($recent.Count -gt 0)
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
    $out = $p.StandardOutput.ReadToEnd()
    $err = $p.StandardError.ReadToEnd()
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
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return (Read-AmText -Path $Path | ConvertFrom-Json) } catch { return $null }
}
