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

REM ---- Self-update stan.bat itself from GitHub ------------------------
REM v0.2.302: pip-install only refreshes the Python package; .bat files
REM on the desktop stay frozen at whatever was last downloaded. Without
REM this step, every change to stan.bat (e.g. the auto-update step
REM added in v0.2.297) requires every operator to re-download the file
REM by hand — which Brett's timsTOF didn't do, so it stayed on the
REM v0.2.295 stan.bat that doesn't auto-update at all.
REM
REM Strategy: download main/stan.bat to a temp file. Hash both. If
REM different, copy temp over self, spawn a fresh cmd window with the
REM new file, and exit cleanly. cmd.exe normally can't overwrite a
REM running .bat, but Windows allows the copy as long as it's not
REM exclusively locked (which cmd doesn't do for .bat reads). We
REM relaunch via "start" + "exit" so the new cmd reads the fresh file
REM from the start instead of trying to resume mid-script.
REM
REM Network failures fall through silently. Hash failures fall through.
REM Only an actual content diff triggers the replace+relaunch.
set "STAN_BAT_NEW=%TEMP%\stan_new_%RANDOM%%RANDOM%.bat"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri ('https://raw.githubusercontent.com/bsphinney/stan/main/stan.bat?t=' + [DateTime]::Now.Ticks) -OutFile '%STAN_BAT_NEW%' -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop; $n = (Get-FileHash -Path '%STAN_BAT_NEW%' -Algorithm SHA256).Hash; $c = (Get-FileHash -Path '%~f0' -Algorithm SHA256).Hash; if ($n -ne $c) { exit 100 } else { exit 0 } } catch { exit 200 }"
if errorlevel 200 (
    echo [%DATE% %TIME%] Self-update check skipped: couldn't reach GitHub.
    if exist "%STAN_BAT_NEW%" del "%STAN_BAT_NEW%" 2>nul
    goto after_self_update
)
if errorlevel 100 (
    echo [%DATE% %TIME%] Found newer stan.bat on GitHub — refreshing this file.
    copy /y "%STAN_BAT_NEW%" "%~f0" >nul
    del "%STAN_BAT_NEW%" 2>nul
    if errorlevel 1 (
        echo [%DATE% %TIME%] WARN: couldn't overwrite stan.bat - continuing.
        goto after_self_update
    )
    echo [%DATE% %TIME%] stan.bat refreshed — relaunching with new version.
    start "" cmd /c "\"%~f0\""
    exit /b 0
)
del "%STAN_BAT_NEW%" 2>nul
:after_self_update

REM ---- Schedule pip update via the supervisor flag --------------------
REM v0.2.313: write update_pending.flag instead of pip-installing
REM inline. Pre-fix, an inline pip install --upgrade ran on every
REM launch — but if a previous stan watch process was still running
REM (operator clicked stan.bat with the old window still open), pip
REM would try to overwrite stan.exe while the venv held it open and
REM leave the package in a half-state. Symptom: ModuleNotFoundError
REM crash loop in the watcher window. Brett's Exploris 480 hit this
REM 2026-05-05.
REM
REM The supervisor loop below already has a safe pip-install path:
REM it consumes update_pending.flag BEFORE running stan watch, so
REM pip runs with no stan.exe handle open. Funnel both fresh-launch
REM and remote update_stan commands through that same path so there
REM is exactly one place pip runs and exactly one set of safety
REM guarantees to maintain.
echo [%DATE% %TIME%] Scheduling STAN package update on next watcher restart.
if not exist "%STAN_DIR%" mkdir "%STAN_DIR%" 2>nul
echo. > "%STAN_DIR%\update_pending.flag"
echo.

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
REM
REM v0.2.313: the flag-handler runs a minimal pip install in-place
REM (NOT update-stan.bat / update_stan.ps1, which is a kitchen-sink
REM script that also spawns its own watcher + dashboard + overnight
REM backfill — those would double-launch everything stan.bat owns).
REM Both fresh-launch (top-of-stan.bat writes the flag) and remote
REM update_stan commands now flow through this single safe path:
REM no stan watch process is alive while pip runs, so file-lock
REM collisions on stan.exe become impossible.
set "UPDATE_FLAG=%STAN_DIR%\update_pending.flag"
set "STAN_VENV_PIP=%STAN_DIR%\venv\Scripts\pip.exe"
if not exist "%STAN_VENV_PIP%" set "STAN_VENV_PIP=%USERPROFILE%\.stan\venv\Scripts\pip.exe"

echo [%DATE% %TIME%] STAN watcher starting (auto-restart on crash).
echo                  Close this window to stop STAN.
echo.

:loop
if exist "%UPDATE_FLAG%" (
    echo [%DATE% %TIME%] update_pending.flag detected — running pip install...
    REM Clean up ~tan-proteomics leftover dist-info dirs from prior
    REM partially-renamed installs. pip prints three "Ignoring invalid
    REM distribution ~tan-proteomics" warnings on every run when these
    REM are present. Harmless but noisy — and the cleanup is a one-line
    REM rmdir that pip won't do itself.
    set "STAN_SP=%STAN_DIR%\venv\Lib\site-packages"
    if not exist "!STAN_SP!" set "STAN_SP=%USERPROFILE%\.stan\venv\Lib\site-packages"
    if exist "!STAN_SP!" (
        for /d %%D in ("!STAN_SP!\~tan-proteomics*") do (
            echo [%DATE% %TIME%] Cleaning leftover %%~nxD
            rmdir /s /q "%%D" 2>nul
        )
    )
    REM Kill the dashboard cmd window BEFORE pip install. Pre-fix it
    REM held stan.exe open and pip's --upgrade tried to overwrite the
    REM .exe → "in use" error → partial uninstall left venv broken
    REM (ModuleNotFoundError: No module named 'stan'). Hit Brett 4x on
    REM 2026-05-08. The dashboard relaunches via the line at the top
    REM of stan.bat the next time stan.bat is opened cleanly — we
    REM accept this leaves the dashboard down for ~30s during the
    REM restart cycle but kills the lock contention class of failure.
    taskkill /f /fi "WINDOWTITLE eq STAN Dashboard*" 2>nul
    taskkill /f /fi "IMAGENAME eq stan.exe" 2>nul
    timeout /t 2 /nobreak >nul
    if exist "%STAN_VENV_PIP%" (
        "%STAN_VENV_PIP%" install --upgrade --quiet --no-input ^
            "stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
        if errorlevel 1 (
            echo [%DATE% %TIME%] WARN: pip install failed - continuing with installed version.
        ) else (
            for /f "delims=" %%V in ('"%STAN_EXE%" --version 2^>nul') do echo [%DATE% %TIME%] Running %%V
        )
    ) else (
        echo    WARN: pip not found at %STAN_VENV_PIP% - skipping update.
    )
    del "%UPDATE_FLAG%" 2>nul
    echo [%DATE% %TIME%] update complete — relaunching watcher.
)

"%STAN_EXE%" watch
echo.
echo [%DATE% %TIME%] stan watch exited (code %ERRORLEVEL%); relaunching in 5s
timeout /t 5 /nobreak >nul
goto loop
