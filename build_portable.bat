@echo off
REM ============================================================================
REM build_portable.bat
REM
REM One-shot build of the portable Windows folder for Bonus Reload Bot.
REM
REM After running this, `dist\Bonus Reload Bot\` is a self-contained folder:
REM copy it to any Windows PC (no Python, no Playwright, no Chromium needed)
REM and double-click `Bonus Reload Bot.exe`.
REM
REM Usage:
REM     build_portable.bat
REM ============================================================================
setlocal enabledelayedexpansion

REM Move to the folder that contains this script (project root)
cd /d "%~dp0"

echo.
echo [1/5] Verifying Python 3.13 ...
py -3.13 --version || (
    echo   Python 3.13 is required. Install it and re-run.
    exit /b 1
)

echo.
echo [2/5] Preparing virtual environment ^& dependencies ...
if not exist .venv (
    py -3.13 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

echo.
echo [3/5] Installing Playwright Chromium into ".\pw-browsers" ...
set "PLAYWRIGHT_BROWSERS_PATH=%CD%\pw-browsers"
python -m playwright install chromium
if errorlevel 1 (
    echo   Playwright install failed.
    exit /b 2
)

echo.
echo [4/5] Building portable folder with PyInstaller ...
if exist build   rmdir /s /q build
if exist dist    rmdir /s /q dist
python -m PyInstaller --noconfirm --clean BonusReloadBot.spec
if errorlevel 1 (
    echo   PyInstaller build failed.
    exit /b 3
)

echo.
echo [5/5] Seeding writable folders next to the .exe ...
set "OUT=dist\Bonus Reload Bot"
if not exist "%OUT%\config"                  xcopy /E /I /Y config           "%OUT%\config"           >nul
if not exist "%OUT%\credentials"             mkdir "%OUT%\credentials"
if exist "credentials\service_account.json.example" (
    copy /Y "credentials\service_account.json.example" "%OUT%\credentials\" >nul
)
if not exist "%OUT%\logs"                    mkdir "%OUT%\logs"
if not exist "%OUT%\screenshots"             mkdir "%OUT%\screenshots"
if not exist "%OUT%\browser_profile_bonus_reload" mkdir "%OUT%\browser_profile_bonus_reload"

echo.
echo ============================================================================
echo Portable build ready:  %OUT%\Bonus Reload Bot.exe
echo ============================================================================
echo Deploy checklist for a fresh Windows PC:
echo   1. Copy the entire folder "%OUT%" to the target machine.
echo   2. Drop the real service_account.json into  credentials\
echo   3. Double-click  "Bonus Reload Bot.exe"
echo No Python / Playwright / Chromium install required on the target PC.
echo.
endlocal
