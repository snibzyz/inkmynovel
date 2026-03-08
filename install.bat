@echo off
setlocal
set SCRIPT_DIR=%~dp0
if not exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment...
    py -3 -m venv "%SCRIPT_DIR%.venv"
    if errorlevel 1 goto :fail
)

echo [2/4] Upgrading pip...
call "%SCRIPT_DIR%.venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/4] Installing requirements...
python -m pip install -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 goto :fail

echo [4/4] Installation complete.
echo You can now run run.bat
goto :end

:fail
echo Installation failed.
pause
exit /b 1

:end
endlocal
