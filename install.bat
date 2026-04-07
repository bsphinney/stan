@echo off
:: STAN Installer — double-click this file to install everything.
:: No PowerShell execution policy headaches. No admin rights needed.
:: Works on any Windows 10/11 machine with internet access.
::
:: What it does:
::   1. Checks for Python 3.10+ (installs via winget if missing)
::   2. Creates a virtual environment at %USERPROFILE%\.stan\venv
::   3. Installs STAN from GitHub
::   4. Runs `stan init` to set up config
::   5. Launches `stan setup` for interactive configuration
::
:: After install, just type `stan` in any terminal.

title STAN Installer - Know Your Instrument
color 0B
chcp 65001 >nul 2>&1

echo.
echo  ============================================================
echo     STAN — Standardized proteomic Throughput ANalyzer
echo     Know your instrument.
echo  ============================================================
echo.
echo  This will install STAN on your instrument workstation.
echo  No admin rights required. Takes about 2 minutes.
echo.
echo  Press any key to start, or close this window to cancel.
pause >nul

:: ── DIA-NN License Agreement ────────────────────────────────────
echo.
echo  STAN uses DIA-NN for DIA searches and Sage for DDA searches.
echo.
echo  DIA-NN (by Vadim Demichev) is free for academic use.
echo  Commercial use requires a separate license from Vadim Demichev.
echo  DIA-NN license: https://github.com/vdemichev/DiaNN/blob/master/LICENSE
echo.
echo  Sage (by Michael Lazear) is open source under the MIT license.
echo  Sage license: https://github.com/lazear/sage/blob/master/LICENSE
echo.
set /p ACCEPT_LICENSE="  Do you accept the DIA-NN and Sage license terms? (Y/n): "
if /i "%ACCEPT_LICENSE%"=="n" (
    echo.
    echo  License not accepted. Installation cancelled.
    echo  STAN can still be used without DIA-NN/Sage for metadata
    echo  extraction and dashboard features, but QC searches require
    echo  acceptance of these licenses.
    pause
    exit /b 0
)
echo  License accepted [OK]

:: ── Check Python ────────────────────────────────────────────────
echo.
echo  [1/5] Checking for Python...

where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo  Python not found. Downloading Python 3.12 installer...
    echo.

    :: Try winget first (fast, silent)
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
        goto :python_ok
    )

    :: winget not available — download installer directly
    echo  winget not available. Downloading from python.org...
    set PY_URL=https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe
    set PY_INSTALLER=%TEMP%\python-3.12.4-installer.exe

    :: Try PowerShell download (works on all modern Windows)
    powershell -Command "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%'" >nul 2>&1
    if not exist "%PY_INSTALLER%" (
        :: Try certutil as last resort
        certutil -urlcache -split -f "%PY_URL%" "%PY_INSTALLER%" >nul 2>&1
    )

    if not exist "%PY_INSTALLER%" (
        echo.
        echo  ERROR: Could not download Python automatically.
        echo  Please install Python 3.12 manually:
        echo    1. Go to https://www.python.org/downloads/
        echo    2. Download Python 3.12
        echo    3. IMPORTANT: Check "Add Python to PATH" during installation
        echo    4. Re-run this installer
        echo.
        pause
        exit /b 1
    )

    echo  Installing Python 3.12 (this may take a minute)...
    echo  IMPORTANT: If a dialog appears, make sure "Add Python to PATH" is checked.
    "%PY_INSTALLER%" /passive InstallAllUsers=0 PrependPath=1 Include_test=0
    if %ERRORLEVEL% NEQ 0 (
        echo  Trying interactive install...
        "%PY_INSTALLER%" InstallAllUsers=0 PrependPath=1
    )
    del "%PY_INSTALLER%" >nul 2>&1

    :: Refresh PATH for this session
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
)
:python_ok

:: Verify Python version
python -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo  ERROR: Python 3.10+ is required. Found:
    python --version
    echo  Please update Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do echo  Found %%i [OK]

:: ── Create virtual environment ──────────────────────────────────
echo.
echo  [2/5] Creating virtual environment...

set STAN_HOME=%USERPROFILE%\.stan
set VENV=%STAN_HOME%\venv

if not exist "%STAN_HOME%" mkdir "%STAN_HOME%"

if not exist "%VENV%\Scripts\activate.bat" (
    python -m venv "%VENV%"
    if %ERRORLEVEL% NEQ 0 (
        echo  ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
)
echo  Virtual environment at %VENV% [OK]

:: Activate
call "%VENV%\Scripts\activate.bat"

:: ── Install STAN ────────────────────────────────────────────────
echo.
echo  [3/5] Installing STAN from GitHub...
echo  (this may take a minute on first install)

pip install --upgrade pip >nul 2>&1
pip install "git+https://github.com/bsphinney/stan.git" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  pip install failed. Trying with --no-cache-dir...
    pip install --no-cache-dir "git+https://github.com/bsphinney/stan.git"
    if %ERRORLEVEL% NEQ 0 (
        echo  ERROR: Installation failed. Check your internet connection.
        pause
        exit /b 1
    )
)
echo  STAN installed [OK]

:: ── Initialize config ───────────────────────────────────────────
echo.
echo  [4/5] Initializing configuration...

stan init
echo  Config directory: %STAN_HOME% [OK]

:: ── Add to PATH permanently ─────────────────────────────────────
echo.
echo  [5/5] Adding STAN to your PATH...

:: Add the venv Scripts dir to user PATH so `stan` works in any terminal
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v PATH 2^>nul') do set "USERPATH=%%b"
echo %USERPATH% | find /i "%VENV%\Scripts" >nul
if %ERRORLEVEL% NEQ 0 (
    setx PATH "%USERPATH%;%VENV%\Scripts" >nul 2>&1
    echo  Added %VENV%\Scripts to PATH [OK]
    echo  (new terminals will find `stan` automatically)
) else (
    echo  Already in PATH [OK]
)

:: ── Done! ───────────────────────────────────────────────────────
echo.
echo  ============================================================
echo     STAN is installed!
echo  ============================================================
echo.
echo  Next steps:
echo    1. Run interactive setup:   stan setup
echo    2. Start the watcher:       stan watch
echo    3. Open the dashboard:      stan dashboard
echo.
echo  Or just close this window and type `stan` in any terminal.
echo.

:: Offer to run setup now
set /p RUNSETUP="  Run `stan setup` now? (Y/n): "
if /i "%RUNSETUP%" NEQ "n" (
    echo.
    stan setup
)

echo.
echo  STAN installation complete. Happy QC!
echo.
pause
