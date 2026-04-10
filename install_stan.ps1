# STAN Installer - downloaded and executed by install-stan.bat

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "    STAN - Standardized proteomic Throughput ANalyzer" -ForegroundColor Cyan
Write-Host "    Know your instrument." -ForegroundColor Cyan
Write-Host "  ============================================================" -ForegroundColor Cyan
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
} catch {}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# -- Find Python --
Write-Host ""
Write-Host "  [1/7] Checking for Python..." -ForegroundColor Cyan

function Find-Python {
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

$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = "$machinePath;$userPath"

$python = Find-Python

if (-not $python) {
    Write-Host "  Python 3.10+ not found. Downloading from python.org..." -ForegroundColor Yellow
    $pyUrl = "https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe"
    $pyInst = "$env:TEMP\python-installer.exe"
    try {
        Invoke-WebRequest -Uri $pyUrl -OutFile $pyInst -UseBasicParsing
    } catch {
        Write-Host "  ERROR: Download failed." -ForegroundColor Red
        Write-Host "  Install Python 3.12 from https://www.python.org/downloads/" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  Installing Python..." -ForegroundColor Yellow
    Start-Process -FilePath $pyInst -ArgumentList "/passive","InstallAllUsers=0","PrependPath=1","Include_test=0" -Wait
    Remove-Item $pyInst -ErrorAction SilentlyContinue

    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
    $python = Find-Python

    if (-not $python) {
        Write-Host "  ERROR: Python still not found after installation." -ForegroundColor Red
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
$newStanDir = "$env:USERPROFILE\STAN"
if (-not (Test-Path $newStanDir)) { New-Item -ItemType Directory -Path $newStanDir -Force | Out-Null }
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    & $python -m venv $venv
}
Write-Host "  Done." -ForegroundColor Green

& "$venv\Scripts\Activate.ps1"

# -- Install STAN --
Write-Host ""
Write-Host "  [3/7] Installing STAN (may take a minute)..." -ForegroundColor Cyan

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

$stanExe = "$venv\Scripts\stan.exe"
if (-not (Test-Path $stanExe)) {
    Write-Host "  ERROR: STAN installation failed." -ForegroundColor Red
    exit 1
}
Write-Host "  STAN installed." -ForegroundColor Green

# -- Install DIA-NN (2.3+ required for community benchmark) --
Write-Host ""
Write-Host "  [4/7] Installing DIA-NN..." -ForegroundColor Cyan
$diannInstalled = $false

$diannSearchPaths = @(
    "C:\DIA-NN", "C:\Program Files\DIA-NN", "$env:LOCALAPPDATA\DIA-NN",
    "C:\DiaNN", "C:\Program Files\DiaNN", "$env:LOCALAPPDATA\DiaNN",
    "$env:PROGRAMFILES\DIA-NN", "$env:PROGRAMFILES(x86)\DIA-NN"
)
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = "$machinePath;$userPath"

$allDiann = @()
$onPath = Get-Command "DiaNN.exe" -ErrorAction SilentlyContinue
if ($onPath) { $allDiann += $onPath.Source }
foreach ($sp in $diannSearchPaths) {
    if (Test-Path $sp) {
        $found = Get-ChildItem -Path $sp -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue
        foreach ($f in $found) { if ($allDiann -notcontains $f.FullName) { $allDiann += $f.FullName } }
    }
}

# Find the best DIA-NN — prefer 2.3+ for community benchmark compatibility
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

$isCompatible = $false
if ($bestDiann -and ($bestMajor -gt 2 -or ($bestMajor -eq 2 -and $bestMinor -ge 3))) {
    $isCompatible = $true
}

if ($isCompatible) {
    Write-Host "  DIA-NN 2.3+ already installed: $bestDiann (v$bestMajor.$bestMinor)" -ForegroundColor Green
    $diannDir = Split-Path $bestDiann -Parent
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -notlike "*$diannDir*") {
        [Environment]::SetEnvironmentVariable("PATH", "$userPath;$diannDir", "User")
        $env:Path = "$diannDir;$env:Path"
    }
    $diannInstalled = $true
} elseif ($bestDiann) {
    Write-Host "  DIA-NN found but outdated: $bestDiann (v$bestMajor.$bestMinor)" -ForegroundColor Yellow
    Write-Host "  Community benchmark requires DIA-NN 2.3+. Upgrading..." -ForegroundColor Yellow
}

if (-not $diannInstalled) {
    $ErrorActionPreference = "Continue"
    try {
        $diannRelease = Invoke-RestMethod "https://api.github.com/repos/vdemichev/DiaNN/releases/latest" -TimeoutSec 15
        $diannAsset = $diannRelease.assets | Where-Object { $_.name -match "\.msi$" -and $_.name -notmatch "linux" } | Select-Object -First 1
        if (-not $diannAsset) {
            $diannAsset = $diannRelease.assets | Where-Object { $_.name -match "\.exe$" -and $_.name -notmatch "linux" } | Select-Object -First 1
        }
        if ($diannAsset) {
            $diannInstaller = "$env:TEMP\$($diannAsset.name)"
            Write-Host "  Downloading $($diannAsset.name)..." -ForegroundColor Gray
            Invoke-WebRequest -Uri $diannAsset.browser_download_url -OutFile $diannInstaller -UseBasicParsing
            Write-Host "  Running installer (silent)..." -ForegroundColor Gray
            if ($diannInstaller -match "\.msi$") {
                $diannProc = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$diannInstaller`"", "/quiet", "/norestart" -Wait -PassThru
                if ($diannProc.ExitCode -ne 0) {
                    Write-Host "  Silent install failed. Trying with admin prompt..." -ForegroundColor Yellow
                    Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$diannInstaller`"", "/passive", "/norestart" -Wait -Verb RunAs
                }
            } else {
                Start-Process -FilePath $diannInstaller -ArgumentList "/S" -Wait
            }
            Remove-Item $diannInstaller -ErrorAction SilentlyContinue

            $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
            $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
            $env:Path = "$machinePath;$userPath"
            foreach ($sp in $diannSearchPaths) {
                if (Test-Path $sp) {
                    $f = Get-ChildItem -Path $sp -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($f) {
                        $diannDir = Split-Path $f.FullName -Parent
                        $up2 = [Environment]::GetEnvironmentVariable("PATH", "User")
                        if ($up2 -notlike "*$diannDir*") {
                            [Environment]::SetEnvironmentVariable("PATH", "$up2;$diannDir", "User")
                        }
                        Write-Host "  DIA-NN installed: $($f.FullName)" -ForegroundColor Green
                        $diannInstalled = $true
                        break
                    }
                }
            }
        } else {
            Write-Host "  No installer found. Install manually: https://github.com/vdemichev/DiaNN/releases" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  Could not install DIA-NN: $_" -ForegroundColor Yellow
    }
    $ErrorActionPreference = "Stop"
}

if (-not $diannInstalled) {
    Write-Host "  Skipped (STAN will still work, but DIA searches require DIA-NN 2.3+)." -ForegroundColor Yellow
}

# -- Install Sage --
Write-Host ""
Write-Host "  [5/7] Installing Sage..." -ForegroundColor Cyan
$sageInstalled = $false
$sageDir = "$env:USERPROFILE\STAN\tools\sage"
$ErrorActionPreference = "Continue"
try {
    $sageRelease = Invoke-RestMethod "https://api.github.com/repos/lazear/sage/releases/latest" -TimeoutSec 15
    $sageAsset = $sageRelease.assets | Where-Object { $_.name -match "windows" -and $_.name -match "\.zip$" } | Select-Object -First 1
    if ($sageAsset) {
        $sageZip = "$env:TEMP\$($sageAsset.name)"
        Write-Host "  Downloading $($sageAsset.name)..." -ForegroundColor Gray
        Invoke-WebRequest -Uri $sageAsset.browser_download_url -OutFile $sageZip -UseBasicParsing

        if (-not (Test-Path $sageDir)) { New-Item -ItemType Directory -Path $sageDir -Force | Out-Null }
        Expand-Archive -Path $sageZip -DestinationPath $sageDir -Force
        Remove-Item $sageZip -ErrorAction SilentlyContinue

        $sageExe = Get-ChildItem -Path $sageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($sageExe) {
            $sageExeDir = Split-Path $sageExe.FullName -Parent
            $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
            if ($userPath -notlike "*$sageExeDir*") {
                [Environment]::SetEnvironmentVariable("PATH", "$userPath;$sageExeDir", "User")
                $env:Path = "$sageExeDir;$env:Path"
            }
            Write-Host "  Sage installed: $($sageExe.FullName)" -ForegroundColor Green
            $sageInstalled = $true
        }
    }
} catch {
    Write-Host "  Could not install Sage: $_" -ForegroundColor Yellow
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
$up = [Environment]::GetEnvironmentVariable("PATH", "User")

# Remove old .stan\venv from PATH if it exists (avoid shadowing new STAN\venv)
$oldScripts = "$env:USERPROFILE\.stan\venv\Scripts"
if ($up -like "*$oldScripts*") {
    $parts = $up -split ";"
    $filtered = @()
    foreach ($p in $parts) { if ($p -ne $oldScripts -and $p -ne "") { $filtered += $p } }
    $up = $filtered -join ";"
    Write-Host "  Removed old .stan\venv from PATH." -ForegroundColor Gray
}

if ($up -notlike "*$sp*") {
    [Environment]::SetEnvironmentVariable("PATH", "$up;$sp", "User")
    $env:Path = "$sp;$env:Path"
    Write-Host "  Added to PATH." -ForegroundColor Green
} else {
    [Environment]::SetEnvironmentVariable("PATH", $up, "User")
    Write-Host "  Already in PATH." -ForegroundColor Green
}

# -- Self-update .bat files for next run --
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $scriptDir) { $scriptDir = Get-Location }
try {
    $t = [DateTime]::Now.Ticks
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat?t=$t" -OutFile "$scriptDir\install-stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/update-stan.bat?t=$t" -OutFile "$scriptDir\update-stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/start_stan.bat?t=$t" -OutFile "$scriptDir\start_stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
} catch {}

# -- Done --
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "    STAN is installed!" -ForegroundColor Green
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
Write-Host "  Happy QC!" -ForegroundColor Green
Write-Host ""
