@echo off
REM Started by the "Spike Net Monitor" scheduled task (install_service.bat). Do not run by hand.
cd /d "%~dp0\.."
".venv\Scripts\python.exe" -m spike_monitor
