@echo off
REM ------------------------------------------------------------------
REM flinders_copy_tray.bat
REM
REM Launcher for the timsTOF -> Flinders tray copier. Double-click it,
REM or leave it running; it puts an icon in the notification area
REM (bottom-right, next to the clock) and copies each .d in D:\Data up
REM to the Flinders archive once acquisition has finished.
REM
REM First launch asks which drive letter the Flinders share is mapped
REM to and remembers the answer, so it only asks once.
REM
REM To start it automatically at logon: press Win+R, run
REM   shell:startup
REM and drop a shortcut to this .bat into the folder that opens.
REM
REM Useful variants:
REM   flinders_copy_tray.bat -ShowConsole    keep the console visible
REM   flinders_copy_tray.bat -Reconfigure    ask for the drive again
REM ------------------------------------------------------------------

setlocal

set "PS1=%~dp0flinders_copy_tray.ps1"

if not exist "%PS1%" (
    echo.
    echo   ERROR: cannot find flinders_copy_tray.ps1 next to this .bat
    echo   Expected: %PS1%
    echo.
    pause
    exit /b 1
)

REM -WindowStyle Hidden keeps the console from lingering; the script
REM also hides its own console window on startup, so at worst there is
REM a brief flash. -ExecutionPolicy Bypass because instrument PCs are
REM locked to Restricted by default and this script is not signed.
start "STAN Flinders Copier" /b powershell.exe -NoProfile -NoLogo ^
    -ExecutionPolicy Bypass -WindowStyle Hidden ^
    -File "%PS1%" %*

endlocal
