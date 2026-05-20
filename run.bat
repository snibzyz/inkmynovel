@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found.
    echo Please double-click install.bat first.
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" inkxmynovel_pyqt.py

echo.
echo Program closed.
pause
