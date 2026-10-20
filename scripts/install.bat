@echo off
REM Spike Net Monitoring Server - one-time setup on Windows 10/11 or Windows Server.
REM Requires Python 3.11+ from https://www.python.org (tick "Add python.exe to PATH").
cd /d "%~dp0\.."
python -m venv .venv || goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || goto :error
echo.
echo Setup complete. Start the server with scripts\run.bat
echo Then open http://localhost:8000 and log in as admin / admin@2026
pause
exit /b 0
:error
echo Setup failed. Check that Python is installed and on PATH.
pause
exit /b 1
