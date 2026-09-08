# test_instrument_copy_scripts.ps1
#
# Tests for the two self-installing instrument copiers:
#   instrument/scripts/copy_evosep_logs.ps1
#   instrument/scripts/copy_bruker_backup.ps1
#
#     pwsh -NoProfile -File tests/test_instrument_copy_scripts.ps1
#
# Not part of the pytest suite (CI is Python-only) -- run it by hand after
# touching either script. There is no PowerShell on the dev Mac; a portable
# pwsh from the PowerShell/PowerShell release tarball runs it without
# installing anything.
#
# Like test_flinders_copy.ps1 it pulls the real definitions out of the
# shipped .ps1 through the AST, so the tests cannot drift from what ships,
# and it never executes either script's top-level pass.
#
# WHAT THIS IS GUARDING. Both feeds reached Hive only when someone ran a
# script by hand, and both stopped: Evosep on 2026-09-03, Bruker on
# 2026-09-01. The Evosep column panel then showed six-day-old numbers about
# a column that had already been replaced. The self-install is the fix, and
# the destination arithmetic below is the part that decides whether the Hive
# extractor can see the result at all.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Failures = 0

function Check($Label, $Got, $Want) {
    if ("$Got" -eq "$Want") { Write-Host "  ok   $Label" }
    else { Write-Host "  FAIL $Label -- got '$Got', want '$Want'"; $script:Failures++ }
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

# ---------------------------------------------------------------- 1. parse
Write-Host ""
Write-Host "1. both scripts parse"
$evAst = Load-Script "instrument/scripts/copy_evosep_logs.ps1"
$brAst = Load-Script "instrument/scripts/copy_bruker_backup.ps1"
Check "evosep parses"  ($evAst -ne $null) "True"
Check "bruker parses"  ($brAst -ne $null) "True"

# ------------------------------------------------- 2. self-install surface
# The whole point is that no flag has to be remembered, so the pieces that
# make that work must actually be present in what ships.
Write-Host ""
Write-Host "2. self-install machinery is present in both"
foreach ($pair in @(@("evosep", $evAst), @("bruker", $brAst))) {
    $name = $pair[0]; $ast = $pair[1]
    $funcs = @()
    foreach ($f in $ast.FindAll({
        $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        $funcs += $f.Name
    }
    Check "$name has Test-Task"          ($funcs -contains "Test-Task") "True"
    Check "$name has Test-Elevated"      ($funcs -contains "Test-Elevated") "True"
    Check "$name has Install-Task"       ($funcs -contains "Install-Task") "True"
    Check "$name has Remove-Task"        ($funcs -contains "Remove-Task") "True"
    Check "$name gates the key-press"    ($funcs -contains "Pause-IfInteractive") "True"

    # Count ReadKey through the AST, not the file text: the first version of
    # this test matched the word in a COMMENT explaining the gate and
    # reported two calls where there is one. Structure, not prose.
    $readKeys = @($ast.FindAll({
        $args[0] -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
        $args[0].Member.Value -eq "ReadKey" }, $true))
    Check "$name has exactly one ReadKey call" $readKeys.Count 1
    # ...and it must sit inside Pause-IfInteractive, which returns early when
    # -Scheduled. An ungated ReadKey holds the task open until the execution
    # time limit kills it, every tick, forever.
    $gate = $null
    foreach ($f in $ast.FindAll({
        $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        if ($f.Name -eq "Pause-IfInteractive") { $gate = $f }
    }
    $inGate = $false
    if ($gate -and $readKeys.Count -eq 1) {
        $rk = $readKeys[0].Extent.StartOffset
        if ($rk -gt $gate.Extent.StartOffset -and $rk -lt $gate.Extent.EndOffset) { $inGate = $true }
    }
    Check "$name ReadKey is inside the gate" $inGate "True"
    # Registration must be conditional on absence, or a manual run stomps the
    # task settings every time someone double-clicks it.
    Check "$name installs only when absent" `
        ($ast.Extent.Text -match "if \(Test-Task\)") "True"
}

# Param defaults come from the AST too, so a path asserted here is the path
# that actually ships rather than any string that happens to appear in a
# comment. The previous version of this file compared against over-escaped
# regexes: "old bruker_db path is gone" passed because the pattern matched
# nothing at all, which is the worst way for a test to be green.
function Get-ParamDefault($ast, $paramName) {
    $pb = $ast.ParamBlock
    if (-not $pb) { return "" }
    foreach ($p in $pb.Parameters) {
        if ($p.Name.VariablePath.UserPath -eq $paramName) {
            if ($p.DefaultValue) { return $p.DefaultValue.Extent.Text.Trim("'", '"') }
            return ""
        }
    }
    return ""
}

# ------------------------------------- 3. the Bruker destination, the bug
# cron_bruker_maintenance.sh does:
#     find /quobyte/proteomics-grp/brett/BrukerDBBackup -name '*.backup'
# and derives the snapshot date from the PARENT DIRECTORY NAME. The previous
# version of this script wrote to bruker_db/backup_<HOST>_<stamp>/ instead,
# which the extractor never reads -- so a scheduled copy would have looked
# entirely healthy while the panel stayed frozen.
Write-Host ""
Write-Host "3. bruker copies where the extractor actually looks"
$brOut = Get-ParamDefault $brAst "OutRoot"
$brUnc = Get-ParamDefault $brAst "OutRootUnc"
Check "OutRoot is BrukerDBBackup"      $brOut 'Y:\brett\BrukerDBBackup'
Check "UNC fallback matches it"        $brUnc '\\128.120.208.42\proteomics-grp\brett\BrukerDBBackup'
# The old destination must be gone from the CODE. It is still named in the
# docstring, deliberately, because a reader needs to know what changed.
$brCodeHasOld = $false
foreach ($sl in $brAst.FindAll({
    $args[0] -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)) {
    if ($sl.Value -match "bruker_db") { $brCodeHasOld = $true }
}
Check "no bruker_db in any code string" $brCodeHasOld "False"

# Relative-path arithmetic, as pure string ops so it is meaningful off
# Windows: D:\BrukerDBBackup\daily\2026-08-31_180000\compass.backup must
# arrive as daily\2026-08-31_180000\compass.backup, because the date the
# extractor reports comes from that middle segment.
$src = 'D:\BrukerDBBackup'
$full = 'D:\BrukerDBBackup\daily\2026-08-31_180000\compass.backup'
$rel = $full.Substring($src.Length).TrimStart('\')
Check "relative path keeps the layout" $rel 'daily\2026-08-31_180000\compass.backup'
$stampDir = ($rel -split '\\')[1]
Check "snapshot date survives"         $stampDir.Substring(0, 10) '2026-08-31'

# ------------------------------------------------ 4. Evosep mirror target
Write-Host ""
Write-Host "4. evosep still mirrors to the stable per-host path"
$evOut = Get-ParamDefault $evAst "OutRoot"
Check "evosep OutRoot unchanged"    $evOut 'Y:\brett\evosep_logs'
Check "mirror dir is <HOST>_mirror" `
    ($evAst.Extent.Text -match '\$\(\$env:COMPUTERNAME\)_mirror') "True"

# ------------------------------------------------------- 5. PS 5.1 rules
# CLAUDE.md: no Where-Object pipelines, no '+' string concatenation. Both
# have shipped as real bugs in this repo's PowerShell before.
Write-Host ""
Write-Host "5. PowerShell 5.1 house rules"
foreach ($pair in @(@("evosep", $evAst), @("bruker", $brAst))) {
    $name = $pair[0]; $t = $pair[1].Extent.Text
    Check "$name has no Where-Object pipeline" ($t -match "\|\s*Where-Object") "False"
}

# ------------------------------------------------- 6. version banners
# These files get copied onto instrument PCs and then live there alone, so
# "is the copy in front of me current?" has to be answerable without a git
# checkout. A banner that drifts from the package is worse than none -- it
# answers the question wrongly and confidently -- so the version is asserted
# equal to stan/__init__.py rather than merely present.
Write-Host ""
Write-Host "6. version banners match stan/__init__.py"
$initPath = Join-Path $repoRoot "stan/__init__.py"
$pkgVersion = ""
foreach ($line in (Get-Content -LiteralPath $initPath)) {
    if ($line -match '^__version__\s*=\s*"([^"]+)"') { $pkgVersion = $Matches[1] }
}
Check "found package version" ($pkgVersion -ne "") "True"

foreach ($pair in @(@("evosep", $evAst), @("bruker", $brAst))) {
    $name = $pair[0]; $ast = $pair[1]
    $scriptVersion = ""
    foreach ($a in $ast.FindAll({
        $args[0] -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($a.Left.Extent.Text -eq '$ScriptVersion') {
            $scriptVersion = $a.Right.Extent.Text.Trim("'", '"')
        }
    }
    Check "$name banner = $pkgVersion" $scriptVersion $pkgVersion
    # It must actually reach the operator, not just sit in a variable.
    Check "$name prints its version" `
        ($ast.Extent.Text -match 'v\$ScriptVersion') "True"
}

# ------------------- 7. the script accepts every flag it launches itself with
# THE BUG THIS EXISTS FOR. Install-Task re-launches the script elevated with
# -InstallOnly, and -InstallOnly was not a declared parameter -- it was read
# out of $MyInvocation.UnboundArguments. Under [CmdletBinding()] PowerShell
# rejects an unknown named parameter outright and never populates
# UnboundArguments, so the elevated child died on parameter binding, the task
# was never registered, and the operator saw "The task still is not there
# after elevating" on a real instrument.
#
# The structural checks above all passed while that was broken, because they
# never asked whether the script could actually be invoked the way it invokes
# itself. This asks exactly that.
Write-Host ""
Write-Host "7. self-relaunch flags are declared parameters"
# Flags belonging to powershell.exe itself, not to our script.
$hostFlags = @("-NoProfile", "-ExecutionPolicy", "-File", "-WindowStyle",
               "-Bypass", "-Hidden", "-Command", "-NoExit")
foreach ($pair in @(@("evosep", $evAst), @("bruker", $brAst))) {
    $name = $pair[0]; $ast = $pair[1]
    $declared = @()
    if ($ast.ParamBlock) {
        foreach ($p in $ast.ParamBlock.Parameters) { $declared += "-" + $p.Name.VariablePath.UserPath }
    }
    $missing = @()
    foreach ($sc in $ast.FindAll({
        $args[0] -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)) {
        $v = $sc.Value
        if ($v -match '^-[A-Za-z][A-Za-z0-9]*$') {
            if ($hostFlags -notcontains $v -and $declared -notcontains $v) { $missing += $v }
        }
    }
    $missing = @($missing | Sort-Object -Unique)
    Check "$name passes no undeclared flag to itself" ($missing -join ",") ""
    Check "$name declares -InstallOnly" ($declared -contains "-InstallOnly") "True"
    Check "$name declares -Scheduled"   ($declared -contains "-Scheduled") "True"
    Check "$name declares -Uninstall"   ($declared -contains "-Uninstall") "True"
}

# ------------------------------- 8. the installed copy gets refreshed
# The scheduled task runs the COPY under ProgramData taken when it was first
# registered. Updating the share changed nothing about what actually ran, so
# an installed task would have kept its original version forever -- the same
# silent-staleness shape as every other failure in this system.
Write-Host ""
Write-Host "8. a re-run refreshes a stale installed copy"
foreach ($pair in @(@("evosep", $evAst), @("bruker", $brAst))) {
    $name = $pair[0]; $ast = $pair[1]; $t = $ast.Extent.Text
    $funcs = @()
    foreach ($f in $ast.FindAll({
        $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        $funcs += $f.Name
    }
    Check "$name has Get-InstalledVersion" ($funcs -contains "Get-InstalledVersion") "True"
    Check "$name compares versions"        ($t -match '\$installedVersion -eq \$ScriptVersion') "True"

    # The pattern must not contain a bare "$ScriptVersion", because a
    # double-quoted PowerShell string interpolates it and the regex then
    # matches nothing -- Get-InstalledVersion silently answers "" forever and
    # the task reinstalls on every run. That shipped once; this pins it.
    $gv = $null
    foreach ($f in $ast.FindAll({
        $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        if ($f.Name -eq "Get-InstalledVersion") { $gv = $f }
    }
    $gvText = ""
    if ($gv) { $gvText = $gv.Extent.Text }
    Check "$name version regex does not interpolate" `
        ($gvText -match '-match "[^"]*\$ScriptVersion') "False"
}

# And prove the extraction actually works, rather than merely existing.
$verLine = ""
foreach ($line in (Get-Content -LiteralPath (Join-Path $repoRoot "instrument/scripts/copy_evosep_logs.ps1"))) {
    if ($line -match "ScriptVersion\s*=\s*'([^']+)'") { $verLine = $Matches[1]; break }
}
Check "extraction returns the real version" $verLine $pkgVersion

Write-Host ""
if ($Failures -gt 0) { Write-Host "$Failures failure(s)"; exit 1 }
Write-Host "all checks passed"
exit 0
