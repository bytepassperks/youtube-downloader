@echo off
echo ============================================
echo  MegaTransfer Desktop - Build Script
echo ============================================
echo.

REM Install dependencies
echo Installing dependencies...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)

REM Download Psiphon if not present
if not exist psiphon-tunnel-core.exe (
    echo.
    echo WARNING: psiphon-tunnel-core.exe not found!
    echo Download it from: https://github.com/nickoala/nickoala-psiphon-tunnel-core-builds
    echo Place it in this directory and re-run build.bat
    echo Building without Psiphon support...
    echo.
)

REM Build
echo Building MegaTransfer.exe...
python build.py
if %errorlevel% neq 0 (
    echo ERROR: Build failed
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Build complete! Output: dist\MegaTransfer.exe
echo ============================================
pause
