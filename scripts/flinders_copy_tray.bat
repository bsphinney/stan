@echo off
REM ------------------------------------------------------------------
REM flinders_copy_tray.bat
REM
REM Starts the timsTOF -> Flinders copier. It puts an icon in the
REM notification area (bottom right, by the clock) and copies each .d
REM in D:\Data to the Flinders archive once acquisition has finished.
REM Leave it running.
REM
REM Pure PowerShell. Nothing to install -- no Python, no STAN, no
REM admin rights. It uses robocopy, which is already on Windows.
REM
REM First start asks where the Flinders timsTOF folder is and
REM remembers the answer.
REM
REM Start it automatically at logon: press Win+R, run  shell:startup
REM and drop a shortcut to this file into the folder that opens.
REM
REM   flinders_copy_tray.bat -ShowConsole    watch the log live
REM   flinders_copy_tray.bat -AskDest        ask for the folder again
REM ------------------------------------------------------------------

set "PS1=%~dp0flinders_copy_tray.ps1"

if not exist "%PS1%" (
    echo   ERROR: flinders_copy_tray.ps1 is not next to this .bat
    echo   Expected it at: %PS1%
    pause
    exit /b 1
)

REM -ExecutionPolicy Bypass because instrument PCs default to
REM Restricted and this script is not signed.
start "STAN Flinders" /b powershell.exe -NoProfile -NoLogo ^
    -ExecutionPolicy Bypass -WindowStyle Hidden -File "%PS1%" %*
