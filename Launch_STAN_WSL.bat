@echo off
:: ============================================================================
::  Launch_STAN_WSL.bat — One-click launcher for STAN Mode B on Windows via WSL2
:: ============================================================================
::
::  Runs STAN inside a WSL2 Ubuntu distro. No Docker needed. No HPC needed.
::  Instrument PCs push raw files to an SMB share; this box ingests them.
::
::  Mode B: beefy Windows lab box, WSL2 Ubuntu, DIA-NN + Sage locally.
::  Raw files arrive via SMB from instrument PCs on the same network.
::  Dashboard on http://localhost:8421 accessible from any browser on this host.
::
::  On first run, installs STAN + DIA-NN + Sage + .NET 8 inside WSL (~15 min).
::  Subsequent runs start in under 30 seconds.
::
::  Requirements:
::    - Windows 10 build 19041+ or Windows 11
::    - WSL2 with Ubuntu (installed automatically if missing)
::    - Virtualization enabled in BIOS (Intel VT-x / AMD-V)
::
:: ============================================================================

setlocal enableextensions
title STAN (WSL2 Mode B)

echo.
echo  ============================================
echo    STAN Proteomics — WSL2 Mode B launcher
echo  ============================================
echo.

:: ----------------------------------------------------------------------------
:: 1. Check wsl.exe is on PATH
:: ----------------------------------------------------------------------------
where wsl >nul 2>&1
if errorlevel 1 (
    echo  ERROR: wsl.exe not found. You need Windows 10 build 19041+ or Windows 11.
    echo.
    echo  To install WSL2, open PowerShell as Administrator and run:
    echo      wsl --install
    echo  Then restart Windows and re-run this launcher.
    echo.
    pause
    exit /b 1
)

:: ----------------------------------------------------------------------------
:: 2. Check that an Ubuntu distro is installed and reachable.
::
::    wsl --list --quiet emits UTF-16 which findstr can't parse.
::    wsl -d Ubuntu -e true returns negative-ish exit codes for
::    WSL_E_DISTRO_NOT_FOUND that batch if errorlevel misreads as success.
::    Use a sentinel-string probe instead — exit-code-independent.
:: ----------------------------------------------------------------------------
wsl -d Ubuntu -e bash -c "echo __STAN_UBUNTU_OK__" 2>&1 | findstr /c:"__STAN_UBUNTU_OK__" >nul
if errorlevel 1 (
    echo  No working Ubuntu distro in WSL. Installing Ubuntu now...
    echo  ^(This opens a separate window. Follow the prompts to create a
    echo   Linux username + password, then close it and re-run this launcher.^)
    echo.
    echo  Write down the Ubuntu password. The second run will prompt for it
    echo  when installing system packages ^(sudo apt-get^). Ubuntu does NOT
    echo  echo the password as you type it -- that is normal.
    echo.
    wsl --install -d Ubuntu
    echo.
    echo  Ubuntu install triggered. After setup finishes, re-run this launcher.
    pause
    exit /b 0
)

:: ----------------------------------------------------------------------------
:: 3. Verify stan_wsl_setup.sh sits next to this .bat file
:: ----------------------------------------------------------------------------
set "SETUP_SRC=%~dp0stan_wsl_setup.sh"
if not exist "%SETUP_SRC%" (
    echo  ERROR: stan_wsl_setup.sh not found next to this .bat file.
    echo  Expected at: %SETUP_SRC%
    echo.
    echo  Make sure you downloaded the full STAN repo, not just this file.
    pause
    exit /b 1
)

:: ----------------------------------------------------------------------------
:: 4. Copy the setup script into WSL (handles OneDrive / long path issues)
:: ----------------------------------------------------------------------------
echo  Copying setup script into WSL...
wsl -d Ubuntu -e bash -c "cp \"$(wslpath -u '%SETUP_SRC%')\" ~/stan_wsl_setup.sh && chmod +x ~/stan_wsl_setup.sh"
if errorlevel 1 (
    echo  ERROR: Failed to copy setup script into WSL.
    echo  If this repo lives in OneDrive, try cloning to C:\STAN\ instead.
    pause
    exit /b 1
)

:: ----------------------------------------------------------------------------
:: 5. Run the app (setup is auto-triggered on first run via the script's
::    'auto' mode — idempotent, safe to re-run)
:: ----------------------------------------------------------------------------
echo.
echo  ============================================
echo    Starting STAN...
echo  ============================================
echo.
echo    FIRST INSTALL: 10-20 MINUTES
echo    (Python, DIA-NN, Sage, .NET 8 SDK all download)
echo.
echo    Subsequent runs: under 30 seconds.
echo.
echo    Watch this terminal for progress. When you see
echo        "STAN dashboard ready"
echo    the browser opens automatically at http://localhost:8421
echo.
echo    You can also open http://localhost:8421 manually
echo    once the dashboard line appears.
echo.
echo  ============================================
echo.

:: Poll for the STAN dashboard port (8421) and open browser when ready.
:: Waits up to 60 minutes (first install is long). Silent on timeout.
start "" /B powershell -NoProfile -WindowStyle Hidden -Command ^
  "$d=3600; while($d -gt 0){ if (Get-NetTCPConnection -LocalPort 8421 -State Listen -ErrorAction SilentlyContinue) { Start-Process 'http://localhost:8421'; exit 0 }; Start-Sleep -Seconds 2; $d -= 2 }"

:: Blocks until the user presses Ctrl+C to stop STAN
wsl -d Ubuntu -e bash -c "bash ~/stan_wsl_setup.sh"

echo.
echo  STAN has stopped.
pause
endlocal
