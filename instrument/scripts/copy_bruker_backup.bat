@echo off
REM Mirrors Bruker's own DB backups from D:\BrukerDBBackup to the share,
REM preserving the daily\<stamp>\ layout the Hive extractor reads.
REM Read-only on D:. Double-click.
REM
REM IT SETS ITSELF UP. The first run registers a scheduled task that repeats
REM every 4 hours, so the nightly 18:00 Compass backup reaches Hive by itself.
REM Say Yes to the administrator prompt on that first run.
REM
REM   (no argument)  incremental -- copies only backups not already on the share
REM   /uninstall     remove the scheduled task
setlocal
set "PS1=%~dp0copy_bruker_backup.ps1"
if not exist "%PS1%" ( echo ERROR: copy_bruker_backup.ps1 missing next to this .bat & pause & exit /b 1 )
set "EXTRA="
if /i "%~1"=="/all" set "EXTRA=-All"
if /i "%~1"=="/uninstall" set "EXTRA=-Uninstall"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %EXTRA%
exit /b %errorlevel%
