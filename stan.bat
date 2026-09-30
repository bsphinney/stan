@echo off
REM ===================================================================
REM  stan.bat - single entry point for STAN
REM
REM  Double-click and walk away. No menus, no choices.
REM
REM  First run on a new PC:
REM    - detects no venv present
REM    - finds install-stan.bat next to this file or in Downloads, and
REM      downloads it from GitHub next to this file when it is in
REM      neither place
REM    - calls install-stan.bat (downloads + runs install_stan.ps1 from
REM      GitHub, which creates the venv, pip-installs STAN, installs the
REM      pinned search engines - DIA-NN 2.3.x and Sage v0.14.7 - and
REM      then offers `stan setup` to configure the instrument)
REM    - re-reads PATH from the registry, so the watcher started by this
REM      same window finds the search engines the installer just put on
REM      the user PATH
REM
REM  Every subsequent run:
REM    - warns, without stopping, when the DIA-NN on PATH is not the
REM      2.3 line the community benchmark accepts
REM    - launches dashboard server in a separate window
REM    - tells the operator to open http://localhost:8421 in Chrome/Edge
REM    - enters a supervisor loop that relaunches `stan watch` on
REM      crash and runs pending updates between restarts
REM
REM  Power-user actions (baseline, submit-all, backfills, send-command,
REM  godmode flows) are CLI subcommands of the `stan` exe directly.
REM  This launcher never surfaces them - that is by design so routine
REM  labs see one icon and one icon only.
REM
REM  Authoring rules (PS 5.1 / cmd.exe parsing traps are real):
REM    - rewrite this entire file when changing it; never patch a line
REM    - keep it ASCII with CRLF line endings (.gitattributes stores
REM      *.bat as-is; cmd.exe can miss labels in an LF-only file)
REM    - quote every path, and keep this file's own folder (tilde-dp0)
REM      out of ( ) blocks: a folder named "STAN (1)" closes one early
REM    - no redirection or percent characters in REM lines; cmd expands
REM      percent signs before it notices the line is a comment
REM    - hand values to PowerShell through the environment, never by
REM      splicing them into a -Command string
REM    - PowerShell here is -Command one-liners only, never a .ps1, so
REM      this file never trips an execution-policy lockdown
REM  Checked by tests/test_windows_installer.ps1.
REM ===================================================================

setlocal enabledelayedexpansion

set "STAN_DIR=%USERPROFILE%\STAN"
set "STAN_EXE=%STAN_DIR%\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" set "STAN_EXE=%USERPROFILE%\.stan\venv\Scripts\stan.exe"

REM ---- First-time install detection ----------------------------------
if exist "%STAN_EXE%" goto run

echo.
echo ===================================================================
echo  STAN first-time setup
echo ===================================================================
echo.
echo  No STAN install detected on this machine. I will run the
echo  installer now. This downloads STAN from GitHub, creates a
echo  Python venv, installs the DIA-NN and Sage search engines, and
echo  then walks you through the instrument configuration wizard.
echo.
echo  Approximately 5-10 minutes. Stay near the keyboard for the
echo  license question and the config questions in the wizard.
echo.
pause

set "STAN_HERE=%~dp0"
set "INSTALLER=%~dp0install-stan.bat"
if exist "%INSTALLER%" goto have_installer
set "INSTALLER=%USERPROFILE%\Downloads\install-stan.bat"
if exist "%INSTALLER%" goto have_installer

REM ---- install-stan.bat is in neither place: fetch it ----------------
REM A lab that downloaded only stan.bat used to stop here with an error.
REM Now the installer is fetched next to this file. The URL and the
REM destination reach PowerShell through the environment, so a folder
REM name containing a quote or a percent sign cannot break the command.
REM Exit codes: 0 saved, 1 download failed, 2 something other than
REM install-stan.bat came back (a proxy or sign-in page) and was deleted.
set "INSTALLER=%~dp0install-stan.bat"
set "STAN_INSTALLER_URL=https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat"
set "STAN_INSTALLER_DEST=%INSTALLER%"
echo  install-stan.bat is not next to stan.bat or in your Downloads folder.
echo  Downloading it from GitHub:
echo    !STAN_INSTALLER_URL!
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $u=$env:STAN_INSTALLER_URL; $d=$env:STAN_INSTALLER_DEST; try { Invoke-WebRequest -Uri $u -OutFile $d -UseBasicParsing -TimeoutSec 60 } catch { Write-Host $_.Exception.Message; exit 1 }; if (-not (Select-String -LiteralPath $d -Pattern 'install_stan.ps1' -SimpleMatch -Quiet)) { Remove-Item -LiteralPath $d -Force; Write-Host 'What came back was not install-stan.bat - a proxy or sign-in page may have answered instead of GitHub.'; exit 2 }; exit 0"
if errorlevel 1 goto installer_download_failed
echo  Saved !STAN_INSTALLER_DEST!
echo.
goto have_installer

:installer_download_failed
echo.
echo ERROR: could not download install-stan.bat (the reason is above).
echo        Download it yourself from
echo          !STAN_INSTALLER_URL!
echo        save it in the same folder as stan.bat:
echo          !STAN_HERE!
echo        and run stan.bat again.
pause
exit /b 1

:have_installer
call "%INSTALLER%"

REM The installer put the venv, DIA-NN and Sage on the user PATH in the
REM registry, but this window still holds the PATH it started with, and
REM the watcher launched below inherits it - so on this first run it
REM would miss the pinned search engines (or run an older DIA-NN that was
REM already on PATH). Re-read the system + user PATH in the order a fresh
REM logon builds it. When the registry cannot be read nothing is printed
REM and PATH is left as it was.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$m=[Environment]::GetEnvironmentVariable('Path','Machine'); $u=[Environment]::GetEnvironmentVariable('Path','User'); if ($m) { @($m,$u) -join ';' }"`) do set "PATH=%%P"

REM Re-resolve STAN_EXE after install - the installer may have
REM written to either of the two known venv roots.
set "STAN_EXE=%STAN_DIR%\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" set "STAN_EXE=%USERPROFILE%\.stan\venv\Scripts\stan.exe"
if not exist "%STAN_EXE%" (
    echo.
    echo ERROR: install completed but stan.exe still not found.
    echo        Scroll up in this window for the installer's messages,
    echo        or contact bsphinney@ucdavis.edu.
    pause
    exit /b 1
)

echo.
echo ===================================================================
echo  Setup complete. Starting STAN now.
echo ===================================================================
echo.

REM ---- Daily run ------------------------------------------------------
:run

REM ---- Self-update stan.bat itself from GitHub ------------------------
REM v0.2.302: pip-install only refreshes the Python package; .bat files
REM on the desktop stay frozen at whatever was last downloaded. Without
REM this step, every change to stan.bat (e.g. the auto-update step
REM added in v0.2.297) requires every operator to re-download the file
REM by hand - which Brett's timsTOF didn't do, so it stayed on the
REM v0.2.295 stan.bat that doesn't auto-update at all.
REM
REM Strategy: download main/stan.bat to a temp file. Hash both. If
REM different, copy temp over self, spawn a fresh cmd window with the
REM new file, and exit cleanly. cmd.exe normally can't overwrite a
REM running .bat, but Windows allows the copy as long as it's not
REM exclusively locked (which cmd doesn't do for .bat reads). We
REM relaunch via "start" + "exit" so the new cmd reads the fresh file
REM from the start instead of trying to resume mid-script.
REM
REM Network failures fall through silently. Hash failures fall through.
REM Only an actual content diff triggers the replace+relaunch.
set "STAN_BAT_NEW=%TEMP%\stan_new_%RANDOM%%RANDOM%.bat"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri ('https://raw.githubusercontent.com/bsphinney/stan/main/stan.bat?t=' + [DateTime]::Now.Ticks) -OutFile '%STAN_BAT_NEW%' -UseBasicParsing -TimeoutSec 15 -ErrorAction Stop; $n = (Get-FileHash -Path '%STAN_BAT_NEW%' -Algorithm SHA256).Hash; $c = (Get-FileHash -Path '%~f0' -Algorithm SHA256).Hash; if ($n -ne $c) { exit 100 } else { exit 0 } } catch { exit 200 }"
if errorlevel 200 (
    echo [%DATE% %TIME%] Self-update check skipped: couldn't reach GitHub.
    if exist "%STAN_BAT_NEW%" del "%STAN_BAT_NEW%" 2>nul
    goto after_self_update
)
if errorlevel 100 (
    echo [%DATE% %TIME%] Found newer stan.bat on GitHub - refreshing this file.
    copy /y "%STAN_BAT_NEW%" "%~f0" >nul
    del "%STAN_BAT_NEW%" 2>nul
    if errorlevel 1 (
        echo [%DATE% %TIME%] WARN: couldn't overwrite stan.bat - continuing.
        goto after_self_update
    )
    echo [%DATE% %TIME%] stan.bat refreshed - relaunching with new version.
    start "" cmd /c "\"%~f0\""
    exit /b 0
)
del "%STAN_BAT_NEW%" 2>nul
:after_self_update

REM ---- Schedule pip update via the supervisor flag --------------------
REM v0.2.313: write update_pending.flag instead of pip-installing
REM inline. Pre-fix, an inline pip install --upgrade ran on every
REM launch - but if a previous stan watch process was still running
REM (operator clicked stan.bat with the old window still open), pip
REM would try to overwrite stan.exe while the venv held it open and
REM leave the package in a half-state. Symptom: ModuleNotFoundError
REM crash loop in the watcher window. Brett's Exploris 480 hit this
REM 2026-05-05.
REM
REM The supervisor loop below already has a safe pip-install path:
REM it consumes update_pending.flag BEFORE running stan watch, so
REM pip runs with no stan.exe handle open. Funnel both fresh-launch
REM and remote update_stan commands through that same path so there
REM is exactly one place pip runs and exactly one set of safety
REM guarantees to maintain.
echo [%DATE% %TIME%] Scheduling STAN package update on next watcher restart.
if not exist "%STAN_DIR%" mkdir "%STAN_DIR%" 2>nul
echo. > "%STAN_DIR%\update_pending.flag"
echo.

REM ---- Is the DIA-NN on PATH one the community benchmark accepts? -----
REM The supervisor below only ever pip-installs STAN. It never runs
REM update-stan.bat, which is what installs the pinned DIA-NN 2.3.x and
REM puts it first on PATH, and it must not: that can need an administrator,
REM and this loop runs unattended. So a PC an older installer gave DIA-NN
REM 2.5 to 2.7 would go on producing DIA runs the relay rejects, and say
REM nothing. This check says so once per launch and never stops it. The
REM version comes from the install folder name, or from the program header
REM when the folder names none. Keep STAN_DIANN_LINE equal to the
REM major.minor of DiannPinnedVersion in install_stan.ps1; the test checks.
set "STAN_DIANN_LINE=2.3"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $want=$env:STAN_DIANN_LINE; $exe=''; foreach ($d in ($env:PATH -split [IO.Path]::PathSeparator)) { $d=$d.Trim().Trim([char]34); if (-not $d) { continue }; try { $c=Join-Path $d 'DiaNN.exe' -ErrorAction Stop; if (Test-Path -LiteralPath $c -PathType Leaf) { $exe=$c; break } } catch {} }; if (-not $exe) { exit 0 }; $v=''; $m=[regex]::Matches((Split-Path -Parent $exe),'\d+\.\d+(\.\d+)?'); if ($m.Count -gt 0) { $v=$m[$m.Count-1].Value }; if (-not $v) { $o=[IO.Path]::GetTempFileName(); $e=[IO.Path]::GetTempFileName(); try { $p=Start-Process -FilePath $exe -NoNewWindow -PassThru -RedirectStandardOutput $o -RedirectStandardError $e; if (-not $p.WaitForExit(15000)) { try { $p.Kill() } catch {} }; $t=@((Get-Content -LiteralPath $o -Raw),(Get-Content -LiteralPath $e -Raw)) -join ' '; if ($t -match 'DIA-NN\s+(\d+\.\d+(\.\d+)?)') { $v=$Matches[1] } } catch {}; Remove-Item -LiteralPath $o,$e -Force }; $ok=$false; if ($v) { $a=$v.Split('.'); $b=$want.Split('.'); if (($a.Count -ge 2) -and ($b.Count -ge 2)) { $ok=(($a[0] -eq $b[0]) -and ($a[1] -eq $b[1])) } }; if ($ok) { exit 0 }; if (-not $v) { $v='of unknown version' }; Write-Host ''; Write-Host ('  WARNING: the watcher runs DIA-NN {0} from PATH: {1}' -f $v,$exe) -ForegroundColor Yellow; Write-Host ('  The community benchmark accepts DIA-NN {0}.x only, so DIA runs searched' -f $want) -ForegroundColor Yellow; Write-Host '  with it cannot be submitted. Run update-stan.bat once, by hand: it installs' -ForegroundColor Yellow; Write-Host ('  DIA-NN {0}.x beside this one and puts it first on PATH, or prints the' -f $want) -ForegroundColor Yellow; Write-Host '  download link when installing needs an administrator. An instrument whose' -ForegroundColor Yellow; Write-Host ('  diann_path in instruments.yml names a DIA-NN {0}.x is not affected.' -f $want) -ForegroundColor Yellow; exit 0"

echo [%DATE% %TIME%] Launching STAN dashboard...
start "STAN Dashboard" cmd /c ""%STAN_EXE%" dashboard"

REM Give the dashboard server a moment to bind. Do NOT auto-open the
REM browser - Windows defaults to Internet Explorer on instrument PCs,
REM and IE doesn't support the React 18 + Babel runtime the dashboard
REM ships with (blank page on every IE launch). Operator opens
REM http://localhost:8421 in Chrome or Edge themselves. Brett 2026-05-08.
timeout /t 4 /nobreak >nul
echo.
echo ===================================================================
echo  Dashboard ready at http://localhost:8421
echo  Open it in Chrome or Edge (NOT Internet Explorer).
echo ===================================================================
echo.

REM ---- Supervisor loop ------------------------------------------------
REM Mirrors the proven start_stan_loop.bat flow: a crash triggers a
REM restart within 5s, and a remote update_stan command writes
REM update_pending.flag which is consumed here BETWEEN watcher
REM restarts (so pip never races stan.exe file locks).
REM
REM v0.2.313: the flag-handler runs a minimal pip install in-place
REM (NOT update-stan.bat / update_stan.ps1, which is a kitchen-sink
REM script that also spawns its own watcher + dashboard + overnight
REM backfill - those would double-launch everything stan.bat owns).
REM Both fresh-launch (top-of-stan.bat writes the flag) and remote
REM update_stan commands now flow through this single safe path:
REM no stan watch process is alive while pip runs, so file-lock
REM collisions on stan.exe become impossible.
REM
REM After a successful pip install the loop prints `stan version`
REM ("STAN vX.Y.Z"). It used to call `stan --version`, which is not an
REM option, so that line never printed anything.
set "UPDATE_FLAG=%STAN_DIR%\update_pending.flag"
set "STAN_VENV_PIP=%STAN_DIR%\venv\Scripts\pip.exe"
if not exist "%STAN_VENV_PIP%" set "STAN_VENV_PIP=%USERPROFILE%\.stan\venv\Scripts\pip.exe"

echo [%DATE% %TIME%] STAN watcher starting (auto-restart on crash).
echo                  Close this window to stop STAN.
echo.

:loop
if exist "%UPDATE_FLAG%" (
    echo [%DATE% %TIME%] update_pending.flag detected - running pip install...
    REM Clean up ~tan-proteomics leftover dist-info dirs from prior
    REM partially-renamed installs. pip prints three "Ignoring invalid
    REM distribution ~tan-proteomics" warnings on every run when these
    REM are present. Harmless but noisy - and the cleanup is a one-line
    REM rmdir that pip won't do itself.
    set "STAN_SP=%STAN_DIR%\venv\Lib\site-packages"
    if not exist "!STAN_SP!" set "STAN_SP=%USERPROFILE%\.stan\venv\Lib\site-packages"
    if exist "!STAN_SP!" (
        for /d %%D in ("!STAN_SP!\~tan-proteomics*") do (
            echo [%DATE% %TIME%] Cleaning leftover %%~nxD
            rmdir /s /q "%%D" 2>nul
        )
    )
    REM Kill the dashboard cmd window BEFORE pip install. Pre-fix it
    REM held stan.exe open and pip's --upgrade tried to overwrite the
    REM .exe, got an "in use" error, and the partial uninstall left venv broken
    REM (ModuleNotFoundError: No module named 'stan'). Hit Brett 4x on
    REM 2026-05-08. The dashboard relaunches via the line at the top
    REM of stan.bat the next time stan.bat is opened cleanly - we
    REM accept this leaves the dashboard down for ~30s during the
    REM restart cycle but kills the lock contention class of failure.
    taskkill /f /fi "WINDOWTITLE eq STAN Dashboard*" 2>nul
    taskkill /f /fi "IMAGENAME eq stan.exe" 2>nul
    timeout /t 2 /nobreak >nul
    if exist "%STAN_VENV_PIP%" (
        "%STAN_VENV_PIP%" install --upgrade --quiet --no-input ^
            "stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
        if errorlevel 1 (
            echo [%DATE% %TIME%] WARN: pip install failed - continuing with installed version.
        ) else (
            for /f "delims=" %%V in ('"%STAN_EXE%" version 2^>nul') do echo [%DATE% %TIME%] Running %%V
        )
    ) else (
        echo    WARN: pip not found at %STAN_VENV_PIP% - skipping update.
    )
    del "%UPDATE_FLAG%" 2>nul
    REM Relaunch dashboard now that pip is done - earlier in stan.bat
    REM the dashboard launched once before the loop; on every later
    REM update cycle the kill-before-pip step takes it down. Without
    REM this the dashboard stays dead until operator restarts stan.bat
    REM completely. Brett 2026-05-08: had to close/reopen 4x to recover
    REM the dashboard after pip cycles.
    echo [%DATE% %TIME%] Relaunching dashboard...
    start "STAN Dashboard" cmd /c ""%STAN_EXE%" dashboard"
    echo [%DATE% %TIME%] update complete - relaunching watcher.
)

"%STAN_EXE%" watch
echo.
echo [%DATE% %TIME%] stan watch exited (code %ERRORLEVEL%); relaunching in 5s
timeout /t 5 /nobreak >nul
goto loop
