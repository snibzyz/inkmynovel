@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo    INKMYNOVEL - Install
echo ==========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment...
    py -3 -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ERROR: Could not create the virtual environment.
    echo Please install Python 3 from https://www.python.org/downloads/
    echo During setup, tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo Installing dependencies, please wait 1-2 minutes...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo.
echo ==========================================
echo    Done. Now double-click run.bat
echo ==========================================
echo.
pause
