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

:loop
echo [%DATE% %TIME%] stan watch starting
stan watch
echo [%DATE% %TIME%] stan watch exited (code %ERRORLEVEL%); relaunching in 5s
timeout /t 5 /nobreak >nul
goto loop
