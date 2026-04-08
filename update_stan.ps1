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

$venv = "$env:USERPROFILE\.stan\venv"
$venvPython = "$venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "  STAN is not installed. Run install.bat first." -ForegroundColor Red
    exit 1
}

# -- Update STAN --
Write-Host "  [1/3] Updating STAN..." -ForegroundColor Cyan
$pipTrust = @("--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org", "--trusted-host", "github.com", "--trusted-host", "objects.githubusercontent.com")
& $venvPython -m pip install --no-cache-dir --force-reinstall @pipTrust "https://github.com/bsphinney/stan/archive/refs/heads/main.zip" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "Successfully installed") { Write-Host "  $line" -ForegroundColor Green }
    elseif ($line -match "ERROR|error") { Write-Host "  $line" -ForegroundColor Red }
}

$stanExe = "$venv\Scripts\stan.exe"
if (-not (Test-Path $stanExe)) {
    Write-Host "  ERROR: STAN update failed." -ForegroundColor Red
    exit 1
}
Write-Host "  STAN updated." -ForegroundColor Green

# -- Check DIA-NN --
Write-Host ""
Write-Host "  [2/3] Checking DIA-NN..." -ForegroundColor Cyan
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
$diannExe = Get-Command "DiaNN.exe" -ErrorAction SilentlyContinue
if (-not $diannExe) { $diannExe = Get-Command "diann.exe" -ErrorAction SilentlyContinue }

# Also check common install locations
if (-not $diannExe) {
    $diannSearchPaths = @(
        "C:\DIA-NN", "C:\Program Files\DIA-NN", "$env:LOCALAPPDATA\DIA-NN",
        "C:\DiaNN", "C:\Program Files\DiaNN", "$env:LOCALAPPDATA\DiaNN",
        "$env:PROGRAMFILES\DIA-NN", "$env:PROGRAMFILES(x86)\DIA-NN"
    )
    foreach ($searchPath in $diannSearchPaths) {
        if (Test-Path $searchPath) {
            $found = Get-ChildItem -Path $searchPath -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $found) { $found = Get-ChildItem -Path $searchPath -Recurse -Filter "diann.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 }
            if ($found) { $diannExe = $found; break }
        }
    }
}

if ($diannExe) {
    Write-Host "  DIA-NN found: $($diannExe.Source ?? $diannExe.FullName)" -ForegroundColor Green
} else {
    Write-Host "  DIA-NN not found. Installing..." -ForegroundColor Yellow
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
                Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$diannInstaller`"", "/quiet", "/norestart" -Wait
            } else {
                $proc = Start-Process -FilePath $diannInstaller -ArgumentList "/S" -Wait -PassThru
                if ($proc.ExitCode -ne 0) { Start-Process -FilePath $diannInstaller -ArgumentList "/VERYSILENT" -Wait }
            }
            Remove-Item $diannInstaller -ErrorAction SilentlyContinue

            # Find and add to PATH
            $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
            foreach ($searchPath in $diannSearchPaths) {
                if (Test-Path $searchPath) {
                    $found = Get-ChildItem -Path $searchPath -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($found) {
                        $diannDir = Split-Path $found.FullName -Parent
                        $userPath = [Environment]::GetEnvironmentVariable("PATH","User")
                        if ($userPath -notlike "*$diannDir*") {
                            [Environment]::SetEnvironmentVariable("PATH","$userPath;$diannDir","User")
                        }
                        Write-Host "  DIA-NN installed at $($found.FullName)" -ForegroundColor Green
                        break
                    }
                }
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
$sageDir = "$env:USERPROFILE\.stan\tools\sage"

if (-not $sageExe) {
    # Check tools directory
    $found = Get-ChildItem -Path $sageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $sageExe = $found }
}

if ($sageExe) {
    Write-Host "  Sage found: $($sageExe.Source ?? $sageExe.FullName)" -ForegroundColor Green
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
                    [Environment]::SetEnvironmentVariable("PATH","$userPath;$sageExeDir","User")
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

# -- Done --
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "    STAN is up to date!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Starting dashboard..." -ForegroundColor Cyan
& $stanExe dashboard
