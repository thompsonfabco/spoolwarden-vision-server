@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install\install.ps1" -Start -OpenBrowser %*
if errorlevel 1 (
    echo.
    echo [install.bat] Installation failed. See messages above.
    pause
    exit /b 1
)
exit /b 0
