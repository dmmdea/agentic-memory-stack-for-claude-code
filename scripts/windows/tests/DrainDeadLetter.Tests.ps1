# DrainDeadLetter.Tests.ps1 — offline-first: connection-level failures (status_code 0)
# must never quarantine and never accrue attempts in Drain-Mem0DeadLetter.
Describe "Drain-Mem0DeadLetter connection-failure handling" {
  BeforeAll {
    . "$PSScriptRoot/../memory-common.ps1"
    $script:StateDir = Join-Path $TestDrive 'state'
    New-Item -ItemType Directory -Force -Path $script:StateDir | Out-Null
    # Force every re-POST to fail at the connection level (status_code 0)
    function Add-Mem0Memory { param($Text,$Source,$Metadata) return $false }
    function Test-IsShipLog { param($Text) return $false }
    function Write-MemoryLog { param($Component,$Message) }
  }
  AfterAll {
    # Restore StateDir to the real path so other tests are not affected
    $script:StateDir = Join-Path $env:USERPROFILE '.claude\state'
  }
  It "does not quarantine a connection-failure (status_code 0) record after 5 drains" {
    $dlq = Join-Path $script:StateDir 'mem0-post-failures.jsonl'
    $rec = @{ text='offline fact'; source='l1a'; metadata=@{tier='evidence'}; attempts=1; error='refused'; status_code=0; timestamp=(Get-Date).ToString('o') } | ConvertTo-Json -Compress
    Set-Content -LiteralPath $dlq -Value $rec -Encoding UTF8
    1..6 | ForEach-Object { Drain-Mem0DeadLetter | Out-Null }
    $quar = Join-Path $script:StateDir 'mem0-post-poison.jsonl'
    (Test-Path $quar) | Should -BeFalse
    (Test-Path $dlq) | Should -BeTrue    # still queued, not quarantined
    $kept = (Get-Content $dlq | ConvertFrom-Json)
    $kept.attempts | Should -Be 1        # never incremented for connection failures
  }
}
