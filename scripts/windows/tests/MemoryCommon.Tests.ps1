# MemoryCommon.Tests.ps1 — v0.23: coverage for memory-common.ps1 helpers that have
# no side effects at load (Get-RecentTranscriptTurns). Dot-sources the lib directly
# (Initialize-MemoryEnv is NOT called — load defines functions only).
BeforeAll {
    . (Join-Path (Split-Path -Parent $PSScriptRoot) 'memory-common.ps1')
}

Describe 'Get-RecentTranscriptTurns pathological-transcript guard (v0.23)' {
    # Regression guard for the 11-CPU-hour runaway: a 24.6 MB single-line transcript
    # fed PS 5.1 ConvertFrom-Json (O(n^2)) and pegged a core for ~11h. The per-record
    # size cap must skip oversized lines WITHOUT calling ConvertFrom-Json on them.

    It 'skips an oversized record and still returns the normal turns — fast, no hang' {
        $f = Join-Path $TestDrive 'big.jsonl'
        $giant  = '{"message":{"role":"user","content":"' + ('x' * 300000) + '"}}'
        $normal = '{"message":{"role":"assistant","content":"hello world from a normal turn"}}'
        Set-Content -Path $f -Value @($giant, $normal) -Encoding UTF8

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $out = Get-RecentTranscriptTurns -TranscriptPath $f -MaxTurns 24 -MaxChars 12000
        $sw.Stop()

        $sw.Elapsed.TotalSeconds | Should -BeLessThan 10 -Because 'the oversized record must be skipped, never parsed'
        $out | Should -Match 'hello world from a normal turn'
        $out | Should -Not -Match 'xxxxx'
    }

    It 'returns $null when the only record is oversized (no usable turns)' {
        $f = Join-Path $TestDrive 'onlybig.jsonl'
        Set-Content -Path $f -Value ('{"message":{"role":"user","content":"' + ('y' * 300000) + '"}}') -Encoding UTF8
        Get-RecentTranscriptTurns -TranscriptPath $f -MaxTurns 24 | Should -BeNullOrEmpty
    }

    It 'parses normal multi-line transcripts unchanged (role-tagged, newest-bounded)' {
        $f = Join-Path $TestDrive 'normal.jsonl'
        Set-Content -Path $f -Encoding UTF8 -Value @(
            '{"message":{"role":"user","content":"first question"}}'
            '{"message":{"role":"assistant","content":"first answer"}}'
        )
        $out = Get-RecentTranscriptTurns -TranscriptPath $f -MaxTurns 24 -MaxChars 12000
        $out | Should -Match '\[user\] first question'
        $out | Should -Match '\[assistant\] first answer'
    }
}

Describe 'Get-RecentTranscriptTurns secret redaction (security)' {
    # Credentials pasted into a session must never reach the extraction LLM (Codex) or mem0.
    # Redaction runs inside Get-RecentTranscriptTurns (the single chokepoint), so every
    # downstream consumer of the joined transcript text gets scrubbed input.

    It 'Redact-Secrets scrubs common credential shapes and keeps safe prose' {
        $out = Redact-Secrets ('deploy sk-ABCD1234567890efgh; Authorization: Bearer tok_secret_xyz; ' +
                               'api_key=supersecretvalue123; the build passes')
        $out | Should -Not -Match 'sk-ABCD1234567890efgh'
        $out | Should -Not -Match 'tok_secret_xyz'
        $out | Should -Not -Match 'supersecretvalue123'
        $out | Should -Match 'REDACTED'
        $out | Should -Match 'the build passes'
    }

    It 'redacts a secret embedded in a transcript turn before returning it' {
        $f = Join-Path $TestDrive 'secret.jsonl'
        Set-Content -Path $f -Encoding UTF8 -Value @(
            '{"message":{"role":"user","content":"my key is sk-ABCD1234567890efgh keep it safe"}}'
            '{"message":{"role":"assistant","content":"noted; the deploy is green"}}'
        )
        $out = Get-RecentTranscriptTurns -TranscriptPath $f -MaxTurns 24 -MaxChars 12000
        $out | Should -Not -Match 'sk-ABCD1234567890efgh'
        $out | Should -Match 'REDACTED_OPENAI_KEY'
        $out | Should -Match 'the deploy is green'
    }

    It 'does not over-redact benign sentences that contain trigger words' {
        # regression guard: a bare-word rule used to eat the word after token/password/secret,
        # mangling prose and (on the joined transcript) the next [role] tag.
        (Redact-Secrets 'the password reset email') | Should -Be 'the password reset email'
        (Redact-Secrets 'token bucket algorithm')   | Should -Be 'token bucket algorithm'
        (Redact-Secrets 'the secret sauce')         | Should -Be 'the secret sauce'
    }
}

Describe 'Test-IsShipLog keep/route classifier' {
    It 'routes a long dated checkpoint (>=150 chars)' {
        Test-IsShipLog ('x' * 900) | Should -Be $true
    }

    It 'routes a short-ish dated status line' {
        Test-IsShipLog 'Shipped the canonical fix and fixed surfacing on 2026-06-15, deployed to prod.' | Should -Be $true
    }

    It 'KEEPS an atomic config fact' {
        Test-IsShipLog 'APIFY_MAX_USD on Railway is set to $20' | Should -Be $false
    }

    It 'KEEPS a terse version fact' {
        Test-IsShipLog 'v0.17 final pytest result was 97 PASS and 1 SKIP' | Should -Be $false
    }

    It 'KEEPS a comma-heavy atomic (ports)' {
        Test-IsShipLog 'The reserved ports are 80, 443, 3000, 5000, 8000, 6443' | Should -Be $false
    }

    It 'KEEPS empty/whitespace' {
        Test-IsShipLog '   ' | Should -Be $false
    }

    It 'KEEPS a short dated-status line that carries a value marker (over-KEEP tie-break)' {
        Test-IsShipLog 'The prod webhook was added on 2026-01-15 at https://api.x.com/hook' | Should -BeFalse
    }

    It 'KEEPS a long credential fact with no ship-signal (value-marker beats length)' {
        Test-IsShipLog 'The Hermes OAuth client secret is X9z-kL2mPq8vRt7wNy3dBs6jFh1cAe4uGi5oUp0 and must never be rotated without updating all three callers (Brain, Zora, and the mem0 sidecar).' | Should -BeFalse
    }
    It 'KEEPS a long path fact with no ship-signal' {
        Test-IsShipLog 'C:\path\to\agentic-memory-stack-for-claude-code\scripts\windows\memory-common.ps1 is the canonical location for all shared PowerShell helpers used by the L1a and L1b extractors.' | Should -BeFalse
    }
    It 'routes a realistic long dated ship-log (status verbs + date)' {
        Test-IsShipLog 'Shipped the canonical-surfacing fix and deployed storage-cap-check.sh on 2026-06-19; verified 7 of 7 facts surface and updated Test-MemoryStack with the R-surface invariant.' | Should -BeTrue
    }
    It 'routes a long dated ship-log even though it mentions a port (ship-signal beats marker at length)' {
        Test-IsShipLog 'Deployed the API gateway and migrated all traffic on 2026-06-15; the new service binds port 8080, the old one was removed, and we verified latency across all three regions before cutover.' | Should -BeTrue
    }
}

Describe 'Split-FactsByShipLog partitioner (phase3)' {
    It 'splits a mixed array: 1 evergreen atomic -> Evergreen, 1 dated ship-log -> ShipLogs' {
        $evergreenFact = 'The reserved ports are 80, 443, 3000'
        $shipLogFact   = 'Shipped X and deployed Y on 2026-06-15, verified all tests, committed and pushed to prod across regions.'
        $result = Split-FactsByShipLog -Facts @($evergreenFact, $shipLogFact)
        $result.Evergreen.Count | Should -Be 1
        $result.ShipLogs.Count  | Should -Be 1
        $result.Evergreen[0]    | Should -Be $evergreenFact
    }

    It 'puts all entries in Evergreen when all are atomic (ShipLogs empty)' {
        $result = Split-FactsByShipLog -Facts @(
            'The reserved ports are 80, 443, 3000',
            'mem0 API endpoint is http://127.0.0.1:18791'
        )
        $result.Evergreen.Count | Should -Be 2
        $result.ShipLogs.Count  | Should -Be 0
    }

    It 'drops empty and whitespace-only entries from both buckets' {
        $result = Split-FactsByShipLog -Facts @('', '   ', 'The reserved ports are 80, 443, 3000')
        $result.Evergreen.Count | Should -Be 1
        $result.ShipLogs.Count  | Should -Be 0
    }

    It 'routes all entries to ShipLogs when every fact is a ship-log (Evergreen empty)' {
        $r = Split-FactsByShipLog -Facts @(
            'Shipped X and deployed Y on 2026-06-15, verified all tests and pushed to prod across regions.',
            'Completed the migration on 2026-06-10, removed the old service, updated all callers and docs.')
        $r.Evergreen.Count | Should -Be 0
        $r.ShipLogs.Count  | Should -Be 2
    }
}

Describe 'Drain-Mem0DeadLetter Phase-3 ship-log gate' {
    # Verifies that DLQ entries whose text is a ship-log are DROPPED (not replayed to
    # mem0) while evergreen entries are still replayed normally.
    # Strategy: override $script:StateDir to point at $TestDrive so the function reads
    # a controlled DLQ file, and Mock Add-Mem0Memory to capture calls.

    BeforeEach {
        # Redirect the DLQ file to the Pester temp dir
        $script:StateDir = $TestDrive
    }

    AfterEach {
        # Restore StateDir to the real path so other tests are not affected
        $script:StateDir = Join-Path $env:USERPROFILE '.claude\state'
    }

    It 'replays evergreen entry and drops ship-log entry; Add-Mem0Memory called once' {
        $dlqPath = Join-Path $TestDrive 'mem0-post-failures.jsonl'
        $evergreenText = 'mem0 API endpoint is http://127.0.0.1:18791'
        $shipLogText   = 'Shipped X and deployed Y on 2026-06-15, verified all tests and pushed to prod across regions.'
        $evergreenRec = @{ text = $evergreenText; source = 'l1a-extractor'; metadata = @{}; attempts = 1; error = 'timeout'; status_code = 0; timestamp = (Get-Date).ToString('o') } | ConvertTo-Json -Compress
        $shipLogRec   = @{ text = $shipLogText;   source = 'l1a-extractor'; metadata = @{}; attempts = 1; error = 'timeout'; status_code = 0; timestamp = (Get-Date).ToString('o') } | ConvertTo-Json -Compress
        Set-Content -Path $dlqPath -Value @($evergreenRec, $shipLogRec) -Encoding UTF8

        Mock Add-Mem0Memory { return 'mock-mem-id' }

        $result = Drain-Mem0DeadLetter

        # Add-Mem0Memory must be called exactly once — for the evergreen entry only
        Should -Invoke Add-Mem0Memory -Exactly 1
        $result.dropped   | Should -Be 1
        $result.drained   | Should -Be 1
        $result.remaining | Should -Be 0
    }
}

Describe 'Invoke-CodexSubagent -TimeoutSeconds enforcement (v0.27 R5)' {
    # The prior version DECLARED -TimeoutSeconds but never applied it — a hung
    # codex.cmd/node blocked the caller forever (the L1a Stop-hook extractor + the
    # dream consolidator call this DIRECTLY with no outer guard). These tests inject
    # a fake codex.cmd via $script:CodexCmd (the dot-sourced script-scope var) and
    # prove the timeout is now enforced (and the happy/error paths still hold).

    It 'returns codex output on the happy path' {
        $fake = Join-Path $TestDrive 'codex-ok.cmd'
        Set-Content -Path $fake -Encoding ASCII -Value @('@echo off', 'echo CODEX_OK_SENTINEL')
        $script:CodexCmd = $fake
        $out = Invoke-CodexSubagent -Prompt 'hi' -TimeoutSeconds 30
        $out | Should -Match 'CODEX_OK_SENTINEL'
    }

    It 'throws (does NOT hang) when codex exceeds -TimeoutSeconds, killing the tree' {
        $fake = Join-Path $TestDrive 'codex-hang.cmd'
        # ~7s of sleep via ping; the 2s timeout MUST fire first and kill the subtree.
        Set-Content -Path $fake -Encoding ASCII -Value @('@echo off', 'ping -n 8 127.0.0.1 >nul', 'echo SHOULD_NOT_REACH')
        $script:CodexCmd = $fake
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        { Invoke-CodexSubagent -Prompt 'hi' -TimeoutSeconds 2 } | Should -Throw -ExpectedMessage '*timed out*'
        $sw.Stop()
        $sw.Elapsed.TotalSeconds | Should -BeLessThan 6 -Because 'the 2s timeout must fire well before the ~7s sleep ends'
    }

    It 'throws on a non-zero codex exit (preserves error semantics)' {
        $fake = Join-Path $TestDrive 'codex-fail.cmd'
        Set-Content -Path $fake -Encoding ASCII -Value @('@echo off', 'echo boom 1>&2', 'exit /b 3')
        $script:CodexCmd = $fake
        { Invoke-CodexSubagent -Prompt 'hi' -TimeoutSeconds 30 } | Should -Throw -ExpectedMessage '*exited 3*'
    }
}

Describe 'Split-OversizeFact write-time oversize guard (MEM-10, 2026-07-03)' {
    # The prompt-side atomicity rule is the real fix; this guard is the
    # belt-and-braces: a Codex multi-topic dump (>700 chars) is split at
    # sentence boundaries BEFORE the mem0 POST, so no single record trips the
    # l10-audit OVERSIZE line (1200) or embeds many topics into one vector.

    It 'passes a normal atomic fact through untouched (single-element array)' {
        $fact = 'The mem0 fastapi server is bound to 127.0.0.1 port 18791.'
        $out = @(Split-OversizeFact -Fact $fact)
        $out.Count | Should -Be 1
        $out[0] | Should -Be $fact
    }

    It 'passes a fact at exactly the cap through untouched' {
        $fact = 'a' * 700
        $out = @(Split-OversizeFact -Fact $fact)
        $out.Count | Should -Be 1
        $out[0].Length | Should -Be 700
    }

    It 'splits an over-cap multi-sentence dump at sentence boundaries, all chunks under cap' {
        $sentences = @()
        foreach ($i in 1..24) { $sentences += "Decision $i locked the port to $((18000 + $i)) after the audit run." }
        $dump = $sentences -join ' '
        $dump.Length | Should -BeGreaterThan 700
        $out = @(Split-OversizeFact -Fact $dump)
        $out.Count | Should -BeGreaterThan 1
        foreach ($chunk in $out) {
            $chunk.Length | Should -BeLessOrEqual 700
            $chunk | Should -Match 'Decision \d+'
        }
        # no content lost: every sentence survives in some chunk
        foreach ($i in 1..24) { ($out -join ' ') | Should -Match "Decision $i " }
    }

    It 'hard-wraps a single monster sentence so no chunk can exceed the cap' {
        $monster = ('x' * 1800) + '.'
        $out = @(Split-OversizeFact -Fact $monster)
        $out.Count | Should -BeGreaterThan 1
        foreach ($chunk in $out) { $chunk.Length | Should -BeLessOrEqual 700 }
        (($out -join '').Length) | Should -Be $monster.Length
    }

    It 'honors a custom -MaxChars' {
        $out = @(Split-OversizeFact -Fact ('One sentence here. ' * 20).Trim() -MaxChars 100)
        foreach ($chunk in $out) { $chunk.Length | Should -BeLessOrEqual 100 }
    }

    It 'returns whitespace/empty input as-is (caller filters blanks)' {
        @(Split-OversizeFact -Fact '').Count | Should -Be 1
    }
}
