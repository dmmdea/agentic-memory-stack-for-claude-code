# memory-compact.ps1 - the autonomous compactor for the harness-native per-workspace
# auto-memory stores ("System A"). Registered as the Windows Task Scheduler entry
# ClaudeCode-MemoryCompactor (daily ~5am, hidden via run-hidden.vbs, all roles).
#
# WHAT IT OWNS: the INDEX (MEMORY.md) only. It shortens over-long index lines, drops or merges
# lines whose fact is dead, re-indexes orphans, removes dangling lines, and migrates a bounded
# number of durable-but-pullable facts to mem0. It NEVER edits a fact file's body, never
# touches a doctrine entry, and never deletes anything - removals are recorded as git history
# in an out-of-tree repo, from which any file can be restored individually.
#
# THE FIVE GUARDS (each one exists because a specific failure was identified before build):
#   1. LIVENESS GATE - a session in that workspace writing memories concurrently would have its
#      appended lines silently dropped by a rewrite built from a stale read. Observed live
#      (an index grew 3 entries mid-build), so this is not theoretical. Skip if recent activity.
#   2. CAS - the index hash and the fact-file set are re-read immediately before the swap. Any
#      drift aborts the whole store with no write at all. This is the abort-not-rollback rule:
#      a directory-level rollback would clobber whatever the live session just wrote.
#   3. HARD DOCTRINE RULE - `metadata.type: feedback` (nested; the top-level form matches zero
#      real files) or imperative phrasing means the line is never shortened by an LLM, merged,
#      migrated or dropped. Deterministic, applied BEFORE Codex sees anything.
#   4. STRICT DECREASE + SEAL + BLAST CAP - an action applies only if it strictly shrinks the
#      index; a line the job has already shortened once is never re-sent to the model (that is
#      what stops nightly semantic drift); and one run may never remove more than a fifth of
#      the lines.
#   5. WRITE-THEN-VERIFY MIGRATION - a fact reaches mem0 verbatim and is read back BY ID and
#      compared byte-for-byte before its line leaves the index. The helper returns $true (not
#      an id) when mem0 omits one; that counts as unverifiable, so the line stays.
#
# NO LOCAL FALLBACK: if Codex is unreachable the job logs and skips WITHOUT marking the
# throttle. Deterministic hygiene still applies - it needs no model.

param(
    [switch]$DryRun,
    [switch]$Force,
    [string]$Workspace,          # limit to one workspace (rehearsal / manual run)
    [int]$MaxMigrationsPerRun = 5,
    [int]$CodexTimeoutSeconds = 240
)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

. (Join-Path $ScriptDir 'memory-common.ps1')
. (Join-Path $ScriptDir 'memory-store-lib.ps1')
Initialize-MemoryEnv

$Component = 'memory-compact'
$ThrottleName = 'memory-compact'
$ReceiptPath = Join-Path (Get-AmStateRoot) 'compact-receipts.jsonl'

function Get-AmLineCount {
    # Count lines the way Get-AmStoreStats does: a trailing newline terminates the last line,
    # it does not start a new empty one. Counting them differently in two places made a dry run
    # report 118 lines for an index that has 117 - a receipt that misreports is worse than none.
    param([AllowEmptyString()][string]$Text)
    $n = @([regex]::Split($Text, "\r?\n")).Count
    if ($Text.EndsWith("`n")) { $n-- }
    return $n
}

function Write-AmReceipt {
    param([Parameter(Mandatory)]$Record)
    try {
        $json = ($Record | ConvertTo-Json -Depth 8 -Compress)
        [System.IO.File]::AppendAllText($ReceiptPath, $json + "`n", (Get-AmUtf8))
    } catch {}
}

# 23h like the dream: the stamp is written at completion, so a strict 24h window would make a
# fixed 05:00 trigger ineligible by a few seconds every other night.
if (-not $DryRun -and -not $Force -and -not (Test-Throttle -Name $ThrottleName -MinIntervalSeconds 82800)) {
    Write-MemoryLog -Component $Component -Message 'skipping: nightly throttle (23h) not yet elapsed'
    exit 0
}

$stores = @(Get-AmStores) | Where-Object { -not $_.IsAlias }
if ($Workspace) { $stores = @($stores | Where-Object { $_.Workspace -eq $Workspace }) }
if (@($stores).Count -eq 0) { Write-MemoryLog -Component $Component -Message 'no stores found'; exit 0 }

# Only stores at/above a trigger are candidates; hysteresis (target < trigger) keeps a store
# that was just compacted from being re-processed every night.
$candidates = @()
foreach ($s in $stores) {
    try {
        $st = Get-AmStoreStats -Store $s
        if ($st.OverTrigger -or $Force) { $candidates += [pscustomobject]@{ Store = $s; Stats = $st } }
    } catch { Write-MemoryLog -Component $Component -Message ('stats failed for ' + $s.Workspace + ': ' + $_.Exception.Message) }
}
if (@($candidates).Count -eq 0) {
    Write-MemoryLog -Component $Component -Message ('nothing above trigger (' + $script:AmTriggerBytes + ' B / ' + $script:AmTriggerLines + ' lines) across ' + @($stores).Count + ' store(s)')
    if (-not $DryRun) { Mark-Throttle -Name $ThrottleName }
    exit 0
}

try { Initialize-AmHistory | Out-Null } catch {
    Write-MemoryLog -Component $Component -Message ('history init failed, aborting (no snapshot = no safe apply): ' + $_.Exception.Message)
    exit 0
}
if (Test-AmHistoryHasRemote) {
    Write-MemoryLog -Component $Component -Message 'ABORT: the memory history repo has a remote configured; these stores hold private facts and must never be pushed'
    exit 0
}

# One Codex mutex for the whole run, shared with L1a / dream / dedup.
# Taken for a -DryRun TOO: a dry run writes nothing to the stores but still calls the judge,
# and the mutex exists to serialise judge calls, not writes. Skipping it here (the shape the
# other nightly jobs use) would let a rehearsal collide with a live extractor.
$lockTaken = $false
if (-not (Acquire-CodexLock -Owner 'memory-compact' -MaxAgeMinutes 30)) {
    Write-MemoryLog -Component $Component -Message 'skipping: codex lock held by another worker'
    exit 0
}
$lockTaken = $true

function Get-AmSealPath { param($Store) return (Join-Path (Get-AmWorkspaceStateDir -Workspace $Store.Workspace) 'sealed-lines.json') }

function Get-AmSealed {
    param($Store)
    $o = Read-AmJsonFile -Path (Get-AmSealPath -Store $Store)
    $h = @{}
    if ($o) { foreach ($p in $o.PSObject.Properties) { $h[$p.Name] = $p.Value } }
    return $h
}

function Save-AmSealed {
    param($Store, [hashtable]$Sealed)
    $o = New-Object psobject
    foreach ($k in $Sealed.Keys) { $o | Add-Member -NotePropertyName $k -NotePropertyValue $Sealed[$k] -Force }
    Write-AmJsonFile -Path (Get-AmSealPath -Store $Store) -Object $o
}

function Get-AmMem0Record {
    # Read a record back BY ID. Byte-equality against what we sent is the only falsifiable
    # proof the migration landed; a top-ranked semantic search for the fact's own text merely
    # proves something similar exists (it may be a pre-existing near-duplicate).
    param([Parameter(Mandatory)][string]$Id)
    try {
        $key = Get-Mem0Key
        $r = Invoke-RestMethod -Uri ('http://127.0.0.1:18791/v1/memories/' + $Id) -Headers @{ 'X-API-Key' = $key } -TimeoutSec 15
        return $r
    } catch { return $null }
}

function Test-AmMigrationLanded {
    param([Parameter(Mandatory)][string]$Id, [Parameter(Mandatory)][string]$Text)
    $rec = Get-AmMem0Record -Id $Id
    if (-not $rec) { return $false }
    $stored = $null
    foreach ($f in @('memory', 'data', 'text')) {
        if ($rec.PSObject.Properties[$f] -and $rec.$f) { $stored = [string]$rec.$f; break }
    }
    if ($null -eq $stored) { return $false }
    if ($stored -ne $Text) { return $false }
    if ($rec.PSObject.Properties['retrievable'] -and ($rec.retrievable -eq $false)) { return $false }
    return $true
}

$runReceipts = @()
try {
foreach ($cand in $candidates) {
    $store = $cand.Store
    $ws = $store.Workspace
    $rel = Get-AmStoreRelPath -Store $store
    $before = $cand.Stats
    $result = [ordered]@{
        ts = (Get-Date).ToUniversalTime().ToString('o'); workspace = $ws; dry_run = [bool]$DryRun
        before_bytes = $before.Bytes; before_lines = $before.Lines; status = 'unknown'
        shortened = 0; dropped = 0; merged = 0; migrated = 0; reindexed = 0; dedangled = 0
        mem0 = @(); after_bytes = $null; after_lines = $null; commit = $null; note = ''
    }

    # ---- GUARD 1: liveness -------------------------------------------------------------
    if (-not $Force -and (Test-AmWorkspaceLive -Workspace $ws -WithinMinutes 30)) {
        $result.status = 'skipped-live-session'
        $result.note = 'a session in this workspace wrote within 30 min; the harness writes the index whole from an in-context copy'
        Write-MemoryLog -Component $Component -Message ($ws + ': skipped (live session)')
        $runReceipts += [pscustomobject]$result
        continue
    }

    # ---- snapshot ----------------------------------------------------------------------
    $snapSha = $null
    try { $snapSha = Save-AmHistorySnapshot -Store $store -Message ('snapshot before compaction: ' + $ws) }
    catch { Write-MemoryLog -Component $Component -Message ($ws + ': snapshot failed: ' + $_.Exception.Message); $result.status = 'error-snapshot'; $runReceipts += [pscustomobject]$result; continue }
    if (-not $snapSha) {
        $h = Invoke-AmGit -GitArgs @('rev-parse', 'HEAD')
        if ($h.Code -eq 0) { $snapSha = $h.Out.Trim() }
    }
    $result.snapshot = $snapSha

    $preText = Read-AmText -Path $store.IndexPath
    $preHash = Get-AmContentHash -Path $store.IndexPath
    $preFiles = @(Get-AmFactFiles -Dir $store.Dir | ForEach-Object { $_.Name })
    $idx = Read-AmIndex -Text $preText
    $records = @($idx.Records)
    $entries = @($records | Where-Object { $_.Kind -eq 'entry' })

    # frontmatter + doctrine classification for every entry (deterministic, pre-Codex)
    $meta = @{}
    foreach ($e in $entries) {
        $fp = Join-Path $store.Dir $e.Slug
        $fm = $null
        if (Test-Path -LiteralPath $fp) { $fm = Read-AmFrontmatter -Path $fp }
        $meta[$e.Slug] = [pscustomobject]@{ Frontmatter = $fm; Doctrine = (Test-AmDoctrine -Record $e -Frontmatter $fm) }
    }

    # ---- deterministic hygiene (no model needed) ---------------------------------------
    $onDisk = @{}
    foreach ($n in $preFiles) { $onDisk[$n] = $true }
    $keep = New-Object System.Collections.ArrayList
    $seenSlug = @{}
    foreach ($r in $records) {
        if ($r.Kind -ne 'entry') { [void]$keep.Add($r); continue }
        if ($seenSlug.ContainsKey($r.Slug)) { $result.dedangled++; continue }   # duplicate link
        if (-not $onDisk.ContainsKey($r.Slug)) { $result.dedangled++; continue } # dangling link
        $seenSlug[$r.Slug] = $true
        [void]$keep.Add($r)
    }
    # orphan re-index: a fact on disk that nothing links to is invisible to every session.
    $linkedNow = Get-AmIndexLinkedSlugs -Records @($keep)
    foreach ($n in $preFiles) {
        if ($linkedNow.ContainsKey($n)) { continue }
        $fm = Read-AmFrontmatter -Path (Join-Path $store.Dir $n)
        $title = if ($fm -and $fm.Name) { $fm.Name } else { [System.IO.Path]::GetFileNameWithoutExtension($n) }
        $desc = if ($fm -and $fm.Description) { $fm.Description } else { 'recovered orphan; no description' }
        if ((Get-AmByteCount -Text $desc) -gt $script:AmLineByteCap) { $desc = $desc.Substring(0, [Math]::Min($desc.Length, 100)) }
        $newRec = [pscustomobject]@{ Index = -1; Kind = 'entry'; Raw = ''; Title = $title; Slug = $n; Summary = $desc; ExtraSlugs = @(); Bytes = 0; Dirty = $true }
        [void]$keep.Add($newRec)
        $meta[$n] = [pscustomobject]@{ Frontmatter = $fm; Doctrine = (Test-AmDoctrine -Record $newRec -Frontmatter $fm) }
        $result.reindexed++
    }

    # The index as deterministic hygiene alone would leave it. This is the baseline the
    # strict-decrease rule is measured against: hygiene is CORRECTNESS (a dangling line points
    # nowhere; an orphan file is invisible) and must apply even when re-indexing an orphan
    # costs a few more bytes than the dangling line it replaced. Only the MODEL's edits are
    # required to shrink - that is what stops nightly drift, and it must never gate a fix.
    $hygieneBytes = Get-AmByteCount -Text (ConvertTo-AmIndexText -Records @($keep) -Newline $idx.Newline)
    $hygieneChanged = ($result.dedangled -gt 0 -or $result.reindexed -gt 0)

    # ---- feasibility: can the protected set alone even fit? ----------------------------
    $protected = @($keep | Where-Object { $_.Kind -eq 'entry' -and $meta[$_.Slug].Doctrine })
    $protectedFloor = 0
    foreach ($p in $protected) { $protectedFloor += [Math]::Min($p.Bytes, $script:AmLineByteCap) + 1 }
    if ($protected.Count -gt $script:AmTargetLines -or $protectedFloor -gt $script:AmTargetBytes) {
        $result.status = 'protected-set-overflow'
        $result.note = ('doctrine lines alone (' + $protected.Count + ' lines, ~' + $protectedFloor + ' B) exceed the target budget; the hard rule is never loosened autonomously - re-home doctrine into a topic file by hand')
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        $runReceipts += [pscustomobject]$result
        continue
    }

    # ---- the delta the model actually judges -------------------------------------------
    $sealed = Get-AmSealed -Store $store
    $shortenable = @($keep | Where-Object {
        $_.Kind -eq 'entry' -and $_.Bytes -gt $script:AmLineByteCap -and -not $meta[$_.Slug].Doctrine -and -not $sealed.ContainsKey($_.Slug)
    })
    $migratable = @($keep | Where-Object {
        $_.Kind -eq 'entry' -and -not $meta[$_.Slug].Doctrine -and $meta[$_.Slug].Frontmatter -and
        (@('project', 'reference') -contains ("" + $meta[$_.Slug].Frontmatter.Type).ToLowerInvariant())
    })

    $plan = $null
    if (@($shortenable).Count -gt 0 -or @($migratable).Count -gt 0) {
        $sb = New-Object System.Text.StringBuilder
        [void]$sb.AppendLine('You are compacting one workspace index of an agent memory system. Only the index is loaded into every session; each entry points at a fact file that is loaded on demand.')
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('For each candidate below return ONE decision:')
        [void]$sb.AppendLine('  SHORTEN - rewrite the hook to <= 130 bytes. It must still say WHEN to open the file: keep the distinguishing detail (numbers, paths, identifiers, the surprising claim). Never a generic label.')
        [void]$sb.AppendLine('  MIGRATE - the fact is a pullable lookup (an endpoint, id, path, version, a finished status) that a session only needs once the topic is already in hand. Migrating removes it from every future session prompt, so if it changes behaviour BEFORE the agent knows to ask, do not migrate.')
        [void]$sb.AppendLine('  KEEP - leave the line exactly as it is. This is the safe default whenever you are unsure.')
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('Return STRICT JSON, no prose, no code fence: {"plan":[{"slug":"<file.md>","action":"SHORTEN|MIGRATE|KEEP","hook":"<new hook if SHORTEN>"}]}')
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('CANDIDATES:')
        foreach ($c in @($shortenable)) {
            $fm = $meta[$c.Slug].Frontmatter
            [void]$sb.AppendLine('- slug: ' + $c.Slug + ' | type: ' + $(if ($fm) { $fm.Type } else { '?' }) + ' | ' + $c.Bytes + ' B')
            [void]$sb.AppendLine('  current: ' + $c.Summary)
            if ($fm -and $fm.Description) { [void]$sb.AppendLine('  description: ' + $fm.Description) }
        }
        foreach ($c in @($migratable | Where-Object { $shortenable -notcontains $_ } | Select-Object -First 20)) {
            $fm = $meta[$c.Slug].Frontmatter
            [void]$sb.AppendLine('- slug: ' + $c.Slug + ' | type: ' + $(if ($fm) { $fm.Type } else { '?' }) + ' | migrate-candidate')
            [void]$sb.AppendLine('  current: ' + $c.Summary)
        }
        $prompt = $sb.ToString()
        $raw = $null
        try { $raw = Invoke-CodexSubagent -Prompt $prompt -ReasoningEffort 'medium' -TimeoutSeconds $CodexTimeoutSeconds }
        catch {
            # NO LOCAL FALLBACK, and the throttle is NOT marked: judgment work waits for the
            # judge. Deterministic hygiene from this pass is still applied below.
            Write-MemoryLog -Component $Component -Message ($ws + ': codex unavailable (' + $_.Exception.Message + '); deterministic hygiene only')
            $result.note = 'codex unavailable; deterministic hygiene only'
        }
        if ($raw) {
            $text = Get-CodexResponseText -RawOutput $raw
            # The helper matches an OBJECT carrying an expected key; a bare top-level array
            # returns $null (observed in the dream log), hence the {"plan":[...]} wrapper.
            $parsed = Extract-JsonFromText -Text $text -ExpectedKey 'plan'
            if ($parsed) { $plan = @($parsed.plan) }
            else { Write-MemoryLog -Component $Component -Message ($ws + ': codex returned unparseable JSON; deterministic hygiene only') }
        }
    }

    # ---- apply ------------------------------------------------------------------------
    $byslug = @{}
    foreach ($r in $keep) { if ($r.Kind -eq 'entry') { $byslug[$r.Slug] = $r } }
    $blastCap = [Math]::Max(1, [int][Math]::Floor(@($entries).Count * 0.2))
    $removals = 0
    $migrationsDone = 0
    $newlySealed = @{}

    foreach ($d in @($plan)) {
        if (-not $d -or -not $d.slug) { continue }
        $slug = [string]$d.slug
        if (-not $byslug.ContainsKey($slug)) { continue }
        $rec = $byslug[$slug]
        if ($meta[$slug].Doctrine) { continue }   # guard 3, re-checked at apply time
        $action = ("" + $d.action).ToUpperInvariant()

        if ($action -eq 'SHORTEN') {
            $hook = ("" + $d.hook).Trim()
            if (-not $hook) { continue }
            $candidateLine = New-AmEntryLine -Title $rec.Title -Slug $rec.Slug -Summary $hook
            $newBytes = Get-AmByteCount -Text $candidateLine
            if ($newBytes -ge $rec.Bytes) { continue }                       # strict decrease
            $anchors = Get-AmAnchorTokens -Text $rec.Summary
            if ($anchors.Count -gt 0) {
                $kept = $false
                foreach ($a in $anchors.Keys) { if ($hook -like ('*' + $a + '*')) { $kept = $true; break } }
                if (-not $kept) { continue }   # a hook that keeps no anchor has lost the trigger
            }
            $rec.Summary = $hook
            $rec.Bytes = $newBytes
            $rec | Add-Member -NotePropertyName Dirty -NotePropertyValue $true -Force
            $newlySealed[$slug] = $true        # one LLM rewrite per line, ever
            $result.shortened++
        }
        elseif ($action -eq 'MIGRATE') {
            if ($migrationsDone -ge $MaxMigrationsPerRun) { continue }
            if ($removals -ge $blastCap) { continue }
            $fm = $meta[$slug].Frontmatter
            if (-not $fm -or -not $fm.Body) { continue }
            # Verbatim, never a paraphrase: a re-run must produce the identical text so a
            # retry deduplicates instead of creating a second variant.
            $textOut = ($fm.Description + "`n`n" + $fm.Body).Trim()
            if ($DryRun) { $migrationsDone++; $result.migrated++; continue }
            $mdata = @{ tier = 'evidence'; origin_slug = $slug; workspace = $ws }
            $id = Add-Mem0Memory -Text $textOut -Source ('automemory:' + $ws + '/' + $slug) -Metadata $mdata
            if ($id -is [string] -and $id) {
                if (Test-AmMigrationLanded -Id $id -Text $textOut) {
                    $keep.Remove($rec) | Out-Null
                    Remove-Item -LiteralPath (Join-Path $store.Dir $slug) -Force -ErrorAction SilentlyContinue
                    $result.mem0 += ('' + $id + ' | ' + $slug + ' | ' + $rec.Raw)
                    $migrationsDone++; $removals++; $result.migrated++
                } else {
                    Write-MemoryLog -Component $Component -Message ($ws + ': migration ' + $slug + ' not verifiable by id; line kept')
                }
            } else {
                # $true (no id returned) or $false (dead-lettered) - both unverifiable.
                Write-MemoryLog -Component $Component -Message ($ws + ': migration ' + $slug + ' returned no id; line kept')
            }
        }
    }

    $newText = ConvertTo-AmIndexText -Records @($keep) -Newline $idx.Newline
    $newBytesTotal = Get-AmByteCount -Text $newText

    if ($newText -eq $preText) {
        $result.status = 'no-op'
        $result.after_bytes = $before.Bytes; $result.after_lines = $before.Lines
        Write-MemoryLog -Component $Component -Message ($ws + ': no change')
        $runReceipts += [pscustomobject]$result
        continue
    }
    if ($newBytesTotal -gt $hygieneBytes -or (-not $hygieneChanged -and $newBytesTotal -ge $before.Bytes)) {
        $result.status = 'rejected-no-shrink'
        $result.note = 'the model-driven edits did not shrink the index past the hygiene baseline; discarded'
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        $runReceipts += [pscustomobject]$result
        continue
    }
    if ($DryRun) {
        $result.status = 'dry-run'
        $result.after_bytes = $newBytesTotal
        $result.after_lines = Get-AmLineCount -Text $newText
        Write-MemoryLog -Component $Component -Message ($ws + ': DRY RUN ' + $before.Bytes + ' -> ' + $newBytesTotal + ' B')
        $runReceipts += [pscustomobject]$result
        continue
    }

    # ---- GUARD 2: compare-and-swap ------------------------------------------------------
    $nowHash = Get-AmContentHash -Path $store.IndexPath
    $nowFiles = @(Get-AmFactFiles -Dir $store.Dir | ForEach-Object { $_.Name })
    $expectedFiles = @($preFiles | Where-Object { $_ -notin @($result.mem0 | ForEach-Object { ($_ -split ' \| ')[1] }) })
    $unexpected = @($nowFiles | Where-Object { $_ -notin $expectedFiles })
    if ($nowHash -ne $preHash -or @($unexpected).Count -gt 0) {
        $result.status = 'aborted-concurrent-write'
        $result.note = 'the store changed under the job (index hash or file set); no write performed - abort, never roll back over a live session'
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        $runReceipts += [pscustomobject]$result
        continue
    }

    Write-AmTextAtomic -Path $store.IndexPath -Text $newText

    # ---- post-apply invariants ---------------------------------------------------------
    $postText = Read-AmText -Path $store.IndexPath
    $postIdx = Read-AmIndex -Text $postText
    $postFiles = @(Get-AmFactFiles -Dir $store.Dir | ForEach-Object { $_.Name })
    $postLinked = Get-AmIndexLinkedSlugs -Records $postIdx.Records
    $ghosts = @($postLinked.Keys | Where-Object { $_ -notin $postFiles })
    $orphans = @($postFiles | Where-Object { -not $postLinked.ContainsKey($_) })
    $lost = @($preFiles | Where-Object { $_ -notin $postFiles -and $_ -notin @($result.mem0 | ForEach-Object { ($_ -split ' \| ')[1] }) })
    $ok = (@($ghosts).Count -eq 0 -and @($orphans).Count -eq 0 -and @($lost).Count -eq 0)
    if (-not $ok) {
        # Restore ONLY the index - the single file this job wrote. Never the directory.
        [void](Restore-AmHistoryFile -Sha $snapSha -RelPath ($rel + '/MEMORY.md'))
        $result.status = 'reverted-invariant-failure'
        $result.note = ('ghosts=' + @($ghosts).Count + ' orphans=' + @($orphans).Count + ' lost=' + @($lost).Count + '; index restored from ' + $snapSha)
        Write-MemoryLog -Component $Component -Message ($ws + ': INVARIANT FAILURE, index restored: ' + $result.note)
        $runReceipts += [pscustomobject]$result
        continue
    }

    foreach ($k in $newlySealed.Keys) { $sealed[$k] = (Get-Date).ToUniversalTime().ToString('o') }
    Save-AmSealed -Store $store -Sealed $sealed

    $postLines = Get-AmLineCount -Text $postText
    $msg = ('compact ' + $ws + ': ' + $before.Bytes + '->' + (Get-AmByteCount -Text $postText) + ' B, ' + $before.Lines + '->' + $postLines + ' lines; shortened=' + $result.shortened + ' migrated=' + $result.migrated + ' reindexed=' + $result.reindexed + ' dedangled=' + $result.dedangled)
    $commit = $null
    try { $commit = Save-AmHistorySnapshot -Store $store -Message $msg } catch {}
    $result.status = 'applied'
    $result.after_bytes = (Get-AmByteCount -Text $postText)
    $result.after_lines = $postLines
    $result.commit = $commit
    if ($commit -and $snapSha) {
        try {
            $diff = Get-AmHistoryDiff -FromSha $snapSha -ToSha $commit -RelPath ($rel + '/MEMORY.md')
            $dp = Join-Path (Get-AmWorkspaceStateDir -Workspace $ws) ('index-' + (Get-Date -Format 'yyyy-MM-dd') + '.diff')
            Write-AmTextAtomic -Path $dp -Text $diff
            $result.diff = $dp
        } catch {}
    }
    Write-MemoryLog -Component $Component -Message $msg
    $runReceipts += [pscustomobject]$result
}

foreach ($r in $runReceipts) { Write-AmReceipt -Record $r }

# Mark the throttle only when at least one store reached a real decision. A run that skipped
# everything because a session was live, or because Codex was down, must be retried.
$productive = @($runReceipts | Where-Object { @('applied', 'no-op', 'dry-run', 'protected-set-overflow') -contains $_.status })
if (-not $DryRun -and @($productive).Count -gt 0) { Mark-Throttle -Name $ThrottleName }

} finally {
    if ($lockTaken) { Release-CodexLock }
}
exit 0
