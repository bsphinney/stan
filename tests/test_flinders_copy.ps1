# test_flinders_copy.ps1
#
# Tests for scripts/flinders_copy.ps1, the timsTOF -> Flinders copier
# that runs as a scheduled task.
#
#     pwsh -NoProfile -File tests/test_flinders_copy.ps1
#
# Not part of the pytest suite (CI is Python-only) -- run it by hand
# after touching the script. There is no PowerShell on the dev Mac; a
# portable pwsh from the PowerShell/PowerShell release tarball runs it
# without installing anything.
#
# It pulls the real function definitions out of the shipped .ps1
# through the PowerShell AST, so the tests cannot drift from what
# ships, and it never executes the script's top-level pass.
#
# The month names in section 1 are the ACTUAL folder names in
# /nfs/lssc0/flinders/proteomics/Data/raw_data/tTOF_HT. The archive
# collected several spellings of the same month over the years and the
# copier has to recognise one rather than add another beside it. The
# first version of this parser matched none of them.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$target = Join-Path $repoRoot "scripts/flinders_copy.ps1"
if (-not (Test-Path -LiteralPath $target)) { Write-Host "cannot find $target"; exit 1 }

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($target, [ref] $tokens, [ref] $errors)
if ($errors -and $errors.Count -gt 0) {
    Write-Host "PARSE ERRORS in $target"
    foreach ($err in $errors) { Write-Host "  line $($err.Extent.StartLineNumber): $($err.Message)" }
    exit 1
}
foreach ($func in $ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true)) {
    Invoke-Expression $func.Extent.Text
}

$Failures = 0
function Check($Label, $Got, $Want) {
    if ("$Got" -eq "$Want") { Write-Host "  ok   $Label" }
    else { Write-Host "  FAIL $Label -- got '$Got', want '$Want'"; $script:Failures += 1 }
}

# The script's own settings, which its functions read.
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) "stan_flinders_$(Get-Random)"
$InstrumentDir = "tTOF_HT"
$LookbackHours = 72
$LogDir = Join-Path $sandbox "logs"
$LogFile = Join-Path $LogDir "flinders_copy.log"
$StatusFile = Join-Path $LogDir "flinders_status.txt"
$Show = $false
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

# ---- 1. is this folder name a month? ------------------------------
Write-Host ""
Write-Host "[1] month folder names (the real ones in tTOF_HT)"
foreach ($case in @(
    @("June26","2026-06"), @("Jun26","2026-06"), @("jun25","2025-06"),
    @("JUL26","2026-07"),  @("july26","2026-07"), @("Mar26","2026-03"),
    @("March25","2025-03"),@("nov25","2025-11"),  @("Feb26","2026-02"),
    @("aug26","2026-08"),  @("Aug26","2026-08"),  @("sep25","2025-09"),
    @("may26","2026-05"),  @("apr26","2026-04"),  @("Dec25","2025-12"),
    @("oct25","2025-10"),  @("Apr25","2025-04"),  @("jan25","2025-01"))) {
    $parsed = Get-MonthDate $case[0]
    if ($null -eq $parsed) { Check "parse $($case[0])" "NULL" $case[1] }
    else { Check "parse $($case[0])" $parsed.ToString("yyyy-MM") $case[1] }
}
Write-Host "  -- real folders in there that are NOT months --"
foreach ($name in @("HeLSTDs", "Reports", "Service", "MSmeth", "ServiceBrukerEngineers",
                    "jan25AndPM", "processing", "Bruker_FAS_Promega_samples_Mar26", "")) {
    if ($null -eq (Get-MonthDate $name)) { Write-Host "  ok   reject '$name'" }
    else { Write-Host "  FAIL '$name' parsed as a month"; $Failures += 1 }
}

# ---- 2. destination folder mirrors the local one ------------------
Write-Host ""
Write-Host "[2] destination folder mirrors what the operator acquired into"
$Dest = Join-Path $sandbox "tTOF_HT"
New-Item -ItemType Directory -Path $Dest -Force | Out-Null
foreach ($seed in @("Aug26", "June26", "JUL26", "jun25", "March25", "Reports")) {
    New-Item -ItemType Directory -Path (Join-Path $Dest $seed) -Force | Out-Null
}
Check "Aug26 goes to Aug26"          (Split-Path -Leaf (Get-DestFolder "Aug26"))   "Aug26"
Check "case difference reuses it"    (Split-Path -Leaf (Get-DestFolder "aug26"))   "Aug26"
# Both sides collected several spellings of the same month; a local
# july26 must land in the archive's JUL26, not make a second folder.
Check "july26 reuses JUL26"          (Split-Path -Leaf (Get-DestFolder "july26"))  "JUL26"
Check "Jun26 reuses June26"          (Split-Path -Leaf (Get-DestFolder "Jun26"))   "June26"
Check "Mar25 reuses March25"         (Split-Path -Leaf (Get-DestFolder "Mar25"))   "March25"
Check "an unknown month is created"  (Split-Path -Leaf (Get-DestFolder "Sep26"))   "Sep26"
# A folder that is not a month at all is mirrored verbatim.
Check "a non-month folder is mirrored" (Split-Path -Leaf (Get-DestFolder "Bruker_FAS_samples")) "Bruker_FAS_samples"
Check "no parent means the root"     (Get-DestFolder "") $Dest
Check "6 seeded + exactly 2 created" (@(Get-ChildItem -LiteralPath $Dest -Directory)).Count 8

# ---- 3. finding runs inside the month folders --------------------
# The bug that reached the instrument: timsControl acquires into
# D:\Data\Aug26\, and a top-level-only scan found nothing at all.
Write-Host ""
Write-Host "[3] scanning D:\Data the way it is actually laid out"
$SourceDir = Join-Path $sandbox "Data"
New-Item -ItemType Directory -Path $SourceDir -Force | Out-Null
$Done = @()
# Real paths from the TIMS-10878 database.
$nested = New-Item -ItemType Directory -Force -Path `
    (Join-Path $SourceDir "Aug26/11aug26_HeL50-Flex-questionablGlasCap-tf9d0_60spd_S4-E1_1_23549.d")
$nested2 = New-Item -ItemType Directory -Force -Path `
    (Join-Path $SourceDir "july26/31iul26_HeL50-tf9d0_60spd_S4-E7_1_23330.d")
$atRoot = New-Item -ItemType Directory -Force -Path `
    (Join-Path $SourceDir "wash_S1-A1_1_24181.d")
# A .d holds files and folders of its own -- we must never treat those
# as runs, however they are named.
New-Item -ItemType Directory -Force -Path (Join-Path $nested.FullName "inner.d") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $SourceDir "Reports") | Out-Null

$found = @(Get-Candidates)
Check "finds both nested runs and the root one" $found.Count 3
$rels = @()
foreach ($item in $found) { $rels += $item.Rel }
$sep = [System.IO.Path]::DirectorySeparatorChar
Check "a nested run is keyed by month\name" ($rels -contains "Aug26${sep}11aug26_HeL50-Flex-questionablGlasCap-tf9d0_60spd_S4-E1_1_23549.d") "True"
Check "a root run is keyed by name alone" ($rels -contains "wash_S1-A1_1_24181.d") "True"
Check "it never descends into a .d" ($rels -notcontains "Aug26${sep}11aug26_HeL50-Flex-questionablGlasCap-tf9d0_60spd_S4-E1_1_23549.d${sep}inner.d") "True"
foreach ($item in $found) {
    if ($item.Rel -like "Aug26*") { Check "parent is carried through" $item.Parent "Aug26" }
}

Write-Host ""
Write-Host "[4] what gets skipped"
$old = New-Item -ItemType Directory -Force -Path (Join-Path $SourceDir "June26/063026_HE50_60-spd-dia_S1-A2_1_22380.d")
(Get-Item $old.FullName).LastWriteTime = [datetime] "2026-06-30"
Check "an old run is out of range" (@(Get-Candidates)).Count 3
# The done list is keyed by the relative path...
$Done = @("Aug26${sep}11aug26_HeL50-Flex-questionablGlasCap-tf9d0_60spd_S4-E1_1_23549.d")
Check "a done relative path is skipped" (@(Get-Candidates)).Count 2
# ...but a bare name is honoured too, so a done list seeded before the
# nested scan existed still counts.
$Done = @("31iul26_HeL50-tf9d0_60spd_S4-E7_1_23330.d")
Check "a done bare name is skipped" (@(Get-Candidates)).Count 2
$Done = @("WASH_S1-A1_1_24181.D")
Check "matching ignores case" (@(Get-Candidates)).Count 2
$Done = @()

Write-Host ""
Write-Host "[5] size signature"
Set-Content -LiteralPath (Join-Path $atRoot.FullName "analysis.tdf") -Value ("x" * 100) -NoNewline
New-Item -ItemType Directory -Path (Join-Path $atRoot.FullName "nested") -Force | Out-Null
Set-Content -LiteralPath (Join-Path $atRoot.FullName "nested/frames.bin") -Value ("y" * 50) -NoNewline
Check "counts the whole tree" (Get-Sig $atRoot.FullName) "2/150"
$before = Get-Sig $atRoot.FullName
Check "unchanged tree, same signature" (Get-Sig $atRoot.FullName) $before
# A file growing in place is exactly what a directory mtime misses,
# which is why this is size-based and not mtime-based.
Set-Content -LiteralPath (Join-Path $atRoot.FullName "nested/frames.bin") -Value ("y" * 900) -NoNewline
Check "a file growing changes it" ($before -ne (Get-Sig $atRoot.FullName)) "True"
Check "missing path is survivable" (Get-Sig (Join-Path $sandbox "gone")) "0/0"

Write-Host ""
Write-Host "[5b] still-being-acquired guard"
# The size check alone is not enough: a .d can sit unchanged for
# minutes during LC equilibration, and copying then would archive a
# truncated run permanently, since it gets marked done.
$acq = New-Item -ItemType Directory -Force -Path (Join-Path $SourceDir "Aug26/mid_acquisition_1_99999.d")
Check "a .d with no tdf_bin is not held back" (Test-StillWriting $acq.FullName) "False"
$bin = Join-Path $acq.FullName "analysis.tdf_bin"
Set-Content -LiteralPath $bin -Value "data" -NoNewline
Check "an unlocked tdf_bin is finished" (Test-StillWriting $acq.FullName) "False"
# Hold it the way timsControl would during acquisition.
$held = $null
try { $held = [System.IO.File]::Open($bin, "Open", "ReadWrite", "None") } catch { $held = $null }
if ($null -ne $held) {
    Check "an open tdf_bin means still acquiring" (Test-StillWriting $acq.FullName) "True"
    $held.Close(); $held.Dispose()
    Check "and it is released once the handle closes" (Test-StillWriting $acq.FullName) "False"
} else {
    Write-Host "  skip lock case (this OS does not enforce share modes)"
}

# ---- 6. the across-passes memory ---------------------------------
# This is what replaces the resident watcher's 60-second timer: a run
# is copied when this pass's signature matches what the previous pass
# wrote five minutes ago.
Write-Host ""
Write-Host "[6] size memory across passes"
$SizeFile = Join-Path $LogDir "flinders_sizes.txt"
Check "absent file gives an empty map" (Read-Map $SizeFile).Count 0
$map = @{}
$map["20260828_100spd_COH-46_S5-F6_1_24165.d"] = "412/8891234"
$map["wash_S1-A1_1_24166.d"] = "0/0"
$map["a name with spaces.d"] = "7/42"
Write-Map $SizeFile $map
$back = Read-Map $SizeFile
Check "round trips as a Hashtable, not unrolled" $back.GetType().Name "Hashtable"
Check "all three entries survive" $back.Count 3
Check "signature survives exactly" $back["20260828_100spd_COH-46_S5-F6_1_24165.d"] "412/8891234"
Check "a name with spaces survives" $back["a name with spaces.d"] "7/42"
Check "an unknown name is null, not a crash" ($null -eq $back["never seen.d"]) "True"
# The decision itself: same signature two passes running means finished.
$sig = "412/8891234"
Check "unchanged since last pass = ready" ($back["20260828_100spd_COH-46_S5-F6_1_24165.d"] -eq $sig) "True"
Check "changed since last pass = still acquiring" ($back["wash_S1-A1_1_24166.d"] -eq $sig) "False"
Check "never seen before = still acquiring" ($back["brand_new.d"] -eq $sig) "False"
# Only current candidates are written back, so it cannot grow forever.
Write-Map $SizeFile @{ "only_one.d" = "1/1" }
Check "stale entries are dropped" (Read-Map $SizeFile).Count 1

# ---- 7. finding the Flinders folder ------------------------------
Write-Host ""
Write-Host "[7] destination resolution"
$share = Join-Path $sandbox "share"
$sep = [System.IO.Path]::DirectorySeparatorChar
$deep = Join-Path $share "Data${sep}raw_data${sep}tTOF_HT"
New-Item -ItemType Directory -Path $deep -Force | Out-Null
if ($IsWindows -or ($null -eq $IsWindows)) {
    Check "share root finds the instrument folder" (Resolve-Dest $share) $deep
    Check "trailing slash is fine" (Resolve-Dest "$share\") $deep
} else {
    Write-Host "  skip share-root cases (Windows-separated suffixes)"
}
Check "the folder itself is accepted" (Resolve-Dest $deep) $deep
Check "somewhere unrelated gives nothing" (Resolve-Dest $LogDir) ""
Check "a path that does not exist gives nothing" (Resolve-Dest (Join-Path $sandbox "ghost")) ""
Check "empty gives nothing" (Resolve-Dest "") ""

# ---- 8. it must not drag anything onto the instrument PC ---------
Write-Host ""
Write-Host "[8] no dependencies on an acquiring PC"
# Strip comments first -- the header talks about Python precisely to
# say it is not used.
$codeOnly = @()
foreach ($line in (Get-Content -LiteralPath $target)) {
    if ($line.Trim().StartsWith("#")) { continue }
    $codeOnly += $line
}
$code = $codeOnly -join "`n"
Check "no Python anywhere in the code" ($code -notmatch '(?i)python|\bpip\b|\.py\b') "True"
Check "no STAN CLI or database dependency" ($code -notmatch '(?i)stan\.exe|stan\.db|sqlite|venv') "True"
# robocopy does the copying; powershell.exe is only ever the
# self-elevation relaunch of this same script. Nothing else may be
# spawned on a PC that is acquiring data.
Check "only robocopy and the elevation relaunch are spawned" ($code -notmatch '(?i)Start-Process\s+(?!robocopy|powershell)') "True"
Check "shipped suffixes are Windows-separated" ($code -match 'Data\\raw_data') "True"
# Mapped network drives are per-session: a task running as SYSTEM
# could not see the Flinders drive at all.
Check "task does not run as SYSTEM" ($code -notmatch '(?i)-User\s+"?SYSTEM|RunLevel\s+Highest') "True"
Check "task tolerates a missed window" ($code -match 'StartWhenAvailable') "True"
Check "overlapping passes are refused" ($code -match 'IgnoreNew') "True"

# ---- 9. install must not lie, and must not elevate too much -----
# Both of these shipped broken to the instrument PC on the first try:
# Register-ScheduledTask was refused with 0x80070005 because UAC hands
# a filtered token even to an Administrators account, and the installer
# then printed "Done. It will check D:\Data every 5 minutes" anyway.
Write-Host ""
Write-Host "[9] installer honesty and the elevation split"

function Get-FuncText($Name) {
    $found = $ast.Find({
        $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $args[0].Name -eq $Name
    }, $true)
    if ($found) { return $found.Extent.Text }
    return ""
}

$reg = Get-FuncText "Register-Task"
$dst = Get-FuncText "Set-Destination"
Check "Register-Task exists" ($reg.Length -gt 0) "True"
Check "it looks for the task before claiming success" ($reg -match "Test-Task") "True"
Check "it elevates itself when refused" ($reg -match "RunAs") "True"
# An elevated process cannot see mapped network drives, so the half
# that has to find R:\ must never be the half that elevates.
Check "finding the drive never elevates" ($dst -notmatch "RunAs") "True"
Check "the destination is stored as a UNC" ($dst -match "ConvertTo-Unc") "True"
# -Install has to set the destination first, unelevated, or the
# elevated half would have no drive to look at.
Check "install does destination before task" ($code.IndexOf("Set-Destination)") -lt $code.IndexOf("Register-Task)")) "True"
# The scan has to look inside the month folders or it finds nothing.
Check "the scan goes one level down" ((Get-FuncText "Get-Candidates") -match "New-Candidate .top|New-Candidate .sub") "True"

$bat = Get-Content -LiteralPath (Join-Path $repoRoot "scripts/install_flinders_copy.bat") -Raw
Check "the .bat checks the exit code before saying Done" ($bat -match "errorlevel 1 goto :failed") "True"
Check "and says nothing is scheduled when it fails" ($bat -match "NOTHING is scheduled") "True"

Write-Host ""
Write-Host "[10] UNC conversion"
Check "a UNC path is left alone" (ConvertTo-Unc "\\srv\share\Data") "\\srv\share\Data"
Check "an unmapped letter is left alone" (ConvertTo-Unc "Q:\nope") "Q:\nope"
Check "empty is left alone" (ConvertTo-Unc "") ""

Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "===================================="
if ($Failures -gt 0) { Write-Host "$Failures FAILURE(S)"; exit 1 }
Write-Host "all checks passed"
exit 0
