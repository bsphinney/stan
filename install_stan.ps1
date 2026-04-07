# STAN Installer v7 - downloaded and executed by install.bat

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "    STAN - Standardized proteomic Throughput ANalyzer" -ForegroundColor Cyan
Write-Host "    Know your instrument." -ForegroundColor Cyan
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "  Installer v7" -ForegroundColor DarkGray
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

# -- Find Python --
Write-Host ""
Write-Host "  [1/5] Checking for Python..." -ForegroundColor Cyan

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
Write-Host "  [2/5] Creating virtual environment..." -ForegroundColor Cyan
$venv = "$env:USERPROFILE\.stan\venv"
if (-not (Test-Path "$env:USERPROFILE\.stan")) { New-Item -ItemType Directory -Path "$env:USERPROFILE\.stan" -Force | Out-Null }
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    & $python -m venv $venv
}
Write-Host "  Done." -ForegroundColor Green

# Activate
& "$venv\Scripts\Activate.ps1"

# -- Install STAN --
Write-Host ""
Write-Host "  [3/5] Installing STAN (may take a minute)..." -ForegroundColor Cyan

$pip = "$venv\Scripts\pip.exe"
$ErrorActionPreference = "Continue"
& $pip install --upgrade pip setuptools wheel 2>&1 | Out-Null
& $pip install "https://github.com/bsphinney/stan/archive/refs/heads/main.zip" 2>&1 | ForEach-Object {
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

# -- Init --
Write-Host ""
Write-Host "  [4/5] Initializing..." -ForegroundColor Cyan
$ErrorActionPreference = "Continue"
& $stanExe init 2>&1 | Out-Null
$ErrorActionPreference = "Stop"
Write-Host "  Done." -ForegroundColor Green

# -- PATH --
Write-Host ""
Write-Host "  [5/5] Adding to PATH..." -ForegroundColor Cyan
$sp = "$venv\Scripts"
$up = [Environment]::GetEnvironmentVariable("PATH","User")
if ($up -notlike "*$sp*") {
    [Environment]::SetEnvironmentVariable("PATH","$up;$sp","User")
    $env:Path = "$sp;$env:Path"
    Write-Host "  Added to PATH." -ForegroundColor Green
} else {
    Write-Host "  Already in PATH." -ForegroundColor Green
}

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
