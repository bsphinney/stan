@echo off
REM Finds the Evosep One logs on this instrument (read-only) and checks them
REM for column pressure. Double-click. Writes a transcript to the share.
setlocal
set "PS1=%~dp0find_evosep_logs.ps1"
if not exist "%PS1%" ( echo ERROR: find_evosep_logs.ps1 missing next to this .bat & pause & exit /b 1 )
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
pause
exit /b %errorlevel%
