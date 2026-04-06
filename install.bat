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

title STAN Installer — Know Your Instrument
color 0B

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

:: ── Check Python ────────────────────────────────────────────────
echo.
echo  [1/5] Checking for Python...

where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo  Python not found. Attempting to install via winget...
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo.
        echo  ERROR: Could not install Python automatically.
        echo  Please install Python 3.10+ from https://www.python.org/downloads/
        echo  Make sure to check "Add Python to PATH" during installation.
        echo.
        pause
        exit /b 1
    )
    :: Refresh PATH
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
)

:: Verify Python version
python -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo  ERROR: Python 3.10+ is required. Found:
    python --version
    echo  Please update Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do echo  Found %%i ✓

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
echo  Virtual environment at %VENV% ✓

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
echo  STAN installed ✓

:: ── Initialize config ───────────────────────────────────────────
echo.
echo  [4/5] Initializing configuration...

stan init
echo  Config directory: %STAN_HOME% ✓

:: ── Add to PATH permanently ─────────────────────────────────────
echo.
echo  [5/5] Adding STAN to your PATH...

:: Add the venv Scripts dir to user PATH so `stan` works in any terminal
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v PATH 2^>nul') do set "USERPATH=%%b"
echo %USERPATH% | find /i "%VENV%\Scripts" >nul
if %ERRORLEVEL% NEQ 0 (
    setx PATH "%USERPATH%;%VENV%\Scripts" >nul 2>&1
    echo  Added %VENV%\Scripts to PATH ✓
    echo  (new terminals will find `stan` automatically)
) else (
    echo  Already in PATH ✓
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
