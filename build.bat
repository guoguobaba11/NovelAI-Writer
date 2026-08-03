@echo off
REM ============================================================
REM  NovelAI Writer Desktop Build (Windows, self-contained)
REM  Just double-click or run from cmd.
REM  Output: dist\NovelAI Writer.exe
REM ============================================================
REM  IMPORTANT: ASCII-only main, all non-ASCII text via temp file.
REM  Subroutines are at the END of file (cmd labels are NOT
REM  auto-skipped -- if defined before main, they execute and
REM  their 'goto :eof' skips everything below them).

cd /d "%~dp0"

setlocal enabledelayedexpansion
set "TMPMSG=%TEMP%\novelai_msg_%RANDOM%.txt"

REM ============================================================
REM  MAIN
REM ============================================================

REM 0. banner
call :msg "NovelAI Writer Desktop Build"
call :msg "Project: %CD%"
echo.

REM 1. Python
call :line "[1/5] Checking Python"
where python >nul 2>&1
if errorlevel 1 (
    call :msg "ERROR: Python not found."
    call :msg "Install from https://www.python.org/downloads/ and check 'Add to PATH'."
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
call :msg "Detected Python !PYVER!"

REM 2. pip install
call :line "[2/5] Installing dependencies (2 to 3 min)"
call :msg "Upgrading pip ..."
python -m pip install --upgrade pip --quiet
if errorlevel 1 (
    call :msg "WARN: pip upgrade failed, continuing ..."
)
call :msg "Installing requirements.txt + requirements-desktop.txt ..."
python -m pip install -r requirements.txt -r requirements-desktop.txt --quiet
if errorlevel 1 (
    call :msg "ERROR: pip install failed. Check network / proxy."
    pause
    exit /b 1
)
call :msg "Dependencies installed."

REM 3. assets
call :line "[3/5] Checking assets"
if exist "assets\icon.ico" (
    call :msg "Custom icon: assets\icon.ico"
) else (
    call :msg "WARN: assets\icon.ico not found, will use default."
)

REM 4. clean
call :line "[4/5] Cleaning old build"
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "dist\NovelAI Writer" rmdir /s /q "dist\NovelAI Writer"
call :msg "Cleaned."

REM 5. PyInstaller
call :line "[5/5] Running PyInstaller 3 to 7 min"
call :msg "This is the longest step. Do not close this window."
python -m PyInstaller novelai_desktop.spec --clean --noconfirm
if errorlevel 1 (
    call :msg "ERROR: PyInstaller failed. Scroll up to see the error."
    pause
    exit /b 1
)

REM 6. done
call :line "BUILD SUCCESS"
call :msg "Output: dist\NovelAI Writer\NovelAI Writer.exe"
echo.
call :msg "FIRST RUN CHECKLIST:"
call :msg "  1. Copy your .env to dist\NovelAI Writer\ (next to NovelAI Writer.exe)"
call :msg "  2. If Windows Defender blocks: 'More info' then 'Run anyway'"
call :msg "  3. Errors are logged to: dist\NovelAI Writer\data\novelai.log"
echo.
call :msg "Shortcuts inside the app:"
call :msg "  F          focus mode (hide all chrome)"
call :msg "  Ctrl+.     toggle dark / light theme"
call :msg "  Esc        exit focus mode"
echo.

REM 7. desktop shortcut
powershell -NoProfile -Command "$d = [Environment]::GetFolderPath('Desktop'); $s = (New-Object -COM WScript.Shell).CreateShortcut($d + '\NovelAI Writer.lnk'); $s.TargetPath = '%CD%\dist\NovelAI Writer\NovelAI Writer.exe'; $s.WorkingDirectory = '%CD%\dist\NovelAI Writer'; $s.IconLocation = '%CD%\dist\NovelAI Writer\NovelAI Writer.exe,0'; $s.Description = 'NovelAI Writer - long novel AI editor'; $s.Save(); Write-Host 'Desktop shortcut:' $d"
echo.

pause
exit /b 0

REM ============================================================
REM  SUBROUTINES (must be AFTER all main code, otherwise cmd
REM  executes them on first pass and the 'goto :eof' jumps
REM  over the rest of the main code).
REM ============================================================

:msg
REM call :msg <text>  -- echo a (possibly non-ASCII) message
REM If called outside main (e.g. from main code above), %~1 is set.
REM If executed on first pass, %~1 is empty, so we just return.
if "%~1"=="" goto :eof
set "MSG=%~1"
powershell -NoProfile -Command "Set-Content -Path '%TMPMSG%' -Value '%MSG%' -Encoding utf8"
type "%TMPMSG%"
del "%TMPMSG%" 2>nul
goto :eof

:line
REM call :line <text>  -- print a section header
if "%~1"=="" goto :eof
echo.
echo ============================================================
echo  %~1
echo ============================================================
echo.
goto :eof
