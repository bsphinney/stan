# STAN Installer v10 - downloaded and executed by install.bat

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "    STAN - Standardized proteomic Throughput ANalyzer" -ForegroundColor Cyan
Write-Host "    Know your instrument." -ForegroundColor Cyan
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "  Installer v10" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  This will install STAN on your instrument workstation."
Write-Host "  No admin rights required. Takes about 2 minutes."
Write-Host ""

# -- License --
Write-Host "  STAN uses DIA-NN (free academic, commercial license required)" -ForegroundColor Gray
Write-Host "  and Sage (MIT open source) as external search engines." -ForegroundColor Gray
Write-Host ""
$accept = Read-Host "  Accept DIA-NN and Sage license terms? (Y/n)"
if ($accept -eq "n") { Write-Host "  Cancelled." -ForegroundColor Yellow; exit 0 }
Write-Host "  License accepted." -ForegroundColor Green

# -- SSL workaround for corporate/university proxy networks --
# Some networks (e.g. UC Davis) use SSL inspection that breaks certificate verification.
# This callback trusts all certs for PowerShell web requests in this session only.
try {
    Add-Type @"
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public class TrustAll {
    public static void Enable() {
        ServicePointManager.ServerCertificateValidationCallback =
            delegate { return true; };
    }
}
"@
    [TrustAll]::Enable()
} catch {
    # Already defined from a previous run, or .NET type not available — safe to ignore
}

# -- Find Python --
Write-Host ""
Write-Host "  [1/7] Checking for Python..." -ForegroundColor Cyan

function Find-Python {
    # Check PATH first
    foreach ($cmd in @("python", "python3", "py")) {
        try {
            $p = Get-Command $cmd -ErrorAction SilentlyContinue
            if ($p) {
                $ver = & $p.Source --version 2>&1
                if ($ver -match "3\.(1[0-9]|[2-9][0-9])") {
                    return $p.Source
                }
            }
        } catch {}
    }
    # Check common install locations directly (PATH may not be updated yet)
    $locations = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe",
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe"
    )
    foreach ($loc in $locations) {
        if (Test-Path $loc) {
            $ver = & $loc --version 2>&1
            if ($ver -match "3\.(1[0-9]|[2-9][0-9])") {
                return $loc
            }
        }
    }
    return $null
}

# Refresh PATH from registry (picks up changes from previous installs)
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")

$python = Find-Python

if (-not $python) {
    Write-Host "  Python 3.10+ not found. Downloading from python.org..." -ForegroundColor Yellow
    $pyUrl = "https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe"
    $pyInst = "$env:TEMP\python-installer.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $pyUrl -OutFile $pyInst -UseBasicParsing
    } catch {
        Write-Host "  ERROR: Download failed." -ForegroundColor Red
        Write-Host "  Install Python 3.12 from https://www.python.org/downloads/" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  Installing Python..." -ForegroundColor Yellow
    Start-Process -FilePath $pyInst -ArgumentList "/passive","InstallAllUsers=0","PrependPath=1","Include_test=0" -Wait
    Remove-Item $pyInst -ErrorAction SilentlyContinue

    # Refresh PATH and search again
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    $python = Find-Python

    if (-not $python) {
        Write-Host "  ERROR: Python still not found after installation." -ForegroundColor Red
        Write-Host "  Please close this window and try again." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  Python installed." -ForegroundColor Green
} else {
    $ver = & $python --version 2>&1
    Write-Host "  Found $ver" -ForegroundColor Green
}

# -- Virtual environment --
Write-Host ""
Write-Host "  [2/7] Creating virtual environment..." -ForegroundColor Cyan
$venv = "$env:USERPROFILE\STAN\venv"
# Fresh start in visible STAN directory (old .stan may have permission issues)
if (-not (Test-Path "$env:USERPROFILE\STAN")) { New-Item -ItemType Directory -Path "$env:USERPROFILE\STAN" -Force | Out-Null }
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    & $python -m venv $venv
}
Write-Host "  Done." -ForegroundColor Green

# Activate
& "$venv\Scripts\Activate.ps1"

# -- Install STAN --
Write-Host ""
Write-Host "  [3/7] Installing STAN (may take a minute)..." -ForegroundColor Cyan

# Use venv python -m pip to guarantee we're in the right environment
$venvPython = "$venv\Scripts\python.exe"
$ErrorActionPreference = "Continue"
Write-Host "  Upgrading pip + setuptools..." -ForegroundColor Gray
$pipTrust = @("--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org", "--trusted-host", "github.com", "--trusted-host", "objects.githubusercontent.com")
& $venvPython -m pip install --upgrade pip setuptools wheel @pipTrust 2>&1 | Out-Null
Write-Host "  Installing STAN package..." -ForegroundColor Gray
& $venvPython -m pip install --no-cache-dir --force-reinstall @pipTrust "https://github.com/bsphinney/stan/archive/refs/heads/main.zip" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "Successfully installed") { Write-Host "  $line" -ForegroundColor Green }
    elseif ($line -match "ERROR|error") { Write-Host "  $line" -ForegroundColor Red }
}
$ErrorActionPreference = "Stop"

# Verify
$stanExe = "$venv\Scripts\stan.exe"
if (-not (Test-Path $stanExe)) {
    Write-Host "  ERROR: STAN installation failed." -ForegroundColor Red
    exit 1
}
Write-Host "  STAN installed." -ForegroundColor Green

# -- Install DIA-NN --
Write-Host ""
Write-Host "  [4/7] Installing DIA-NN..." -ForegroundColor Cyan
$diannInstalled = $false

# Check if DIA-NN 2.0+ is already installed
$diannSearchPaths = @(
    "C:\DIA-NN", "C:\Program Files\DIA-NN", "$env:LOCALAPPDATA\DIA-NN",
    "C:\DiaNN", "C:\Program Files\DiaNN", "$env:LOCALAPPDATA\DiaNN",
    "$env:PROGRAMFILES\DIA-NN", "$env:PROGRAMFILES(x86)\DIA-NN"
)
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
$allDiann = @()
$onPath = Get-Command "DiaNN.exe" -ErrorAction SilentlyContinue
if ($onPath) { $allDiann += $onPath.Source }
foreach ($sp in $diannSearchPaths) {
    if (Test-Path $sp) {
        $found = Get-ChildItem -Path $sp -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue
        foreach ($f in $found) { if ($allDiann -notcontains $f.FullName) { $allDiann += $f.FullName } }
    }
}
$bestDiann = $null
foreach ($p in $allDiann) {
    if ($p -match "(\d+)\.(\d+)") {
        if ([int]$Matches[1] -ge 2) { $bestDiann = $p; break }
    }
}
if ($bestDiann) {
    Write-Host "  DIA-NN 2.0+ already installed: $bestDiann" -ForegroundColor Green
    $diannDir = Split-Path $bestDiann -Parent
    $userPath = [Environment]::GetEnvironmentVariable("PATH","User")
    if ($userPath -notlike "*$diannDir*") {
        [Environment]::SetEnvironmentVariable("PATH","$userPath;$diannDir","User")
        $env:Path = "$diannDir;$env:Path"
        Write-Host "  Added $diannDir to PATH." -ForegroundColor Gray
    }
    $diannInstalled = $true
}

if (-not $diannInstalled) {
$ErrorActionPreference = "Continue"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $diannRelease = Invoke-RestMethod "https://api.github.com/repos/vdemichev/DiaNN/releases/latest" -TimeoutSec 15
    # Look for .msi first (DIA-NN 2.x), then .exe (older versions)
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
            # MSI installer — try silent, then passive with admin prompt if it fails
            $diannProc = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$diannInstaller`"", "/quiet", "/norestart" -Wait -PassThru
            if ($diannProc.ExitCode -ne 0) {
                Write-Host "  Silent install failed (exit $($diannProc.ExitCode)). Trying with admin prompt..." -ForegroundColor Yellow
                $diannProc = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$diannInstaller`"", "/passive", "/norestart" -Wait -PassThru -Verb RunAs
            }
        } else {
            # EXE installer (legacy)
            $diannProc = Start-Process -FilePath $diannInstaller -ArgumentList "/S" -Wait -PassThru
            if ($diannProc.ExitCode -ne 0) {
                Write-Host "  Silent install returned exit code $($diannProc.ExitCode), trying /VERYSILENT..." -ForegroundColor Yellow
                Start-Process -FilePath $diannInstaller -ArgumentList "/VERYSILENT" -Wait
            }
        }
        Remove-Item $diannInstaller -ErrorAction SilentlyContinue

        # Find DiaNN.exe and add to PATH
        $diannExe = $null
        $diannSearchPaths = @(
            "C:\DIA-NN",
            "C:\Program Files\DIA-NN",
            "$env:LOCALAPPDATA\DIA-NN",
            "C:\DiaNN",
            "C:\Program Files\DiaNN",
            "$env:LOCALAPPDATA\DiaNN",
            "$env:PROGRAMFILES\DIA-NN",
            "$env:PROGRAMFILES(x86)\DIA-NN"
        )
        foreach ($searchPath in $diannSearchPaths) {
            if (Test-Path $searchPath) {
                $found = Get-ChildItem -Path $searchPath -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
                if (-not $found) {
                    $found = Get-ChildItem -Path $searchPath -Recurse -Filter "diann.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
                }
                if ($found) {
                    $diannExe = $found.FullName
                    break
                }
            }
        }
        # Also check PATH after install
        if (-not $diannExe) {
            $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
            $onPath = Get-Command "DiaNN.exe" -ErrorAction SilentlyContinue
            if (-not $onPath) { $onPath = Get-Command "diann.exe" -ErrorAction SilentlyContinue }
            if ($onPath) { $diannExe = $onPath.Source }
        }
        if ($diannExe) {
            $diannDir = Split-Path $diannExe -Parent
            $userPath = [Environment]::GetEnvironmentVariable("PATH","User")
            if ($userPath -notlike "*$diannDir*") {
                [Environment]::SetEnvironmentVariable("PATH","$userPath;$diannDir","User")
                $env:Path = "$diannDir;$env:Path"
                Write-Host "  Added $diannDir to PATH." -ForegroundColor Gray
            }
            $diannVer = & $diannExe 2>&1 | Select-String -Pattern "DIA-NN|version|v\d" | Select-Object -First 1
            if ($diannVer) {
                Write-Host "  DIA-NN installed: $diannVer" -ForegroundColor Green
            } else {
                Write-Host "  DIA-NN installed at $diannExe" -ForegroundColor Green
            }
            $diannInstalled = $true
        } else {
            Write-Host "  DIA-NN installer completed but DiaNN.exe not found on disk." -ForegroundColor Yellow
            Write-Host "  You may need to install DIA-NN manually: https://github.com/vdemichev/DiaNN/releases" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  No Windows .exe found in latest DIA-NN release." -ForegroundColor Yellow
        Write-Host "  Install DIA-NN manually: https://github.com/vdemichev/DiaNN/releases" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Could not download/install DIA-NN automatically: $_" -ForegroundColor Yellow
    Write-Host "  Install DIA-NN manually: https://github.com/vdemichev/DiaNN/releases" -ForegroundColor Yellow
}
if (-not $diannInstalled) {
    Write-Host "  Skipped (STAN will still work, but DIA searches require DIA-NN)." -ForegroundColor Yellow
}
$ErrorActionPreference = "Stop"
}  # end if (-not $diannInstalled) — skip download if 2.0+ already present

# -- Install Sage --
Write-Host ""
Write-Host "  [5/7] Installing Sage..." -ForegroundColor Cyan
$sageInstalled = $false
$sageDir = "$env:USERPROFILE\STAN\tools\sage"
$ErrorActionPreference = "Continue"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $sageRelease = Invoke-RestMethod "https://api.github.com/repos/lazear/sage/releases/latest" -TimeoutSec 15
    $sageAsset = $sageRelease.assets | Where-Object { $_.name -match "windows" -and $_.name -match "\.zip$" } | Select-Object -First 1
    if ($sageAsset) {
        $sageUrl = $sageAsset.browser_download_url
        $sageZip = "$env:TEMP\$($sageAsset.name)"
        Write-Host "  Downloading $($sageAsset.name)..." -ForegroundColor Gray
        Invoke-WebRequest -Uri $sageUrl -OutFile $sageZip -UseBasicParsing

        # Extract to tools directory
        if (-not (Test-Path $sageDir)) { New-Item -ItemType Directory -Path $sageDir -Force | Out-Null }
        Write-Host "  Extracting to $sageDir..." -ForegroundColor Gray
        Expand-Archive -Path $sageZip -DestinationPath $sageDir -Force
        Remove-Item $sageZip -ErrorAction SilentlyContinue

        # Sage zips sometimes have a nested directory; find sage.exe
        $sageExe = Get-ChildItem -Path $sageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($sageExe) {
            $sageExeDir = Split-Path $sageExe.FullName -Parent
            $userPath = [Environment]::GetEnvironmentVariable("PATH","User")
            if ($userPath -notlike "*$sageExeDir*") {
                [Environment]::SetEnvironmentVariable("PATH","$userPath;$sageExeDir","User")
                $env:Path = "$sageExeDir;$env:Path"
                Write-Host "  Added $sageExeDir to PATH." -ForegroundColor Gray
            }
            $sageVer = & $sageExe.FullName --version 2>&1
            Write-Host "  Sage installed: $sageVer" -ForegroundColor Green
            $sageInstalled = $true
        } else {
            Write-Host "  Extraction succeeded but sage.exe not found." -ForegroundColor Yellow
            Write-Host "  Check $sageDir manually." -ForegroundColor Yellow
        }
    } else {
        Write-Host "  No Windows .zip found in latest Sage release." -ForegroundColor Yellow
        Write-Host "  Install Sage manually: https://github.com/lazear/sage/releases" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Could not download/install Sage automatically: $_" -ForegroundColor Yellow
    Write-Host "  Install Sage manually: https://github.com/lazear/sage/releases" -ForegroundColor Yellow
}
if (-not $sageInstalled) {
    Write-Host "  Skipped (STAN will still work, but DDA searches require Sage)." -ForegroundColor Yellow
}
$ErrorActionPreference = "Stop"

# -- Init --
Write-Host ""
Write-Host "  [6/7] Initializing..." -ForegroundColor Cyan
$ErrorActionPreference = "Continue"
& $stanExe init 2>&1 | Out-Null
$ErrorActionPreference = "Stop"
Write-Host "  Done." -ForegroundColor Green

# -- PATH --
Write-Host ""
Write-Host "  [7/7] Adding to PATH..." -ForegroundColor Cyan
$sp = "$venv\Scripts"
$up = [Environment]::GetEnvironmentVariable("PATH","User")
if ($up -notlike "*$sp*") {
    [Environment]::SetEnvironmentVariable("PATH","$up;$sp","User")
    $env:Path = "$sp;$env:Path"
    Write-Host "  Added to PATH." -ForegroundColor Green
} else {
    Write-Host "  Already in PATH." -ForegroundColor Green
}

# -- Self-update .bat files for next run --
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $scriptDir) { $scriptDir = Get-Location }
try {
    $t = [DateTime]::Now.Ticks
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat?t=$t" -OutFile "$scriptDir\install-stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/update-stan.bat?t=$t" -OutFile "$scriptDir\update-stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
} catch {}

# -- Done --
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "    STAN is installed\!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "    stan setup       - configure your instrument" -ForegroundColor Cyan
Write-Host "    stan watch       - start monitoring" -ForegroundColor Cyan
Write-Host "    stan dashboard   - open QC dashboard" -ForegroundColor Cyan
Write-Host ""

$go = Read-Host "  Run 'stan setup' now? (Y/n)"
if ($go -ne "n") {
    Write-Host ""
    & $stanExe setup
}

Write-Host ""
Write-Host "  Happy QC\!" -ForegroundColor Green
Write-Host ""
