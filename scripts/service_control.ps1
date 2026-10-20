# Stop / restart / remove the "Spike Net Monitor" background task (see install_service.ps1).
param([ValidateSet("stop", "restart", "uninstall")][string]$Action = "restart")
$name = "Spike Net Monitor"

function Stop-Server {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*spike_monitor*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

Stop-Server
switch ($Action) {
    "restart"   { Start-Sleep -Seconds 2; Start-ScheduledTask -TaskName $name; Write-Host "Restarted." -ForegroundColor Green }
    "stop"      { Write-Host "Stopped (it starts again at the next Windows startup)." }
    "uninstall" { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
                  Write-Host "Removed. You can start the server by hand again with run.bat." -ForegroundColor Green }
}
