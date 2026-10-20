@echo off
REM Right-click -> Run as administrator. Stops the background server and removes it from startup.
net session >nul 2>&1 || (echo Please right-click this file and choose "Run as administrator". & pause & exit /b 1)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0service_control.ps1" -Action uninstall
pause
