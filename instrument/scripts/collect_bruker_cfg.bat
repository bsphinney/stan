@echo off
REM Copies Bruker Compass Server config files to the share so Brett can find
REM the datasource credential. Read-only. Double-click it.
setlocal
set "PS1=%~dp0collect_bruker_cfg.ps1"
if not exist "%PS1%" (
    echo   ERROR: collect_bruker_cfg.ps1 is not next to this .bat
    pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
exit /b %errorlevel%
