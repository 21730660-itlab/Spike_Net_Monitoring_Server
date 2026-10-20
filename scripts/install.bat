@echo off
REM Spike Net Monitoring Server - one-time setup on Windows 10/11 or Windows Server.
REM Needs Python 3.11 or newer from https://www.python.org
REM Only ready-made packages are installed (--only-binary), so no C++ compiler is needed.
cd /d "%~dp0\.."

set "PY="
for %%V in (3.13 3.12 3.14 3.15 3.11) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY goto :nopython

echo Using: %PY%
%PY% --version
if exist .venv rmdir /s /q .venv
%PY% -m venv .venv || goto :error
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install --only-binary=:all: -r requirements.txt || goto :error
echo.
echo Setup complete. Start the server with scripts\run.bat
echo Then open http://localhost:8000 and log in as admin / admin@2026
pause
exit /b 0

:nopython
echo.
echo No supported Python found. Install Python 3.13 from:
echo   https://www.python.org/downloads/release/python-3139/
echo (tick "Add python.exe to PATH"), then run this file again.
pause
exit /b 1

:error
echo.
echo Setup failed. Copy the messages above and send them to support.
pause
exit /b 1
