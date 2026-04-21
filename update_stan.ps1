# STAN Updater - reinstalls STAN and checks for missing/outdated search engines

# SSL workaround for corporate/university proxy networks
try {
    Add-Type @"
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public class TrustAllUpdate {
    public static void Enable() {
        ServicePointManager.ServerCertificateValidationCallback =
            delegate { return true; };
    }
}
"@
    [TrustAllUpdate]::Enable()
} catch {}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Helper: pick the latest DIA-NN .msi asset from a release by parsing version from filename
# The DIA-NN "latest" release contains multiple versions (2.0, 2.1, 2.2, 2.3.x) as assets
function Select-LatestDiannMsi {
    param($assets)
    $bestAsset = $null
    $bestMajor = 0
    $bestMinor = 0
    $bestPatch = 0
    foreach ($asset in $assets) {
        $name = $asset.name
        if ($name -notmatch "\.msi$") { continue }
        if ($name -match "linux") { continue }
        if ($name -match "[Pp]review") { continue }
        # Parse version like "DIA-NN-2.3.2-Academia.msi"
        if ($name -match "(\d+)\.(\d+)\.(\d+)") {
            $maj = [int]$Matches[1]
            $min = [int]$Matches[2]
            $pat = [int]$Matches[3]
            if ($maj -gt $bestMajor -or
                ($maj -eq $bestMajor -and $min -gt $bestMinor) -or
                ($maj -eq $bestMajor -and $min -eq $bestMinor -and $pat -gt $bestPatch)) {
                $bestMajor = $maj
                $bestMinor = $min
                $bestPatch = $pat
                $bestAsset = $asset
            }
        } elseif ($name -match "(\d+)\.(\d+)") {
            $maj = [int]$Matches[1]
            $min = [int]$Matches[2]
            if ($maj -gt $bestMajor -or
                ($maj -eq $bestMajor -and $min -gt $bestMinor)) {
                $bestMajor = $maj
                $bestMinor = $min
                $bestPatch = 0
                $bestAsset = $asset
            }
        }
    }
    return $bestAsset
}

# Use new STAN directory, fall back to old .stan
$venv = "$env:USERPROFILE\STAN\venv"
$venvPython = "$venv\Scripts\python.exe"
$oldVenv = "$env:USERPROFILE\.stan\venv"
$oldVenvPython = "$oldVenv\Scripts\python.exe"
$newStanDir = "$env:USERPROFILE\STAN"

if (-not (Test-Path $venvPython)) {
    if (Test-Path $oldVenvPython) {
        Write-Host "  Migrating from .stan to STAN..." -ForegroundColor Yellow
        if (-not (Test-Path $newStanDir)) { New-Item -ItemType Directory -Path $newStanDir -Force | Out-Null }
        try {
            $destVenv = Join-Path $newStanDir "venv"
            Copy-Item -Path $oldVenv -Destination $destVenv -Recurse -Force
            Write-Host "  Copied venv" -ForegroundColor Gray
            $oldStanDir = Join-Path $env:USERPROFILE ".stan"
            $configFiles = Get-ChildItem $oldStanDir -File -ErrorAction SilentlyContinue
            foreach ($cf in $configFiles) {
                $destFile = Join-Path $newStanDir $cf.Name
                if (-not (Test-Path $destFile)) {
                    Copy-Item $cf.FullName $destFile
                    Write-Host "  Copied $($cf.Name)" -ForegroundColor Gray
                }
            }
            $subDirs = Get-ChildItem $oldStanDir -Directory -ErrorAction SilentlyContinue
            foreach ($sd in $subDirs) {
                if ($sd.Name -ne "venv") {
                    $destSub = Join-Path $newStanDir $sd.Name
                    if (-not (Test-Path $destSub)) {
                        Copy-Item $sd.FullName $destSub -Recurse -Force
                        Write-Host "  Copied $($sd.Name)" -ForegroundColor Gray
                    }
                }
            }
            $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
            $oldScripts = Join-Path $oldVenv "Scripts"
            $newScripts = Join-Path $destVenv "Scripts"
            if ($userPath -like "*$oldScripts*") {
                $parts = $userPath -split ";"
                $filtered = @()
                foreach ($p in $parts) { if ($p -ne $oldScripts -and $p -ne "") { $filtered += $p } }
                $userPath = $filtered -join ";"
            }
            if ($userPath -notlike "*$newScripts*") {
                $userPath = "$userPath;$newScripts"
            }
            [Environment]::SetEnvironmentVariable("PATH", $userPath, "User")
            $env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));$userPath"
            Write-Host "  Migration complete." -ForegroundColor Green
        } catch {
            Write-Host "  Migration failed, using old location." -ForegroundColor Yellow
            $venv = $oldVenv
            $venvPython = $oldVenvPython
        }
    } else {
        Write-Host "  STAN is not installed. Run install-stan.bat first." -ForegroundColor Red
        exit 1
    }
}

$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $venv = $oldVenv
    $venvPython = Join-Path $oldVenv "Scripts\python.exe"
}

# -- Update STAN --
Write-Host "  [1/3] Updating STAN..." -ForegroundColor Cyan
$stanExe = "$venv\Scripts\stan.exe"
$pipTrust = @("--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org", "--trusted-host", "github.com", "--trusted-host", "objects.githubusercontent.com")
$t = [DateTime]::Now.Ticks

# Kill any running stan.exe (watcher, dashboard, etc) so pip can
# overwrite the executable. Without this, pip hits WinError 32 and
# leaves the venv half-installed -> ModuleNotFoundError on next launch.
# This is the root cause of the 16:22 and 16:28 update failures today.
Write-Host "  Stopping running stan.exe processes..." -ForegroundColor Gray
Get-Process stan -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

# Nuke any half-broken stan install artifacts. Past failed updates
# leave a stan-0.2.XX.dist-info\ dir WITHOUT a RECORD manifest -- pip
# then can't determine what to uninstall under --force-reinstall,
# reports 'error: uninstall-no-record-file', and exits 1. Removing
# the package dir + dist-info before install skips the uninstall
# step entirely. Brett's Exploris 2026-04-14 regression.
$sitePackages = Join-Path $venv "Lib\site-packages"
if (Test-Path $sitePackages) {
    Write-Host "  Clearing stale stan package artifacts..." -ForegroundColor Gray
    $stanPkg = Join-Path $sitePackages "stan"
    if (Test-Path $stanPkg) { Remove-Item -Recurse -Force $stanPkg -ErrorAction SilentlyContinue }
    Get-ChildItem $sitePackages -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "stan-*.dist-info" -or $_.Name -like "stan_proteomics-*.dist-info" } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

# Real failures we should actually block on. Deliberately narrow -- pip
# prints plenty of noise that looks scary but isn't. "uninstall-no-
# record-file" in particular is just "I can't remove the prior install
# because its RECORD manifest is gone; moving on" and always resolves
# fine under --force-reinstall. Trust pip's exit code as the final
# word; match only the dramatic, unambiguous-failure strings here.
$pipFatalError = $false
& $venvPython -m pip install --no-cache-dir --force-reinstall @pipTrust "https://github.com/bsphinney/stan/archive/refs/heads/main.zip?t=$t" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "Successfully installed") {
        Write-Host "  $line" -ForegroundColor Green
    } elseif ($line -match "WinError 32|Could not install packages|No matching distribution|HTTP error") {
        Write-Host "  $line" -ForegroundColor Red
        $script:pipFatalError = $true
    } elseif ($line -match "^error|^ERROR") {
        # Warnings like 'error: uninstall-no-record-file' -- yellow, not red.
        Write-Host "  $line" -ForegroundColor DarkYellow
    }
}

if ($pipFatalError -or $LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: pip reported fatal errors (exit=$LASTEXITCODE). The venv may be in a partial state." -ForegroundColor Red
    Write-Host "         Close every cmd window running stan.exe, then re-run this script." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $stanExe)) {
    Write-Host "  ERROR: STAN update failed." -ForegroundColor Red
    exit 1
}

# Confirm the new install actually imports -- catches the broken-venv
# case where files land but the package is incomplete.
& $venvPython -c "import stan; print('  STAN v' + stan.__version__)" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "ModuleNotFoundError|Error") {
        Write-Host "  ERROR: installed package does not import:" -ForegroundColor Red
        Write-Host "  $line" -ForegroundColor Red
        exit 1
    } else {
        Write-Host $line -ForegroundColor Green
    }
}
Write-Host "  STAN updated." -ForegroundColor Green

# Install fisher_py for fast Thermo .raw TIC extraction + Sample Health
# monitor. Depends on pythonnet + .NET -- optional, STAN falls back to
# ThermoRawFileParser if this fails. Don't block the update on failure.
Write-Host "  Installing fisher_py (Thermo fast-path)..." -ForegroundColor Gray
& $venvPython -m pip install --quiet @pipTrust fisher_py 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "Successfully installed") {
        Write-Host "  $line" -ForegroundColor Green
    } elseif ($line -match "error|ERROR") {
        Write-Host "  fisher_py: $line" -ForegroundColor DarkYellow
    }
}
$fisherOk = & $venvPython -c "import fisher_py; print('ok')" 2>&1
if ($fisherOk -match "ok") {
    Write-Host "  fisher_py available." -ForegroundColor Green
} else {
    Write-Host "  fisher_py not available (Thermo TIC falls back to TRFP - slower but works)." -ForegroundColor Yellow
}

# Install alphatims for Bruker MS1 spectrum access (PEG contamination
# detection, `stan backfill-peg`). First install downloads ~150 MB of
# deps (numpy/pandas are usually already present; alphatims itself is
# small, h5py/pyzstd/tqdm are the new ones). Same don't-block-on-failure
# pattern as fisher_py -- PEG detection is optional; STAN proper works
# without it.
Write-Host "  Installing alphatims (Bruker PEG reader)..." -ForegroundColor Gray
& $venvPython -m pip install --quiet @pipTrust alphatims 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "Successfully installed") {
        Write-Host "  $line" -ForegroundColor Green
    } elseif ($line -match "error|ERROR") {
        Write-Host "  alphatims: $line" -ForegroundColor DarkYellow
    }
}
$alphatimsOk = & $venvPython -c "import alphatims; print('ok')" 2>&1
if ($alphatimsOk -match "ok") {
    Write-Host "  alphatims available." -ForegroundColor Green
} else {
    Write-Host "  alphatims not available (PEG detection disabled). Rerun update to retry." -ForegroundColor Yellow
}

# If both venvs exist, retire the old .stan location.
# Pre-v0.2.137 also re-installed STAN into the old .stan venv on
# every update. That added 60-120 sec to each update because it
# was a full --force-reinstall pulling the whole repo. The user
# already migrated to STAN\venv earlier in this script, and PATH
# now points only at the new venv, so re-installing into the old
# one is wasted work. We keep the PATH cleanup (fast, useful) and
# drop the re-install. Operator can manually `rmdir /s .stan`
# whenever they want to free the disk space; nothing relies on it.
$newStanExe = Join-Path $env:USERPROFILE "STAN\venv\Scripts\stan.exe"
if ((Test-Path $newStanExe) -and (Test-Path $oldVenvPython)) {
    $oldScripts = Join-Path $oldVenv "Scripts"
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -and $userPath -like "*$oldScripts*") {
        $parts = $userPath -split ";"
        $filtered = @()
        foreach ($p in $parts) { if ($p -ne $oldScripts -and $p -ne "") { $filtered += $p } }
        $cleanPath = $filtered -join ";"
        [Environment]::SetEnvironmentVariable("PATH", $cleanPath, "User")
        $env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));$cleanPath"
        Write-Host "  Removed old .stan\venv from PATH." -ForegroundColor Gray
    }
    Write-Host "  Old .stan venv left in place (no longer on PATH). Delete manually to free disk space." -ForegroundColor Gray
}

# -- Check DIA-NN (2.3+ required for community benchmark) --
Write-Host ""
Write-Host "  [2/3] Checking DIA-NN..." -ForegroundColor Cyan
$env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));$([Environment]::GetEnvironmentVariable('Path','User'))"

$diannSearchPaths = @(
    "C:\DIA-NN",
    "C:\Program Files\DIA-NN",
    "C:\DiaNN",
    "C:\Program Files\DiaNN"
)

$allDiann = @()
foreach ($sp in $diannSearchPaths) {
    if (Test-Path $sp) {
        $exes = Get-ChildItem -Path $sp -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue
        foreach ($e in $exes) { $allDiann += $e.FullName }
    }
}
$onPath = Get-Command "DiaNN.exe" -ErrorAction SilentlyContinue
if ($onPath) { $allDiann += $onPath.Source }

$bestDiann = $null
$bestMajor = 0
$bestMinor = 0
foreach ($p in $allDiann) {
    if ($p -match "(\d+)\.(\d+)") {
        $maj = [int]$Matches[1]
        $min = [int]$Matches[2]
        if ($maj -gt $bestMajor -or ($maj -eq $bestMajor -and $min -gt $bestMinor)) {
            $bestMajor = $maj
            $bestMinor = $min
            $bestDiann = $p
        }
    }
}

$needsDiannInstall = $false
$isCompatible = $false
if ($bestDiann -and ($bestMajor -gt 2 -or ($bestMajor -eq 2 -and $bestMinor -ge 3))) {
    $isCompatible = $true
}

if ($isCompatible) {
    Write-Host "  DIA-NN found: $bestDiann (v$bestMajor.$bestMinor)" -ForegroundColor Green
    $diannDir = Split-Path $bestDiann -Parent
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -notlike "*$diannDir*") {
        [Environment]::SetEnvironmentVariable("PATH", "$userPath;$diannDir", "User")
        $env:Path = "$diannDir;$env:Path"
        Write-Host "  Added $diannDir to PATH." -ForegroundColor Gray
    }
} elseif ($bestDiann) {
    Write-Host "  DIA-NN found but outdated: $bestDiann (v$bestMajor.$bestMinor)" -ForegroundColor Yellow
    Write-Host "  Community benchmark requires DIA-NN 2.3+. Upgrading..." -ForegroundColor Yellow
    $needsDiannInstall = $true
} else {
    Write-Host "  DIA-NN not found. Installing..." -ForegroundColor Yellow
    $needsDiannInstall = $true
}

if ($needsDiannInstall) {
    $ErrorActionPreference = "Continue"
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/vdemichev/DiaNN/releases/latest" -TimeoutSec 15
        # The "latest" release contains multiple versions -- pick the highest
        $asset = Select-LatestDiannMsi $rel.assets
        if ($asset) {
            $installer = "$env:TEMP\$($asset.name)"
            Write-Host "  Downloading $($asset.name)..." -ForegroundColor Gray
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $installer -UseBasicParsing
            Write-Host "  Running installer (silent)..." -ForegroundColor Gray
            $proc = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$installer`"", "/quiet", "/norestart" -Wait -PassThru
            if ($proc.ExitCode -ne 0) {
                Write-Host "  Silent failed. Trying with admin..." -ForegroundColor Yellow
                Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$installer`"", "/passive", "/norestart" -Wait -Verb RunAs
            }
            Remove-Item $installer -ErrorAction SilentlyContinue
            $env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));$([Environment]::GetEnvironmentVariable('Path','User'))"
            foreach ($sp in $diannSearchPaths) {
                if (Test-Path $sp) {
                    $f = Get-ChildItem -Path $sp -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($f) {
                        $d = Split-Path $f.FullName -Parent
                        $up = [Environment]::GetEnvironmentVariable("PATH", "User")
                        if ($up -notlike "*$d*") {
                            [Environment]::SetEnvironmentVariable("PATH", "$up;$d", "User")
                        }
                        Write-Host "  DIA-NN installed: $($f.FullName)" -ForegroundColor Green
                        break
                    }
                }
            }
        } else {
            Write-Host "  No MSI found. Install manually: https://github.com/vdemichev/DiaNN/releases" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  Could not install DIA-NN: $_" -ForegroundColor Yellow
    }
    $ErrorActionPreference = "Stop"
}

# -- Check Sage --
Write-Host ""
Write-Host "  [3/3] Checking Sage..." -ForegroundColor Cyan
$env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));$([Environment]::GetEnvironmentVariable('Path','User'))"
$sageExe = Get-Command "sage.exe" -ErrorAction SilentlyContinue
$sageDir = "$env:USERPROFILE\STAN\tools\sage"

if (-not $sageExe) {
    if (Test-Path $sageDir) {
        $f = Get-ChildItem -Path $sageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { $sageExe = $f }
    }
    $oldSageDir = "$env:USERPROFILE\.stan\tools\sage"
    if (-not $sageExe -and (Test-Path $oldSageDir)) {
        $f = Get-ChildItem -Path $oldSageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { $sageExe = $f }
    }
}

if ($sageExe) {
    $sp = $sageExe.FullName
    if ($sageExe.Source) { $sp = $sageExe.Source }
    Write-Host "  Sage found: $sp" -ForegroundColor Green
} else {
    Write-Host "  Sage not found. Installing..." -ForegroundColor Yellow
    $ErrorActionPreference = "Continue"
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/lazear/sage/releases/latest" -TimeoutSec 15
        $asset = $rel.assets | Where-Object { $_.name -match "windows" -and $_.name -match "\.zip$" } | Select-Object -First 1
        if ($asset) {
            $zip = "$env:TEMP\$($asset.name)"
            Write-Host "  Downloading $($asset.name)..." -ForegroundColor Gray
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing
            if (-not (Test-Path $sageDir)) { New-Item -ItemType Directory -Path $sageDir -Force | Out-Null }
            Expand-Archive -Path $zip -DestinationPath $sageDir -Force
            Remove-Item $zip -ErrorAction SilentlyContinue
            $f = Get-ChildItem -Path $sageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($f) {
                $d = Split-Path $f.FullName -Parent
                $up = [Environment]::GetEnvironmentVariable("PATH", "User")
                if ($up -notlike "*$d*") {
                    [Environment]::SetEnvironmentVariable("PATH", "$up;$d", "User")
                }
                Write-Host "  Sage installed: $($f.FullName)" -ForegroundColor Green
            }
        } else {
            Write-Host "  No Windows zip found. Install manually: https://github.com/lazear/sage/releases" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  Could not install Sage: $_" -ForegroundColor Yellow
    }
    $ErrorActionPreference = "Stop"
}

# -- Self-update bat files --
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $scriptDir) { $scriptDir = Get-Location }
try {
    $t = [DateTime]::Now.Ticks
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/update-stan.bat?t=$t" -OutFile "$scriptDir\update-stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/start_stan.bat?t=$t" -OutFile "$scriptDir\start_stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
    # start_stan_loop.bat is the supervised wrapper used by the fleet
    # restart_watcher action. Refreshed here on every update so each
    # instrument always has the current version (the .bat lives at the
    # repo root, not in the pip package, so update-stan.bat has to
    # fetch it explicitly).
    # Dropped alongside update-stan.bat in $scriptDir so operators find
    # both in the same location (typically %USERPROFILE%\Downloads).
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/start_stan_loop.bat?t=$t" -OutFile "$scriptDir\start_stan_loop.bat" -UseBasicParsing -ErrorAction SilentlyContinue
} catch {}

# -- Done --
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "    STAN is up to date!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Starting dashboard..." -ForegroundColor Cyan
& $stanExe dashboard

