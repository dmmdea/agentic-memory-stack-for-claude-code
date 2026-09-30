#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# LearnRulesDrain.Tests.ps1 - WP-17: learn-rules-drain.ps1 turns the operator-correction queue
# (~/.mem0/learn-rules.jsonl) into evidence-tier memories on the authority.
#
# The authority is a MOCK: an HttpListener on an ephemeral loopback port in a background job. It
# answers every POST with a status taken from a list (last one repeats), logs each request body to a
# file, and can append a line to the queue while a POST is in flight (the capture-during-drain
# race). The drain runs in-process from a sandbox scripts dir (real memory-common.ps1 + the script
# under test + a brands.json) with USERPROFILE pointing into $TestDrive, so nothing under the real
# ~/.mem0 or ~/.claude is touched and no live authority is contacted.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester <repo>\scripts\windows\tests\LearnRulesDrain.Tests.ps1 -Output Detailed"

BeforeAll {
    $script:winDir = Split-Path -Parent $PSScriptRoot
    $script:ps51   = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

    function script:Get-FreePort {
        $tl = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $tl.Start(); $p = $tl.LocalEndpoint.Port; $tl.Stop(); return $p
    }

    function script:Start-MockAuthority {
        # -Statuses: HTTP status per request (the last repeats). -AppendOnFirst: a raw line the mock
        # appends to -QueuePath while handling the first request.
        param([int[]]$Statuses = @(200), [string]$LogPath, [string]$AppendOnFirst = '', [string]$QueuePath = '')
        $port = script:Get-FreePort
        $stop = $LogPath + '.stop'
        $job = Start-Job -ScriptBlock {
            param($port, $statuses, $log, $appendLine, $queue, $stop)
            $l = [System.Net.HttpListener]::new()
            $l.Prefixes.Add("http://127.0.0.1:$port/")
            $l.Start()
            $n = 0
            while ($true) {
                # Poll so the test teardown can end the job: a blocking GetContext() would make
                # Stop-Job wait out its two-minute grace period after every test.
                $pending = $l.GetContextAsync()
                while (-not $pending.Wait(100)) {
                    if (Test-Path -LiteralPath $stop) { $l.Stop(); return }
                }
                $ctx = $pending.Result
                $req = $ctx.Request
                $sr = New-Object System.IO.StreamReader($req.InputStream, [System.Text.Encoding]::UTF8)
                $body = $sr.ReadToEnd()
                $entry = @{ path = $req.Url.AbsolutePath; key = $req.Headers['X-API-Key']; body = $body } | ConvertTo-Json -Compress
                [System.IO.File]::AppendAllText($log, $entry + "`n")
                if ($n -eq 0 -and $appendLine) { [System.IO.File]::AppendAllText($queue, $appendLine + "`n") }
                $code = $statuses[[Math]::Min($n, $statuses.Count - 1)]
                $n++
                $payload = if ($code -ge 200 -and $code -lt 300) { '{"results":[{"id":"mem-' + $n + '"}]}' } else { '{"detail":"mock"}' }
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
                $ctx.Response.StatusCode = $code
                $ctx.Response.ContentType = 'application/json'
                $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
                $ctx.Response.Close()
            }
        } -ArgumentList $port, $Statuses, $LogPath, $AppendOnFirst, $QueuePath, $stop
        foreach ($i in 1..50) {
            try { $c = [System.Net.Sockets.TcpClient]::new('127.0.0.1', $port); $c.Close(); break } catch { Start-Sleep -Milliseconds 100 }
        }
        return [pscustomobject]@{ Port = $port; Url = "http://127.0.0.1:$port"; Job = $job; Log = $LogPath; Stop = $stop }
    }

    function script:Stop-MockAuthority {
        param($Mock)
        if ($Mock -and $Mock.Job) {
            Set-Content -LiteralPath $Mock.Stop -Value 'stop'
            [void](Wait-Job $Mock.Job -Timeout 10)
            Remove-Job $Mock.Job -Force -ErrorAction SilentlyContinue
        }
    }

    function script:Get-MockRequests {
        param($Mock)
        if (-not (Test-Path -LiteralPath $Mock.Log)) { return @() }
        return @(Get-Content -LiteralPath $Mock.Log | Where-Object { $_ } | ForEach-Object {
            $o = $_ | ConvertFrom-Json
            $o | Add-Member -NotePropertyName json -NotePropertyValue ($o.body | ConvertFrom-Json) -PassThru
        })
    }

    function script:New-DrainSandbox {
        $root = Join-Path $TestDrive ([guid]::NewGuid().ToString('N'))
        $scripts = Join-Path $root '.claude\scripts'
        foreach ($d in @($scripts, (Join-Path $root '.claude\state'), (Join-Path $root '.claude\logs'), (Join-Path $root '.mem0'))) {
            New-Item -ItemType Directory -Path $d -Force | Out-Null
        }
        Copy-Item (Join-Path $script:winDir 'memory-common.ps1') $scripts
        Copy-Item (Join-Path $script:winDir 'learn-rules-drain.ps1') $scripts
        # One path rule, plus one content-rule workspace ("mixed-root": its sessions are classified by
        # what they say) with two content rules; the C3 map shape, see docs/systems/brands.md.
        Set-Content -Path (Join-Path $scripts 'brands.json') -Encoding UTF8 -Value ('{"rules":[{"pattern":"alpha-proj","brand":"brand-a"}],' +
            '"content_rule_workspaces":["mixed-root"],' +
            '"content_rules":[{"pattern":"widget","brand":"brand-c"},{"pattern":"gadget","brand":"brand-d"}]}')
        return [pscustomobject]@{
            Root = $root; Drain = (Join-Path $scripts 'learn-rules-drain.ps1')
            Queue = (Join-Path $root '.mem0\learn-rules.jsonl'); State = (Join-Path $root '.claude\state')
        }
    }

    function script:New-QueueLine {
        param([string]$Text = 'no, that is wrong', [string]$Kind = 'correction', [string]$Status = 'pending',
              [string]$Session = 'sid-1', [string]$Brand = '', [string]$Transcript = '', [string]$Ts = '2026-09-01T10:00:00Z',
              [string]$KindField = 'kind', [hashtable]$Extra = @{})
        $o = [ordered]@{ ts = $Ts; $KindField = $Kind; session_id = $Session; brand = $Brand; initiative = ''; transcript = $Transcript; correction = $Text; status = $Status }
        foreach ($k in $Extra.Keys) { $o[$k] = $Extra[$k] }
        return ($o | ConvertTo-Json -Compress)
    }

    function script:Invoke-Drain {
        param($Sb, [string]$Url, [switch]$Force, [int]$Max = 50, [int]$CommitRetries = 0, [int]$CommitRetryMs = 0)
        $saved = $env:USERPROFILE; $savedUrl = $env:MEM0_URL
        try {
            $env:USERPROFILE = $Sb.Root
            Remove-Item Env:MEM0_URL -ErrorAction SilentlyContinue
            $args_ = @{ QueuePath = $Sb.Queue; AuthorityUrl = $Url; ApiKey = 'test-key'; Max = $Max }
            if ($Force) { $args_['Force'] = $true }
            if ($CommitRetries -gt 0) { $args_['CommitRetries'] = $CommitRetries }
            if ($CommitRetryMs -gt 0) { $args_['CommitRetryMs'] = $CommitRetryMs }
            return (& $Sb.Drain @args_)
        } finally {
            $env:USERPROFILE = $saved
            if ($savedUrl) { $env:MEM0_URL = $savedUrl }
        }
    }

    function script:Read-Queue {
        param($Sb)
        return @(Get-Content -LiteralPath $Sb.Queue | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
    }
}

Describe 'learn-rules-drain: corrections reach the authority' {
    BeforeEach { $script:sb = script:New-DrainSandbox; $script:mock = $null }
    AfterEach  { script:Stop-MockAuthority $script:mock }

    It 'posts each pending correction as an evidence-tier learn-rules memory and stamps it drained' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value @(
            (script:New-QueueLine -Text 'use bge-reranker not the embedder' -Session 'sid-a' -Brand 'brand-z')
            (script:New-QueueLine -Text 'revert that change' -Session 'sid-b')
        )
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        $r = script:Invoke-Drain $sb $mock.Url -Force

        $r.status  | Should -Be 'ok'
        $r.drained | Should -Be 2
        $reqs = script:Get-MockRequests $mock
        $reqs.Count | Should -Be 2
        $reqs[0].path | Should -Be '/v1/memories'
        $reqs[0].key  | Should -Be 'test-key'
        $j = $reqs[0].json
        $j.messages | Should -Be 'use bge-reranker not the embedder'
        $j.infer    | Should -BeFalse
        $j.metadata.tier        | Should -Be 'evidence'
        $j.metadata.source      | Should -Be 'learn-rules'
        $j.metadata.kind        | Should -Be 'correction'
        # asserted on the wire text: pwsh 7's ConvertFrom-Json would turn the value into a [datetime]
        $reqs[0].body | Should -Match '"captured_at":"2026-09-01T10:00:00Z"'
        $j.metadata.session_id  | Should -Be 'sid-a'
        $j.metadata.brand       | Should -Be 'brand-z'      # no transcript: the captured brand is kept

        $rows = script:Read-Queue $sb
        $rows.Count | Should -Be 2
        foreach ($row in $rows) { $row.status | Should -Be 'drained' }
        $rows[0].mem0_id | Should -Be 'mem-1'
        $rows[1].mem0_id | Should -Be 'mem-2'
    }

    It 'resolves the brand from the transcript path with the brand map before the captured brand' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value @(
            (script:New-QueueLine -Text 'no, wrong file' -Brand 'stale-brand' -Transcript 'C:\u\.claude\projects\D--dev-alpha-proj\s.jsonl')
            (script:New-QueueLine -Text 'undo that' -Transcript '')
        )
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        [void](script:Invoke-Drain $sb $mock.Url -Force)
        $reqs = script:Get-MockRequests $mock
        $reqs[0].json.metadata.brand | Should -Be 'brand-a'
        $reqs[1].json.metadata.PSObject.Properties.Name | Should -Not -Contain 'brand'
    }

    It 'classifies a transcript under a content-rule workspace by the correction text, as L1a does for facts' {
        # Nothing in the path routes this workspace (no path rule matches "mixed-root"), so the words
        # are all there is: the drain must hand the correction's own text to the resolver.
        $mixed = 'C:\u\.claude\projects\D--dev-mixed-root\s.jsonl'
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value @(
            (script:New-QueueLine -Text 'no, the widget total is wrong' -Brand 'stale-brand' -Transcript $mixed)
            (script:New-QueueLine -Text 'the widget and the gadget disagree' -Transcript $mixed)
            (script:New-QueueLine -Text 'undo that last edit' -Transcript $mixed)
            (script:New-QueueLine -Text 'undo that last edit too' -Brand 'kept-brand' -Transcript $mixed)
            (script:New-QueueLine -Text 'no, the widget name is wrong' -Brand 'kept-brand' -Transcript 'C:\u\.claude\projects\D--dev-alpha-proj\s.jsonl')
        )
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        [void](script:Invoke-Drain $sb $mock.Url -Force)
        $reqs = script:Get-MockRequests $mock
        $reqs.Count | Should -Be 5
        # exactly one content rule matches: its brand, over the brand captured with the line
        $reqs[0].json.metadata.brand | Should -Be 'brand-c'
        # two brands match: ambiguous, so none (never a guess between two businesses)
        $reqs[1].json.metadata.PSObject.Properties.Name | Should -Not -Contain 'brand'
        # no rule matches: none, or the brand the line was captured with when it has one
        $reqs[2].json.metadata.PSObject.Properties.Name | Should -Not -Contain 'brand'
        $reqs[3].json.metadata.brand | Should -Be 'kept-brand'
        # a path rule still wins over the words
        $reqs[4].json.metadata.brand | Should -Be 'brand-a'
    }

    It 'redacts a credential that an older, unredacted capture left in the queue before posting it' {
        $key = 's' + 'k-ABCD1234567890efghIJKL'
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value (script:New-QueueLine -Text "no, use $key for that call")
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        [void](script:Invoke-Drain $sb $mock.Url -Force)
        $reqs = script:Get-MockRequests $mock
        $reqs.Count | Should -Be 1
        $reqs[0].body | Should -Not -Match ([regex]::Escape($key))
        $reqs[0].json.messages | Should -Match 'REDACTED_OPENAI_KEY'
    }

    It 'drops test-failure lines (kind or type) without posting them' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value @(
            (script:New-QueueLine -Text 'npm test failed: 3 failing' -Kind 'test-failure')
            (script:New-QueueLine -Text 'pytest failed' -Kind 'test-failure' -KindField 'type')
            (script:New-QueueLine -Text 'no, wrong' -Kind 'correction')
        )
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        $r = script:Invoke-Drain $sb $mock.Url -Force
        $r.dropped | Should -Be 2
        $r.drained | Should -Be 1
        (script:Get-MockRequests $mock).Count | Should -Be 1
        $rows = script:Read-Queue $sb
        @($rows | Where-Object { $_.status -eq 'dropped' }).Count | Should -Be 2
        @($rows | Where-Object { $_.status -eq 'drained' }).Count | Should -Be 1
    }

    It 'posts at most 50 corrections per run and does not count dropped test failures against the cap' {
        $lines = @(1..55 | ForEach-Object { script:New-QueueLine -Text "correction number $_" -Session "s$_" })
        $lines += @(1..5 | ForEach-Object { script:New-QueueLine -Text "flaky $_" -Kind 'test-failure' })
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value $lines
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        $r = script:Invoke-Drain $sb $mock.Url -Force
        $r.drained | Should -Be 50
        $r.dropped | Should -Be 5
        (script:Get-MockRequests $mock).Count | Should -Be 50
        $rows = script:Read-Queue $sb
        @($rows | Where-Object { $_.status -eq 'pending' }).Count | Should -Be 5
        # oldest first: the 5 left over are the last five captured
        (@($rows | Where-Object { $_.status -eq 'pending' }) | ForEach-Object { $_.correction }) | Should -Contain 'correction number 55'
    }
}

Describe 'learn-rules-drain: failure handling leaves the queue intact' {
    BeforeEach { $script:sb = script:New-DrainSandbox; $script:mock = $null }
    AfterEach  { script:Stop-MockAuthority $script:mock }

    It 'leaves every line pending on 503, stops after the first failure, and retries on the next run' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value @(
            (script:New-QueueLine -Text 'first correction')
            (script:New-QueueLine -Text 'second correction')
        )
        $mock = script:Start-MockAuthority -Statuses @(503, 200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock

        $r1 = script:Invoke-Drain $sb $mock.Url
        $r1.status | Should -Be 'aborted'
        $r1.reason | Should -Be 'http-503'
        $r1.drained | Should -Be 0
        (script:Get-MockRequests $mock).Count | Should -Be 1     # did not hammer a sick authority
        @(script:Read-Queue $sb | Where-Object { $_.status -eq 'pending' }).Count | Should -Be 2

        # a failed run does not burn the 1 h throttle, so the very next run (no -Force) goes through
        $r2 = script:Invoke-Drain $sb $mock.Url
        $r2.status  | Should -Be 'ok'
        $r2.drained | Should -Be 2
        @(script:Read-Queue $sb | Where-Object { $_.status -eq 'drained' }).Count | Should -Be 2
    }

    It 'leaves lines pending when the authority is unreachable (connect failure)' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value (script:New-QueueLine -Text 'no, wrong')
        $dead = script:Get-FreePort
        $r = script:Invoke-Drain $sb "http://127.0.0.1:$dead" -Force
        $r.status | Should -Be 'aborted'
        $r.reason | Should -Be 'unreachable'
        (script:Read-Queue $sb)[0].status | Should -Be 'pending'
    }

    It 'marks a deterministic 4xx rejected so it cannot block the queue, and keeps going' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value @(
            (script:New-QueueLine -Text 'poison')
            (script:New-QueueLine -Text 'good')
        )
        $mock = script:Start-MockAuthority -Statuses @(422, 200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        $r = script:Invoke-Drain $sb $mock.Url -Force
        $r.rejected | Should -Be 1
        $r.drained  | Should -Be 1
        $rows = script:Read-Queue $sb
        $rows[0].status | Should -Be 'rejected'
        $rows[0].error  | Should -Be 'http-422'
        $rows[1].status | Should -Be 'drained'
    }

    It 'preserves a malformed line byte for byte' {
        $bad = '{"correction": "half a line'
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value @($bad, (script:New-QueueLine -Text 'good'))
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        $r = script:Invoke-Drain $sb $mock.Url -Force
        $r.malformed | Should -Be 1
        @(Get-Content -LiteralPath $sb.Queue) | Should -Contain $bad
    }
}

Describe 'learn-rules-drain: the rewrite is safe' {
    BeforeEach { $script:sb = script:New-DrainSandbox; $script:mock = $null }
    AfterEach  { script:Stop-MockAuthority $script:mock }

    It 'rewrites atomically: keeps one .bak of the previous queue and leaves no temp file' {
        $orig = @((script:New-QueueLine -Text 'first'), (script:New-QueueLine -Text 'second'))
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value $orig
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        [void](script:Invoke-Drain $sb $mock.Url -Force)
        Test-Path ($sb.Queue + '.tmp') | Should -BeFalse
        Test-Path ($sb.Queue + '.bak') | Should -BeTrue
        $bak = @(Get-Content -LiteralPath ($sb.Queue + '.bak') | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
        $bak.Count | Should -Be 2
        foreach ($b in $bak) { $b.status | Should -Be 'pending' }      # the .bak is the pre-run image
        @(Get-ChildItem -LiteralPath (Split-Path -Parent $sb.Queue) -Filter 'learn-rules.jsonl.bak*').Count | Should -Be 1
    }

    It 'carries forward a correction captured while the drain was posting' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value (script:New-QueueLine -Text 'already queued')
        $late = script:New-QueueLine -Text 'typed during the POST' -Session 'late'
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log') -AppendOnFirst $late -QueuePath $sb.Queue
        $script:mock = $mock
        [void](script:Invoke-Drain $sb $mock.Url -Force)
        $rows = script:Read-Queue $sb
        $rows.Count | Should -Be 2
        $rows[0].status | Should -Be 'drained'
        $rows[1].correction | Should -Be 'typed during the POST'
        $rows[1].status | Should -Be 'pending'
    }

    It 'skips at once, touching nothing, when another drain holds the lock' {
        $orig = @(script:New-QueueLine -Text 'first')
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value $orig
        $before = [System.IO.File]::ReadAllBytes($sb.Queue)
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        $held = New-Object System.IO.FileStream(($sb.Queue + '.lock'), [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        try {
            $r = script:Invoke-Drain $sb $mock.Url -Force
        } finally { $held.Dispose() }
        $r.status | Should -Be 'locked'
        (script:Get-MockRequests $mock).Count | Should -Be 0
        [System.IO.File]::ReadAllBytes($sb.Queue) | Should -Be $before
        Test-Path ($sb.Queue + '.bak') | Should -BeFalse
    }

    It 'prunes finished lines older than the retention window and never prunes a pending line' {
        $old = '2026-06-01T00:00:00Z'
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value @(
            (script:New-QueueLine -Text 'old drained' -Status 'drained' -Ts $old -Extra @{ resolved_at = $old; mem0_id = 'x' })
            (script:New-QueueLine -Text 'old dropped' -Kind 'test-failure' -Status 'dropped' -Ts $old -Extra @{ resolved_at = $old })
            (script:New-QueueLine -Text 'old but still pending' -Ts $old)
        )
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        $r = script:Invoke-Drain $sb $mock.Url -Force
        $r.pruned | Should -Be 2
        $rows = script:Read-Queue $sb
        $rows.Count | Should -Be 1
        $rows[0].correction | Should -Be 'old but still pending'
    }

    It 'runs at most once an hour: a second run without -Force is throttled' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value (script:New-QueueLine -Text 'one')
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        (script:Invoke-Drain $sb $mock.Url).status | Should -Be 'ok'
        Add-Content -LiteralPath $sb.Queue -Value (script:New-QueueLine -Text 'two') -Encoding UTF8
        (script:Invoke-Drain $sb $mock.Url).status | Should -Be 'throttled'
        (script:Get-MockRequests $mock).Count | Should -Be 1
    }
}

Describe 'learn-rules-drain: a failed commit never re-posts what the authority already has' {
    BeforeEach { $script:sb = script:New-DrainSandbox; $script:mock = $null; $script:held = $null }
    AfterEach  {
        if ($script:held) { $script:held.Dispose() }
        script:Stop-MockAuthority $script:mock
    }

    It 'keeps the stamped results in a journal when File.Replace keeps failing, and applies them on the next run without posting again' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value @(
            (script:New-QueueLine -Text 'first correction' -Session 'sid-1')
            (script:New-QueueLine -Text 'second correction' -Session 'sid-2')
        )
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        # A handle without delete-share on the queue (what capture's Add-Content holds while it writes):
        # File.Replace cannot swap the file for as long as it is open.
        $script:held = New-Object System.IO.FileStream($sb.Queue, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)

        $r1 = script:Invoke-Drain $sb $mock.Url -CommitRetries 3 -CommitRetryMs 20
        $r1.status | Should -Be 'aborted'
        $r1.reason | Should -Match 'commit-failed'
        (script:Get-MockRequests $mock).Count | Should -Be 2
        Test-Path ($sb.Queue + '.pending-commit') | Should -BeTrue
        Test-Path ($sb.Queue + '.tmp') | Should -BeFalse
        @(script:Read-Queue $sb | Where-Object { $_.status -eq 'pending' }).Count | Should -Be 2   # swap did not happen

        $script:held.Dispose(); $script:held = $null
        # Next run (the throttle is open again: -Force stands in for the elapsed hour).
        $r2 = script:Invoke-Drain $sb $mock.Url -Force -CommitRetries 3 -CommitRetryMs 20
        $r2.status | Should -Be 'ok'
        (script:Get-MockRequests $mock).Count | Should -Be 2       # not one extra POST
        $rows = script:Read-Queue $sb
        $rows.Count | Should -Be 2
        foreach ($row in $rows) { $row.status | Should -Be 'drained' }
        $rows[0].mem0_id | Should -Be 'mem-1'                      # the id of the first run's POST survived
        $rows[1].mem0_id | Should -Be 'mem-2'
        Test-Path ($sb.Queue + '.pending-commit') | Should -BeFalse
    }

    It 'a journaled line does not hide a correction captured after the failed run' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value (script:New-QueueLine -Text 'posted once' -Session 'sid-1')
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        $script:held = New-Object System.IO.FileStream($sb.Queue, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        [void](script:Invoke-Drain $sb $mock.Url -CommitRetries 2 -CommitRetryMs 20)
        $script:held.Dispose(); $script:held = $null
        Add-Content -LiteralPath $sb.Queue -Value (script:New-QueueLine -Text 'typed later' -Session 'sid-2') -Encoding UTF8

        $r = script:Invoke-Drain $sb $mock.Url -Force -CommitRetries 2 -CommitRetryMs 20
        $r.status | Should -Be 'ok'
        $reqs = script:Get-MockRequests $mock
        $reqs.Count | Should -Be 2
        $reqs[1].json.messages | Should -Be 'typed later'
        $rows = script:Read-Queue $sb
        $rows[0].status | Should -Be 'drained'
        $rows[0].mem0_id | Should -Be 'mem-1'
        $rows[1].status | Should -Be 'drained'
    }

    It 'retries File.Replace and commits normally when the handle is released within the retry window' {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value (script:New-QueueLine -Text 'no, wrong')
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        # Another process holds the queue open for ~1.5 s, then lets go while the drain is retrying.
        $ready = Join-Path $sb.Root 'holder.ready'
        $holder = Start-Job -ScriptBlock {
            param($q, $ready)
            $fs = New-Object System.IO.FileStream($q, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            Set-Content -LiteralPath $ready -Value 'ready'
            Start-Sleep -Milliseconds 600
            # Capture appends while the drain is stuck in its retry loop.
            Add-Content -LiteralPath $q -Value '{"ts":"2026-09-29T10:00:00Z","kind":"correction","correction":"typed during the commit","session_id":"sid-late","status":"pending"}' -Encoding UTF8
            Start-Sleep -Milliseconds 900
            $fs.Dispose()
        } -ArgumentList $sb.Queue, $ready
        try {
            foreach ($i in 1..100) { if (Test-Path -LiteralPath $ready) { break }; Start-Sleep -Milliseconds 100 }
            Test-Path -LiteralPath $ready | Should -BeTrue
            $r = script:Invoke-Drain $sb $mock.Url -Force -CommitRetries 40 -CommitRetryMs 100
        } finally { Remove-Job $holder -Force -ErrorAction SilentlyContinue }
        $r.status | Should -Be 'ok'
        $rows = script:Read-Queue $sb
        $rows.Count | Should -Be 2                                # the line appended during the retries survived the swap
        $rows[0].status | Should -Be 'drained'
        $rows[1].status | Should -Be 'pending'
        $rows[1].session_id | Should -Be 'sid-late'
        Test-Path ($sb.Queue + '.pending-commit') | Should -BeFalse
        (script:Get-MockRequests $mock).Count | Should -Be 1
    }
}

Describe 'learn-rules-drain under Windows PowerShell 5.1 (the hook host)' {
    BeforeEach { $script:sb = script:New-DrainSandbox; $script:mock = $null }
    AfterEach  { script:Stop-MockAuthority $script:mock }

    It 'drains a correction when launched as a child powershell.exe' -Skip:(-not (Test-Path "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe")) {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value (script:New-QueueLine -Text 'no, wrong')
        $mock = script:Start-MockAuthority -Statuses @(200) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        $saved = $env:USERPROFILE
        try {
            $env:USERPROFILE = $sb.Root
            & $script:ps51 -NoProfile -ExecutionPolicy Bypass -File $sb.Drain -QueuePath $sb.Queue -AuthorityUrl $mock.Url -ApiKey 'test-key' -Force *> $null
        } finally { $env:USERPROFILE = $saved }
        (script:Read-Queue $sb)[0].status | Should -Be 'drained'
    }

    It 'leaves the line pending on a 503 (HTTP status read the 5.1 way) and on an unreachable authority, without throwing' -Skip:(-not (Test-Path "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe")) {
        Set-Content -LiteralPath $sb.Queue -Encoding UTF8 -Value (script:New-QueueLine -Text 'no, wrong')
        $mock = script:Start-MockAuthority -Statuses @(503) -LogPath (Join-Path $sb.Root 'mock.log')
        $script:mock = $mock
        $saved = $env:USERPROFILE
        try {
            $env:USERPROFILE = $sb.Root
            & $script:ps51 -NoProfile -ExecutionPolicy Bypass -File $sb.Drain -QueuePath $sb.Queue -AuthorityUrl $mock.Url -ApiKey 'test-key' -Force *> $null
            $LASTEXITCODE | Should -Be 0
            (script:Read-Queue $sb)[0].status | Should -Be 'pending'
            (script:Get-MockRequests $mock).Count | Should -Be 1
            & $script:ps51 -NoProfile -ExecutionPolicy Bypass -File $sb.Drain -QueuePath $sb.Queue -AuthorityUrl "http://127.0.0.1:$(script:Get-FreePort)" -ApiKey 'test-key' -Force *> $null
            $LASTEXITCODE | Should -Be 0
            (script:Read-Queue $sb)[0].status | Should -Be 'pending'
        } finally { $env:USERPROFILE = $saved }
    }
}
