# STAN Updater - reinstalls STAN and checks for missing/outdated search engines
#
# Authoring rules (Windows PowerShell 5.1 is the target, and it reads a
# BOM-less .ps1 as ANSI): ASCII only, rewrite the whole file rather than
# patching lines, no '+' string concatenation, no inline 'if' expressions,
# no Where-Object pipelines, Join-Path for paths. Verified by
# tests/test_windows_installer.ps1 (pwsh -NoProfile -File ...).

# SSL workaround for corporate/university proxy networks. This turns
# certificate validation off for the rest of the script; the search-engine
# downloads below are therefore checked against pinned sha256 values.
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

# v0.2.148 - defuse the updater-cascade bug.
#
# start_stan_loop.bat checks %USERPROFILE%\STAN\update_pending.flag at
# startup and if present calls update-stan.bat. The previous version of
# this PS1 launched a new start_stan_loop.bat at the end of EVERY run,
# and the flag wasn't deleted until the OUTER loop finished - so the
# just-spawned inner start_stan_loop saw the flag, ran update-stan.bat
# AGAIN, and each recursion doubled every window (watcher, dashboard,
# backfill). Brett 2026-04-21 saw 6 parallel backfill windows after
# one manual click.
#
# Fix: delete the flag at the VERY TOP of this script so any
# start_stan_loop that starts during or after this run sees a clean
# slate and skips its update branch.
try {
    $_flagPath = Join-Path $env:USERPROFILE "STAN\update_pending.flag"
    if (Test-Path $_flagPath) {
        Remove-Item -Force -ErrorAction SilentlyContinue $_flagPath
        Write-Host "  Cleared stale update_pending.flag (cascade defused)." -ForegroundColor Gray
    }
} catch {}

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

# Use new STAN directory, fall back to old .stan
$venv = "$env:USERPROFILE\STAN\venv"
$venvPython = "$venv\Scripts\python.exe"
$oldVenv = "$env:USERPROFILE\.stan\venv"
$oldVenvPython = "$oldVenv\Scripts\python.exe"
$newStanDir = "$env:USERPROFILE\STAN"

if (-not (Test-Path $venvPython)) {
    if (Test-Path $oldVenvPython) {
        Write-Host "  Migrating from .stan to STAN..." -ForegroundColor Yellow
        if (-not (Test-Path $newStanDir)) { New-Item -ItemType Directory -Path $newStanDir -Force | Out-Null }
        try {
            $destVenv = Join-Path $newStanDir "venv"
            Copy-Item -Path $oldVenv -Destination $destVenv -Recurse -Force
            Write-Host "  Copied venv" -ForegroundColor Gray
            $oldStanDir = Join-Path $env:USERPROFILE ".stan"
            $configFiles = Get-ChildItem $oldStanDir -File -ErrorAction SilentlyContinue
            foreach ($cf in $configFiles) {
                $destFile = Join-Path $newStanDir $cf.Name
                if (-not (Test-Path $destFile)) {
                    Copy-Item $cf.FullName $destFile
                    Write-Host "  Copied $($cf.Name)" -ForegroundColor Gray
                }
            }
            $subDirs = Get-ChildItem $oldStanDir -Directory -ErrorAction SilentlyContinue
            foreach ($sd in $subDirs) {
                if ($sd.Name -ne "venv") {
                    $destSub = Join-Path $newStanDir $sd.Name
                    if (-not (Test-Path $destSub)) {
                        Copy-Item $sd.FullName $destSub -Recurse -Force
                        Write-Host "  Copied $($sd.Name)" -ForegroundColor Gray
                    }
                }
            }
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
                $userPath = "$userPath;$newScripts"
            }
            [Environment]::SetEnvironmentVariable("PATH", $userPath, "User")
            $env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));$userPath"
            Write-Host "  Migration complete." -ForegroundColor Green
        } catch {
            Write-Host "  Migration failed, using old location." -ForegroundColor Yellow
            $venv = $oldVenv
            $venvPython = $oldVenvPython
        }
    } else {
        Write-Host "  STAN is not installed. Run install-stan.bat first." -ForegroundColor Red
        exit 1
    }
}

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

# Kill any running stan.exe (watcher, dashboard, etc) so pip can
# overwrite the executable. Without this, pip hits WinError 32 and
# leaves the venv half-installed -> ModuleNotFoundError on next launch.
# This is the root cause of the 16:22 and 16:28 update failures today.
Write-Host "  Stopping running stan.exe processes..." -ForegroundColor Gray
Get-Process stan -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

# Nuke any half-broken stan install artifacts. Past failed updates
# leave a stan-0.2.XX.dist-info\ dir WITHOUT a RECORD manifest -- pip
# then can't determine what to uninstall under --force-reinstall,
# reports 'error: uninstall-no-record-file', and exits 1. Removing
# the package dir + dist-info before install skips the uninstall
# step entirely. Brett's Exploris 2026-04-14 regression.
$sitePackages = Join-Path $venv "Lib\site-packages"
if (Test-Path $sitePackages) {
    Write-Host "  Clearing stale stan package artifacts..." -ForegroundColor Gray
    $stanPkg = Join-Path $sitePackages "stan"
    if (Test-Path $stanPkg) { Remove-Item -Recurse -Force $stanPkg -ErrorAction SilentlyContinue }
    foreach ($distInfo in @(Get-ChildItem -LiteralPath $sitePackages -Directory -ErrorAction SilentlyContinue)) {
        if (-not $distInfo) { continue }
        if ($distInfo.Name -like "stan-*.dist-info" -or $distInfo.Name -like "stan_proteomics-*.dist-info") {
            Remove-Item -LiteralPath $distInfo.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# Real failures we should actually block on. Deliberately narrow -- pip
# prints plenty of noise that looks scary but isn't. "uninstall-no-
# record-file" in particular is just "I can't remove the prior install
# because its RECORD manifest is gone; moving on" and always resolves
# fine under --force-reinstall. Trust pip's exit code as the final
# word; match only the dramatic, unambiguous-failure strings here.
$pipFatalError = $false
& $venvPython -m pip install --no-cache-dir --force-reinstall @pipTrust "https://github.com/bsphinney/stan/archive/refs/heads/main.zip?t=$t" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "Successfully installed") {
        Write-Host "  $line" -ForegroundColor Green
    } elseif ($line -match "WinError 32|Could not install packages|No matching distribution|HTTP error") {
        Write-Host "  $line" -ForegroundColor Red
        $script:pipFatalError = $true
    } elseif ($line -match "^error|^ERROR") {
        # Warnings like 'error: uninstall-no-record-file' -- yellow, not red.
        Write-Host "  $line" -ForegroundColor DarkYellow
    }
}

if ($pipFatalError -or $LASTEXITCODE -ne 0) {
    Write-Host "  ERROR: pip reported fatal errors (exit=$LASTEXITCODE). The venv may be in a partial state." -ForegroundColor Red
    Write-Host "         Close every cmd window running stan.exe, then re-run this script." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $stanExe)) {
    Write-Host "  ERROR: STAN update failed." -ForegroundColor Red
    exit 1
}

# Confirm the new install actually imports -- catches the broken-venv
# case where files land but the package is incomplete.
& $venvPython -c "import stan; print('  STAN v' + stan.__version__)" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "ModuleNotFoundError|Error") {
        Write-Host "  ERROR: installed package does not import:" -ForegroundColor Red
        Write-Host "  $line" -ForegroundColor Red
        exit 1
    } else {
        Write-Host $line -ForegroundColor Green
    }
}
Write-Host "  STAN updated." -ForegroundColor Green

# Install fisher_py for fast Thermo .raw TIC extraction + Sample Health
# monitor. Depends on pythonnet + .NET -- optional, STAN falls back to
# ThermoRawFileParser if this fails. Don't block the update on failure.
Write-Host "  Installing fisher_py (Thermo fast-path)..." -ForegroundColor Gray
& $venvPython -m pip install --quiet @pipTrust fisher_py 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match "Successfully installed") {
        Write-Host "  $line" -ForegroundColor Green
    } elseif ($line -match "error|ERROR") {
        Write-Host "  fisher_py: $line" -ForegroundColor DarkYellow
    }
}
$fisherOk = & $venvPython -c "import fisher_py; print('ok')" 2>&1
if ($fisherOk -match "ok") {
    Write-Host "  fisher_py available." -ForegroundColor Green
} else {
    Write-Host "  fisher_py not available (Thermo TIC falls back to TRFP - slower but works)." -ForegroundColor Yellow
}

# Install alphatims for Bruker MS1 spectrum access (PEG contamination
# detection, `stan backfill-peg`). First install downloads ~150 MB of
# deps (numpy/pandas are usually already present; alphatims itself is
# small, h5py/pyzstd/tqdm are the new ones). Same don't-block-on-failure
# pattern as fisher_py -- PEG detection is optional; STAN proper works
# without it.
#
# ONLY installed when instruments.yml declares at least one Bruker
# instrument. Orbitrap-only hosts (Exploris / Astral / Lumos) don't
# need the Bruker reader and skipping it saves ~150 MB + ~60 sec on
# every update.
#
# The instruments.yml read here is the one STAN reads (Get-StanConfigFile):
# a PC set up by install_stan.ps1 may have only the legacy
# .stan\instruments.yml, which a hard-coded STAN\instruments.yml missed.
# Older installers also wrote that file with a byte-order mark Python on
# Windows cannot parse; it is dropped here, the file otherwise untouched.
$hasBruker = $false
$instYml = Get-StanConfigFile "instruments.yml" $env:USERPROFILE
if (Remove-Utf8Bom $instYml) {
    Write-Host "  Removed the byte-order mark an older installer wrote to $instYml." -ForegroundColor Gray
}
if (Test-Path -LiteralPath $instYml -PathType Leaf) {
    $instContent = Get-Content $instYml -Raw
    if ($instContent -match "(?im)^\s*vendor\s*:\s*['""]?bruker['""]?\s*$") {
        $hasBruker = $true
    }
}
if ($hasBruker) {
    # v0.2.160: only reinstall alphatims when the installed version is
    # missing or >=1.0.9 (the broken one). Otherwise verify the pin is
    # satisfied and move on. Previous v0.2.157-159 ran --force-reinstall
    # on every click - Brett 2026-04-22 called it out as wasteful.
    $alphaVer = & $venvPython -c "import alphatims; print(alphatims.__version__)" 2>&1
    $needsAlpha = $false
    if ($alphaVer -match "^1\.0\.9") {
        Write-Host "  alphatims 1.0.9 detected - forcing downgrade to <1.0.9 (polars compat fix)..." -ForegroundColor Yellow
        $needsAlpha = $true
    } elseif ($alphaVer -match "^1\.0\.([5-8])") {
        Write-Host "  alphatims $alphaVer already satisfies pin (<1.0.9)." -ForegroundColor Green
    } else {
        Write-Host "  alphatims not importable - installing..." -ForegroundColor Gray
        $needsAlpha = $true
    }
    # alphatims 1.0.8 depends on pandas (not polars - polars dep
    # started in 1.0.9). Don't use --no-deps.
    if ($needsAlpha) {
        & $venvPython -m pip install --quiet --force-reinstall @pipTrust "alphatims>=1.0,<1.0.9" 2>&1 | ForEach-Object {
            $line = $_.ToString()
            if ($line -match "Successfully installed") {
                Write-Host "  $line" -ForegroundColor Green
            } elseif ($line -match "error|ERROR") {
                Write-Host "  alphatims: $line" -ForegroundColor DarkYellow
            }
        }
        $alphatimsOk = & $venvPython -c "import alphatims; print('ok')" 2>&1
        if ($alphatimsOk -match "ok") {
            Write-Host "  alphatims available." -ForegroundColor Green
        } else {
            Write-Host "  alphatims not available (PEG/drift disabled). Rerun update to retry." -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "  Skipping alphatims (no Bruker instrument in instruments.yml)." -ForegroundColor Gray
}

# If both venvs exist, retire the old .stan location.
# Pre-v0.2.137 also re-installed STAN into the old .stan venv on
# every update. That added 60-120 sec to each update because it
# was a full --force-reinstall pulling the whole repo. The user
# already migrated to STAN\venv earlier in this script, and PATH
# now points only at the new venv, so re-installing into the old
# one is wasted work. We keep the PATH cleanup (fast, useful) and
# drop the re-install. Operator can manually `rmdir /s .stan`
# whenever they want to free the disk space; nothing relies on it.
$newStanExe = Join-Path $env:USERPROFILE "STAN\venv\Scripts\stan.exe"
if ((Test-Path $newStanExe) -and (Test-Path $oldVenvPython)) {
    $oldScripts = Join-Path $oldVenv "Scripts"
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -and $userPath -like "*$oldScripts*") {
        $parts = $userPath -split ";"
        $filtered = @()
        foreach ($p in $parts) { if ($p -ne $oldScripts -and $p -ne "") { $filtered += $p } }
        $cleanPath = $filtered -join ";"
        [Environment]::SetEnvironmentVariable("PATH", $cleanPath, "User")
        $env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));$cleanPath"
        Write-Host "  Removed old .stan\venv from PATH." -ForegroundColor Gray
    }
    Write-Host "  Old .stan venv left in place (no longer on PATH). Delete manually to free disk space." -ForegroundColor Gray
}

# -- Check DIA-NN, pinned to the 2.3 line the community benchmark accepts --
$diannLine = ($DiannPinnedVersion.Split(".")[0..1]) -join "."
Write-Host ""
Write-Host "  [2/3] Checking DIA-NN (community benchmark needs $diannLine.x)..." -ForegroundColor Cyan
$ErrorActionPreference = "Continue"
$env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));$([Environment]::GetEnvironmentVariable('Path','User'))"

$diannSearchPaths = @(Get-DiannSearchRoots)
$diannFound = @(Find-DiannCandidates $diannSearchPaths $env:Path)
foreach ($c in $diannFound) {
    $shownVer = $c.Version
    if (-not $shownVer) { $shownVer = "unknown version" }
    Write-Host "  found $($c.Path) ($shownVer)" -ForegroundColor Gray
}
$diannBest = Select-CompatibleDiann $diannFound $DiannPinnedVersion $DiannCommunityExactVersion

# This step must never wait for a person. start_stan_loop.bat runs this
# script unattended for the remote update_stan action, after the watcher has
# been stopped above and before the relaunch below, so an admin prompt here
# would leave the watcher down until someone at the instrument answered it.
# So Install-PinnedDiann gets no elevation, and a failed MSI install is
# remembered in $diannMarker: the ~250 MB download is not repeated on every
# later update, only the instructions are.
$diannMarker = Get-DiannMsiMarkerPath $env:USERPROFILE
if ($diannBest) {
    Write-Host "  DIA-NN $($diannBest.Version) is compatible: $($diannBest.Path)" -ForegroundColor Green
} elseif (Test-DiannMsiMarker $diannMarker $DiannPinnedMsiName) {
    Write-Host "  No DIA-NN $diannLine.x here, and an earlier update could not install" -ForegroundColor Yellow
    Write-Host "  $DiannPinnedMsiName without admin rights, so it is not downloaded again." -ForegroundColor Yellow
} else {
    if ($diannFound.Count -gt 0) {
        Write-Host "  None of these is DIA-NN $diannLine.x, and the community benchmark rejects" -ForegroundColor Yellow
        Write-Host "  any other version. Installing DIA-NN $DiannPinnedVersion alongside; the" -ForegroundColor Yellow
        Write-Host "  existing install is left untouched." -ForegroundColor Yellow
    } else {
        Write-Host "  DIA-NN not found. Installing DIA-NN $DiannPinnedVersion..." -ForegroundColor Yellow
    }
    $diannOk = Install-PinnedDiann $DiannPinnedMsiName $DiannPinnedMsiSha256 $false $diannMarker
    if ($diannOk) {
        $env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));$([Environment]::GetEnvironmentVariable('Path','User'))"
        $diannFound = @(Find-DiannCandidates $diannSearchPaths $env:Path)
        $diannBest = Select-CompatibleDiann $diannFound $DiannPinnedVersion $DiannCommunityExactVersion
        if ($diannBest) {
            Write-Host "  DIA-NN installed: $($diannBest.Path)" -ForegroundColor Green
        } else {
            Write-Host "  The installer finished but no DIA-NN $diannLine.x appeared under the usual folders." -ForegroundColor Yellow
        }
    }
}

if ($diannBest) {
    Remove-Item -LiteralPath $diannMarker -Force -ErrorAction SilentlyContinue
    $null = Use-EngineFirstOnPath $diannBest.Path "DiaNN.exe" "DIA-NN $($diannBest.Version)" "diann_path"
    $baselineWarning = @(Get-BaselineDiannWarning (Get-BaselineDiannPick $venvPython) $diannBest.Path)
    foreach ($line in $baselineWarning) { Write-Host $line -ForegroundColor Yellow }
} else {
    $manualUrl = Get-DiannMsiUrl $DiannPinnedMsiName
    Write-Host "  DIA searches need DIA-NN $diannLine.x. Download and double-click" -ForegroundColor Yellow
    Write-Host "    $manualUrl" -ForegroundColor Yellow
    Write-Host "  (Windows asks for an administrator), then run update-stan.bat again so STAN uses it." -ForegroundColor Yellow
}
$ErrorActionPreference = "Stop"

# -- Check Sage, pinned to v0.14.7 --
Write-Host ""
Write-Host "  [3/3] Checking Sage (pinned v$SagePinnedVersion)..." -ForegroundColor Cyan
$ErrorActionPreference = "Continue"
$env:Path = "$([Environment]::GetEnvironmentVariable('Path','Machine'));$([Environment]::GetEnvironmentVariable('Path','User'))"
$sageDir = Join-Path $env:USERPROFILE "STAN\tools\sage"

$sagePath = Resolve-PinnedSage $sageDir $SagePinnedVersion $SagePinnedExeSha256 $env:Path
if ($sagePath) {
    Write-Host "  Sage v$SagePinnedVersion found: $sagePath" -ForegroundColor Green
} else {
    $otherSage = @(Get-ExesOnPath $env:Path "sage.exe")
    $oldSageDir = Join-Path $env:USERPROFILE ".stan\tools\sage"
    if ($otherSage.Count -gt 0) {
        Write-Host "  $($otherSage[0]) is not the Sage v$SagePinnedVersion release binary (sha256 differs)." -ForegroundColor Yellow
        Write-Host "  Installing v$SagePinnedVersion alongside; the existing copy is left in place." -ForegroundColor Yellow
    } elseif ((Test-Path $sageDir) -or (Test-Path $oldSageDir)) {
        Write-Host "  The Sage under STAN\tools is not the v$SagePinnedVersion release binary. Installing v$SagePinnedVersion..." -ForegroundColor Yellow
    } else {
        Write-Host "  Sage not found. Installing v$SagePinnedVersion..." -ForegroundColor Yellow
    }
    $sagePath = Install-PinnedSage $sageDir $SagePinnedVersion $SagePinnedZipSha256 $SagePinnedExeSha256
    if ($sagePath) {
        Write-Host "  Sage installed: $sagePath" -ForegroundColor Green
        Write-Host "  (it prints 'sage 0.14.6' for --version; that is the v$SagePinnedVersion release binary)" -ForegroundColor Gray
    }
}
if ($sagePath) {
    $null = Use-EngineFirstOnPath $sagePath "sage.exe" "Sage v$SagePinnedVersion" "sage_path"
} else {
    $manualSage = Get-SageZipUrl $SagePinnedVersion
    Write-Host "  DDA searches need Sage v$SagePinnedVersion. Install it by hand from $manualSage" -ForegroundColor Yellow
}
$ErrorActionPreference = "Stop"

# -- Self-update bat files --
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $scriptDir) { $scriptDir = Get-Location }
try {
    $t = [DateTime]::Now.Ticks
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/update-stan.bat?t=$t" -OutFile "$scriptDir\update-stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/start_stan.bat?t=$t" -OutFile "$scriptDir\start_stan.bat" -UseBasicParsing -ErrorAction SilentlyContinue
    # start_stan_loop.bat is the supervised wrapper used by the fleet
    # restart_watcher action. Refreshed here on every update so each
    # instrument always has the current version (the .bat lives at the
    # repo root, not in the pip package, so update-stan.bat has to
    # fetch it explicitly).
    # Dropped alongside update-stan.bat in $scriptDir so operators find
    # both in the same location (typically %USERPROFILE%\Downloads).
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/bsphinney/stan/main/start_stan_loop.bat?t=$t" -OutFile "$scriptDir\start_stan_loop.bat" -UseBasicParsing -ErrorAction SilentlyContinue
} catch {}

# -- Done --
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "    STAN is up to date!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""

# Auto-launch the watcher. v0.2.146: the updater kills stan.exe at the
# start (step 1 above) so pip can overwrite it; before this change it
# never re-launched, which meant operators often forgot to restart
# watch after an update and newly-acquired files piled up as orphans
# (saw 158 orphans on timsTOF HT on 2026-04-21). Prefer the supervised
# start_stan_loop.bat wrapper (auto-restart on crash) if present; fall
# back to a plain `stan watch` otherwise. Detached via Start-Process
# so closing this updater window doesn't kill watch.
# v0.2.148 duplicate-process guard: check whether a stan.exe is
# already running before spawning a new one. Prevents a second
# click on update-stan.bat (or the supervised-watch update branch)
# from creating parallel watchers / dashboards / backfill windows.
# We distinguish watch vs dashboard by CommandLine because both
# share the same image name. Get-CimInstance is available on
# Windows PowerShell 5.1 which is the instrument-PC target.
function Test-StanProcessRunning {
    param([string]$subcommand)
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='stan.exe'" -ErrorAction SilentlyContinue
        foreach ($p in $procs) {
            if ($p.CommandLine -and $p.CommandLine -match "\b$subcommand\b") {
                return $true
            }
        }
    } catch {}
    return $false
}

# v0.2.170: set a stable working directory for every spawned
# process. Without this they inherit update-stan.bat's parent
# folder (usually Downloads) and any STAN code path using a
# relative output path writes there. Brett 2026-04-23 found
# ~40 DIA-NN report directories in Downloads for this reason.
$stanHome = Join-Path $env:USERPROFILE "STAN"
if (-not (Test-Path $stanHome)) {
    New-Item -ItemType Directory -Path $stanHome -Force | Out-Null
}

$loopBat = Join-Path $scriptDir "start_stan_loop.bat"
if (Test-StanProcessRunning "watch") {
    Write-Host "  Watcher already running - skipping launch." -ForegroundColor Gray
} elseif (Test-Path $loopBat) {
    Write-Host "  Launching watcher (supervised)..." -ForegroundColor Cyan
    Start-Process -FilePath $loopBat -WorkingDirectory $stanHome -WindowStyle Normal
} else {
    Write-Host "  Launching watcher..." -ForegroundColor Cyan
    Start-Process -FilePath $stanExe -ArgumentList "watch" -WorkingDirectory $stanHome -WindowStyle Normal
}

# Dashboard also gets its own detached window - unless one is already up.
if (Test-StanProcessRunning "dashboard") {
    Write-Host "  Dashboard already running - skipping launch." -ForegroundColor Gray
} else {
    Write-Host "  Launching dashboard..." -ForegroundColor Cyan
    Start-Process -FilePath $stanExe -ArgumentList "dashboard" -WorkingDirectory $stanHome -WindowStyle Normal
}

# Auto-backfill every metric gap. New metrics land in new releases
# (v0.2.139 PEG, v0.2.143 drift, v0.2.116 cIRT, v0.2.147 PEG/drift
# breakdown tables...) and existing DB rows from before the release
# have NULL for those columns until backfill fills them in.
# v0.2.147: chain ALL the backfills in one detached console so an
# overnight update truly fills every gap (metrics + cIRT + TIC +
# PEG + window drift). Sequential within the one window so they
# don't thrash the disk by running in parallel on the same .d
# files. The report-parquet-only commands (backfill-metrics,
# backfill-cirt) run first - they're fast. Then the slow raw-
# file-scanning commands (backfill-tic, backfill-peg, backfill-
# window-drift). On a timsTOF with a few hundred runs this is
# multiple hours of work - designed for overnight.
# v0.2.148: skip backfill launch if a previous run's console is
# still open (window title set to "STAN overnight backfill" below).
# Avoids spawning parallel backfills when the operator clicks the
# updater twice or start_stan_loop races the manual click.
$backfillAlreadyRunning = $false
try {
    $cmdProcs = @(Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue)
    foreach ($cp in $cmdProcs) {
        if ($cp -and $cp.CommandLine -match "STAN overnight backfill") { $backfillAlreadyRunning = $true }
    }
} catch {}

if ($backfillAlreadyRunning) {
    Write-Host "  Overnight backfill already running - skipping launch." -ForegroundColor Gray
    $backfillCmd = $null
} else {
    Write-Host "  Launching overnight backfill sweep (metrics + cIRT + TIC + PEG + features + drift)..." -ForegroundColor Cyan
    # v0.2.201: `stan install-4dff` fetches Bruker's universal feature
    # finder binary (~65 MB) on first run; idempotent after that.
    # `backfill-features` runs 4DFF on every Bruker .d that doesn't
    # yet have a .features sidecar, generating authoritative charge
    # assignments that the v0.2.200 feature-based drift detector
    # uses. On Thermo instruments stan install-4dff is a no-op and
    # backfill-features iterates nothing (Bruker-only feature finder)
    # so it's safe to always include in the chain.
    # Drift runs AFTER features so detect_drift_best() has the new
    # data available on rescoring.
    # Built with -join rather than '+' (house rule for PS 5.1); the
    # resulting command line is unchanged.
    $backfillSteps = @(
        "title STAN overnight backfill",
        "echo === stan install-4dff ===", "stan install-4dff",
        "echo === stan fix-spds      ===", "stan fix-spds",
        "echo === stan backfill-metrics ===", "stan backfill-metrics",
        "echo === stan derive-cirt-panel --auto ===", "stan derive-cirt-panel --auto",
        "echo === stan backfill-cirt    ===", "stan backfill-cirt",
        "echo === stan backfill-tic --force --push ===", "stan backfill-tic --force --push",
        "echo === stan backfill-peg     ===", "stan backfill-peg",
        "echo === stan backfill-features ===", "stan backfill-features",
        "echo === stan backfill-window-drift --force ===", "stan backfill-window-drift --force",
        "echo ALL BACKFILLS COMPLETE", "pause"
    )
    $backfillCmd = $backfillSteps -join " && "
    Start-Process -FilePath "cmd.exe" -ArgumentList "/k", $backfillCmd -WorkingDirectory $stanHome -WindowStyle Normal
}

Write-Host ""
Write-Host "  Launched (or skipped): watcher, dashboard, overnight backfill." -ForegroundColor Green
Write-Host "  Closing this updater window will NOT stop them." -ForegroundColor Gray
Write-Host "  The backfill window sweeps every metric gap in the DB and prints" -ForegroundColor Gray
Write-Host "  'ALL BACKFILLS COMPLETE' when done. Safe to leave running overnight." -ForegroundColor Gray
Write-Host ""
