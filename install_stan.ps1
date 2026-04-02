# STAN — One-Click Windows Installer
# ====================================
# Run this script in PowerShell to install STAN and all dependencies.
#
# Usage:
#   Right-click → "Run with PowerShell"
#   OR: powershell -ExecutionPolicy Bypass -File install_stan.ps1
#
# What this does:
#   1. Checks for Python 3.10+ (offers to install via winget if missing)
#   2. Creates a virtual environment
#   3. Installs STAN and all dependencies
#   4. Runs `stan init` to create config files
#   5. Opens the config directory so you can edit instruments.yml
#
# Requirements:
#   - Windows 10/11
#   - Internet connection (for downloads)
#   - Python 3.10+ (installed automatically if winget is available)
#
# Admin access: NOT required. All tools are installed to your user
# directory (~\STAN\tools\). No system-wide changes are made.
# The only exception: if Python is missing, winget install may prompt
# for elevation. You can pre-install Python to avoid this.

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  STAN Installer — Know Your Instrument" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Check Python ──────────────────────────────────────────────

$pythonCmd = $null
foreach ($cmd in @("python3", "python", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.(\d+)") {
            $minor = [int]$Matches[1]
            if ($minor -ge 10) {
                $pythonCmd = $cmd
                Write-Host "[OK] Found $ver" -ForegroundColor Green
                break
            }
        }
    } catch { }
}

if (-not $pythonCmd) {
    Write-Host "[!] Python 3.10+ not found." -ForegroundColor Yellow
    Write-Host "    Attempting to install via winget..." -ForegroundColor Yellow

    try {
        winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        $pythonCmd = "python"
        Write-Host "[OK] Python installed. You may need to restart PowerShell." -ForegroundColor Green
    } catch {
        Write-Host "[ERROR] Could not install Python automatically." -ForegroundColor Red
        Write-Host "        Please install Python 3.10+ from https://www.python.org/downloads/" -ForegroundColor Red
        Write-Host "        Make sure to check 'Add Python to PATH' during installation." -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# ── Step 2: Choose install location ───────────────────────────────────

$defaultDir = "$env:USERPROFILE\STAN"
Write-Host ""
Write-Host "Install location: $defaultDir"
Write-Host "(Press Enter to accept, or type a different path)"
$installDir = Read-Host "Install directory"
if ([string]::IsNullOrWhiteSpace($installDir)) {
    $installDir = $defaultDir
}

if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
}

Set-Location $installDir
Write-Host "[OK] Install directory: $installDir" -ForegroundColor Green

# ── Step 3: Clone or download STAN ────────────────────────────────────

$stanDir = Join-Path $installDir "stan"

if (Test-Path $stanDir) {
    Write-Host "[OK] STAN directory already exists — updating..." -ForegroundColor Yellow
    Set-Location $stanDir
    try {
        git pull origin main 2>&1 | Out-Null
        Write-Host "[OK] Updated from GitHub" -ForegroundColor Green
    } catch {
        Write-Host "[!] git pull failed — using existing files" -ForegroundColor Yellow
    }
} else {
    Write-Host "Downloading STAN from GitHub..." -ForegroundColor Cyan
    try {
        git clone https://github.com/bsphinney/stan.git $stanDir
        Write-Host "[OK] Cloned from GitHub" -ForegroundColor Green
    } catch {
        Write-Host "[!] git not found — downloading as ZIP..." -ForegroundColor Yellow
        $zipUrl = "https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
        $zipPath = Join-Path $installDir "stan.zip"
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath
        Expand-Archive -Path $zipPath -DestinationPath $installDir -Force
        Rename-Item (Join-Path $installDir "stan-main") $stanDir
        Remove-Item $zipPath
        Write-Host "[OK] Downloaded and extracted" -ForegroundColor Green
    }
    Set-Location $stanDir
}

# ── Step 4: Create virtual environment ────────────────────────────────

$venvDir = Join-Path $stanDir ".venv"

if (-not (Test-Path $venvDir)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    & $pythonCmd -m venv $venvDir
}

# Activate venv
$activateScript = Join-Path $venvDir "Scripts\Activate.ps1"
. $activateScript
Write-Host "[OK] Virtual environment activated" -ForegroundColor Green

# ── Step 5: Install STAN ──────────────────────────────────────────────

Write-Host "Installing STAN and dependencies (this may take a minute)..." -ForegroundColor Cyan
pip install --upgrade pip 2>&1 | Out-Null
pip install -e ".[dev]" 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] pip install failed. Check the output above." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "[OK] STAN installed successfully" -ForegroundColor Green

# ── Step 5b: Download DIA-NN ──────────────────────────────────────────

$toolsDir = Join-Path $installDir "tools"
New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null

# ══════════════════════════════════════════════════════════════════════
# PINNED TOOL VERSIONS — do NOT change without updating community params.
# Changing search engine or converter versions could change results and
# break community benchmark comparability across labs.
# ══════════════════════════════════════════════════════════════════════
$DIANN_VERSION = "2.3.2"
$DIANN_MSI_URL = "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.2-Academia.msi"
$SAGE_VERSION = "0.14.7"
$SAGE_ZIP_URL = "https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-x86_64-pc-windows-msvc.zip"
# ThermoRawFileParser runs on Hive (Linux) only — not downloaded locally
$TRFP_VERSION = "1.4.5"  # pinned for Hive SLURM jobs

$diannDir = Join-Path $toolsDir "diann"
if (-not (Test-Path $diannDir)) {
    Write-Host ""
    Write-Host "Downloading DIA-NN $DIANN_VERSION (pinned for community benchmark)..." -ForegroundColor Cyan

    $diannMsi = Join-Path $toolsDir "DIA-NN-$DIANN_VERSION-Academia.msi"

    try {
        Write-Host "  Downloading from GitHub releases..." -ForegroundColor White
        Invoke-WebRequest -Uri $DIANN_MSI_URL -OutFile $diannMsi -UseBasicParsing

        # Extract MSI contents to user directory — NO admin required.
        # "msiexec /a" performs an "administrative install" which just extracts
        # files to TARGETDIR without registering anything system-wide.
        Write-Host "  Extracting DIA-NN (no admin required)..." -ForegroundColor White
        New-Item -ItemType Directory -Path $diannDir -Force | Out-Null
        Start-Process msiexec.exe -ArgumentList "/a `"$diannMsi`" /qn TARGETDIR=`"$diannDir`"" -Wait -NoNewWindow
        Remove-Item $diannMsi -ErrorAction SilentlyContinue

        $diannExe = Get-ChildItem -Path $diannDir -Recurse -Filter "diann.exe" | Select-Object -First 1
        if ($diannExe) {
            Write-Host "[OK] DIA-NN $DIANN_VERSION installed: $($diannExe.FullName)" -ForegroundColor Green
        } else {
            Write-Host "[!] DIA-NN extracted but diann.exe not found — check $diannDir" -ForegroundColor Yellow
            Write-Host "    You may need to download manually from: $DIANN_MSI_URL" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "[!] DIA-NN download failed. Install manually from:" -ForegroundColor Yellow
        Write-Host "    $DIANN_MSI_URL" -ForegroundColor Yellow
    }
} else {
    Write-Host "[OK] DIA-NN already installed in $diannDir" -ForegroundColor Green
}

# ── Step 5c: Download Sage ────────────────────────────────────────────

$sageDir = Join-Path $toolsDir "sage"
if (-not (Test-Path $sageDir)) {
    Write-Host ""
    Write-Host "Downloading Sage v$SAGE_VERSION (pinned for community benchmark)..." -ForegroundColor Cyan

    $sageZip = Join-Path $toolsDir "sage.zip"

    try {
        Write-Host "  Downloading from GitHub releases..." -ForegroundColor White
        Invoke-WebRequest -Uri $SAGE_ZIP_URL -OutFile $sageZip -UseBasicParsing
        New-Item -ItemType Directory -Path $sageDir -Force | Out-Null
        Expand-Archive -Path $sageZip -DestinationPath $sageDir -Force
        Remove-Item $sageZip

        $sageExe = Get-ChildItem -Path $sageDir -Recurse -Filter "sage.exe" | Select-Object -First 1
        if ($sageExe) {
            Write-Host "[OK] Sage v$SAGE_VERSION installed: $($sageExe.FullName)" -ForegroundColor Green
        } else {
            Write-Host "[!] Sage extracted but sage.exe not found — check $sageDir" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "[!] Sage download failed. Install manually from:" -ForegroundColor Yellow
        Write-Host "    $SAGE_ZIP_URL" -ForegroundColor Yellow
    }
} else {
    Write-Host "[OK] Sage already installed in $sageDir" -ForegroundColor Green
}

# ── Step 5d: ThermoRawFileParser (self-contained Windows build) ───────
# Needed for Thermo DDA: .raw → mzML conversion before Sage search.
# NOT needed for: DIA-NN (reads .raw natively), Bruker .d (Sage reads natively).
#
# We use the self-contained Windows build which bundles the .NET runtime
# and Thermo vendor DLLs — nothing else to install. No .NET, no admin.

$TRFP_WIN_URL = "https://github.com/CompOmics/ThermoRawFileParser/releases/download/v.2.0.0-dev/ThermoRawFileParser-v.2.0.0-dev-win.zip"
$TRFP_WIN_VERSION = "2.0.0-dev"

$trfpDir = Join-Path $toolsDir "ThermoRawFileParser"
if (-not (Test-Path $trfpDir)) {
    Write-Host ""
    Write-Host "Downloading ThermoRawFileParser $TRFP_WIN_VERSION (self-contained, no .NET needed)..." -ForegroundColor Cyan

    $trfpZip = Join-Path $toolsDir "trfp.zip"

    try {
        Write-Host "  Downloading from GitHub releases (~50 MB)..." -ForegroundColor White
        Invoke-WebRequest -Uri $TRFP_WIN_URL -OutFile $trfpZip -UseBasicParsing
        New-Item -ItemType Directory -Path $trfpDir -Force | Out-Null
        Expand-Archive -Path $trfpZip -DestinationPath $trfpDir -Force
        Remove-Item $trfpZip

        $trfpExe = Get-ChildItem -Path $trfpDir -Recurse -Filter "ThermoRawFileParser.exe" | Select-Object -First 1
        if ($trfpExe) {
            Write-Host "[OK] ThermoRawFileParser installed: $($trfpExe.FullName)" -ForegroundColor Green
            Write-Host "     Self-contained — no .NET installation required" -ForegroundColor White
        } else {
            Write-Host "[!] ThermoRawFileParser extracted but .exe not found — check $trfpDir" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "[!] ThermoRawFileParser download failed." -ForegroundColor Yellow
        Write-Host "    Only needed for Thermo DDA. DIA works without it." -ForegroundColor Yellow
        Write-Host "    Manual download: $TRFP_WIN_URL" -ForegroundColor Yellow
    }
} else {
    Write-Host "[OK] ThermoRawFileParser already installed in $trfpDir" -ForegroundColor Green
}

# ── Step 5e: Add tools to PATH (session) ─────────────────────────────

# Find the actual exe directories and add to PATH for this session
$diannExePath = Get-ChildItem -Path $diannDir -Recurse -Filter "diann.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
$sageExePath = Get-ChildItem -Path $sageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
$trfpExePath = Get-ChildItem -Path $trfpDir -Recurse -Filter "ThermoRawFileParser.exe" -ErrorAction SilentlyContinue | Select-Object -First 1

if ($diannExePath) { $env:PATH = "$($diannExePath.DirectoryName);$env:PATH" }
if ($sageExePath) { $env:PATH = "$($sageExePath.DirectoryName);$env:PATH" }
if ($trfpExePath) { $env:PATH = "$($trfpExePath.DirectoryName);$env:PATH" }

# Write a version lock file so STAN can verify correct tools are installed
$versionLock = @{
    diann = $DIANN_VERSION
    sage = $SAGE_VERSION
    thermorawfileparser = $TRFP_WIN_VERSION
    search_params_version = "v1.0.0"
    pinned_date = (Get-Date -Format "yyyy-MM-dd")
    execution_mode = "local"
}
$versionLock | ConvertTo-Json | Out-File (Join-Path $toolsDir "versions.json") -Encoding utf8
Write-Host "[OK] Tool versions pinned in $toolsDir\versions.json" -ForegroundColor Green

# ── Step 6: Initialize config ─────────────────────────────────────────

Write-Host ""
Write-Host "Initializing STAN config..." -ForegroundColor Cyan
stan init

$configDir = "$env:USERPROFILE\.stan"
Write-Host ""
Write-Host "[OK] Config files created in: $configDir" -ForegroundColor Green

# ── Step 7: Create desktop shortcuts ──────────────────────────────────

$desktopPath = [Environment]::GetFolderPath("Desktop")

# Shortcut: STAN Dashboard
$dashShortcut = Join-Path $desktopPath "STAN Dashboard.lnk"
$WshShell = New-Object -ComObject WScript.Shell
$shortcut = $WshShell.CreateShortcut($dashShortcut)
$shortcut.TargetPath = (Join-Path $venvDir "Scripts\stan.exe")
$shortcut.Arguments = "dashboard"
$shortcut.WorkingDirectory = $stanDir
$shortcut.Description = "Start STAN QC Dashboard"
$shortcut.Save()

# Shortcut: STAN Watcher
$watchShortcut = Join-Path $desktopPath "STAN Watcher.lnk"
$shortcut = $WshShell.CreateShortcut($watchShortcut)
$shortcut.TargetPath = (Join-Path $venvDir "Scripts\stan.exe")
$shortcut.Arguments = "watch"
$shortcut.WorkingDirectory = $stanDir
$shortcut.Description = "Start STAN Instrument Watcher"
$shortcut.Save()

Write-Host "[OK] Desktop shortcuts created" -ForegroundColor Green

# ── Done ──────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  STAN installed successfully!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Edit your instrument config:" -ForegroundColor White
Write-Host "     notepad $configDir\instruments.yml" -ForegroundColor Yellow
Write-Host ""
Write-Host "  2. Start the watcher:" -ForegroundColor White
Write-Host "     stan watch" -ForegroundColor Yellow
Write-Host ""
Write-Host "  3. Start the dashboard:" -ForegroundColor White
Write-Host "     stan dashboard" -ForegroundColor Yellow
Write-Host "     Then open http://localhost:8421" -ForegroundColor Yellow
Write-Host ""
Write-Host "  4. (Optional) Set up community benchmark:" -ForegroundColor White
Write-Host "     Edit $configDir\community.yml with your HF token" -ForegroundColor Yellow
Write-Host ""

# Open config directory
Write-Host "Opening config directory..." -ForegroundColor Cyan
explorer.exe $configDir

Read-Host "Press Enter to close"
