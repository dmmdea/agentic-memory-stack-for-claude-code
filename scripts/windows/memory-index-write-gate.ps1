# memory-index-write-gate.ps1 - PostToolUse (Write|Edit) gate for a workspace auto-memory index.
#
# WHY (2026-09-03): the bash advisory that preceded this printed a warning and did nothing; live
# sessions ignored it. One store reached 27.4 KB with 126 of 180 index lines over the 130 B cap,
# and the harness - which refuses to sync a MEMORY.md at/over 25,000 B - silently loaded only
# part of it in another session. The nightly compactor cannot protect a session that is running
# NOW; the write-time gate is the only same-moment defense, so at the sync limit it acts.
#
# WHAT: after any Write/Edit whose file_path is a .../memory/MEMORY.md:
#   1. advisory (always): size, line count, and the over-cap index lines, as before;
#   2. normalization (ONLY when the index is at/over the sync limit): the longest non-doctrine
#      entry lines are truncated on a word boundary to the line cap until the index is back
#      under the compactor trigger (Invoke-AmConvergenceFloor - the same rule the nightly job
#      uses), written atomically behind a compare-and-swap on the content hash read at entry,
#      and receipted to ~/.claude/state/automemory/write-gate-receipts.jsonl.
#   Doctrine lines (metadata.type: feedback, or imperative phrasing) are never touched.
#   The full text of every truncated hook still lives in its fact file; only the pointer shrinks.
#
# CONTRACT: Windows PowerShell 5.1 (hooks run under powershell.exe) - no PS7 syntax. Never
# blocks (always exits 0), never throws to the harness, never writes unless the index is over the
# sync limit and unchanged since it was read.
$ErrorActionPreference = 'SilentlyContinue'
try {
    $payload = [Console]::In.ReadToEnd()
    if (-not $payload) { exit 0 }
    $o = $null
    try { $o = $payload | ConvertFrom-Json } catch { exit 0 }
    $path = $null
    if ($o -and $o.tool_input -and $o.tool_input.file_path) { $path = [string]$o.tool_input.file_path }
    if (-not $path) { exit 0 }
    $path = $path -replace '/', '\'
    if ($path -notmatch '\\memory\\MEMORY\.md$') { exit 0 }
    if (-not (Test-Path -LiteralPath $path)) { exit 0 }

    . (Join-Path $PSScriptRoot 'memory-store-lib.ps1')

    $hashAtRead = Get-AmContentHash -Path $path
    $text = Read-AmText -Path $path
    $bytes = Get-AmByteCount -Text $text
    $idx = Read-AmIndex -Text $text
    $lineCount = @([regex]::Split($text, "\r?\n") | Where-Object { $_.Trim() }).Count
    $long = @($idx.Records | Where-Object { $_.Kind -eq 'entry' -and $_.Bytes -gt $script:AmLineByteCap })

    if ($long.Count -eq 0 -and $bytes -lt $script:AmSyncLimitBytes -and $lineCount -lt $script:AmInjectLimitLines) { exit 0 }

    # ---- advisory (unchanged contract with the bash version) ----------------------------
    Write-Output ('[auto-memory index] ' + $bytes + ' B / ' + $lineCount + ' lines (caps: ' + $script:AmSyncLimitBytes + ' B sync, ' + $script:AmInjectLimitLines + ' lines injected)')
    if ($bytes -ge $script:AmSyncLimitBytes) {
        Write-Output '  OVER THE SYNC LIMIT: the harness loads only part of this file until it is under the limit.'
    } elseif ($lineCount -ge $script:AmInjectLimitLines) {
        Write-Output ('  OVER THE INJECTION CAP: entries past line ' + $script:AmInjectLimitLines + ' are not loaded into context.')
    }
    if ($long.Count -gt 0) {
        Write-Output ('  ' + $long.Count + ' index line(s) over ' + $script:AmLineByteCap + ' B. The index format is one pointer per entry: "- [Title](file.md) - hook". Keep the hook to the trigger for opening the file; detail belongs in the fact file.')
    }

    # ---- normalization: only at/over the sync limit -------------------------------------
    if ($bytes -ge $script:AmSyncLimitBytes) {
        $dir = Split-Path -Parent $path
        $floor = Invoke-AmConvergenceFloor -Records $idx.Records -StoreDir $dir -Newline $idx.Newline
        if ($floor.Floored -gt 0) {
            if ((Get-AmContentHash -Path $path) -ne $hashAtRead) {
                Write-Output '  (index changed under the gate - normalization skipped this time; the next write or the nightly compactor will retry)'
            } else {
                $newText = ConvertTo-AmIndexText -Records @($idx.Records) -Newline $idx.Newline
                Write-AmTextAtomic -Path $path -Text $newText
                $after = Get-AmByteCount -Text $newText
                try {
                    $stateDir = Join-Path $env:USERPROFILE '.claude\state\automemory'
                    [System.IO.Directory]::CreateDirectory($stateDir) | Out-Null
                    $rec = [ordered]@{ ts = (Get-Date).ToUniversalTime().ToString('o'); index = $path; before_bytes = $bytes; after_bytes = $after; floored = $floor.Floored; converged = ($after -lt $script:AmSyncLimitBytes) }
                    [System.IO.File]::AppendAllText((Join-Path $stateDir 'write-gate-receipts.jsonl'), (($rec | ConvertTo-Json -Compress) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
                } catch { }
                Write-Output ('  NORMALIZED: ' + $floor.Floored + ' over-cap hook(s) truncated to the line cap; index ' + $bytes + ' -> ' + $after + ' B. Your in-context copy of MEMORY.md is now STALE - re-read it before the next edit, and keep new hooks under ' + $script:AmLineByteCap + ' B. Full text of every entry is still in its fact file.')
                if ($after -ge $script:AmSyncLimitBytes) {
                    Write-Output '  STILL OVER THE SYNC LIMIT: the remaining over-cap lines are doctrine (never truncated) - re-home doctrine detail into topic files by hand.'
                }
            }
        } else {
            Write-Output '  Nothing normalizable (every over-cap line is doctrine) - re-home doctrine detail into topic files by hand.'
        }
    }
} catch { }
exit 0
