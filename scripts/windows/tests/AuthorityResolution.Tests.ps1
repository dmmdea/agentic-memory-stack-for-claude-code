#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }
# AuthorityResolution.Tests.ps1 — v1.23 Phase 2 (P2-3 / P2-7, spec §7): every Windows hook
# resolves its memory authority from the per-host file ~\.mem0\authority-url, a failed hook
# post queues to the WSL Outbox (never a dead-letter file), a replica's bundle read fails over
# to the dormant local store and the [MEMORY CONTEXT] header says where the block came from,
# and nothing under scripts/travel rewrites the user-scope MEM0_URL any more.
#
# Run: pwsh -NoProfile -Command "Invoke-Pester <repo>\scripts\windows\tests\AuthorityResolution.Tests.ps1 -Output Detailed"

BeforeAll {
    $script:winDir   = Split-Path -Parent $PSScriptRoot
    $script:repoRoot = Split-Path -Parent (Split-Path -Parent $script:winDir)
    . (Join-Path $script:winDir 'user-prompt-lib.ps1')
    . (Join-Path $script:winDir 'memory-common.ps1')
    function script:Get-CodeLines {
        param([string]$Path)
        ((Get-Content $Path -Raw) -split "`r?`n" | Where-Object { $_.TrimStart() -notmatch '^#' }) -join "`n"
    }
    function script:Get-FunctionBody {
        # comment-stripped text of ONE function definition in a file (for the identical-copy pin)
        param([string]$Path, [string]$Name)
        $code = script:Get-CodeLines $Path
        $m = [regex]::Match($code, "(?ms)^function $Name \{.*?^\}")
        if (-not $m.Success) { return '' }
        return ($m.Value -replace '\s+', ' ').Trim()
    }
}

Describe 'Get-Mem0AuthorityUrl / Get-Mem0Role (per-host file first)' {
    BeforeEach {
        $script:savedProfile = $env:USERPROFILE; $script:savedUrl = $env:MEM0_URL
        $env:USERPROFILE = Join-Path $TestDrive ('p' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE '.mem0') | Out-Null
        Remove-Item Env:MEM0_URL -ErrorAction SilentlyContinue
    }
    AfterEach {
        $env:USERPROFILE = $script:savedProfile
        if ($script:savedUrl) { $env:MEM0_URL = $script:savedUrl } else { Remove-Item Env:MEM0_URL -ErrorAction SilentlyContinue }
    }

    It 'prefers ~\.mem0\authority-url over MEM0_URL, skips comments, trims the slash' {
        Set-Content -Path (Join-Path $env:USERPROFILE '.mem0\authority-url') -Value "# comment`nhttp://brain.example:18791/`n" -Encoding ASCII
        $env:MEM0_URL = 'http://env.example:18791'
        Get-Mem0AuthorityUrl | Should -Be 'http://brain.example:18791'
    }
    It 'falls back to MEM0_URL when the file is absent, and to loopback when both are' {
        $env:MEM0_URL = 'http://env.example:18791'
        Get-Mem0AuthorityUrl | Should -Be 'http://env.example:18791'
        Remove-Item Env:MEM0_URL
        Get-Mem0AuthorityUrl | Should -Be 'http://127.0.0.1:18791'
    }
    It 'ignores a file value that is not a plain http(s) URL (it reaches a command line)' {
        Set-Content -Path (Join-Path $env:USERPROFILE '.mem0\authority-url') -Value 'http://x;rm -rf /' -Encoding ASCII
        Get-Mem0AuthorityUrl | Should -Be 'http://127.0.0.1:18791'
    }
    It 'reads the role file, else brain' {
        Get-Mem0Role | Should -Be 'brain'
        Set-Content -Path (Join-Path $env:USERPROFILE '.mem0\role') -Value 'replica' -Encoding ASCII
        Get-Mem0Role | Should -Be 'replica'
    }
    It 'names a local replica as the read failover only on a replica whose authority is remote' {
        Get-Mem0BundleFailoverUrl -AuthorityUrl 'http://brain.example:18791' | Should -BeNullOrEmpty   # role brain
        Set-Content -Path (Join-Path $env:USERPROFILE '.mem0\role') -Value 'replica' -Encoding ASCII
        Get-Mem0BundleFailoverUrl -AuthorityUrl 'http://brain.example:18791' | Should -Be 'http://127.0.0.1:18791'
        Get-Mem0BundleFailoverUrl -AuthorityUrl 'http://127.0.0.1:18791' | Should -BeNullOrEmpty       # already local
    }
    It 'keeps the two library copies byte-identical (comment-stripped)' {
        foreach ($fn in 'Get-Mem0AuthorityUrl', 'Get-Mem0Role') {
            $a = script:Get-FunctionBody (Join-Path $script:winDir 'user-prompt-lib.ps1') $fn
            $b = script:Get-FunctionBody (Join-Path $script:winDir 'memory-common.ps1') $fn
            $a | Should -Not -BeNullOrEmpty -Because "$fn must exist in user-prompt-lib.ps1"
            $b | Should -Be $a -Because "$fn in memory-common.ps1 must be the same function (edit both)"
        }
    }
}

Describe '[MEMORY CONTEXT] header names its source' {
    BeforeEach {
        $script:auditPath = Join-Path $TestDrive ("bundle-admission-{0}.jsonl" -f ([guid]::NewGuid().ToString('N')))
        $script:bundle = [pscustomobject]@{
            ok = $true; checkpoint = [pscustomobject]@{ ok = $true; episode_id = 1; action = 'updated'; state = 'in_progress' }
            memories = @([pscustomobject]@{ id = 'm-1'; memory = 'evidence fact - brand match'
                                            metadata = [pscustomobject]@{ tier = 'evidence'; brand = 'ai-ecosystem' } })
            goals = @(); open_questions = @()
        }
    }
    It 'renders source=authority:<host:port> when given, and the legacy header when not' {
        $block = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath -Source 'authority:brain.example:18791'
        ($block -split "`n")[0] | Should -Be '[MEMORY CONTEXT - auto-surfaced by user-prompt-extract.ps1 v0.17 Phase 0.D source=authority:brain.example:18791]'
        $block = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath
        ($block -split "`n")[0] | Should -Be '[MEMORY CONTEXT - auto-surfaced by user-prompt-extract.ps1 v0.17 Phase 0.D]'
    }
    It 'renders source=local-replica after a failover' {
        $block = Format-MemoryContextBlock -Bundle $script:bundle -Brand 'ai-ecosystem' -AuditPath $script:auditPath -Source 'local-replica'
        ($block -split "`n")[0] | Should -Match 'source=local-replica\]$'
    }
}

Describe 'Add-Mem0Memory failure path: Outbox, not DLQ' {
    BeforeEach {
        $script:savedProfile = $env:USERPROFILE
        $env:USERPROFILE = Join-Path $TestDrive ('p' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE '.mem0'), (Join-Path $env:USERPROFILE '.claude\state') | Out-Null
        $script:StateDir = Join-Path $env:USERPROFILE '.claude\state'
        $script:OutboxPath = Join-Path $env:USERPROFILE '.mem0\outbox.jsonl'   # test seam for the \\wsl.localhost path
        Mock Get-Mem0Key { 'k' }
    }
    AfterEach { $env:USERPROFILE = $script:savedProfile; $script:OutboxPath = $null }

    It 'queues an add op in the shim record shape on a connection failure and returns $false' {
        Mock Invoke-RestMethod { throw [System.Net.WebException]::new('connect refused') }
        Add-Mem0Memory -Text 'hola' -Source 'l1a' -Metadata @{ tier = 'evidence' } | Should -Be $false
        $rec = Get-Content $script:OutboxPath | ConvertFrom-Json
        $rec.op | Should -Be 'add'
        $rec.args.text | Should -Be 'hola'
        $rec.args.infer | Should -Be $false
        $rec.args.user_id | Should -Be '__WSL_USER__'
        $rec.args.metadata.source | Should -Be 'l1a'
        $rec.args.metadata.tier | Should -Be 'evidence'
        $rec.key | Should -Match '^[0-9a-f-]{36}$'
        # pwsh 7's ConvertFrom-Json turns an ISO string into [DateTime]; assert on the raw bytes instead
        (Get-Content -Raw $script:OutboxPath) | Should -Match '"queued_ts":"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"'
        Test-Path (Join-Path $script:StateDir 'mem0-post-failures.jsonl') | Should -Be $false
    }
    It 'sends a deterministic 4xx to the poison file, never the outbox' {
        # a real HttpResponseException (what Invoke-RestMethod throws on pwsh 7): its .Response.StatusCode
        # is what Add-Mem0Memory reads; a NoteProperty on a plain exception is lost in the throw
        Mock Invoke-RestMethod { throw [Microsoft.PowerShell.Commands.HttpResponseException]::new('422', [System.Net.Http.HttpResponseMessage]::new([System.Net.HttpStatusCode]422)) }
        Add-Mem0Memory -Text 'bad' -Source 'l1a' | Should -Be $false
        Test-Path $script:OutboxPath | Should -Be $false
        (Get-Content (Join-Path $script:StateDir 'mem0-post-poison.jsonl') | ConvertFrom-Json).status_code | Should -Be 422
    }
    It 'falls back to the legacy dead-letter file (drained next run) when the Outbox itself is unwritable — never the poison file' {
        $script:OutboxPath = Join-Path $env:USERPROFILE 'no-such-dir\outbox.jsonl'   # WSL asleep / UNC unmounted
        Mock Invoke-RestMethod { throw [System.Net.WebException]::new('connect refused') }
        Add-Mem0Memory -Text 'stranded' -Source 'l1a' | Should -Be $false
        Test-Path (Join-Path $script:StateDir 'mem0-post-poison.jsonl') | Should -Be $false
        $rec = Get-Content (Join-Path $script:StateDir 'mem0-post-failures.jsonl') | ConvertFrom-Json
        $rec.text | Should -Be 'stranded'; $rec.attempts | Should -Be 1; $rec.status_code | Should -Be 0
        $rec.error | Should -Match '^outbox-unwritable:'
    }
    It 'appends, never truncates, when the outbox already holds records' {
        Set-Content -Path $script:OutboxPath -Value '{"op":"add","args":{"text":"earlier"},"key":"k0"}' -Encoding UTF8
        Mock Invoke-RestMethod { throw [System.Net.WebException]::new('timeout') }
        Add-Mem0Memory -Text 'later' -Source 'c1' | Out-Null
        @(Get-Content $script:OutboxPath).Count | Should -Be 2
    }
}

Describe 'Regression guards for the authority contract' {
    It 'no Windows hook initialises its URL from $env:MEM0_URL directly' {
        foreach ($f in 'memory-common.ps1', 'user-prompt-extract.ps1', 'mem0-hook-daemon.ps1', 'sessionstart-capture.ps1', 'user-prompt-lib.ps1') {
            $lines = script:Get-CodeLines (Join-Path $script:winDir $f)
            ($lines -split "`n" | Where-Object { $_ -match 'if\s*\(\s*\$env:MEM0_URL\s*\)\s*\{\s*\$env:MEM0_URL\s*\}' }) |
                Should -BeNullOrEmpty -Because "$f must call Get-Mem0AuthorityUrl (per-host file first)"
        }
    }
    It 'nothing under scripts/travel writes the user-scope MEM0_URL any more' {
        foreach ($f in 'travel-mode.ps1', 'offline-watcher.ps1', 'install-offline-watcher.ps1') {
            $lines = script:Get-CodeLines (Join-Path $script:repoRoot "scripts\travel\$f")
            ($lines -split "`n" | Where-Object { $_ -match "SetEnvironmentVariable\('MEM0_URL'" }) |
                Should -BeNullOrEmpty -Because "$f must leave ~\.mem0\authority-url as the truth, not the env var"
        }
    }
    It 'the installer writes the Windows-side authority-url and role files' {
        $code = script:Get-CodeLines (Join-Path $script:repoRoot 'install\2-windows-config.ps1')
        $code | Should -Match "Join-Path \`$winMem0 'authority-url'"
        $code | Should -Match "Join-Path \`$winMem0 'role'"
    }
    It '3-verify gates the brain-only WSL timer checks on the role (v1.23.1)' {
        $code = script:Get-CodeLines (Join-Path $script:repoRoot 'install\3-verify.ps1')
        $i = $code.IndexOf('Check "decay-scan.timer enabled (brain)"')
        $i | Should -BeGreaterThan 0
        $gate = $code.LastIndexOf("if (`$stackRole -eq 'brain')", $i)
        $gate | Should -BeGreaterThan 0 -Because 'the enabled-timer checks must sit inside a brain-role gate'
        $code | Should -Match 'NOT enabled \(replica, one-brain rule\)'
    }
    It 'restore-replica refuses artifacts WSL cannot read and asserts the manifest count (v1.23.1)' {
        $code = script:Get-CodeLines (Join-Path $script:repoRoot 'scripts\travel\restore-replica.ps1')
        $code | Should -Match "test -r '\`$\(\`$pair\[1\]\)'"
        $code | Should -Match 'manifest-\$Stamp\.json'
        $code | Should -Match 'replica point count .* != manifest qdrant_points'
    }
    It 'the Windows receipt records AuthoritySsh and inherits it on a re-run (v1.23.1)' {
        $code = script:Get-CodeLines (Join-Path $script:repoRoot 'install\2-windows-config.ps1')
        $code | Should -Match "AuthoritySsh = '\`$eAuthoritySsh'"
        $code | Should -Match '\(Import-PowerShellDataFile \$receiptPath\)\.AuthoritySsh'
    }
    It 'the Windows receipt records HubHost and inherits it on a re-run (P4-1a)' {
        # Same rule as AuthoritySsh: a plain re-run must not blank the store hub, or the next
        # install silently strips the sync hooks and keeps the legacy nightly.
        $code = script:Get-CodeLines (Join-Path $script:repoRoot 'install\2-windows-config.ps1')
        $code | Should -Match "HubHost     = '\`$eHubHost'"
        $code | Should -Match '\(Import-PowerShellDataFile \$receiptPath\)\.HubHost'
    }
    It 'memory-compact.ps1 posts, reads back and deletes through Get-Mem0AuthorityUrl, never loopback (v1.23.2)' {
        # The compactor runs on EVERY box (brain and replicas). Its three mem0 calls were the last
        # hard-coded loopback probes in scripts/windows: on a replica they hit the dormant local
        # store — or, during an outage, wrote migrations INTO the disposable replica.
        $code = script:Get-CodeLines (Join-Path $script:winDir 'memory-compact.ps1')
        $code | Should -Not -Match '127\.0\.0\.1:18791'
        ([regex]::Matches($code, "\(Get-Mem0AuthorityUrl\) \+ '/v1/memories")).Count | Should -Be 3
        $code | Should -Match "memory-common\.ps1" -Because 'the resolver comes from the shared lib the compactor dot-sources'
    }
    It 'the installer removes a stale loopback user-scope MEM0_URL on a replica and only there (v1.23.2)' {
        $code = script:Get-CodeLines (Join-Path $script:repoRoot 'install\2-windows-config.ps1')
        $i = $code.IndexOf("[Environment]::SetEnvironmentVariable('MEM0_URL', `$null, 'User')")
        $i | Should -BeGreaterThan 0
        $gate = $code.LastIndexOf("if (`$Role -eq 'replica')", $i)
        $gate | Should -BeGreaterThan 0 -Because 'a brain keeps whatever the operator set'
        $code.Substring($gate, $i - $gate) | Should -Match '127\\\.0\\\.0\\\.1\|localhost' -Because 'only a LOOPBACK value is residue; a remote value is a choice'
    }
    It 'Add-Mem0Memory writes the dead-letter file only on the outbox-unwritable branch' {
        $body = script:Get-FunctionBody (Join-Path $script:winDir 'memory-common.ps1') 'Add-Mem0Memory'
        $body | Should -Not -BeNullOrEmpty
        $body | Should -Match 'Add-Mem0OutboxOp'
        ([regex]::Matches($body, 'mem0-post-failures')).Count | Should -Be 1
        $body.IndexOf('mem0-post-failures') | Should -BeGreaterThan $body.IndexOf('Add-Mem0OutboxOp') -Because 'the DLQ is the fallback AFTER the Outbox write fails, never the first choice'
    }
}
