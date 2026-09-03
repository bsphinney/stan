@echo off
REM Mirror the Evosep One procedure logs (which hold Pressure [bar]) to the share.
REM Read-only on the source. Double-click it.
REM
REM   (no argument)  full history, incremental -- safe to re-run, resumes if stopped
REM   /recent        just the last 30 days
REM
REM 2026-09-02: the full history is now the DEFAULT. It used to need /all, which
REM is why the first pull only reached back to 2026-08-14.
setlocal
set "PS1=%~dp0copy_evosep_logs.ps1"
if not exist "%PS1%" ( echo ERROR: copy_evosep_logs.ps1 missing next to this .bat & pause & exit /b 1 )
set "EXTRA="
if /i "%~1"=="/recent" set "EXTRA=-Recent"
REM /all is now the default; accepted so older notes keep working.
if /i "%~1"=="/all" set "EXTRA="
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %EXTRA%
exit /b %errorlevel%
