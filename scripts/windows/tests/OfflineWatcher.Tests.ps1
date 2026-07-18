Describe "Step-OfflineState hysteresis" {
  BeforeAll { . "$PSScriptRoot/../../travel/offline-watcher.ps1" -DefineOnly }
  It "does not flip offline on a single blip (N=3)" {
    $s = [pscustomobject]@{ mode='online'; consecutive_down=0; consecutive_up=0; transition='none' }
    $s = Step-OfflineState -State $s -Reachable:$false   # 1 down
    $s.mode | Should -Be 'online'; $s.transition | Should -Be 'none'
    $s = Step-OfflineState -State $s -Reachable:$true    # recovers — resets
    $s.consecutive_down | Should -Be 0
  }
  It "goes offline after 3 consecutive down, online after 2 consecutive up" {
    $s = [pscustomobject]@{ mode='online'; consecutive_down=0; consecutive_up=0; transition='none' }
    1..3 | ForEach-Object { $s = Step-OfflineState -State $s -Reachable:$false }
    $s.mode | Should -Be 'offline'; $s.transition | Should -Be 'go_offline'
    $s = Step-OfflineState -State $s -Reachable:$true    # 1 up — not yet
    $s.mode | Should -Be 'offline'
    $s = Step-OfflineState -State $s -Reachable:$true    # 2 up — online
    $s.mode | Should -Be 'online'; $s.transition | Should -Be 'go_online'
  }
}
