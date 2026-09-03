@echo off
REM Copies Bruker's own DB backups from D:\BrukerDBBackup to the share.
REM Read-only on D:. Double-click.  /all = every backup, not just newest.
setlocal
set "PS1=%~dp0copy_bruker_backup.ps1"
if not exist "%PS1%" ( echo ERROR: copy_bruker_backup.ps1 missing next to this .bat & pause & exit /b 1 )
set "EXTRA="
if /i "%~1"=="/all" set "EXTRA=-All"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %EXTRA%
exit /b %errorlevel%
