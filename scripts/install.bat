@echo off
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"

echo ============================================================
echo   INKMYNOVEL - install dependencies (Windows)
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment (.venv)...
    py -3 -m venv .venv
    if errorlevel 1 goto :fail
)

echo [2/3] Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/3] Installing requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo Done. Launch the app with:  scripts\run.bat
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Installation failed. Is Python 3 installed and on PATH?
echo         Get it from https://www.python.org/downloads/
pause
exit /b 1
