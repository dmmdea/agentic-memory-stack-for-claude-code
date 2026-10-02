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

# 1.32.5: a record its re-post POISONED leaves the queue (no loop, no poison line per drain), and a
# line another process appends while the drain runs survives the drain's rewrite.
Describe "Drain-Mem0DeadLetter outcome handling (1.32.5)" {
  BeforeAll {
    . "$PSScriptRoot/../memory-common.ps1"
    function Test-IsShipLog { param($Text) return $false }
    function Write-MemoryLog { param($Component,$Message) }
  }
  BeforeEach {
    $script:StateDir = Join-Path $TestDrive ('s' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $script:StateDir | Out-Null
    $script:dlq = Join-Path $script:StateDir 'mem0-post-failures.jsonl'
    $rec = @{ text='an insight'; source='dream-consolidator'; metadata=@{tier='insight'}; attempts=1; error='refused'; status_code=0; timestamp=(Get-Date).ToString('o') } | ConvertTo-Json -Compress
    Set-Content -LiteralPath $script:dlq -Value $rec -Encoding UTF8
  }
  AfterAll { $script:StateDir = Join-Path $env:USERPROFILE '.claude\state' }

  It "drops a record whose re-post was poisoned instead of re-queueing it" {
    function Add-Mem0Memory { param($Text,$Source,$Metadata) $script:Mem0LastPostOutcome = 'poisoned'; return $false }
    $r = Drain-Mem0DeadLetter
    $r.quarantined | Should -Be 1
    Test-Path $script:dlq | Should -BeFalse
    $script:Mem0InDeadLetterDrain | Should -BeFalse -Because 'the drain flag is always reset'
  }
  It "keeps a line another process appended while the drain ran" {
    function Add-Mem0Memory {
      param($Text,$Source,$Metadata)
      $late = @{ text='appended mid-drain'; source='l1a'; metadata=@{tier='evidence'}; attempts=1; error='x'; status_code=0; timestamp=(Get-Date).ToString('o') } | ConvertTo-Json -Compress
      Add-Content -LiteralPath $script:dlq -Value $late -Encoding UTF8
      $script:Mem0LastPostOutcome = 'retry'; return $false
    }
    Drain-Mem0DeadLetter | Out-Null
    $texts = @(Get-Content $script:dlq | ForEach-Object { ($_ | ConvertFrom-Json).text })
    $texts | Should -Contain 'an insight'
    $texts | Should -Contain 'appended mid-drain'
    $texts.Count | Should -Be 2
  }
}
