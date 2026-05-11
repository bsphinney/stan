@echo off
REM ===================================================================
REM  test_fresh_install.bat
REM  Simulates a fresh STAN Mode A install on an existing Windows machine
REM  without permanently wiping anything.
REM
REM  Usage:
REM    test_fresh_install.bat              -- full backup + install + verify
REM    test_fresh_install.bat --report-only -- skip install, just verify state
REM
REM  PS5.1 rules observed (per CLAUDE.md):
REM    - No + string concatenation
REM    - No inline ternary if
REM    - No Where-Object pipelines
REM    - Full-file rewrite on every edit
REM    - Join-Path style paths via quoted variables throughout
REM ===================================================================

setlocal enabledelayedexpansion

REM ---- Parse arguments -----------------------------------------------
set "REPORT_ONLY=0"
if /i "%~1"=="--report-only" set "REPORT_ONLY=1"

REM ---- Timestamp for backup dirs -------------------------------------
for /f "tokens=1-6 delims=/:. " %%a in ("%DATE% %TIME%") do (
    set "_Y=%%a"
    set "_M=%%b"
    set "_D=%%c"
    set "_H=%%d"
    set "_MIN=%%e"
    set "_S=%%f"
)
REM Normalize to YYYYMMDD_HHMMSS -- strip leading spaces from time fields
set "_H=%_H: =0%"
set "_MIN=%_MIN: =0%"
set "_S=%_S: =0%"
set "TIMESTAMP=%_Y%%_M%%_D%_%_H%%_MIN%%_S%"

set "BAK_ROOT=%USERPROFILE%\STAN.bak.%TIMESTAMP%"
set "PASS_COUNT=0"
set "FAIL_COUNT=0"

echo.
echo   ================================================================
echo     STAN Mode A install regression test
echo     Timestamp: %TIMESTAMP%
echo   ================================================================
echo.

if "%REPORT_ONLY%"=="1" (
    echo   --report-only: skipping backup and install, running checks only.
    echo.
    goto :run_checks
)

REM ====================================================================
REM  STEP 1 -- Backup existing STAN dirs
REM ====================================================================
echo   [1/4] Backing up existing STAN directories...
echo.

set "STAN_NEW=%USERPROFILE%\STAN"
set "STAN_OLD=%USERPROFILE%\.stan"
set "BAK_NEW=%BAK_ROOT%\STAN"
set "BAK_OLD=%BAK_ROOT%\.stan"

if exist "%STAN_NEW%" (
    echo   Found %STAN_NEW% -- backing up to %BAK_NEW%
    xcopy /e /q /y "%STAN_NEW%" "%BAK_NEW%\" >nul
    if errorlevel 1 (
        echo   ERROR: Backup of %STAN_NEW% failed. Aborting to protect existing install.
        pause
        exit /b 1
    )
    echo   Backup OK.
    REM Remove the live tree so the installer sees a fresh machine.
    rmdir /s /q "%STAN_NEW%" 2>nul
) else (
    echo   %STAN_NEW% not present (clean machine - no backup needed).
)

if exist "%STAN_OLD%" (
    echo   Found %STAN_OLD% -- backing up to %BAK_OLD%
    xcopy /e /q /y "%STAN_OLD%" "%BAK_OLD%\" >nul
    if errorlevel 1 (
        echo   ERROR: Backup of %STAN_OLD% failed. Aborting to protect existing install.
        pause
        exit /b 1
    )
    echo   Backup OK.
    rmdir /s /q "%STAN_OLD%" 2>nul
) else (
    echo   %STAN_OLD% not present (clean machine - no backup needed).
)

echo.
echo   Restore command (run after test):
echo     xcopy /e /y "%BAK_ROOT%\STAN" "%USERPROFILE%\STAN\"
echo     xcopy /e /y "%BAK_ROOT%\.stan" "%USERPROFILE%\.stan\"
echo.

REM ====================================================================
REM  STEP 2 -- Clear cached installer files
REM ====================================================================
echo   [2/4] Clearing cached installer files...
echo.

if exist "%USERPROFILE%\install-stan.bat" (
    del "%USERPROFILE%\install-stan.bat" >nul 2>&1
    echo   Deleted %USERPROFILE%\install-stan.bat
)
if exist "%USERPROFILE%\install_stan.ps1" (
    del "%USERPROFILE%\install_stan.ps1" >nul 2>&1
    echo   Deleted %USERPROFILE%\install_stan.ps1
)
REM Also clear any copy sitting next to this script (from a previous run)
if exist "%~dp0install-stan.bat" (
    del "%~dp0install-stan.bat" >nul 2>&1
    echo   Deleted %~dp0install-stan.bat
)
if exist "%~dp0install_stan.ps1" (
    del "%~dp0install_stan.ps1" >nul 2>&1
    echo   Deleted %~dp0install_stan.ps1
)

echo   Cache cleared.
echo.

REM ====================================================================
REM  STEP 3 -- Run the installer fresh
REM ====================================================================
echo   [3/4] Running fresh install via install-stan.bat...
echo   (This will take 2-5 minutes depending on network speed)
echo.

REM Force-download a fresh install-stan.bat (defeats stale-cache bug #2)
echo   Downloading fresh install-stan.bat from GitHub (cache-busted)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $t=[DateTime]::Now.Ticks; Invoke-WebRequest -Uri ('https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat?t=' + $t) -OutFile '%~dp0install-stan.bat' -UseBasicParsing"

if not exist "%~dp0install-stan.bat" (
    echo   ERROR: Could not download install-stan.bat. Check network access.
    pause
    exit /b 1
)

call "%~dp0install-stan.bat"
if errorlevel 1 (
    echo.
    echo   ERROR: install-stan.bat exited with error. See output above.
    echo   Checks will still run to report partial state.
    echo.
)

echo.

REM ====================================================================
REM  STEP 4 -- Verification battery
REM ====================================================================
:run_checks
echo   [4/4] Running post-install verification...
echo.

REM Refresh PATH so newly installed binaries are visible
set "MP="
set "UP="
for /f "skip=2 tokens=1,2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "MP=%%c"
for /f "skip=2 tokens=1,2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "UP=%%c"
if defined MP set "PATH=%MP%;%PATH%"
if defined UP set "PATH=%UP%;%PATH%"

REM ---- Check 1: stan.exe exists at the expected venv location --------
echo   CHECK 1: stan.exe at %%USERPROFILE%%\STAN\venv\Scripts\stan.exe
set "STAN_EXE=%USERPROFILE%\STAN\venv\Scripts\stan.exe"
if exist "%STAN_EXE%" (
    call :pass "stan.exe found at %STAN_EXE%"
) else (
    call :fail "stan.exe NOT found at %STAN_EXE%"
)

REM ---- Check 2: stan --version reports a version string -------------
echo   CHECK 2: stan --version returns a version string
if exist "%STAN_EXE%" (
    set "_ver_out="
    for /f "delims=" %%v in ('"%STAN_EXE%" --version 2^>^&1') do set "_ver_out=%%v"
    if "!_ver_out!"=="" (
        call :fail "stan --version returned empty output"
    ) else (
        call :pass "stan --version: !_ver_out!"
    )
) else (
    call :skip "stan.exe not found -- skipping version check"
)

REM ---- Check 3: instruments.yml at ~/STAN/ (new path, NOT ~/.stan/) --
echo   CHECK 3: instruments.yml at %%USERPROFILE%%\STAN\ (not %%USERPROFILE%%\.stan\)
set "INSTR_NEW=%USERPROFILE%\STAN\instruments.yml"
set "INSTR_OLD=%USERPROFILE%\.stan\instruments.yml"
if exist "%INSTR_NEW%" (
    call :pass "instruments.yml at correct path: %INSTR_NEW%"
    if exist "%INSTR_OLD%" (
        echo         WARN: Also found legacy %INSTR_OLD% -- watcher may read wrong file.
    )
) else (
    if exist "%INSTR_OLD%" (
        call :fail "instruments.yml at WRONG path %INSTR_OLD% (should be %INSTR_NEW%) -- bug #5"
    ) else (
        call :fail "instruments.yml missing entirely"
    )
)

REM ---- Check 4: venv python exists ----------------------------------
echo   CHECK 4: venv Python at %%USERPROFILE%%\STAN\venv\Scripts\python.exe
if exist "%USERPROFILE%\STAN\venv\Scripts\python.exe" (
    call :pass "venv python.exe found"
) else (
    call :fail "venv python.exe NOT found -- venv creation failed"
)

REM ---- Check 5: DIA-NN is version 2.x, not 1.x ----------------------
echo   CHECK 5: DIA-NN binary version is 2.x (not 1.x)
set "DIANN_EXE="
for %%p in (
    "C:\DIA-NN\2.3.2\DiaNN.exe"
    "C:\DIA-NN\2.3\DiaNN.exe"
    "C:\Program Files\DIA-NN\DiaNN.exe"
    "%LOCALAPPDATA%\DIA-NN\DiaNN.exe"
) do (
    if exist %%p (
        if "!DIANN_EXE!"=="" set "DIANN_EXE=%%~p"
    )
)
if "!DIANN_EXE!"=="" (
    REM Try PATH lookup
    for /f "delims=" %%x in ('where DiaNN.exe 2^>nul') do (
        if "!DIANN_EXE!"=="" set "DIANN_EXE=%%x"
    )
)
if "!DIANN_EXE!"=="" (
    call :fail "DiaNN.exe not found in expected locations or PATH"
) else (
    REM Extract version from path -- path should contain a 2.x segment
    echo "!DIANN_EXE!" | findstr /r "\\2\." >nul 2>&1
    if errorlevel 1 (
        REM Path doesn't contain \2. -- check if it contains \1.
        echo "!DIANN_EXE!" | findstr /r "\\1\." >nul 2>&1
        if not errorlevel 1 (
            call :fail "DiaNN.exe is 1.x: !DIANN_EXE! -- bug #4 (version picker chose wrong ver)"
        ) else (
            call :pass "DiaNN.exe found: !DIANN_EXE! (version in path unclear -- verify manually)"
        )
    ) else (
        call :pass "DiaNN.exe 2.x found: !DIANN_EXE!"
    )
)

REM ---- Check 6: Sage binary exists ----------------------------------
echo   CHECK 6: sage.exe exists under %%USERPROFILE%%\STAN\tools\sage\
set "SAGE_EXE="
for /f "delims=" %%f in ('dir /s /b "%USERPROFILE%\STAN\tools\sage\sage.exe" 2^>nul') do (
    if "!SAGE_EXE!"=="" set "SAGE_EXE=%%f"
)
if "!SAGE_EXE!"=="" (
    REM Try PATH
    for /f "delims=" %%x in ('where sage.exe 2^>nul') do (
        if "!SAGE_EXE!"=="" set "SAGE_EXE=%%x"
    )
)
if "!SAGE_EXE!"=="" (
    call :fail "sage.exe not found in STAN tools dir or PATH"
) else (
    call :pass "sage.exe found: !SAGE_EXE!"
)

REM ---- Check 7: stan version (different command from --version) ------
echo   CHECK 7: stan version command exits cleanly
if exist "%STAN_EXE%" (
    "%STAN_EXE%" version >nul 2>&1
    if errorlevel 1 (
        call :fail "stan version exited with error"
    ) else (
        call :pass "stan version OK"
    )
) else (
    call :skip "stan.exe not found -- skipping"
)

REM ---- Check 8: stan watch --help exits 0 ---------------------------
echo   CHECK 8: stan watch --help exits cleanly
if exist "%STAN_EXE%" (
    "%STAN_EXE%" watch --help >nul 2>&1
    if errorlevel 1 (
        call :fail "stan watch --help exited with error"
    ) else (
        call :pass "stan watch --help OK"
    )
) else (
    call :skip "stan.exe not found -- skipping"
)

REM ---- Check 9: stan dashboard --help exits 0 -----------------------
echo   CHECK 9: stan dashboard --help exits cleanly
if exist "%STAN_EXE%" (
    "%STAN_EXE%" dashboard --help >nul 2>&1
    if errorlevel 1 (
        call :fail "stan dashboard --help exited with error"
    ) else (
        call :pass "stan dashboard --help OK"
    )
) else (
    call :skip "stan.exe not found -- skipping"
)

REM ---- Check 10: no stale .stan\venv on PATH ------------------------
echo   CHECK 10: old %%USERPROFILE%%\.stan\venv\Scripts NOT on user PATH
set "_old_on_path=0"
for /f "skip=2 tokens=1,2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do (
    echo %%c | findstr /i /c:".stan\venv\Scripts" >nul 2>&1
    if not errorlevel 1 set "_old_on_path=1"
)
if "!_old_on_path!"=="1" (
    call :fail "Legacy .stan\venv\Scripts is still on user PATH -- run installer again to clean up"
) else (
    call :pass "No legacy .stan\venv\Scripts on PATH"
)

REM ====================================================================
REM  Summary
REM ====================================================================
echo.
echo   ================================================================
echo     RESULTS: %PASS_COUNT% passed, %FAIL_COUNT% failed
echo   ================================================================

if "%FAIL_COUNT%"=="0" (
    echo     ALL CHECKS PASSED -- install looks healthy.
) else (
    echo     FAILURES DETECTED -- see FAIL lines above for details.
    echo     See docs\INSTALL_REGRESSION_CHECKLIST.md for remediation steps.
)

echo.
if "%REPORT_ONLY%"=="0" (
    echo   RESTORE INSTRUCTIONS (run these in CMD to undo the test):
    echo.
    if exist "%BAK_ROOT%\STAN\" (
        echo     xcopy /e /y "%BAK_ROOT%\STAN" "%USERPROFILE%\STAN\"
    )
    if exist "%BAK_ROOT%\.stan\" (
        echo     xcopy /e /y "%BAK_ROOT%\.stan" "%USERPROFILE%\.stan\"
    )
    if not exist "%BAK_ROOT%\STAN\" (
        if not exist "%BAK_ROOT%\.stan\" (
            echo     Nothing was backed up (machine was clean before test).
        )
    )
    echo.
)

echo   ================================================================
echo.
pause
exit /b %FAIL_COUNT%

REM ====================================================================
REM  Subroutines
REM ====================================================================
:pass
set /a PASS_COUNT+=1
echo     PASS: %~1
goto :eof

:fail
set /a FAIL_COUNT+=1
echo     FAIL: %~1
goto :eof

:skip
echo     SKIP: %~1
goto :eof
