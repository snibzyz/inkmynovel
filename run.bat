@echo off
setlocal
set SCRIPT_DIR=%~dp0
if not exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    echo Virtual environment not found. Please run install.bat first.
    pause
    exit /b 1
)

call "%SCRIPT_DIR%.venv\Scripts\activate.bat"
python "%SCRIPT_DIR%inkxmynovel_pyqt.py"
if errorlevel 1 (
    echo.
    echo Program exited with an error.
    pause
)
endlocal
