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
$oldVenv = "$env:USERPROFILE\.stan\venv"
$oldVenvPython = "$oldVenv\Scripts\python.exe"
$newStanDir = "$env:USERPROFILE\STAN"

if (-not (Test-Path $venvPython)) {
    if (Test-Path $oldVenvPython) {
        # Auto-migrate: create new STAN dir and copy venv from .stan
        Write-Host "  Migrating from .stan to STAN..." -ForegroundColor Yellow
        if (-not (Test-Path $newStanDir)) { New-Item -ItemType Directory -Path $newStanDir -Force | Out-Null }
        $migrationOk = $true
        try {
            $destVenv = Join-Path $newStanDir "venv"
            Copy-Item -Path $oldVenv -Destination $destVenv -Recurse -Force
            Write-Host "  Copied venv" -ForegroundColor Gray
            # Copy config files
            $oldStanDir = Join-Path $env:USERPROFILE ".stan"
            $configFiles = Get-ChildItem $oldStanDir -File -ErrorAction SilentlyContinue
            foreach ($cf in $configFiles) {
                $destFile = Join-Path $newStanDir $cf.Name
                if (-not (Test-Path $destFile)) {
                    Copy-Item $cf.FullName $destFile
                    Write-Host ("  Copied " + $cf.Name) -ForegroundColor Gray
                }
            }
            # Copy subdirectories except venv
            $subDirs = Get-ChildItem $oldStanDir -Directory -ErrorAction SilentlyContinue
            foreach ($sd in $subDirs) {
                if ($sd.Name -ne "venv") {
                    $destSub = Join-Path $newStanDir $sd.Name
                    if (-not (Test-Path $destSub)) {
                        Copy-Item $sd.FullName $destSub -Recurse -Force
                        Write-Host ("  Copied " + $sd.Name) -ForegroundColor Gray
                    }
                }
            }
            # Update PATH
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
                $userPath = $userPath + ";" + $newScripts
            }
            [Environment]::SetEnvironmentVariable("PATH", $userPath, "User")
            $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
            $env:Path = $machinePath + ";" + $userPath
            Write-Host "  PATH updated." -ForegroundColor Gray
            Write-Host "  Migration complete." -ForegroundColor Green
        } catch {
            Write-Host "  Migration failed, using old location." -ForegroundColor Yellow
            $migrationOk = $false
            $venv = $oldVenv
            $venvPython = $oldVenvPython
        }
    } else {
        Write-Host "  STAN is not installed. Run install-stan.bat first." -ForegroundColor Red
        exit 1
    }
}

# Ensure venvPython points to the right place after potential migration
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
& $venvPython -m pip install --no-cache-dir --force-reinstall @pipTrust "https://github.com/bsphinney/stan/archive/refs/heads/main.zip?t=$t" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "Successfully installed") { Write-Host "  $line" -ForegroundColor Green }
    elseif ($line -match "ERROR|error") { Write-Host "  $line" -ForegroundColor Red }
}

if (-not (Test-Path $stanExe)) {
    Write-Host "  ERROR: STAN update failed." -ForegroundColor Red
    Write-Host "  Close all STAN windows and retry." -ForegroundColor Yellow
    exit 1
}
Write-Host "  STAN updated." -ForegroundColor Green

# If both venvs exist, clean up the old .stan location
$newStanExe = Join-Path $env:USERPROFILE "STAN\venv\Scripts\stan.exe"
if ((Test-Path $newStanExe) -and (Test-Path $oldVenvPython)) {
    Write-Host "  Cleaning up old .stan location..." -ForegroundColor Yellow
    $oldScripts = Join-Path $oldVenv "Scripts"
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -and $userPath -like "*$oldScripts*") {
        $parts = $userPath -split ";"
        $filtered = @()
        foreach ($p in $parts) { if ($p -ne $oldScripts -and $p -ne "") { $filtered += $p } }
        $cleanPath = $filtered -join ";"
        [Environment]::SetEnvironmentVariable("PATH", $cleanPath, "User")
        $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $env:Path = $machinePath + ";" + $cleanPath
        Write-Host "  Removed old .stan\venv from PATH." -ForegroundColor Gray
    }
    # Also update the old venv so any stale shortcuts still work
    $ticks = [DateTime]::Now.Ticks
    $zipUrl = "https://github.com/bsphinney/stan/archive/refs/heads/main.zip?t=$ticks"
    & $oldVenvPython -m pip install --no-cache-dir --force-reinstall --quiet @pipTrust $zipUrl 2>&1 | Out-Null
    Write-Host "  Old .stan venv also updated." -ForegroundColor Gray
}

# -- Check DIA-NN --
Write-Host ""
Write-Host "  [2/3] Checking DIA-NN..." -ForegroundColor Cyan
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPathRefresh = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = $machinePath + ";" + $userPathRefresh

$diannSearchPaths = @(
    "C:\DIA-NN",
    "C:\Program Files\DIA-NN",
    "C:\DiaNN",
    "C:\Program Files\DiaNN"
)

# Find all DIA-NN installs
$allDiann = @()
foreach ($sp in $diannSearchPaths) {
    if (Test-Path $sp) {
        $exes = Get-ChildItem -Path $sp -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue
        foreach ($e in $exes) { $allDiann += $e.FullName }
    }
}
# Also check PATH
$onPath = Get-Command "DiaNN.exe" -ErrorAction SilentlyContinue
if ($onPath) { $allDiann += $onPath.Source }

# Pick newest version
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
if ($bestDiann -and $bestMajor -ge 2) {
    Write-Host "  DIA-NN found: $bestDiann (v$bestMajor.$bestMinor)" -ForegroundColor Green
    $diannDir = Split-Path $bestDiann -Parent
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -notlike "*$diannDir*") {
        $np = $userPath + ";" + $diannDir
        [Environment]::SetEnvironmentVariable("PATH", $np, "User")
        $env:Path = $diannDir + ";" + $env:Path
        Write-Host "  Added $diannDir to PATH." -ForegroundColor Gray
    }
} elseif ($bestDiann) {
    Write-Host "  DIA-NN found but outdated: $bestDiann (v$bestMajor.$bestMinor)" -ForegroundColor Yellow
    Write-Host "  Version 2.0+ required. Upgrading..." -ForegroundColor Yellow
    $needsDiannInstall = $true
} else {
    Write-Host "  DIA-NN not found. Installing..." -ForegroundColor Yellow
    $needsDiannInstall = $true
}

if ($needsDiannInstall) {
    $ErrorActionPreference = "Continue"
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/vdemichev/DiaNN/releases/latest" -TimeoutSec 15
        $asset = $rel.assets | Where-Object { $_.name -match "\.msi$" -and $_.name -notmatch "linux" } | Select-Object -First 1
        if (-not $asset) { $asset = $rel.assets | Where-Object { $_.name -match "\.exe$" -and $_.name -notmatch "linux" } | Select-Object -First 1 }
        if ($asset) {
            $installer = "$env:TEMP\$($asset.name)"
            Write-Host "  Downloading $($asset.name)..." -ForegroundColor Gray
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $installer -UseBasicParsing
            Write-Host "  Running installer (silent)..." -ForegroundColor Gray
            if ($installer -match "\.msi$") {
                $proc = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$installer`"", "/quiet", "/norestart" -Wait -PassThru
                if ($proc.ExitCode -ne 0) {
                    Write-Host "  Silent failed. Trying with admin..." -ForegroundColor Yellow
                    Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$installer`"", "/passive", "/norestart" -Wait -Verb RunAs
                }
            } else {
                Start-Process -FilePath $installer -ArgumentList "/S" -Wait
            }
            Remove-Item $installer -ErrorAction SilentlyContinue
            # Find installed exe
            $mp = [Environment]::GetEnvironmentVariable("Path", "Machine"); $up = [Environment]::GetEnvironmentVariable("Path", "User"); $env:Path = "$mp;$up"
            foreach ($sp in $diannSearchPaths) {
                if (Test-Path $sp) {
                    $f = Get-ChildItem -Path $sp -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($f) {
                        $d = Split-Path $f.FullName -Parent
                        $up = [Environment]::GetEnvironmentVariable("PATH", "User")
                        if ($up -notlike "*$d*") {
                            $np = $up + ";" + $d
                            [Environment]::SetEnvironmentVariable("PATH", $np, "User")
                        }
                        Write-Host "  DIA-NN installed: $($f.FullName)" -ForegroundColor Green
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

# -- Check Sage --
Write-Host ""
Write-Host "  [3/3] Checking Sage..." -ForegroundColor Cyan
$mp = [Environment]::GetEnvironmentVariable("Path", "Machine"); $up = [Environment]::GetEnvironmentVariable("Path", "User"); $env:Path = "$mp;$up"
$sageExe = Get-Command "sage.exe" -ErrorAction SilentlyContinue
$sageDir = "$env:USERPROFILE\STAN\tools\sage"

if (-not $sageExe) {
    if (Test-Path $sageDir) {
        $f = Get-ChildItem -Path $sageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { $sageExe = $f }
    }
    # Also check old location
    $oldSageDir = "$env:USERPROFILE\.stan\tools\sage"
    if (-not $sageExe -and (Test-Path $oldSageDir)) {
        $f = Get-ChildItem -Path $oldSageDir -Recurse -Filter "sage.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { $sageExe = $f }
    }
}

if ($sageExe) {
    $sp = if ($sageExe.Source) { $sageExe.Source } else { $sageExe.FullName }
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
                    $np = $up + ";" + $d
                    [Environment]::SetEnvironmentVariable("PATH", $np, "User")
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

# -- Self-update bat file --
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $scriptDir) { $scriptDir = Get-Location }
try {
    $t = [DateTime]::Now.Ticks
    $url = "https://raw.githubusercontent.com/bsphinney/stan/main/update-stan.bat"
    Invoke-WebRequest -Uri ($url + "?t=" + $t) -OutFile "$scriptDir\update-stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
    Invoke-WebRequest -Uri ("https://raw.githubusercontent.com/bsphinney/stan/main/start_stan.bat?t=" + $t) -OutFile "$scriptDir\start_stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
} catch {}

# -- Done --
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "    STAN is up to date!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Starting dashboard..." -ForegroundColor Cyan
& $stanExe dashboard
