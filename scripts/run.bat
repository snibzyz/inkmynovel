@echo off
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Run scripts\install.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "inkxmynovel_pyqt.py"
if errorlevel 1 (
    echo.
    echo Program exited with an error.
    pause
)
endlocal
