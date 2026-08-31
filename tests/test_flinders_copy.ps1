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

# ---- 2. reuse the spelling that is already there ------------------
Write-Host ""
Write-Host "[2] month folder reuse -- never add a second spelling"
$Dest = Join-Path $sandbox "tTOF_HT"
New-Item -ItemType Directory -Path $Dest -Force | Out-Null
foreach ($seed in @("June26", "jun25", "JUL26", "aug26", "March25", "Reports")) {
    New-Item -ItemType Directory -Path (Join-Path $Dest $seed) -Force | Out-Null
}
Check "Jun 2026 uses June26"   (Split-Path -Leaf (Get-MonthDir ([datetime] "2026-06-15"))) "June26"
Check "Jul 2026 uses JUL26"    (Split-Path -Leaf (Get-MonthDir ([datetime] "2026-07-02"))) "JUL26"
Check "Aug 2026 uses aug26"    (Split-Path -Leaf (Get-MonthDir ([datetime] "2026-08-28"))) "aug26"
Check "Jun 2025 uses jun25"    (Split-Path -Leaf (Get-MonthDir ([datetime] "2025-06-01"))) "jun25"
Check "Mar 2025 uses March25"  (Split-Path -Leaf (Get-MonthDir ([datetime] "2025-03-09"))) "March25"
Check "Sep 2026 creates Sep26" (Split-Path -Leaf (Get-MonthDir ([datetime] "2026-09-01"))) "Sep26"
Check "6 seeded + exactly 1 new" (@(Get-ChildItem -LiteralPath $Dest -Directory)).Count 7

# ---- 3. which date files the run ---------------------------------
Write-Host ""
Write-Host "[3] the run's own name beats the folder mtime"
$SourceDir = Join-Path $sandbox "Data"
New-Item -ItemType Directory -Path $SourceDir -Force | Out-Null
$dated = New-Item -ItemType Directory -Path (Join-Path $SourceDir "20260828_100spd_COH-46_S5-F6_1_24165.d") -Force
$plain = New-Item -ItemType Directory -Path (Join-Path $SourceDir "wash_S1-A1_1_24166.d") -Force
# Acquired 28 Aug, copied 3 Sep -- must still be filed under August.
(Get-Item $dated.FullName).LastWriteTime = [datetime] "2026-09-03 04:00"
(Get-Item $plain.FullName).LastWriteTime = [datetime] "2026-08-30 11:00"
Check "dated name wins" (Get-Stamp (Get-Item $dated.FullName)).ToString("yyyy-MM-dd") "2026-08-28"
Check "undated falls back to mtime" (Get-Stamp (Get-Item $plain.FullName)).ToString("yyyy-MM-dd") "2026-08-30"
Check "and it lands in August" (Split-Path -Leaf (Get-MonthDir (Get-Stamp (Get-Item $dated.FullName)))) "aug26"

# ---- 4. the finished-acquiring signal ----------------------------
Write-Host ""
Write-Host "[4] size signature"
Set-Content -LiteralPath (Join-Path $dated.FullName "analysis.tdf") -Value ("x" * 100) -NoNewline
New-Item -ItemType Directory -Path (Join-Path $dated.FullName "nested") -Force | Out-Null
Set-Content -LiteralPath (Join-Path $dated.FullName "nested/frames.bin") -Value ("y" * 50) -NoNewline
Check "counts the whole tree" (Get-Sig $dated.FullName) "2/150"
$before = Get-Sig $dated.FullName
Check "unchanged tree, same signature" (Get-Sig $dated.FullName) $before
# A file growing in place is exactly what a directory mtime misses,
# which is the whole reason this is size-based.
Set-Content -LiteralPath (Join-Path $dated.FullName "nested/frames.bin") -Value ("y" * 900) -NoNewline
Check "a file growing changes it" ($before -ne (Get-Sig $dated.FullName)) "True"
Check "missing path is survivable" (Get-Sig (Join-Path $sandbox "gone")) "0/0"

# ---- 5. what gets picked up --------------------------------------
Write-Host ""
Write-Host "[5] candidate selection"
$Done = @()
New-Item -ItemType Directory -Path (Join-Path $SourceDir "not_a_raw_folder") -Force | Out-Null
$old = New-Item -ItemType Directory -Path (Join-Path $SourceDir "20200101_ancient.d") -Force
(Get-Item $old.FullName).LastWriteTime = [datetime] "2020-01-01"
(Get-Item $dated.FullName).LastWriteTime = (Get-Date)
(Get-Item $plain.FullName).LastWriteTime = (Get-Date)
Check "only recent .d folders" (@(Get-Candidates)).Count 2
$Done = @("wash_S1-A1_1_24166.d")
Check "skips what the done file lists" (@(Get-Candidates)).Count 1
# Windows paths are case-insensitive, so this has to be too, or a run
# already on Flinders gets copied a second time.
$Done = @("wash_S1-A1_1_24166.d", "20260828_100SPD_COH-46_S5-F6_1_24165.D")
Check "matching ignores case" (@(Get-Candidates)).Count 0
$Done = @()
Check "empty done list is fine" (@(Get-Candidates)).Count 2

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
Check "robocopy is the only external program" ($code -notmatch '(?i)Start-Process\s+(?!robocopy)') "True"
Check "shipped suffixes are Windows-separated" ($code -match 'Data\\raw_data') "True"
# Mapped network drives are per-session: a task running as SYSTEM
# could not see the Flinders drive at all.
Check "task does not run as SYSTEM" ($code -notmatch '(?i)-User\s+"?SYSTEM|RunLevel\s+Highest') "True"
Check "task tolerates a missed window" ($code -match 'StartWhenAvailable') "True"
Check "overlapping passes are refused" ($code -match 'IgnoreNew') "True"

Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "===================================="
if ($Failures -gt 0) { Write-Host "$Failures FAILURE(S)"; exit 1 }
Write-Host "all checks passed"
exit 0
