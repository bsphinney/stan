# STAN Installer - downloaded and executed by install.bat

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

# -- Check Python --
Write-Host ""
Write-Host "  [1/5] Checking for Python..." -ForegroundColor Cyan

$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $p = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($p) {
            $ver = & $p.Source --version 2>&1
            if ($ver -match "3\.(1[0-9]|[2-9][0-9])") {
                $python = $p.Source
                Write-Host "  Found $ver" -ForegroundColor Green
                break
            }
        }
    } catch {}
}

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
    Write-Host "  Installing Python (check 'Add to PATH' if prompted)..." -ForegroundColor Yellow
    Start-Process -FilePath $pyInst -ArgumentList "/passive","InstallAllUsers=0","PrependPath=1","Include_test=0" -Wait
    Remove-Item $pyInst -ErrorAction SilentlyContinue
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
    $lp = "$env:LOCALAPPDATA\Programs\Python\Python312"
    if (Test-Path "$lp\python.exe") { $env:Path = "$lp;$lp\Scripts;$env:Path" }
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        Write-Host "  ERROR: Python still not found. Restart and try again." -ForegroundColor Red
        exit 1
    }
    Write-Host "  Python installed." -ForegroundColor Green
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

# Upgrade pip silently
& $pip install --upgrade pip 2>&1 | Out-Null

# Install from GitHub zip (no git required) instead of git+https
# This works on any Windows machine without Git installed.
$stanUrl = "https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
& $pip install $stanUrl 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "Successfully installed") { Write-Host "  $line" -ForegroundColor Green }
    elseif ($line -match "ERROR|error") { Write-Host "  $line" -ForegroundColor Red }
}
$ErrorActionPreference = "Stop"

# Verify stan is installed
$stanExe = "$venv\Scripts\stan.exe"
if (-not (Test-Path $stanExe)) {
    Write-Host "  ERROR: STAN installation failed." -ForegroundColor Red
    Write-Host "  Try: pip install https://github.com/bsphinney/stan/archive/main.zip" -ForegroundColor Yellow
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
