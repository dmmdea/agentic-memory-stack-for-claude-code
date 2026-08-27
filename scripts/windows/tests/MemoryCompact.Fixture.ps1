# MemoryCompact.Fixture.ps1 - shared sandbox for the compactor test files. Dot-sourced from
# each file's BeforeAll. The compactor suite is split across two files because every
# scenario spawns a child pwsh + a git repo (~7 s each) and the canonical runner bounds a
# single file at 180 s - a bound that exists to catch hung daemons and must not be loosened.
# Not a *.Tests.ps1, so Pester never discovers it directly.
    $script:WinDir = Split-Path -Parent $PSScriptRoot
    $script:EmDash = [string][char]0x2014

    function New-Sandbox {
        param(
            [string]$CodexPlanJson = '{"plan":[]}',
            [switch]$CodexThrows,
            [string]$Mem0Mode = 'ok',        # ok | noid | mismatch | fail
            [string]$MutateIndexDuringCodex  # text appended to the index while "Codex thinks" (CAS)
        )
        $root = Join-Path $TestDrive ([guid]::NewGuid().ToString('N').Substring(0, 8))
        $home_ = Join-Path $root 'home'
        $bin = Join-Path $root 'bin'
        foreach ($d in @($home_, $bin, (Join-Path $home_ '.claude\projects'), (Join-Path $home_ '.claude\state'), (Join-Path $home_ '.claude\logs'))) {
            [System.IO.Directory]::CreateDirectory($d) | Out-Null
        }
        Copy-Item (Join-Path $script:WinDir 'memory-compact.ps1') $bin
        Copy-Item (Join-Path $script:WinDir 'memory-store-lib.ps1') $bin
        $stub = @'
# stub memory-common.ps1 (test double)
$script:StubDir = Split-Path -Parent $MyInvocation.MyCommand.Path
function Initialize-MemoryEnv { }
function Write-MemoryLog { param($Component, $Message) Add-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\logs\compact-test.log') -Value $Message }
function Test-Throttle { param($Name, $MinIntervalSeconds) return $true }
function Mark-Throttle { param($Name) Set-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\throttle-marked') -Value '1' }
function Acquire-CodexLock { param($Owner, $MaxAgeMinutes) return $true }
function Release-CodexLock { }
function Get-Mem0Key { return 'test-key' }
function Invoke-CodexSubagent {
    param($Prompt, $ReasoningEffort, $TimeoutSeconds)
    Set-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\last-codex-prompt.txt') -Value $Prompt
    if ($env:STUB_CODEX_THROWS -eq '1') { throw 'codex.cmd not found (stub)' }
    if ($env:STUB_MUTATE_INDEX) {
        Add-Content -LiteralPath $env:STUB_MUTATE_TARGET -Value $env:STUB_MUTATE_INDEX
    }
    return ("header`ncodex`n" + $env:STUB_CODEX_PLAN + "`ntokens used`n42")
}
function Get-CodexResponseText {
    param($RawOutput)
    $lines = $RawOutput -split "`r?`n"
    $s = -1; $e = $lines.Length
    for ($i = $lines.Length - 1; $i -ge 0; $i--) {
        if ($lines[$i].Trim() -eq 'tokens used') { $e = $i }
        if ($lines[$i].Trim() -eq 'codex') { $s = $i + 1; break }
    }
    if ($s -lt 0) { return $RawOutput }
    return (($lines[$s..($e - 1)] -join "`n").Trim())
}
function Extract-JsonFromText {
    param($Text, $ExpectedKey)
    try { $o = $Text | ConvertFrom-Json } catch { return $null }
    if ($o.PSObject.Properties.Name -contains $ExpectedKey) { return $o }
    return $null
}
function Invoke-RestMethod {
    # Shadows the cmdlet for the compactor's own POST (Add-AmMem0Migration), the by-id
    # read-back, and the undo delete. Modes via STUB_MEM0_MODE:
    #   ok | noid (server returns no id) | mismatch (read-back differs) |
    #   dedup-mismatch (server reports deduplicated=true AND read-back differs)
    param($Uri, $Headers, $TimeoutSec, $Method, $Body, $ContentType)
    if ($Method -eq 'Post') {
        $json = [System.Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json
        Add-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\mem0-posts.txt') -Value ($json.metadata.source + '||' + $json.messages)
        if ($env:STUB_MEM0_MODE -eq 'noid') { return [pscustomobject]@{ results = @() } }
        Set-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\mem0-last.txt') -Value $json.messages
        $dedup = ($env:STUB_MEM0_MODE -eq 'dedup-mismatch')
        return [pscustomobject]@{ results = @([pscustomobject]@{ id = 'stub-id-0001' }); deduplicated = $dedup }
    }
    if ($Method -eq 'Delete') {
        Add-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\mem0-deleted.txt') -Value ([string]$Uri)
        return [pscustomobject]@{ ok = $true }
    }
    if ($env:STUB_MEM0_MODE -like '*mismatch*') { return [pscustomobject]@{ memory = 'SOMETHING ELSE'; retrievable = $true } }
    $t = Get-Content -LiteralPath (Join-Path $env:USERPROFILE '.claude\state\mem0-last.txt') -Raw
    return [pscustomobject]@{ memory = $t.TrimEnd("`r", "`n"); retrievable = $true }
}
'@
        Set-Content -LiteralPath (Join-Path $bin 'memory-common.ps1') -Value $stub -Encoding UTF8
        return [pscustomobject]@{
            Root = $root; Home = $home_; Bin = $bin
            Projects = (Join-Path $home_ '.claude\projects')
            Plan = $CodexPlanJson; Throws = [bool]$CodexThrows; Mem0 = $Mem0Mode; Mutate = $MutateIndexDuringCodex
        }
    }

    function Add-SandboxStore {
        param($Sandbox, [string]$Workspace, [string[]]$IndexLines, [hashtable]$Facts)
        $dir = Join-Path (Join-Path $Sandbox.Projects $Workspace) 'memory'
        [System.IO.Directory]::CreateDirectory($dir) | Out-Null
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText((Join-Path $dir 'MEMORY.md'), (($IndexLines -join "`n") + "`n"), $utf8)
        foreach ($k in $Facts.Keys) { [System.IO.File]::WriteAllText((Join-Path $dir $k), $Facts[$k], $utf8) }
        return $dir
    }

    function New-FactFile {
        param([string]$Name, [string]$Desc, [string]$Type = 'project', [string]$Body = 'the body of the fact')
        return ("---`nname: $Name`ndescription: `"$Desc`"`nmetadata: `n  type: $Type`n  modified: 2026-07-01`n---`n`n$Body`n")
    }

    function Invoke-Compactor {
        param($Sandbox, [string[]]$ExtraArgs = @())
        $envAssign = @(
            '$env:USERPROFILE=' + "'" + $Sandbox.Home + "'"
            '$env:STUB_CODEX_PLAN=' + "'" + ($Sandbox.Plan -replace "'", "''") + "'"
            '$env:STUB_CODEX_THROWS=' + "'" + $(if ($Sandbox.Throws) { '1' } else { '0' }) + "'"
            '$env:STUB_MEM0_MODE=' + "'" + $Sandbox.Mem0 + "'"
        )
        if ($Sandbox.Mutate) {
            $envAssign += '$env:STUB_MUTATE_INDEX=' + "'" + $Sandbox.Mutate + "'"
            $envAssign += '$env:STUB_MUTATE_TARGET=' + "'" + $Sandbox.MutateTarget + "'"
        }
        $argLine = if ($ExtraArgs.Count) { ' ' + ($ExtraArgs -join ' ') } else { '' }
        $cmd = ($envAssign -join '; ') + '; & "' + (Join-Path $Sandbox.Bin 'memory-compact.ps1') + '"' + $argLine
        $out = & pwsh -NoProfile -NonInteractive -Command $cmd 2>&1
        $receiptPath = Join-Path $Sandbox.Home '.claude\state\automemory\compact-receipts.jsonl'
        $receipts = @()
        if (Test-Path -LiteralPath $receiptPath) {
            foreach ($l in (Get-Content -LiteralPath $receiptPath)) { if ($l.Trim()) { $receipts += ($l | ConvertFrom-Json) } }
        }
        return [pscustomobject]@{ Output = ($out | Out-String); Receipts = $receipts }
    }

    function Get-Bytes { param([string]$P) return ([System.IO.File]::ReadAllBytes($P)).Length }

    # A big index: 60 long lines (over the 130 B hook cap) => over the 20,000 B trigger.
    function New-BigIndexLines {
        param([int]$Count = 60, [string]$Prefix = 'fact')
        $lines = @('# Memory Index', '')
        for ($i = 1; $i -le $Count; $i++) {
            $lines += ('- [Fact ' + $i + '](' + $Prefix + $i + '.md) ' + $script:EmDash + ' ' + ('detail number ' + $i + ' ') * 22)
        }
        return $lines
    }
