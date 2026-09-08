@echo off
REM Mirror the Evosep One procedure logs (which hold Pressure [bar]) to the share.
REM Read-only on the source. Double-click it.
REM
REM IT SETS ITSELF UP. The first run registers a scheduled task that repeats
REM hourly, so the logs keep flowing without anyone remembering to run this.
REM Later runs see the task is already there and just copy. Say Yes to the
REM administrator prompt on that first run -- creating a scheduled task needs it.
REM
REM   (no argument)  full history, incremental -- safe to re-run, resumes if stopped
REM   /recent        just the last 30 days
REM   /uninstall     remove the scheduled task
setlocal
set "PS1=%~dp0copy_evosep_logs.ps1"
if not exist "%PS1%" ( echo ERROR: copy_evosep_logs.ps1 missing next to this .bat & pause & exit /b 1 )
set "EXTRA="
if /i "%~1"=="/recent" set "EXTRA=-Recent"
if /i "%~1"=="/uninstall" set "EXTRA=-Uninstall"
REM /all is the default; accepted so older notes keep working.
if /i "%~1"=="/all" set "EXTRA="
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %EXTRA%
exit /b %errorlevel%
