# test_windows_installer.ps1
#
# Tests for the Windows install path:
#   install_stan.ps1   run by install-stan.bat on a new PC
#   update_stan.ps1    run by update-stan.bat, by hand or unattended from
#                      start_stan_loop.bat for the remote update_stan action
#   stan.bat           the one-icon launcher (first-run bootstrap + supervisor)
#
#     pwsh -NoProfile -File tests/test_windows_installer.ps1
#
# Not part of the pytest suite (CI is Python-only) -- run it by hand after
# touching any of those files, like test_flinders_copy.ps1 and
# test_instrument_copy_scripts.ps1. There is no PowerShell on the dev Mac; a
# portable pwsh from the PowerShell/PowerShell release tarball runs it
# without installing anything.
#
# It loads the real function definitions out of the shipped scripts through
# the AST and never runs their top-level code as a whole (which asks for the
# DIA-NN license and installs things); where it runs a step's own code, it
# cuts that step out of the shipped file and stubs everything that would
# touch the network or msiexec. Release metadata is a trimmed copy of what
# the GitHub releases API returned on 2026-09-29. Section 17 asks the real
# stan/baseline.py through python3 when that can import STAN, and skips
# otherwise.
#
# WHAT THIS IS GUARDING. Both scripts used to install "the newest MSI in the
# DIA-NN release", which by September 2026 was DIA-NN 2.7.0, and accepted
# any existing DIA-NN >= 2.3. The community relay rejects every submission
# whose DIA-NN major.minor is not 2.3, so a fresh Windows install produced
# rows that could never be submitted. Sage was "latest" too. And stan.bat
# stopped with an error when install-stan.bat was not already on disk.
# The review of that fix then found:
#   - `stan baseline` picks the highest DIA-NN on disk and labels its rows
#     from PATH, so 2.7.0 left beside the pinned 2.3.2 gave rows the relay
#     accepts under the wrong version (sections 13b, 17);
#   - 2.3.2 rows were accepted but never asset-verified (only an exact 2.3.0
#     was, and it ships for Windows only as a Preview MSI). Brett widened the
#     rule to any 2.3.x on 2026-09-29 (sections 2, 3, 7);
#   - the unattended updater raised a UAC prompt and re-downloaded 250 MB on
#     every update (sections 13, 13b);
#   - a PATH entry on a missing drive broke Get-ExesOnPath (section 8b);
#   - the instruments.yml step wrote keys nothing reads, with a byte-order
#     mark Python on Windows cannot parse (section 13c);
#   - stan.bat never checks DIA-NN, so labs that only use it never hear
#     about the pin (section 16).

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Failures = 0
$Skips = 0
$onWindows = $false
if ($IsWindows) { $onWindows = $true }

function Check($Label, $Got, $Want) {
    if ("$Got" -eq "$Want") { Write-Host "  ok   $Label" }
    else { Write-Host "  FAIL $Label -- got '$Got', want '$Want'"; $script:Failures++ }
}

function Skip($Label, $Why) {
    Write-Host "  skip $Label -- $Why"
    $script:Skips++
}

function Load-Script($relPath) {
    $target = Join-Path $repoRoot $relPath
    if (-not (Test-Path -LiteralPath $target)) { Write-Host "cannot find $target"; exit 1 }
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($target, [ref] $tokens, [ref] $errors)
    if ($errors -and $errors.Count -gt 0) {
        Write-Host "PARSE ERRORS in $relPath"
        foreach ($e in $errors) { Write-Host "  line $($e.Extent.StartLineNumber): $($e.Message)" }
        exit 1
    }
    return $ast
}

function Get-Block($relPath) {
    $text = [System.IO.File]::ReadAllText((Join-Path $repoRoot $relPath))
    $m = [regex]::Match($text, '(?s)# >>> engine pins.*?# <<< engine pins')
    if (-not $m.Success) { return "" }
    return $m.Value
}

# The value a script assigns to $Name at top level, and how many times it
# is assigned anywhere (a second assignment would silently override the pin).
function Get-Constant($ast, $Name) {
    $vals = @()
    foreach ($a in $ast.FindAll({
        $args[0] -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($a.Left.Extent.Text -eq "`$$Name") { $vals += $a.Right.Extent.Text.Trim('"', "'") }
    }
    return ,$vals
}

# The shipped text between two markers: one step of a script.
function Get-Segment($rel, $from, $to) {
    $text = [System.IO.File]::ReadAllText((Join-Path $repoRoot $rel))
    $i = $text.IndexOf($from)
    if ($i -lt 0) { return "" }
    $j = $text.IndexOf($to, $i)
    if ($j -le $i) { return "" }
    return $text.Substring($i, $j - $i)
}

$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) "stan_wininst_$(Get-Random)"
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null

function New-Fake($path, $content) {
    $dir = Split-Path -Parent $path
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    Set-Content -LiteralPath $path -Value $content -NoNewline
    return $path
}

# A stand-in for a native program that prints $Lines and exits 0. A .cmd on
# Windows, a sh script elsewhere; returns the path to run.
function New-FakeProgram($pathNoExt, [string[]]$Lines) {
    $dir = Split-Path -Parent $pathNoExt
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    if ($script:onWindows) {
        $p = "$pathNoExt.cmd"
        $body = @("@echo off")
        foreach ($l in $Lines) { $body += "echo $l" }
        Set-Content -LiteralPath $p -Value $body
        return $p
    }
    $body = @("#!/bin/sh")
    foreach ($l in $Lines) { $body += "echo '$l'" }
    Set-Content -LiteralPath $pathNoExt -Value $body
    & chmod +x $pathNoExt
    return $pathNoExt
}

function Sha($path) { return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }

# ------------------------------------------------------------ 1. parse
Write-Host ""
Write-Host "1. both installers parse, and share one engine-pin block"
$instAst = Load-Script "install_stan.ps1"
$updAst  = Load-Script "update_stan.ps1"
Check "install_stan.ps1 parses" ($null -ne $instAst) "True"
Check "update_stan.ps1 parses"  ($null -ne $updAst) "True"
$instBlock = Get-Block "install_stan.ps1"
$updBlock  = Get-Block "update_stan.ps1"
Check "install_stan.ps1 has the block" ($instBlock.Length -gt 1000) "True"
# Each script is downloaded and run on its own, so the helpers are
# duplicated. Two copies are only safe while they are the same copy.
Check "the two blocks are byte-identical" ($instBlock -ceq $updBlock) "True"

# Load the functions (never the top-level code).
foreach ($func in $instAst.FindAll({
    $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    Invoke-Expression $func.Extent.Text
}

# --------------------------------------------------- 2. the pins themselves
Write-Host ""
Write-Host "2. pins are single constants that agree with community_params.py"
foreach ($pair in @(@("install", $instAst), @("update", $updAst))) {
    $name = $pair[0]; $ast = $pair[1]
    foreach ($c in @("DiannPinnedVersion", "DiannPinnedMsiName", "DiannPinnedMsiSha256",
                     "DiannCommunityExactVersion", "SagePinnedVersion",
                     "SagePinnedZipSha256", "SagePinnedExeSha256")) {
        Check "$name assigns `$$c exactly once" (Get-Constant $ast $c).Count 1
    }
}
$DiannPinnedVersion         = (Get-Constant $instAst "DiannPinnedVersion")[0]
$DiannPinnedMsiName         = (Get-Constant $instAst "DiannPinnedMsiName")[0]
$DiannPinnedMsiSha256       = (Get-Constant $instAst "DiannPinnedMsiSha256")[0]
$DiannCommunityExactVersion = (Get-Constant $instAst "DiannCommunityExactVersion")[0]
$SagePinnedVersion          = (Get-Constant $instAst "SagePinnedVersion")[0]
$SagePinnedZipSha256        = (Get-Constant $instAst "SagePinnedZipSha256")[0]
$SagePinnedExeSha256        = (Get-Constant $instAst "SagePinnedExeSha256")[0]

$paramsText = [System.IO.File]::ReadAllText((Join-Path $repoRoot "stan/search/community_params.py"))
$communityDiann = ""
$communitySage = ""
if ($paramsText -match '"diann":\s*"([^"]+)"') { $communityDiann = $Matches[1] }
if ($paramsText -match '"sage":\s*"([^"]+)"') { $communitySage = $Matches[1] }
Check "read PINNED_TOOL_VERSIONS diann" ($communityDiann -ne "") "True"
Check "installer DIA-NN $DiannPinnedVersion passes the relay's major.minor rule vs $communityDiann" `
    (Test-DiannCompatible $DiannPinnedVersion $communityDiann) "True"
Check "DiannCommunityExactVersion = PINNED_TOOL_VERSIONS diann" $DiannCommunityExactVersion $communityDiann
Check "installer Sage = PINNED_TOOL_VERSIONS sage" $SagePinnedVersion $communitySage
Check "the MSI name is for the pinned version" `
    ($DiannPinnedMsiName -like "DIA-NN-$DiannPinnedVersion-*.msi") "True"
foreach ($h in @($DiannPinnedMsiSha256, $SagePinnedZipSha256, $SagePinnedExeSha256)) {
    Check "hash '$($h.Substring(0, 8))...' is 64 lowercase hex" ($h -cmatch '^[0-9a-f]{64}$') "True"
}
# The comment must describe the rule community_params.py actually applies:
# since 2026-09-29 submit.py fills the asset hashes for any 2.3.x
# (is_asset_hash_eligible_diann, the same major.minor rule as acceptance).
Check "block no longer claims 'any 2.3.x is usable'" ($instBlock -match "any 2\.3\.x is usable") "False"
Check "block no longer claims 2.3.2 rows are unverified" ($instBlock -match "assets_verified=False") "False"
Check "block names the rule it relies on" ($instBlock -match "is_asset_hash_eligible_diann") "True"
Check "community_params gates asset hashes on the pinned major.minor" `
    ($paramsText -match 'def is_asset_hash_eligible_diann[\s\S]*?check_diann_version_compatible') "True"
# The switch-to-2.3.0 instructions in the comment, read back for section 3.
$altName = ""
$altSha = ""
foreach ($l in ($instBlock -split "`n")) {
    if ($l -match '^#\s+\$DiannPinnedMsiName\s*=\s*"([^"]+)"') { $altName = $Matches[1] }
    if ($l -match '^#\s+\$DiannPinnedMsiSha256\s*=\s*"([0-9a-f]{64})"') { $altSha = $Matches[1] }
}
Check "comment gives the exact-pin MSI to switch to" $altName "DIA-NN-2.3.0-Academia-Preview.msi"

# ------------------------------------ 3. the asset choice, on real release JSON
# Trimmed from GET https://api.github.com/repos/vdemichev/DiaNN/releases/tags/2.0
# on 2026-09-29 (tag "2.0" holds every 2.x build).
$diannReleaseJson = @'
{ "tag_name": "2.0", "assets": [
  {"name": "DIA-NN-2.0-Academia-Linux.zip", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.0-Academia-Linux.zip"},
  {"name": "DIA-NN-2.0-Academia.msi", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.0-Academia.msi"},
  {"name": "DIA-NN-2.0.1-Academia-Linux.zip", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.0.1-Academia-Linux.zip"},
  {"name": "DIA-NN-2.0.1-Academia.msi", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.0.1-Academia.msi"},
  {"name": "DIA-NN-2.0.2-Academia-Linux.zip", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.0.2-Academia-Linux.zip"},
  {"name": "DIA-NN-2.0.2-Academia.msi", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.0.2-Academia.msi"},
  {"name": "DIA-NN-2.1.0-Academia-Linux.zip", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.1.0-Academia-Linux.zip"},
  {"name": "DIA-NN-2.1.0-Academia.msi", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.1.0-Academia.msi"},
  {"name": "DIA-NN-2.2.0-Academia-Linux.zip", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.2.0-Academia-Linux.zip"},
  {"name": "DIA-NN-2.2.0-Academia.msi", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.2.0-Academia.msi"},
  {"name": "DIA-NN-2.3.0-Academia-Linux-Preview.zip", "digest": "sha256:1971187adbe81964923517db4c2d29703787a67686dfc6899a01f7337a39d0a4", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.0-Academia-Linux-Preview.zip"},
  {"name": "DIA-NN-2.3.0-Academia-Preview.msi", "digest": "sha256:e3343856740529a865a8a04013158b95b929e56779a3911e13b8270a898088d6", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.0-Academia-Preview.msi"},
  {"name": "DIA-NN-2.3.1-Academia-Linux.zip", "digest": "sha256:614dba4449e06358469fbe443826658749f610d6e015ed4c771ec645e8457eb4", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.1-Academia-Linux.zip"},
  {"name": "DIA-NN-2.3.1-Academia.msi", "digest": "sha256:c5e737efb0febe3168c19573cab042fb922796701a42ba5a9cee6b210030ee7b", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.1-Academia.msi"},
  {"name": "DIA-NN-2.3.2-Academia-Linux.zip", "digest": "sha256:dab4a5267a39d2c3f4e36430389f6f6f893c9b9cfd05571cc94c5941aad7b446", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.2-Academia-Linux.zip"},
  {"name": "DIA-NN-2.3.2-Academia.msi", "digest": "sha256:207233c438ef9f7d9d7afe2e28c8062180305b04f9d5f6bd587f4733eb0a9389", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.2-Academia.msi"},
  {"name": "DIA-NN-2.5.0-Academia-Linux.zip", "digest": "sha256:05268fb6c778471beb46583400c862529b8ee452a620bb8bfdc8e9cc42eaf695", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.5.0-Academia-Linux.zip"},
  {"name": "DIA-NN-2.5.0-Academia.msi", "digest": "sha256:5747a36874a35a8fea2c9b0a118a66b277007e3061413cffa383470ad3519c40", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.5.0-Academia.msi"},
  {"name": "DIA-NN-2.5.1-Academia-Linux.zip", "digest": "sha256:0445996c6f1f49d10865693cb575a5665272411607e22d6c1caeba3acebc5173", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.5.1-Academia-Linux.zip"},
  {"name": "DIA-NN-2.5.1-Academia.msi", "digest": "sha256:2db4ebea0e9501f8fbb3ead99f9f95f9cb3a9c59ec3df8fb157877485d4be6e4", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.5.1-Academia.msi"},
  {"name": "DIA-NN-2.6.0-Academia-Linux.zip", "digest": "sha256:d03fe10161313c00c20529f6f67d7f9b2da1be30c3de0f174021972e12eaa90e", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.6.0-Academia-Linux.zip"},
  {"name": "DIA-NN-2.6.0-Academia.msi", "digest": "sha256:0eebe81de659ebcf74c86303daff4cd3bc27eaa0f21bd6ccb54350ff7cc1f4df", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.6.0-Academia.msi"},
  {"name": "DIA-NN-2.6.1-Academia-Linux.zip", "digest": "sha256:f98e396af791f1903168b365a83fc2cc72208cc133281b9ce6ebe3a794b999ba", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.6.1-Academia-Linux.zip"},
  {"name": "DIA-NN-2.6.1-Academia.msi", "digest": "sha256:6f142e4e0f9e141a3f6d54e6152cc228d9b7f049659aff10d95edfc1dc1bb06e", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.6.1-Academia.msi"},
  {"name": "DIA-NN-2.7.0-Academia-Linux.zip", "digest": "sha256:246ca8baf2e9f308854976f94b8761e452275a97b3632070e275a2b4401cb223", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.7.0-Academia-Linux.zip"},
  {"name": "DIA-NN-2.7.0-Academia.msi", "digest": "sha256:649b064713b009d5cf8c6bc7f8052729c89cf635130bd1fd1e7e4acc139dcddf", "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.7.0-Academia.msi"},
  {"name": "LICENSE.txt", "digest": null, "browser_download_url": "https://github.com/vdemichev/DiaNN/releases/download/2.0/LICENSE.txt"}
] }
'@
# Trimmed from GET https://api.github.com/repos/lazear/sage/releases/tags/v0.14.7
$sageReleaseJson = @'
{ "tag_name": "v0.14.7", "assets": [
  {"name": "sage-v0.14.7-aarch64-apple-darwin.tar.gz", "size": 6619916, "digest": null, "browser_download_url": "https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-aarch64-apple-darwin.tar.gz"},
  {"name": "sage-v0.14.7-aarch64-unknown-linux-gnu.tar.gz", "size": 7619337, "digest": null, "browser_download_url": "https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-aarch64-unknown-linux-gnu.tar.gz"},
  {"name": "sage-v0.14.7-aarch64-unknown-linux-musl.tar.gz", "size": 7983280, "digest": null, "browser_download_url": "https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-aarch64-unknown-linux-musl.tar.gz"},
  {"name": "sage-v0.14.7-x86_64-apple-darwin.tar.gz", "size": 7318856, "digest": null, "browser_download_url": "https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-x86_64-apple-darwin.tar.gz"},
  {"name": "sage-v0.14.7-x86_64-pc-windows-msvc.zip", "size": 6024770, "digest": null, "browser_download_url": "https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-x86_64-pc-windows-msvc.zip"},
  {"name": "sage-v0.14.7-x86_64-unknown-linux-gnu.tar.gz", "size": 8135705, "digest": null, "browser_download_url": "https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-x86_64-unknown-linux-gnu.tar.gz"},
  {"name": "sage-v0.14.7-x86_64-unknown-linux-musl.tar.gz", "size": 8681640, "digest": null, "browser_download_url": "https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-x86_64-unknown-linux-musl.tar.gz"}
] }
'@
Write-Host ""
Write-Host "3. the pinned DIA-NN asset is the newest non-preview 2.3.x MSI"
$diannRel = $diannReleaseJson | ConvertFrom-Json
$pinnedAsset = $null
$altAsset = $null
foreach ($a in $diannRel.assets) {
    if ($a.name -eq $DiannPinnedMsiName) { $pinnedAsset = $a }
    if ($a.name -eq $altName) { $altAsset = $a }
}
Check "release has $DiannPinnedMsiName" ($null -ne $pinnedAsset) "True"
if ($pinnedAsset) {
    Check "download URL is the one the script builds" $pinnedAsset.browser_download_url (Get-DiannMsiUrl $DiannPinnedMsiName)
    Check "GitHub's digest equals the pinned sha256" $pinnedAsset.digest "sha256:$DiannPinnedMsiSha256"
}
# Recompute the choice from the JSON, so the pin is shown to be the newest
# non-preview 2.3.x rather than merely asserted to be.
$newest23 = $null
$newestAny = $null
foreach ($a in $diannRel.assets) {
    if ($a.name -notmatch '^DIA-NN-(\d+\.\d+(\.\d+)?)-Academia\.msi$') { continue }
    $v = [version]$Matches[1]
    if (($null -eq $newestAny) -or ($v -gt $newestAny)) { $newestAny = $v }
    if (Test-DiannCompatible $Matches[1] $communityDiann) {
        if (($null -eq $newest23) -or ($v -gt $newest23)) { $newest23 = $v }
    }
}
Check "newest non-preview 2.3.x MSI in the release" "$newest23" $DiannPinnedVersion
Check "2.3.0 exists only as a Preview MSI (why the pin is not 2.3.0)" `
    (@($diannRel.assets | ForEach-Object { $_.name }) -contains "DIA-NN-2.3.0-Academia.msi") "False"
# The retired rule -- newest MSI in the release -- and why it was wrong.
Check "the old 'newest MSI' rule would pick" "$newestAny" "2.7.0"
Check "...which the relay rejects" (Test-DiannCompatible "$newestAny" $communityDiann) "False"
# The comment's switch-to-2.3.0 instructions point at a real asset and its
# published digest, so following them works.
Check "the documented exact-pin MSI is in the release" ($null -ne $altAsset) "True"
if ($altAsset) {
    Check "...and the documented sha256 is GitHub's digest for it" $altAsset.digest "sha256:$altSha"
    Check "...and it is exactly the community version" ($altName -like "DIA-NN-$communityDiann-*") "True"
}

Write-Host ""
Write-Host "4. the pinned Sage asset"
$sageRel = $sageReleaseJson | ConvertFrom-Json
$sageAsset = $null
foreach ($a in $sageRel.assets) { if ($a.name -eq (Get-SageZipName $SagePinnedVersion)) { $sageAsset = $a } }
Check "release has sage-v0.14.7-x86_64-pc-windows-msvc.zip" ($null -ne $sageAsset) "True"
if ($sageAsset) {
    Check "Sage URL is the one the script builds" $sageAsset.browser_download_url (Get-SageZipUrl $SagePinnedVersion)
    # No upstream checksum, which is why the pin is a hash we computed. If
    # GitHub starts publishing one, compare it with the pin.
    Check "release publishes no digest for it" "$($sageAsset.digest)" ""
    Check "size matches the release (6024770 bytes)" $sageAsset.size 6024770
}
Check "unpacked exe path" (Get-SageExePath "T" "0.14.7") (Join-Path (Join-Path "T" "sage-v0.14.7-x86_64-pc-windows-msvc") "sage.exe")

# ------------------------------------------------------ 5. compatibility rule
Write-Host ""
Write-Host "5. an existing DIA-NN counts only when major.minor is 2.3"
foreach ($case in @(
    @("2.3.0", "True"), @("2.3.1", "True"), @("2.3.2", "True"), @("2.3", "True"),
    @("2.7.0", "False"), @("2.5.1", "False"), @("2.2.0", "False"), @("1.8.1", "False"),
    @("3.3.0", "False"), @("2.30.0", "False"), @("", "False"), @("abc", "False"), @("2", "False"))) {
    Check "Test-DiannCompatible '$($case[0])'" (Test-DiannCompatible $case[0] $communityDiann) $case[1]
}

Write-Host ""
Write-Host "6. version from the install folder, and from the program header"
foreach ($case in @(
    @('C:\Program Files\DIA-NN\2.3.2\DiaNN.exe', "2.3.2"),
    @('C:\Program Files\DIA-NN\2.7.0\diann.exe', "2.7.0"),
    @('C:\DIA-NN\1.8.1\DiaNN.exe', "1.8.1"),
    @('C:\DIA-NN\2.0\DiaNN.exe', "2.0"),
    @('C:\Users\lab\Downloads\DIA-NN-2.3.1-Academia\DiaNN.exe', "2.3.1"),
    @('C:\tools\v1.2\DIA-NN\2.7.0\DiaNN.exe', "2.7.0"),
    @('C:\Program Files\DIA-NN\DiaNN.exe', ""),
    @('', ""))) {
    Check "from path '$($case[0])'" (Get-DiannVersionFromPath $case[0]) $case[1]
}
$banner = "DIA-NN 2.3.2 Academia  (Data-Independent Acquisition by Neural Networks)`nCompiled on Sep 26 2025 02:56:25"
Check "from header" (Get-DiannVersionFromBanner $banner) "2.3.2"
Check "from an empty header" (Get-DiannVersionFromBanner "") ""
Check "from unrelated output" (Get-DiannVersionFromBanner "error: missing dll") ""

# ------------------------------------------------------ 7. choosing an install
Write-Host ""
Write-Host "7. choosing among installed DIA-NN versions"
function C($p, $v) { return [pscustomobject]@{ Path = $p; Version = $v; How = "test" } }
$exact = $DiannCommunityExactVersion
$mixed = @((C "a\2.7.0" "2.7.0"), (C "b\2.3.1" "2.3.1"), (C "c\1.8.1" "1.8.1"), (C "d\2.3.2" "2.3.2"), (C "e" ""))
Check "newest 2.3.x wins over 2.7.0" (Select-CompatibleDiann $mixed $DiannPinnedVersion $exact).Path "d\2.3.2"
[array]::Reverse($mixed)
Check "order does not matter" (Select-CompatibleDiann $mixed $DiannPinnedVersion $exact).Path "d\2.3.2"
Check "2.7 and 2.6 only: nothing (so the pinned one is installed)" `
    ($null -eq (Select-CompatibleDiann @((C "x" "2.7.0"), (C "y" "2.6.1")) $DiannPinnedVersion $exact)) "True"
Check "an existing 2.3.0 Preview is kept" (Select-CompatibleDiann @((C "p" "2.3.0"), (C "q" "2.5.0")) $DiannPinnedVersion $exact).Path "p"
# 2.3.0 is the exact version Hive's container runs, so an install of it is
# used even when a newer 2.3.x sits beside it.
Check "2.3.0 beside 2.3.2: 2.3.0 (matches Hive)" `
    (Select-CompatibleDiann @((C "new" "2.3.2"), (C "exact" "2.3.0"), (C "big" "2.7.0")) $DiannPinnedVersion $exact).Path "exact"
Check "...in either order" `
    (Select-CompatibleDiann @((C "exact" "2.3.0"), (C "new" "2.3.2")) $DiannPinnedVersion $exact).Path "exact"
Check "no preference given: newest 2.3.x" `
    (Select-CompatibleDiann @((C "exact" "2.3.0"), (C "new" "2.3.2")) $DiannPinnedVersion "").Path "new"
Check "no installs: nothing" ($null -eq (Select-CompatibleDiann @() $DiannPinnedVersion $exact)) "True"
Check "null input: nothing" ($null -eq (Select-CompatibleDiann $null $DiannPinnedVersion $exact)) "True"

# A real folder tree. The header stub stands in for running DiaNN.exe, and
# counts calls so we can see it is used only when the folder is silent.
Write-Host ""
Write-Host "8. finding installs on disk and on PATH"
Check "sandbox path names no version (else every guess below is off)" (Get-DiannVersionFromPath (Join-Path $sandbox "x")) ""
$pf = Join-Path $sandbox "Program Files/DIA-NN"
$d27 = New-Fake (Join-Path $pf "2.7.0/DiaNN.exe") "2.7.0"
$d232 = New-Fake (Join-Path $pf "2.3.2/DiaNN.exe") "2.3.2"
$d181 = New-Fake (Join-Path $pf "1.8.1/DiaNN.exe") "1.8.1"
$dBare = New-Fake (Join-Path $sandbox "C/DIA-NN/DiaNN.exe") "bare"
$dPath = New-Fake (Join-Path $sandbox "pathdir/DiaNN.exe") "onpath"
$script:bannerCalls = 0
function Get-DiannBannerVersion {
    param([string]$Exe)
    $script:bannerCalls++
    if ($Exe -like "*pathdir*") { return "2.5.1" }
    return "1.9.2"
}
$pathValue = "$(Join-Path $sandbox 'pathdir');$(Join-Path $pf '2.7.0');;$(Join-Path $sandbox 'nope')"
$roots = @($pf, (Join-Path $sandbox "C/DIA-NN"), (Join-Path $sandbox "missing"))
$found = @(Find-DiannCandidates $roots $pathValue)
Check "five distinct installs (PATH and folder scan de-duplicated)" $found.Count 5
Check "PATH hits come first, in PATH order" $found[0].Path $dPath
$byPath = @{}
foreach ($f in $found) { $byPath[$f.Path] = $f }
Check "2.3.2 read from its folder"  $byPath[$d232].Version "2.3.2"
Check "...by folder name"           $byPath[$d232].How "folder name"
Check "bare folder asks the program" $byPath[$dBare].How "program header"
Check "PATH-only copy asks too"     $byPath[$dPath].Version "2.5.1"
Check "header consulted only for the two silent folders" $script:bannerCalls 2
Check "selected: the 2.3.2 install" (Select-CompatibleDiann $found $DiannPinnedVersion $exact).Path $d232
Remove-Item -LiteralPath (Split-Path -Parent $d232) -Recurse -Force
$found = @(Find-DiannCandidates $roots $pathValue)
Check "without it: none compatible, so install alongside" ($null -eq (Select-CompatibleDiann $found $DiannPinnedVersion $exact)) "True"
Check "nothing found at all: empty, not null" (@(Find-DiannCandidates @((Join-Path $sandbox "missing")) "")).Count 0

# A PATH entry on a drive that is not there -- a disconnected mapped drive
# is common on lab PCs. Join-Path throws "Cannot find drive" for it, which
# used to escape Get-ExesOnPath: an error printed on every call where the
# engine steps run (Continue), and a stop where the rest of the script runs
# (Stop).
Write-Host ""
Write-Host "8b. a PATH entry on a missing drive is skipped quietly"
$realBin = Join-Path $sandbox "realbin"
$realExe = New-Fake (Join-Path $realBin "DiaNN.exe") "x"
$ghost = "Q:\tools"
$threw = ""
$got = @()
try { $got = @(Get-ExesOnPath "$ghost;$realBin" "DiaNN.exe") } catch { $threw = "$_" }
Check "under Stop: no throw" $threw ""
Check "...and the real one is still found" ($got -join "|") $realExe
$ErrorActionPreference = "Continue"
$all = @(Get-ExesOnPath "$ghost;$realBin" "DiaNN.exe" 2>&1)
$ErrorActionPreference = "Stop"
$errs = 0
$hitsOut = @()
foreach ($o in $all) {
    if ($o -is [System.Management.Automation.ErrorRecord]) { $errs++ } else { $hitsOut += "$o" }
}
Check "under Continue: no error written" $errs 0
Check "...and the same result" ($hitsOut -join "|") $realExe
$threw = ""
try { $null = Get-EnginePathPlan $ghost $realBin $realExe "DiaNN.exe" } catch { $threw = "$_" }
Check "the PATH plan survives it too" $threw ""
$threw = ""
try { $null = Resolve-PinnedSage (Join-Path $sandbox "notools") "0.14.7" "00" "$ghost;$realBin" } catch { $threw = "$_" }
Check "and the Sage lookup" $threw ""

# ------------------------------------------------------ 9. pointing STAN at it
Write-Host ""
Write-Host "9. PATH: pinned folder first, and a system-PATH copy is reported"
Check "moves an existing entry to the front" (Get-PathWithDirFirst 'A;B;C' 'C') 'C;A;B'
Check "ignores case and a trailing backslash" (Get-PathWithDirFirst 'A;c:\x\;B' 'C:\X') 'C:\X;A;B'
Check "adds a new entry" (Get-PathWithDirFirst 'A;B' 'X') 'X;A;B'
Check "drops empty entries" (Get-PathWithDirFirst 'A;;B;' 'X') 'X;A;B'
Check "empty PATH" (Get-PathWithDirFirst '' 'X') 'X'

$machineBin = Join-Path $sandbox "machinebin"
$sys27 = New-Fake (Join-Path $machineBin "DiaNN.exe") "2.7.0 on system PATH"
$want = New-Fake (Join-Path $sandbox "Program Files/DIA-NN/2.3.2/DiaNN.exe") "2.3.2"
$wantDir = Split-Path -Parent $want
$userBin = Join-Path $sandbox "userbin"
New-Item -ItemType Directory -Path $userBin -Force | Out-Null
Check "exes along PATH, in order" ((Get-ExesOnPath "$machineBin;$wantDir" "DiaNN.exe") -join "|") "$sys27|$want"
Check "system copy first: it shadows" (Get-ShadowingExe "$machineBin;$wantDir" "DiaNN.exe" $want) $sys27
Check "pinned first: nothing shadows" (Get-ShadowingExe "$wantDir;$machineBin" "DiaNN.exe" $want) ""
Check "none anywhere: nothing shadows" (Get-ShadowingExe $userBin "DiaNN.exe" $want) ""

$plan = Get-EnginePathPlan $userBin "$userBin;$machineBin" $want "DiaNN.exe"
Check "plan puts the pinned folder first on the user PATH" $plan.UserPath "$wantDir;$userBin;$machineBin"
Check "...and reports the change" $plan.Changed "True"
Check "...with nothing on the system PATH to beat it" $plan.Shadow ""
$plan = Get-EnginePathPlan $machineBin $userBin $want "DiaNN.exe"
Check "system-PATH 2.7.0 still wins, and the plan says so" $plan.Shadow $sys27
$plan = Get-EnginePathPlan $userBin "$wantDir;$userBin" $want "DiaNN.exe"
Check "already first: user PATH untouched" $plan.Changed "False"

# ------------------------------------------------------------ 10. hashing
Write-Host ""
Write-Host "10. sha256 checks"
$hello = New-Fake (Join-Path $sandbox "hello.txt") "hello"
$helloSha = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
Check "matching hash" (Test-FileSha256 $hello $helloSha) "True"
Check "upper-case hash also matches" (Test-FileSha256 $hello $helloSha.ToUpperInvariant()) "True"
Check "wrong hash" (Test-FileSha256 $hello $DiannPinnedMsiSha256) "False"
Check "missing file" (Test-FileSha256 (Join-Path $sandbox "nope.bin") $helloSha) "False"
Check "empty expectation never matches" (Test-FileSha256 $hello "") "False"

# ------------------------------------------------------------ 11. Sage
Write-Host ""
Write-Host "11. which Sage counts as pinned"
$tools = Join-Path $sandbox "tools/sage"
$ours = New-Fake (Get-SageExePath $tools $SagePinnedVersion) "pinned sage"
$oursSha = Sha $ours
$other = New-Fake (Join-Path $sandbox "sagepath/sage.exe") "sage 0.15 from elsewhere"
$twin = New-Fake (Join-Path $sandbox "sagetwin/sage.exe") "pinned sage"
Check "a different sage on PATH is passed over for ours" `
    (Resolve-PinnedSage $tools $SagePinnedVersion $oursSha (Join-Path $sandbox "sagepath")) $ours
Check "an identical binary already on PATH is used as is" `
    (Resolve-PinnedSage $tools $SagePinnedVersion $oursSha "$(Join-Path $sandbox 'sagetwin');$(Join-Path $sandbox 'sagepath')") $twin
Check "the old Linux pin's hash does not match ours" `
    (Resolve-PinnedSage $tools $SagePinnedVersion "e3dc6b41015cb167574f6c82525b75e946c094f30bd700271b05c051c30cbe8a" "") ""
Remove-Item -LiteralPath $ours -Force
Check "no pinned copy anywhere: empty (so it is installed)" `
    (Resolve-PinnedSage $tools $SagePinnedVersion $oursSha (Join-Path $sandbox "sagepath")) ""

# -------------------------------------------- 12. installs, with the network stubbed
# A stub named like the cmdlet wins over it inside the loaded functions.
$script:iwrMode = "ok"
$script:iwrSource = ""
$script:iwrUris = New-Object System.Collections.Generic.List[string]
function Invoke-WebRequest {
    [CmdletBinding()]
    param($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec)
    $script:iwrUris.Add("$Uri")
    if ($script:iwrMode -eq "fail") { throw "simulated: could not resolve host" }
    Copy-Item -LiteralPath $script:iwrSource -Destination $OutFile -Force
}
$script:spCalls = New-Object System.Collections.Generic.List[object]
$script:msiExit = 0
function Start-Process {
    [CmdletBinding()]
    param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru, $Verb,
          [switch]$NoNewWindow, $RedirectStandardOutput, $RedirectStandardError)
    $script:spCalls.Add([pscustomobject]@{ FilePath = $FilePath; Args = ($ArgumentList -join " "); Verb = "$Verb" })
    if ($PassThru) { return [pscustomobject]@{ ExitCode = $script:msiExit } }
}

Write-Host ""
Write-Host "12. Install-PinnedSage (network stubbed)"
$stage = Join-Path $sandbox "stage"
$zipInner = Join-Path $stage "sage-v$SagePinnedVersion-x86_64-pc-windows-msvc"
New-Fake (Join-Path $zipInner "sage.exe") "pinned sage" | Out-Null
New-Fake (Join-Path $zipInner "LICENSE") "MIT" | Out-Null
$fakeZip = Join-Path $sandbox "fake-sage.zip"
Compress-Archive -Path $zipInner -DestinationPath $fakeZip -Force
$fakeZipSha = Sha $fakeZip
$tmpZip = Join-Path ([System.IO.Path]::GetTempPath()) (Get-SageZipName $SagePinnedVersion)

$script:iwrSource = $fakeZip
$toolsA = Join-Path $sandbox "toolsA"
$got = Install-PinnedSage $toolsA $SagePinnedVersion $fakeZipSha $oursSha
Check "installs and returns the unpacked exe" $got (Get-SageExePath $toolsA $SagePinnedVersion)
Check "fetched the pinned release URL" $script:iwrUris[$script:iwrUris.Count - 1] (Get-SageZipUrl $SagePinnedVersion)
Check "download removed afterwards" (Test-Path -LiteralPath $tmpZip) "False"

$toolsB = Join-Path $sandbox "toolsB"
$got = Install-PinnedSage $toolsB $SagePinnedVersion $SagePinnedZipSha256 $oursSha
Check "zip hash mismatch: refused" $got ""
Check "...and nothing unpacked" (Test-Path -LiteralPath $toolsB) "False"
Check "...and the download deleted" (Test-Path -LiteralPath $tmpZip) "False"

$toolsC = Join-Path $sandbox "toolsC"
$got = Install-PinnedSage $toolsC $SagePinnedVersion $fakeZipSha $SagePinnedExeSha256
Check "zip fine but exe is not the pinned binary: refused" $got ""

$script:iwrMode = "fail"
$got = Install-PinnedSage (Join-Path $sandbox "toolsD") $SagePinnedVersion $fakeZipSha $oursSha
Check "network failure: empty, no throw" $got ""
$script:iwrMode = "ok"

Write-Host ""
Write-Host "13. Install-PinnedDiann (network and msiexec stubbed)"
$fakeMsi = New-Fake (Join-Path $sandbox "fake.msi") "not really an msi"
$fakeMsiSha = Sha $fakeMsi
$tmpMsi = Join-Path ([System.IO.Path]::GetTempPath()) $DiannPinnedMsiName
$script:iwrSource = $fakeMsi
$marker = Get-DiannMsiMarkerPath (Join-Path $sandbox "profile13")
Check "marker lives under %USERPROFILE%\STAN" $marker (Join-Path (Join-Path (Join-Path $sandbox "profile13") "STAN") "diann_msi_needs_admin.txt")

$script:spCalls.Clear(); $script:msiExit = 0
$ok = Install-PinnedDiann $DiannPinnedMsiName $fakeMsiSha $true ""
Check "checksum ok, silent install: returns exactly True" "$ok" "True"
Check "fetched the pinned MSI URL" $script:iwrUris[$script:iwrUris.Count - 1] (Get-DiannMsiUrl $DiannPinnedMsiName)
Check "msiexec ran once" $script:spCalls.Count 1
if ($script:spCalls.Count -gt 0) {
    Check "...quietly, on the pinned MSI" `
        ($script:spCalls[0].FilePath -eq "msiexec.exe" -and $script:spCalls[0].Args -match ('^/i ".*{0}" /quiet /norestart$' -f [regex]::Escape($DiannPinnedMsiName))) "True"
}
Check "MSI deleted afterwards" (Test-Path -LiteralPath $tmpMsi) "False"

# The interactive installer: the operator is there, so an admin prompt is
# allowed after the silent install fails.
$script:spCalls.Clear(); $script:msiExit = 1603
$ok = Install-PinnedDiann $DiannPinnedMsiName $fakeMsiSha $true ""
Check "installer, silent install fails: retried elevated" $script:spCalls.Count 2
if ($script:spCalls.Count -gt 1) { Check "...with RunAs and /passive" "$($script:spCalls[1].Verb) $($script:spCalls[1].Args -match '/passive')" "RunAs True" }
Check "...and still returns exactly True" "$ok" "True"

# The updater: nobody may be there, so no prompt, and the failure is kept.
$script:spCalls.Clear(); $script:msiExit = 1603
$ok = Install-PinnedDiann $DiannPinnedMsiName $fakeMsiSha $false $marker
$runAs = 0
foreach ($c in $script:spCalls) { if ($c.Verb -eq "RunAs") { $runAs++ } }
Check "updater, silent install fails: returns False" "$ok" "False"
Check "...msiexec ran once, never elevated" "$($script:spCalls.Count) $runAs" "1 0"
Check "...the failure is remembered" (Test-DiannMsiMarker $marker $DiannPinnedMsiName) "True"
Check "...for this MSI only" (Test-DiannMsiMarker $marker "DIA-NN-2.3.0-Academia-Preview.msi") "False"
Check "...and the MSI deleted" (Test-Path -LiteralPath $tmpMsi) "False"
Check "no marker file: not remembered" (Test-DiannMsiMarker (Join-Path $sandbox "none.txt") $DiannPinnedMsiName) "False"
Check "empty marker path: not remembered" (Test-DiannMsiMarker "" $DiannPinnedMsiName) "False"

# 1618: another installation was running. Worth retrying next time.
Remove-Item -LiteralPath $marker -Force
$script:spCalls.Clear(); $script:msiExit = 1618
$ok = Install-PinnedDiann $DiannPinnedMsiName $fakeMsiSha $false $marker
Check "1618 (another install running): False, not remembered" "$ok $(Test-Path -LiteralPath $marker)" "False False"

# 3010 / 1641: installed, reboot wanted. A success, not a reason to elevate.
foreach ($code in @(3010, 1641)) {
    $script:spCalls.Clear(); $script:msiExit = $code
    $ok = Install-PinnedDiann $DiannPinnedMsiName $fakeMsiSha $true ""
    Check "$code (reboot wanted) is success, no retry" "$ok $($script:spCalls.Count)" "True 1"
}

$script:spCalls.Clear(); $script:msiExit = 0
$ok = Install-PinnedDiann $DiannPinnedMsiName $DiannPinnedMsiSha256 $true ""
Check "MSI hash mismatch: returns False" "$ok" "False"
Check "...and msiexec never runs" $script:spCalls.Count 0
Check "...and the MSI is deleted" (Test-Path -LiteralPath $tmpMsi) "False"

$script:spCalls.Clear()
$script:iwrMode = "fail"
$ok = Install-PinnedDiann $DiannPinnedMsiName $fakeMsiSha $false $marker
Check "network failure: False, msiexec never runs, not remembered" "$ok $($script:spCalls.Count) $(Test-Path -LiteralPath $marker)" "False 0 False"
$script:iwrMode = "ok"

Write-Host ""
Write-Host "13a. what the operator is told when 'stan baseline' would use another DIA-NN"
$pick27 = Join-Path $sandbox "lad/DIA-NN/2.7.0/DiaNN.exe"
$chose232 = Join-Path $sandbox "lad/DIA-NN/2.3.2/DiaNN.exe"
$w = @(Get-BaselineDiannWarning $pick27 $chose232)
Check "different binaries: a warning" ($w.Count -gt 0) "True"
Check "...naming both, and the version it would search with" `
    ((($w -join "`n") -match [regex]::Escape($pick27)) -and (($w -join "`n") -match [regex]::Escape($chose232)) -and (($w -join "`n") -match "\(2\.7\.0\)")) "True"
Check "...and the prompt to answer n to" (($w -join " ") -match "Submit results to community") "True"
Check "same binary: nothing" (@(Get-BaselineDiannWarning $chose232 $chose232).Count) 0
Check "same binary, different case: nothing" (@(Get-BaselineDiannWarning $chose232.ToUpperInvariant() $chose232).Count) 0
Check "pick unknown: nothing" (@(Get-BaselineDiannWarning "" $chose232).Count) 0
Check "no python: empty pick" (Get-BaselineDiannPick "") ""
Check "missing python: empty pick" (Get-BaselineDiannPick (Join-Path $sandbox "no-python")) ""
$fakePy = New-FakeProgram (Join-Path $sandbox "py13a/fakepy") @("some import noise", $pick27)
Check "pick is the last line the probe prints" (Get-BaselineDiannPick $fakePy) $pick27

# ------------------------------ 13b. the real step code, end to end, stubbed
# The functions above can all be right while the top-level glue that calls
# them is wrong -- a misspelt variable is silently $null in PowerShell. So
# run the actual DIA-NN and Sage steps of each script, dot-sourced under
# StrictMode (an unset variable then throws), against a fake PC that has
# only DIA-NN 2.7.0. msiexec is a stub: on an "admin" PC its silent install
# puts 2.3.2 into the fake Program Files; on a "user" PC the silent install
# fails with 1603 and only an elevated (RunAs) run installs. Downloads come
# from the fakes built above, and the venv python is a stub that answers
# the baseline probe the way stan/baseline.py does today: with the highest
# version on disk, 2.7.0. Off Windows the registry PATH reads as empty and
# writes to it are ignored.
Write-Host ""
Write-Host "13b. the scripts' own DIA-NN + Sage steps, end to end (stubbed PC)"
$savedPath = $env:Path
$savedProfile = $env:USERPROFILE
$script:fakeMsi = $fakeMsi
$script:fakeZip = $fakeZip
function Start-Process {
    [CmdletBinding()]
    param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru, $Verb,
          [switch]$NoNewWindow, $RedirectStandardOutput, $RedirectStandardError)
    $code = 0
    if ($FilePath -eq "msiexec.exe") {
        $script:msiCalls++
        $elevated = ("$Verb" -eq "RunAs")
        if ($elevated) { $script:runAsCalls++ }
        if (($script:pcKind -eq "admin") -or $elevated) {
            New-Fake (Join-Path $script:pcPf "2.3.2/DiaNN.exe") "2.3.2" | Out-Null
        } else {
            $code = 1603
        }
    }
    if ($PassThru) { return [pscustomobject]@{ ExitCode = $code } }
}
function Invoke-WebRequest {
    [CmdletBinding()]
    param($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec)
    if ("$Uri" -like "*.msi") { $script:msiDownloads++; Copy-Item -LiteralPath $script:fakeMsi -Destination $OutFile -Force; return }
    Copy-Item -LiteralPath $script:fakeZip -Destination $OutFile -Force
}
function Get-DiannSearchRoots { return @($script:pcPf) }

# Run one step's code in the caller's scope, so the variables it sets can be
# checked afterwards; returns what it printed and whether it threw. Call it
# dot-sourced (". Invoke-Step $code"), and keep its own names unusual: they
# land in the caller's scope too.
function Invoke-Step($stepCodeText) {
    $stepResult = [pscustomobject]@{ Threw = ""; Out = "" }
    Set-StrictMode -Version 2.0
    try {
        $stepOutput = . ([scriptblock]::Create($stepCodeText)) 6>&1
        $stepResult.Out = ($stepOutput -join "`n")
    } catch {
        $stepResult.Threw = "$_"
    }
    Set-StrictMode -Off
    return $stepResult
}

$segments = @(
    @("install_stan.ps1", "# -- DIA-NN, pinned to the 2.3 line", "# -- instruments.yml --", "sageBinPath"),
    @("update_stan.ps1", "# -- Check DIA-NN, pinned to the 2.3 line", "# -- Self-update bat files --", "sagePath"))
foreach ($kind in @("admin", "user")) {
    foreach ($seg in $segments) {
        $rel = $seg[0]
        $isUpdate = ($rel -eq "update_stan.ps1")
        $code = Get-Segment $rel $seg[1] $seg[2]
        Check "$rel step code located" ($code.Length -gt 500) "True"
        $pc = Join-Path $sandbox "pc_$($kind)_$($rel.Split('.')[0])"
        $pcPf = Join-Path $pc "Program Files/DIA-NN"
        $old = New-Fake (Join-Path $pcPf "2.7.0/DiaNN.exe") "2.7.0"
        $env:USERPROFILE = Join-Path $pc "home"
        $script:pcPf = $pcPf
        $script:pcKind = $kind
        $script:msiCalls = 0
        $script:runAsCalls = 0
        $script:msiDownloads = 0
        $venvPython = New-FakeProgram (Join-Path $pc "venv/fakepy") @($old)
        # The pins the step code reads, pointed at the fakes' hashes.
        $DiannPinnedVersion = "2.3.2"
        $DiannPinnedMsiName = "DIA-NN-2.3.2-Academia.msi"
        $DiannPinnedMsiSha256 = $fakeMsiSha
        $DiannCommunityExactVersion = "2.3.0"
        $SagePinnedVersion = "0.14.7"
        $SagePinnedZipSha256 = $fakeZipSha
        $SagePinnedExeSha256 = $oursSha
        $want232 = Join-Path $pcPf "2.3.2/DiaNN.exe"
        $markerPath = Get-DiannMsiMarkerPath $env:USERPROFILE
        $tag = "$rel on a $kind PC"

        if (($kind -eq "user") -and $isUpdate) {
            # Unattended update, no admin rights: never a prompt, and the
            # 250 MB download happens once, not on every update.
            $r = . Invoke-Step $code
            $ErrorActionPreference = "Stop"
            Check "$tag (1st update) runs clean under StrictMode" $r.Threw ""
            Check "$tag (1st update): one silent msiexec, no admin prompt" "$($script:msiCalls) $($script:runAsCalls)" "1 0"
            Check "$tag (1st update): nothing compatible yet" ($null -eq $diannBest) "True"
            Check "$tag (1st update): failure remembered" (Test-DiannMsiMarker $markerPath $DiannPinnedMsiName) "True"
            Check "$tag (1st update): prints the MSI to install by hand" ($r.Out -match [regex]::Escape((Get-DiannMsiUrl $DiannPinnedMsiName))) "True"
            $r = . Invoke-Step $code
            $ErrorActionPreference = "Stop"
            Check "$tag (2nd update) runs clean" $r.Threw ""
            Check "$tag (2nd update): MSI not downloaded again, msiexec not run" "$($script:msiDownloads) $($script:msiCalls)" "1 1"
            Check "$tag (2nd update): still says what to do" ($r.Out -match [regex]::Escape((Get-DiannMsiUrl $DiannPinnedMsiName))) "True"
            # Someone installs the MSI by hand, then updates again.
            New-Fake $want232 "2.3.2" | Out-Null
            $r = . Invoke-Step $code
            $ErrorActionPreference = "Stop"
            Check "$tag (after a hand install) runs clean" $r.Threw ""
            Check "$tag (after a hand install): picks 2.3.2" "$($diannBest.Path)" $want232
            Check "$tag (after a hand install): marker cleared" (Test-Path -LiteralPath $markerPath) "False"
            Check "${tag}: never an admin prompt, over three updates" $script:runAsCalls 0
        } else {
            foreach ($pass in @("first run", "re-run")) {
                $r = . Invoke-Step $code
                $ErrorActionPreference = "Stop"
                Check "$tag ($pass) runs clean under StrictMode" $r.Threw ""
                Check "$tag ($pass) picked DIA-NN 2.3.2, not the 2.7.0 already there" "$($diannBest.Version)" "2.3.2"
                Check "$tag ($pass) left 2.7.0 in place" (Test-Path -LiteralPath $old) "True"
                $sageChosen = Get-Variable -Name $seg[3] -ValueOnly
                Check "$tag ($pass) chose the pinned sage.exe" $sageChosen (Get-SageExePath (Join-Path $env:USERPROFILE "STAN\tools\sage") "0.14.7")
                # stan baseline would still search with 2.7.0: say so.
                $warnText = @($baselineWarning) -join "`n"
                Check "$tag ($pass) warns that stan baseline would use 2.7.0" `
                    (($warnText -match [regex]::Escape($old)) -and ($r.Out -match "does not take DIA-NN from PATH")) "True"
                Check "$tag ($pass) no longer says 2.3.2 rows are unverified" ($r.Out -match "asset-verified only when") "False"
            }
            Check "${tag}: msiexec installs once over both passes" ($script:msiCalls -ge 1 -and $script:msiDownloads -eq 1) "True"
            if ($kind -eq "user") {
                Check "${tag}: the interactive installer may ask for admin rights" $script:runAsCalls 1
            } else {
                Check "${tag}: no admin prompt needed" $script:runAsCalls 0
            }
            Check "${tag}: nothing remembered as failed" (Test-Path -LiteralPath $markerPath) "False"
            # Once stan baseline picks the same binary (a fixed baseline.py,
            # or 2.7.0 uninstalled), the warning goes away by itself.
            $venvPython = New-FakeProgram (Join-Path $pc "venv/fakepy") @($want232)
            $r = . Invoke-Step $code
            $ErrorActionPreference = "Stop"
            Check "${tag}: no warning once baseline picks 2.3.2 too" "$(@($baselineWarning).Count) $($r.Threw)" "0 "
        }
        $env:Path = $savedPath
    }
}
$env:USERPROFILE = $savedProfile
$env:Path = $savedPath

# ---------------------------------------- 13c. instruments.yml, for real files
# Nothing reads diann_binary / sage_binary, so the step no longer writes
# them. It used to rewrite the file with Out-File -Encoding utf8, whose BOM
# Python on Windows (ANSI code page) cannot parse. Now it creates a plain
# ASCII skeleton only when STAN reads no instruments.yml at all, and leaves
# an existing one alone apart from dropping such a BOM.
Write-Host ""
Write-Host "13c. instruments.yml: created only when missing, never with a BOM"
$python3 = $null
foreach ($n in @("python3", "python")) {
    if (-not $python3) {
        $cmd = Get-Command $n -ErrorAction SilentlyContinue
        if ($cmd) { $python3 = $cmd.Source }
    }
}
# Parse a file the way STAN on Windows reads it: yaml.safe_load over the
# bytes decoded in the ANSI code page (cp1252).
function Test-YamlAsWindowsReads($path) {
    if (-not $script:python3) { return "no-python" }
    $env:STAN_TEST_YML = $path
    $out = & $script:python3 -c "import os, yaml; print(repr(yaml.safe_load(open(os.environ['STAN_TEST_YML'], 'rb').read().decode('cp1252'))))" 2>&1
    $rc = $LASTEXITCODE
    Remove-Item Env:STAN_TEST_YML -ErrorAction SilentlyContinue
    if ($rc -ne 0) { return "error" }
    return "$out".Trim()
}
$code6 = Get-Segment "install_stan.ps1" "# -- instruments.yml --" "# -- PATH --"
Check "install step 6-7 code located" ($code6.Length -gt 300) "True"
$fakeStanDir = Join-Path $sandbox "stanexe"
foreach ($case in @("fresh", "primary", "legacy-bom")) {
    $prof = Join-Path $sandbox "yml_$case"
    New-Item -ItemType Directory -Path $prof -Force | Out-Null
    $env:USERPROFILE = $prof
    $initFlag = Join-Path $prof "init-called"
    $stanExe = New-FakeProgram (Join-Path $fakeStanDir "stan_$case") @("init ran")
    $primaryYml = Join-Path (Join-Path $prof "STAN") "instruments.yml"
    $legacyYml = Join-Path (Join-Path $prof ".stan") "instruments.yml"
    $before = $null
    if ($case -eq "primary") {
        New-Item -ItemType Directory -Path (Split-Path -Parent $primaryYml) -Force | Out-Null
        [System.IO.File]::WriteAllText($primaryYml, "instruments:`n- name: Astral`n  vendor: thermo`ndiann_binary: C:/old/DiaNN.exe`n")
        $before = Sha $primaryYml
    }
    if ($case -eq "legacy-bom") {
        New-Item -ItemType Directory -Path (Split-Path -Parent $legacyYml) -Force | Out-Null
        $body = [System.Text.Encoding]::ASCII.GetBytes("# STAN instrument configuration`ninstruments: []`n`ndiann_binary: `"C:/x/DiaNN.exe`"")
        $bom = [byte[]](0xEF, 0xBB, 0xBF)
        [System.IO.File]::WriteAllBytes($legacyYml, [byte[]]($bom + $body))
        Check "legacy-bom: the old file does not parse as Windows reads it" (Test-YamlAsWindowsReads $legacyYml) "error"
    }
    $r = . Invoke-Step $code6
    $ErrorActionPreference = "Stop"
    Check "${case}: step 6-7 runs clean under StrictMode" $r.Threw ""
    Check "${case}: stan init not run (the file exists)" ($r.Out -match "init ran") "False"
    if ($case -eq "fresh") {
        Check "fresh: skeleton written where the wrappers expect it (.stan)" $instrYml $legacyYml
        Check "fresh: ...and nothing in STAN\ to shadow them" (Test-Path -LiteralPath $primaryYml) "False"
        $bytes = [System.IO.File]::ReadAllBytes($legacyYml)
        $high = 0
        foreach ($b in $bytes) { if ($b -gt 127) { $high++ } }
        Check "fresh: plain ASCII, so no BOM" $high 0
        $text = [System.IO.File]::ReadAllText($legacyYml)
        Check "fresh: an empty instrument list" ($text -match "(?m)^instruments: \[\]") "True"
        Check "fresh: no keys nothing reads" ($text -match "diann_binary|sage_binary") "False"
        $parsed = Test-YamlAsWindowsReads $legacyYml
        if ($parsed -eq "no-python") { Skip "fresh: parses as Windows reads it" "no python3" }
        else { Check "fresh: parses as Windows reads it" $parsed "{'instruments': []}" }
    }
    if ($case -eq "primary") {
        Check "primary: STAN\instruments.yml is the one used" $instrYml $primaryYml
        Check "primary: left byte for byte as it was" (Sha $primaryYml) $before
        Check "primary: no legacy file created" (Test-Path -LiteralPath $legacyYml) "False"
    }
    if ($case -eq "legacy-bom") {
        Check "legacy-bom: the legacy file is the one used" $instrYml $legacyYml
        $bytes = [System.IO.File]::ReadAllBytes($legacyYml)
        Check "legacy-bom: BOM gone" ($bytes[0] -ne 0xEF) "True"
        Check "legacy-bom: the rest untouched" ([System.Text.Encoding]::ASCII.GetString($bytes)) ([System.Text.Encoding]::ASCII.GetString($body))
        $parsed = Test-YamlAsWindowsReads $legacyYml
        if ($parsed -eq "no-python") { Skip "legacy-bom: parses as Windows reads it" "no python3" }
        else { Check "legacy-bom: now parses as Windows reads it" ($parsed -match "'instruments': \[\]") "True" }
    }
}
Check "Remove-Utf8Bom on a missing file: False" (Remove-Utf8Bom (Join-Path $sandbox "none.yml")) "False"
$short = New-Fake (Join-Path $sandbox "short.yml") "ab"
Check "Remove-Utf8Bom on a 2-byte file: False, untouched" "$(Remove-Utf8Bom $short) $([System.IO.File]::ReadAllText($short))" "False ab"

# The updater decides whether to install alphatims from the instruments.yml
# STAN reads, which on a PC this installer set up may be the legacy one.
$codeB = Get-Segment "update_stan.ps1" '$hasBruker = $false' 'if ($hasBruker) {'
Check "updater Bruker check located" ($codeB.Length -gt 100) "True"
$prof = Join-Path $sandbox "yml_bruker"
$legacyYml = Join-Path (Join-Path $prof ".stan") "instruments.yml"
New-Item -ItemType Directory -Path (Split-Path -Parent $legacyYml) -Force | Out-Null
[System.IO.File]::WriteAllBytes($legacyYml, [byte[]]([byte[]](0xEF, 0xBB, 0xBF) + [System.Text.Encoding]::ASCII.GetBytes("instruments:`n- name: timsTOF`n  vendor: bruker`n")))
$env:USERPROFILE = $prof
$r = . Invoke-Step $codeB
$ErrorActionPreference = "Stop"
Check "updater: Bruker check runs clean" $r.Threw ""
Check "updater: finds the Bruker in the legacy .stan\instruments.yml" $hasBruker "True"
Check "updater: and drops its BOM" ([System.IO.File]::ReadAllBytes($legacyYml)[0] -ne 0xEF) "True"
$env:USERPROFILE = $savedProfile

Check "Get-StanConfigFile: neither exists -> STAN\" (Get-StanConfigFile "x.yml" (Join-Path $sandbox "noprof")) (Join-Path (Join-Path (Join-Path $sandbox "noprof") "STAN") "x.yml")

# ------------------------------------------- 14. top-level wiring + house rules
Write-Host ""
Write-Host "14. both scripts use the pinned path, and nothing else"
function Get-TopLevelCommands($ast) {
    $cmds = @()
    foreach ($c in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $p = $c.Parent
        $inFunc = $false
        while ($p) {
            if ($p -is [System.Management.Automation.Language.FunctionDefinitionAst]) { $inFunc = $true }
            $p = $p.Parent
        }
        if (-not $inFunc) { $cmds += $c }
    }
    return ,$cmds
}
foreach ($pair in @(@("install", $instAst, '$true'), @("update", $updAst, '$false'))) {
    $name = $pair[0]; $ast = $pair[1]; $t = $ast.Extent.Text
    $cmds = Get-TopLevelCommands $ast
    $calls = @()
    foreach ($c in $cmds) { $calls += $c.GetCommandName() }
    foreach ($fn in @("Find-DiannCandidates", "Select-CompatibleDiann", "Install-PinnedDiann",
                      "Resolve-PinnedSage", "Install-PinnedSage", "Use-EngineFirstOnPath",
                      "Get-BaselineDiannPick", "Get-BaselineDiannWarning", "Get-StanConfigFile",
                      "Remove-Utf8Bom")) {
        Check "$name calls $fn" ($calls -contains $fn) "True"
    }
    # Install-PinnedDiann's third argument decides whether a UAC prompt may
    # appear: yes in the interactive installer, never in the updater.
    $elev = @()
    foreach ($c in $cmds) {
        if ($c.GetCommandName() -eq "Install-PinnedDiann") { $elev += $c.CommandElements[3].Extent.Text }
    }
    Check "$name calls Install-PinnedDiann with elevation $($pair[2]) only" ($elev -join ",") $pair[2]
    $selects = 0
    foreach ($c in $cmds) {
        if ($c.GetCommandName() -eq "Select-CompatibleDiann") {
            if (($c.CommandElements.Count -eq 4) -and ($c.CommandElements[3].Extent.Text -eq '$DiannCommunityExactVersion')) { $selects++ }
            else { $selects = -99 }
        }
    }
    Check "$name always prefers an installed 2.3.0" ($selects -gt 0) "True"
    $funcs = @()
    foreach ($f in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) { $funcs += $f.Name }
    Check "$name no longer has Select-LatestDiannMsi" ($funcs -contains "Select-LatestDiannMsi") "False"
    Check "$name never asks for releases/latest" ($t -match "releases/latest") "False"
    Check "$name never asks the GitHub API" ($t -match "api\.github\.com") "False"
    # Keys nothing reads may appear in comments, never in code.
    $deadKeyStrings = 0
    foreach ($s in $ast.FindAll({
        ($args[0] -is [System.Management.Automation.Language.StringConstantExpressionAst]) -or
        ($args[0] -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) }, $true)) {
        if ($s.Extent.Text -match "diann_binary|sage_binary") { $deadKeyStrings++ }
    }
    Check "$name writes no diann_binary / sage_binary" $deadKeyStrings 0
    $bomWriters = 0
    foreach ($c in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $cn = $c.GetCommandName()
        if (($cn -eq "Out-File") -or ($cn -eq "Set-Content") -or ($cn -eq "Add-Content")) {
            if ($c.Extent.Text -match "-Encoding\s+['""]?utf8") { $bomWriters++ }
        }
    }
    Check "$name has no -Encoding utf8 writer (5.1 adds a BOM)" $bomWriters 0
}
$updNames = @()
foreach ($c in (Get-TopLevelCommands $updAst)) { $updNames += $c.GetCommandName() }
Check "the updater consults the failed-install memory" ($updNames -contains "Test-DiannMsiMarker") "True"

Write-Host ""
Write-Host "15. PowerShell 5.1 house rules"
foreach ($rel in @("install_stan.ps1", "update_stan.ps1")) {
    $bytes = [System.IO.File]::ReadAllBytes((Join-Path $repoRoot $rel))
    $nonAscii = 0
    foreach ($b in $bytes) { if ($b -gt 127) { $nonAscii++ } }
    # 5.1 reads a BOM-less script as ANSI: a stray UTF-8 dash or quote
    # becomes mojibake, or worse, a quote character to the parser.
    Check "$rel is pure ASCII" $nonAscii 0
    $ast = Load-Script $rel
    $t = $ast.Extent.Text
    Check "$rel has no Where-Object pipeline" ($t -match "\|\s*Where-Object") "False"
    Check "$rel has no inline `$(if ...)" ($t -match '\$\(\s*if\b') "False"
    $plus = @($ast.FindAll({
        $n = $args[0]
        ($n -is [System.Management.Automation.Language.BinaryExpressionAst]) -and
        ($n.Operator -eq "Plus") -and (
            ($n.Left -is [System.Management.Automation.Language.StringConstantExpressionAst]) -or
            ($n.Left -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) -or
            ($n.Right -is [System.Management.Automation.Language.StringConstantExpressionAst]) -or
            ($n.Right -is [System.Management.Automation.Language.ExpandableStringExpressionAst])) }, $true))
    Check "$rel has no '+' string concatenation" $plus.Count 0
    # Syntax pwsh 7 accepts and Windows PowerShell 5.1 does not.
    $ps7 = @($ast.FindAll({
        $n = $args[0]
        ($n.GetType().Name -eq "TernaryExpressionAst") -or ($n.GetType().Name -eq "PipelineChainAst") -or
        (($n -is [System.Management.Automation.Language.BinaryExpressionAst]) -and ("$($n.Operator)" -eq "QuestionQuestion")) }, $true))
    Check "$rel uses no pwsh-7-only syntax" $ps7.Count 0
}

# The backfill command was built with '+'; it is now joined. It must be the
# same command line, byte for byte.
$stepsRhs = ""
foreach ($a in $updAst.FindAll({ $args[0] -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
    if ($a.Left.Extent.Text -eq '$backfillSteps') { $stepsRhs = $a.Right.Extent.Text }
}
$steps = Invoke-Expression $stepsRhs
$expected = "title STAN overnight backfill && echo === stan install-4dff === && stan install-4dff && echo === stan fix-spds      === && stan fix-spds && echo === stan backfill-metrics === && stan backfill-metrics && echo === stan derive-cirt-panel --auto === && stan derive-cirt-panel --auto && echo === stan backfill-cirt    === && stan backfill-cirt && echo === stan backfill-tic --force --push === && stan backfill-tic --force --push && echo === stan backfill-peg     === && stan backfill-peg && echo === stan backfill-features === && stan backfill-features && echo === stan backfill-window-drift --force === && stan backfill-window-drift --force && echo ALL BACKFILLS COMPLETE && pause"
Check "backfill command line unchanged" (($steps -join " && ") -ceq $expected) "True"
Check "backfill command is built by -join" ($updAst.Extent.Text -match '\$backfillCmd = \$backfillSteps -join " && "') "True"

# ------------------------------------------------------------- 16. stan.bat
Write-Host ""
Write-Host "16. stan.bat fetches install-stan.bat when it is missing"
$batPath = Join-Path $repoRoot "stan.bat"
$batBytes = [System.IO.File]::ReadAllBytes($batPath)
$batText = [System.IO.File]::ReadAllText($batPath)
$nonAscii = 0
$bareLf = 0
for ($i = 0; $i -lt $batBytes.Length; $i++) {
    if ($batBytes[$i] -gt 127) { $nonAscii++ }
    if ($batBytes[$i] -eq 10 -and ($i -eq 0 -or $batBytes[$i - 1] -ne 13)) { $bareLf++ }
}
Check "stan.bat is pure ASCII" $nonAscii 0
# .gitattributes stores *.bat as-is so GitHub Raw serves CRLF; cmd.exe can
# fail to find a goto label in an LF-only batch file.
Check "stan.bat has CRLF line endings only" $bareLf 0

$batLines = $batText -split "`r`n"
$code = @()
foreach ($l in $batLines) { if ($l -notmatch '^\s*(REM\b|rem\b|::)') { $code += $l } }
$codeText = $code -join "`n"
# cmd expands percent signs in a REM line before it sees the REM.
$remPct = 0
foreach ($l in $batLines) { if (($l -match '^\s*REM\b') -and ($l -match '[%<>|&]')) { $remPct++ } }
Check "no percent or redirection characters in REM lines" $remPct 0

$iNext = $codeText.IndexOf('set "INSTALLER=%~dp0install-stan.bat"')
$iDown = $codeText.IndexOf('set "INSTALLER=%USERPROFILE%\Downloads\install-stan.bat"')
$iFetch = $codeText.IndexOf('STAN_INSTALLER_URL=')
Check "looks next to stan.bat, then Downloads, then downloads" (($iNext -ge 0) -and ($iNext -lt $iDown) -and ($iDown -lt $iFetch)) "True"
$url = ""
if ($codeText -match 'set "STAN_INSTALLER_URL=([^"]+)"') { $url = $Matches[1] }
Check "download URL" $url "https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat"
Check "saved next to stan.bat" ($codeText -match 'set "STAN_INSTALLER_DEST=%INSTALLER%"') "True"
Check "no more dead-end 'not found' error" ($codeText -match "install-stan\.bat not found next to stan\.bat") "False"

# Every goto has a label to land on.
$labels = @{}
foreach ($l in $code) { if ($l -match '^\s*:([A-Za-z_][A-Za-z0-9_]*)\s*$') { $labels[$Matches[1]] = $true } }
$missing = @()
foreach ($l in $code) {
    foreach ($m in [regex]::Matches($l, '(?i)\bgoto\s+:?([A-Za-z_][A-Za-z0-9_]*)')) {
        if (-not $labels.ContainsKey($m.Groups[1].Value)) { $missing += $m.Groups[1].Value }
    }
}
Check "every goto has its label" ($missing -join ",") ""

# The script's own folder must stay out of ( ) blocks: "STAN (1)\" closes one.
# The depth of each line is kept for the DIA-NN check below.
$depth = 0
$dp0InBlock = 0
$lineDepth = @{}
for ($k = 0; $k -lt $code.Count; $k++) {
    $l = $code[$k]
    $t = $l.Trim()
    if ($t.StartsWith(")")) { $depth-- }
    $lineDepth[$k] = $depth
    if ($depth -gt 0 -and $l -match '%~dp0') { $dp0InBlock++ }
    if ($t.EndsWith("(")) { $depth++ }
}
Check "no %~dp0 inside a ( ) block" $dp0InBlock 0
Check "post-update banner uses 'stan version'" ($codeText -match '"%STAN_EXE%" version 2\^>nul') "True"
Check "no 'stan --version' (not an option)" ($codeText -match '"%STAN_EXE%" --version') "False"

# Pull the PowerShell one-liner out of stan.bat and run it in a child pwsh
# with Invoke-WebRequest stubbed: success, network failure, and a proxy page.
$dlLine = ""
foreach ($l in $code) { if ($l -match '^powershell .*STAN_INSTALLER_DEST') { $dlLine = $l } }
$dlCmd = ""
if ($dlLine -match '-Command "(.*)"\s*$') { $dlCmd = $Matches[1] }
Check "found the download one-liner" ($dlCmd.Length -gt 50) "True"
# cmd rewrites %, and ! under enabledelayedexpansion, before PowerShell
# sees anything; a " would end the argument. None may appear.
Check "one-liner is cmd-safe (no % ! or quote)" ($dlCmd -match '[%!"]') "False"
Check "one-liner parses as PowerShell" `
    ([System.Management.Automation.Language.Parser]::ParseInput($dlCmd, [ref]$null, [ref]$null) -ne $null) "True"
Check "the real install-stan.bat passes its content check" `
    ([System.IO.File]::ReadAllText((Join-Path $repoRoot "install-stan.bat")) -match 'install_stan\.ps1') "True"

$pwshExe = (Get-Process -Id $PID).Path
$stubIwr = @'
function Invoke-WebRequest {
    [CmdletBinding()]
    param($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec)
    if ($env:STAN_TEST_IWR -eq "ok") { Set-Content -LiteralPath $OutFile -Value "REM from $Uri`r`npowershell ... install_stan.ps1 ..." ; return }
    if ($env:STAN_TEST_IWR -eq "html") { Set-Content -LiteralPath $OutFile -Value "<html>Please sign in</html>" ; return }
    throw "simulated: No such host is known."
}
'@
$dlDir = Join-Path $sandbox "dl"
New-Item -ItemType Directory -Path $dlDir -Force | Out-Null
$env:STAN_INSTALLER_URL = $url
$env:STAN_INSTALLER_DEST = Join-Path $dlDir "install-stan.bat"
foreach ($case in @(@("ok", 0, "True"), @("fail", 1, "False"), @("html", 2, "False"))) {
    Remove-Item -LiteralPath $env:STAN_INSTALLER_DEST -Force -ErrorAction SilentlyContinue
    $env:STAN_TEST_IWR = $case[0]
    $out = & $pwshExe -NoProfile -NonInteractive -Command "$stubIwr`n$dlCmd" 2>&1
    $rc = $LASTEXITCODE
    Check "download '$($case[0])': exit $($case[1])" $rc $case[1]
    Check "download '$($case[0])': file left behind = $($case[2])" (Test-Path -LiteralPath $env:STAN_INSTALLER_DEST) $case[2]
    if ($case[0] -eq "ok") {
        Check "fetched the URL stan.bat names" ((Get-Content -LiteralPath $env:STAN_INSTALLER_DEST -Raw) -match [regex]::Escape($url)) "True"
    }
    if ($case[0] -eq "fail") {
        Check "the reason is printed" (($out -join " ") -match "No such host") "True"
    }
    if ($case[0] -eq "html") {
        Check "a proxy page is explained" (($out -join " ") -match "proxy or sign-in page") "True"
    }
}
Remove-Item Env:STAN_TEST_IWR, Env:STAN_INSTALLER_URL, Env:STAN_INSTALLER_DEST -ErrorAction SilentlyContinue

# After the install, the same window re-reads PATH so the first watcher
# finds the engines. Off Windows the registry targets read as empty, which
# exercises the "print nothing, leave PATH alone" guard.
$pathLine = ""
foreach ($l in $code) { if ($l -match '^for /f "usebackq delims=" %%P in') { $pathLine = $l } }
$pathCmd = ""
if ($pathLine -match '-Command "(.*)"`\) do set "PATH=%%P"\s*$') { $pathCmd = $Matches[1] }
Check "found the PATH refresh after install" ($pathCmd.Length -gt 20) "True"
Check "refresh one-liner is cmd-safe" ($pathCmd -match '[%!"`]') "False"
Check "refresh comes after the installer call" ($codeText.IndexOf($pathLine) -gt $codeText.IndexOf('call "%INSTALLER%"')) "True"
$refreshOut = & $pwshExe -NoProfile -NonInteractive -Command $pathCmd 2>&1
$refreshRc = $LASTEXITCODE
Check "refresh exits 0" $refreshRc 0
if ($IsWindows) {
    Check "refresh prints the logon PATH" ("$refreshOut".Length -gt 0) "True"
} else {
    Check "no registry: refresh prints nothing" "$refreshOut" ""
}

# The supervisor only ever pip-installs STAN, so a lab that uses stan.bat
# alone never ran the updater that applies the DIA-NN pin, and kept
# producing rows the relay rejects without a word. stan.bat now says so on
# every launch -- and must never try to fix it from the loop, which runs
# unattended (an MSI can need an administrator).
Write-Host ""
Write-Host "16b. stan.bat warns when the DIA-NN on PATH is not 2.3.x"
$lineSet = ""
if ($codeText -match '(?m)^set "STAN_DIANN_LINE=([^"]+)"') { $lineSet = $Matches[1] }
$pinnedLine = ((Get-Constant $instAst "DiannPinnedVersion")[0].Split(".")[0..1]) -join "."
Check "STAN_DIANN_LINE matches the installers' pin" $lineSet $pinnedLine
$ckIdx = -1
for ($k = 0; $k -lt $code.Count; $k++) { if ($code[$k] -match '^powershell .*STAN_DIANN_LINE') { $ckIdx = $k } }
Check "found the DIA-NN check" ($ckIdx -ge 0) "True"
$ckCmd = ""
if ($ckIdx -ge 0) {
    if ($code[$ckIdx] -match '-Command "(.*)"\s*$') { $ckCmd = $Matches[1] }
    Check "check sits outside every ( ) block" $lineDepth[$ckIdx] 0
}
$iRun = $codeText.IndexOf(":run")
$iCheck = $codeText.IndexOf('set "STAN_DIANN_LINE=')
$iLoop = $codeText.IndexOf(":loop")
Check "check runs on every launch, once, before the supervisor loop" (($iRun -ge 0) -and ($iRun -lt $iCheck) -and ($iCheck -lt $iLoop)) "True"
Check "check one-liner is cmd-safe (no % ! or quote)" ($ckCmd -match '[%!"]') "False"
$ckErrs = $null
$ckAst = [System.Management.Automation.Language.Parser]::ParseInput($ckCmd, [ref]$null, [ref]$ckErrs)
Check "check one-liner parses as PowerShell" "$(@($ckErrs).Count)" "0"
$ck7 = @($ckAst.FindAll({
    $n = $args[0]
    ($n.GetType().Name -eq "TernaryExpressionAst") -or ($n.GetType().Name -eq "PipelineChainAst") -or
    (($n -is [System.Management.Automation.Language.BinaryExpressionAst]) -and ("$($n.Operator)" -eq "QuestionQuestion")) }, $true))
Check "check one-liner uses no pwsh-7-only syntax" $ck7.Count 0
Check "the supervisor never runs msiexec" ($codeText -match "msiexec") "False"
Check "the supervisor never calls update-stan.bat" ($codeText -match '(?i)call\s+"[^"]*update-stan\.bat') "False"

# Run it in a child pwsh against fake PATHs. The folder name gives the
# version; a folder that names none makes it run the program for its header
# (a sh script here, so those cases are skipped on Windows).
$ck = Join-Path $sandbox "ck"
$ck27 = Join-Path $ck "Program Files/DIA-NN/2.7.0"
$ck232 = Join-Path $ck "Program Files/DIA-NN/2.3.2"
New-Fake (Join-Path $ck27 "DiaNN.exe") "x" | Out-Null
New-Fake (Join-Path $ck232 "DiaNN.exe") "x" | Out-Null
$ckCases = @(
    @("2.7.0 first on PATH", $ck27, "warn", "2.7.0"),
    @("2.3.2 first on PATH", "$ck232", "quiet", ""),
    @("2.3.2 ahead of 2.7.0", "$ck232$([System.IO.Path]::PathSeparator)$ck27", "quiet", ""),
    @("no DIA-NN at all", (Join-Path $ck "empty"), "quiet", ""))
if (-not $onWindows) {
    $b25 = Join-Path $ck "bare25"
    $b23 = Join-Path $ck "bare23"
    $null = New-FakeProgram (Join-Path $b25 "DiaNN.exe") @("DIA-NN 2.5.1 Academia  (Data-Independent Acquisition by Neural Networks)")
    $null = New-FakeProgram (Join-Path $b23 "DiaNN.exe") @("DIA-NN 2.3.0 Academia")
    $ckCases += ,@("unnamed folder, header says 2.5.1", $b25, "warn", "2.5.1")
    $ckCases += ,@("unnamed folder, header says 2.3.0", $b23, "quiet", "")
} else {
    Skip "header-only DIA-NN cases" "they use sh stand-ins"
}
$savedEnvPath = $env:PATH
$env:STAN_DIANN_LINE = $lineSet
foreach ($case in $ckCases) {
    $env:PATH = "$($case[1])$([System.IO.Path]::PathSeparator)$savedEnvPath"
    $out = & $pwshExe -NoProfile -NonInteractive -Command $ckCmd 2>&1
    $rc = $LASTEXITCODE
    $env:PATH = $savedEnvPath
    $txt = ($out -join "`n")
    Check "check, $($case[0]): exit 0 (never stops the launch)" $rc 0
    if ($case[2] -eq "warn") {
        Check "check, $($case[0]): warns, naming $($case[3])" ($txt -match ("WARNING: the watcher runs DIA-NN {0}" -f [regex]::Escape($case[3]))) "True"
        Check "check, $($case[0]): says to run update-stan.bat" ($txt -match "Run update-stan\.bat once") "True"
    } else {
        Check "check, $($case[0]): says nothing" $txt.Trim() ""
    }
}
Remove-Item Env:STAN_DIANN_LINE -ErrorAction SilentlyContinue

# ------------------- 17. stan baseline's own pick, asked of the real baseline.py
# The installer asks the installed STAN which DiaNN.exe `stan baseline`
# would use. Here the same probe runs against this checkout's
# stan/baseline.py with a PC laid out the way the installer leaves it:
# 2.7.0 kept, 2.3.2 installed beside it. Whatever _find_diann() answers --
# 2.7.0 today -- the warning must match it.
Write-Host ""
Write-Host "17. the baseline probe against the real stan/baseline.py"
if (-not $python3) {
    Skip "baseline probe" "no python3"
} else {
    $lad = Join-Path $sandbox "lad17"
    $b27 = New-Fake (Join-Path $lad "DIA-NN/2.7.0/DiaNN.exe") "x"
    $b232 = New-Fake (Join-Path $lad "DIA-NN/2.3.2/DiaNN.exe") "x"
    $savedLad = $env:LOCALAPPDATA
    $savedPyPath = $env:PYTHONPATH
    $env:LOCALAPPDATA = $lad
    $env:PYTHONPATH = $repoRoot
    $null = & $python3 -c "import stan.baseline" 2>&1
    $importable = ($LASTEXITCODE -eq 0)
    if (-not $importable) {
        Skip "baseline probe" "python3 cannot import stan.baseline from this checkout"
    } else {
        $pick = Get-BaselineDiannPick $python3
        Check "the probe returns one of the two installs" (($pick -eq $b27) -or ($pick -eq $b232)) "True"
        Write-Host "       (stan/baseline.py picks: $pick)"
        $w = @(Get-BaselineDiannWarning $pick $b232)
        Check "a warning exactly when baseline would not use 2.3.2" ($w.Count -gt 0) ($pick -ne $b232)
        Remove-Item -LiteralPath (Split-Path -Parent $b27) -Recurse -Force
        $pick = Get-BaselineDiannPick $python3
        Check "2.3.2 alone: baseline picks it" $pick $b232
        Check "...and there is nothing to warn about" (@(Get-BaselineDiannWarning $pick $b232).Count) 0
    }
    $env:LOCALAPPDATA = $savedLad
    $env:PYTHONPATH = $savedPyPath
}

Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
if ($Failures -gt 0) { Write-Host "$Failures failure(s), $Skips skipped"; exit 1 }
Write-Host "all checks passed ($Skips skipped)"
exit 0
