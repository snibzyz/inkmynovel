@echo off
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"

echo ============================================================
echo  Building INKMYNOVEL.exe  (portable, single file)
echo ============================================================
echo.

REM Prefer the project's .venv; fall back to system Python.
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python not found. Run scripts\install.bat first.
    pause
    exit /b 1
)

echo [1/3] Installing build + runtime dependencies...
"%PY%" -m pip install --upgrade pip pyinstaller
if errorlevel 1 goto :fail
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
echo.

echo [2/3] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
echo.

echo [3/3] Running PyInstaller...
"%PY%" -m PyInstaller --noconfirm --clean INKMYNOVEL.spec
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo  Build OK
echo  Output : %CD%\dist\INKMYNOVEL.exe
echo  Tip    : copy INKMYNOVEL.exe anywhere and double-click to run.
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Build failed. See the messages above.
pause
exit /b 1
