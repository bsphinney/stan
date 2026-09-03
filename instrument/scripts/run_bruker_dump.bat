@echo off
REM ------------------------------------------------------------------
REM run_bruker_dump.bat
REM
REM Read-only survey of the Bruker HyStar PostgreSQL database on this
REM instrument. Double-click it. Everything lands on the proteomics-grp
REM share, in Y:\brett\bruker_db\<HOSTNAME>_<timestamp>\.
REM
REM Nothing is installed and nothing is scheduled. The database is never
REM written to, the service is never stopped, and no file inside
REM D:\BrukerDBData is modified. It is safe to run while acquiring.
REM
REM   run_bruker_dump.bat            survey + schema + server logs (fast)
REM   run_bruker_dump.bat /data      also dumps sample-table rows
REM
REM Use /data when the instrument is idle -- it reads table contents, so
REM it costs more disk I/O than the default pass.
REM ------------------------------------------------------------------

setlocal

set "PS1=%~dp0dump_bruker_db.ps1"

if not exist "%PS1%" (
    echo.
    echo   ERROR: dump_bruker_db.ps1 is not next to this .bat
    echo   Expected it at: %PS1%
    echo.
    echo   Copy BOTH files together, e.g.
    echo       copy Y:\brett\scripts\dump_bruker_db.ps1 C:\Temp\
    echo       copy Y:\brett\scripts\run_bruker_dump.bat C:\Temp\
    echo.
    pause
    exit /b 1
)

set "EXTRA="
if /i "%~1"=="/data" set "EXTRA=-IncludeData"
if /i "%~1"=="-data" set "EXTRA=-IncludeData"

echo.
if defined EXTRA (
    echo   Bruker DB dump - survey + schema + logs + SAMPLE TABLE ROWS
    echo   Prefer to run this while the instrument is idle.
) else (
    echo   Bruker DB dump - survey + schema + server logs
    echo   Safe to run during an acquisition.
)
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %EXTRA%
if errorlevel 1 goto :failed

echo.
echo   Done. Tell Brett the folder name printed above.
echo   On Hive it is: /quobyte/proteomics-grp/brett/bruker_db/
echo.
pause
exit /b 0

:failed
echo.
echo   The dump did not finish. NOTHING was changed on the instrument.
echo.
echo   If it could not connect, the database may want a password:
echo       set PGPASSWORD=yourpassword
echo   then run this again. The local auth method is in
echo   D:\BrukerDBData\pg_hba.conf.
echo.
pause
exit /b 1
