@echo off
REM ------------------------------------------------------------------
REM install_flinders_copy.bat
REM
REM Sets up the timsTOF -> Flinders copier on this instrument PC.
REM Double-click it. It asks where the Flinders timsTOF folder is,
REM then creates a scheduled task that runs every 5 minutes.
REM
REM Nothing to install first -- no Python, no STAN. It uses robocopy,
REM which is already part of Windows.
REM
REM Windows will ask for administrator approval once, to create the
REM task. Only that one step is elevated: finding the Flinders drive
REM happens first, unelevated, because an elevated process cannot see
REM your mapped network drives.
REM
REM To remove it later:  install_flinders_copy.bat /remove
REM ------------------------------------------------------------------

set "PS1=%~dp0flinders_copy.ps1"

if not exist "%PS1%" (
    echo   ERROR: flinders_copy.ps1 is not next to this .bat
    echo   Expected it at: %PS1%
    pause
    exit /b 1
)

if /i "%~1"=="/remove" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Uninstall
    pause
    exit /b 0
)

echo.
echo   Setting up the timsTOF -^> Flinders copier
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Install
if errorlevel 1 goto :failed

echo.
echo   Done. It will check D:\Data every 5 minutes from now on,
echo   including after a reboot.
echo.
echo   Proof it is running:  %%USERPROFILE%%\STAN\logs\flinders_status.txt
echo   What it has done:     %%USERPROFILE%%\STAN\logs\flinders_copy.log
echo   To pause it:          create %%USERPROFILE%%\STAN\flinders_pause.txt
echo.
pause
exit /b 0

:failed
echo.
echo   Setup did not finish. NOTHING is scheduled.
echo   Read the message above, fix it, and run this again.
echo.
pause
exit /b 1
