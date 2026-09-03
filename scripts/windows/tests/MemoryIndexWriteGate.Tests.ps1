# MemoryIndexWriteGate.Tests.ps1 - the PostToolUse write-time gate for MEMORY.md.
# 2026-09-03: the bash advisory it replaces was ignored live; the gate must ACT at the sync limit
# and never touch doctrine. Runs the real script under powershell.exe (5.1) because that is the
# hook runtime; a PS7-only construct would pass here under pwsh and die in production.
BeforeAll {
    $script:WinDir = Split-Path -Parent $PSScriptRoot
    $script:EmDash = [string][char]0x2014
    function New-GateStore {
        param([int]$Count, [string]$Type = 'project', [int]$Repeat = 22)
        $root = Join-Path $TestDrive ([guid]::NewGuid().ToString('N').Substring(0, 8))
        $dir = Join-Path $root 'projects\ws\memory'
        [System.IO.Directory]::CreateDirectory($dir) | Out-Null
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        $lines = @('# Memory Index', '')
        for ($i = 1; $i -le $Count; $i++) {
            $lines += ('- [Fact ' + $i + '](fact' + $i + '.md) ' + $script:EmDash + ' ' + ('detail number ' + $i + ' ') * $Repeat)
            [System.IO.File]::WriteAllText((Join-Path $dir "fact$i.md"), "---`nname: fact$i`ndescription: `"d`"`nmetadata:`n  type: $Type`n---`n`nbody`n", $utf8)
        }
        [System.IO.File]::WriteAllText((Join-Path $dir 'MEMORY.md'), (($lines -join "`n") + "`n"), $utf8)
        return [pscustomobject]@{ Root = $root; Dir = $dir; Index = (Join-Path $dir 'MEMORY.md') }
    }
    function Invoke-Gate {
        param([string]$FilePath, [string]$UserHome)
        $payload = (@{ hook_event_name = 'PostToolUse'; tool_name = 'Edit'; tool_input = @{ file_path = $FilePath } } | ConvertTo-Json -Compress)
        $gate = Join-Path $script:WinDir 'memory-index-write-gate.ps1'
        $prev = $env:USERPROFILE
        try {
            $env:USERPROFILE = $UserHome
            $out = $payload | & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $gate 2>&1
        } finally { $env:USERPROFILE = $prev }
        return ($out | Out-String)
    }
    function Get-Bytes { param([string]$P) return ([System.IO.File]::ReadAllBytes($P)).Length }
}

Describe 'write-time index gate' {
    It 'is silent and writes nothing for an index under every cap' {
        $s = New-GateStore -Count 5 -Repeat 2
        $before = [System.IO.File]::ReadAllBytes($s.Index)
        $out = Invoke-Gate -FilePath $s.Index -UserHome $s.Root
        $out.Trim() | Should -BeNullOrEmpty
        [System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($s.Index)) | Should -Be ([System.BitConverter]::ToString($before))
    }

    It 'ignores files that are not a workspace MEMORY.md' {
        $s = New-GateStore -Count 80
        $other = Join-Path $s.Dir 'notes.md'
        Copy-Item $s.Index $other
        $out = Invoke-Gate -FilePath $other -UserHome $s.Root
        $out.Trim() | Should -BeNullOrEmpty
    }

    It 'advises but does NOT rewrite an index that is over the line cap yet under the sync limit' {
        $s = New-GateStore -Count 20
        (Get-Bytes $s.Index) | Should -BeLessThan 25000
        $before = [System.IO.File]::ReadAllBytes($s.Index)
        $out = Invoke-Gate -FilePath $s.Index -UserHome $s.Root
        $out | Should -Match 'index line\(s\) over 130 B'
        $out | Should -Not -Match 'NORMALIZED'
        [System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($s.Index)) | Should -Be ([System.BitConverter]::ToString($before))
    }

    It 'normalizes an index at/over the sync limit back under the trigger, receipted, and says the in-context copy is stale' {
        $s = New-GateStore -Count 80
        (Get-Bytes $s.Index) | Should -BeGreaterThan 25000
        $out = Invoke-Gate -FilePath $s.Index -UserHome $s.Root
        $out | Should -Match 'NORMALIZED'
        $out | Should -Match 'STALE'
        (Get-Bytes $s.Index) | Should -BeLessThan 20000
        $entries = @(Get-Content $s.Index | Where-Object { $_ -match '^- \[Fact \d+\]\(fact\d+\.md\)' })
        $entries.Count | Should -Be 80 -Because 'normalization shortens hooks; it never drops an entry'
        $receipt = Join-Path $s.Root '.claude\state\automemory\write-gate-receipts.jsonl'
        Test-Path $receipt | Should -BeTrue
        (Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json).converged | Should -BeTrue
    }

    It 'never truncates doctrine (feedback-typed) lines, and says so when nothing else is normalizable' {
        $s = New-GateStore -Count 80 -Type 'feedback'
        $before = [System.IO.File]::ReadAllBytes($s.Index)
        $out = Invoke-Gate -FilePath $s.Index -UserHome $s.Root
        [System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($s.Index)) | Should -Be ([System.BitConverter]::ToString($before)) -Because 'doctrine is the hard rule: never touched'
        $out | Should -Match 'doctrine'
    }
}
