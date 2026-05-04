@echo off
REM ===================================================================
REM  stan.bat — single entry point for STAN
REM
REM  Double-click and walk away. No menus, no choices.
REM
REM  First run on a new PC:
REM    - detects no venv present
REM    - calls install-stan.bat (downloads + runs install_stan.ps1 from
REM      GitHub, which creates the venv, pip-installs STAN, then runs
REM      `stan init` interactive wizard for instruments.yml)
REM
REM  Every subsequent run:
REM    - launches dashboard server in a separate window
REM    - opens http://localhost:8421 in default browser
REM    - enters a supervisor loop that relaunches `stan watch` on
REM      crash and runs pending updates between restarts
REM
REM  Power-user actions (baseline, submit-all, backfills, send-command,
REM  godmode flows) are CLI subcommands of the `stan` exe directly.
REM  This launcher never surfaces them — that is by design so routine
REM  labs see one icon and one icon only.
REM
REM  Authoring rules (PS 5.1 / cmd.exe parsing traps are real):
REM    - rewrite this entire file when changing it; never patch a line
REM    - quote every path
REM    - keep PowerShell isolated to install-stan.bat / update-stan.bat
REM      so this file never trips an execution-policy lockdown
REM ===================================================================

setlocal enabledelayedexpansion

set "STAN_DIR=%USERPROFILE%\STAN"
set "STAN_EXE=%STAN_DIR%\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" set "STAN_EXE=%USERPROFILE%\.stan\venv\Scripts\stan.exe"

REM ---- First-time install detection ----------------------------------
if exist "%STAN_EXE%" goto run

echo.
echo ===================================================================
echo  STAN first-time setup
echo ===================================================================
echo.
echo  No STAN install detected on this machine. I will run the
echo  installer now. This downloads STAN from GitHub, creates a
echo  Python venv, and then walks you through the instrument
echo  configuration wizard.
echo.
echo  Approximately 5-10 minutes. Stay near the keyboard for the
echo  config questions in the wizard.
echo.
pause

set "INSTALLER=%~dp0install-stan.bat"
if not exist "%INSTALLER%" set "INSTALLER=%USERPROFILE%\Downloads\install-stan.bat"
if not exist "%INSTALLER%" (
    echo ERROR: install-stan.bat not found next to stan.bat or in
    echo        %USERPROFILE%\Downloads. Re-download STAN from
    echo        https://github.com/bsphinney/stan and try again.
    pause
    exit /b 1
)

call "%INSTALLER%"

REM Re-resolve STAN_EXE after install — the installer may have
REM written to either of the two known venv roots.
set "STAN_EXE=%STAN_DIR%\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" set "STAN_EXE=%USERPROFILE%\.stan\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" (
    echo.
    echo ERROR: install completed but stan.exe still not found.
    echo        Check %TEMP%\stan_install.log for what went wrong,
    echo        or contact bsphinney@ucdavis.edu.
    pause
    exit /b 1
)

echo.
echo ===================================================================
echo  Setup complete. Starting STAN now.
echo ===================================================================
echo.

REM ---- Daily run ------------------------------------------------------
:run

echo [%DATE% %TIME%] Launching STAN dashboard...
start "STAN Dashboard" cmd /c ""%STAN_EXE%" dashboard"

REM Give the dashboard server a moment to bind, then open the browser.
timeout /t 4 /nobreak >nul
start "" http://localhost:8421

REM ---- Supervisor loop ------------------------------------------------
REM Mirrors the proven start_stan_loop.bat flow: a crash triggers a
REM restart within 5s, and a remote update_stan command writes
REM update_pending.flag which is consumed here BETWEEN watcher
REM restarts (so pip never races stan.exe file locks).
set "UPDATE_FLAG=%STAN_DIR%\update_pending.flag"
set "UPDATER=%~dp0update-stan.bat"
if not exist "%UPDATER%" set "UPDATER=%USERPROFILE%\Downloads\update-stan.bat"
if not exist "%UPDATER%" set "UPDATER=%STAN_DIR%\update-stan.bat"

echo [%DATE% %TIME%] STAN watcher starting (auto-restart on crash).
echo                  Close this window to stop STAN.
echo.

:loop
if exist "%UPDATE_FLAG%" (
    echo [%DATE% %TIME%] update_pending.flag detected — running updater
    if exist "%UPDATER%" (
        call "%UPDATER%"
    ) else (
        echo    WARN: update-stan.bat not found; skipping update
    )
    del "%UPDATE_FLAG%" 2>nul
    echo [%DATE% %TIME%] update complete — relaunching watcher
)

"%STAN_EXE%" watch
echo.
echo [%DATE% %TIME%] stan watch exited (code %ERRORLEVEL%); relaunching in 5s
timeout /t 5 /nobreak >nul
goto loop
