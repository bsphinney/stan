@echo off
REM ===================================================================
REM  install_stan_tims10878.bat
REM  One-click STAN installer for TIMS-10878 (timsTOF HT).
REM
REM  What this script does -- no surprises:
REM    1. Calls install-stan.bat (downloads + installs STAN, DIA-NN,
REM       Sage, creates %USERPROFILE%\STAN\venv)
REM    2. Writes %USERPROFILE%\.stan\instruments.yml pre-configured
REM       for the timsTOF HT: hive mode, D:\Data watch dir, .d extension,
REM       submit_after_upload=true (Mode C full dispatch)
REM    3. Writes %USERPROFILE%\.stan\community.yml (stub -- operator
REM       must fill in auth_token)
REM    4. Prints post-install verification steps
REM
REM  timsTOF HT NOTE:
REM    Bruker .d acquisitions are directories, not single files.
REM    The watcher checks the total directory size every 10 s and
REM    triggers after stable_secs=60 s of no change -- never after
REM    mtime alone (mtime updates while sub-files are still writing).
REM
REM  Authoring rules per CLAUDE.md:
REM    - Full file rewrite when changing
REM    - Quoted paths throughout
REM    - No PowerShell + concat, no inline ternary, no Where-Object
REM ===================================================================

setlocal enabledelayedexpansion

REM Force UTF-8 output so rich/Unicode chars don't crash on redirect.
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

REM ------------------------------------------------------------------
REM  Instrument-specific constants (TIMS-10878 timsTOF HT)
REM ------------------------------------------------------------------
set "INSTRUMENT_NAME=timsTOF HT"
set "FAMILY=timsTOF"
set "VENDOR=bruker"
set "WATCH_DIR=D:\Data"
set "EXTENSIONS=.d"
set "STABLE_SECS=60"
set "HIVE_USER=brettsp"
set "HIVE_HOST=hive.hpc.ucdavis.edu"
set "HIVE_VENV=/quobyte/proteomics-grp/brett/stan_venv"
set "HIVE_DISPATCH_YML=/quobyte/proteomics-grp/STAN/dispatch.yml"
REM SMB path: Y:\ maps to the proteomics-grp share root on instrument PCs.
REM Y:\STAN\incoming\TIMS-10878 becomes /quobyte/proteomics-grp/STAN/incoming/TIMS-10878
REM on Hive (see stan/sync/upload_to_hive.py _smb_to_quobyte_path).
set "HIVE_UPLOAD_DIR=Y:\STAN\incoming\TIMS-10878"

echo.
echo   ============================================================
echo     STAN installer -- timsTOF HT (TIMS-10878)
echo   ============================================================
echo.
echo   This script will:
echo     1. Install STAN, DIA-NN, and Sage
echo     2. Pre-configure hive mode for the timsTOF HT
echo     3. Print verification steps
echo.
echo   Estimated time: 3-5 minutes (network dependent)
echo.

REM ------------------------------------------------------------------
REM  Step 1: run the base STAN installer
REM ------------------------------------------------------------------
echo   [1/3] Running base STAN installer...
echo.

set "INSTALLER=%~dp0..\install-stan.bat"
if not exist "%INSTALLER%" (
    echo   install-stan.bat not found at %INSTALLER%
    echo   Downloading from GitHub...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat' -OutFile '%~dp0..\install-stan.bat' -UseBasicParsing"
    set "INSTALLER=%~dp0..\install-stan.bat"
)

call "%INSTALLER%"
if errorlevel 1 (
    echo.
    echo   ERROR: Base STAN install failed. Fix errors above then re-run.
    pause
    exit /b 1
)

REM ------------------------------------------------------------------
REM  Step 2: write instruments.yml + community.yml
REM ------------------------------------------------------------------
echo.
echo   [2/3] Writing hive-mode config for TIMS-10878...
echo.

set "CONFIGURE_PS1=%~dp0configure_instruments_yml.ps1"
if not exist "%CONFIGURE_PS1%" (
    echo   ERROR: configure_instruments_yml.ps1 not found at %CONFIGURE_PS1%
    echo   Ensure this file is in the same scripts\ directory as this .bat.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%CONFIGURE_PS1%" ^
    -InstrumentName "%INSTRUMENT_NAME%" ^
    -Family "%FAMILY%" ^
    -Vendor "%VENDOR%" ^
    -WatchDir "%WATCH_DIR%" ^
    -Extensions "%EXTENSIONS%" ^
    -StableSecs %STABLE_SECS% ^
    -HiveUploadDir "%HIVE_UPLOAD_DIR%" ^
    -HiveUser "%HIVE_USER%" ^
    -HiveHost "%HIVE_HOST%" ^
    -HiveVenv "%HIVE_VENV%" ^
    -HiveDispatchYml "%HIVE_DISPATCH_YML%"

if errorlevel 1 (
    echo.
    echo   ERROR: Config write failed. See above.
    pause
    exit /b 1
)

REM ------------------------------------------------------------------
REM  Step 3: post-install instructions
REM ------------------------------------------------------------------
echo.
echo   ============================================================
echo     [3/3] Installation complete -- TIMS-10878 (timsTOF HT)
echo   ============================================================
echo.
echo   Config written to: %USERPROFILE%\.stan\instruments.yml
echo.
echo   VERIFY THESE SETTINGS before starting the watcher:
echo.
echo     watch_dir : D:\Data
echo       Confirm this is where HyStar writes .d directories.
echo       If your lab uses a different data path (e.g. E:\Data),
echo       edit %USERPROFILE%\.stan\instruments.yml now.
echo.
echo     Y:\ drive : must be mapped to \\proteomics-grp (Quobyte)
echo       Uploads go to Y:\STAN\incoming\TIMS-10878.
echo       If Y:\ is not mapped, run hive_dashboard.bat to mount it
echo       or map it manually in Windows Explorer.
echo.
echo   SSH KEY -- required for SLURM dispatch (submit_after_upload):
echo.
echo     Expected at: %USERPROFILE%\.ssh\id_ed25519
echo     If that file does not exist, copy your Hive private key there.
echo     Then run this command in an elevated CMD to lock down permissions
echo     (Windows equivalent of chmod 600):
echo.
echo       icacls "%USERPROFILE%\.ssh\id_ed25519" /inheritance:r /grant:r "%USERNAME%:R"
echo.
echo   COMMUNITY BENCHMARK -- fill in auth_token:
echo.
echo     Edit: %USERPROFILE%\.stan\community.yml
echo     Paste the auth_token Brett provided.
echo     display_name is shown on the public leaderboard (<=40 chars).
echo.
echo   FIRST-RUN VERIFICATION:
echo.
echo     1. Open a new CMD window (to pick up the updated PATH)
echo     2. Run:  stan watch
echo     3. Look for:  Active watchers: 1  (D:\Data)
echo     4. Acquire or copy a HeLa .d directory into D:\Data
echo     5. STAN polls the .d directory size every 10 s and triggers
echo        after %STABLE_SECS% s of no size change, then uploads to
echo        Hive and submits a SLURM job automatically.
echo     6. Check the Hive mirror for the submission log:
echo        \\proteomics-grp\STAN\TIMS-10878\logs\
echo.
echo   STABILITY DETECTION NOTE (Bruker .d):
echo.
echo     A Bruker .d acquisition is a DIRECTORY, not a single file.
echo     HyStar writes multiple sub-files (.tdf, .tdf_bin, analysis.baf,
REM     etc.) inside it simultaneously.  STAN measures the TOTAL size of
echo     the .d directory and only triggers after %STABLE_SECS% s of no
echo     change -- it does NOT use mtime, which updates for each sub-file.
echo     Do not reduce stable_secs below 60 s for the timsTOF.
echo.
echo   ============================================================
echo.
pause
