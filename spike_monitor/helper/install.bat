@echo off
REM Installs the Spike Net helper for the current Windows user (no administrator rights needed).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0spikenet-helper.ps1" -Install
