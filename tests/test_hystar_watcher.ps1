# test_hystar_watcher.ps1
#
# Tests for the reading and parsing half of instrument/scripts/watch_hystar_errors.ps1
#
#     pwsh -NoProfile -File tests/test_hystar_watcher.ps1
#
# Same harness as test_instrument_copy_scripts.ps1: the real function
# definitions are pulled out of the shipped .ps1 through the AST, so the tests
# cannot drift from what ships, and the script's top level never runs.
#
# Fixtures are real slices of HyStar's CompassHyStarErrorLogbook.log (UTF-16LE
# with BOM). Real records start with a DC3 (0x13) byte and end with 0x11 CR LF;
# an embedded newline inside a message is a bare LF.

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

$scriptRel = "instrument/scripts/watch_hystar_errors.ps1"
$ast = Load-Script $scriptRel

# Define every function from the shipped file in this scope, without running
# the script's top level.
foreach ($f in $ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    . ([scriptblock]::Create($f.Extent.Text))
}

function Fixture($name) { return (Join-Path $repoRoot "tests/fixtures/hystar_logbook/$name") }

# Parse every record of a fixture; returns the converted records.
function Parse-Fixture($name) {
    $r = Read-NewLogText -Path (Fixture $name) -Offset 0
    $s = Split-LogRecords -Text $r.Text
    $out = @()
    foreach ($rec in $s.Records) {
        $p = ConvertFrom-LogRecord -Record $rec
        if ($p -ne $null) { $out += $p }
    }
    return ,$out
}
function Count-Type($parsed, $type) {
    $n = 0
    foreach ($p in $parsed) { if ($p.Type -eq $type) { $n++ } }
    return $n
}
function First-Type($parsed, $type) {
    foreach ($p in $parsed) { if ($p.Type -eq $type) { return $p } }
    return $null
}

# ---------------------------------------------------------------- 1. evotip
Write-Host ""
Write-Host "1. Evotip fixture (multi-line message)"
$p = Parse-Fixture "evotip_2026-08-31.log"
Check "exactly one stop"   (Count-Type $p "stop") 1
$stop = First-Type $p "stop"
Check "At"                 $stop.At.ToString("yyyy-MM-dd HH:mm:ss") "2026-08-31 14:39:51"
Check "source"             $stop.Source "ApplicationException"
Check "message starts"     $stop.Message.StartsWith("Evosep One: No Evotip was present") "True"
Check "message has tail"   $stop.Message.Contains("matches the sample list") "True"
Check "no wrapper left"    $stop.Message.Contains("Critical Error in ICF System") "False"
Check "no closing marks"   $stop.Message.EndsWith("!!!") "False"
Check "no newline left"    $stop.Message.Contains("`n") "False"
Check "category"           (Get-StopCategory $stop.Message).Category "evotip"
Check "title"              (Get-StopCategory $stop.Message).Title "No Evotip"
Check "evotip check line"  ((Get-StopCategory $stop.Message).Check -like "*Check:* the Evotip in {vial}*") "True"

# -------------------------------------------------------------- 2. pressure
Write-Host ""
Write-Host "2. pressure fixture (Critical error line is not a stop)"
$p = Parse-Fixture "pressure_2026-09-02.log"
Check "exactly one stop"   (Count-Type $p "stop") 1
$stop = First-Type $p "stop"
Check "message unwrapped"  $stop.Message "Evosep One: pressure limit exceeded"
Check "category"           (Get-StopCategory $stop.Message).Category "overpressure"
Check "other check line"   (Get-StopCategory $stop.Message).Check "The queue stays stopped until someone restarts it."

# ------------------------------------------------------------------- 3. MS
Write-Host ""
Write-Host "3. MS not connected fixture"
$p = Parse-Fixture "ms_not_connected.log"
Check "two stops"          (Count-Type $p "stop") 2
$allConn = $true
foreach ($x in $p) {
    if ($x.Type -eq "stop") {
        $c = Get-StopCategory $x.Message
        if ($c.Category -ne "connection_lost" -or $c.Title -ne "MS not connected") { $allConn = $false }
    }
}
Check "all connection_lost / MS not connected" $allConn "True"
$stop = First-Type $p "stop"
Check "OTOF text kept intact" $stop.Message.Contains("Bruker OTOF MS - 'not connected'") "True"

# --------------------------------------------------------------- 4. restart
Write-Host ""
Write-Host "4. restart fixture (H: + S: header, no shutdown)"
$p = Parse-Fixture "restart.log"
Check "one restart"        (Count-Type $p "restart") 1
Check "no stop"            (Count-Type $p "stop") 0
$rs = First-Type $p "restart"
Check "restart At"         $rs.At.ToString("yyyy-MM-dd HH:mm:ss") "2026-01-12 06:06:35"

# --------------------------------------------------------------- 5. partial
Write-Host ""
Write-Host "5. partial tail is held back, then released"
$r = Read-NewLogText -Path (Fixture "partial_tail.log") -Offset 0
$s = Split-LogRecords -Text $r.Text
Check "one whole record yielded"   $s.Records.Count 1
Check "last record not consumed"   ($s.ConsumedChars -lt $r.Text.Length) "True"
Check "consumed stops at record"   ($s.ConsumedChars -gt 0) "True"
$now = Get-Date
$s2 = Split-LogRecords -Text $r.Text -PendingSince $now.AddMinutes(-3) -Now $now
Check "released after 3 min"       $s2.Records.Count 2
Check "all consumed when released" $s2.ConsumedChars $r.Text.Length
$s3 = Split-LogRecords -Text $r.Text -PendingSince $now.AddSeconds(-30) -Now $now
Check "not released after 30 s"    $s3.Records.Count 1
# text with no trailing newline is held even when it is not a shutdown
$s4 = Split-LogRecords -Text "E:2026/09/02 00:27:10,976 Critical error. Error code #1"
Check "no newline -> held"         $s4.Records.Count 0
$s5 = Split-LogRecords -Text "E:2026/09/02 00:27:10,976 Critical error. Error code #1`r`n"
Check "newline -> consumed"        $s5.Records.Count 1

# ------------------------------------------------------------- 6. odd offset
Write-Host ""
Write-Host "6. odd offset lands on a char boundary"
$r3 = Read-NewLogText -Path (Fixture "pressure_2026-09-02.log") -Offset 3
$r2 = Read-NewLogText -Path (Fixture "pressure_2026-09-02.log") -Offset 2
Check "offset 3 same as 2"    $r3.Text $r2.Text
Check "text starts at record" $r3.Text.StartsWith(([string][char]0x13 + "S:2026/08/31"), [System.StringComparison]::Ordinal) "True"
$r1 = Read-NewLogText -Path (Fixture "pressure_2026-09-02.log") -Offset 0
Check "offset 0 skips BOM"    ([int][char]$r1.Text[0]) 19
Check "NewOffset is length"   $r1.NewOffset $r1.Length
# an offset into the middle of a record: leading debris is skipped and the
# next whole record still parses
$mid = Read-NewLogText -Path (Fixture "pressure_2026-09-02.log") -Offset 101
$sm = Split-LogRecords -Text $mid.Text
Check "mid-record offset even"   ($mid.NewOffset % 2) 0
$sawStop = $false
foreach ($rec in $sm.Records) {
    $pp = ConvertFrom-LogRecord -Record $rec
    if ($pp -ne $null -and $pp.Type -eq "stop") { $sawStop = $true }
}
Check "next whole record parses" $sawStop "True"
# offset past the end (rotation) is not an exception
$past = Read-NewLogText -Path (Fixture "pressure_2026-09-02.log") -Offset ($r1.Length + 1000)
Check "past EOF -> empty text"   $past.Text ""
Check "past EOF reports Length"  $past.Length $r1.Length

# ---------------------------------------------------------- 7. stray bytes
Write-Host ""
Write-Host "7. stray bytes parse without an exception"
$stray = [string][char]0x13 + "E:2026/09/02 00:27:11,047 !!! EmergencyShutdown Source=PlugInCriticalError Message='Critical error from ICF System! Error #1 -> 'Evosep" + [string][char]0xA0 + "One: pressure" + [string][char]0x13 + " limit exceeded'' !!!" + [string][char]0x11 + "`r`n"
$threw = $false; $pr = $null
try { $pr = ConvertFrom-LogRecord -Record $stray } catch { $threw = $true }
Check "no exception"      $threw "False"
Check "still a stop"      $pr.Type "stop"
Check "overpressure"      (Get-StopCategory $pr.Message).Category "overpressure"
$restartRec = Parse-Fixture "restart.log"
Check "NBSP header parses" (Count-Type $restartRec "restart") 1
$junk = $null; $threw = $false
try { $junk = ConvertFrom-LogRecord -Record "garbage" } catch { $threw = $true }
Check "garbage -> null, no throw" "$threw $($junk -eq $null)" "False True"

# ----------------------------------------------------------- 8. fan storm
Write-Host ""
Write-Host "8. fan-fault storm"
$p = Parse-Fixture "fan_storm.log"
$stops = Count-Type $p "stop"
Check "at least 200 stops"  ($stops -ge 200) "True"
$cats = @{}
$keys = @{}
$gapOk = $true
$prev = $null
foreach ($x in $p) {
    if ($x.Type -eq "stop") {
        $cats[(Get-StopCategory $x.Message).Category] = 1
        $keys[(Get-DedupKey $x.Message)] = 1
    }
}
Check "fan fault is evosep_hw" ($cats.ContainsKey("evosep_hw")) "True"
Check "fan fault key folds"  ($keys.ContainsKey("critical error from icf system! error #n -> 'evosep one: fan detection fault'") -or $keys.ContainsKey("evosep one: fan detection fault")) "True"
$fanStops = 0
foreach ($x in $p) { if ($x.Type -eq "stop" -and $x.Message -like "*fan detection fault*") { $fanStops++ } }
Check "at least 200 fan-fault stops" ($fanStops -ge 200) "True"

# -------------------------------------------------------------- 9. categories
Write-Host ""
Write-Host "9. category table"
$want = @(
    @("Evosep One: Tip expected but not present!", "evotip", "No Evotip"),
    @("Evosep One: High pressure on pump A and/or B detected.", "overpressure", "Pressure limit"),
    @("Critical error from Bruker OTOF MS! Error #200 -> 'not connected'", "connection_lost", "MS not connected"),
    @("Critical Error in Bruker OTOF MS - 'Required Python plugin proteoscape not available'", "ms_error", "MS software error"),
    @("UCEERR_RENDERTHREADFAILURE", "hystar_crash", "HyStar crashed"),
    @("Evosep One: mcu reset", "evosep_hw", "Evosep hardware fault"),
    @("Evosep One: Unable to access autosampler", "evosep_hw", "Evosep hardware fault"),
    @("Evosep One: Object reference not set to an instance of an object.", "other", "Stopped")
)
foreach ($w in $want) {
    $c = Get-StopCategory $w[0]
    Check "category: $($w[0])" "$($c.Category)|$($c.Title)" "$($w[1])|$($w[2])"
}

# ------------------------------------------------------------------ 10. dedup
Write-Host ""
Write-Host "10. dedup key"
$k1 = Get-DedupKey "Evosep One: fan detection fault (fan 3, 1200 rpm)"
$k2 = Get-DedupKey "Evosep One: fan detection fault (fan 7,  980 rpm)"
Check "numbers ignored"     $k1 $k2
Check "lower-cased, N runs" $k1 "evosep one: fan detection fault (fan n, n rpm)"
Check "different text differs" ((Get-DedupKey "Evosep One: mcu reset") -ne $k1) "True"

# ---------------------------------------------------------------- 11. version
Write-Host ""
Write-Host "11. version"
$initText = Get-Content -LiteralPath (Join-Path $repoRoot "stan/__init__.py") -Raw
$pyVer = ""
if ($initText -match '__version__\s*=\s*"([^"]+)"') { $pyVer = $Matches[1] }
$shipped = ""
foreach ($a in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
    if ($a.Left.Extent.Text -eq '$ScriptVersion') { $shipped = $a.Right.Extent.Text.Trim("'", '"', '(', ')') }
}
Check "ScriptVersion equals stan/__init__.py" $shipped $pyVer
$sw = $false
if ($ast.ParamBlock) {
    foreach ($pp in $ast.ParamBlock.Parameters) { if ($pp.Name.VariablePath.UserPath -eq "Scheduled") { $sw = $true } }
}
Check "has -Scheduled switch" $sw "True"

Write-Host ""
if ($Failures -gt 0) { Write-Host "$Failures FAILED"; exit 1 }
Write-Host "all ok"
exit 0
