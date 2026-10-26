@echo off
REM Watches HyStar's error logbook on this instrument PC and @mentions Brett in
REM Slack within a minute when HyStar stops the acquisition queue. Read-only on
REM everything HyStar and Evosep own. Double-click it.
REM
REM IT SETS ITSELF UP. The first run finds the logbook, asks for your Slack
REM member ID and the Slack webhook (paste it; it is not shown), registers a
REM scheduled task that runs every minute, and sends a test message: your
REM phone should buzz. Say Yes to the administrator prompt. Running it again
REM updates an older install, or re-checks the settings (Enter keeps each).
REM
REM   (no argument)  install, update, or re-check
REM   /test          send the test message again
REM   /pause 8h      planned downtime: Hive does not page "watcher down" for
REM                  8 hours (2d for two days, up to 31d; 0h ends a pause).
REM                  Queue-stop alerts still go out.
REM   /uninstall     remove the scheduled task (its settings folder is kept)
setlocal
set "PS1=%~dp0watch_hystar_errors.ps1"
if not exist "%PS1%" ( echo ERROR: watch_hystar_errors.ps1 missing next to this .bat & pause & exit /b 1 )
set "EXTRA="
if /i "%~1"=="/test" set "EXTRA=-Test"
if /i "%~1"=="-test" set "EXTRA=-Test"
if /i "%~1"=="/uninstall" set "EXTRA=-Uninstall"
if /i "%~1"=="-uninstall" set "EXTRA=-Uninstall"
if /i "%~1"=="/pause" set "EXTRA=-Pause %~2"
if /i "%~1"=="-pause" set "EXTRA=-Pause %~2"
if /i "%~1"=="/pause" if "%~2"=="" goto :usage
if /i "%~1"=="-pause" if "%~2"=="" goto :usage
if not "%~1"=="" if not defined EXTRA goto :usage
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %EXTRA%
exit /b %errorlevel%

:usage
echo Usage: watch_hystar_errors.bat [/test ^| /pause 8h ^| /uninstall]
echo   /pause takes hours or days: 8h, 2d (up to 31d; 0h ends a pause).
pause
exit /b 1
