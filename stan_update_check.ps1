# stan_update_check.ps1
#
# Called from stan.bat at every launch. Fetches the latest version
# string + recent commit messages from GitHub, compares to the
# locally-installed version, and prompts the operator Y/N before
# installing. 30-second timeout defaults to N so an unattended PC
# never blocks startup.
#
# Exit codes (informational only — stan.bat proceeds regardless):
#   0 = update applied / declined / nothing to do
#   1 = network error, couldn't reach GitHub
#
# Stays a separate file (not inlined into stan.bat) for the same
# reason update-stan.bat is separate: PowerShell quoting inside
# cmd.exe is a nightmare and one parsing trap takes the launcher
# down.

# SSL workaround for university / corporate networks (mirrors the
# pattern in install_stan.ps1 / update_stan.ps1).
try {
    Add-Type @"
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public class TrustAllCheck {
    public static void Enable() {
        ServicePointManager.ServerCertificateValidationCallback =
            delegate { return true; };
    }
}
"@
    [TrustAllCheck]::Enable()
} catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- Resolve installed version ---------------------------------------
$venvPip = Join-Path $env:USERPROFILE "STAN\venv\Scripts\pip.exe"
$venvPython = Join-Path $env:USERPROFILE "STAN\venv\Scripts\python.exe"
if (-not (Test-Path $venvPip)) {
    $venvPip = Join-Path $env:USERPROFILE ".stan\venv\Scripts\pip.exe"
    $venvPython = Join-Path $env:USERPROFILE ".stan\venv\Scripts\python.exe"
}
if (-not (Test-Path $venvPython)) {
    Write-Host "  [update-check] No venv found — skipping update check." -ForegroundColor Yellow
    exit 0
}

$localVersion = (& $venvPython -c "import stan; print(stan.__version__)" 2>$null).Trim()
if (-not $localVersion) {
    Write-Host "  [update-check] Couldn't read installed version — skipping." -ForegroundColor Yellow
    exit 0
}

# ---- Fetch remote version --------------------------------------------
$remoteUrl = "https://raw.githubusercontent.com/bsphinney/stan/main/stan/__init__.py?t=$([DateTime]::Now.Ticks)"
try {
    $initText = (Invoke-WebRequest -Uri $remoteUrl -UseBasicParsing -TimeoutSec 15).Content
} catch {
    Write-Host "  [update-check] Couldn't reach GitHub — running with v$localVersion." -ForegroundColor Yellow
    exit 1
}

$remoteVersion = $null
foreach ($line in ($initText -split "`n")) {
    if ($line -match '__version__\s*=\s*"([^"]+)"') {
        $remoteVersion = $Matches[1]
        break
    }
}
if (-not $remoteVersion) {
    Write-Host "  [update-check] Couldn't parse remote version — running with v$localVersion." -ForegroundColor Yellow
    exit 0
}

if ($remoteVersion -eq $localVersion) {
    Write-Host "  [update-check] Up to date (v$localVersion)." -ForegroundColor Green
    exit 0
}

# ---- Show changelog + prompt -----------------------------------------
Write-Host ""
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host "  STAN update available" -ForegroundColor Cyan
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host "    Installed: v$localVersion"
Write-Host "    Available: v$remoteVersion"
Write-Host ""
Write-Host "  What's changed:" -ForegroundColor Cyan

# Most recent 8 commit subject lines from main. Walks back until we
# hit the locally-installed version's tag in the message, OR runs out
# of room — whichever comes first.
try {
    $commitsUrl = "https://api.github.com/repos/bsphinney/stan/commits?per_page=20"
    $commits = Invoke-RestMethod -Uri $commitsUrl -UseBasicParsing -TimeoutSec 15 -Headers @{ "User-Agent" = "stan-bat-update-check" }
    $shown = 0
    foreach ($c in $commits) {
        $subject = ($c.commit.message -split "`n")[0]
        # Stop once we reach the locally-installed version. The
        # version-bump commits embed (v0.2.NNN) in the subject.
        if ($subject -match "\(v$localVersion\)") { break }
        Write-Host "    - $subject"
        $shown++
        if ($shown -ge 10) { break }
    }
    if ($shown -eq 0) {
        Write-Host "    (no commit summary available)"
    }
} catch {
    Write-Host "    (couldn't fetch commit list — proceeding with prompt)"
}

Write-Host ""
Write-Host "  Update now? [Y/n] (auto-skip in 30s)" -ForegroundColor Cyan -NoNewline
Write-Host " " -NoNewline

# Read with timeout so unattended PCs don't block forever. Default = N.
$reply = $null
$start = [DateTime]::Now
while (([DateTime]::Now - $start).TotalSeconds -lt 30) {
    if ([Console]::KeyAvailable) {
        $key = [Console]::ReadKey($true)
        $reply = $key.KeyChar.ToString().ToLower()
        Write-Host $reply
        break
    }
    Start-Sleep -Milliseconds 200
}
Write-Host ""

if ($reply -ne 'y') {
    Write-Host "  Skipping update — running with v$localVersion." -ForegroundColor Yellow
    Write-Host "  (Re-launch stan.bat any time to update.)" -ForegroundColor Gray
    Write-Host ""
    exit 0
}

# ---- Apply update ----------------------------------------------------
Write-Host "  Installing v$remoteVersion ..." -ForegroundColor Cyan
$pipArgs = @(
    "install",
    "--upgrade",
    "--no-input",
    "--quiet",
    "stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
)
& $venvPip @pipArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARN: pip install exited $LASTEXITCODE — continuing with v$localVersion." -ForegroundColor Yellow
    exit 0
}

$newVersion = (& $venvPython -c "import stan; print(stan.__version__)" 2>$null).Trim()
Write-Host "  Updated to v$newVersion." -ForegroundColor Green
Write-Host ""
exit 0
