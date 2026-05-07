@echo off
REM ===================================================================
REM  test-hive-flow.bat — install STAN v0.2.318+ and smoke-test the
REM  new Hive-side CLI surface (stan hive-process + stan hive-dispatch).
REM
REM  Hosted at /quobyte/proteomics-grp/STAN/test-hive-flow.bat. Copy to
REM  a local dir before running — Windows MOTW blocks .bat execution
REM  from UNC paths. After running, the full stdout/stderr transcript
REM  uploads to /quobyte/proteomics-grp/STAN/test_logs/ via SCP so
REM  Brett (and Claude reading the Hive mirror from the Mac side) can
REM  see exactly what happened on each instrument PC without anyone
REM  copy-pasting from a cmd window.
REM
REM  One-time setup per instrument PC (master key from Brett's Mac
REM  hosted temporarily on Quobyte):
REM     copy "<quobyte>\proteomics-grp\brett\.tmp_keys\id_ed25519" ^
REM          "%USERPROFILE%\.ssh\id_ed25519"
REM     icacls "%USERPROFILE%\.ssh\id_ed25519" /inheritance:r ^
REM            /grant:r "%USERNAME%:R"
REM
REM  After the rollout to all instrument PCs, clean up the Quobyte
REM  copy of the key:
REM     ssh hive "rm -rf /quobyte/proteomics-grp/brett/.tmp_keys"
REM
REM  Authoring rules per CLAUDE.md: rewrite the whole file when changing
REM  it, quote every path, no PowerShell concatenation, no inline ternary.
REM ===================================================================

setlocal enabledelayedexpansion

set "HIVE_USER=brettsp"
set "HIVE_HOST=hive.hpc.ucdavis.edu"
set "HIVE_VENV=/quobyte/proteomics-grp/brett/stan_venv"
set "HIVE_DISPATCH_YML=/quobyte/proteomics-grp/STAN/dispatch.yml"
set "HIVE_LOG_DIR=/quobyte/proteomics-grp/STAN/test_logs"

REM ----- Outer wrapper: log capture + upload --------------------------
REM Two-pass invocation. First pass: resolve SSH key, build a local log
REM path under %TEMP%, then re-invoke self with --inner redirecting all
REM output to that log. Second pass (--inner): runs the actual smoke
REM test. After inner exits, outer types the log so Brett sees it on
REM screen, then scp-uploads to Hive Quobyte if the SSH key is
REM available.

if "%1"=="--inner" goto inner

REM Resolve SSH key once at the outer level so both step 7 (inside
REM the inner) and the post-run SCP upload share the same key.
set "SSH_KEY="
if exist "%USERPROFILE%\.ssh\id_ed25519" set "SSH_KEY=%USERPROFILE%\.ssh\id_ed25519"
if not defined SSH_KEY if exist "%USERPROFILE%\.ssh\id_rsa" set "SSH_KEY=%USERPROFILE%\.ssh\id_rsa"

REM Sortable timestamp YYYYMMDD_HHMMSS via PowerShell. Cmd's %DATE% /
REM %TIME% are locale-specific and contain spaces / colons that break
REM filenames; PowerShell's Get-Date is universal.
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%I"

set "LOCAL_LOG_DIR=%TEMP%\test-hive-flow"
if not exist "%LOCAL_LOG_DIR%" mkdir "%LOCAL_LOG_DIR%"
set "LOG_NAME=test-hive-flow_%COMPUTERNAME%_!TS!.log"
set "LOCAL_LOG=%LOCAL_LOG_DIR%\%LOG_NAME%"

echo.
echo Running test, capturing output to:
echo   !LOCAL_LOG!
echo.
echo Wait for "TEST COMPLETE" before closing this window.
echo.

call "%~f0" --inner > "!LOCAL_LOG!" 2>&1
set "INNER_EXIT=!ERRORLEVEL!"

echo.
echo ===================================================================
echo  Inner test finished (exit !INNER_EXIT!). Replaying log on screen.
echo ===================================================================
type "!LOCAL_LOG!"

echo.
echo ===================================================================
echo  Uploading log to Hive Quobyte
echo ===================================================================
echo.

if not defined SSH_KEY (
    echo No SSH key at %USERPROFILE%\.ssh\ - log NOT uploaded.
    echo Local path: !LOCAL_LOG!
    echo.
    echo To enable auto-upload on next run, copy your master key once:
    echo   copy "^<quobyte^>\proteomics-grp\brett\.tmp_keys\id_ed25519" ^
"%USERPROFILE%\.ssh\id_ed25519"
    echo.
    goto done
)

where ssh >nul 2>&1
if errorlevel 1 (
    echo OpenSSH client not on PATH. Log NOT uploaded.
    echo Local path: !LOCAL_LOG!
    goto done
)

ssh -i "!SSH_KEY!" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new !HIVE_USER!@!HIVE_HOST! "mkdir -p !HIVE_LOG_DIR!"
if errorlevel 1 (
    echo SSH connect failed. Log NOT uploaded. Local path: !LOCAL_LOG!
    goto done
)

scp -i "!SSH_KEY!" -o BatchMode=yes "!LOCAL_LOG!" !HIVE_USER!@!HIVE_HOST!:!HIVE_LOG_DIR!/!LOG_NAME!
if errorlevel 1 (
    echo SCP upload failed. Local path: !LOCAL_LOG!
    goto done
)

echo Uploaded to:
echo   !HIVE_LOG_DIR!/!LOG_NAME!
echo.
echo Visible from a Mac with Quobyte mounted at:
echo   /Volumes/proteomics-grp/STAN/test_logs/!LOG_NAME!

:done
echo.
echo ===================================================================
echo  TEST COMPLETE
echo ===================================================================
echo Local log: !LOCAL_LOG!
echo.
pause
exit /b 0


REM ----- Inner test pass ----------------------------------------------
:inner

set "STAN_DIR=%USERPROFILE%\STAN"
set "STAN_VENV=%STAN_DIR%\venv"
if not exist "%STAN_VENV%\Scripts\stan.exe" (
    set "STAN_DIR=%USERPROFILE%\.stan"
    set "STAN_VENV=%STAN_DIR%\venv"
)

if not exist "%STAN_VENV%\Scripts\stan.exe" (
    echo.
    echo ERROR: no STAN venv found at either:
    echo   %USERPROFILE%\STAN\venv\Scripts\stan.exe
    echo   %USERPROFILE%\.stan\venv\Scripts\stan.exe
    echo.
    echo Run stan.bat first to install STAN, then re-run this test.
    exit /b 1
)

set "STAN_PIP=%STAN_VENV%\Scripts\pip.exe"
set "STAN_EXE=%STAN_VENV%\Scripts\stan.exe"
set "STAN_PY=%STAN_VENV%\Scripts\python.exe"

echo.
echo ===================================================================
echo  Computer: %COMPUTERNAME%   User: %USERNAME%
echo  STAN venv: %STAN_VENV%
echo ===================================================================

echo.
echo ===================================================================
echo  STEP 1/7  Installing STAN v0.2.318+ from GitHub main
echo ===================================================================
echo.

REM Pre-install version. Failed silently on the Exploris 480
REM 2026-05-07 because pip with --quiet hid the lock contention from a
REM running stan.exe; the version stayed at 0.2.315 with no errorlevel
REM signal. Now we capture before/after and warn loudly if they match.
for /f "delims=" %%V in ('"%STAN_EXE%" version 2^>nul') do set "VERSION_BEFORE=%%V"

REM --force-reinstall bulldozes ~tan-proteomics corruption left over
REM from prior partial upgrades. No --quiet so any failure (lock,
REM permission, network) is visible in the log.
"%STAN_PIP%" install --upgrade --force-reinstall "stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed. If the message above mentions
    echo "Permission denied" or "in use", close any running stan.exe
    echo / stan dashboard windows and re-run.
    exit /b 1
)

for /f "delims=" %%V in ('"%STAN_EXE%" version 2^>nul') do set "VERSION_AFTER=%%V"
echo.
echo Before: !VERSION_BEFORE!
echo After:  !VERSION_AFTER!
if "!VERSION_BEFORE!"=="!VERSION_AFTER!" (
    echo.
    echo WARNING: version did not change. Likely cause: a running
    echo stan.exe process was holding the venv files open. Close
    echo any stan watch / stan dashboard windows and re-run.
)

echo.
echo ===================================================================
echo  STEP 2/7  Installed version
echo ===================================================================
echo.
"%STAN_EXE%" version

echo.
echo ===================================================================
echo  STEP 3/7  New CLI commands
echo ===================================================================
echo.
echo --- stan hive-process ---
"%STAN_EXE%" hive-process --help
echo.
echo --- stan hive-dispatch ---
"%STAN_EXE%" hive-dispatch --help

echo.
echo ===================================================================
echo  STEP 4/7  Default dispatcher config template
echo ===================================================================
echo.
"%STAN_EXE%" hive-dispatch --print-default-config

echo.
echo ===================================================================
echo  STEP 5/7  Synthetic dispatcher dry-run (local fixture)
echo ===================================================================
echo.

set "TEST_DIR=%TEMP%\stan_hive_test"
if exist "%TEST_DIR%" rmdir /s /q "%TEST_DIR%"
mkdir "%TEST_DIR%"
mkdir "%TEST_DIR%\watch\timsTOF\HeLa_QC.d"
type nul > "%TEST_DIR%\watch\timsTOF\HeLa_QC.d\analysis.tdf"
mkdir "%TEST_DIR%\watch\lumos"
type nul > "%TEST_DIR%\watch\lumos\HeLa_50ng_run01.raw"
type nul > "%TEST_DIR%\watch\lumos\notqc_random.raw"
type nul > "%TEST_DIR%\watch\lumos\inflight.raw.partial"
mkdir "%TEST_DIR%\out"
mkdir "%TEST_DIR%\sbatch_logs"

set "TEST_YML=%TEST_DIR%\dispatch.yml"
> "%TEST_YML%" echo db_path: %TEST_DIR%\test.db
>> "%TEST_YML%" echo out_root: %TEST_DIR%\out
>> "%TEST_YML%" echo sbatch_log_dir: %TEST_DIR%\sbatch_logs
>> "%TEST_YML%" echo dispatch_log_dir: %TEST_DIR%\dispatch_logs
>> "%TEST_YML%" echo stan_venv: %STAN_VENV%
>> "%TEST_YML%" echo instruments:
>> "%TEST_YML%" echo   - name: timsTOF HT
>> "%TEST_YML%" echo     family: timsTOF
>> "%TEST_YML%" echo     vendor: bruker
>> "%TEST_YML%" echo     watch_dir: %TEST_DIR%\watch\timsTOF
>> "%TEST_YML%" echo   - name: Orbitrap Fusion Lumos
>> "%TEST_YML%" echo     family: Lumos
>> "%TEST_YML%" echo     vendor: thermo
>> "%TEST_YML%" echo     watch_dir: %TEST_DIR%\watch\lumos

echo Wrote %TEST_YML%
echo.
type "%TEST_YML%"
echo.
echo Fixture layout:
dir /s /b "%TEST_DIR%\watch"
echo.
echo --- stan hive-dispatch --dry-run ---
"%STAN_EXE%" hive-dispatch --config "%TEST_YML%" --dry-run

echo.
echo ===================================================================
echo  STEP 6/7  Sample rendered SLURM script
echo ===================================================================
echo.

"%STAN_PY%" -c "from pathlib import Path; from stan.community.scripts.dispatch_hive import _render_sbatch, _load_config; cfg = _load_config(Path(r'%TEST_YML%')); inst = cfg['instruments'][0]; raw = Path(r'%TEST_DIR%\watch\timsTOF\HeLa_QC.d'); print(_render_sbatch(raw, inst, cfg))"

echo.
echo ===================================================================
echo  STEP 7/7  OPTIONAL Hive-side test (requires SSH key)
echo ===================================================================
echo.

if not defined SSH_KEY (
    echo No SSH key found in %USERPROFILE%\.ssh\.
    echo.
    echo To enable, copy Brett's master key from Quobyte once:
    echo   copy "^<quobyte^>\proteomics-grp\brett\.tmp_keys\id_ed25519" ^
"%USERPROFILE%\.ssh\id_ed25519"
    echo   icacls "%USERPROFILE%\.ssh\id_ed25519" /inheritance:r ^
/grant:r "%USERNAME%:R"
    echo.
    echo Skipping Hive test.
    goto inner_done
)

where ssh >nul 2>&1
if errorlevel 1 (
    echo OpenSSH client not on PATH. Skipping.
    goto inner_done
)

echo Found SSH key at %SSH_KEY%
echo Connecting to %HIVE_USER%@%HIVE_HOST% ...
echo.

ssh -i "%SSH_KEY%" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new %HIVE_USER%@%HIVE_HOST% "echo Connected as $(whoami) on $(hostname); ls -d %HIVE_VENV% 2>/dev/null || echo MISSING_VENV"
if errorlevel 1 (
    echo.
    echo SSH failed. Verify your key is authorized on Hive.
    goto inner_done
)

echo.
echo --- Updating STAN on the Hive shared venv ---
ssh -i "%SSH_KEY%" -o BatchMode=yes %HIVE_USER%@%HIVE_HOST% "if [ -d %HIVE_VENV% ]; then %HIVE_VENV%/bin/pip install --upgrade --quiet --no-input 'stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip' && %HIVE_VENV%/bin/stan version; else echo SKIP: %HIVE_VENV% missing - create with: python3 -m venv %HIVE_VENV%; fi"

echo.
echo --- Hive dispatcher --dry-run (real watch dirs) ---
ssh -i "%SSH_KEY%" -o BatchMode=yes %HIVE_USER%@%HIVE_HOST% "if [ -f %HIVE_DISPATCH_YML% ]; then %HIVE_VENV%/bin/stan hive-dispatch --config %HIVE_DISPATCH_YML% --dry-run; else echo SKIP: %HIVE_DISPATCH_YML% missing - bootstrap with: %HIVE_VENV%/bin/stan hive-dispatch --print-default-config ^> %HIVE_DISPATCH_YML%; fi"

:inner_done

echo.
echo ===================================================================
echo  Inner test complete.
echo ===================================================================
echo.
echo Test fixture: %TEST_DIR%   (delete manually when done)
echo.

exit /b 0
