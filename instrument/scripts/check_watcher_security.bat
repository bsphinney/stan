@echo off
REM Will security software on this PC block or flag the HyStar watcher?
REM READ-ONLY -- reports, changes nothing. Saves its report to Y:\brett\security_check\.
REM
REM   (no argument)  report only
REM   /trylaunch     also start ONE hidden PowerShell through a VBScript, the way
REM                  the watcher does, and check whether it ran or was blocked.
REM                  An endpoint-protection agent may report this to IT.
setlocal
set "PS1=%~dp0check_watcher_security.ps1"
if not exist "%PS1%" ( echo ERROR: check_watcher_security.ps1 missing next to this .bat & pause & exit /b 1 )
set "EXTRA="
if /i "%~1"=="/trylaunch" set "EXTRA=-TryLaunch"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %EXTRA%
echo.
pause
exit /b %errorlevel%
