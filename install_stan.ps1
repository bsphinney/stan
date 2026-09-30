# STAN Installer - downloaded and executed by install-stan.bat
#
# Authoring rules (Windows PowerShell 5.1 is the target, and it reads a
# BOM-less .ps1 as ANSI): ASCII only, rewrite the whole file rather than
# patching lines, no '+' string concatenation, no inline 'if' expressions,
# no Where-Object pipelines, Join-Path for paths. Verified by
# tests/test_windows_installer.ps1 (pwsh -NoProfile -File ...).

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
Write-Host "  =================== DIA-NN License ===================" -ForegroundColor Yellow
Write-Host "  DIA-NN is developed by Vadim Demichev." -ForegroundColor Gray
Write-Host "  Free for academic and non-commercial use." -ForegroundColor Gray
Write-Host "  Commercial use requires a separate license from the author." -ForegroundColor Gray
Write-Host ""
Write-Host "  Full terms: https://github.com/vdemichev/DiaNN/blob/master/LICENSE.md" -ForegroundColor Gray
Write-Host ""
Write-Host "  Citation: Demichev V et al. Nature Methods. 2020;17(1):41-44." -ForegroundColor Gray
Write-Host "  ======================================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Sage (DDA search engine) is MIT open source." -ForegroundColor Gray
Write-Host "  https://github.com/lazear/sage" -ForegroundColor Gray
Write-Host ""
$accept = Read-Host "  Do you accept the DIA-NN license terms? [yes/no]"
if ($accept -ne "yes") { Write-Host "  Cancelled." -ForegroundColor Yellow; exit 0 }
Write-Host "  License accepted." -ForegroundColor Green

# -- SSL workaround for corporate/university proxy networks --
# This turns certificate validation off for the rest of the script. The
# search-engine downloads below are therefore checked against pinned
# sha256 values instead of trusting the transport.
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

# >>> engine pins -- keep this block byte-identical in install_stan.ps1 and update_stan.ps1
#
# tests/test_windows_installer.ps1 fails if the two copies differ. They are
# duplicated rather than shared because each script is downloaded and run
# on its own by its .bat launcher.
#
# DIA-NN. Two rules in stan/search/community_params.py decide what a DIA row
# is worth to the community benchmark, and both key on major.minor:
#   - check_diann_version_compatible(): the relay accepts a row only when its
#     DIA-NN major.minor equals PINNED_TOOL_VERSIONS["diann"] ("2.3.0").
#     2.5, 2.6 and 2.7 rows are rejected outright ("Submission rejected:
#     DIA-NN version mismatch" in stan/community/submit.py).
#   - is_asset_hash_eligible_diann(): submit.py fills in the canonical FASTA
#     and library hashes for any 2.3.x row, so the relay marks it
#     assets_verified=True. Until 2026-09-29 only an exact 2.3.0 qualified;
#     Brett widened it (2.3.2 vs 2.3.0 measured 1.007x on the same raws).
# On Windows 2.3.0 ships only as DIA-NN-2.3.0-Academia-Preview.msi, so this
# pin installs the newest non-preview 2.3.x MSI, DIA-NN-2.3.2-Academia.msi.
# To install the exact pin instead, change the three values below to
#   $DiannPinnedVersion   = "2.3.0"
#   $DiannPinnedMsiName   = "DIA-NN-2.3.0-Academia-Preview.msi"
#   $DiannPinnedMsiSha256 = "e3343856740529a865a8a04013158b95b929e56779a3911e13b8270a898088d6"
# An install that already has 2.3.0 keeps using it either way: see
# Select-CompatibleDiann and $DiannCommunityExactVersion.
# Every 2.x MSI is an asset of the one GitHub release tagged "2.0". Both
# sha256 values are the digests GitHub publishes for those assets, checked
# against the releases API on 2026-09-29. Before this pin the installer took
# the newest MSI in that release -- 2.7.0 by then -- and the relay rejected
# every row it produced.
$DiannPinnedVersion = "2.3.2"
$DiannPinnedMsiName = "DIA-NN-2.3.2-Academia.msi"
$DiannPinnedMsiSha256 = "207233c438ef9f7d9d7afe2e28c8062180305b04f9d5f6bd587f4733eb0a9389"
# PINNED_TOOL_VERSIONS["diann"]: the exact version Hive's container runs.
$DiannCommunityExactVersion = "2.3.0"

# Sage. PINNED_TOOL_VERSIONS["sage"] = "0.14.7". The v0.14.7 release binary
# still prints "sage 0.14.6" for --version, so the version string cannot
# tell the two releases apart and the sha256 is the only proof. The release
# publishes no checksum, so both hashes were computed from the release
# asset sage-v0.14.7-x86_64-pc-windows-msvc.zip on 2026-09-29 (the zip, and
# the sage.exe inside it).
$SagePinnedVersion = "0.14.7"
$SagePinnedZipSha256 = "ffae29b1181b979e89aa82a8ff6ba5f2c7b372d224d8180c08e4b5a25241b8f9"
$SagePinnedExeSha256 = "bdcadd26c540640b79239515166b8934be6039b52061f686d2857ab663fd4b02"

function Get-DiannMsiUrl {
    param([string]$MsiName)
    return "https://github.com/vdemichev/DiaNN/releases/download/2.0/$MsiName"
}

function Get-SageZipName {
    param([string]$Version)
    return "sage-v$Version-x86_64-pc-windows-msvc.zip"
}

function Get-SageZipUrl {
    param([string]$Version)
    $name = Get-SageZipName $Version
    return "https://github.com/lazear/sage/releases/download/v$Version/$name"
}

# Where Install-PinnedSage leaves sage.exe: the zip holds one folder named
# after the release, so each version lands in its own subfolder.
function Get-SageExePath {
    param([string]$ToolsDir, [string]$Version)
    $sub = Join-Path $ToolsDir "sage-v$Version-x86_64-pc-windows-msvc"
    return (Join-Path $sub "sage.exe")
}

# The same rule as check_diann_version_compatible(): major.minor must match.
function Test-DiannCompatible {
    param([string]$Version, [string]$Pinned)
    if (-not $Version) { return $false }
    if (-not $Pinned) { return $false }
    $v = $Version.Trim().Split(".")
    $p = $Pinned.Trim().Split(".")
    if ($v.Count -lt 2) { return $false }
    if ($p.Count -lt 2) { return $false }
    return (($v[0] -eq $p[0]) -and ($v[1] -eq $p[1]))
}

# "2.3.2" from "C:\Program Files\DIA-NN\2.3.2\DiaNN.exe", "" when no folder
# on the way names a version. The LAST version-looking token wins, because
# it is the one nearest the executable.
function Get-DiannVersionFromPath {
    param([string]$Path)
    if (-not $Path) { return "" }
    $dir = $Path -replace '[\\/][^\\/]*$', ''
    $found = [regex]::Matches($dir, '\d+\.\d+(\.\d+)?')
    if ($found.Count -eq 0) { return "" }
    return $found[$found.Count - 1].Value
}

# "2.3.2" from the header DIA-NN prints when run with no arguments, the
# same pattern stan/search/version_detect.py uses.
function Get-DiannVersionFromBanner {
    param([string]$Text)
    if (-not $Text) { return "" }
    if ($Text -match 'DIA-NN\s+(\d+\.\d+(\.\d+)?)') { return $Matches[1] }
    return ""
}

function ConvertTo-EngineVersion {
    param([string]$Version)
    $parsed = $null
    if ([System.Version]::TryParse($Version, [ref]$parsed)) { return $parsed }
    return $null
}

# Run the binary with no arguments and read its header. Only used when the
# install folder does not name the version. Capped at 30 s so a binary that
# waits for input cannot hang the installer.
function Get-DiannBannerVersion {
    param([string]$Exe)
    $tmp = [System.IO.Path]::GetTempPath()
    $outFile = Join-Path $tmp "stan_diann_banner_$PID.out.txt"
    $errFile = Join-Path $tmp "stan_diann_banner_$PID.err.txt"
    $text = ""
    try {
        $proc = Start-Process -FilePath $Exe -NoNewWindow -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        if (-not $proc.WaitForExit(30000)) {
            try { $proc.Kill() } catch {}
        }
        foreach ($f in @($outFile, $errFile)) {
            if (Test-Path -LiteralPath $f) {
                $chunk = Get-Content -LiteralPath $f -Raw
                $text = "$text`n$chunk"
            }
        }
    } catch {
        $text = ""
    }
    foreach ($f in @($outFile, $errFile)) {
        Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
    }
    return (Get-DiannVersionFromBanner $text)
}

# Folders the DIA-NN MSI uses, plus the older hand-made layouts.
function Get-DiannSearchRoots {
    $bases = @("C:\", "C:\Program Files", $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA, $env:USERPROFILE)
    $roots = New-Object System.Collections.Generic.List[string]
    foreach ($b in $bases) {
        if (-not $b) { continue }
        foreach ($n in @("DIA-NN", "DiaNN")) {
            $r = Join-Path $b $n
            if (-not $roots.Contains($r)) { $roots.Add($r) }
        }
    }
    return $roots.ToArray()
}

# Every full path to $ExeName along a PATH string, in the order Windows
# searches it. The first one is what a bare `diann` or `sage` runs.
# Join-Path stays inside the try: an entry on a drive that is not there
# (a disconnected mapped drive, say Q:\tools) makes it throw "Cannot find
# drive", which would otherwise print an error on every call, or stop the
# script where $ErrorActionPreference is Stop.
function Get-ExesOnPath {
    param([string]$PathValue, [string]$ExeName)
    $hits = New-Object System.Collections.Generic.List[string]
    if (-not $PathValue) { return $hits.ToArray() }
    foreach ($entry in $PathValue.Split(";")) {
        $d = $entry.Trim().Trim('"')
        if (-not $d) { continue }
        $candidate = ""
        $isFile = $false
        try {
            $candidate = Join-Path $d $ExeName -ErrorAction Stop
            $isFile = Test-Path -LiteralPath $candidate -PathType Leaf -ErrorAction Stop
        } catch {
            $isFile = $false
        }
        if ($isFile) {
            $dup = $false
            foreach ($h in $hits) { if ($h -ieq $candidate) { $dup = $true } }
            if (-not $dup) { $hits.Add($candidate) }
        }
    }
    return $hits.ToArray()
}

# The binary a bare $ExeName would actually run, when it is not $Wanted.
# "" when $Wanted wins or nothing is found.
function Get-ShadowingExe {
    param([string]$PathValue, [string]$ExeName, [string]$Wanted)
    $hits = @(Get-ExesOnPath $PathValue $ExeName)
    if ($hits.Count -eq 0) { return "" }
    if ($hits[0] -ieq $Wanted) { return "" }
    return $hits[0]
}

# $PathValue with $Dir moved (or added) to the front. Entries compare
# case-insensitively and without a trailing backslash, as Windows resolves
# them, so an existing copy is moved rather than duplicated.
function Get-PathWithDirFirst {
    param([string]$PathValue, [string]$Dir)
    $want = $Dir.TrimEnd("\")
    $kept = New-Object System.Collections.Generic.List[string]
    $kept.Add($want)
    if ($PathValue) {
        foreach ($entry in $PathValue.Split(";")) {
            $e = $entry.Trim()
            if (-not $e) { continue }
            if ($e.TrimEnd("\") -ieq $want) { continue }
            $kept.Add($e)
        }
    }
    return ($kept -join ";")
}

# Every DiaNN.exe on PATH or under the search roots, once each, with the
# version read from its folder name or, failing that, its own header.
function Find-DiannCandidates {
    param($SearchRoots, [string]$PathValue)
    $seen = @{}
    $paths = New-Object System.Collections.Generic.List[string]
    foreach ($p in @(Get-ExesOnPath $PathValue "DiaNN.exe")) {
        if (-not $p) { continue }
        if (-not $seen.ContainsKey($p)) { $seen[$p] = $true; $paths.Add($p) }
    }
    foreach ($root in @($SearchRoots)) {
        if (-not $root) { continue }
        $exists = $false
        try { $exists = Test-Path -LiteralPath $root } catch {}
        if (-not $exists) { continue }
        $found = @(Get-ChildItem -LiteralPath $root -Recurse -Filter "DiaNN.exe" -ErrorAction SilentlyContinue)
        foreach ($f in $found) {
            if (-not $f) { continue }
            $full = $f.FullName
            if (-not $seen.ContainsKey($full)) { $seen[$full] = $true; $paths.Add($full) }
        }
    }
    foreach ($p in $paths) {
        $ver = Get-DiannVersionFromPath $p
        $how = "folder name"
        if (-not $ver) {
            $ver = Get-DiannBannerVersion $p
            $how = "program header"
        }
        if (-not $ver) { $how = "unknown" }
        [pscustomobject]@{ Path = $p; Version = $ver; How = $how }
    }
}

# Of the installs found, the one to use; $null when none has the pinned
# major.minor. An install of exactly $Preferred wins (2.3.0: the exact version
# Hive's container runs), otherwise the newest compatible.
function Select-CompatibleDiann {
    param($Candidates, [string]$Pinned, [string]$Preferred)
    $best = $null
    $bestVer = $null
    foreach ($c in @($Candidates)) {
        if ($null -eq $c) { continue }
        if (-not (Test-DiannCompatible $c.Version $Pinned)) { continue }
        $v = ConvertTo-EngineVersion $c.Version
        if ($null -eq $v) { continue }
        if ($Preferred) {
            if ($c.Version -eq $Preferred) { return $c }
        }
        if (($null -eq $best) -or ($v -gt $bestVer)) {
            $best = $c
            $bestVer = $v
        }
    }
    return $best
}

function Test-FileSha256 {
    param([string]$Path, [string]$Expected)
    if (-not $Path) { return $false }
    if (-not $Expected) { return $false }
    $exists = $false
    try { $exists = Test-Path -LiteralPath $Path -PathType Leaf } catch {}
    if (-not $exists) { return $false }
    $h = ""
    try { $h = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash } catch { return $false }
    return ($h.ToLowerInvariant() -eq $Expected.Trim().ToLowerInvariant())
}

# Where update_stan.ps1 remembers that msiexec could not install an MSI,
# so later updates do not download the same ~250 MB only to fail again.
function Get-DiannMsiMarkerPath {
    param([string]$UserProfile)
    $stanDir = Join-Path $UserProfile "STAN"
    return (Join-Path $stanDir "diann_msi_needs_admin.txt")
}

# True when $Marker records a failed install of exactly $MsiName. A marker
# left by an older pin does not count, so a new pin is always tried once.
function Test-DiannMsiMarker {
    param([string]$Marker, [string]$MsiName)
    if (-not $Marker) { return $false }
    if (-not $MsiName) { return $false }
    try {
        if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) { return $false }
        $first = @(Get-Content -LiteralPath $Marker -TotalCount 1)
        if ($first.Count -eq 0) { return $false }
        return ("$($first[0])".Trim() -ieq $MsiName)
    } catch {
        return $false
    }
}

# Download the pinned MSI, refuse it unless its sha256 matches, install it.
# The MSI puts each version in its own folder, and DIA-NN supports several
# versions side by side (DIA-NN README, FAQ), so an existing install of
# another version is left untouched.
#
# msiexec runs silently first. When that fails -- a per-machine MSI needs
# admin rights, and lab accounts usually lack them -- only a run with
# $AllowElevation asks for an admin prompt. install_stan.ps1 passes it: the
# operator is at the keyboard, having just answered the license question.
# update_stan.ps1 does not: start_stan_loop.bat runs it unattended for the
# remote update_stan action, and a UAC prompt would block the script before
# it relaunches the watcher. Instead it records the failure in
# $NeedsAdminMarker (see Test-DiannMsiMarker). 1618 means another install
# was running, which the next update can simply retry, so it is not
# recorded. 3010 and 1641 are successes that want a reboot.
function Install-PinnedDiann {
    param([string]$MsiName, [string]$Sha256, [bool]$AllowElevation, [string]$NeedsAdminMarker)
    $ProgressPreference = "SilentlyContinue"
    $url = Get-DiannMsiUrl $MsiName
    $msi = Join-Path ([System.IO.Path]::GetTempPath()) $MsiName
    Write-Host "  Downloading $MsiName (about 250 MB)..." -ForegroundColor Gray
    try {
        Invoke-WebRequest -Uri $url -OutFile $msi -UseBasicParsing -ErrorAction Stop
    } catch {
        Write-Host "  Could not download $url" -ForegroundColor Yellow
        Write-Host "  $_" -ForegroundColor Yellow
        return $false
    }
    if (-not (Test-FileSha256 $msi $Sha256)) {
        Write-Host "  REFUSING to install $MsiName - its sha256 does not match the pinned value." -ForegroundColor Red
        Write-Host "  expected sha256 $Sha256" -ForegroundColor Red
        Remove-Item -LiteralPath $msi -Force -ErrorAction SilentlyContinue
        return $false
    }
    Write-Host "  Checksum OK. Running installer (silent)..." -ForegroundColor Gray
    $okCodes = @(0, 1641, 3010)
    try {
        $proc = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$msi`"", "/quiet", "/norestart" -Wait -PassThru
        $code = $null
        if ($proc) { $code = $proc.ExitCode }
        if ($okCodes -notcontains $code) {
            if ($AllowElevation) {
                Write-Host "  Silent install failed (msiexec exit $code). Trying with an admin prompt..." -ForegroundColor Yellow
                Start-Process -FilePath "msiexec.exe" -ArgumentList "/i", "`"$msi`"", "/passive", "/norestart" -Wait -Verb RunAs
            } else {
                Write-Host "  DIA-NN's installer failed (msiexec exit $code). It usually needs admin rights," -ForegroundColor Yellow
                Write-Host "  and this updater never asks for them, so that it can run unattended." -ForegroundColor Yellow
                if (($code -ne 1618) -and $NeedsAdminMarker) {
                    try {
                        $markerDir = Split-Path -Parent $NeedsAdminMarker
                        if (-not (Test-Path -LiteralPath $markerDir)) { New-Item -ItemType Directory -Path $markerDir -Force | Out-Null }
                        $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm")
                        Set-Content -LiteralPath $NeedsAdminMarker -Value @($MsiName, "msiexec exit $code at $stamp") -Encoding ASCII
                    } catch {}
                }
                Remove-Item -LiteralPath $msi -Force -ErrorAction SilentlyContinue
                return $false
            }
        }
    } catch {
        Write-Host "  DIA-NN installer did not run: $_" -ForegroundColor Yellow
        Remove-Item -LiteralPath $msi -Force -ErrorAction SilentlyContinue
        return $false
    }
    Remove-Item -LiteralPath $msi -Force -ErrorAction SilentlyContinue
    return $true
}

# The pinned sage.exe to use, or "" when there is none yet: first whatever
# a bare `sage` resolves to, if it is the pinned release binary, then the
# copy Install-PinnedSage unpacks under $ToolsDir.
function Resolve-PinnedSage {
    param([string]$ToolsDir, [string]$Version, [string]$ExeSha256, [string]$PathValue)
    $onPath = @(Get-ExesOnPath $PathValue "sage.exe")
    if ($onPath.Count -gt 0) {
        if (Test-FileSha256 $onPath[0] $ExeSha256) { return $onPath[0] }
    }
    $ours = Get-SageExePath $ToolsDir $Version
    if (Test-FileSha256 $ours $ExeSha256) { return $ours }
    return ""
}

# Download the pinned zip, refuse it unless its sha256 matches, unpack it
# under $ToolsDir. Returns the sage.exe path, or "" on any failure.
function Install-PinnedSage {
    param([string]$ToolsDir, [string]$Version, [string]$ZipSha256, [string]$ExeSha256)
    $ProgressPreference = "SilentlyContinue"
    $name = Get-SageZipName $Version
    $url = Get-SageZipUrl $Version
    $zip = Join-Path ([System.IO.Path]::GetTempPath()) $name
    Write-Host "  Downloading $name..." -ForegroundColor Gray
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -ErrorAction Stop
    } catch {
        Write-Host "  Could not download $url" -ForegroundColor Yellow
        Write-Host "  $_" -ForegroundColor Yellow
        return ""
    }
    if (-not (Test-FileSha256 $zip $ZipSha256)) {
        Write-Host "  REFUSING to install $name - its sha256 does not match the pinned value." -ForegroundColor Red
        Write-Host "  expected sha256 $ZipSha256" -ForegroundColor Red
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
        return ""
    }
    try {
        if (-not (Test-Path -LiteralPath $ToolsDir)) { New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null }
        Expand-Archive -LiteralPath $zip -DestinationPath $ToolsDir -Force
    } catch {
        Write-Host "  Could not unpack $name : $_" -ForegroundColor Yellow
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
        return ""
    }
    Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
    $exe = Get-SageExePath $ToolsDir $Version
    if (-not (Test-FileSha256 $exe $ExeSha256)) {
        Write-Host "  Unpacked $name but $exe is not the expected binary." -ForegroundColor Yellow
        return ""
    }
    return $exe
}

# What to do to the user PATH so a bare $ExeName runs $ExePath. Windows
# builds a logon PATH as system PATH then user PATH. Returns the new user
# PATH, whether it changed, and the system-PATH binary that would still win
# ("" when none would).
function Get-EnginePathPlan {
    param([string]$MachinePath, [string]$UserPath, [string]$ExePath, [string]$ExeName)
    $newUser = $UserPath
    $current = @(Get-ExesOnPath "$MachinePath;$UserPath" $ExeName)
    $alreadyFirst = $false
    if ($current.Count -gt 0) {
        if ($current[0] -ieq $ExePath) { $alreadyFirst = $true }
    }
    if (-not $alreadyFirst) {
        $dir = Split-Path -Parent $ExePath
        $newUser = Get-PathWithDirFirst $UserPath $dir
    }
    $shadow = Get-ShadowingExe "$MachinePath;$newUser" $ExeName $ExePath
    return [pscustomobject]@{ UserPath = $newUser; Changed = ($newUser -ne $UserPath); Shadow = $shadow }
}

# The config file STAN reads for $Name on Windows, as stan/config.py
# resolve_config_path() finds it: %USERPROFILE%\STAN\<name>, else the legacy
# %USERPROFILE%\.stan\<name>, else where a new one would go (STAN\<name>).
function Get-StanConfigFile {
    param([string]$Name, [string]$UserProfile)
    $primary = Join-Path (Join-Path $UserProfile "STAN") $Name
    $legacy = Join-Path (Join-Path $UserProfile ".stan") $Name
    $isPrimary = $false
    $isLegacy = $false
    try { $isPrimary = Test-Path -LiteralPath $primary -PathType Leaf } catch {}
    if ($isPrimary) { return $primary }
    try { $isLegacy = Test-Path -LiteralPath $legacy -PathType Leaf } catch {}
    if ($isLegacy) { return $legacy }
    return $primary
}

# Drop a UTF-8 byte-order mark from the start of $Path; $true when there was
# one. PowerShell 5.1's Out-File -Encoding utf8 writes one, and Python on
# Windows reads STAN's YAML in the ANSI code page, where the mark becomes
# three stray characters and the file no longer parses. Nothing else in the
# file changes.
function Remove-Utf8Bom {
    param([string]$Path)
    try {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        if ($bytes.Length -lt 3) { return $false }
        if (($bytes[0] -ne 0xEF) -or ($bytes[1] -ne 0xBB) -or ($bytes[2] -ne 0xBF)) { return $false }
        $rest = New-Object byte[] ($bytes.Length - 3)
        [System.Array]::Copy($bytes, 3, $rest, 0, $rest.Length)
        [System.IO.File]::WriteAllBytes($Path, $rest)
        return $true
    } catch {
        return $false
    }
}

# The watcher runs a bare `diann` / `sage` from PATH unless an instrument
# sets diann_path / sage_path in instruments.yml, so pointing the watcher at
# the pinned binary means putting its folder first on the user PATH.
# `stan baseline` does NOT take DIA-NN from PATH; see Get-BaselineDiannPick.
# Windows searches the system PATH before the user PATH, and changing that
# needs admin rights, so when a system-PATH copy would still win we say so
# and name the per-instrument override. $env:Path is left in the order a
# fresh logon gets (system, then user), so anything launched from here sees
# exactly what the next stan.bat will.
function Use-EngineFirstOnPath {
    param([string]$ExePath, [string]$ExeName, [string]$Label, [string]$ConfigKey)
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $plan = Get-EnginePathPlan $machinePath $userPath $ExePath $ExeName
    if ($plan.Changed) {
        [Environment]::SetEnvironmentVariable("Path", $plan.UserPath, "User")
        $dir = Split-Path -Parent $ExePath
        Write-Host "  Put $dir first on your user PATH, so the watcher runs this $Label." -ForegroundColor Gray
    }
    $env:Path = "$machinePath;$($plan.UserPath)"
    $shadow = $plan.Shadow
    if ($shadow) {
        $yamlPath = $ExePath -replace "\\", "/"
        $instrumentsYml = Get-StanConfigFile "instruments.yml" $env:USERPROFILE
        Write-Host "  WARNING: $shadow is on the system PATH, which Windows searches" -ForegroundColor Red
        Write-Host "           before your user PATH, so STAN would run that instead of" -ForegroundColor Red
        Write-Host "           $ExePath." -ForegroundColor Red
        Write-Host "           Removing it from the system PATH needs admin rights. Until then" -ForegroundColor Yellow
        Write-Host "           add this line to each instrument in" -ForegroundColor Yellow
        Write-Host "           $($instrumentsYml):" -ForegroundColor Yellow
        Write-Host "             $ConfigKey`: `"$yamlPath`"" -ForegroundColor Yellow
        return $false
    }
    return $true
}

# The DiaNN.exe `stan baseline` would search with, asked of the installed
# STAN itself, or "" when that cannot be found out. The baseline builder
# picks its binary with stan/baseline.py _find_diann(). Before STAN 1.2.6
# that took the highest version on disk, so 2.7.0 left beside the pinned
# 2.3.2 won; since 1.2.6 it prefers the pinned 2.3.x (2.3.0 first) and
# labels rows with the binary that searched them. Asking the live code,
# rather than copying its rule here, keeps this right whatever version of
# STAN is installed.
function Get-BaselineDiannPick {
    param([string]$Python)
    $ErrorActionPreference = "Continue"
    if (-not $Python) { return "" }
    $exists = $false
    try { $exists = Test-Path -LiteralPath $Python -PathType Leaf } catch {}
    if (-not $exists) { return "" }
    $code = "from stan.baseline import _find_diann; print(_find_diann() or str())"
    $out = @()
    try {
        $out = @(& $Python -c $code 2>$null)
    } catch {
        return ""
    }
    if ($LASTEXITCODE -ne 0) { return "" }
    $last = ""
    foreach ($line in $out) {
        $s = "$line".Trim()
        if ($s) { $last = $s }
    }
    return $last
}

# What to tell the operator when `stan baseline` would search with a
# different DiaNN.exe than the one the watcher now runs. Returns no lines
# when they are the same binary or either is unknown.
function Get-BaselineDiannWarning {
    param([string]$Pick, [string]$Chosen)
    $lines = New-Object System.Collections.Generic.List[string]
    if ((-not $Pick) -or (-not $Chosen)) { return $lines.ToArray() }
    $a = $Pick
    $b = $Chosen
    try { $a = [System.IO.Path]::GetFullPath($Pick) } catch {}
    try { $b = [System.IO.Path]::GetFullPath($Chosen) } catch {}
    if ($a -ieq $b) { return $lines.ToArray() }
    $pickVer = Get-DiannVersionFromPath $Pick
    if (-not $pickVer) { $pickVer = "version unknown" }
    $lines.Add("  WARNING: 'stan baseline' does not take DIA-NN from PATH. It would search with")
    $lines.Add("             $Pick ($pickVer)")
    $lines.Add("           rather than")
    $lines.Add("             $Chosen")
    $lines.Add("           Baseline DIA runs from this PC must not go to the community benchmark,")
    $lines.Add("           which could take them under the wrong DIA-NN version: answer n when")
    $lines.Add("           the baseline builder asks 'Submit results to community benchmark?',")
    $lines.Add("           or uninstall the other DIA-NN (Settings > Apps) if nothing else here")
    $lines.Add("           needs it.")
    return $lines.ToArray()
}
# <<< engine pins

# A new instruments.yml with no instruments in it, written as plain ASCII
# (no byte-order mark: see Remove-Utf8Bom).
function New-InstrumentsSkeleton {
    param([string]$Path)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $lines = @(
        "# STAN instrument configuration",
        "# 'stan setup' adds your instruments here.",
        "instruments: []",
        ""
    )
    [System.IO.File]::WriteAllText($Path, ($lines -join "`r`n"), [System.Text.Encoding]::ASCII)
}

# -- Find Python --
Write-Host ""
Write-Host "  [1/8] Checking for Python..." -ForegroundColor Cyan

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
Write-Host "  [2/8] Creating virtual environment..." -ForegroundColor Cyan
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
Write-Host "  [3/8] Installing STAN (may take a minute)..." -ForegroundColor Cyan

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

# -- DIA-NN, pinned to the 2.3 line the community benchmark accepts --
$diannLine = ($DiannPinnedVersion.Split(".")[0..1]) -join "."
Write-Host ""
Write-Host "  [4/8] DIA-NN (community benchmark needs $diannLine.x)..." -ForegroundColor Cyan
$ErrorActionPreference = "Continue"
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = "$machinePath;$userPath"

$diannSearchPaths = @(Get-DiannSearchRoots)
$diannFound = @(Find-DiannCandidates $diannSearchPaths $env:Path)
foreach ($c in $diannFound) {
    $shownVer = $c.Version
    if (-not $shownVer) { $shownVer = "unknown version" }
    Write-Host "  found $($c.Path) ($shownVer)" -ForegroundColor Gray
}
$diannBest = Select-CompatibleDiann $diannFound $DiannPinnedVersion $DiannCommunityExactVersion

if ($diannBest) {
    Write-Host "  DIA-NN $($diannBest.Version) is compatible: $($diannBest.Path)" -ForegroundColor Green
} else {
    if ($diannFound.Count -gt 0) {
        Write-Host "  None of these is DIA-NN $diannLine.x, and the community benchmark rejects" -ForegroundColor Yellow
        Write-Host "  any other version. Installing DIA-NN $DiannPinnedVersion alongside; the" -ForegroundColor Yellow
        Write-Host "  existing install is left untouched." -ForegroundColor Yellow
    } else {
        Write-Host "  DIA-NN not found. Installing DIA-NN $DiannPinnedVersion..." -ForegroundColor Yellow
    }
    # The operator is at the keyboard (they just answered the license
    # question), so a failed silent install may fall back to an admin prompt.
    $diannOk = Install-PinnedDiann $DiannPinnedMsiName $DiannPinnedMsiSha256 $true ""
    if ($diannOk) {
        $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $env:Path = "$machinePath;$userPath"
        $diannFound = @(Find-DiannCandidates $diannSearchPaths $env:Path)
        $diannBest = Select-CompatibleDiann $diannFound $DiannPinnedVersion $DiannCommunityExactVersion
        if ($diannBest) {
            Write-Host "  DIA-NN installed: $($diannBest.Path)" -ForegroundColor Green
        } else {
            Write-Host "  The installer finished but no DIA-NN $diannLine.x appeared under the usual folders." -ForegroundColor Yellow
        }
    }
}

# Shown again just before 'stan setup', which offers to run the baseline
# builder: by then this has scrolled out of sight.
$baselineWarning = @()
if ($diannBest) {
    $null = Use-EngineFirstOnPath $diannBest.Path "DiaNN.exe" "DIA-NN $($diannBest.Version)" "diann_path"
    $baselineWarning = @(Get-BaselineDiannWarning (Get-BaselineDiannPick $venvPython) $diannBest.Path)
    foreach ($line in $baselineWarning) { Write-Host $line -ForegroundColor Yellow }
} else {
    $manualUrl = Get-DiannMsiUrl $DiannPinnedMsiName
    Write-Host "  Skipped (STAN will still work, but DIA searches need DIA-NN $diannLine.x)." -ForegroundColor Yellow
    Write-Host "  Install it by hand from $manualUrl" -ForegroundColor Yellow
    Write-Host "  and run update-stan.bat afterwards, so STAN uses it." -ForegroundColor Yellow
}
$ErrorActionPreference = "Stop"

# -- Sage, pinned to v0.14.7 --
Write-Host ""
Write-Host "  [5/8] Sage v$SagePinnedVersion..." -ForegroundColor Cyan
$ErrorActionPreference = "Continue"
$sageDir = Join-Path $env:USERPROFILE "STAN\tools\sage"
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = "$machinePath;$userPath"

$sageBinPath = Resolve-PinnedSage $sageDir $SagePinnedVersion $SagePinnedExeSha256 $env:Path
if ($sageBinPath) {
    Write-Host "  Sage v$SagePinnedVersion already installed: $sageBinPath" -ForegroundColor Green
} else {
    $otherSage = @(Get-ExesOnPath $env:Path "sage.exe")
    if ($otherSage.Count -gt 0) {
        Write-Host "  $($otherSage[0]) is not the Sage v$SagePinnedVersion release binary (sha256 differs)." -ForegroundColor Yellow
        Write-Host "  Installing v$SagePinnedVersion alongside; the existing copy is left in place." -ForegroundColor Yellow
    }
    $sageBinPath = Install-PinnedSage $sageDir $SagePinnedVersion $SagePinnedZipSha256 $SagePinnedExeSha256
    if ($sageBinPath) {
        Write-Host "  Sage installed: $sageBinPath" -ForegroundColor Green
        Write-Host "  (it prints 'sage 0.14.6' for --version; that is the v$SagePinnedVersion release binary)" -ForegroundColor Gray
    }
}
if ($sageBinPath) {
    $null = Use-EngineFirstOnPath $sageBinPath "sage.exe" "Sage v$SagePinnedVersion" "sage_path"
} else {
    $manualSage = Get-SageZipUrl $SagePinnedVersion
    Write-Host "  Skipped (STAN will still work, but DDA searches require Sage)." -ForegroundColor Yellow
    Write-Host "  Install it by hand from $manualSage" -ForegroundColor Yellow
}
$ErrorActionPreference = "Stop"

# -- instruments.yml --
# This step used to append diann_binary / sage_binary to instruments.yml.
# Nothing reads those keys: the watcher reads the per-instrument diann_path
# and sage_path, and otherwise runs a bare `diann` / `sage` from PATH, which
# steps 4 and 5 have just pointed at the pinned binaries. It also rewrote the
# file with Out-File -Encoding utf8, which in PowerShell 5.1 adds a
# byte-order mark that Python on Windows cannot parse. Now it only makes sure
# the instruments.yml STAN reads exists, and never rewrites one that does.
Write-Host ""
Write-Host "  [6/8] Checking instruments.yml..." -ForegroundColor Cyan
$instrYml = Get-StanConfigFile "instruments.yml" $env:USERPROFILE
if (Test-Path -LiteralPath $instrYml -PathType Leaf) {
    if (Remove-Utf8Bom $instrYml) {
        Write-Host "  Removed the byte-order mark an older installer wrote to it." -ForegroundColor Gray
    }
    Write-Host "  Using $instrYml (left as it is)." -ForegroundColor Green
} else {
    # A new one goes in the legacy .stan folder, where this installer has
    # always put it: the per-instrument wrappers in scripts\ write
    # .stan\instruments.yml after calling install-stan.bat, and a file in
    # %USERPROFILE%\STAN would shadow theirs (stan/config.py reads STAN\
    # first). `stan setup` carries its contents into STAN\ when it writes.
    $instrYml = Join-Path (Join-Path $env:USERPROFILE ".stan") "instruments.yml"
    New-InstrumentsSkeleton $instrYml
    Write-Host "  Created $instrYml ('stan setup' adds your instruments to it)." -ForegroundColor Green
}

# -- Init --
Write-Host ""
Write-Host "  [7/8] Initializing STAN config..." -ForegroundColor Cyan
# Skip the interactive `stan init` wizard if instruments.yml already
# exists -- per-instrument wrappers (install_stan_lumosrox.bat,
# install_stan_tims10878.bat) write the file themselves and `stan init`
# would just block silently waiting for tty input that isn't piped.
if (Test-Path -LiteralPath $instrYml -PathType Leaf) {
    Write-Host "  instruments.yml already present -- skipping stan init." -ForegroundColor Gray
} else {
    $ErrorActionPreference = "Continue"
    # Don't redirect to Out-Null -- if init prompts, the operator needs
    # to see the prompt to answer it.
    & $stanExe init
    $ErrorActionPreference = "Stop"
}
Write-Host "  Done." -ForegroundColor Green

# -- PATH --
Write-Host ""
Write-Host "  [8/8] Adding to PATH..." -ForegroundColor Cyan
$sp = "$venv\Scripts"
$up = [Environment]::GetEnvironmentVariable("PATH", "User")

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

# -- Self-update .bat files --
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

if ($baselineWarning.Count -gt 0) {
    foreach ($line in $baselineWarning) { Write-Host $line -ForegroundColor Yellow }
    Write-Host ""
}

$go = Read-Host "  Run 'stan setup' now? (Y/n)"
if ($go -ne "n") {
    Write-Host ""
    & $stanExe setup
}

Write-Host ""
Write-Host "  Happy QC!" -ForegroundColor Green
Write-Host ""
