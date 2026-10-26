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

# ------------------------------------------------- 12. burst rule / restart
Write-Host ""
Write-Host "12. burst rule and restart detection"
$storm = Parse-Fixture "fan_storm.log"
# The fixture holds unrelated stops from other days too; the fan-fault storm
# is the contiguous run of "fan detection" stops, 2 min apart.
$fan = @()
# The first fan stop (13:19) is a lone one 12 min before the storm proper
# (13:31 on, every ~2 min), so it is correctly its own alert; start at 13:31.
$seen = 0
foreach ($e in $storm) {
    if ($e.Type -eq "stop" -and $e.Message -match "fan detection") {
        $seen++
        if ($seen -gt 1) { $fan += $e }
    }
}
Check "storm: fan stops seen > 100" ($fan.Count -gt 100) "True"
$st = @{ keys = @{}; lastShutdownAt = $null }
$nNew = 0; $nRem = 0
for ($i = 0; $i -lt 100; $i++) {
    $d = Get-AlertDecision -State $st -Key (Get-DedupKey $fan[$i].Message) -At $fan[$i].At
    if ($d -eq "new") { $nNew++ }
    if ($d -eq "reminder") { $nRem++ }
}
Check "storm (first 100, under 4h): exactly 1 new" $nNew 1
Check "storm (first 100, under 4h): 0 reminders"   $nRem 0
$stAll = @{ keys = @{}; lastShutdownAt = $null }
$nNew = 0; $nRem = 0
foreach ($e in $fan) {
    $d = Get-AlertDecision -State $stAll -Key (Get-DedupKey $e.Message) -At $e.At
    if ($d -eq "new") { $nNew++ }
    if ($d -eq "reminder") { $nRem++ }
}
Check "whole storm: 1 new"       $nNew 1
Check "whole storm: 1 reminder"  $nRem 1

# the same storm stretched: one event every 2 min for 4h10m
$st2 = @{ keys = @{}; lastShutdownAt = $null }
$t0 = [datetime]"2026-10-24 02:00:00"
$nNew = 0; $nRem = 0
for ($i = 0; $i -le 125; $i++) {
    $d = Get-AlertDecision -State $st2 -Key "fan" -At $t0.AddMinutes(2 * $i)
    if ($d -eq "new") { $nNew++ }
    if ($d -eq "reminder") { $nRem++ }
}
Check "long storm: 1 new"       $nNew 1
Check "long storm: 1 reminder"  $nRem 1
Check "long storm: count"       $st2.keys["fan"].Count 126

$st3 = @{ keys = @{}; lastShutdownAt = $null }
$k = Get-DedupKey "Evosep One: No Evotip was present"
Check "evotip 02:04 new"        (Get-AlertDecision -State $st3 -Key $k -At ([datetime]"2026-10-24 02:04:24")) "new"
Check "evotip 02:06 folds"      (Get-AlertDecision -State $st3 -Key $k -At ([datetime]"2026-10-24 02:06:00")) "fold"
Check "evotip 03:15 new again"  (Get-AlertDecision -State $st3 -Key $k -At ([datetime]"2026-10-24 03:15:00")) "new"
Check "new resets count"        $st3.keys[$k].Count 1
Check "different key is new"    (Get-AlertDecision -State $st3 -Key "other" -At ([datetime]"2026-10-24 03:15:30")) "new"

# state survives a JSON round trip (PS 5.1 loads PSCustomObjects + string dates)
$json = ConvertTo-Json $st2 -Depth 6
$loaded = ConvertTo-WatchState (ConvertFrom-Json $json)
Check "roundtrip: key kept"     $loaded.keys.ContainsKey("fan") "True"
Check "roundtrip: count"        $loaded.keys["fan"].Count 126
Check "roundtrip: reminder"     $loaded.keys["fan"].ReminderSent "True"
Check "roundtrip: LastAt type"  ($loaded.keys["fan"].LastAt -is [datetime]) "True"
Check "roundtrip: decision"     (Get-AlertDecision -State $loaded -Key "fan" -At $loaded.keys["fan"].LastAt.AddMinutes(2)) "fold"
$strState = ConvertFrom-Json '{"keys":{"x":{"FirstAt":"2026-10-24T02:00:00","LastAt":"2026-10-24T02:01:00","Count":2,"ReminderSent":false}},"lastShutdownAt":"2026-10-24T02:00:00"}'
$ws = ConvertTo-WatchState $strState
Check "string dates: fold"      (Get-AlertDecision -State $ws -Key "x" -At ([datetime]"2026-10-24 02:03:00")) "fold"
Check "empty state ok"          (ConvertTo-WatchState $null).keys.Count 0

$rs = @{ keys = @{}; lastShutdownAt = [datetime]"2026-10-24 02:00:00" }
Check "restart 2 min after shutdown expected"  (Test-UnexpectedRestart -State $rs -At ([datetime]"2026-10-24 02:02:00")) "False"
Check "restart 30 min after shutdown is not"   (Test-UnexpectedRestart -State $rs -At ([datetime]"2026-10-24 02:30:00")) "True"
Check "restart with no shutdown ever"          (Test-UnexpectedRestart -State @{ keys = @{}; lastShutdownAt = $null } -At ([datetime]"2026-10-24 02:30:00")) "True"

# ------------------------------------------------------------- 13. vial lookup
Write-Host ""
Write-Host "13. Evosep vial lookup"
$procRoot = Join-Path $repoRoot "tests/fixtures/evosep_procedures"
Check "vial at 02:04:24"        (Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 02:04:24")) "S1-A5"
Check "newest folder > 90 min"  ((Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 04:00:00")) -eq $null) "True"
Check "older folder, its vial"  (Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-23 20:10:00")) "S1-B3"
Check "start after At ignored"  ((Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 02:00:00")) -eq $null) "True"
Check "missing root -> null"    ((Get-EvosepVial -ProcedureRoot (Join-Path $repoRoot "nope") -At ([datetime]"2026-10-24 02:04:24")) -eq $null) "True"

# --------------------------------------------------------- 14. sample (tail)
Write-Host ""
Write-Host "14. sample name from the HyStar logbook (best effort)"
$tmpLog = Join-Path ([System.IO.Path]::GetTempPath()) ("hystar_sample_" + [guid]::NewGuid().ToString("N") + ".log")
$lines = "2026/10/24 01:50:00,100 something else`r`n2026/10/24 02:03:40,100 PHASE.INJECTED D:\Data\10212026_HE50_100-spd-dia_S1-A5_1_4821.d`r`n"
[System.IO.File]::WriteAllText($tmpLog, $lines, [System.Text.Encoding]::Unicode)
Check "sample found"            (Get-HyStarSample -LogbookPath $tmpLog -At ([datetime]"2026-10-24 02:04:24")) "10212026_HE50_100-spd-dia"
Check "too old -> null"         ((Get-HyStarSample -LogbookPath $tmpLog -At ([datetime]"2026-10-24 03:30:00")) -eq $null) "True"
Remove-Item -LiteralPath $tmpLog -Force
Check "missing log -> null"     ((Get-HyStarSample -LogbookPath $tmpLog -At ([datetime]"2026-10-24 02:04:24")) -eq $null) "True"

# ------------------------------------------------------------- 15. payloads
Write-Host ""
Write-Host "15. Slack payloads"
$dot = [string][char]0x00B7
$dash = [string][char]0x2014
$ev = @{
    Category = "evotip"; Title = "No Evotip"; At = [datetime]"2026-10-24 02:04:24"
    Message = "Evosep One: No Evotip was present during the analysis and the sample was aborted. Please check if the Evotip position in the autosampler matches the sample list."
    Vial = "S1-A5"; Sample = "10212026_HE50_100-spd-dia"
}
$pl = Format-StopPayload -Event $ev -MemberId "U0ABC1234" -Host "TIMS-10878"
Check "text" $pl.text "<@U0ABC1234> :red_circle: TIMS-10878 queue STOPPED $dash No Evotip $dot S1-A5 $dot 10212026_HE50_100-spd-dia $dot 02:04"
Check "block 1" $pl.blocks[0].text.text "<@U0ABC1234> :red_circle: *TIMS-10878 queue STOPPED $dash No Evotip*`n*S1-A5 $dot 10212026_HE50_100-spd-dia $dot 02:04*"
Check "block 2 quote"   $pl.blocks[1].text.text.StartsWith("> Evosep One: No Evotip was present") "True"
Check "block 2 check"   $pl.blocks[1].text.text.Contains("*Check:* the Evotip in S1-A5, and that the tray matches the sample list. The queue stays stopped until someone restarts it.") "True"
Check "block 2 footer"  $pl.blocks[1].text.text.Contains("_HyStar stopped the queue at 2026-10-24 02:04:24 $dot posted by the TIMS-10878 watcher_") "True"
Check "context"         $pl.blocks[2].elements[0].text "<https://ucd.stan-proteomics.org|STAN dashboard> $dot Maintenance tab"
Check "block count"     $pl.blocks.Count 3

$ev2 = @{ Category = "evotip"; Title = "No Evotip"; At = [datetime]"2026-10-24 02:04:24"; Message = "m"; Vial = $null; Sample = $null }
$pl2 = Format-StopPayload -Event $ev2 -MemberId "U0ABC1234" -Host "TIMS-10878"
Check "no vial/sample text" $pl2.text "<@U0ABC1234> :red_circle: TIMS-10878 queue STOPPED $dash No Evotip $dot 02:04"
Check "no vial: R11 check"  $pl2.blocks[1].text.text.Contains("*Check:* the Evotip position, and that the tray matches the sample list. The queue stays stopped until someone restarts it.") "True"
$pl3 = Format-StopPayload -Event $ev2 -MemberId "" -Host "TIMS-10878"
Check "no member: no mention" $pl3.text ":red_circle: TIMS-10878 queue STOPPED $dash No Evotip $dot 02:04"
$ev3 = @{ Category = "overpressure"; Title = "Pressure limit"; At = [datetime]"2026-10-24 02:04:24"; Message = "p"; Vial = "S1-A5"; Sample = $null }
$pl4 = Format-StopPayload -Event $ev3 -MemberId "U1" -Host "TIMS-10878"
Check "generic check" $pl4.blocks[1].text.text.Contains("`nThe queue stays stopped until someone restarts it.`n") "True"
Check "identity vial only" $pl4.text.EndsWith("Pressure limit $dot S1-A5 $dot 02:04") "True"

$rem = Format-ReminderPayload -Event @{ Title = "No Evotip"; Count = 126; FirstAt = [datetime]"2026-10-24 02:04:24" } -MemberId "U1"
Check "reminder" $rem.text "<@U1> :red_circle: TIMS-10878 still stopping $dash No Evotip: 126 times since 02:04"
$rst = Format-RestartPayload -Event @{ At = [datetime]"2026-10-24 09:05:00" } -MemberId "U1"
Check "restart" $rst.text "<@U1> :red_circle: TIMS-10878 HyStar restarted $dash no shutdown logged $dot 09:05"
$hq = Format-HiveQuietPayload -At ([datetime]"2026-10-24 09:05:00")
Check "hive quiet" $hq.text ":large_orange_diamond: STAN's Hive alerting has gone quiet $dash last check 09:05"
Check "hive back" (Format-HiveQuietPayload -Back).text ":white_check_mark: STAN's Hive alerting is back"
Check "payload is JSON-able" ((ConvertTo-Json $pl -Depth 8).Contains("U0ABC1234")) "True"

# identity line must equal Python identity_line
$py = & python3 -c "from datetime import datetime; from stan.reports.injection_identity import identity_line as i; print(i('S1-A5','smp',datetime(2026,10,24,2,4)))" 2>$null
if ($LASTEXITCODE -eq 0 -and $py) {
    Check "identity equals Python" ([string]$py).Trim() (Get-IdentityLine "S1-A5" "smp" ([datetime]"2026-10-24 02:04:00"))
}

# ---------------------------------------------------------------- 16. ASCII
Write-Host ""
Write-Host "16. shipped script is pure ASCII, no BOM"
$bytes = [System.IO.File]::ReadAllBytes((Join-Path $repoRoot $scriptRel))
$bad = 0
foreach ($b in $bytes) { if ($b -gt 0x7F) { $bad++ } }
Check "no byte > 0x7F"  $bad 0
$tb = [System.IO.File]::ReadAllBytes((Join-Path $repoRoot "tests/test_hystar_watcher.ps1"))
$bad = 0
foreach ($b in $tb) { if ($b -gt 0x7F) { $bad++ } }
Check "test file ASCII" $bad 0

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
