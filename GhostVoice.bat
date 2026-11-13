@echo off
rem Double-click to start GhostVoice from source on Windows.
rem The first run sets up a private Python environment in .venv (needs internet once).
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(sys.version_info < (3, 9))" || goto nopython

if exist ".venv\.installed" goto run
echo First run: setting up GhostVoice - one time, needs internet...
if exist ".venv" rmdir /s /q ".venv"
%PY% -m venv .venv || goto fail
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto fail
".venv\Scripts\python.exe" -m pip install --quiet -e . || goto fail
type nul > ".venv\.installed"

:run
if not "%~1"=="--selftest" goto launch
".venv\Scripts\python.exe" -m ghostvoice gui --selftest
exit /b %ERRORLEVEL%

:launch
start "" ".venv\Scripts\pythonw.exe" -m ghostvoice gui
exit /b 0

:nopython
echo.
echo Python 3.9 or newer is needed. Get it from https://www.python.org/downloads/
echo During setup, tick "Add python.exe to PATH". Then double-click this file again.
pause
exit /b 1

:fail
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
