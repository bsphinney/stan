@echo off
REM ------------------------------------------------------------------
REM install_flinders_copy.bat
REM
REM Sets up the timsTOF -> Flinders copier on this instrument PC.
REM Double-click it. It will ask where the Flinders timsTOF folder is,
REM then create a scheduled task that runs every 5 minutes.
REM
REM Nothing to install first -- no Python, no STAN, no admin rights.
REM It uses robocopy, which is already part of Windows.
REM
REM To remove it later, run:  install_flinders_copy.bat /remove
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
if errorlevel 1 (
    echo.
    echo   Setup did not finish. Nothing was scheduled.
    pause
    exit /b 1
)

echo.
echo   Done. It will check D:\Data every 5 minutes from now on,
echo   including after a reboot.
echo.
echo   Proof it is running:  %%USERPROFILE%%\STAN\logs\flinders_status.txt
echo   What it has done:     %%USERPROFILE%%\STAN\logs\flinders_copy.log
echo   To pause it:          create %%USERPROFILE%%\STAN\flinders_pause.txt
echo.
pause
