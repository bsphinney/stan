@echo off
REM ===================================================================
REM  test-hive-flow.bat — one-click test of the Hive-side STAN flow.
REM
REM  Does everything end-to-end so Brett doesn't have to remember any
REM  setup steps:
REM    1. Auto-installs his master SSH key from Quobyte temp drop
REM    2. Force-reinstalls latest STAN locally
REM    3. Bootstraps Hive (creates venv + dispatch.yml if missing)
REM    4. Picks the smallest QC file from the configured watch_dir
REM    5. Uploads it to Quobyte via SMB
REM    6. Submits SLURM jobs to LOW + HIGH partitions and times both
REM    7. Saves the full transcript to /quobyte/.../STAN/test_logs/
REM
REM  Hosted at /quobyte/proteomics-grp/STAN/test-hive-flow.bat. Copy
REM  to a local dir before running — Windows MOTW blocks .bat from
REM  UNC paths.
REM
REM  Requires:
REM    - STAN already installed locally (run stan.bat first if not)
REM    - Quobyte mounted at Y:\ (Brett's lab convention)
REM    - Hive temp key drop at
REM      Y:\proteomics-grp\brett\.tmp_keys\id_ed25519
REM      (cleaned up after rollout via:
REM         ssh hive "rm -rf /quobyte/proteomics-grp/brett/.tmp_keys")
REM
REM  Authoring rules per CLAUDE.md: rewrite the whole file when
REM  changing it, quote every path, no PowerShell concatenation.
REM ===================================================================

setlocal enabledelayedexpansion

set "HIVE_USER=brettsp"
set "HIVE_HOST=hive.hpc.ucdavis.edu"
set "HIVE_VENV=/quobyte/proteomics-grp/brett/stan_venv"
set "HIVE_DISPATCH_YML=/quobyte/proteomics-grp/STAN/dispatch.yml"
set "HIVE_LOG_DIR=/quobyte/proteomics-grp/STAN/test_logs"
set "HIVE_BOOTSTRAP_SH=/quobyte/proteomics-grp/STAN/hive_bootstrap.sh"
REM Y:\ on instrument PCs maps to the proteomics-grp share root (NOT
REM /quobyte/), so the path is Y:\brett\..., not Y:\proteomics-grp\brett\...
set "QUOBYTE_KEY_SRC=Y:\brett\.tmp_keys\id_ed25519"

REM ----- Outer wrapper: log capture + auto-upload ---------------------
if "%1"=="--inner" goto inner

REM ---- SSH key auto-install --------------------------------------
set "SSH_KEY="
if exist "%USERPROFILE%\.ssh\id_ed25519" set "SSH_KEY=%USERPROFILE%\.ssh\id_ed25519"
if not defined SSH_KEY if exist "%USERPROFILE%\.ssh\id_rsa" set "SSH_KEY=%USERPROFILE%\.ssh\id_rsa"

if not defined SSH_KEY (
    if exist "%QUOBYTE_KEY_SRC%" (
        echo Auto-installing master SSH key from Quobyte temp drop...
        if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
        copy /y "%QUOBYTE_KEY_SRC%" "%USERPROFILE%\.ssh\id_ed25519" >nul
        if errorlevel 1 (
            echo WARNING: copy failed; SSH-dependent steps will skip.
        ) else (
            icacls "%USERPROFILE%\.ssh\id_ed25519" /inheritance:r /grant:r "%USERNAME%:R" >nul
            set "SSH_KEY=%USERPROFILE%\.ssh\id_ed25519"
            echo Key installed at %USERPROFILE%\.ssh\id_ed25519
        )
    )
)

REM ---- Build a timestamped log path on the Y: Quobyte mount ------
REM v0.2.323+: log writes directly to Y:\STAN\test_logs\ instead of
REM staging in %TEMP% then SCP-uploading at the end. Real-time
REM visibility — Brett (and Claude on the Mac side) can tail -f the
REM log on /Volumes/proteomics-grp/STAN/test_logs/ WHILE the test is
REM running, not just post-hoc. If a step hangs we can see exactly
REM where instead of waiting for the .bat to finish.
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%I"
set "LOG_NAME=test-hive-flow_%COMPUTERNAME%_!TS!.log"
set "LOG_DIR=Y:\STAN\test_logs"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_PATH=%LOG_DIR%\%LOG_NAME%"

echo.
echo Running test. Real-time log being written to:
echo   %LOG_PATH%
echo Hive mount Mac side:
echo   /Volumes/proteomics-grp/STAN/test_logs/%LOG_NAME%
echo.
echo This will take a while — pip install + Hive bootstrap +
echo SLURM jobs. Wait for "TEST COMPLETE" before closing this window.
echo.

call "%~f0" --inner > "!LOG_PATH!" 2>&1
set "INNER_EXIT=!ERRORLEVEL!"

echo.
echo ===================================================================
echo  Inner test finished (exit !INNER_EXIT!). Tail of log:
echo ===================================================================
powershell -NoProfile -Command "Get-Content -Path '%LOG_PATH%' -Tail 60"

echo.
echo ===================================================================
echo  TEST COMPLETE
echo ===================================================================
echo Log: %LOG_PATH%
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
    echo ERROR: no STAN venv found. Run stan.bat first to install.
    exit /b 1
)

set "STAN_PIP=%STAN_VENV%\Scripts\pip.exe"
set "STAN_EXE=%STAN_VENV%\Scripts\stan.exe"

echo ===================================================================
echo  Computer: %COMPUTERNAME%   User: %USERNAME%
echo  STAN venv: %STAN_VENV%
echo ===================================================================

REM ---- Step 1: install latest STAN locally ------------------------
echo.
echo ===================================================================
echo  STEP 1/4  Install latest STAN from GitHub main
echo ===================================================================
echo.

for /f "delims=" %%V in ('"%STAN_EXE%" version 2^>nul') do set "VERSION_BEFORE=%%V"

"%STAN_PIP%" install --upgrade --force-reinstall "stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
if errorlevel 1 (
    echo ERROR: pip install failed. If "Permission denied" / "in use",
    echo close any running stan watch / dashboard windows and re-run.
    exit /b 1
)

for /f "delims=" %%V in ('"%STAN_EXE%" version 2^>nul') do set "VERSION_AFTER=%%V"
echo.
echo Local: !VERSION_BEFORE! ^-^> !VERSION_AFTER!

if "!VERSION_BEFORE!"=="!VERSION_AFTER!" (
    echo.
    echo WARNING: version did not change. A running stan.exe may be
    echo holding venv files open. Close stan.bat windows + retry.
)

echo.
echo --- New CLI commands present? ---
"%STAN_EXE%" hive-process --help | findstr /R "Usage" >nul && echo OK: hive-process || echo MISSING: hive-process
"%STAN_EXE%" hive-dispatch --help | findstr /R "Usage" >nul && echo OK: hive-dispatch || echo MISSING: hive-dispatch
"%STAN_EXE%" hive-upload --help | findstr /R "Usage" >nul && echo OK: hive-upload || echo MISSING: hive-upload
"%STAN_EXE%" time-hive-partitions --help | findstr /R "Usage" >nul && echo OK: time-hive-partitions || echo MISSING: time-hive-partitions

REM ---- Step 2: bootstrap Hive (idempotent) ------------------------
echo.
echo ===================================================================
echo  STEP 2/4  Bootstrap Hive (venv + dispatch.yml)
echo ===================================================================
echo.

if not defined SSH_KEY (
    echo SSH key missing — cannot SSH to Hive. Skipping bootstrap and
    echo timing test. Local steps above already validated the install.
    exit /b 1
)

ssh -i "%SSH_KEY%" -o BatchMode=yes -o StrictHostKeyChecking=accept-new %HIVE_USER%@%HIVE_HOST% "bash -l %HIVE_BOOTSTRAP_SH%"
if errorlevel 1 (
    echo ERROR: Hive bootstrap failed. See the log above for the
    echo specific error. Skipping timing test.
    exit /b 1
)

REM ---- Step 3: pick QC file + show what will be tested ------------
echo.
echo ===================================================================
echo  STEP 3/4  Discover QC file from instruments.yml
echo ===================================================================
echo.

"%STAN_EXE%" -c-not-supported >nul 2>&1
REM Just show the user which file we'll pick. time-hive-partitions
REM picks it again internally (and uploads + submits). Single source
REM of truth — same selection logic both places.
"%STAN_VENV%\Scripts\python.exe" -c "from stan.config import load_instruments; from pathlib import Path; import re, sys; _,insts=load_instruments(); inst=insts[0] if insts else None; sys.exit('No instruments.yml entries' if not inst else 0); wd=Path(inst.get('watch_dir','')); pat=re.compile(r'(?i)(he(l[a5\d]|\d)|qc|std[_\-\s]?he)'); cands=[]; print(f'instrument: {inst.get(\"name\")}'); print(f'watch_dir:  {wd}');" 2>&1

REM ---- Step 4: time low + high partition round trips -------------
echo.
echo ===================================================================
echo  STEP 4/4  Partition timing test (low + high)
echo ===================================================================
echo.
echo This uploads ONE QC file to Hive, submits a SLURM job to the
echo LOW partition, then a second job to the HIGH partition (with
echo --force so the dispatcher dedup doesn't skip the second one),
echo and polls sacct until both end. Total time depends on Hive
echo queue load — typically 10-90 minutes.
echo.

"%STAN_EXE%" time-hive-partitions --partitions low,high
if errorlevel 1 (
    echo.
    echo time-hive-partitions reported a non-zero exit — see above
    echo for the specific failure mode.
)

echo.
echo === inner pass done ===
exit /b 0
