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
# -RepetitionDuration was [TimeSpan]::MaxValue, the old "repeat forever" idiom. Current Windows
# REJECTS it — Register-ScheduledTask fails with
#   The task XML contains a value which is incorrectly formatted or out of range.
#   (8,42):Duration:P99999999DT23H59M59S
# so this installer could not register the watcher at all, while still printing "Registered".
# 10 years is indistinguishable from forever for this purpose and serialises to valid task XML.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden -MultipleInstances IgnoreNew
# Registration must FAIL LOUDLY. It previously ran with the default non-terminating error
# behaviour and then unconditionally printed a success line, so a rejected task XML looked
# exactly like a successful install — which is how the box ended up with a hand-made task.
try {
    Register-ScheduledTask -TaskName 'mem0-offline-watcher' -Action $action -Trigger $trigger -Settings $set -Force -RunLevel Limited -ErrorAction Stop | Out-Null
} catch {
    throw "Failed to register 'mem0-offline-watcher': $($_.Exception.Message)"
}
$check = Get-ScheduledTask -TaskName 'mem0-offline-watcher' -ErrorAction SilentlyContinue
if (-not $check) { throw "Register-ScheduledTask reported success but 'mem0-offline-watcher' does not exist." }
Write-Host "Registered scheduled task 'mem0-offline-watcher' (every 2 min, windowless)."
