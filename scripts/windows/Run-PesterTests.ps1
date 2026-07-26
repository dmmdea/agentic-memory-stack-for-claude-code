#Requires -Version 7.0
<#
.SYNOPSIS
  Canonical Pester runner for agentic-memory-stack. Guarantees the CORRECT Pester
  (5.x, NOT the stale system Pester 3.4.0) is used, dodges OneDrive/Defender DLL
  locks, AND runs the suite deterministically.

  Two machine-specific failure modes this fixes:
    1. Pester 3.4.0 (C:\Program Files\WindowsPowerShell\Modules) shadows 5.x on a bare
       Invoke-Pester -> "Should operator 'Be' is not registered".
    2. Pester 5.x under a cloud-synced Documents\...\Modules path gets its Pester.dll locked by
       OneDrive sync / Defender scan / another loaded session -> import fails
       ("The process cannot access the file ... used by another process").
  Fix: keep a repo-local copy of Pester 5.x under .tools\PSModules (gitignored, OUTSIDE
  any synced folder), fetched fresh from PSGallery, imported by EXPLICIT PATH + fixed
  version. No admin, no PSModulePath ambiguity, no OneDrive.

  Plus a third, runtime failure mode:
    3. Some tests spin up a DETACHED daemon / named-pipe server (mem0-hook-daemon.ps1).
       If one leaks, it blocks or contaminates the NEXT test file in a single shared
       process -> the full suite hangs indefinitely after HookDaemon. So in directory
       mode each *.Tests.ps1 runs in its OWN child pwsh, with stray test-daemons killed
       between files and a per-file timeout. Every file is hermetic.

.EXAMPLE
  pwsh -NoProfile -File scripts\windows\Run-PesterTests.ps1            # isolated full suite
  pwsh -NoProfile -File scripts\windows\Run-PesterTests.ps1 -Path scripts\windows\tests\UserPromptExtract.Tests.ps1 -Detailed
#>
[CmdletBinding()]
param(
    [string]$Path,
    [string]$PesterVersion = '5.7.1',
    [switch]$Detailed,
    [switch]$Refresh,            # force re-stage the repo-local Pester copy
    [int]$PerFileTimeoutSec = 180
)
$ErrorActionPreference = 'Stop'

$repoRoot  = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $Path) { $Path = Join-Path $PSScriptRoot 'tests' }
$toolsRoot = Join-Path $repoRoot '.tools\PSModules'
$pesterDir = Join-Path $toolsRoot "Pester\$PesterVersion"
$psd1      = Join-Path $pesterDir 'Pester.psd1'

function Find-LocalPesterSource {
    param([string]$Version)
    Get-Module -ListAvailable Pester |
        Where-Object { $_.Version -eq [version]$Version } |
        Select-Object -First 1 -ExpandProperty ModuleBase
}

function Stop-StrayTestDaemons {
    # Kill ONLY test-spawned daemons (cmdline points at a Temp/TestDrive/Pester path),
    # never the real deployed daemon under ~\.claude\scripts.
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'mem0-hook-daemon' -and $_.CommandLine -match '(?i)\\Temp\\|TestDrive|Pester' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# --- stage Pester (repo-local, non-OneDrive) ---
if ($Refresh) { Remove-Item $pesterDir -Recurse -Force -ErrorAction SilentlyContinue }
if (-not (Test-Path $psd1)) {
    New-Item -ItemType Directory -Force $toolsRoot | Out-Null
    $staged = $false
    try {
        Write-Host "Staging Pester $PesterVersion from PSGallery -> $toolsRoot"
        Save-Module -Name Pester -RequiredVersion $PesterVersion -Path $toolsRoot -Force -Confirm:$false -ErrorAction Stop
        $staged = Test-Path $psd1
    } catch {
        Write-Host "PSGallery unavailable ($($_.Exception.Message)); trying a local Pester copy"
    }
    if (-not $staged) {
        $src = Find-LocalPesterSource -Version $PesterVersion
        if (-not $src) { throw "Pester $PesterVersion unavailable: not on PSGallery and no local copy found" }
        New-Item -ItemType Directory -Force $pesterDir | Out-Null
        robocopy $src $pesterDir /E /R:3 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { $global:LASTEXITCODE = 0; throw "robocopy fallback failed (source locked: $src)" }
        $global:LASTEXITCODE = 0
    }
}
if (-not (Test-Path $psd1)) { throw "Pester $PesterVersion not available at $psd1 after staging" }

# --- single-file mode: import + run (this is what each isolated child does) ---
if (-not (Test-Path $Path -PathType Container)) {
    Import-Module $psd1   # explicit path, no -Force (no Add-Type recompile/lock)
    $loaded = (Get-Module Pester).Version
    if ($loaded -lt [version]'5.0') { throw "Loaded Pester $loaded, expected 5.x — module shadowing not resolved" }
    $r = Invoke-Pester -Path $Path -PassThru -Output ($(if ($Detailed) { 'Detailed' } else { 'Minimal' }))
    'TOTAL={0} PASSED={1} FAILED={2} SKIPPED={3}' -f $r.TotalCount, $r.PassedCount, $r.FailedCount, $r.SkippedCount
    if ($r.FailedCount -gt 0) { $r.Failed | ForEach-Object { '  FAIL: ' + $_.ExpandedName }; exit 1 }
    exit 0
}

# --- directory mode: each file in an isolated child pwsh (hermetic) ---
Write-Host "Pester $PesterVersion (repo-local) — isolated per-file run of $Path"
$files = Get-ChildItem -Path $Path -Filter '*.Tests.ps1' -File | Sort-Object Name
$tot = 0; $pass = 0; $fail = 0; $skip = 0; $failedFiles = @()
foreach ($f in $files) {
    Stop-StrayTestDaemons
    $o = [System.IO.Path]::GetTempFileName()
    # Start-Process does NOT quote -ArgumentList elements: any element containing a space is
    # split into multiple argv tokens. A clone under "C:\Users\First Last\..." or a synced
    # drive therefore made EVERY file report "NO RESULT (exit 64)" — the whole directory mode
    # dead, with a message that blamed the tests. Build one explicitly quoted argument string.
    $argLine = '-NoProfile -File "{0}" -Path "{1}" -PesterVersion "{2}"' -f `
        $PSCommandPath, $f.FullName, $PesterVersion
    $proc = Start-Process pwsh -ArgumentList $argLine `
        -PassThru -NoNewWindow -RedirectStandardOutput $o
    if (-not $proc.WaitForExit($PerFileTimeoutSec * 1000)) {
        try { $proc.Kill($true) } catch {}
        Stop-StrayTestDaemons
        Write-Host ("  [{0,-34}] TIMEOUT after {1}s" -f $f.Name, $PerFileTimeoutSec)
        $failedFiles += "$($f.Name) (timeout)"; $fail++
        Remove-Item $o -ErrorAction SilentlyContinue
        continue
    }
    $out = Get-Content $o -Raw -ErrorAction SilentlyContinue
    Remove-Item $o -ErrorAction SilentlyContinue
    $m = [regex]::Match(([string]$out), 'TOTAL=(\d+) PASSED=(\d+) FAILED=(\d+) SKIPPED=(\d+)')  # [string] coerce, not PS7-only ??
    if ($m.Success) {
        $fp = [int]$m.Groups[2].Value; $ff = [int]$m.Groups[3].Value; $fs = [int]$m.Groups[4].Value
        $tot += [int]$m.Groups[1].Value; $pass += $fp; $fail += $ff; $skip += $fs
        Write-Host ("  [{0,-34}] {1,-4}  passed={2} failed={3} skipped={4}" -f $f.Name, $(if ($ff -gt 0) { 'FAIL' } else { 'ok' }), $fp, $ff, $fs)
        if ($ff -gt 0) { $failedFiles += $f.Name }
    } else {
        Write-Host ("  [{0,-34}] NO RESULT (exit {1})" -f $f.Name, $proc.ExitCode)
        $failedFiles += "$($f.Name) (no marker)"; $fail++
    }
}
Stop-StrayTestDaemons
''
'AGGREGATE TOTAL={0} PASSED={1} FAILED={2} SKIPPED={3}' -f $tot, $pass, $fail, $skip
if ($failedFiles.Count -gt 0) { 'FAILED FILES: ' + ($failedFiles -join ', '); exit 1 }
exit 0
