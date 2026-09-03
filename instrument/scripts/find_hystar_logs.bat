@echo off
REM Find HyStar / TimsControl instrument logs. READ-ONLY -- copies nothing.
REM Looking for over-pressure EVENTS and the column oven TEMPERATURE, which
REM is recorded nowhere else we have checked. Double-click it.
setlocal
set "PS1=%~dp0find_hystar_logs.ps1"
if not exist "%PS1%" ( echo ERROR: find_hystar_logs.ps1 missing next to this .bat & pause & exit /b 1 )
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
exit /b %errorlevel%
