@echo off
REM ===================================================================
REM  test-hive-flow.bat — install STAN v0.2.318+ and smoke-test the
REM  new Hive-side CLI surface (stan hive-process + stan hive-dispatch).
REM
REM  Hosted at /quobyte/proteomics-grp/STAN/test-hive-flow.bat so any
REM  instrument PC with the Quobyte client can grab it directly.
REM
REM  Steps:
REM    1-6  Local install + smoke test on this PC (no Hive needed).
REM    7    Optional Hive-side install + dry-run via SSH. Only runs
REM         if you have an SSH key at %USERPROFILE%\.ssh\. Skips
REM         gracefully and prints manual instructions otherwise.
REM
REM  SSH key handling: this file does NOT bundle a private key.
REM  Anyone reading the file would get your Hive credentials; that's
REM  a hard no even on lab-internal Quobyte. The .bat uses whatever
REM  key is already at %USERPROFILE%\.ssh\id_ed25519 or id_rsa on
REM  this instrument PC. If you've never SSH'd from this box to Hive,
REM  copy your key over once (e.g. via OpenSSH on your Mac:
REM      scp ~/.ssh/id_ed25519 brettsp@<this-pc>:.ssh/
REM  ) and re-run.
REM
REM  Authoring rules per CLAUDE.md: rewrite the whole file when changing
REM  it, quote every path, no PowerShell concatenation, no inline ternary.
REM ===================================================================

setlocal enabledelayedexpansion

set "HIVE_USER=brettsp"
set "HIVE_HOST=hive.hpc.ucdavis.edu"
set "HIVE_VENV=/quobyte/proteomics-grp/brett/stan_venv"
set "HIVE_DISPATCH_YML=/quobyte/proteomics-grp/STAN/dispatch.yml"

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
    echo.
    pause
    exit /b 1
)

set "STAN_PIP=%STAN_VENV%\Scripts\pip.exe"
set "STAN_EXE=%STAN_VENV%\Scripts\stan.exe"
set "STAN_PY=%STAN_VENV%\Scripts\python.exe"

echo.
echo ===================================================================
echo  STEP 1/7  Installing STAN v0.2.318+ from GitHub main
echo ===================================================================
echo.

"%STAN_PIP%" install --upgrade --quiet --no-input "stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed. Check network + GitHub reachability.
    echo.
    pause
    exit /b 1
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

REM Build a minimal dispatch.yml. qc_pattern + slurm fall back to
REM defaults inside _load_config so we can omit them here.
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
echo This is what would land on Hive for one Bruker .d. Inspect the
echo #SBATCH headers + argv line — paths, --instrument, --family must
echo all be quoted correctly.
echo.

"%STAN_PY%" -c "from pathlib import Path; from stan.community.scripts.dispatch_hive import _render_sbatch, _load_config; cfg = _load_config(Path(r'%TEST_YML%')); inst = cfg['instruments'][0]; raw = Path(r'%TEST_DIR%\watch\timsTOF\HeLa_QC.d'); print(_render_sbatch(raw, inst, cfg))"

echo.
echo ===================================================================
echo  STEP 7/7  OPTIONAL Hive-side test (requires SSH key)
echo ===================================================================
echo.

REM Resolve SSH key with this precedence:
REM   1. existing local %USERPROFILE%\.ssh\id_ed25519
REM   2. existing local %USERPROFILE%\.ssh\id_rsa
REM   3. copy from Quobyte temp drop (only if .bat ran from Quobyte STAN dir)
REM      Brett uploads the key to /quobyte/proteomics-grp/brett/.tmp_keys/
REM      with chmod 600 + dir 700 so only his UID can read it. Cleanup
REM      command after the rollout:
REM        ssh hive "rm -rf /quobyte/proteomics-grp/brett/.tmp_keys"
set "SSH_KEY="
if exist "%USERPROFILE%\.ssh\id_ed25519" set "SSH_KEY=%USERPROFILE%\.ssh\id_ed25519"
if not defined SSH_KEY if exist "%USERPROFILE%\.ssh\id_rsa" set "SSH_KEY=%USERPROFILE%\.ssh\id_rsa"

if not defined SSH_KEY (
    REM Try the Quobyte temp drop. %~dp0 is this .bat's directory; if
    REM it lives at \\quobyte\proteomics-grp\STAN\, then ..\brett\
    REM resolves to /quobyte/proteomics-grp/brett/. Resolves to nothing
    REM useful if Brett copied the .bat to a local dir before running.
    set "QUOBYTE_KEY=%~dp0..\brett\.tmp_keys\id_ed25519"
    if exist "!QUOBYTE_KEY!" (
        echo Found Brett's temp key on Quobyte. Copying to local
        echo %USERPROFILE%\.ssh\ for this and future runs.
        if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
        copy /y "!QUOBYTE_KEY!" "%USERPROFILE%\.ssh\id_ed25519" >nul
        if errorlevel 1 (
            echo ERROR: copy failed. Skipping Hive test.
            goto skip_hive
        )
        REM Lock down the local copy. icacls strips inherited perms
        REM and grants read only to the current user.
        icacls "%USERPROFILE%\.ssh\id_ed25519" /inheritance:r /grant:r "%USERNAME%:R" >nul
        set "SSH_KEY=%USERPROFILE%\.ssh\id_ed25519"
        echo Key installed at %USERPROFILE%\.ssh\id_ed25519
        echo.
        echo NOTE: this is Brett's master Hive key. After the
        echo rollout is done across all instrument PCs, clean up
        echo the Quobyte copy with:
        echo   ssh hive "rm -rf /quobyte/proteomics-grp/brett/.tmp_keys"
        echo.
    )
)

if not defined SSH_KEY (
    echo No SSH key found in %USERPROFILE%\.ssh\ and no Quobyte temp
    echo drop reachable from %~dp0.
    echo.
    echo Either run this .bat directly from the Quobyte STAN share
    echo (\\^<quobyte^>\proteomics-grp\STAN\test-hive-flow.bat) so it
    echo can find the temp key, or copy your key from your Mac one
    echo time:
    echo   scp ~/.ssh/id_ed25519 ^<this-pc-username^>@^<this-pc-host^>:.ssh/
    echo.
    echo Skipping Hive test.
    goto skip_hive
)

where ssh >nul 2>&1
if errorlevel 1 (
    echo OpenSSH client not on PATH. Win10+ ships with it; install
    echo "OpenSSH Client" via Settings -^> Optional Features. Skipping.
    goto skip_hive
)

echo Found SSH key at %SSH_KEY%
echo Connecting to %HIVE_USER%@%HIVE_HOST% ...
echo.

REM Step A: prove we can reach Hive at all.
ssh -i "%SSH_KEY%" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new %HIVE_USER%@%HIVE_HOST% "echo Connected as $(whoami) on $(hostname); ls -d %HIVE_VENV% 2>/dev/null || echo MISSING_VENV"
if errorlevel 1 (
    echo.
    echo SSH failed. Verify your key is authorized in your Hive
    echo ~/.ssh/authorized_keys file and that the username
    echo "%HIVE_USER%" matches your account.
    goto skip_hive
)

echo.
echo --- Updating STAN on the Hive shared venv ---
echo (this only runs if %HIVE_VENV% already exists; if MISSING_VENV
echo printed above, create it once with:
echo    ssh hive
echo    python3 -m venv %HIVE_VENV%
echo    %HIVE_VENV%/bin/pip install --upgrade pip
echo )
ssh -i "%SSH_KEY%" -o BatchMode=yes %HIVE_USER%@%HIVE_HOST% "if [ -d %HIVE_VENV% ]; then %HIVE_VENV%/bin/pip install --upgrade --quiet --no-input 'stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip' && %HIVE_VENV%/bin/stan version; else echo SKIP: venv missing; fi"

echo.
echo --- Hive dispatcher --dry-run (real watch dirs) ---
echo (skips silently if dispatch.yml hasn't been bootstrapped yet)
ssh -i "%SSH_KEY%" -o BatchMode=yes %HIVE_USER%@%HIVE_HOST% "if [ -f %HIVE_DISPATCH_YML% ]; then %HIVE_VENV%/bin/stan hive-dispatch --config %HIVE_DISPATCH_YML% --dry-run; else echo SKIP: %HIVE_DISPATCH_YML% missing - bootstrap with: %HIVE_VENV%/bin/stan hive-dispatch --print-default-config ^> %HIVE_DISPATCH_YML%; fi"

:skip_hive

echo.
echo ===================================================================
echo  Test complete.
echo ===================================================================
echo.
echo Local checks: scanned=3 submitted=2 in step 5 means the .partial
echo was correctly skipped. Step 6 SLURM script should have all paths
echo quoted. If step 7 ran, you saw what the Hive side will actually
echo dispatch given current /quobyte/.../hela_qcs/ contents.
echo.
echo Test fixture: %TEST_DIR%   (delete manually when done)
echo.
pause
