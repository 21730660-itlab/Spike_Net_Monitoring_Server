@echo off
REM Right-click -> Run as administrator. Makes Spike Net Monitor start with Windows, in the background.
REM Close run.bat first if it is open (both use port 8000).
net session >nul 2>&1 || (echo Please right-click this file and choose "Run as administrator". & pause & exit /b 1)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_service.ps1"
pause
