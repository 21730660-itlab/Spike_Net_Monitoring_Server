# Runs Spike Net Monitor in the background at Windows startup (before anyone logs in), as the
# SYSTEM account, restarting it if it stops. Uses the built-in Task Scheduler: nothing to install.
$ErrorActionPreference = "Stop"
$name = "Spike Net Monitor"
$runner = Join-Path $PSScriptRoot "service_run.bat"
if (-not (Test-Path (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"))) {
    Write-Host "Run install.bat first." -ForegroundColor Red; exit 1
}
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$runner`"" -WorkingDirectory (Split-Path $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Spike Net network monitoring server" -Force | Out-Null
New-NetFirewallRule -DisplayName "Spike Net Monitor" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow `
    -ErrorAction SilentlyContinue | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "Installed. Spike Net Monitor now runs in the background and starts with Windows." -ForegroundColor Green
Write-Host "Open http://localhost:8000  -  log file: $env:ProgramData\SpikeNetMonitor\server.log"
