# memory-lint.ps1 - deterministic, zero-LLM health check over the native per-workspace
# auto-memory stores. Spawned DETACHED from SessionStart by memory-maintenance-spawn.ps1
# (Windows PowerShell 5.1), so it must exit fast and fail open: a failure here can never
# block a session.
#
# READ-ONLY BY CONTRACT. This job never writes inside a store. It recomputes every finding
# from disk (a store holds <= ~200 small files, so a full scan is milliseconds) and writes a
# single summary to ~/.claude/state/automemory/lint-summary.json, which storage-cap-check.sh
# reads for the SessionStart banner. There is deliberately NO flag ledger / audited-keys
# watermark: the finding set is tiny and fully recomputable, and a monotone watermark would
# suppress a recurrence of an issue that was fixed and then came back.
#
# Mutations belong to memory-compact.ps1, which holds the Codex lock and runs when no session
# in that workspace is live.

param([switch]$Quiet)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

try {
    . (Join-Path $ScriptDir 'memory-common.ps1')
    . (Join-Path $ScriptDir 'memory-store-lib.ps1')
    Initialize-MemoryEnv
} catch { exit 0 }

# Own 6h throttle (independent of the other SessionStart children), so a burst of session
# starts costs one scan at worst. Marked only on success.
if (-not $Quiet -and -not (Test-Throttle -Name 'memory-lint' -MinIntervalSeconds 21600)) { exit 0 }

try {
    $stores = @(Get-AmStores) | Where-Object { -not $_.IsAlias }
    $findings = New-Object System.Collections.ArrayList
    $storeRows = New-Object System.Collections.ArrayList
    foreach ($s in $stores) {
        try {
            $stats = Get-AmStoreStats -Store $s
            foreach ($f in @(Get-AmLintFindings -Store $s)) { [void]$findings.Add($f) }
            [void]$storeRows.Add([pscustomobject]@{
                workspace = $s.Workspace
                bytes     = $stats.Bytes
                lines     = $stats.Lines
                entries   = $stats.Entries
                files     = $stats.Files
                over_trigger = $stats.OverTrigger
            })
        } catch {
            [void]$findings.Add([pscustomobject]@{ store = $s.Workspace; kind = 'scan-error'; file = 'MEMORY.md'; detail = $_.Exception.Message })
        }
    }

    # Watch-the-watcher: a store above the compaction trigger with no recent receipt means the
    # scheduled job is not running (task never fired, box was off at 5am, Codex unreachable).
    # Silence is the failure mode this stack has been bitten by; surface it.
    $receiptPath = Join-Path (Get-AmStateRoot) 'compact-receipts.jsonl'
    $lastReceiptAgeH = $null
    if (Test-Path -LiteralPath $receiptPath) {
        try { $lastReceiptAgeH = [math]::Round((([DateTime]::UtcNow) - (Get-Item -LiteralPath $receiptPath).LastWriteTimeUtc).TotalHours, 1) } catch {}
    }
    $overTrigger = @($storeRows | Where-Object { $_.over_trigger })
    $stale = ($overTrigger.Count -gt 0 -and ($null -eq $lastReceiptAgeH -or $lastReceiptAgeH -gt 48))
    if ($stale) {
        [void]$findings.Add([pscustomobject]@{
            store = '(fleet)'; kind = 'compactor-silent'; file = 'ClaudeCode-MemoryCompactor-5am'
            detail = ('' + $overTrigger.Count + ' store(s) above trigger, last compactor receipt: ' + $(if ($null -eq $lastReceiptAgeH) { 'never' } else { '' + $lastReceiptAgeH + 'h ago' }))
        })
    }
    # A FRESH receipt file is not proof of progress: a compactor that aborts every night writes
    # a receipt every night. If the last few runs for a store above trigger all ended in a
    # non-productive status, the maintainer is stuck, not working.
    if ($overTrigger.Count -gt 0 -and (Test-Path -LiteralPath $receiptPath)) {
        try {
            $tail = @(Get-Content -LiteralPath $receiptPath -Tail 40 -ErrorAction Stop)
            foreach ($row in $storeRows) {
                if (-not $row.over_trigger) { continue }
                $mine = @()
                foreach ($l in $tail) {
                    if (-not $l.Trim()) { continue }
                    try { $o = $l | ConvertFrom-Json } catch { continue }
                    if ($o.workspace -eq $row.workspace -and -not $o.dry_run) { $mine += $o }
                }
                $recent = @($mine | Select-Object -Last 3)
                if (@($recent).Count -ge 3) {
                    $good = @($recent | Where-Object { @('applied', 'applied-unrecorded', 'no-op') -contains $_.status })
                    if (@($good).Count -eq 0) {
                        [void]$findings.Add([pscustomobject]@{
                            store = $row.workspace; kind = 'compactor-unproductive'; file = 'MEMORY.md'
                            detail = ('above trigger and the last 3 runs all ended: ' + (@($recent | ForEach-Object { $_.status }) -join ', '))
                        })
                    }
                }
            }
        } catch {}
    }
    # The history repo must never gain a remote: these stores hold credentials and private
    # brand facts, and a push would publish them.
    if (Test-AmHistoryHasRemote) {
        [void]$findings.Add([pscustomobject]@{ store = '(fleet)'; kind = 'history-remote'; file = 'history.git'; detail = 'the memory history repo has a REMOTE configured - it must be local-only' })
    }

    $summary = [pscustomobject]@{
        generated_at = (Get-Date).ToUniversalTime().ToString('o')
        stores       = @($storeRows)
        findings     = @($findings)
        counts       = [pscustomobject]@{
            total       = @($findings).Count
            orphan      = @($findings | Where-Object kind -eq 'orphan').Count
            dangling    = @($findings | Where-Object kind -eq 'dangling').Count
            dup_slug    = @($findings | Where-Object kind -eq 'dup-slug').Count
            long_line   = @($findings | Where-Object kind -eq 'long-line').Count
            oversized   = @($findings | Where-Object kind -eq 'oversized-file').Count
            over_budget = @($findings | Where-Object { $_.kind -eq 'over-sync-limit' -or $_.kind -eq 'over-inject-limit' }).Count
            scan_error  = @($findings | Where-Object kind -eq 'scan-error').Count
            # 'scan-error' and 'compactor-unproductive' MUST be here: the banner renders only
            # actionable kinds, so a finding missing from this list reaches no surface at all -
            # a store that cannot be read would have been completely invisible.
            actionable  = @($findings | Where-Object { @('orphan', 'dangling', 'dup-slug', 'over-sync-limit', 'over-inject-limit', 'compactor-silent', 'compactor-unproductive', 'history-remote', 'scan-error') -contains $_.kind }).Count
        }
        last_receipt_age_hours = $lastReceiptAgeH
    }
    Write-AmJsonFile -Path (Join-Path (Get-AmStateRoot) 'lint-summary.json') -Object $summary
    Write-MemoryLog -Component 'memory-lint' -Message ('scanned ' + @($stores).Count + ' store(s): ' + $summary.counts.total + ' finding(s), ' + $summary.counts.actionable + ' actionable')
    if (-not $Quiet) { Mark-Throttle -Name 'memory-lint' }
} catch {
    try { Write-MemoryLog -Component 'memory-lint' -Message ('FAILED: ' + $_.Exception.Message) } catch {}
    exit 0
}
exit 0
