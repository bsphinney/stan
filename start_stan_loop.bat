@echo off
REM -------------------------------------------------------------------
REM  stan watch supervisor loop
REM
REM  Relaunches `stan watch` whenever it exits. Required for the
REM  `restart_watcher` control action to take effect — without this
REM  loop, a remote restart just stops the daemon.
REM
REM  Add a shortcut to the user's Startup folder to run this at login.
REM -------------------------------------------------------------------

REM Find stan.exe explicitly via the STAN venv so this works even when
REM the enclosing terminal didn't inherit the user PATH. Falls back to
REM plain `stan` (PATH) if the venv path isn't there.
set "STAN_EXE=%USERPROFILE%\STAN\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" set "STAN_EXE=%USERPROFILE%\.stan\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" set "STAN_EXE=stan"

echo start_stan_loop: using %STAN_EXE%
echo.

:loop
echo [%DATE% %TIME%] stan watch starting
"%STAN_EXE%" watch
echo [%DATE% %TIME%] stan watch exited (code %ERRORLEVEL%); relaunching in 5s
timeout /t 5 /nobreak >nul
goto loop
