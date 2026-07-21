#Requires -PSEdition Core
param([string]$AuthorityHost = 'your-machine')   # machine that OWNS the live brain — same convention as travel-mode.ps1
# HARD GUARD — never register the watcher on the authority machine: its go_online transition
# stops mem0/qdrant, which ARE the production brain there (mirrors travel-mode.ps1's refusal).
if ($env:COMPUTERNAME -ieq $AuthorityHost) {
    throw "REFUSED: this machine ($env:COMPUTERNAME) IS the memory authority — the watcher's go_online stops the LIVE mem0/qdrant. The watcher is for the laptop. (Override only by editing -AuthorityHost, deliberately.)"
}
$watcher = Join-Path $PSScriptRoot 'offline-watcher.ps1'
# WINDOWLESS (2026-07-21). This task repeats every 2 MINUTES. Registered as a bare
# `pwsh.exe` action it runs on the interactive desktop, so it flashed a console window and
# stole focus every two minutes all day — the operator ended up silencing it by hand, which
# fixed one box and left the registration still wrong for every future install.
# run-hidden.vbs starts the child with no window at all (wscript has no console of its own).
# -WindowStyle Hidden would not do: the console is created, then hidden, which still flashes.
# -Hidden additionally keeps the task out of the Task Scheduler library view.
$hiddenVbs = Join-Path (Split-Path -Parent $PSScriptRoot) 'windows\run-hidden.vbs'
if (-not (Test-Path $hiddenVbs)) { throw "run-hidden.vbs not found at $hiddenVbs — required so the 2-minute watcher does not flash a console window." }
$action  = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "//nologo `"$hiddenVbs`" pwsh.exe -NoProfile -ExecutionPolicy Bypass -File `"$watcher`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration ([TimeSpan]::MaxValue)
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'mem0-offline-watcher' -Action $action -Trigger $trigger -Settings $set -Force -RunLevel Limited | Out-Null
Write-Host "Registered scheduled task 'mem0-offline-watcher' (every 2 min, windowless)."
