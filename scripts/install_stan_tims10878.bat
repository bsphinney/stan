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
REM  Step 1: run the base STAN installer (skip if already installed)
REM ------------------------------------------------------------------
echo   [1/4] Base STAN install...
echo.

REM Detect existing install. Two known venv roots: %USERPROFILE%\STAN\venv
REM (current default) and %USERPROFILE%\.stan\venv (legacy). If stan.exe
REM exists at either, skip the base install -- this script can then be
REM re-run cheaply just to refresh the SSH key and instruments.yml.
set "STAN_EXE=%USERPROFILE%\STAN\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" set "STAN_EXE=%USERPROFILE%\.stan\venv\Scripts\stan.exe"

if exist "%STAN_EXE%" (
    echo   STAN already installed at %STAN_EXE%
    echo   Skipping base install. Use stan.bat to update STAN itself
    echo   ^(it auto-updates from GitHub on launch^).
) else (
    REM Always force-redownload install-stan.bat to defeat stale-cache footguns.
    set "INSTALLER=%~dp0install-stan.bat"
    echo   Downloading fresh install-stan.bat from GitHub (cache-busted)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $t=[DateTime]::Now.Ticks; Invoke-WebRequest -Uri (\"https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat?t=$t\") -OutFile '%~dp0install-stan.bat' -UseBasicParsing"
    del "%~dp0install_stan.ps1" >nul 2>&1

    call "%~dp0install-stan.bat"
    if errorlevel 1 (
        echo.
        echo   ERROR: Base STAN install failed. Fix errors above then re-run.
        pause
        exit /b 1
    )
)

REM ------------------------------------------------------------------
REM  Step 2: SSH key auto-install (from Quobyte temp drop)
REM ------------------------------------------------------------------
echo.
echo   [2/4] Installing SSH key from Quobyte temp drop...
echo.

set "TEMP_KEY_SRC=Y:\STAN\temp_keys\TIMS-10878\id_ed25519"
set "SSH_DIR=%USERPROFILE%\.ssh"
set "SSH_KEY=%SSH_DIR%\id_ed25519"

if not exist "%SSH_DIR%" mkdir "%SSH_DIR%"

if exist "%TEMP_KEY_SRC%" (
    copy /y "%TEMP_KEY_SRC%" "%SSH_KEY%" >nul
    if errorlevel 1 (
        echo   WARN: copy from %TEMP_KEY_SRC% failed -- check Y: drive mapping.
    ) else (
        REM Lock perms (Windows equivalent of chmod 600).
        icacls "%SSH_KEY%" /inheritance:r >nul
        icacls "%SSH_KEY%" /grant:r "%USERNAME%:R" >nul
        echo   SSH key installed to: %SSH_KEY%
        echo   Permissions locked (icacls).
        echo.
        echo   IMPORTANT: After confirming SLURM dispatch works, delete the
        echo   temp drop on Quobyte:  %TEMP_KEY_SRC%
    )
) else (
    echo   No temp key found at %TEMP_KEY_SRC%
    echo   Skipping auto-install. If you already have a key at %SSH_KEY%
    echo   you can ignore this. Otherwise have Brett drop the key on
    echo   Quobyte and re-run this script.
)

REM ------------------------------------------------------------------
REM  Step 3: write instruments.yml + community.yml
REM ------------------------------------------------------------------
echo.
echo   [3/4] Writing hive-mode config for TIMS-10878...
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
REM  Step 4: post-install instructions
REM ------------------------------------------------------------------
echo.
echo   ============================================================
echo     [4/4] Installation complete -- TIMS-10878 (timsTOF HT)
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
echo     SSH key   : auto-installed by Step 2 above (if found).
echo       If Step 2 reported "No temp key found", have Brett drop
echo       a key at Y:\STAN\temp_keys\TIMS-10878\id_ed25519 then
echo       re-run this script.
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
