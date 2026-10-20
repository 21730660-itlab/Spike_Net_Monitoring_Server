@echo off
REM Starts the server. Run this "as administrator" for the fastest ping engine
REM (raw ICMP); otherwise it falls back to Windows ping.exe automatically.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
python -m spike_monitor
