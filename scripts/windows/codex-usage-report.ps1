# codex-usage-report.ps1 — what the Codex judges actually cost, per job.
#
# WHY THIS EXISTS (2026-09-07): the NLI write-gate is pinned to the classify model but left
# DISABLED, because enabling it would put an uncached judge call in front of EVERY mem0 write
# and nobody could say what that costs. "It is probably fine" is not a budget. This turns the
# usage ledger + the plan's own 7-day window into the number that decision needs.
#
# Read-only. Never throws: a missing ledger, an unreadable token or an unreachable endpoint
# each degrade to a stated "unknown" rather than a failure — a reporting tool that dies on a
# missing optional input teaches you nothing.
#
#   codex-usage-report.ps1                 # last 7 days
#   codex-usage-report.ps1 -Days 1         # yesterday's shape
#   codex-usage-report.ps1 -Json           # machine-readable, for a future health row
[CmdletBinding()]
param(
    [int]$Days = 7,
    [switch]$Json
)

$ErrorActionPreference = 'Continue'
$ledger = Join-Path $env:USERPROFILE '.claude\logs\codex-usage.jsonl'
$cutoff = (Get-Date).ToUniversalTime().AddDays(-1 * [Math]::Abs($Days))

$rows = @()
if (Test-Path -LiteralPath $ledger) {
    foreach ($line in (Get-Content -LiteralPath $ledger -ErrorAction SilentlyContinue)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $o = $null
        try { $o = $line | ConvertFrom-Json } catch { continue }   # one torn line must not end the report
        if (-not $o.ts) { continue }
        $t = [datetime]::MinValue
        try { $t = ([datetime]::Parse([string]$o.ts, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal)).ToUniversalTime() } catch { continue }
        if ($t -lt $cutoff) { continue }
        $rows += $o
    }
}

# --- per job: calls, tokens, latency, and how often it FAILED --------------------------------
# Outcome matters as much as cost here: a job that is cheap because half its calls die is not
# cheap, it is broken, and a token total alone hides that completely.
$byJob = @()
foreach ($g in ($rows | Group-Object -Property component)) {
    $tok = ($g.Group | Measure-Object -Property tokens_used -Sum).Sum
    $durs = @($g.Group | ForEach-Object { [int]$_.duration_ms } | Where-Object { $_ -gt 0 } | Sort-Object)
    $p50 = if ($durs.Count) { $durs[[int][Math]::Floor($durs.Count * 0.5)] } else { 0 }
    $mx = if ($durs.Count) { $durs[-1] } else { 0 }
    $bad = @($g.Group | Where-Object { $_.outcome -and $_.outcome -ne 'ok' -and $_.outcome -ne 'skipped_no_candidates' }).Count
    $models = @($g.Group | ForEach-Object { $_.model_requested } | Where-Object { $_ } | Select-Object -Unique)
    # a requested/resolved mismatch is silent model drift — surface it, never average it away
    $drift = @($g.Group | Where-Object {
        $_.model_requested -and $_.model_resolved -and
        $_.model_resolved -ne 'unparsed' -and $_.model_requested -ne $_.model_resolved }).Count
    $byJob += [pscustomobject]@{
        job = $g.Name
        calls = $g.Count
        tokens = [int]($tok)
        per_day = [Math]::Round($g.Count / [Math]::Max(1, $Days), 1)
        p50_ms = $p50
        max_ms = $mx
        failed = $bad
        drift = $drift
        model = ($models -join ',')
    }
}
$byJob = @($byJob | Sort-Object -Property tokens -Descending)

# --- the plan window: the only figure that converts calls into headroom ----------------------
$window = [pscustomobject]@{ used_percent = $null; resets_in_days = $null; note = '' }
try {
    $auth = Get-Content -LiteralPath (Join-Path $env:USERPROFILE '.codex\auth.json') -Raw -ErrorAction Stop | ConvertFrom-Json
    $tokv = $auth.tokens.access_token
    if ([string]::IsNullOrWhiteSpace($tokv)) { throw 'no access_token' }
    $resp = Invoke-RestMethod -Uri 'https://chatgpt.com/backend-api/wham/usage' -Headers @{ Authorization = "Bearer $tokv" } -TimeoutSec 20
    $pw = $resp.rate_limit.primary_window
    $window.used_percent = [int]$pw.used_percent
    $window.resets_in_days = [Math]::Round(([double]$pw.reset_after_seconds) / 86400.0, 1)
} catch {
    # UNOFFICIAL endpoint: say it is unknown rather than invent a number.
    $window.note = "window unavailable ($($_.Exception.Message))"
}

# [int] on purpose: Measure-Object -Sum over an EMPTY set returns $null, not 0, so a box with
# no ledger yet would emit total_calls:null and every consumer would have to special-case it.
$totalTokens = [int](($byJob | Measure-Object -Property tokens -Sum).Sum)
$totalCalls = [int](($byJob | Measure-Object -Property calls -Sum).Sum)

if ($Json) {
    [pscustomobject]@{ days = $Days; jobs = $byJob; window = $window
                       total_calls = $totalCalls; total_tokens = $totalTokens } | ConvertTo-Json -Depth 5
    return
}

Write-Host ''
Write-Host "Codex judge usage - last $Days day(s)" -ForegroundColor Cyan
Write-Host ('{0,-20} {1,6} {2,8} {3,10} {4,8} {5,8} {6,6} {7,5}  {8}' -f 'job', 'calls', '/day', 'tokens', 'p50 ms', 'max ms', 'fail', 'drift', 'model')
Write-Host ('-' * 108)
foreach ($r in $byJob) {
    Write-Host ('{0,-20} {1,6} {2,8} {3,10} {4,8} {5,8} {6,6} {7,5}  {8}' -f `
        $r.job, $r.calls, $r.per_day, $r.tokens, $r.p50_ms, $r.max_ms, $r.failed, $r.drift, $r.model)
}
Write-Host ('-' * 108)
Write-Host ('{0,-20} {1,6} {2,8} {3,10}' -f 'TOTAL', $totalCalls, [Math]::Round($totalCalls / [Math]::Max(1, $Days), 1), $totalTokens)
Write-Host ''
if ($null -ne $window.used_percent) {
    Write-Host ("7-day plan window: {0}% used, resets in {1} day(s)" -f $window.used_percent, $window.resets_in_days)
} else {
    Write-Host ("7-day plan window: UNKNOWN - {0}" -f $window.note) -ForegroundColor Yellow
}
# The decision this report exists to inform. Stated as a comparison, not a verdict: the ratio is
# solid, the conversion from tokens to window percent is not published anywhere.
$l1a = @($byJob | Where-Object { $_.job -eq 'l1a' })
if ($l1a.Count -and $l1a[0].calls -gt 0) {
    Write-Host ''
    Write-Host 'NLI write-gate sizing (the gate is currently OFF):' -ForegroundColor Cyan
    Write-Host ("  the extractor runs {0} call(s)/day at {1} tokens total over {2} day(s)." -f $l1a[0].per_day, $l1a[0].tokens, $Days)
    Write-Host '  the gate would fire on EVERY mem0 write, uncached - compare its projected write'
    Write-Host '  rate against the row above before enabling it, and re-run this after a week.'
}
Write-Host ''
