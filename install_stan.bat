@echo off
REM STAN — One-Click Windows Installer
REM Double-click this file to install STAN.
REM
REM This launches the PowerShell installer script with the correct
REM execution policy so it can run without manual configuration.

echo.
echo ========================================
echo   STAN Installer - Know Your Instrument
echo ========================================
echo.
echo Starting installer...
echo.

powershell -ExecutionPolicy Bypass -File "%~dp0install_stan.ps1"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Installation encountered an error. See above for details.
    pause
)
