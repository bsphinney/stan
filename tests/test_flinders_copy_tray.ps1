# test_flinders_copy_tray.ps1
#
# Tests for scripts/flinders_copy_tray.ps1 -- the timsTOF -> Flinders
# tray copier.
#
# Run with PowerShell (5.1 on Windows, or pwsh anywhere):
#     pwsh -NoProfile -File tests/test_flinders_copy_tray.ps1
#
# Not part of the pytest suite -- CI is Python-only -- so run it by hand
# after touching the script. It loads the real function definitions out
# of the shipped .ps1 via the PowerShell AST, so it exercises the code
# that actually ships rather than a copy that can drift.
#
# The cases in "month folder parsing" are the REAL directory names in
# /nfs/lssc0/flinders/proteomics/Data/raw_data/tTOF_HT. The archive
# accumulated several spellings of the same month over the years
# (June26 and jun25 and JUL26 and july26 and March25 and Mar26), so the
# copier has to recognise an existing month folder rather than create a
# second spelling beside it. That is the whole point of this file: the
# first version of ConvertTo-MonthDate matched none of them, because
# PowerShell binds a PS array to TryParseExact's (string, string, ...)
# overload and stringifies it to "System.Object[]".

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$target = Join-Path $repoRoot "scripts/flinders_copy_tray.ps1"
if (-not (Test-Path -LiteralPath $target)) {
    Write-Host "cannot find $target"
    exit 1
}

# ---- load the real functions, and only the functions --------------
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($target, [ref] $tokens, [ref] $errors)
if ($errors -and $errors.Count -gt 0) {
    Write-Host "PARSE ERRORS in $target"
    foreach ($err in $errors) {
        Write-Host "  line $($err.Extent.StartLineNumber): $($err.Message)"
    }
    exit 1
}
$funcs = $ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true)
foreach ($func in $funcs) {
    Invoke-Expression $func.Extent.Text
}
Write-Host "loaded $($funcs.Count) functions from flinders_copy_tray.ps1"

# ---- harness ------------------------------------------------------
$script:Failures = 0

function Assert-Equal {
    param(
        [string] $Label,
        $Got,
        $Want
    )
    if ("$Got" -eq "$Want") {
        Write-Host "  ok   $Label"
    } else {
        Write-Host "  FAIL $Label -- got '$Got', want '$Want'"
        $script:Failures += 1
    }
}

$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) "stan_flinders_test_$(Get-Random)"
$script:LogDir = Join-Path $sandbox "logs"
$null = New-Item -ItemType Directory -Path $script:LogDir -Force
$script:ShowConsoleMode = $false
$script:InstrumentDir = "tTOF_HT"
$script:StatePath = Join-Path $sandbox "state.json"
$script:ConfigPath = Join-Path $sandbox "config.json"

# ---- 1. month folder name parsing ---------------------------------
Write-Host ""
Write-Host "[1] month folder parsing (real tTOF_HT folder names)"
$monthCases = @(
    @("June26", "2026-06"), @("Jun26",   "2026-06"), @("jun25",  "2025-06"),
    @("JUL26",  "2026-07"), @("july26",  "2026-07"), @("Mar26",  "2026-03"),
    @("March25","2025-03"), @("nov25",   "2025-11"), @("Feb26",  "2026-02"),
    @("aug26",  "2026-08"), @("Aug26",   "2026-08"), @("sep25",  "2025-09"),
    @("may26",  "2026-05"), @("apr26",   "2026-04"), @("Dec25",  "2025-12"),
    @("oct25",  "2025-10"), @("Apr25",   "2025-04"), @("jan25",  "2025-01")
)
foreach ($case in $monthCases) {
    $parsed = ConvertTo-MonthDate $case[0]
    if ($null -eq $parsed) {
        Assert-Equal "parse $($case[0])" "NULL" $case[1]
    } else {
        Assert-Equal "parse $($case[0])" $parsed.ToString("yyyy-MM") $case[1]
    }
}

Write-Host "  -- these are NOT month folders and must not parse --"
$notMonths = @(
    "HeLSTDs", "Reports", "Service", "MSmeth", "ServiceBrukerEngineers",
    "jan25AndPM", "Bruker_FAS_Promega_samples_Mar26", "processing", ""
)
foreach ($name in $notMonths) {
    $parsed = ConvertTo-MonthDate $name
    if ($null -eq $parsed) {
        Write-Host "  ok   reject '$name'"
    } else {
        Write-Host "  FAIL '$name' parsed as $($parsed.ToString('yyyy-MM'))"
        $script:Failures += 1
    }
}

# ---- 2. month folder reuse ----------------------------------------
Write-Host ""
Write-Host "[2] month folder reuse -- never create a second spelling"
$script:DestRoot = Join-Path $sandbox "tTOF_HT"
$null = New-Item -ItemType Directory -Path $script:DestRoot -Force
foreach ($seed in @("June26", "jun25", "JUL26", "aug26", "March25", "Reports")) {
    $null = New-Item -ItemType Directory -Path (Join-Path $script:DestRoot $seed) -Force
}
Assert-Equal "Jun 2026 reuses June26"  (Split-Path -Leaf (Resolve-MonthDir ([datetime] "2026-06-15"))) "June26"
Assert-Equal "Jul 2026 reuses JUL26"   (Split-Path -Leaf (Resolve-MonthDir ([datetime] "2026-07-02"))) "JUL26"
Assert-Equal "Aug 2026 reuses aug26"   (Split-Path -Leaf (Resolve-MonthDir ([datetime] "2026-08-28"))) "aug26"
Assert-Equal "Jun 2025 reuses jun25"   (Split-Path -Leaf (Resolve-MonthDir ([datetime] "2025-06-01"))) "jun25"
Assert-Equal "Mar 2025 reuses March25" (Split-Path -Leaf (Resolve-MonthDir ([datetime] "2025-03-09"))) "March25"
Assert-Equal "Sep 2026 creates Sep26"  (Split-Path -Leaf (Resolve-MonthDir ([datetime] "2026-09-01"))) "Sep26"
$dirsAfter = @(Get-ChildItem -LiteralPath $script:DestRoot -Directory)
Assert-Equal "6 seeded + exactly 1 new folder" $dirsAfter.Count 7

# ---- 3. which timestamp decides the month -------------------------
Write-Host ""
Write-Host "[3] run timestamp -- filename date beats directory mtime"
$dataDir = Join-Path $sandbox "Data"
$null = New-Item -ItemType Directory -Path $dataDir -Force
$dated = New-Item -ItemType Directory -Path (Join-Path $dataDir "20260828_100spd_COH-46_S5-F6_1_24165.d") -Force
$undated = New-Item -ItemType Directory -Path (Join-Path $dataDir "wash_S1-A1_1_24166.d") -Force
# A run acquired 28 Aug but copied on 3 Sep must still land in Aug26.
(Get-Item $dated.FullName).LastWriteTime = [datetime] "2026-09-03 04:00"
(Get-Item $undated.FullName).LastWriteTime = [datetime] "2026-08-30 11:00"
Assert-Equal "dated name wins over mtime" (Get-RunStamp (Get-Item $dated.FullName)).ToString("yyyy-MM-dd") "2026-08-28"
Assert-Equal "undated name falls back to mtime" (Get-RunStamp (Get-Item $undated.FullName)).ToString("yyyy-MM-dd") "2026-08-30"

# ---- 4. tree measurement (the stability signal) -------------------
Write-Host ""
Write-Host "[4] directory measurement"
Set-Content -LiteralPath (Join-Path $dated.FullName "analysis.tdf") -Value ("x" * 100) -NoNewline
$null = New-Item -ItemType Directory -Path (Join-Path $dated.FullName "nested") -Force
Set-Content -LiteralPath (Join-Path $dated.FullName "nested/frames.bin") -Value ("y" * 50) -NoNewline
$measured = Measure-Tree $dated.FullName
Assert-Equal "counts files recursively" $measured.Files 2
Assert-Equal "sums bytes recursively" $measured.Bytes 150
Assert-Equal "missing path returns null" (Measure-Tree (Join-Path $sandbox "does_not_exist")) ""

# ---- 5. candidate selection ---------------------------------------
Write-Host ""
Write-Host "[5] candidate selection"
$script:SourceDir = $dataDir
$script:LookbackHours = 72
$script:Copied = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
$null = New-Item -ItemType Directory -Path (Join-Path $dataDir "some_other_folder") -Force
$ancient = New-Item -ItemType Directory -Path (Join-Path $dataDir "20200101_ancient.d") -Force
(Get-Item $ancient.FullName).LastWriteTime = [datetime] "2020-01-01"
(Get-Item $dated.FullName).LastWriteTime = (Get-Date)
(Get-Item $undated.FullName).LastWriteTime = (Get-Date)
Assert-Equal "only recent .d directories" (@(Get-Candidates)).Count 2
[void] $script:Copied.Add("wash_S1-A1_1_24166.d")
Assert-Equal "skips what state says is archived" (@(Get-Candidates)).Count 1
# Windows paths are case-insensitive, so the state set must be too --
# otherwise a case difference re-copies a run that is already there.
[void] $script:Copied.Add("20260828_100SPD_COH-46_S5-F6_1_24165.D")
Assert-Equal "state matching is case-insensitive" (@(Get-Candidates)).Count 0

# ---- 6. state file round trip -------------------------------------
Write-Host ""
Write-Host "[6] state file round trip"
Save-StateFile
$reloaded = Read-StateFile
Assert-Equal "both names survive a save/load" $reloaded.Count 2
Assert-Equal "reload is a HashSet, not unrolled" $reloaded.GetType().Name 'HashSet`1'
Assert-Equal "reload is still case-insensitive" $reloaded.Contains("WASH_S1-A1_1_24166.D") "True"
# A one-name set used to come back as a bare String and an empty one
# as $null, so Add threw and nothing was ever recorded as archived.
[void] $reloaded.Add("later.d")
Assert-Equal "reloaded set still accepts Add" $reloaded.Count 3
# A single-element JSON array must not collapse to a scalar on reload.
$script:Copied = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
[void] $script:Copied.Add("only_one.d")
Save-StateFile
Assert-Equal "single entry survives" (Read-StateFile).Count 1
# A corrupt state file must degrade to "nothing archived yet", not throw.
Set-Content -LiteralPath $script:StatePath -Value "{ this is not json" -Encoding UTF8
Assert-Equal "corrupt state file is survivable" (Read-StateFile).Count 0

# ---- 7. StrictMode-safe JSON property access ----------------------
Write-Host ""
Write-Host "[7] JSON property access under StrictMode"
$obj = ConvertFrom-Json '{ "sourceDir": "D:\\Data", "stableSecs": 60 }'
Assert-Equal "reads a present property" (Get-JsonProp $obj "sourceDir") "D:\Data"
Assert-Equal "missing property is null, not a throw" (Get-JsonProp $obj "nope") ""
Assert-Equal "null object is null, not a throw" (Get-JsonProp $null "sourceDir") ""

# ---- 8. destination resolution ------------------------------------
# The shipped $script:DestSuffixes are Windows-separated, so rebuild
# them with this platform's separator to exercise the same control flow
# off Windows. The suffix list itself is asserted separately below.
Write-Host ""
Write-Host "[8] Flinders destination resolution"
$sep = [System.IO.Path]::DirectorySeparatorChar
$script:DestSuffixes = @(
    "Data${sep}raw_data${sep}$($script:InstrumentDir)",
    "raw_data${sep}$($script:InstrumentDir)",
    $script:InstrumentDir
)
$shareRoot = Join-Path $sandbox "share"
$deep = Join-Path $shareRoot "Data${sep}raw_data${sep}tTOF_HT"
$null = New-Item -ItemType Directory -Path $deep -Force
Assert-Equal "share root resolves to the instrument dir" (Resolve-FlindersTarget $shareRoot) $deep
Assert-Equal "trailing separator is tolerated" (Resolve-FlindersTarget "$shareRoot$sep") $deep
Assert-Equal "the instrument dir itself is accepted" (Resolve-FlindersTarget $deep) $deep
Assert-Equal "an unrelated dir resolves to null" (Resolve-FlindersTarget $script:LogDir) ""
Assert-Equal "a nonexistent path resolves to null" (Resolve-FlindersTarget (Join-Path $sandbox "ghost")) ""
Assert-Equal "empty input resolves to null" (Resolve-FlindersTarget "") ""

# The suffixes that actually ship must stay Windows-separated; this
# test rewrote them above so it can run off Windows, so assert on the
# source text rather than on the rewritten variable.
$shippedText = Get-Content -LiteralPath $target -Raw
Assert-Equal "shipped suffixes are Windows-separated" ($shippedText -match 'Data\\raw_data') "True"

Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "===================================="
if ($script:Failures -gt 0) {
    Write-Host "$($script:Failures) FAILURE(S)"
    exit 1
}
Write-Host "all checks passed"
exit 0
