#Requires -PSEdition Core
param([string]$AuthorityHost = 'your-machine')   # machine that OWNS the live brain — same convention as travel-mode.ps1
# HARD GUARD — never register the watcher on the authority machine: its go_online transition
# stops mem0/qdrant, which ARE the production brain there (mirrors travel-mode.ps1's refusal).
if ($env:COMPUTERNAME -ieq $AuthorityHost) {
    throw "REFUSED: this machine ($env:COMPUTERNAME) IS the memory authority — the watcher's go_online stops the LIVE mem0/qdrant. The watcher is for the laptop. (Override only by editing -AuthorityHost, deliberately.)"
}
$watcher = Join-Path $PSScriptRoot 'offline-watcher.ps1'
$action  = New-ScheduledTaskAction -Execute 'pwsh.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$watcher`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration ([TimeSpan]::MaxValue)
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'mem0-offline-watcher' -Action $action -Trigger $trigger -Settings $set -Force -RunLevel Limited | Out-Null
Write-Host "Registered scheduled task 'mem0-offline-watcher' (every 2 min)."
