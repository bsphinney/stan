@echo off
REM -------------------------------------------------------------------
REM  stan watch supervisor loop (v0.2.90+)
REM
REM  Relaunches `stan watch` whenever it exits, and runs pending
REM  updates BEFORE launching so pip never races stan.exe file locks.
REM
REM  Flow when a remote update arrives:
REM    1. update_stan action writes %USERPROFILE%\STAN\update_pending.flag
REM    2. restart_watcher action writes restart.flag
REM    3. stan watch exits cleanly on next tick
REM    4. THIS loop sees update_pending.flag, runs update-stan.bat
REM       (now nothing has stan.exe open — pip succeeds)
REM    5. Flag is deleted, stan watch relaunches on the new version
REM -------------------------------------------------------------------

set "STAN_EXE=%USERPROFILE%\STAN\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" set "STAN_EXE=%USERPROFILE%\.stan\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" set "STAN_EXE=stan"

set "UPDATE_FLAG=%USERPROFILE%\STAN\update_pending.flag"
set "UPDATER=%USERPROFILE%\Downloads\update-stan.bat"
if not exist "%UPDATER%" set "UPDATER=%USERPROFILE%\STAN\update-stan.bat"

echo start_stan_loop: stan.exe = %STAN_EXE%
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

echo [%DATE% %TIME%] stan watch starting
"%STAN_EXE%" watch
echo [%DATE% %TIME%] stan watch exited (code %ERRORLEVEL%); relaunching in 5s
timeout /t 5 /nobreak >nul
goto loop
