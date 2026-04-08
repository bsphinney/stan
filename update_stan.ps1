# STAN Updater — reinstalls STAN and checks for missing search engines

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

# Use new STAN directory, fall back to old .stan
$venv = "$env:USERPROFILE\STAN\venv"
$venvPython = "$venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    # Also check old location
    if (Test-Path "$env:USERPROFILE\.stan\venv\Scripts\python.exe") {
        $venv = "$env:USERPROFILE\.stan\venv"
        $venvPython = "$venv\Scripts\python.exe"
    } else {
        Write-Host "  STAN is not installed. Run install-stan.bat first." -ForegroundColor Red
        exit 1
    }
}

# -- Update STAN --
Write-Host "  [1/3] Updating STAN..." -ForegroundColor Cyan

# Show current version before update
$stanExe = "$venv\Scripts\stan.exe"
$oldVer = ""
if (Test-Path $stanExe) {
    try { $oldVer = & $stanExe version 2>&1 | Out-String } catch {}
    $oldVer = $oldVer.Trim()
}

$pipTrust = @("--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org", "--trusted-host", "github.com", "--trusted-host", "objects.githubusercontent.com")
$installOk = $false
& $venvPython -m pip install --no-cache-dir --force-reinstall @pipTrust "https://github.com/bsphinney/stan/archive/refs/heads/main.zip" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "Successfully installed") { Write-Host "  $line" -ForegroundColor Green; $script:installOk = $true }
    elseif ($line -match "ERROR|error") { Write-Host "  $line" -ForegroundColor Red }
}

if (-not (Test-Path $stanExe)) {
    Write-Host "  ERROR: STAN update failed — stan.exe not found." -ForegroundColor Red
    Write-Host "  Was another process using stan.exe? Close all STAN windows and retry." -ForegroundColor Yellow
    exit 1
}

# Show version after update
$newVer = ""
try { $newVer = & $stanExe version 2>&1 | Out-String } catch {}
$newVer = $newVer.Trim()

if ($oldVer -and $newVer -and $oldVer -eq $newVer) {
    Write-Host "  STAN $newVer (no change)." -ForegroundColor Gray
} elseif ($newVer) {
    Write-Host "  STAN updated: $newVer" -ForegroundColor Green
} else {
    Write-Host "  STAN updated." -ForegroundColor Green
}

# -- Check DIA-NN --
Write-Host ""
Write-Host "  [2/3] Checking DIA-NN..." -ForegroundColor Cyan
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
$diannExe = Get-Command "DiaNN.exe" -ErrorAction SilentlyContinue
if (-not $diannExe) { $diannExe = Get-Command "diann.exe" -ErrorAction SilentlyContinue }

$diannSearchPaths = @(
    "C:\DIA-NN", "C:\Program Files\DIA-NN", "$env:LOCALAPPDATA\DIA-NN",
    "C:\DiaNN", "C:\Program Files\DiaNN", "$env:LOCALAPPDATA\DiaNN",
    "$env:PROGRAMFILES\DIA-NN", "$env:PROGRAMFILES(x86)\DIA-NN"
)

# Also check common install locations
if (-not $diannExe) {
    foreach ($searchPath in $diannSearchPaths) {
        if (Test-Path $searchPath) {
            $found = Get-ChildItem -Path $searchPath -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $found) { $found = Get-ChildItem -Path $searchPath -Recurse -Filter "diann.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 }
            if ($found) { $diannExe = $found; break }
        }
    }
}

# Collect ALL DIA-NN installs, pick the newest
$allDiann = @()
if ($diannExe) {
    $p = if ($diannExe.Source) { $diannExe.Source } else { $diannExe.FullName }
    $allDiann += $p
}
foreach ($searchPath in $diannSearchPaths) {
    if (Test-Path $searchPath) {
        $found = Get-ChildItem -Path $searchPath -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue
        foreach ($f in $found) {
            if ($allDiann -notcontains $f.FullName) { $allDiann += $f.FullName }
        }
    }
}

# Pick the newest version (prefer 2.x over 1.x)
$bestDiann = $null
$bestVer = @(0, 0, 0)
foreach ($p in $allDiann) {
    if ($p -match "(\d+)\.(\d+)\.?(\d*)") {
        $ver = @([int]$Matches[1], [int]$Matches[2], [int]$(if ($Matches[3]) { $Matches[3] } else { 0 }))
        if ($ver[0] -gt $bestVer[0] -or ($ver[0] -eq $bestVer[0] -and $ver[1] -gt $bestVer[1])) {
            $bestVer = $ver
            $bestDiann = $p
        }
    }
}

$needsDiannInstall = $false
if ($bestDiann -and $bestVer[0] -ge 2) {
    $verStr = "$($bestVer[0]).$($bestVer[1])"
    Write-Host "  DIA-NN found: $bestDiann (v$verStr)" -ForegroundColor Green
    # Ensure it's on PATH
    $diannDir = Split-Path $bestDiann -Parent
    $userPath = [Environment]::GetEnvironmentVariable("PATH","User")
    if ($userPath -notlike "*$diannDir*") {
        $newPath = "$userPath;$diannDir"
                [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
        $env:Path = "$diannDir;$env:Path"
        Write-Host "  Added $diannDir to PATH." -ForegroundColor Gray
    }
} elseif ($bestDiann) {
    $verStr = "$($bestVer[0]).$($bestVer[1])"
    Write-Host "  DIA-NN found but outdated: $bestDiann (v$verStr)" -ForegroundColor Yellow
    Write-Host "  Version 2.0+ required. Upgrading..." -ForegroundColor Yellow
    $needsDiannInstall = $true
} else {
    Write-Host "  DIA-NN not found. Installing..." -ForegroundColor Yellow
    $needsDiannInstall = $true
}

if ($needsDiannInstall) {
    $ErrorActionPreference = "Continue"
    try {
        $diannRelease = Invoke-RestMethod "https://api.github.com/repos/vdemichev/DiaNN/releases/latest" -TimeoutSec 15
        $diannAsset = $diannRelease.assets | Where-Object { $_.name -match "\.msi$" -and $_.name -notmatch "linux" } | Select-Object -First 1
        if (-not $diannAsset) {
            $diannAsset = $diannRelease.assets | Where-Object { $_.name -match "\.exe$" -and $_.name -notmatch "linux" } | Select-Object -First 1
        }
        if ($diannAsset) {
            $diannUrl = $diannAsset.browser_download_url
            $diannInstaller = "$env:TEMP\$($diannAsset.name)"
            Write-Host "  Downloading $($diannAsset.name)..." -ForegroundColor Gray
            Invoke-WebRequest -Uri $diannUrl -OutFile $diannInstaller -UseBasicParsing
            Write-Host "  Running DIA-NN installer (silent)..." -ForegroundColor Gray
            if ($diannInstaller -match "\.msi$") {
                # Try silent install first (may need admin)
                $proc = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$diannInstaller`"", "/quiet", "/norestart" -Wait -PassThru
                if ($proc.ExitCode -ne 0) {
                    $exitCode = $proc.ExitCode
                    Write-Host "  Silent install failed (exit $exitCode). Trying with admin prompt..." -ForegroundColor Yellow
                    $proc = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$diannInstaller`"", "/passive", "/norestart" -Wait -PassThru -Verb RunAs
                }
                if ($proc.ExitCode -ne 0) {
                    $exitCode = $proc.ExitCode
                    Write-Host "  MSI install failed with exit code $exitCode." -ForegroundColor Red
                    Write-Host "  Try running update-stan.bat as Administrator, or install DIA-NN manually." -ForegroundColor Yellow
                }
            } else {
                $proc = Start-Process -FilePath $diannInstaller -ArgumentList "/S" -Wait -PassThru
                if ($proc.ExitCode -ne 0) { Start-Process -FilePath $diannInstaller -ArgumentList "/VERYSILENT" -Wait }
            }
            Remove-Item $diannInstaller -ErrorAction SilentlyContinue

            # Find and add to PATH
            $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
            $diannFound = $false
            foreach ($searchPath in $diannSearchPaths) {
                if (Test-Path $searchPath) {
                    $found = Get-ChildItem -Path $searchPath -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($found) {
                        $diannDir = Split-Path $found.FullName -Parent
                        $userPath = [Environment]::GetEnvironmentVariable("PATH","User")
                        if ($userPath -notlike "*$diannDir*") {
                            $newPath = "$userPath;$diannDir"
                [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
                        }
                        Write-Host "  DIA-NN installed at $($found.FullName)" -ForegroundColor Green
                        $diannFound = $true
                        break
                    }
                }
            }
            if (-not $diannFound) {
                Write-Host "  DIA-NN installer ran but DiaNN.exe not found on disk." -ForegroundColor Red
                Write-Host "  Install manually: https://github.com/vdemichev/DiaNN/releases" -ForegroundColor Yellow
            }
        } else {
            Write-Host "  No Windows installer found. Install manually: https://github.com/vdemichev/DiaNN/releases" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  Could not install DIA-NN: $_" -ForegroundColor Yellow
        Write-Host "  Install manually: https://github.com/vdemichev/DiaNN/releases" -ForegroundColor Yellow
    }
    $ErrorActionPreference = "Stop"
}

# -- Check Sage --
Write-Host ""
Write-Host "  [3/3] Checking Sage..." -ForegroundColor Cyan
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
$sageExe = Get-Command "sage.exe" -ErrorAction SilentlyContinue
$sageDir = "$env:USERPROFILE\STAN\tools\sage"

if (-not $sageExe) {
    # Check tools directory
    $found = Get-ChildItem -Path $sageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $sageExe = $found }
}

if ($sageExe) {
    $sagePath = if ($sageExe.Source) { $sageExe.Source } else { $sageExe.FullName }
    Write-Host "  Sage found: $sagePath" -ForegroundColor Green
} else {
    Write-Host "  Sage not found. Installing..." -ForegroundColor Yellow
    $ErrorActionPreference = "Continue"
    try {
        $sageRelease = Invoke-RestMethod "https://api.github.com/repos/lazear/sage/releases/latest" -TimeoutSec 15
        $sageAsset = $sageRelease.assets | Where-Object { $_.name -match "windows" -and $_.name -match "\.zip$" } | Select-Object -First 1
        if ($sageAsset) {
            $sageZip = "$env:TEMP\$($sageAsset.name)"
            Write-Host "  Downloading $($sageAsset.name)..." -ForegroundColor Gray
            Invoke-WebRequest -Uri $sageZip -OutFile $sageZip -UseBasicParsing
            if (-not (Test-Path $sageDir)) { New-Item -ItemType Directory -Path $sageDir -Force | Out-Null }
            Expand-Archive -Path $sageZip -DestinationPath $sageDir -Force
            Remove-Item $sageZip -ErrorAction SilentlyContinue
            $found = Get-ChildItem -Path $sageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($found) {
                $sageExeDir = Split-Path $found.FullName -Parent
                $userPath = [Environment]::GetEnvironmentVariable("PATH","User")
                if ($userPath -notlike "*$sageExeDir*") {
                    $newPath = "$userPath;$sageExeDir"
                [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
                }
                Write-Host "  Sage installed: $($found.FullName)" -ForegroundColor Green
            }
        } else {
            Write-Host "  No Windows zip found. Install manually: https://github.com/lazear/sage/releases" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  Could not install Sage: $_" -ForegroundColor Yellow
        Write-Host "  Install manually: https://github.com/lazear/sage/releases" -ForegroundColor Yellow
    }
    $ErrorActionPreference = "Stop"
}

# -- Self-update .bat files for next run --
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $scriptDir) { $scriptDir = Get-Location }
try {
    $batUrl = "https://raw.githubusercontent.com/bsphinney/stan/main/update-stan.bat"
    $t = [DateTime]::Now.Ticks
    Invoke-WebRequest -Uri "$batUrl`?t=$t" -OutFile "$scriptDir\update-stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
} catch {}

# -- Done --
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "    STAN is up to date!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Starting dashboard..." -ForegroundColor Cyan
& $stanExe dashboard
