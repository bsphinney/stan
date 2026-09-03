@echo off
REM ------------------------------------------------------------------
REM run_bruker_dump_auto.bat
REM
REM Reads the Bruker database credential from HyStar's own config, then
REM dumps the database to the share -- no typing, no password to know.
REM Read-only: the database is never written to and no Bruker file is
REM changed. Output lands in Y:\brett\bruker_db\<HOST>_<timestamp>\.
REM
REM   run_bruker_dump_auto.bat            survey + schema + logs
REM   run_bruker_dump_auto.bat /data      also dumps sample-table rows
REM
REM Needs dump_bruker_auto.ps1 AND dump_bruker_db.ps1 in the same folder.
REM ------------------------------------------------------------------

setlocal
set "PS1=%~dp0dump_bruker_auto.ps1"
if not exist "%PS1%" (
    echo.
    echo   ERROR: dump_bruker_auto.ps1 is not next to this .bat
    echo   Copy ALL THREE files together:
    echo       dump_bruker_auto.ps1  dump_bruker_db.ps1  run_bruker_dump_auto.bat
    echo.
    pause
    exit /b 1
)

set "EXTRA="
if /i "%~1"=="/data" set "EXTRA=-IncludeData"
if /i "%~1"=="-data" set "EXTRA=-IncludeData"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %EXTRA%
echo.
pause
exit /b %errorlevel%
