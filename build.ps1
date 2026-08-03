# NovelAI Writer 桌面版构建脚本 (PowerShell 版本)
# 输出: dist\NovelAI Writer.exe
# 用法：在 PowerShell 里跑 .\build.ps1

$ErrorActionPreference = "Stop"
# 切到脚本所在目录（兼容被 dot-source / 跨目录调用）
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ScriptDir) { $ScriptDir = $PSScriptRoot }
Set-Location $ScriptDir
$ProjectRoot = (Get-Location).Path

function Write-Step($msg) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " $msg" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host ""
}

# 1. 检查 Python
Write-Step "[1/5] Checking Python"
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) {
    Write-Host "ERROR: Python not found. Install from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "       and check 'Add to PATH' option." -ForegroundColor Red
    pause
    exit 1
}
$pyVer = python --version 2>&1
Write-Host "  Detected: $pyVer" -ForegroundColor Green

# 2. 装依赖
Write-Step "[2/5] Installing dependencies (this may take 2 to 3 min)"
python -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "pip upgrade failed" -ForegroundColor Red; pause; exit 1 }
python -m pip install -r requirements.txt -r requirements-desktop.txt --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed. Check network." -ForegroundColor Red
    pause
    exit 1
}
Write-Host "  Done." -ForegroundColor Green

# 3. 检查图标
Write-Step "[3/5] Checking assets"
if (Test-Path "assets\icon.ico") {
    Write-Host "  Custom icon: assets\icon.ico ✓" -ForegroundColor Green
} else {
    Write-Host "  Note: assets\icon.ico not found, will use default." -ForegroundColor Yellow
}

# 4. 清理
Write-Step "[4/5] Cleaning old build"
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
Write-Host "  Done." -ForegroundColor Green

# 5. PyInstaller
Write-Step "[5/5] Running PyInstaller 3 to 7 min"
python -m PyInstaller novelai_desktop.spec --clean --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: PyInstaller failed." -ForegroundColor Red
    pause
    exit 1
}

# 完成
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " BUILD SUCCESS!" -ForegroundColor Green
Write-Host " Output: dist\NovelAI Writer.exe" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "First run checklist:" -ForegroundColor Yellow
Write-Host "  1. Copy your .env to the dist\ folder" -ForegroundColor Yellow
Write-Host "     (next to NovelAI Writer.exe)" -ForegroundColor Yellow
Write-Host "  2. If Windows Defender blocks: click 'More info' -> 'Run anyway'" -ForegroundColor Yellow
Write-Host "  3. Log file: dist\data\novelai.log (errors go here)" -ForegroundColor Yellow
Write-Host ""
Write-Host "Shortcuts inside the app:" -ForegroundColor Cyan
Write-Host "  F          focus mode (hide all chrome)"
Write-Host "  Ctrl+.     toggle dark/light theme"
Write-Host "  Esc        exit focus mode"
Write-Host ""

# 桌面快捷方式
$exe = Resolve-Path "dist\NovelAI Writer.exe"
$shortcutPath = [Environment]::GetFolderPath('Desktop') + '\NovelAI Writer.lnk'
$shell = New-Object -COM WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exe.Path
$shortcut.WorkingDirectory = (Resolve-Path "dist").Path
$shortcut.IconLocation = $exe.Path + ",0"
$shortcut.Description = "NovelAI Writer - long novel AI editor"
$shortcut.Save()
Write-Host "Desktop shortcut: $shortcutPath" -ForegroundColor Green

pause
