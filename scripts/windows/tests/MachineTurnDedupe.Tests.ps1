#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# MachineTurnDedupe.Tests.ps1 — C10: the [MEMORY CONTEXT] block is for HUMAN prompts, and a
# session is not shown the same memory twice.
#
# Measured before the change: the block rode 81% of human prompts AND ~91% of background
# <task-notification> turns; in long sessions 60-75% of UserPromptSubmit events are machine
# turns, and 56% of the memory lines had already been shown earlier in the same session.
#
# Contract pinned here (every path that can emit the block):
#   1. A task notification (own turn OR queued — same <task-notification> wrapper, see the
#      corpus) gets NO block. The 0.A checkpoint still fires (checkpoint-only POST, like a
#      trivial prompt); no bundle search runs.
#   2. Within one session a memory line already injected is not injected again; the goals and
#      frontier-question sections render only when their content differs from what this
#      session was last shown. R2 abstention still holds: no novel memory -> no block at all.
#   3. A compaction resets that state (PreCompact via stop-extract.ps1, backstopped by
#      SessionStart source=compact via mem0-hook-daemon-spawn.ps1), so everything re-surfaces.
#   4. Any block that IS emitted is byte-identical to what Format-MemoryContextBlock renders
#      for the same (reduced) bundle — caps, 0.30, R2 and R6 are untouched.
#
# Isolation: every Describe that reaches a default state path sandboxes $env:USERPROFILE to a
# TestDrive root and carries a positive control (the state file appearing INSIDE the sandbox).

BeforeAll {
    $script:winDir = Split-Path -Parent $PSScriptRoot
    $libPath = Join-Path $script:winDir 'user-prompt-lib.ps1'
    if (-not (Test-Path $libPath)) { throw "user-prompt-lib.ps1 not found at $libPath" }
    . $libPath
    $daemonPath = Join-Path $script:winDir 'mem0-hook-daemon.ps1'
    . $daemonPath -DefineOnly
    $script:DaemonLogPath = Join-Path $TestDrive 'hook-daemon-test.log'

    $script:corpus = (Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot 'fixtures\machine-turn-prompts.json') | ConvertFrom-Json).prompts
    $script:taskOwnTurn = ($script:corpus | Where-Object { $_.name -like 'task notification, own turn*' }).prompt
    $script:taskQueued  = ($script:corpus | Where-Object { $_.name -like 'task notification, queued behind*' }).prompt
    $script:humanPrompt = ($script:corpus | Where-Object { $_.name -eq 'human prompt (substantive)' }).prompt

    function script:FromB64([string]$s) {
        if ([string]::IsNullOrEmpty($s)) { return '' }
        return [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s))
    }

    # Real UserPromptSubmit hook-input key order (sampled fixtures). No cwd on purpose: with no
    # session sidecar a cwd makes the pipeline spawn git for the initiative.
    function script:New-HookStdin([string]$Prompt, [string]$Sid) {
        $o = [ordered]@{
            session_id      = $Sid
            transcript_path = "C:\x\agentic-memory-stack\$Sid.jsonl"
            prompt_id       = '11111111-2222-4333-8444-555555555555'
            permission_mode = 'default'
            hook_event_name = 'UserPromptSubmit'
            prompt          = $Prompt
        }
        return ($o | ConvertTo-Json -Compress)
    }

    function script:New-BundleJson {
        param([string[]]$Memories = @('alpha fact about the admission gate'),
              [string[]]$Goals = @('Ship the C10 hook change'),
              [string[]]$Questions = @('Is the goals section load-bearing?'))
        $mems = @(); $i = 0
        foreach ($m in $Memories) { $i++; $mems += [ordered]@{ id = "m$i-" + ($m -replace '\W', '').Substring(0, [Math]::Min(8, ($m -replace '\W', '').Length)); memory = $m; metadata = [ordered]@{ tier = 'evidence'; brand = $null } } }
        $gs = @(); $i = 0
        foreach ($g in $Goals) { $i++; $gs += [ordered]@{ id = $i; title = $g; priority = 2; status = 'open'; brand = $null } }
        $qs = @(); $i = 0
        foreach ($q in $Questions) { $i++; $qs += [ordered]@{ id = 100 + $i; question_text = $q; status = 'open'; brand = $null } }
        return ([ordered]@{ ok = $true; checkpoint = [ordered]@{ ok = $true; episode_id = 9; action = 'updated' }; memories = $mems; goals = $gs; open_questions = $qs } | ConvertTo-Json -Compress -Depth 6)
    }
}

Describe 'Test-MachineTurnPrompt (lib gate, shared corpus)' {
    It '<name> -> machine_turn=<machine_turn>' -ForEach @((Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot 'fixtures\machine-turn-prompts.json') | ConvertFrom-Json).prompts | ForEach-Object { @{ name = $_.name; prompt = [string]$_.prompt; machine_turn = [bool]$_.machine_turn } }) {
        Test-MachineTurnPrompt -Prompt $prompt | Should -Be $machine_turn
    }

    It 'a $null prompt is not a machine turn (fail toward the human path)' {
        Test-MachineTurnPrompt -Prompt $null | Should -BeFalse
    }
}

Describe 'Daemon raw pipeline: machine turns get no block, the checkpoint still fires' {

    BeforeEach {
        $script:LibHash = 'f' * 64
        $script:BaseUrl = 'http://127.0.0.1:1'
        $script:HookContractVersion = '20.0'
        $script:sandboxHome = Join-Path $TestDrive ("home-{0}" -f ([guid]::NewGuid().ToString('N')))
        $script:stateDir = Join-Path $script:sandboxHome '.claude\state'
        $script:fixDir = Join-Path $script:stateDir 'hook-fixtures'
        New-Item -ItemType Directory -Path $script:stateDir -Force | Out-Null
        $script:savedUserProfile = $env:USERPROFILE
        $env:USERPROFILE = $script:sandboxHome
        $script:sid = [guid]::NewGuid().ToString()
    }
    AfterEach { if ($null -ne $script:savedUserProfile) { $env:USERPROFILE = $script:savedUserProfile } }

    It 'task notification (own turn): no block, checkpoint-only POST, no bundle search' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { if ($Uri -like '*context/bundle') { $bundle } else { '{"ok":true,"episode_id":9,"action":"updated"}' } }
        $r = Invoke-DaemonRawBundle -RawStdin (New-HookStdin $script:taskOwnTurn $script:sid) -StateDir $script:stateDir -FixtureDir $script:fixDir
        $r.served | Should -BeTrue
        $r.context_b64 | Should -Be ''
        (FromB64 $r.diag_b64) | Should -Match 'checkpoint-only \(machine-turn\)'
        Should -Invoke Invoke-Mem0Post -Times 0 -ParameterFilter { $Uri -like '*context/bundle' }
        Should -Invoke Invoke-Mem0Post -Times 1 -Exactly -ParameterFilter { $Uri -like '*episodes/checkpoint' }
    }

    It 'queued task notification: same wrapper, same verdict (no block, checkpoint-only)' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { if ($Uri -like '*context/bundle') { $bundle } else { '{"ok":true,"episode_id":9,"action":"updated"}' } }
        $r = Invoke-DaemonRawBundle -RawStdin (New-HookStdin $script:taskQueued $script:sid) -StateDir $script:stateDir -FixtureDir $script:fixDir
        $r.context_b64 | Should -Be ''
        (FromB64 $r.diag_b64) | Should -Match 'checkpoint-only \(machine-turn\)'
        Should -Invoke Invoke-Mem0Post -Times 0 -ParameterFilter { $Uri -like '*context/bundle' }
    }

    It 'human-prompt control: the block still renders from the bundle' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { $bundle }
        $r = Invoke-DaemonRawBundle -RawStdin (New-HookStdin $script:humanPrompt $script:sid) -StateDir $script:stateDir -FixtureDir $script:fixDir
        $block = FromB64 $r.context_b64
        $block | Should -Match '^\[MEMORY CONTEXT - auto-surfaced by user-prompt-extract\.ps1 v0\.17 Phase 0\.D'
        $block | Should -Match 'alpha fact about the admission gate'
        $block | Should -Match 'Open goals \(1 shown\):'
        Should -Invoke Invoke-Mem0Post -Times 1 -Exactly -ParameterFilter { $Uri -like '*context/bundle' }
    }

    It 'a machine turn does not burn the surfacing cooldown: the next human prompt still gets its block' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { if ($Uri -like '*context/bundle') { $bundle } else { '{"ok":true,"episode_id":9,"action":"updated"}' } }
        $null = Invoke-DaemonRawBundle -RawStdin (New-HookStdin $script:taskOwnTurn $script:sid) -StateDir $script:stateDir -FixtureDir $script:fixDir
        Test-Path (Join-Path $script:stateDir "user-prompt-rate-limit-$($script:sid)") | Should -BeFalse
        $r = Invoke-DaemonRawBundle -RawStdin (New-HookStdin $script:humanPrompt $script:sid) -StateDir $script:stateDir -FixtureDir $script:fixDir
        (FromB64 $r.context_b64) | Should -Match 'alpha fact about the admission gate'
    }

    It 'legacy op=bundle: a machine-turn prompt renders nothing even if the server returned sections' {
        Mock Get-Mem0ApiKeyCached { 'k' }
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { $bundle }
        $req = [pscustomobject]@{ op = 'bundle'; session_id = $script:sid; prompt = $script:taskOwnTurn; brand = $null
                                  workspace = 'ai-ecosystem'; project = $null; transcript_path = $null; hook_contract_version = '20.0' }
        $r = Invoke-DaemonRequest -Req $req
        $r.ok | Should -BeTrue
        $r.context_block | Should -BeNullOrEmpty
    }
}

Describe 'In-session dedupe (daemon path) and the compaction reset' {

    BeforeEach {
        $script:LibHash = 'f' * 64
        $script:BaseUrl = 'http://127.0.0.1:1'
        $script:HookContractVersion = '20.0'
        $script:sandboxHome = Join-Path $TestDrive ("home-{0}" -f ([guid]::NewGuid().ToString('N')))
        $script:stateDir = Join-Path $script:sandboxHome '.claude\state'
        $script:fixDir = Join-Path $script:stateDir 'hook-fixtures'
        New-Item -ItemType Directory -Path $script:stateDir -Force | Out-Null
        $script:savedUserProfile = $env:USERPROFILE
        $env:USERPROFILE = $script:sandboxHome
        $script:sid = [guid]::NewGuid().ToString()
        Mock Get-Mem0ApiKeyCached { 'k' }

        # One substantive prompt through the real raw pipeline. The cooldown token is removed
        # first so a suppressed block can only come from the dedupe, never from the 1s rate
        # limit (each test also asserts the bundle POST happened).
        function script:Invoke-Prompt([string]$Sid = $script:sid, [string]$Prompt = $script:humanPrompt) {
            Remove-Item -LiteralPath (Join-Path $script:stateDir "user-prompt-rate-limit-$Sid") -ErrorAction SilentlyContinue
            $r = Invoke-DaemonRawBundle -RawStdin (New-HookStdin $Prompt $Sid) -StateDir $script:stateDir -FixtureDir $script:fixDir
            return (FromB64 $r.context_b64)
        }

        # Copy a production hook script beside the lib into a sandbox scripts dir and run it
        # under Windows PowerShell 5.1 (the production runtime) with the sandboxed profile.
        function script:Invoke-HookScript([string]$Name, [string]$Stdin, [hashtable]$Stubs = @{}) {
            $dir = Join-Path $script:sandboxHome ("scripts-{0}" -f ([guid]::NewGuid().ToString('N')))
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
            Copy-Item (Join-Path $script:winDir $Name) $dir
            Copy-Item (Join-Path $script:winDir 'user-prompt-lib.ps1') $dir
            foreach ($k in $Stubs.Keys) { Set-Content -Path (Join-Path $dir $k) -Value $Stubs[$k] -NoNewline }
            $psi = [System.Diagnostics.ProcessStartInfo]::new()
            $psi.FileName = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
            $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $dir $Name) + '"'
            $psi.UseShellExecute = $false
            $psi.RedirectStandardInput = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.EnvironmentVariables['USERPROFILE'] = $script:sandboxHome
            $psi.EnvironmentVariables['MEM0_HOOK_PIPE'] = 'c10-test-no-such-pipe'
            $p = [System.Diagnostics.Process]::Start($psi)
            $p.StandardInput.Write($Stdin)
            $p.StandardInput.Close()
            $null = $p.StandardOutput.ReadToEnd()
            $null = $p.StandardError.ReadToEnd()
            if (-not $p.WaitForExit(60000)) { try { $p.Kill() } catch {}; throw "$Name did not exit" }
            return $p.ExitCode
        }
    }
    AfterEach { if ($null -ne $script:savedUserProfile) { $env:USERPROFILE = $script:savedUserProfile } }

    It 'same-session repeat: the second prompt with the same memories and goals gets NO block (R2: nothing novel)' {
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { $bundle }
        $first = Invoke-Prompt
        $first | Should -Match 'alpha fact about the admission gate'
        $second = Invoke-Prompt
        $second | Should -BeNullOrEmpty
        Should -Invoke Invoke-Mem0Post -Times 2 -Exactly -ParameterFilter { $Uri -like '*context/bundle' }
        # positive control: the session state lives INSIDE the sandbox
        Test-Path (Join-Path $script:stateDir "mem0-injected-$($script:sid).json") | Should -BeTrue
    }

    It 'a novel memory still surfaces, alone; the unchanged goals/questions sections are omitted' {
        $script:bundleJson = New-BundleJson
        Mock Invoke-Mem0Post { $script:bundleJson }
        $null = Invoke-Prompt
        $script:bundleJson = New-BundleJson -Memories @('beta fact about the deploy gate', 'alpha fact about the admission gate')
        $second = Invoke-Prompt
        $second | Should -Match 'Top 1 relevant memories:'
        $second | Should -Match 'beta fact about the deploy gate'
        $second | Should -Not -Match 'alpha fact'
        $second | Should -Not -Match 'Open goals'
        $second | Should -Not -Match 'Open frontier questions'
    }

    It 'changed goals re-render; unchanged questions stay omitted' {
        $script:bundleJson = New-BundleJson
        Mock Invoke-Mem0Post { $script:bundleJson }
        $null = Invoke-Prompt
        $script:bundleJson = New-BundleJson -Memories @('gamma fact about the rollout') -Goals @('Ship the C10 hook change', 'Measure the injection rate')
        $second = Invoke-Prompt
        $second | Should -Match 'Open goals \(2 shown\):'
        $second | Should -Match 'Measure the injection rate'
        $second | Should -Not -Match 'Open frontier questions'
    }

    It 'another session is independent: it gets the full block' {
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { $bundle }
        $null = Invoke-Prompt
        $other = Invoke-Prompt -Sid ([guid]::NewGuid().ToString())
        $other | Should -Match 'alpha fact about the admission gate'
        $other | Should -Match 'Open goals'
    }

    It 'post-compaction (PreCompact via the real stop-extract.ps1): everything re-surfaces' {
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { $bundle }
        $null = Invoke-Prompt
        (Invoke-Prompt) | Should -BeNullOrEmpty
        $pre = ([ordered]@{ session_id = $script:sid; transcript_path = "C:\x\agentic-memory-stack\$($script:sid).jsonl"; hook_event_name = 'PreCompact'; trigger = 'auto' } | ConvertTo-Json -Compress)
        $code = Invoke-HookScript -Name 'stop-extract.ps1' -Stdin $pre -Stubs @{ 'l1a-extract.ps1' = 'exit 0' }
        $code | Should -Be 0
        Test-Path (Join-Path $script:stateDir "mem0-injected-$($script:sid).json") | Should -BeFalse
        $after = Invoke-Prompt
        $after | Should -Match 'alpha fact about the admission gate'
        $after | Should -Match 'Open goals \(1 shown\):'
        $after | Should -Match 'Open frontier questions:'
    }

    It 'post-compaction (SessionStart source=<source> via the real mem0-hook-daemon-spawn.ps1): everything re-surfaces' -ForEach @(@{ source = 'compact' }, @{ source = 'clear' }) {
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { $bundle }
        $null = Invoke-Prompt
        (Invoke-Prompt) | Should -BeNullOrEmpty
        $ss = ([ordered]@{ session_id = $script:sid; transcript_path = "C:\x\agentic-memory-stack\$($script:sid).jsonl"; hook_event_name = 'SessionStart'; source = $source; model = 'claude-opus-5-5' } | ConvertTo-Json -Compress)
        $code = Invoke-HookScript -Name 'mem0-hook-daemon-spawn.ps1' -Stdin $ss -Stubs @{ 'mem0-hook-daemon.ps1' = 'exit 0' }
        $code | Should -Be 0
        $after = Invoke-Prompt
        $after | Should -Match 'alpha fact about the admission gate'
        $after | Should -Match 'Open goals \(1 shown\):'
    }

    It 'control: SessionStart source=resume does NOT reset (the resumed context still holds the block)' {
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { $bundle }
        $null = Invoke-Prompt
        $ss = ([ordered]@{ session_id = $script:sid; transcript_path = "C:\x\agentic-memory-stack\$($script:sid).jsonl"; hook_event_name = 'SessionStart'; source = 'resume'; model = 'claude-opus-5-5' } | ConvertTo-Json -Compress)
        $null = Invoke-HookScript -Name 'mem0-hook-daemon-spawn.ps1' -Stdin $ss -Stubs @{ 'mem0-hook-daemon.ps1' = 'exit 0' }
        (Invoke-Prompt) | Should -BeNullOrEmpty
    }

    It 'a corrupt state file fails OPEN to the full block (never suppresses on unreadable state)' {
        $bundle = New-BundleJson
        Mock Invoke-Mem0Post { $bundle }
        Set-Content -Path (Join-Path $script:stateDir "mem0-injected-$($script:sid).json") -Value 'not json {{' -NoNewline
        (Invoke-Prompt) | Should -Match 'alpha fact about the admission gate'
    }
}

Describe 'Render parity: an emitted block is byte-identical to the pre-C10 renderer' {

    BeforeEach {
        $script:stateDir = Join-Path $TestDrive ("state-{0}" -f ([guid]::NewGuid().ToString('N')))
        New-Item -ItemType Directory -Path $script:stateDir -Force | Out-Null
        $script:auditPath = Join-Path $TestDrive ("audit-{0}.jsonl" -f ([guid]::NewGuid().ToString('N')))
        $script:sid = [guid]::NewGuid().ToString()
    }

    It 'first prompt of a session: Format-SessionMemoryContextBlock == Format-MemoryContextBlock, byte for byte (<tier>)' -ForEach @(@{ tier = 'frontier' }, @{ tier = 'small' }) {
        $json = New-BundleJson -Memories @('alpha fact', 'beta fact')
        $expected = Format-MemoryContextBlock -Bundle ($json | ConvertFrom-Json) -Brand 'ai-ecosystem' -Tier $tier -AuditPath $script:auditPath -Source 'authority:example:18791'
        $actual = Format-SessionMemoryContextBlock -Bundle ($json | ConvertFrom-Json) -Brand 'ai-ecosystem' -Tier $tier -AuditPath $script:auditPath -Source 'authority:example:18791' -SessionId $script:sid -StateDir $script:stateDir
        $actual | Should -Not -BeNullOrEmpty
        $actual | Should -BeExactly $expected
    }

    It 'a deduped block == the pre-C10 render of the bundle minus what was already shown' {
        $null = Format-SessionMemoryContextBlock -Bundle (New-BundleJson -Memories @('alpha fact') | ConvertFrom-Json) -Brand 'ai-ecosystem' -AuditPath $script:auditPath -SessionId $script:sid -StateDir $script:stateDir
        $actual = Format-SessionMemoryContextBlock -Bundle (New-BundleJson -Memories @('beta fact', 'alpha fact') | ConvertFrom-Json) -Brand 'ai-ecosystem' -AuditPath $script:auditPath -SessionId $script:sid -StateDir $script:stateDir
        $reduced = New-BundleJson -Memories @('beta fact') -Goals @() -Questions @() | ConvertFrom-Json
        $expected = Format-MemoryContextBlock -Bundle $reduced -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        $actual | Should -BeExactly $expected
    }
}

Describe 'Inline PowerShell fallback path end to end (real user-prompt-extract.ps1 -SkipDaemon under powershell.exe 5.1)' {

    BeforeAll {
        # Minimal loopback HTTP responder (TcpListener: no URL ACL needed). Records each request
        # (path + body) as one JSON line and answers the bundle or the checkpoint route.
        function script:Start-FakeMem0([string]$RecordPath, [string]$BundleJson) {
            $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
            $listener.Start()
            $ps = [powershell]::Create()
            [void]$ps.AddScript({
                param($listener, $RecordPath, $BundleJson)
                while ($true) {
                    $client = $null
                    try { $client = $listener.AcceptTcpClient() } catch { break }
                    try {
                        $s = $client.GetStream()
                        $s.ReadTimeout = 5000
                        $hdr = [System.Collections.Generic.List[byte]]::new()
                        while ($true) {
                            $b = $s.ReadByte(); if ($b -lt 0) { break }
                            $hdr.Add([byte]$b)
                            $n = $hdr.Count
                            if ($n -ge 4 -and $hdr[$n-4] -eq 13 -and $hdr[$n-3] -eq 10 -and $hdr[$n-2] -eq 13 -and $hdr[$n-1] -eq 10) { break }
                        }
                        $head = [System.Text.Encoding]::ASCII.GetString($hdr.ToArray())
                        $path = ($head -split ' ')[1]
                        $len = 0
                        if ($head -match '(?im)^Content-Length:\s*(\d+)') { $len = [int]$Matches[1] }
                        $body = [byte[]]::new($len); $read = 0
                        while ($read -lt $len) { $k = $s.Read($body, $read, $len - $read); if ($k -le 0) { break }; $read += $k }
                        $bodyText = [System.Text.Encoding]::UTF8.GetString($body, 0, $read)
                        [System.IO.File]::AppendAllText($RecordPath, (@{ path = $path; body = $bodyText } | ConvertTo-Json -Compress) + "`n")
                        $payload = if ($path -like '*context/bundle') { $BundleJson } else { '{"ok":true,"episode_id":9,"action":"updated"}' }
                        $pb = [System.Text.Encoding]::UTF8.GetBytes($payload)
                        $rh = [System.Text.Encoding]::ASCII.GetBytes("HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nContent-Length: $($pb.Length)`r`nConnection: close`r`n`r`n")
                        $s.Write($rh, 0, $rh.Length); $s.Write($pb, 0, $pb.Length); $s.Flush()
                    } catch {} finally { try { $client.Close() } catch {} }
                }
            }).AddArgument($listener).AddArgument($RecordPath).AddArgument($BundleJson)
            $handle = $ps.BeginInvoke()
            return @{ Listener = $listener; PS = $ps; Handle = $handle; Port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
        }
        function script:Stop-FakeMem0($Fake) {
            try { $Fake.Listener.Stop() } catch {}
            try { $Fake.PS.Stop() } catch {}
            try { $Fake.PS.Dispose() } catch {}
        }
        function script:Get-FakeRequests([string]$RecordPath) {
            if (-not (Test-Path $RecordPath)) { return @() }
            return @(Get-Content $RecordPath | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
        }
    }

    BeforeEach {
        $script:sandboxHome = Join-Path $TestDrive ("inline-{0}" -f ([guid]::NewGuid().ToString('N')))
        $mem0Dir = Join-Path $script:sandboxHome '.mem0'
        New-Item -ItemType Directory -Path $mem0Dir -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $script:sandboxHome '.claude\state') -Force | Out-Null
        # API key cache with the owner-only ACL the lib trusts, so no UNC read is attempted.
        $cache = Join-Path $mem0Dir 'api-key.cache'
        Set-Content -Path $cache -Value 'pester-not-a-real-key' -NoNewline
        $acl = [System.Security.AccessControl.FileSecurity]::new()
        $acl.SetAccessRuleProtection($true, $false)
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new([System.Security.Principal.WindowsIdentity]::GetCurrent().User, 'FullControl', 'Allow'))
        Set-Acl -Path $cache -AclObject $acl
        $script:recordPath = Join-Path $script:sandboxHome 'requests.jsonl'
        $script:fake = Start-FakeMem0 -RecordPath $script:recordPath -BundleJson (New-BundleJson)
        Set-Content -Path (Join-Path $mem0Dir 'authority-url') -Value ("http://127.0.0.1:{0}" -f $script:fake.Port) -NoNewline
        $script:sid = [guid]::NewGuid().ToString()

        function script:Invoke-Inline([string]$Prompt) {
            Remove-Item -LiteralPath (Join-Path $script:sandboxHome ".claude\state\user-prompt-rate-limit-$($script:sid)") -ErrorAction SilentlyContinue
            $psi = [System.Diagnostics.ProcessStartInfo]::new()
            $psi.FileName = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
            $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $script:winDir 'user-prompt-extract.ps1') + '" -SkipDaemon'
            $psi.UseShellExecute = $false
            $psi.RedirectStandardInput = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.EnvironmentVariables['USERPROFILE'] = $script:sandboxHome
            $psi.EnvironmentVariables['MEM0_WSL_DISTRO'] = 'c10-pester-no-such-distro'
            $psi.EnvironmentVariables.Remove('MEM0_URL')
            $p = [System.Diagnostics.Process]::Start($psi)
            $bytes = [System.Text.Encoding]::UTF8.GetBytes((New-HookStdin $Prompt $script:sid))
            $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
            $p.StandardInput.Close()
            $out = $p.StandardOutput.ReadToEnd()
            $null = $p.StandardError.ReadToEnd()
            if (-not $p.WaitForExit(60000)) { try { $p.Kill() } catch {}; throw 'user-prompt-extract.ps1 did not exit' }
            $p.ExitCode | Should -Be 0
            return $out
        }
    }
    AfterEach { Stop-FakeMem0 $script:fake }

    It 'task notification: no stdout at all, and the server saw a checkpoint, not a bundle search' {
        $out = Invoke-Inline $script:taskOwnTurn
        $out | Should -BeNullOrEmpty
        $reqs = Get-FakeRequests $script:recordPath
        @($reqs | Where-Object { $_.path -like '*context/bundle' }).Count | Should -Be 0
        @($reqs | Where-Object { $_.path -like '*episodes/checkpoint' }).Count | Should -Be 1
    }

    It 'queued task notification: no stdout' {
        (Invoke-Inline $script:taskQueued) | Should -BeNullOrEmpty
        @((Get-FakeRequests $script:recordPath) | Where-Object { $_.path -like '*context/bundle' }).Count | Should -Be 0
    }

    It 'human-prompt control: the block reaches stdout (positive control for the whole harness)' {
        $out = Invoke-Inline $script:humanPrompt
        $out | Should -Match '\[MEMORY CONTEXT - auto-surfaced by user-prompt-extract\.ps1 v0\.17 Phase 0\.D'
        $out | Should -Match 'alpha fact about the admission gate'
    }

    It 'same-session repeat on the inline path: the second prompt prints nothing, a bundle POST still ran' {
        (Invoke-Inline $script:humanPrompt) | Should -Match 'alpha fact'
        (Invoke-Inline $script:humanPrompt) | Should -BeNullOrEmpty
        @((Get-FakeRequests $script:recordPath) | Where-Object { $_.path -like '*context/bundle' }).Count | Should -Be 2
        Test-Path (Join-Path $script:sandboxHome ".claude\state\mem0-injected-$($script:sid).json") | Should -BeTrue
    }
}
