# learn-rules-drain.ps1 - drain the operator-correction queue into the memory authority.
#
# Capture (user-prompt-lib.ps1 Add-LearnRuleCapture) appends every correction-shaped prompt to
# ~/.mem0/learn-rules.jsonl the moment it happens. Until this script nothing ever read the queue
# back, so a correction only reached mem0 if the transcript extractor happened to rediscover it.
# This drain closes the loop:
#
#   correction + pending   -> redact -> POST /v1/memories (infer false, tier evidence,
#                             source learn-rules, kind correction) -> status "drained" + mem0_id
#   test-failure (any age) -> status "dropped": a failing test is not an operator correction and
#                             must never become a memory (the hook that wrote them is retired)
#   4xx that can never succeed (400/413/422) -> status "rejected" (else it would block the head
#                             of the queue for ever)
#   connect failure, 401/403/429, 5xx -> the run stops and every unposted line stays "pending"
#
# Bounded: at most -Max corrections are posted per run, and finished lines (drained / dropped /
# rejected) are pruned after -RetainDays so the file does not grow for ever. Pending lines are
# never pruned by age.
#
# Safety:
#   * One drain at a time: an exclusive lock on <queue>.lock. A second drain exits at once.
#   * The queue is rewritten with temp + File.Replace (one .bak kept). Capture appends to the
#     queue WITHOUT the lock, so the commit re-reads the file and carries forward every line
#     that is not one this run processed; a correction typed during the POSTs is not lost.
#   * A commit that fails after the POSTs succeeded (capture's Add-Content handle open without
#     delete-share, an AV or ACL lock, a full disk) is retried -CommitRetries times, each time
#     re-reading the queue. Every accepted POST is also journaled at once to <queue>.pending-commit
#     (line -> stamped line); the next run applies the journal before it posts anything, so a line
#     the authority already has is never posted twice. The journal is deleted after a good commit.
#   * A malformed line is preserved byte for byte.
#   * Text is redacted here as well as at capture, because lines written before capture
#     redacted may still hold a pasted credential.
#   * Fail-open for the session: spawned hidden by memory-maintenance-spawn.ps1 on every role, it
#     never throws to its caller and returns a summary object for tests and logs.
#
# Brand: the record's transcript path AND the correction's own text go to Get-BrandFromTranscriptPath
# (the C3 brand map in brands.json: path rules first, then, for a content-rule workspace, the
# words, exactly as L1a classifies a fact); a record that resolves to nothing keeps the brand it
# was captured with, and a record with neither carries no brand key.
#
# Test seams: -QueuePath, -AuthorityUrl, -ApiKey, -CommitRetries/-CommitRetryMs and -Force (skip the 1 h throttle). Production
# passes none of them; the API key is then read from the authority host on first use.
param(
    [string]$QueuePath = '',
    [string]$AuthorityUrl = '',
    [string]$ApiKey = '',
    [int]$Max = 50,
    [int]$RetainDays = 30,
    [int]$CommitRetries = 5,
    [int]$CommitRetryMs = 200,
    [switch]$Force
)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
try {
    . (Join-Path $ScriptDir 'memory-common.ps1')
    Initialize-MemoryEnv
} catch { return }

function Write-DrainLog {
    param([string]$Message)
    try { Write-MemoryLog -Component 'learn-rules-drain' -Message $Message } catch {}
}

function New-DrainSummary {
    param([string]$Status, [string]$Reason = '')
    return [pscustomobject]@{
        status = $Status; reason = $Reason
        drained = 0; dropped = 0; rejected = 0; pruned = 0
        pending = 0; malformed = 0
    }
}

function Get-DrainRecordKind {
    param($Rec)
    foreach ($n in 'kind', 'type') {
        $p = $Rec.PSObject.Properties[$n]
        if ($p -and $p.Value) { return ([string]$p.Value).ToLowerInvariant() }
    }
    return ''
}

function Get-DrainString {
    # A string field, or '' . pwsh 7's ConvertFrom-Json turns any date-shaped string into a
    # [datetime], so the type is checked instead of cast.
    param($Rec, [string]$Name)
    $p = $Rec.PSObject.Properties[$Name]
    if ($p -and ($p.Value -is [string])) { return [string]$p.Value }
    return ''
}

function Get-DrainRawTimestamp {
    # A timestamp field read from the raw line, as written. Never through ConvertFrom-Json:
    # pwsh 7 would hand back a [datetime] and the string round trip would change its shape.
    param([string]$Raw, [string]$Name)
    $m = [regex]::Match($Raw, '"' + $Name + '"\s*:\s*"([^"\\]*)"')
    if ($m.Success) { return $m.Groups[1].Value }
    return ''
}

function Set-DrainStatus {
    # Textual patch of the one status token, so every other byte of the line (the original ts, the
    # escaping) survives untouched. Capture writes status last, so the LAST match is the field;
    # a correction that quotes the token is JSON-escaped (\"status\") and cannot match.
    param([string]$Raw, [string]$Status, [string]$ResolvedAt, [hashtable]$Extra)
    $ms = [regex]::Matches($Raw, '"status"\s*:\s*"pending"')
    if ($ms.Count -eq 0) { return $null }
    $m = $ms[$ms.Count - 1]
    $frag = '"status":"' + $Status + '","resolved_at":"' + $ResolvedAt + '"'
    foreach ($k in $Extra.Keys) { $frag += ',"' + $k + '":' + (ConvertTo-Json -InputObject ([string]$Extra[$k]) -Compress) }
    return $Raw.Substring(0, $m.Index) + $frag + $Raw.Substring($m.Index + $m.Length)
}

function Get-DrainBrand {
    # -Text is the correction as it will be posted. A transcript under a content-rule workspace is
    # classified by its words, not its path, so without the text such a line could never be routed
    # (it would keep whatever brand capture recorded, which is none for a path that routes nowhere).
    param($Rec, [string]$Text = '')
    $brand = ''
    $tp = Get-DrainString $Rec 'transcript'
    if ($tp) {
        try {
            $b = Get-BrandFromTranscriptPath -Path $tp -Text $Text
            if ($b -and $b.brand) { $brand = [string]$b.brand }
        } catch {}
    }
    if (-not $brand) { $brand = Get-DrainString $Rec 'brand' }
    return $brand
}

function Send-DrainMemory {
    # One POST. Returns @{ ok; id; code; error }. code 0 = no HTTP response at all.
    param([string]$Url, [string]$Key, [string]$Text, [hashtable]$Metadata)
    $body = @{
        messages = $Text
        user_id  = '__WSL_USER__'
        infer    = $false
        metadata = $Metadata
    } | ConvertTo-Json -Depth 5 -Compress
    try {
        # Bytes, not a string: PS 5.1 encodes a string -Body as Latin-1 and a non-ASCII correction
        # (Spanish text, an em-dash) would reach the server as invalid UTF-8.
        $r = Invoke-WebRequest -Uri ($Url + '/v1/memories') -Method Post -UseBasicParsing `
            -Headers @{ 'X-API-Key' = $Key } -ContentType 'application/json' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 20
        $id = $null
        try {
            $j = $r.Content | ConvertFrom-Json
            if ($j -and $j.results -and @($j.results).Count -gt 0) { $id = [string]@($j.results)[0].id }
        } catch {}
        return @{ ok = $true; id = $id; code = [int]$r.StatusCode; error = '' }
    } catch {
        $code = 0
        try { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } } catch {}
        return @{ ok = $false; id = $null; code = $code; error = $_.Exception.Message }
    }
}

function Add-DrainJournal {
    # Best effort: one JSON line per accepted POST, written the moment it is accepted.
    param([string]$Journal, [string]$Raw, [string]$NewRaw)
    try {
        $j = ConvertTo-Json -InputObject ([pscustomobject]@{ k = $Raw; v = $NewRaw }) -Compress
        [System.IO.File]::AppendAllText($Journal, $j + "`n", (New-Object System.Text.UTF8Encoding($false)))
    } catch { Write-DrainLog "journal write failed (non-fatal): $($_.Exception.Message)" }
}

function Read-DrainJournal {
    # raw line -> queue of stamped lines, or an empty dictionary. A malformed journal line is skipped.
    param([string]$Journal)
    $d = New-Object 'System.Collections.Generic.Dictionary[string,System.Collections.Queue]'
    if (-not (Test-Path -LiteralPath $Journal)) { return $d }
    try {
        foreach ($l in [System.IO.File]::ReadAllLines($Journal, (New-Object System.Text.UTF8Encoding($false)))) {
            if ([string]::IsNullOrWhiteSpace($l)) { continue }
            try {
                $e = $l | ConvertFrom-Json
                $k = Get-DrainString $e 'k'; $v = Get-DrainString $e 'v'
                if (-not $k -or -not $v) { continue }
                if (-not $d.ContainsKey($k)) { $d[$k] = New-Object System.Collections.Queue }
                $d[$k].Enqueue($v)
            } catch {}
        }
    } catch { Write-DrainLog "journal unreadable (non-fatal): $($_.Exception.Message)" }
    return $d
}

function Get-DrainCommitPlan {
    # Re-read the queue (capture may have appended meanwhile), apply the stamped lines, prune.
    # Pure with respect to $Replace: it walks a private per-line index.
    param([string]$Queue, $Replace, [int]$RetainDays)
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $current = @([System.IO.File]::ReadAllLines($Queue, $utf8))
    $cutoff = (Get-Date).ToUniversalTime().AddDays(-$RetainDays)
    $final = New-Object System.Collections.Generic.List[string]
    $used = New-Object 'System.Collections.Generic.Dictionary[string,int]'
    $changed = $false; $pruned = 0; $pending = 0
    foreach ($raw in $current) {
        if ([string]::IsNullOrWhiteSpace($raw)) { continue }
        $line = $raw
        if ($Replace.ContainsKey($raw)) {
            $i = 0
            if ($used.ContainsKey($raw)) { $i = $used[$raw] }
            if ($i -lt $Replace[$raw].Count) {
                $line = [string]$Replace[$raw][$i]
                $used[$raw] = $i + 1
                $changed = $true
            }
        }
        $keep = $true
        try {
            $r2 = $line | ConvertFrom-Json
            $st = (Get-DrainString $r2 'status').ToLowerInvariant()
            if (@('drained', 'dropped', 'rejected') -contains $st) {
                $stamp = $null
                foreach ($n in 'resolved_at', 'ts') {
                    $t = Get-DrainRawTimestamp $line $n
                    if (-not $stamp -and $t) {
                        try { $stamp = ([datetime]::Parse($t, [System.Globalization.CultureInfo]::InvariantCulture,
                            [System.Globalization.DateTimeStyles]::AdjustToUniversal -bor [System.Globalization.DateTimeStyles]::AssumeUniversal)) } catch {}
                    }
                }
                if ($stamp -and $stamp -lt $cutoff) { $keep = $false }
            } elseif ($st -eq 'pending') {
                $pending++
            }
        } catch {}
        if ($keep) { $final.Add($line) } else { $pruned++; $changed = $true }
    }
    return @{ final = $final; changed = $changed; pruned = $pruned; pending = $pending }
}

function Invoke-LearnRulesDrain {
    param([string]$Queue, [string]$Url, [string]$Key, [int]$MaxPerRun, [int]$RetainDays, [bool]$IgnoreThrottle,
          [int]$Retries = 5, [int]$RetryMs = 200)

    if (-not (Test-Path -LiteralPath $Queue)) { return (New-DrainSummary 'empty') }

    # ---- one drain at a time (the OS drops the lock if this process dies) ----
    $lock = $null
    try {
        $lock = New-Object System.IO.FileStream(($Queue + '.lock'), [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch {
        Write-DrainLog 'another drain holds the lock; skipping this run'
        return (New-DrainSummary 'locked')
    }
    try {
        if (-not $IgnoreThrottle -and -not (Test-Throttle -Name 'learn-rules-drain' -MinIntervalSeconds 3600)) {
            return (New-DrainSummary 'throttled')
        }

        $utf8 = New-Object System.Text.UTF8Encoding($false)
        $snapshot = @([System.IO.File]::ReadAllLines($Queue, $utf8))
        $summary = New-DrainSummary 'ok'
        $nowIso = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

        # raw line -> replacement raw line, in file order per raw text (duplicates are legal).
        # Ordinal dictionary: a PowerShell @{} is case-insensitive and would merge two lines that
        # differ only in case.
        $replace = New-Object 'System.Collections.Generic.Dictionary[string,System.Collections.Generic.List[string]]'
        # A previous run's POSTs that were accepted but never committed to the queue.
        $journalPath = $Queue + '.pending-commit'
        $journal = Read-DrainJournal $journalPath
        $posted = 0
        $abort = ''
        foreach ($raw in $snapshot) {
            if ([string]::IsNullOrWhiteSpace($raw)) { continue }
            $rec = $null
            try { $rec = $raw | ConvertFrom-Json } catch { $rec = $null }
            if (-not $rec) { $summary.malformed++; continue }
            $status = (Get-DrainString $rec 'status').ToLowerInvariant()
            if ($status -ne 'pending') { continue }
            # Already accepted by the authority in an earlier run whose commit failed: apply the
            # stamped line at this run's commit, never post it again.
            if ($journal.ContainsKey($raw) -and $journal[$raw].Count -gt 0) {
                if (-not $replace.ContainsKey($raw)) { $replace[$raw] = New-Object 'System.Collections.Generic.List[string]' }
                $replace[$raw].Add([string]$journal[$raw].Dequeue())
                continue
            }
            $kind = Get-DrainRecordKind $rec
            # A pending line whose status token cannot be patched is never posted: it could not be
            # marked, and would be posted again on every run.
            if ($raw -notmatch '"status"\s*:\s*"pending"') { $summary.malformed++; continue }

            $newStatus = ''
            $extra = @{}
            if ($kind -eq 'test-failure') {
                $newStatus = 'dropped'
                $summary.dropped++
            } elseif ($kind -eq 'correction') {
                if ($abort -or $posted -ge $MaxPerRun) { continue }
                $text = Redact-Secrets (Get-DrainString $rec 'correction').Trim()
                if ([string]::IsNullOrWhiteSpace($text)) {
                    $newStatus = 'dropped'
                    $summary.dropped++
                } else {
                    $meta = @{
                        tier        = 'evidence'
                        source      = 'learn-rules'
                        kind        = 'correction'
                        captured_at = (Get-DrainRawTimestamp $raw 'ts')
                        session_id  = (Get-DrainString $rec 'session_id')
                    }
                    $brand = Get-DrainBrand $rec -Text $text
                    if ($brand) { $meta['brand'] = $brand }
                    # The key lives in WSL (a UNC read that can wake the distro), so it is fetched
                    # only now: after the lock and the throttle, and only when a line is going out.
                    if (-not $Key) {
                        try { $Key = Get-Mem0Key } catch { $Key = '' }
                        if (-not $Key) {
                            $abort = 'no-key'
                            Write-DrainLog 'no API key available; corrections stay pending'
                        }
                    }
                    if ($abort) { continue }
                    $posted++
                    $res = Send-DrainMemory -Url $Url -Key $Key -Text $text -Metadata $meta
                    if ($res.ok) {
                        $newStatus = 'drained'
                        if ($res.id) { $extra['mem0_id'] = $res.id }
                        $summary.drained++
                    } elseif (@(400, 413, 422) -contains $res.code) {
                        $newStatus = 'rejected'
                        $extra['error'] = 'http-' + $res.code
                        $summary.rejected++
                    } else {
                        # Connect failure, 401/403/429, 5xx: the authority (or the key) is the
                        # problem, not this line. Stop instead of timing out on every other one.
                        $abort = if ($res.code -gt 0) { 'http-' + $res.code } else { 'unreachable' }
                    }
                }
            } else {
                continue
            }
            if (-not $newStatus) { continue }

            $newRaw = Set-DrainStatus -Raw $raw -Status $newStatus -ResolvedAt $nowIso -Extra $extra
            if (-not $replace.ContainsKey($raw)) { $replace[$raw] = New-Object 'System.Collections.Generic.List[string]' }
            $replace[$raw].Add($newRaw)
            # The authority has this line now (or refused it for good): remember it before anything
            # else can fail, so no later run posts it again.
            if ($newStatus -eq 'drained' -or $newStatus -eq 'rejected') { Add-DrainJournal $journalPath $raw $newRaw }
        }

        # ---- commit: re-read (capture may have appended meanwhile), merge, prune, swap ----
        # File.Replace fails while another process holds the queue open without delete-share (the
        # capture append does, briefly), so it is retried; every attempt re-plans from a fresh read
        # so a line appended during the wait is carried over, not lost.
        $tmp = $Queue + '.tmp'
        $bak = $Queue + '.bak'
        $committed = $false
        $commitErr = ''
        for ($attempt = 1; $attempt -le [Math]::Max(1, $Retries); $attempt++) {
            if ($attempt -gt 1) { Start-Sleep -Milliseconds $RetryMs }
            try {
                $plan = Get-DrainCommitPlan -Queue $Queue -Replace $replace -RetainDays $RetainDays
                $summary.pruned = $plan.pruned
                $summary.pending = $plan.pending
                # A queue this run only inspected is left byte for byte as it was.
                if ($plan.changed) {
                    $text = ''
                    if ($plan.final.Count -gt 0) { $text = ($plan.final -join "`n") + "`n" }
                    [System.IO.File]::WriteAllText($tmp, $text, $utf8)
                    [System.IO.File]::Replace($tmp, $Queue, $bak)
                }
                $committed = $true
                break
            } catch {
                $commitErr = [string]$_.Exception.Message
                try { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } } catch {}
            }
        }
        if ($committed) {
            try { if (Test-Path -LiteralPath $journalPath) { Remove-Item -LiteralPath $journalPath -Force -ErrorAction Stop } }
            catch { Write-DrainLog "journal not removed (non-fatal): $($_.Exception.Message)" }
        } else {
            # The journal keeps what the authority accepted; the next run applies it without posting.
            Write-DrainLog "commit failed after $Retries attempts: $commitErr; the accepted lines stay in the journal"
            $summary.status = 'aborted'
            $summary.reason = 'commit-failed: ' + $commitErr
            return $summary
        }

        if ($abort) {
            $summary.status = 'aborted'
            $summary.reason = $abort
        } else {
            Mark-Throttle -Name 'learn-rules-drain'
        }
        Write-DrainLog ("run {0}{1}: drained={2} dropped={3} rejected={4} pruned={5} pending={6} malformed={7}" -f `
            $summary.status, $(if ($abort) { " ($abort)" } else { '' }), $summary.drained, $summary.dropped,
            $summary.rejected, $summary.pruned, $summary.pending, $summary.malformed)
        return $summary
    } finally {
        if ($lock) { $lock.Dispose() }
    }
}

try {
    $home_ = Get-AmsHomeDir
    if (-not $QueuePath)    { $QueuePath = Join-Path $home_ (Join-Path '.mem0' 'learn-rules.jsonl') }
    if (-not $AuthorityUrl) { $AuthorityUrl = Get-Mem0AuthorityUrl }
    $AuthorityUrl = $AuthorityUrl.TrimEnd('/')

    if (-not (Test-Path -LiteralPath $QueuePath)) { return (New-DrainSummary 'empty') }
    return (Invoke-LearnRulesDrain -Queue $QueuePath -Url $AuthorityUrl -Key $ApiKey -MaxPerRun $Max -RetainDays $RetainDays -IgnoreThrottle ([bool]$Force) -Retries $CommitRetries -RetryMs $CommitRetryMs)
} catch {
    Write-DrainLog "drain aborted (non-fatal): $_"
    return (New-DrainSummary 'aborted' ([string]$_.Exception.Message))
}
