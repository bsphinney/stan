@echo off
REM ------------------------------------------------------------------
REM find_bruker_login.bat
REM
REM Works out how to authenticate to Bruker's local PostgreSQL.
REM Read-only: it reads configuration files and changes nothing.
REM
REM Run this when dump_bruker_db.ps1 asks for a password you do not have.
REM ------------------------------------------------------------------

setlocal
set "PS1=%~dp0find_bruker_db_login.ps1"

if not exist "%PS1%" (
    echo.
    echo   ERROR: find_bruker_db_login.ps1 is not next to this .bat
    echo   Expected it at: %PS1%
    echo.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
echo.
pause
exit /b 0
