# memory-compact.ps1 - the autonomous compactor for the harness-native per-workspace
# auto-memory stores ("System A"). Registered as the Windows Task Scheduler entry
# ClaudeCode-MemoryCompactor-5am (daily, hidden via run-hidden.vbs, all roles).
#
# WHAT IT OWNS: the INDEX (MEMORY.md) only. It shortens over-long index lines, re-indexes
# orphans, removes dangling and duplicate lines, and migrates a bounded number of
# durable-but-pullable facts to mem0. It NEVER edits a fact file's body, never touches a
# doctrine entry, and never deletes a fact without its content existing elsewhere - removals
# are recorded as git history in an out-of-tree repo, from which any file restores individually.
#
# THE GUARDS (each exists because a specific failure was identified, several of them by review
# of this very file - see docs/systems/auto-memory-maintenance.md):
#   1. LIVENESS GATE - a session writing memories concurrently would have its appended lines
#      silently dropped by a rewrite built from a stale read. Observed live (an index grew 3
#      entries mid-build). Probes alias paths too, and fails CLOSED.
#   2. CAS - the index hash and the fact-file set are re-read immediately before the swap. Any
#      drift aborts the store with nothing written and nothing deleted. Abort, never roll back:
#      a directory-level revert would clobber whatever the live session just wrote.
#   3. HARD DOCTRINE RULE - `metadata.type: feedback` (NESTED; the top-level form matches zero
#      real files) or imperative phrasing means the line is never shortened by a model, merged,
#      migrated or dropped. Deterministic, applied BEFORE the judge sees anything.
#   4. STRICT DECREASE + SEAL + BLAST CAP + ROUND-TRIP - a judge edit applies only if it
#      strictly shrinks the index past the hygiene baseline, keeps an anchor token, and parses
#      back to the same single link; a line rewritten once is sealed forever; and no run may
#      remove more than a fifth of the lines, hygiene included.
#   5. WRITE-THEN-VERIFY MIGRATION - the fact reaches mem0 verbatim and is read back BY ID and
#      compared byte-for-byte BEFORE anything is removed, and the file is deleted only after
#      the new index is on disk and its invariants hold. An unverifiable write is undone.
#
# NO LOCAL FALLBACK: if the judge is unreachable the job applies deterministic hygiene only and
# does NOT mark the throttle, so the judge-only work is retried rather than counted as done.
#
# EXIT CODES: 0 normal (including per-store skips); 2 = bad invocation (-Force without scope,
# or a -Workspace that matches no store); 3 = refused to run (history unavailable, or the
# history repo has a remote). run-hidden.vbs propagates the code (WScript.Quit exitCode), so
# the scheduler records a non-zero result for a job that deliberately did nothing.

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

# -Force disarms the throttle, the trigger AND the liveness gate. That combination must never
# be aimable at the whole fleet while sessions are live: require it to be scoped or read-only.
if ($Force -and -not $DryRun -and -not $Workspace) {
    Write-MemoryLog -Component $Component -Message 'refusing -Force without -Workspace or -DryRun (it disables the liveness gate on every store)'
    Write-Host 'memory-compact: -Force requires -Workspace <name> or -DryRun.'
    exit 2
}

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
    } catch {
        # Never silent: this file IS the audit trail, and both watchdogs read its mtime, so a
        # failure here later masquerades as "the compactor is dead" and sends the operator to
        # the wrong subsystem.
        try { Write-MemoryLog -Component $Component -Message ('RECEIPT WRITE FAILED (' + $ReceiptPath + '): ' + $_.Exception.Message) } catch {}
    }
}

# 23h like the dream: the stamp is written at completion, so a strict 24h window would make a
# fixed 05:00 trigger ineligible by a few seconds every other night.
if (-not $DryRun -and -not $Force -and -not (Test-Throttle -Name $ThrottleName -MinIntervalSeconds 82800)) {
    Write-MemoryLog -Component $Component -Message 'skipping: nightly throttle (23h) not yet elapsed'
    exit 0
}

$stores = @(Get-AmStores) | Where-Object { -not $_.IsAlias }
if ($Workspace) {
    $stores = @($stores | Where-Object { $_.Workspace -eq $Workspace })
    if (@($stores).Count -eq 0) {
        # A typo'd -Workspace used to log "no stores found" and exit 0 - a rehearsal that
        # silently rehearsed nothing.
        Write-MemoryLog -Component $Component -Message ('-Workspace "' + $Workspace + '" matches no populated store')
        Write-Host ('memory-compact: -Workspace "' + $Workspace + '" matches no populated store.')
        exit 2
    }
}
if (@($stores).Count -eq 0) { Write-MemoryLog -Component $Component -Message 'no stores found'; exit 0 }

# 2026-09-03: EVERY store is a candidate. Deterministic hygiene (orphan re-index, dangling and
# duplicate-slug removal) is cheap, needs no judge, and used to run only for stores over the size
# trigger - so a small store carried 7 orphaned facts for days while the lint reported them every
# session and nothing ever fixed them. Below the trigger a store gets HYGIENE ONLY (no judge, no
# migration, no floor) and writes a receipt only when something changed; hysteresis (target <
# trigger) still keeps a just-compacted store out of the judge's hands.
$candidates = @()
$statsErrors = 0
foreach ($s in $stores) {
    try {
        $st = Get-AmStoreStats -Store $s
        $candidates += [pscustomobject]@{ Store = $s; Stats = $st; HygieneOnly = (-not $st.OverTrigger -and -not $Force) }
    } catch {
        $statsErrors++
        Write-MemoryLog -Component $Component -Message ('stats failed for ' + $s.Workspace + ': ' + $_.Exception.Message)
        Write-AmReceipt -Record ([pscustomobject]@{
            ts = (Get-Date).ToUniversalTime().ToString('o'); workspace = $s.Workspace
            status = 'error-stats'; note = $_.Exception.Message
        })
    }
}
if (@($candidates).Count -eq 0) {
    if ($statsErrors -gt 0) {
        # Do NOT claim "nothing above trigger" when the truth is "nothing could be measured",
        # and do not burn the window on it.
        Write-MemoryLog -Component $Component -Message ("could not measure $statsErrors store(s); not marking the throttle")
        exit 0
    }
    Write-MemoryLog -Component $Component -Message ('no measurable store across ' + @($stores).Count + ' store(s)')
    if (-not $DryRun) { Mark-Throttle -Name $ThrottleName }
    exit 0
}
$overTriggerCount = @($candidates | Where-Object { -not $_.HygieneOnly }).Count
Write-MemoryLog -Component $Component -Message ('candidates: ' + @($candidates).Count + ' store(s); ' + $overTriggerCount + ' above trigger (' + $script:AmTriggerBytes + ' B / ' + $script:AmTriggerLines + ' lines), the rest hygiene-only')

try { Initialize-AmHistory | Out-Null } catch {
    Write-MemoryLog -Component $Component -Message ('history init failed, aborting (no snapshot = no safe apply): ' + $_.Exception.Message)
    exit 3
}
if (Test-AmHistoryHasRemote) {
    Write-MemoryLog -Component $Component -Message 'ABORT: the memory history repo has a remote configured; these stores hold private facts and must never be pushed'
    exit 3
}

# One Codex mutex for the whole run, shared with L1a / dream / dedup.
# Taken for a -DryRun TOO: a dry run writes nothing to the stores but still calls the judge,
# and the mutex exists to serialise judge calls, not writes.
# The staleness window is sized to this run's worst case; a fixed 30 min would let another
# worker reclaim the lock mid-run on a multi-store night, and the mutex would quietly stop
# being one.
$lockMinutes = [Math]::Max(30, [int][Math]::Ceiling((@($candidates).Count * $CodexTimeoutSeconds) / 60.0) + 5)
# 2026-09-03: a held lock no longer aborts the run. The wake-up catch-up fires dream + dedup +
# compactor in the same second, so the compactor lost every catch-up night to the lock while a
# store sat over the sync limit. Only the JUDGE needs the mutex; deterministic hygiene and the
# convergence floor run regardless.
$script:JudgeDisabled = $false
if (-not (Acquire-CodexLock -Owner 'memory-compact' -MaxAgeMinutes $lockMinutes)) {
    Write-MemoryLog -Component $Component -Message 'codex lock held by another worker: judge skipped this run; deterministic hygiene + convergence floor only'
    $script:JudgeDisabled = $true
} else {
    $lockTaken = $true
}
$script:ExitCode = 0

function Get-AmSealPath { param($Store) return (Join-Path (Get-AmWorkspaceStateDir -Workspace $Store.Workspace) 'sealed-lines.json') }

function Get-AmSealed {
    # Throws on a corrupt seal file (Read-AmJsonFile distinguishes absent from unparseable).
    # The caller must not proceed seal-less: an empty seal set re-arms the model on every
    # already-shortened line, and the following Save-AmSealed would overwrite the file with only
    # this run's seals, discarding the history permanently.
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
    # Read a record back BY ID. Byte-equality against what we sent is the only falsifiable proof
    # the migration landed; a top-ranked semantic search for the fact's own text merely proves
    # something similar exists (it may be a pre-existing near-duplicate of the same fact).
    param([Parameter(Mandatory)][string]$Id)
    try {
        $key = Get-Mem0Key
        return (Invoke-RestMethod -Uri ('http://127.0.0.1:18791/v1/memories/' + $Id) -Headers @{ 'X-API-Key' = $key } -TimeoutSec 15)
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

function Add-AmMem0Migration {
    # The compactor's OWN write, not the shared Add-Mem0Memory helper, for two reasons:
    #   - the helper discards the server's `deduplicated` flag. add() with infer=false returns an
    #     EXISTING id on a hash hit; without the flag, a migration whose read-back then times out
    #     would "undo" itself by deleting a record it never created (an L1a fact, or an earlier
    #     migration). Never delete a dedup'd id.
    #   - the helper dead-letters failures and a later drain re-posts them unverified; a failed
    #     migration must simply be "not migrated".
    # Same byte-encoded body as the helper (PS 5.1 Invoke-RestMethod Latin-1-encodes a STRING).
    # Returns @{ Id; Deduplicated } or $null on failure.
    param([Parameter(Mandatory)][string]$Text, [Parameter(Mandatory)][string]$Source, [hashtable]$Metadata = @{})
    $Metadata['source'] = $Source
    if (-not $Metadata.ContainsKey('tier')) { $Metadata['tier'] = 'evidence' }
    $body = @{ messages = $Text; user_id = '__WSL_USER__'; infer = $false; metadata = $Metadata } | ConvertTo-Json -Depth 5 -Compress
    try {
        $key = Get-Mem0Key
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:18791/v1/memories' -Method Post `
            -Headers @{ 'X-API-Key' = $key; 'Content-Type' = 'application/json' } `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 20
        $id = $null
        if ($r -and $r.results -and @($r.results).Count -gt 0) { $id = [string]@($r.results)[0].id }
        if (-not $id) { return $null }
        $dedup = $false
        if ($r.PSObject.Properties['deduplicated'] -and $r.deduplicated) { $dedup = $true }
        return [pscustomobject]@{ Id = $id; Deduplicated = $dedup }
    } catch { return $null }
}

function Remove-AmMem0Record {
    # Undo a migration write whose verification failed. Without it the record stays in the corpus
    # referenced by nothing: its id never reaches a receipt (only verified migrations are
    # recorded), and the dedup exemption migrations now carry means a retry's duplicate is never
    # cleaned up either, because both copies are protected.
    param([Parameter(Mandatory)][string]$Id)
    try {
        $key = Get-Mem0Key
        Invoke-RestMethod -Uri ('http://127.0.0.1:18791/v1/memories/' + $Id) -Method Delete -Headers @{ 'X-API-Key' = $key } -TimeoutSec 15 | Out-Null
        return $true
    } catch { return $false }
}

$runStatuses = @()
try {
foreach ($cand in $candidates) {
    $store = $cand.Store
    $ws = $store.Workspace
    $rel = Get-AmStoreRelPath -Store $store
    $before = $cand.Stats
    $result = [ordered]@{
        ts = (Get-Date).ToUniversalTime().ToString('o'); workspace = $ws; dry_run = [bool]$DryRun
        before_bytes = $before.Bytes; before_lines = $before.Lines; status = 'unknown'
        shortened = 0; migrated = 0; reindexed = 0; dedangled = 0; dedup_slug = 0; floored = 0
        mem0 = @(); mem0_orphan = @(); after_bytes = $null; after_lines = $null
        commit = $null; snapshot = $null; note = ''
    }

    # Each store is wrapped so one unreadable index cannot kill the run - and its receipt is
    # written the moment the store finishes, never buffered to the end: a task hitting its
    # execution time limit mid-loop would otherwise lose every receipt, including the only
    # mapping from a deleted fact file to the corpus id that now holds it.
    try {

    # ---- GUARD 1: liveness -------------------------------------------------------------
    # Probes every workspace directory that reaches this store, alias paths included: a session
    # running under an alias writes its transcripts there, not under the canonical name.
    if (-not $Force -and (Test-AmWorkspaceLive -Workspace $ws -Dirs $store.ProbeDirs -WithinMinutes 30)) {
        $result.status = 'skipped-live-session'
        $result.note = 'a session in this workspace wrote within 30 min (or could not be probed); the harness writes the index whole from an in-context copy'
        Write-MemoryLog -Component $Component -Message ($ws + ': skipped (live session)')
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }

    $sweep = Clear-AmStoreTempFiles -Dir $store.Dir
    foreach ($swept in @($sweep.Removed)) {
        Write-MemoryLog -Component $Component -Message ($ws + ': swept a leftover temp file from a previously failed write: ' + $swept)
    }
    foreach ($stuck in @($sweep.Failed)) {
        # A full copy of the index stuck inside a synced, globbed directory - say so.
        Write-MemoryLog -Component $Component -Message ($ws + ': COULD NOT remove leftover temp file: ' + $stuck)
        $result.note = 'a leftover .am-tmp could not be removed: ' + $stuck
    }

    $sealed = Get-AmSealed -Store $store   # throws on corruption -> caught below, store skipped

    # ---- snapshot ----------------------------------------------------------------------
    $snapSha = Save-AmHistorySnapshot -Store $store -Message ('snapshot before compaction: ' + $ws)
    if (-not $snapSha) {
        $h = Invoke-AmGit -GitArgs @('rev-parse', 'HEAD')
        if ($h.Code -eq 0) { $snapSha = $h.Out.Trim() }
    }
    # The restore path depends on this snapshot actually containing the index. Assert it now,
    # while nothing has been mutated, rather than discovering it during recovery.
    if (-not $snapSha -or -not (Test-AmHistoryHasFile -Sha $snapSha -RelPath ($rel + '/MEMORY.md'))) {
        $result.status = 'skipped-no-snapshot'
        $result.note = 'the pre-run snapshot does not contain the index; refusing to mutate without a verified restore point'
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }
    $result.snapshot = $snapSha

    $preText = Read-AmText -Path $store.IndexPath
    $preHash = Get-AmContentHash -Path $store.IndexPath
    $preFiles = @(Get-AmFactFiles -Dir $store.Dir | ForEach-Object { $_.Name })   # throws if unreadable
    $idx = Read-AmIndex -Text $preText
    $records = @($idx.Records)
    $entries = @($records | Where-Object { $_.Kind -eq 'entry' })

    # An index with entries over a store that enumerates ZERO fact files is not a store to
    # repair, it is a store we cannot see. Treating it literally would classify every line as
    # dangling and wipe the index - and every downstream guard would agree, because they all
    # compare against the same empty set.
    if (@($entries).Count -gt 0 -and @($preFiles).Count -eq 0) {
        $result.status = 'aborted-no-fact-files'
        $result.note = 'the index has entries but the store enumerated no fact files; refusing to treat every line as dangling'
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }

    # frontmatter + doctrine classification for every entry (deterministic, pre-judge)
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
        if ($seenSlug.ContainsKey($r.Slug)) { $result.dedup_slug++; continue }    # duplicate link
        if (-not $onDisk.ContainsKey($r.Slug)) {
            # A title containing "]" is an ambiguous shape ("- [x] task with [link](f.md)" is a
            # checkbox, not a pointer). Such a line is still counted for reachability, but the
            # DESTRUCTIVE step - removing it as dangling - is reserved for the unambiguous form.
            if ($r.Title -match '\]') {
                Write-MemoryLog -Component $Component -Message ($ws + ': ambiguous line left alone (bracketed title, missing target ' + $r.Slug + ')')
                [void]$keep.Add($r); continue
            }
            $result.dedangled++; continue                                          # dangling link
        }
        $seenSlug[$r.Slug] = $true
        # A SECOND link on the line pointing at a missing file is a ghost the invariant check
        # would fail on forever while hygiene never fixed it: the index would be restored every
        # night and every run discarded. Rebuild the line without the dead extra link, keeping
        # any LIVE extra link, and tidy the parentheses the removal may leave behind.
        $deadExtras = @($r.ExtraSlugs | Where-Object { -not $onDisk.ContainsKey($_) })
        if (@($deadExtras).Count -gt 0) {
            $liveExtras = @($r.ExtraSlugs | Where-Object { $onDisk.ContainsKey($_) })
            $newSummary = $r.Summary
            foreach ($dx in $deadExtras) { $newSummary = ($newSummary -replace ('\s*\[[^\]]*\]\(' + [regex]::Escape($dx) + '\)'), '') }
            $newSummary = ($newSummary -replace '\(\s*(?:also|see also|see|and)?\s*\)', '') -replace '\s{2,}', ' '
            $newSummary = ($newSummary -replace '\s+and\s*$', '' -replace '\s*,\s*$', '').Trim()
            $candidate = New-AmEntryLine -Title $r.Title -Slug $r.Slug -Summary $newSummary -Indent $r.Indent
            if (Test-AmLineRoundTrips -Line $candidate -ExpectedSlug $r.Slug -ExpectedExtras $liveExtras) {
                $r.Summary = $newSummary; $r.ExtraSlugs = $liveExtras
                $r.Bytes = Get-AmByteCount -Text $candidate
                $r | Add-Member -NotePropertyName Dirty -NotePropertyValue $true -Force
                $result.dedangled++
            } else {
                Write-MemoryLog -Component $Component -Message ($ws + ': could not repair dead extra link(s) on ' + $r.Slug + ' (' + ($deadExtras -join ',') + '); left as is')
            }
        }
        [void]$keep.Add($r)
    }
    # orphan re-index: a fact on disk that nothing links to is invisible to every session.
    # Link accounting runs over EVERY line, parsed entry or not, so a pointer the entry regex
    # cannot read still counts as a link and its file is not mistaken for an orphan.
    $linkedNow = Get-AmIndexLinkedSlugs -Records @($keep)
    $hygieneRunningBytes = Get-AmByteCount -Text (ConvertTo-AmIndexText -Records @($keep) -Newline $idx.Newline)
    foreach ($n in $preFiles) {
        if ($linkedNow.ContainsKey($n)) { continue }
        $fm = Read-AmFrontmatter -Path (Join-Path $store.Dir $n)
        $title = if ($fm -and $fm.Name) { $fm.Name } else { [System.IO.Path]::GetFileNameWithoutExtension($n) }
        $desc = if ($fm -and $fm.Description) { $fm.Description } else { 'recovered orphan; no description' }
        $desc = Get-AmTruncatedToBytes -Text $desc -MaxBytes ($script:AmLineByteCap - 40)
        $newRec = [pscustomobject]@{ Index = -1; Kind = 'entry'; Raw = ''; Title = $title; Slug = $n; Summary = $desc; ExtraSlugs = @(); Bytes = 0; Dirty = $true }
        $line = New-AmEntryLine -Title $title -Slug $n -Summary $desc
        # A title containing "]" or a description containing a link would regenerate a line that
        # does not parse back - the file would read as an orphan again next run, forever.
        if (-not (Test-AmLineRoundTrips -Line $line -ExpectedSlug $n)) {
            $safeTitle = ($title -replace '[\[\]\(\)]', ' ').Trim()
            if (-not $safeTitle) { $safeTitle = [System.IO.Path]::GetFileNameWithoutExtension($n) }
            $safeDesc = ($desc -replace '[\[\]\(\)]', ' ').Trim()
            $newRec.Title = $safeTitle; $newRec.Summary = $safeDesc
            $line = New-AmEntryLine -Title $safeTitle -Slug $n -Summary $safeDesc
            if (-not (Test-AmLineRoundTrips -Line $line -ExpectedSlug $n)) {
                Write-MemoryLog -Component $Component -Message ($ws + ': cannot build a parseable index line for orphan ' + $n + '; left unindexed')
                continue
            }
        }
        $newRec.Bytes = Get-AmByteCount -Text $line
        # Raw carries the constructed line so a receipt row for this entry ("id | slug | line")
        # can never be blank: the audit trail must show how the index read before a removal.
        $newRec.Raw = $line
        # Re-indexing is the one hygiene action that ADDS bytes. It must never be the reason a
        # store crosses the hard sync limit - that is the exact failure this system exists to
        # prevent, and it would be perverse to cause it while repairing something else.
        if (($hygieneRunningBytes + $newRec.Bytes + 1) -ge $script:AmSyncLimitBytes) {
            Write-MemoryLog -Component $Component -Message ($ws + ': orphan ' + $n + ' left unindexed - re-indexing it would push the index over the sync limit')
            $result.note = 'one or more orphans left unindexed: no byte headroom (compact first)'
            continue
        }
        $hygieneRunningBytes += $newRec.Bytes + 1
        [void]$keep.Add($newRec)
        $meta[$n] = [pscustomobject]@{ Frontmatter = $fm; Doctrine = (Test-AmDoctrine -Record $newRec -Frontmatter $fm) }
        $result.reindexed++
    }

    # ---- planned ghosts: decide BEFORE the judge, before any write, before any mem0 post ----
    # If the index as hygiene would leave it still carries an entry link to a missing file, the
    # post-write invariant is guaranteed to fail. Finding that out AFTER writing (and after
    # posting migrations) is how a store got its files deleted nightly while its restored index
    # kept pointing at them. Abort here with nothing touched.
    $plannedGhosts = @(Get-AmEntryGhosts -Records @($keep) -OnDisk $onDisk)
    if (@($plannedGhosts).Count -gt 0) {
        $result.status = 'aborted-ghost-links'
        $result.note = ('entry link(s) to missing files that hygiene cannot repair: ' + ($plannedGhosts -join ', ') + ' - fix by hand; nothing written, nothing posted')
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }

    # ---- GUARD 4a: blast cap over ALL removals, hygiene included ------------------------
    # The cap used to be consulted only on the migration path, so the one removal loop that can
    # empty an entire index was unbounded.
    $blastCap = [Math]::Max(1, [int][Math]::Floor(@($entries).Count * 0.2))
    $hygieneRemovals = $result.dedangled + $result.dedup_slug
    if ($hygieneRemovals -gt $blastCap) {
        $result.status = 'aborted-blast-cap'
        $result.note = ('hygiene wanted to remove ' + $hygieneRemovals + ' line(s), over the ' + $blastCap + '-line cap for this store; refusing and reporting instead')
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }

    # The index as deterministic hygiene alone would leave it. This is the baseline the strict-
    # decrease rule is measured against: hygiene is CORRECTNESS (a dangling line points nowhere;
    # an orphan file is invisible) and must apply even when re-indexing an orphan costs a few
    # more bytes than the dangling line it replaced. Only the judge's edits must shrink.
    $hygieneBytes = Get-AmByteCount -Text (ConvertTo-AmIndexText -Records @($keep) -Newline $idx.Newline)
    $hygieneChanged = ($hygieneRemovals -gt 0 -or $result.reindexed -gt 0)

    # ---- feasibility: can the protected set alone even fit? ----------------------------
    $protected = @($keep | Where-Object { $_.Kind -eq 'entry' -and $meta[$_.Slug].Doctrine })
    $protectedFloor = 0
    foreach ($p in $protected) { $protectedFloor += [Math]::Min($p.Bytes, $script:AmLineByteCap) + 1 }
    if ($protected.Count -gt $script:AmTargetLines -or $protectedFloor -gt $script:AmTargetBytes) {
        $result.status = 'protected-set-overflow'
        $result.note = ('doctrine lines alone (' + $protected.Count + ' lines, ~' + $protectedFloor + ' B) exceed the target budget; the hard rule is never loosened autonomously - re-home doctrine into a topic file by hand')
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }

    # ---- the delta the judge actually sees ---------------------------------------------
    # Below the trigger there is no delta for the judge: hygiene is the whole job.
    $hygieneOnly = [bool]$cand.HygieneOnly
    $shortenable = @($keep | Where-Object {
        $_.Kind -eq 'entry' -and $_.Bytes -gt $script:AmLineByteCap -and -not $meta[$_.Slug].Doctrine -and -not $sealed.ContainsKey($_.Slug)
    })
    $migratable = @($keep | Where-Object {
        $_.Kind -eq 'entry' -and -not $meta[$_.Slug].Doctrine -and $meta[$_.Slug].Frontmatter -and
        (@('project', 'reference') -contains ("" + $meta[$_.Slug].Frontmatter.Type).ToLowerInvariant())
    })
    # 2026-09-03: a body over the server's storage cap 413s on every migration attempt, nightly,
    # forever ("returned no id; line kept" x3 in the log). Flag-only files are not candidates.
    $migratable = @($migratable | Where-Object {
        $fm = $meta[$_.Slug].Frontmatter
        (-not $fm) -or ((("" + $fm.Description + "`n`n" + $fm.Body).Trim()).Length -le $script:AmMem0MaxChars)
    })
    if ($hygieneOnly) { $shortenable = @(); $migratable = @() }
    $judgeNeeded = (@($shortenable).Count -gt 0 -or @($migratable).Count -gt 0)
    $judgeOk = $false
    $plan = $null

    if ($judgeNeeded -and $script:JudgeDisabled) { $result.note = 'codex lock held; judge skipped' }
    if ($judgeNeeded -and -not $script:JudgeDisabled) {
        $sb = New-Object System.Text.StringBuilder
        [void]$sb.AppendLine('You are compacting one workspace index of an agent memory system. Only the index is loaded into every session; each entry points at a fact file that is loaded on demand.')
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('For each candidate below return ONE decision:')
        [void]$sb.AppendLine('  SHORTEN - rewrite the hook to <= 130 bytes. It must still say WHEN to open the file: keep the distinguishing detail (numbers, identifiers, the surprising claim). Never a generic label. Plain text only - no markdown links, no parentheses containing a file name.')
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
        $raw = $null
        try { $raw = Invoke-CodexSubagent -Prompt $sb.ToString() -ReasoningEffort 'medium' -TimeoutSeconds $CodexTimeoutSeconds }
        catch {
            # NO LOCAL FALLBACK: judgment work waits for the judge. Deterministic hygiene from
            # this pass is still applied below, and the throttle is not marked.
            Write-MemoryLog -Component $Component -Message ($ws + ': judge unavailable (' + $_.Exception.Message + '); deterministic hygiene only')
            $result.note = 'judge unavailable; deterministic hygiene only'
        }
        if ($raw) {
            $text = Get-CodexResponseText -RawOutput $raw
            # The helper matches an OBJECT carrying an expected key; a bare top-level array
            # returns $null (observed in the dream log), hence the {"plan":[...]} wrapper.
            $parsed = Extract-JsonFromText -Text $text -ExpectedKey 'plan'
            if ($parsed) { $plan = @($parsed.plan); $judgeOk = $true }
            else {
                Write-MemoryLog -Component $Component -Message ($ws + ': judge returned unparseable JSON; deterministic hygiene only')
                $result.note = 'judge returned unparseable output; deterministic hygiene only'
            }
        }
    }

    # ---- apply ------------------------------------------------------------------------
    $byslug = @{}
    foreach ($r in $keep) { if ($r.Kind -eq 'entry') { $byslug[$r.Slug] = $r } }
    $removals = $hygieneRemovals
    $migrationsDone = 0
    $newlySealed = @{}
    $pendingDeletes = @()   # applied only after the new index is on disk and verified

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
            # A hook containing a markdown link would inject a phantom second slug: a ghost the
            # invariant check fails on forever while hygiene cannot fix it.
            if (-not (Test-AmLineRoundTrips -Line $candidateLine -ExpectedSlug $rec.Slug)) { continue }
            $anchors = Get-AmAnchorTokens -Text $rec.Summary
            if ($anchors.Count -gt 0) {
                $kept = $false
                # .Contains, never -like: an anchor such as `cfg[0].name` is a wildcard character
                # class to -like, which both rejects hooks that DO keep the anchor and accepts
                # hooks that dropped it. The false-accept defeats the guard entirely.
                foreach ($a in $anchors.Keys) { if ($hook.Contains($a)) { $kept = $true; break } }
                if (-not $kept) { continue }   # a hook that keeps no anchor has lost the trigger
            }
            $rec.Summary = $hook
            $rec.Bytes = $newBytes
            $rec | Add-Member -NotePropertyName Dirty -NotePropertyValue $true -Force
            $newlySealed[$slug] = $true        # one judge rewrite per line, ever
            $result.shortened++
        }
        elseif ($action -eq 'MIGRATE') {
            if ($migrationsDone -ge $MaxMigrationsPerRun) { continue }
            if ($removals -ge $blastCap) { continue }
            $fm = $meta[$slug].Frontmatter
            if (-not $fm -or -not $fm.Body) { continue }
            # Verbatim, never a paraphrase: a re-run must produce identical text so a retry
            # deduplicates instead of creating a second variant.
            $textOut = ($fm.Description + "`n`n" + $fm.Body).Trim()
            if ($textOut.Length -gt $script:AmMem0MaxChars) { continue }   # re-checked at apply time: the server would 413
            if ($DryRun) {
                # Project the removal so after_bytes and `migrated` describe the same world.
                $keep.Remove($rec) | Out-Null
                $migrationsDone++; $removals++; $result.migrated++
                continue
            }
            $mdata = @{ tier = 'evidence'; origin_slug = $slug; workspace = $ws }
            $w = Add-AmMem0Migration -Text $textOut -Source ('automemory:' + $ws + '/' + $slug) -Metadata $mdata
            if ($w -and $w.Id) {
                if (Test-AmMigrationLanded -Id $w.Id -Text $textOut) {
                    $keep.Remove($rec) | Out-Null
                    # The file is deleted only after the index write AND its invariants pass -
                    # deleting earlier is how an abort or a revert left the store pointing at
                    # files that were already gone.
                    $pendingDeletes += [pscustomobject]@{ Slug = $slug; Id = $w.Id; Raw = $rec.Raw; Deduplicated = $w.Deduplicated }
                    $migrationsDone++; $removals++; $result.migrated++
                } elseif ($w.Deduplicated) {
                    # The id belongs to a PRE-EXISTING record we did not create. Never delete it.
                    $result.mem0_orphan += ('' + $w.Id + ' | ' + $slug + ' | pre-existing (dedup) record, read-back failed; line kept, record untouched')
                    Write-MemoryLog -Component $Component -Message ($ws + ': migration ' + $slug + ' hit an existing record ' + $w.Id + ' whose read-back failed; line kept')
                } else {
                    if (Remove-AmMem0Record -Id $w.Id) {
                        Write-MemoryLog -Component $Component -Message ($ws + ': migration ' + $slug + ' unverifiable; the record was removed and the line kept')
                    } else {
                        $result.mem0_orphan += ('' + $w.Id + ' | ' + $slug + ' | unverified write that could not be removed')
                        Write-MemoryLog -Component $Component -Message ($ws + ': migration ' + $slug + ' unverifiable AND its record could not be removed; id ' + $w.Id + ' recorded in the receipt')
                    }
                }
            } else {
                # No id: the write failed or the server returned nothing usable. A record MAY
                # still have landed; the retry is hash-idempotent, so nothing to undo.
                $result.mem0_orphan += ('(no id) | ' + $slug + ' | write returned no id; line kept')
                Write-MemoryLog -Component $Component -Message ($ws + ': migration ' + $slug + ' returned no id; line kept')
            }
        }
    }

    # ---- convergence floor (deterministic; runs with or without the judge) ----------------
    # The judge shortens what it agrees to; the floor guarantees the index leaves this run UNDER
    # the sync limit or the run says so out loud. See Invoke-AmConvergenceFloor for the rule.
    $unconverged = $false
    $floorResult = Invoke-AmConvergenceFloor -Records $keep -StoreDir $store.Dir -Newline $idx.Newline -Meta $meta
    $result.floored = $floorResult.Floored
    if ($floorResult.Bytes -ge $script:AmSyncLimitBytes) {
        $unconverged = $true
        $result.note = (($result.note + '; ').TrimStart('; ') + 'UNCONVERGED: ' + $floorResult.Bytes + ' B still >= the ' + $script:AmSyncLimitBytes + ' B sync limit after the floor (doctrine lines are never shortened autonomously) - re-home doctrine into topic files by hand')
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        $script:ExitCode = 1
    }

    $newText = ConvertTo-AmIndexText -Records @($keep) -Newline $idx.Newline
    $newBytesTotal = Get-AmByteCount -Text $newText

    # Any exit between here and the index write must undo the migration writes it already made:
    # the line and file are still in place, so the corpus record is a duplicate nobody indexes -
    # and the dedup exemption would protect it forever. Records we did not create (dedup hits)
    # are never deleted.
    function Undo-AmPendingMigrations {
        param($Pending, [string]$Why)
        foreach ($pd in @($Pending)) {
            if ($pd.Deduplicated) {
                $result.mem0_orphan += ('' + $pd.Id + ' | ' + $pd.Slug + ' | pre-existing (dedup) record left in place after ' + $Why)
                continue
            }
            if (Remove-AmMem0Record -Id $pd.Id) {
                Write-MemoryLog -Component $Component -Message ($ws + ': undid migration write ' + $pd.Id + ' for ' + $pd.Slug + ' after ' + $Why)
            } else {
                $result.mem0_orphan += ('' + $pd.Id + ' | ' + $pd.Slug + ' | verified write left in corpus after ' + $Why + ' (delete failed)')
            }
        }
    }

    # A run with verified migrations pending is NEVER a no-op, even when the index text is
    # unchanged: an orphan re-indexed by hygiene and then migrated by the judge nets to the same
    # index bytes, but its file must still be deleted and its corpus id must reach the receipt.
    # Treating that as no-op left the file on disk, the record unnamed, and `migrated=1` beside
    # `status=no-op` (found by the receipt-fidelity test).
    if ($newText -eq $preText -and @($pendingDeletes).Count -eq 0) {
        # Distinguish "there was nothing to do" from "the judge never answered".
        $result.status = if ($unconverged) { 'unconverged' } elseif ($judgeNeeded -and -not $judgeOk) { 'skipped-judge-unavailable' } else { 'no-op' }
        $result.after_bytes = $before.Bytes; $result.after_lines = $before.Lines
        # A clean below-trigger store is the common nightly case: no log line, no receipt (a
        # receipt per store per night would bury the ones that matter).
        if (-not $hygieneOnly) {
            Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.status)
            Write-AmReceipt -Record ([pscustomobject]$result)
        }
        $runStatuses += $result.status
        continue
    }
    if ($newBytesTotal -gt $hygieneBytes -or (-not $hygieneChanged -and $newBytesTotal -ge $before.Bytes)) {
        Undo-AmPendingMigrations -Pending $pendingDeletes -Why 'rejected-no-shrink'
        $result.migrated = 0
        $result.status = 'rejected-no-shrink'
        $result.note = 'the judge-driven edits did not shrink the index past the hygiene baseline; discarded'
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }
    if ($DryRun) {
        $result.status = 'dry-run'
        $result.after_bytes = $newBytesTotal
        $result.after_lines = Get-AmLineCount -Text $newText
        Write-MemoryLog -Component $Component -Message ($ws + ': DRY RUN ' + $before.Bytes + ' -> ' + $newBytesTotal + ' B')
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }

    # ---- GUARD 2: compare-and-swap ------------------------------------------------------
    # Nothing has been removed from disk yet, so an abort here really is "no write performed".
    $nowHash = Get-AmContentHash -Path $store.IndexPath
    $nowFiles = @(Get-AmFactFiles -Dir $store.Dir | ForEach-Object { $_.Name })
    $appeared = @($nowFiles | Where-Object { $_ -notin $preFiles })
    $vanished = @($preFiles | Where-Object { $_ -notin $nowFiles })
    if ($nowHash -ne $preHash -or @($appeared).Count -gt 0 -or @($vanished).Count -gt 0) {
        Undo-AmPendingMigrations -Pending $pendingDeletes -Why 'concurrent-write abort'
        $result.migrated = 0
        $result.status = 'aborted-concurrent-write'
        $result.note = ('the store changed under the job (index hash, +' + @($appeared).Count + ' / -' + @($vanished).Count + ' files); nothing written, nothing deleted, migration writes undone - abort, never roll back over a live session')
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }

    Write-AmTextAtomic -Path $store.IndexPath -Text $newText

    # ---- post-write invariants: computed BEFORE any file is deleted ---------------------
    # Order is load-bearing. Deleting first and checking second is how a revert restored an
    # index that still pointed at files already gone (reproduced in review). Files pending
    # migration are EXPECTED to be unlinked at this point, so they are excluded from the orphan
    # test; nothing has been deleted, so `lost` must be empty unless a concurrent process acted.
    $migratedSlugs = @($pendingDeletes | ForEach-Object { $_.Slug })
    $postText = $null; $postIdx = $null; $postFiles = $null; $postOnDisk = @{}
    $verifyError = $null
    try {
        $postText = Read-AmText -Path $store.IndexPath
        $postIdx = Read-AmIndex -Text $postText
        $postFiles = @(Get-AmFactFiles -Dir $store.Dir | ForEach-Object { $_.Name })
        foreach ($n in $postFiles) { $postOnDisk[$n] = $true }
    } catch { $verifyError = $_.Exception.Message }

    if ($verifyError) {
        # The index is written but cannot be verified (enumeration failed AFTER the write).
        # Delete nothing: the pending files stay on disk, unlinked, and the next run re-indexes
        # them as orphans (their corpus copies are verified and hash-idempotent, so a later
        # migration converges). Report honestly instead of 'error-store'.
        $result.status = 'applied-unverified'
        $result.note = ('index written but post-write verification failed: ' + $verifyError + '; ' + @($pendingDeletes).Count + ' migrated file(s) NOT deleted')
        foreach ($pd in $pendingDeletes) { $result.mem0 += ('' + $pd.Id + ' | ' + $pd.Slug + ' | migrated, file retained (unverified state)') }
        foreach ($k in $newlySealed.Keys) { $sealed[$k] = (Get-Date).ToUniversalTime().ToString('o') }
        try { Save-AmSealed -Store $store -Sealed $sealed } catch {}
        try { $result.commit = Save-AmHistorySnapshot -Store $store -Message ('compact (unverified) ' + $ws) } catch {}
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }

    $postLinked = Get-AmIndexLinkedSlugs -Records $postIdx.Records
    $ghosts = @(Get-AmEntryGhosts -Records $postIdx.Records -OnDisk $postOnDisk)
    $orphans = @($postFiles | Where-Object { -not $postLinked.ContainsKey($_) -and $_ -notin $migratedSlugs })
    $lost = @($preFiles | Where-Object { -not $postOnDisk.ContainsKey($_) })
    $ok = (@($ghosts).Count -eq 0 -and @($orphans).Count -eq 0 -and @($lost).Count -eq 0)
    if (-not $ok) {
        # Restore ONLY the index - the single file this job wrote. Nothing has been deleted, so
        # a successful restore returns the store to exactly its pre-run state; the migration
        # writes are undone too. The restore's own success is checked: this is the last line
        # of defence, and a silent failure would leave a mutated index behind a receipt
        # claiming it was reverted.
        Undo-AmPendingMigrations -Pending $pendingDeletes -Why 'invariant failure'
        $result.migrated = 0
        $restored = Restore-AmHistoryFile -Sha $snapSha -RelPath ($rel + '/MEMORY.md')
        if ($restored) {
            $result.status = 'reverted-invariant-failure'
            $result.note = ('ghosts=' + (@($ghosts) -join ',') + ' orphans=' + @($orphans).Count + ' lost=' + @($lost).Count + '; index restored from ' + $snapSha + '; no file deleted')
        } else {
            $result.status = 'invariant-failure-RESTORE-FAILED'
            $result.note = ('ghosts=' + @($ghosts).Count + ' orphans=' + @($orphans).Count + ' lost=' + @($lost).Count +
                '; THE INDEX IS MUTATED AND WAS NOT RESTORED (no file deleted). Recover by hand: git --git-dir="' + (Get-AmHistoryGitDir) + '" --work-tree="' + (Get-AmProjectsRoot) + '" checkout ' + $snapSha + ' -- ' + $rel + '/MEMORY.md')
        }
        Write-MemoryLog -Component $Component -Message ($ws + ': INVARIANT FAILURE: ' + $result.note)
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }

    # ---- deferred deletes: invariants hold and the index no longer references these -----
    $deleteFailed = @()
    foreach ($pd in $pendingDeletes) {
        try {
            Remove-Item -LiteralPath (Join-Path $store.Dir $pd.Slug) -Force -ErrorAction Stop
            $result.mem0 += ('' + $pd.Id + ' | ' + $pd.Slug + ' | ' + $pd.Raw)
        } catch {
            # The fact is safe (verified in the corpus) but the file lingers unreferenced; the
            # next run re-indexes it as an orphan. Say so rather than counting a clean migration.
            $deleteFailed += $pd.Slug
            $result.mem0 += ('' + $pd.Id + ' | ' + $pd.Slug + ' | MIGRATED but file delete FAILED: ' + $_.Exception.Message)
        }
    }

    foreach ($k in $newlySealed.Keys) { $sealed[$k] = (Get-Date).ToUniversalTime().ToString('o') }
    try { Save-AmSealed -Store $store -Sealed $sealed } catch {
        # An unsaved seal silently re-arms the judge on those lines next run.
        $result.note = ('seal save FAILED (' + $_.Exception.Message + '); those lines may be re-offered next run')
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
    }

    $postLines = Get-AmLineCount -Text $postText
    $msg = ('compact ' + $ws + ': ' + $before.Bytes + '->' + (Get-AmByteCount -Text $postText) + ' B, ' + $before.Lines + '->' + $postLines + ' lines; shortened=' + $result.shortened + ' migrated=' + $result.migrated + ' reindexed=' + $result.reindexed + ' dedangled=' + $result.dedangled + ' dedup_slug=' + $result.dedup_slug + ' floored=' + $result.floored)
    $commit = $null
    try { $commit = Save-AmHistorySnapshot -Store $store -Message $msg }
    catch {
        $result.note = ('post-commit FAILED (' + $_.Exception.Message + '); this run is applied but not in history')
        Write-MemoryLog -Component $Component -Message ($ws + ': ' + $result.note)
    }
    $result.status = if ($unconverged) { 'applied-unconverged' } elseif ($commit) { 'applied' } else { 'applied-unrecorded' }
    $result.after_bytes = (Get-AmByteCount -Text $postText)
    $result.after_lines = $postLines
    $result.commit = $commit
    if (@($deleteFailed).Count -gt 0) { $result.note = ('migrated, but ' + @($deleteFailed).Count + ' fact file(s) could not be deleted: ' + ($deleteFailed -join ', ')) }
    if ($commit -and $snapSha) {
        try {
            $diff = Get-AmHistoryDiff -FromSha $snapSha -ToSha $commit -RelPath ($rel + '/MEMORY.md')
            $dp = Join-Path (Get-AmWorkspaceStateDir -Workspace $ws) ('index-' + (Get-Date -Format 'yyyy-MM-dd') + '.diff')
            Write-AmTextAtomic -Path $dp -Text $diff
            $result.diff = $dp
        } catch {
            Write-MemoryLog -Component $Component -Message ($ws + ': diff write failed: ' + $_.Exception.Message)
        }
    }
    Write-MemoryLog -Component $Component -Message $msg
    Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status

    } catch {
        # One store's failure must never cost the others their receipts.
        $result.status = 'error-store'
        $result.note = $_.Exception.Message
        try { Write-MemoryLog -Component $Component -Message ($ws + ': ERROR ' + $_.Exception.Message) } catch {}
        Write-AmReceipt -Record ([pscustomobject]$result); $runStatuses += $result.status
        continue
    }
}

# Mark the throttle only when at least one store reached a real decision. A run that skipped
# everything (live sessions, judge down, no snapshot, an abort) must be retried, not counted as
# done - which is why 'skipped-judge-unavailable' is deliberately absent from this list.
$productive = @($runStatuses | Where-Object { @('applied', 'applied-unrecorded', 'no-op', 'dry-run', 'protected-set-overflow') -contains $_ })
if (-not $DryRun -and -not $Force -and @($productive).Count -gt 0) { Mark-Throttle -Name $ThrottleName }

} finally {
    if ($lockTaken) { Release-CodexLock }
}
# 2026-09-03: an unconverged store is a FAILED run - the scheduled task's LastTaskResult must
# say so, not 0. ('applied-unconverged' / 'unconverged' are absent from $productive: the
# throttle stays open so the next run retries.)
exit $script:ExitCode
