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

BeforeDiscovery {
    # The shared cross-runtime redaction fixture. ONE file, THREE suites: this one,
    # mem0-server/tests/test_redact.py, and claude-config/tests/test_precompact_capture.py.
    # That is the structural fix for AMS-12 — four copies of the pattern set were born
    # identical in a single commit with NO binding test, and had already drifted on case
    # sensitivity before anyone noticed. Cases MUST be loaded in BeforeDiscovery: `-ForEach`
    # is evaluated at discovery time, not at run time.
    #
    # Fixture conventions that matter on this side specifically:
    #   * the field is `text`, NOT `input` — `$input` is an automatic variable and `-ForEach`
    #     binding to it would silently produce nothing.
    #   * the file is ASCII-only — PS 5.1 `Get-Content` defaults to ANSI, so a non-ASCII byte
    #     would decode differently here than in the Python suites and break parity for a
    #     reason that has nothing to do with the rules.
    #   * assertions are literal substrings (`.Contains`, not `-Match`/`-BeLike`) — the markers
    #     contain `[` and `]`, which both of those would read as regex/wildcard syntax.
    #   * `|+|` is a split marker stripped at load time so realistic credential prefixes never
    #     sit contiguously on disk (an unsplit fixture is flagged by gitleaks and unpushable).
    $repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
    $fixturePath = Join-Path $repoRoot 'tests\fixtures\redaction-cases.jsonl'
    $redactionCases = @(
        foreach ($line in (Get-Content -LiteralPath $fixturePath)) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            $o = ConvertFrom-Json $line
            # Hashtable, not the PSCustomObject: hashtable keys are what -ForEach binds as
            # variables. @() around the arrays guards PowerShell's single-element unwrap.
            @{
                name        = $o.name
                text        = $o.text
                must_redact = @($o.must_redact)
                must_keep   = @($o.must_keep)
            }
        }
    )
}

Describe 'Redact-Secrets shared cross-runtime fixture (AMS-12)' {
    It 'fixture case <name>' -ForEach $redactionCases {
        $out = Redact-Secrets ($text.Replace('|+|', ''))
        foreach ($n in $must_redact) {
            $needle = $n.Replace('|+|', '')
            $out.Contains($needle) | Should -BeFalse -Because "case '$name' must redact '$needle' but got: $out"
        }
        foreach ($n in $must_keep) {
            $needle = $n.Replace('|+|', '')
            $out.Contains($needle) | Should -BeTrue -Because "case '$name' must keep '$needle' but got: $out"
        }
    }

    # A fixture that failed to load would make every -ForEach test vacuously ABSENT rather than
    # failing, so the count is asserted too. It is carried through -ForEach because discovery-time
    # variables are not guaranteed to be in scope during the run phase.
    It 'loaded a populated fixture (<caseCount> cases)' -ForEach @(@{ caseCount = @($redactionCases).Count }) {
        $caseCount | Should -BeGreaterOrEqual 20
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

    # AMS-10 (2026-08-07) regression pair: codex emits UTF-8 but PS 5.1's child
    # powershell decoded native stdout with the OEM codepage (CP437), storing
    # mojibake; the input leg turned non-ASCII prompt chars into '?'. Both fakes
    # move RAW bytes (never text) across the boundary, and every expected char is
    # assembled from [char]0xNNNN codepoints — a literal non-ASCII char in THIS
    # file would itself cross an encoding boundary and defeat the test.

    It 'returns codex UTF-8 output with non-ASCII codepoints intact (output leg)' {
        # – → é ñ ≥ assembled from codepoints, never literals.
        $chars = -join @([char]0x2013, [char]0x2192, [char]0x00E9, [char]0x00F1, [char]0x2265)
        $payload = 'UTF8_SENTINEL:' + $chars + ':END'
        $bin = Join-Path $TestDrive 'utf8-payload.bin'
        [System.IO.File]::WriteAllBytes($bin, [System.Text.Encoding]::UTF8.GetBytes($payload))
        # The fake dumps the payload's raw UTF-8 bytes to stdout via the byte
        # stream (bypassing any console text encoder), exactly like codex does.
        $fake = Join-Path $TestDrive 'codex-utf8.cmd'
        Set-Content -Path $fake -Encoding ASCII -Value @(
            '@echo off',
            'powershell -NoProfile -NonInteractive -Command "$b=[System.IO.File]::ReadAllBytes(''%~dp0utf8-payload.bin'');$s=[Console]::OpenStandardOutput();$s.Write($b,0,$b.Length);$s.Flush()"'
        )
        $script:CodexCmd = $fake
        # Simulate the PRODUCTION parent: the L1a/dream hooks run under headless
        # Windows PowerShell whose Console.OutputEncoding is the OEM codepage
        # (CP437). Without this, a UTF-8-console test harness (pwsh) masks the
        # bug — CP437 is byte-bijective, so the child's mojibake re-encodes to
        # the original bytes and the harness's UTF-8 parent decode restores the
        # string. The fixed code sets psi.StandardOutputEncoding explicitly, so
        # it must stay green no matter what the caller's console encoding is.
        $savedEnc = [Console]::OutputEncoding
        try {
            [Console]::OutputEncoding = [System.Text.Encoding]::GetEncoding(437)
            $out = Invoke-CodexSubagent -Prompt 'hi' -TimeoutSeconds 60
        } finally {
            [Console]::OutputEncoding = $savedEnc
        }
        $out.Contains($chars) | Should -BeTrue -Because 'UTF-8 codex output must survive the child-powershell boundary without OEM mojibake'
    }

    It 'delivers a non-ASCII prompt to codex stdin intact, not as ? (input leg)' {
        $eAcute = [string][char]0x00E9   # e-acute from codepoint, never a literal
        $prompt = 'acc' + $eAcute + 'nt survives'
        $cap = Join-Path $TestDrive 'stdin-capture.bin'
        # The fake copies its RAW stdin bytes to a file, then prints a sentinel so
        # the happy path (exit 0, non-empty output) is preserved.
        $fake = Join-Path $TestDrive 'codex-echo-stdin.cmd'
        Set-Content -Path $fake -Encoding ASCII -Value @(
            '@echo off',
            'powershell -NoProfile -NonInteractive -Command "$i=[Console]::OpenStandardInput();$m=New-Object System.IO.MemoryStream;$i.CopyTo($m);[System.IO.File]::WriteAllBytes(''%~dp0stdin-capture.bin'',$m.ToArray())"',
            'echo CODEX_STDIN_CAPTURED'
        )
        $script:CodexCmd = $fake
        $out = Invoke-CodexSubagent -Prompt $prompt -TimeoutSeconds 60
        $out | Should -Match 'CODEX_STDIN_CAPTURED'
        Test-Path $cap | Should -BeTrue
        $received = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($cap))
        $received.Contains('acc' + $eAcute + 'nt') | Should -BeTrue -Because 'the e-acute must reach codex stdin as UTF-8, not be flattened to ?'
        $received.Contains('acc?nt') | Should -BeFalse
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

Describe 'Codex model routing + provenance (2026-09-07)' {
    # Until this change NO call site passed a model, so every judge inherited whatever
    # ~/.codex/config.toml named. A config edit on 2026-09-07 14:42 moved the whole stack onto
    # gpt-6-astra and not one receipt recorded it.

    It 'puts -m <model> on the codex command line when -Model is given' {
        $fake = Join-Path $TestDrive 'codex-args.cmd'
        Set-Content -Path $fake -Encoding ASCII -Value @('@echo off', 'echo ARGS:%*')
        $script:CodexCmd = $fake
        $out = Invoke-CodexSubagent -Prompt 'hi' -TimeoutSeconds 30 -Model 'gpt-6-astra'
        $out | Should -Match '-m gpt-6-astra'
    }

    It 'passes NO -m at all when -Model is omitted (an empty model must not become -m "")' {
        # `codex exec -m ""` exits non-zero; the argument must be absent, not empty. This is
        # why the child builds an ARRAY instead of interpolating a string.
        $fake = Join-Path $TestDrive 'codex-args2.cmd'
        Set-Content -Path $fake -Encoding ASCII -Value @('@echo off', 'echo ARGS:%*')
        $script:CodexCmd = $fake
        $out = Invoke-CodexSubagent -Prompt 'hi' -TimeoutSeconds 30
        $out | Should -Not -Match '\-m\b'
        $out | Should -Match 'model_reasoning_effort' -Because 'the effort override is still passed'
    }

    It 'Parse-CodexHeader reads the RESOLVED model and effort from codex stdout' {
        # Verified verbatim against codex-cli 0.153.4 on 2026-09-07.
        $raw = "OpenAI Codex v0.153.4`n--------`nworkdir: D:\x`nmodel: gpt-5.6-terra`nprovider: openai`napproval: never`nreasoning effort: low`n--------`nuser`nhi`ncodex`nOK"
        $h = Parse-CodexHeader -RawOutput $raw
        $h.Model | Should -Be 'gpt-5.6-terra'
        $h.Effort | Should -Be 'low'
    }

    It 'Parse-CodexHeader reports unparsed (never empty) when the header is unreadable' {
        # "we could not tell" must never be indistinguishable from "it matched".
        foreach ($bad in @('', 'total garbage', $null)) {
            $h = Parse-CodexHeader -RawOutput $bad
            $h.Model | Should -Be 'unparsed'
            $h.Effort | Should -Be 'unparsed'
        }
    }

    It 'Get-CodexResponseText returns $null when codex emitted no assistant message' {
        # It used to return the RAW metadata header, which callers then failed to parse and
        # logged as "json parse failed" with a header preview (5 of 330 live L1a calls).
        Get-CodexResponseText -RawOutput "OpenAI Codex v0.153.4`nmodel: x`n--------" | Should -BeNullOrEmpty
        Get-CodexResponseText -RawOutput "hdr`ncodex`nANSWER`ntokens used`n5" | Should -Be 'ANSWER'
    }

    It 'Extract-JsonFromText accepts a bare top-level array, including the EMPTY one' {
        # The nightly promote-nomination phase asks for a list; dream.log recorded
        # "autopromote: bad Codex JSON (promoting nothing): []" on 2026-09-03 and 09-07.
        # `'[]' | ConvertFrom-Json` yields NOTHING in PowerShell, so the empty list - the exact
        # payload observed - is invisible to any -is [System.Array] test.
        $empty = Extract-JsonFromText -Text '[]' -ExpectedKey 'nominations'
        $empty | Should -Not -BeNullOrEmpty
        @($empty.nominations).Count | Should -Be 0
        $two = Extract-JsonFromText -Text '[{"id":1},{"id":2}]' -ExpectedKey 'nominations'
        @($two.nominations).Count | Should -Be 2
        # the object path is unchanged
        @((Extract-JsonFromText -Text '{"facts":[{"a":1}]}' -ExpectedKey 'facts').facts).Count | Should -Be 1
        Extract-JsonFromText -Text 'not json' -ExpectedKey 'facts' | Should -BeNullOrEmpty
    }

    It 'never reclaims a codex lock whose holder process is ALIVE, however old the file' {
        # Age alone used to reclaim even with the holder alive, so a long legitimate hold could
        # be stolen mid-call by a concurrent L1a run: two codex processes, one corrupted result.
        $home_ = Join-Path $TestDrive ('lk-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
        $stateDir = Join-Path $home_ '.claude\state'
        [System.IO.Directory]::CreateDirectory($stateDir) | Out-Null
        $lock = Join-Path $stateDir 'codex.lock'
        $oldUser = $env:USERPROFILE
        try {
            $env:USERPROFILE = $home_
            # A LIVE pid (this very process) and a file two hours old.
            Set-Content -LiteralPath $lock -Value ("other 2020-01-01T00:00:00Z pid=$PID") -Encoding UTF8
            (Get-Item -LiteralPath $lock).LastWriteTime = (Get-Date).AddHours(-2)
            Acquire-CodexLock -Owner 'test' -MaxAgeMinutes 30 | Should -BeFalse -Because 'the holder is alive; a 2h-old lock is a long job, not a dead one'
            Test-Path -LiteralPath $lock | Should -BeTrue -Because 'the live holder keeps its lock'

            # A DEAD pid is still reclaimed, at any age — the deadlock guard must survive.
            $deadPid = 999999
            Set-Content -LiteralPath $lock -Value ("other 2020-01-01T00:00:00Z pid=$deadPid") -Encoding UTF8
            Acquire-CodexLock -Owner 'test' -MaxAgeMinutes 30 | Should -BeTrue -Because 'a dead holder must always be reclaimable'
        } finally {
            $env:USERPROFILE = $oldUser
        }
    }
}
