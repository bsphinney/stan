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

# =============================================================================
# Task 8: the tick. Each case gets its own temp folders for the state dir, the
# share, the logbook folder and Hive's logs. The Slack sender is a fake
# scriptblock that records every payload; the real Send-SlackPayload is tested
# against a local HTTP listener in section 27.

$ScriptVersion = $shipped    # the tick stamps watcher_version and the heartbeat with it
$TickRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("hystar_tick_" + [guid]::NewGuid().ToString("N"))
[void][System.IO.Directory]::CreateDirectory($TickRoot)
$DC3 = [string][char]0x13
$DC1 = [string][char]0x11
$Inv = [System.Globalization.CultureInfo]::InvariantCulture
$EvotipMsg = "Evosep One: No Evotip was present during the analysis and the sample was aborted.`nPlease check if the Evotip position in the autosampler matches the sample list."
$PressureMsg = "Evosep One: pressure limit exceeded"
$FanMsg = "Evosep One: fan detection fault"

function LogTime([datetime]$at) { return $at.ToString("yyyy/MM/dd HH:mm:ss", $Inv) }
function Rec-Header { return "$($DC3)H:################## BDAL Logbook #################$($DC1)`r`n" }
function Rec-Session([datetime]$at) {
    return "$($DC3)S:$(LogTime $at),063 ++++++++ Start of error logging for Bruker Compass HyStar 6.3 ++++++++$($DC1)`r`n"
}
function Rec-Noise([datetime]$at) {
    return "$($DC3)E:$(LogTime $at),100 Non-critical error. Error code #6000 from Bruker OTOF MS -> 'The source is not ready'$($DC1)`r`n"
}
function Rec-Stop([datetime]$at, [string]$msg) {
    return "$($DC3)E:$(LogTime $at),704 !!! EmergencyShutdown Source=ApplicationException Message='Critical Error in ICF System - '$msg'' !!!$($DC1)`r`n"
}
function Write-Logbook([string]$path, [string]$text) {
    $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
    try {
        $fs.WriteByte(0xFF); $fs.WriteByte(0xFE)
        $b = [System.Text.Encoding]::Unicode.GetBytes($text)
        $fs.Write($b, 0, $b.Length)
    } finally { $fs.Dispose() }
}
function Add-Logbook([string]$path, [string]$text) {
    $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
    try {
        $b = [System.Text.Encoding]::Unicode.GetBytes($text)
        $fs.Write($b, 0, $b.Length)
    } finally { $fs.Dispose() }
}
function New-TickDirs([string]$name) {
    $root = Join-Path $TickRoot $name
    $d = @{
        Root = $root; State = (Join-Path $root "state"); Share = (Join-Path $root "share")
        Logs = (Join-Path $root "LogFiles"); Hive = (Join-Path $root "hivelogs"); Proc = $procRoot
    }
    foreach ($k in @("Share", "Logs", "Hive")) { [void][System.IO.Directory]::CreateDirectory($d[$k]) }
    $d.Logbook = Join-Path $d.Logs "CompassHyStarErrorLogbook.log"
    return $d
}
$script:Posts = New-Object System.Collections.ArrayList
$script:SendOk = $true
$script:SendStatus = 200
$FakeSender = { param($p) [void]$script:Posts.Add($p); return @{ Ok = $script:SendOk; Status = $script:SendStatus } }
function Reset-Posts { $script:Posts.Clear(); $script:SendOk = $true; $script:SendStatus = 200 }
function Run-Tick($d, [datetime]$now, $sender = $FakeSender) {
    return Invoke-WatchTick -StateDir $d.State -LogbookPath $d.Logbook -ShareDir $d.Share `
        -ProcedureRoot $d.Proc -HiveLogsDir $d.Hive -Now $now -Sender $sender `
        -MemberId "U0ABC1234" -HostName "TIMS-10878"
}
function Get-EventFiles([string]$dir) {
    $out = New-Object System.Collections.ArrayList
    if ([System.IO.Directory]::Exists($dir)) {
        $names = [System.IO.Directory]::GetFiles($dir, "*.json")
        [Array]::Sort($names, [System.StringComparer]::Ordinal)
        foreach ($f in $names) {
            $n = [System.IO.Path]::GetFileName($f)
            if ($n -ne "heartbeat.json" -and $n -ne "pause_until.json") { [void]$out.Add($f) }
        }
    }
    return ,$out.ToArray()
}
function Read-JsonFile([string]$path) { return (ConvertFrom-Json ([System.IO.File]::ReadAllText($path))) }
# A top-level field exactly as written: pwsh 7's ConvertFrom-Json turns ISO
# strings into [datetime], which would hide the format being checked.
function JStr([string]$path, [string]$name) {
    $doc = [System.Text.Json.JsonDocument]::Parse([System.IO.File]::ReadAllText($path))
    try {
        $el = $doc.RootElement.GetProperty($name)
        if ($el.ValueKind -eq [System.Text.Json.JsonValueKind]::Null) { return "<null>" }
        if ($el.ValueKind -eq [System.Text.Json.JsonValueKind]::String) { return $el.GetString() }
        return $el.GetRawText()
    } finally { $doc.Dispose() }
}
function Count-Posts([string]$like) {
    $n = 0
    foreach ($p in $script:Posts) { if ($p.text -like $like) { $n++ } }
    return $n
}
function Read-WatchLogText($d) {
    $p = Join-Path $d.State "watch.log"
    if ([System.IO.File]::Exists($p)) { return [System.IO.File]::ReadAllText($p) }
    return ""
}
function File-Length([string]$path) { return (New-Object System.IO.FileInfo $path).Length }
function Event-Types([string]$dir) {
    $t = @()
    foreach ($f in (Get-EventFiles $dir)) { $t += (JStr $f "type") }
    return ($t -join ",")
}

# ------------------------------------------------- 17. offset round trip (R10)
Write-Host ""
Write-Host "17. offset round trip (R10): no record repeated, none missed"
$d = New-TickDirs "roundtrip"
$t = [datetime]"2026-10-24 01:00:00"
$recs = @((Rec-Stop $t $PressureMsg), (Rec-Noise $t.AddMinutes(1)), (Rec-Stop $t.AddMinutes(2) $FanMsg),
    (Rec-Session $t.AddMinutes(3)), (Rec-Stop $t.AddMinutes(4) "Evosep One: mcu reset"))
$wantHeads = @()
foreach ($x in $recs) { $wantHeads += (Clean-LogRecord $x).Substring(0, 21) }
Write-Logbook $d.Logbook ($recs[0] + $recs[1].Substring(0, 30))
$script:rtOff = 0
$script:rtSeen = New-Object System.Collections.ArrayList
function Pump-Logbook {
    $r = Read-NewLogText -Path $d.Logbook -Offset $script:rtOff
    $s = Split-LogRecords -Text $r.Text
    foreach ($x in $s.Records) { [void]$script:rtSeen.Add($x.Substring(0, 21)) }
    $script:rtOff = Get-NextOffset -Read $r -ConsumedChars $s.ConsumedChars
    return $s.Records.Count
}
Check "pump 1: the whole record only"      (Pump-Logbook) 1
Check "offset 0 -> BOM + record 1 bytes"    $script:rtOff (2 + 2 * $recs[0].Length)
Add-Logbook $d.Logbook ($recs[1].Substring(30) + $recs[2] + $recs[3].Substring(0, 10))
Check "pump 2: held record + the next"     (Pump-Logbook) 2
Add-Logbook $d.Logbook $recs[3].Substring(10)
Check "pump 3: the second held record"     (Pump-Logbook) 1
Add-Logbook $d.Logbook $recs[4]
Check "pump 4: the appended record"        (Pump-Logbook) 1
Check "pump 5: nothing new"                (Pump-Logbook) 0
Check "every record once, in order"        ($script:rtSeen -join "|") ($wantHeads -join "|")
Check "offset ends at the file length"     $script:rtOff (File-Length $d.Logbook)

# ------------------------------------------------------- 18. first run seeds
Write-Host ""
Write-Host "18. tick: first run starts at the end and sends nothing"
$d = New-TickDirs "evotip"
Write-Logbook $d.Logbook ((Rec-Header) + (Rec-Session ([datetime]"2026-10-20 08:00:00")) + (Rec-Stop ([datetime]"2026-10-23 22:00:00") $EvotipMsg))
Reset-Posts
$threw = $false
try { $null = Run-Tick $d ([datetime]"2026-10-24 02:00:00") } catch { $threw = $true; Write-Host "    $($_.Exception.Message)" }
Check "first tick does not throw"     $threw "False"
Check "first run: nothing sent"       $script:Posts.Count 0
$stPath = Join-Path $d.State "state.json"
Check "first run: state saved"        ([System.IO.File]::Exists($stPath)) "True"
if ([System.IO.File]::Exists($stPath)) {
    Check "first run: offset = length" (JStr $stPath "offset") (File-Length $d.Logbook)
}
Check "first run: no event files"     (Get-EventFiles $d.Share).Count 0
Check "first run: heartbeat written"  ([System.IO.File]::Exists((Join-Path $d.Share "heartbeat.json"))) "True"

# ------------------------------------------------- 19. a new Evotip stop
Write-Host ""
Write-Host "19. tick: a new Evotip stop is posted once, with its vial, and filed"
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:04:24") $EvotipMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 02:05:09")
Check "sender called once"   $script:Posts.Count 1
if ($script:Posts.Count -gt 0) {
    Check "posted text" ($script:Posts[0].text -like "<@U0ABC1234> :red_circle: TIMS-10878 queue STOPPED*No Evotip*S1-A5*02:04") "True"
}
$files = Get-EventFiles $d.Share
Check "one event file"       $files.Count 1
if ($files.Count -ge 1) {
    $f = $files[0]
    $ev = Read-JsonFile $f
    Check "file name is the id"      ([System.IO.Path]::GetFileName($f)) "$(JStr $f 'id').json"
    Check "id"                       (JStr $f "id") "TIMS-10878_20261024T020424_1"
    Check "schema|type|host|station" "$(JStr $f 'schema')|$(JStr $f 'type')|$(JStr $f 'host')|$(JStr $f 'station')" "1|stop|TIMS-10878|tims10878"
    Check "at (local, naive ISO)"    (JStr $f "at") "2026-10-24T02:04:24"
    Check "source"                   (JStr $f "source") "ApplicationException"
    Check "category|title"           "$(JStr $f 'category')|$(JStr $f 'title')" "evotip|No Evotip"
    Check "message unwrapped"        (JStr $f "message").StartsWith("Evosep One: No Evotip was present") "True"
    Check "vial from the procedure"  (JStr $f "vial") "S1-A5"
    Check "sample null (no logbook)" (JStr $f "sample") "<null>"
    Check "dedup_key"                (JStr $f "dedup_key") (Get-DedupKey (JStr $f "message"))
    Check "count|first_at"           "$(JStr $f 'count')|$(JStr $f 'first_at')" "1|2026-10-24T02:04:24"
    Check "posted_by_pc true"        (JStr $f "posted_by_pc") "true"
    Check "posted_at"                (JStr $f "posted_at") "2026-10-24T02:05:09"
    Check "watcher_version"          (JStr $f "watcher_version") $ScriptVersion
    if ($script:Posts.Count -gt 0) {
        Check "payload is what was posted" $ev.payload.text $script:Posts[0].text
    }
    Check "blocks survive JSON depth (R14)" $ev.payload.blocks[2].elements[0].text "<https://ucd.stan-proteomics.org|STAN dashboard> $dot Maintenance tab"
    $raw = [System.IO.File]::ReadAllBytes($f)
    Check "UTF-8 without a BOM"      (($raw[0] -eq 0xEF) -and ($raw[1] -eq 0xBB)) "False"
    $hasDot = $false
    for ($i = 0; $i -lt $raw.Length - 1; $i++) { if ($raw[$i] -eq 0xC2 -and $raw[$i + 1] -eq 0xB7) { $hasDot = $true } }
    Check "middle dot written as UTF-8" $hasDot "True"
}
Check "no temp files left on the share" ([System.IO.Directory]::GetFiles($d.Share, "*.tmp").Count) 0
$hbPath = Join-Path $d.Share "heartbeat.json"
Check "heartbeat exists"          ([System.IO.File]::Exists($hbPath)) "True"
if ([System.IO.File]::Exists($hbPath)) {
    Check "heartbeat at"            (JStr $hbPath "at") "2026-10-24T02:05:09"
    Check "heartbeat utc"           (JStr $hbPath "utc") (([datetime]"2026-10-24 02:05:09").ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", $Inv))
    Check "heartbeat version"       (JStr $hbPath "version") $ScriptVersion
    Check "heartbeat logbook"       (JStr $hbPath "logbook") $d.Logbook
    Check "heartbeat offset = length" (JStr $hbPath "offset") (File-Length $d.Logbook)
    Check "heartbeat last_error"    (JStr $hbPath "last_error") "<null>"
    $hbRaw = [System.IO.File]::ReadAllBytes($hbPath)
    Check "heartbeat has no BOM"    (($hbRaw[0] -eq 0xEF) -and ($hbRaw[1] -eq 0xBB)) "False"
}
Check "watch.log carries the version" ((Read-WatchLogText $d).Contains("v$ScriptVersion")) "True"
$null = Run-Tick $d ([datetime]"2026-10-24 02:06:09")
Check "second tick sends nothing"  $script:Posts.Count 1
Check "second tick files nothing"  (Get-EventFiles $d.Share).Count 1
$evotipShare = $d.Share

# ------------------------------------------------------------ 20. rotation
Write-Host ""
Write-Host "20. tick: rotation between ticks, and while a record is half-written"
# (a) a record lands, then HyStar rotates and starts a new logbook
$d = New-TickDirs "rotate"
Write-Logbook $d.Logbook ((Rec-Header) + (Rec-Session ([datetime]"2026-10-20 08:00:00")))
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 02:00:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:08:00") $PressureMsg)
Move-Item -LiteralPath $d.Logbook -Destination (Join-Path $d.Logs "CompassHyStarErrorLogbook_2026_10_24_02_10_00.log")
Write-Logbook $d.Logbook ((Rec-Header) + (Rec-Session ([datetime]"2026-10-24 02:10:00")) + (Rec-Stop ([datetime]"2026-10-24 02:10:30") $EvotipMsg))
$null = Run-Tick $d ([datetime]"2026-10-24 02:11:00")
Check "(a) both records delivered"   $script:Posts.Count 2
if ($script:Posts.Count -ge 2) {
    Check "(a) old file's record first" ($script:Posts[0].text -like "*Pressure limit*") "True"
    Check "(a) new file's record next"  ($script:Posts[1].text -like "*No Evotip*") "True"
}
$null = Run-Tick $d ([datetime]"2026-10-24 02:12:00")
Check "(a) next tick: nothing again" $script:Posts.Count 2
Check "(a) two event files"          (Get-EventFiles $d.Share).Count 2

# (b) the record is half-written at one tick; HyStar finishes it and rotates
$d = New-TickDirs "rotate_half"
Write-Logbook $d.Logbook ((Rec-Header) + (Rec-Session ([datetime]"2026-10-20 08:00:00")))
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 02:00:00")
$half = Rec-Stop ([datetime]"2026-10-24 02:08:00") $PressureMsg
Add-Logbook $d.Logbook $half.Substring(0, 60)
$null = Run-Tick $d ([datetime]"2026-10-24 02:08:30")
Check "(b) half a record: nothing yet" $script:Posts.Count 0
Add-Logbook $d.Logbook $half.Substring(60)
Move-Item -LiteralPath $d.Logbook -Destination (Join-Path $d.Logs "CompassHyStarErrorLogbook_2026_10_24_02_10_00.log")
Write-Logbook $d.Logbook ((Rec-Header) + (Rec-Session ([datetime]"2026-10-24 02:10:00")) + (Rec-Stop ([datetime]"2026-10-24 02:10:30") $EvotipMsg))
$null = Run-Tick $d ([datetime]"2026-10-24 02:11:00")
Check "(b) both delivered once"        $script:Posts.Count 2
if ($script:Posts.Count -ge 2) {
    Check "(b) in order" "$($script:Posts[0].text -like '*Pressure limit*')|$($script:Posts[1].text -like '*No Evotip*')" "True|True"
}
$null = Run-Tick $d ([datetime]"2026-10-24 02:12:00")
Check "(b) next tick: nothing again"   $script:Posts.Count 2

# (c) the new logbook is already longer than the old offset: the changed
# first bytes are what reveal the rotation
$d = New-TickDirs "rotate_fp"
Write-Logbook $d.Logbook ((Rec-Header) + (Rec-Session ([datetime]"2026-10-20 08:00:00")))
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 02:55:00")
$oldLen = File-Length $d.Logbook
Move-Item -LiteralPath $d.Logbook -Destination (Join-Path $d.Logs "CompassHyStarErrorLogbook_2026_10_24_03_00_00.log")
$newText = Rec-Header
for ($i = 0; $i -lt 6; $i++) { $newText += (Rec-Noise ([datetime]"2026-10-24 03:00:00").AddSeconds(5 * $i)) }
$newText += (Rec-Stop ([datetime]"2026-10-24 03:01:00") $PressureMsg)
Write-Logbook $d.Logbook $newText
Check "(c) new file longer than old offset" ((File-Length $d.Logbook) -gt $oldLen) "True"
$null = Run-Tick $d ([datetime]"2026-10-24 03:02:00")
Check "(c) the stop is delivered once"  $script:Posts.Count 1
$null = Run-Tick $d ([datetime]"2026-10-24 03:03:00")
Check "(c) next tick: nothing again"    $script:Posts.Count 1

# (d) a logbook seeded under 200 bytes that then grows is not a rotation
$d = New-TickDirs "small_seed"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 04:00:00")
Add-Logbook $d.Logbook ((Rec-Session ([datetime]"2026-10-24 04:00:30")) + (Rec-Stop ([datetime]"2026-10-24 04:01:00") $PressureMsg))
$null = Run-Tick $d ([datetime]"2026-10-24 04:02:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 04:20:00") $PressureMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 04:21:00")
Check "(d) growing small file: each stop once" (Count-Posts "*queue STOPPED*") 2

# (e) the first bytes change in place (a false rotation, no rotated file to
# finish): the file is read again from 0, and the lastRecordAt backstop keeps
# what was already sent from going out twice. A repeated stop would fold on
# its own key anyway; the unexpected restart is what only the backstop stops.
$d = New-TickDirs "false_rotation"
Write-Logbook $d.Logbook ((Rec-Header) + (Rec-Session ([datetime]"2026-10-20 08:00:00")))
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 05:00:00")
Add-Logbook $d.Logbook ((Rec-Stop ([datetime]"2026-10-24 05:10:00") $PressureMsg) + (Rec-Session ([datetime]"2026-10-24 05:30:00")))
$null = Run-Tick $d ([datetime]"2026-10-24 05:31:00")
Check "(e) stop + unexpected restart sent"   "$(Count-Posts '*queue STOPPED*')|$(Count-Posts '*HyStar restarted*')" "1|1"
$bytes = [System.IO.File]::ReadAllBytes($d.Logbook)
$bytes[10] = [byte][char]'*'
[System.IO.File]::WriteAllBytes($d.Logbook, $bytes)
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 05:40:00") $FanMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 05:41:00")
Check "(e) re-read from 0: only the new stop" "$($script:Posts.Count)|$($script:Posts[$script:Posts.Count - 1].text -like '*Evosep hardware fault*')" "3|True"

# ----------------------------------------------------- 21. webhook refused
Write-Host ""
Write-Host "21. tick: webhook refused -> posted_by_pc false; the log names the status, never the URL"
$d = New-TickDirs "refused"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 02:00:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:04:24") $EvotipMsg)
$script:SendOk = $false
$script:SendStatus = 403
$null = Run-Tick $d ([datetime]"2026-10-24 02:05:00")
Check "post was attempted"          $script:Posts.Count 1
$files = Get-EventFiles $d.Share
Check "event file written"          $files.Count 1
if ($files.Count -ge 1) {
    Check "posted_by_pc false"        (JStr $files[0] "posted_by_pc") "false"
    Check "posted_at null"            (JStr $files[0] "posted_at") "<null>"
}
$log = Read-WatchLogText $d
Check "watch.log names 403"         $log.Contains("403") "True"
Check "watch.log has no webhook"    $log.Contains("hooks.slack.com") "False"
# a sender that throws with the URL in its message
$Thrower = { param($p) throw "POST https://hooks.slack.com/services/T0AAAA/B0BBBB/SECRET123 failed: no route" }
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:20:00") $PressureMsg)
$threw = $false
try { $null = Run-Tick $d ([datetime]"2026-10-24 02:21:00") $Thrower } catch { $threw = $true }
Check "throwing sender: tick does not throw" $threw "False"
$files = Get-EventFiles $d.Share
Check "throwing sender: event filed"         $files.Count 2
if ($files.Count -ge 2) {
    $pf = $null
    foreach ($ff in $files) { if ((JStr $ff "category") -eq "overpressure") { $pf = $ff } }
    Check "throwing sender: posted_by_pc false" (JStr $pf "posted_by_pc") "false"
}
$log = Read-WatchLogText $d
Check "throwing sender: log has no webhook"  $log.Contains("hooks.slack.com") "False"
Check "throwing sender: log has no secret"   $log.Contains("SECRET123") "False"
Check "throwing sender: log says it failed"  $log.Contains("no route") "True"
$hbText = [System.IO.File]::ReadAllText((Join-Path $d.Share "heartbeat.json"))
Check "heartbeat has no webhook"             $hbText.Contains("hooks.slack.com") "False"
$refusedShare = $d.Share
# one failed post per tick: the rest of the tick is filed for Hive to relay
$d = New-TickDirs "refused_many"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 02:00:00")
Add-Logbook $d.Logbook ((Rec-Stop ([datetime]"2026-10-24 02:04:00") $EvotipMsg) + (Rec-Stop ([datetime]"2026-10-24 02:04:10") $PressureMsg) + (Rec-Stop ([datetime]"2026-10-24 02:04:20") $FanMsg))
$script:SendOk = $false
$script:SendStatus = "WebException: The operation has timed out"
$null = Run-Tick $d ([datetime]"2026-10-24 02:05:00")
Check "no route: one post tried per tick"   $script:Posts.Count 1
$unposted = 0
foreach ($ff in (Get-EventFiles $d.Share)) { if ((JStr $ff "posted_by_pc") -eq "false") { $unposted++ } }
Check "no route: all three filed for Hive"  $unposted 3
$script:SendOk = $true
$script:SendStatus = 200
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:10:00") $EvotipMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 02:11:00")
Check "next tick tries again"               "$($script:Posts.Count)|$(JStr (Get-EventFiles $d.Share)[3] 'posted_by_pc')" "2|true"

# ---------------------------------------------------------- 22. share down
Write-Host ""
Write-Host "22. tick: share down -> queued locally, written once when it is back, never re-posted"
$d = New-TickDirs "sharedown"
$blocker = Join-Path $d.Root "blocked"
[System.IO.File]::WriteAllText($blocker, "a file where the share folder's parent should be")
$d.Share = Join-Path $blocker "TIMS-10878"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$threw = $false
try { $null = Run-Tick $d ([datetime]"2026-10-24 02:00:00") } catch { $threw = $true }
Check "share down: tick does not throw" $threw "False"
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:04:24") $EvotipMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 02:05:00")
Check "share down: still posted directly" $script:Posts.Count 1
$pendingDir = Join-Path $d.State "pending"
$q = @()
if ([System.IO.Directory]::Exists($pendingDir)) { $q = [System.IO.Directory]::GetFiles($pendingDir, "*.json") }
Check "share down: one event queued"      $q.Count 1
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:20:00") $PressureMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 02:21:00")
$q = [System.IO.Directory]::GetFiles($pendingDir, "*.json")
Check "share down: two events queued"     $q.Count 2
Check "share down: two posts"             $script:Posts.Count 2
[System.IO.File]::Delete($blocker)
[void][System.IO.Directory]::CreateDirectory($d.Share)
$null = Run-Tick $d ([datetime]"2026-10-24 02:22:00")
$files = Get-EventFiles $d.Share
Check "share back: both events written"   $files.Count 2
Check "share back: queue empty"           ([System.IO.Directory]::GetFiles($pendingDir, "*.json").Count) 0
Check "share back: no re-post"            $script:Posts.Count 2
$allPosted = $true
foreach ($ff in $files) { if ((JStr $ff "posted_by_pc") -ne "true") { $allPosted = $false } }
Check "share back: posted_by_pc kept true" $allPosted "True"
Check "share back: heartbeat written"      ([System.IO.File]::Exists((Join-Path $d.Share "heartbeat.json"))) "True"
$null = Run-Tick $d ([datetime]"2026-10-24 02:23:00")
Check "next tick: no new files"           (Get-EventFiles $d.Share).Count 2
Check "next tick: no re-post"             $script:Posts.Count 2

# order: while one queued event still cannot be written, later ones queue
# behind it rather than overtake it (a folder squatting on its file name)
$d = New-TickDirs "queue_order"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 03:00:00")
$squat = Join-Path $d.Share "TIMS-10878_20261024T030424_1.json"
[void][System.IO.Directory]::CreateDirectory($squat)
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 03:04:24") $EvotipMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 03:05:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 03:20:00") $PressureMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 03:21:00")
$pendingDir = Join-Path $d.State "pending"
Check "order: the later event waits behind"  "$([System.IO.Directory]::GetFiles($pendingDir, '*.json').Count)|$((Get-EventFiles $d.Share).Count)" "2|0"
[System.IO.Directory]::Delete($squat)
$null = Run-Tick $d ([datetime]"2026-10-24 03:22:00")
Check "order: both written once unblocked"   "$([System.IO.Directory]::GetFiles($pendingDir, '*.json').Count)|$((Get-EventFiles $d.Share).Count)" "0|2"

# never rewritten: an event already on the share is left as it is
$d = New-TickDirs "no_rewrite"
$blocker = Join-Path $d.Root "blocked"
[System.IO.File]::WriteAllText($blocker, "x")
$realShare = $d.Share
$d.Share = Join-Path $blocker "TIMS-10878"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 04:00:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 04:04:24") $EvotipMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 04:05:00")
$queuedFile = [System.IO.Directory]::GetFiles((Join-Path $d.State "pending"), "*.json")[0]
$qid = JStr $queuedFile "id"
[System.IO.File]::WriteAllText((Join-Path $realShare "$qid.json"), '{"id": "already here"}')
$d.Share = $realShare
$null = Run-Tick $d ([datetime]"2026-10-24 04:06:00")
Check "never rewritten: share copy kept"    ([System.IO.File]::ReadAllText((Join-Path $realShare "$qid.json"))) '{"id": "already here"}'
Check "never rewritten: queue drained"      ([System.IO.Directory]::GetFiles((Join-Path $d.State "pending"), "*.json").Count) 0

# an unreadable queued file is set aside, not left to block the queue
$d = New-TickDirs "bad_pending"
$blocker = Join-Path $d.Root "blocked"
[System.IO.File]::WriteAllText($blocker, "x")
$realShare = $d.Share
$d.Share = Join-Path $blocker "TIMS-10878"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 06:00:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 06:04:24") $EvotipMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 06:05:00")
$queuedFile = [System.IO.Directory]::GetFiles((Join-Path $d.State "pending"), "*.json")[0]
[System.IO.File]::SetUnixFileMode($queuedFile, [System.IO.UnixFileMode]::None)
$d.Share = $realShare
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 06:20:00") $PressureMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 06:21:00")
Check "bad queued file: later event still filed" (Get-EventFiles $d.Share).Count 1
Check "bad queued file: set aside as .bad"       ([System.IO.Directory]::GetFiles((Join-Path $d.State "pending"), "*.bad").Count) 1
foreach ($bf in [System.IO.Directory]::GetFiles((Join-Path $d.State "pending"), "*.bad")) {
    [System.IO.File]::SetUnixFileMode($bf, [System.IO.UnixFileMode]::UserRead -bor [System.IO.UnixFileMode]::UserWrite)
}

# ------------------------------------------------------- 23. partial record
Write-Host ""
Write-Host "23. tick: a shutdown record with no closing !!! waits, then goes"
$d = New-TickDirs "partial"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 02:00:00")
Add-Logbook $d.Logbook "$($DC3)E:2026/10/24 02:04:24,704 !!! EmergencyShutdown Source=ApplicationException Message='Critical Error in ICF System - 'Evosep One: No Evotip was present$($DC1)`r`n"
$null = Run-Tick $d ([datetime]"2026-10-24 02:05:00")
Check "tick 1: not sent"          $script:Posts.Count 0
$null = Run-Tick $d ([datetime]"2026-10-24 02:06:00")
Check "1 min later: still held"   $script:Posts.Count 0
$null = Run-Tick $d ([datetime]"2026-10-24 02:08:00")
Check "3 min later: sent"         $script:Posts.Count 1
$null = Run-Tick $d ([datetime]"2026-10-24 02:09:00")
Check "then not again"            $script:Posts.Count 1

# ---------------------------------------------------------- 24. Hive quiet
Write-Host ""
Write-Host "24. tick: Hive's alerting quiet for 50 min -> one quiet note, then one back note"
$d = New-TickDirs "hive"
Write-Logbook $d.Logbook (Rec-Header)
$now = [datetime]"2026-10-24 09:00:00"
$hiveLog = Join-Path $d.Hive "cron_stan_alerts_20261024.log"
[System.IO.File]::WriteAllText($hiveLog, "tick")
[System.IO.File]::SetLastWriteTime($hiveLog, $now.AddMinutes(-50))
$other = Join-Path $d.Hive "cron_evosep_20261024.log"
[System.IO.File]::WriteAllText($other, "tick")
[System.IO.File]::SetLastWriteTime($other, $now.AddMinutes(-1))
Reset-Posts
$null = Run-Tick $d $now
Check "quiet: one note"            (Count-Posts "*gone quiet*") 1
Check "quiet: no @mention"         ((Count-Posts "*<@*") -eq 0) "True"
$null = Run-Tick $d $now.AddMinutes(1)
Check "quiet: still one note"      (Count-Posts "*gone quiet*") 1
[System.IO.File]::SetLastWriteTime($hiveLog, $now.AddMinutes(1.5))
$null = Run-Tick $d $now.AddMinutes(2)
Check "back: one note"             (Count-Posts "*is back*") 1
$null = Run-Tick $d $now.AddMinutes(3)
Check "back: no more"              $script:Posts.Count 2
Check "quiet + back filed"         (Event-Types $d.Share) "hive_quiet,hive_back"
# no sender (no webhook): nothing to say, nothing filed
$d = New-TickDirs "hive_nosender"
Write-Logbook $d.Logbook (Rec-Header)
$hiveLog = Join-Path $d.Hive "cron_stan_alerts_20261024.log"
[System.IO.File]::WriteAllText($hiveLog, "tick")
[System.IO.File]::SetLastWriteTime($hiveLog, $now.AddMinutes(-50))
$null = Run-Tick $d $now $null
$null = Run-Tick $d $now.AddMinutes(1) $null
Check "no sender: no hive note filed" (Get-EventFiles $d.Share).Count 0

# -------------------------------------------- 25. stop, restart, same stop (R13)
Write-Host ""
Write-Host "25. R13: the same stop after an injection restart pages again; a storm still folds"
$proc13 = Join-Path $TickRoot "procedures"
Copy-Item -LiteralPath $procRoot -Destination $proc13 -Recurse
$srcProc = Join-Path $proc13 "S00230/100-samples-per-day_2026-10-24_02-03-36"
$newProc = Join-Path $proc13 "S00230/100-samples-per-day_2026-10-24_02-06-24"
Copy-Item -LiteralPath $srcProc -Destination $newProc -Recurse
Check "injection since: one started in between" (Test-InjectionSince -ProcedureRoot $proc13 -Since ([datetime]"2026-10-24 02:04:24") -Until ([datetime]"2026-10-24 02:07:24")) "True"
Check "injection since: Until is inclusive"     (Test-InjectionSince -ProcedureRoot $proc13 -Since ([datetime]"2026-10-24 02:05:00") -Until ([datetime]"2026-10-24 02:06:24")) "True"
Check "injection since: Since is exclusive"     (Test-InjectionSince -ProcedureRoot $proc13 -Since ([datetime]"2026-10-24 02:06:24") -Until ([datetime]"2026-10-24 02:07:24")) "False"
Check "injection since: none in the window"     (Test-InjectionSince -ProcedureRoot $proc13 -Since ([datetime]"2026-10-24 02:30:00") -Until ([datetime]"2026-10-24 02:32:00")) "False"
Check "injection since: across midnight"        (Test-InjectionSince -ProcedureRoot $proc13 -Since ([datetime]"2026-10-23 23:58:00") -Until ([datetime]"2026-10-24 02:03:36")) "True"
Check "injection since: missing root"           (Test-InjectionSince -ProcedureRoot (Join-Path $TickRoot "nope") -Since ([datetime]"2026-10-24 02:04:24") -Until ([datetime]"2026-10-24 02:07:24")) "False"
$d = New-TickDirs "r13"
$d.Proc = $proc13
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 02:00:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:04:24") $EvotipMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 02:05:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:07:24") $EvotipMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 02:08:00")
Check "evotip, restart, evotip 3 min later: two pages" (Count-Posts "*queue STOPPED*No Evotip*") 2
Check "two stop events filed"                          (Event-Types $d.Share) "stop,stop"
$d = New-TickDirs "r13_storm"
$d.Proc = $proc13
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 02:29:00")
foreach ($m in @(30, 32, 34)) {
    Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:00:00").AddMinutes($m) $FanMsg)
    $null = Run-Tick $d ([datetime]"2026-10-24 02:00:30").AddMinutes($m)
}
Check "fan storm with no new injection: one page" $script:Posts.Count 1
$stTxt = [System.IO.File]::ReadAllText((Join-Path $d.State "state.json"))
Check "storm count kept in state" ($stTxt -match '"Count":\s*3') "True"

# DST fall-back (Sun 2026-11-01): local times repeat 01:00-02:00. The same
# stop at 01:58 PDT and again at 01:05 PST (67 min later) reads as a negative
# gap; it must page again, not fold
$d = New-TickDirs "dst"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-11-01 01:50:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-11-01 01:58:00") $PressureMsg)
$null = Run-Tick $d ([datetime]"2026-11-01 01:59:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-11-01 01:05:00") $PressureMsg)
$null = Run-Tick $d ([datetime]"2026-11-01 01:06:00")
Check "DST: the repeated hour's stop pages again" (Count-Posts "*queue STOPPED*") 2

# ------------------------------------------- 26. restart, and lastShutdownAt
Write-Host ""
Write-Host "26. tick: unexpected HyStar restart pages; one after a (folded) stop does not"
$d = New-TickDirs "restart"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 05:00:00")
Add-Logbook $d.Logbook (Rec-Session ([datetime]"2026-10-24 06:00:00"))
$null = Run-Tick $d ([datetime]"2026-10-24 06:01:00")
Check "restart with no shutdown: one page" (Count-Posts "*HyStar restarted*") 1
Add-Logbook $d.Logbook ((Rec-Stop ([datetime]"2026-10-24 07:00:00") $FanMsg) + (Rec-Stop ([datetime]"2026-10-24 07:03:00") $FanMsg) + (Rec-Session ([datetime]"2026-10-24 07:07:00")))
$null = Run-Tick $d ([datetime]"2026-10-24 07:08:00")
Check "stop + folded stop + restart 4 min later: one stop page only" "$(Count-Posts '*queue STOPPED*')|$(Count-Posts '*HyStar restarted*')" "1|1"
Check "lastShutdownAt is the folded stop" (JStr (Join-Path $d.State "state.json") "lastShutdownAt") "2026-10-24T07:03:00"
Check "restart event filed" ((Event-Types $d.Share) -like "*restart*") "True"
Add-Logbook $d.Logbook "$($DC3)E:2026/10/24 08:00:00,100 !!! EmergencyShutdown Source=PlugInCriticalError Message='' !!!$($DC1)`r`n"
$null = Run-Tick $d ([datetime]"2026-10-24 08:01:00")
Check "a shutdown with an empty message still pages" (Count-Posts "*queue STOPPED*Stopped*") 1

# ------------------------------------------------- 27. Send-SlackPayload
Write-Host ""
Write-Host "27. Send-SlackPayload against a local listener"
function Get-FreePort {
    $tl = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $tl.Start()
    $port = $tl.LocalEndpoint.Port
    $tl.Stop()
    return $port
}
function Start-FakeSlack([int]$code) {
    $port = Get-FreePort
    $listener = New-Object System.Net.HttpListener
    $listener.Prefixes.Add("http://127.0.0.1:$port/")
    $listener.Start()
    $box = [hashtable]::Synchronized(@{})
    $ps = [powershell]::Create()
    [void]$ps.AddScript('param($l, $code, $box)
        try {
            $ctx = $l.GetContext()
            $ms = New-Object System.IO.MemoryStream
            $ctx.Request.InputStream.CopyTo($ms)
            $box["body"] = $ms.ToArray()
            $box["ctype"] = $ctx.Request.ContentType
            $box["method"] = $ctx.Request.HttpMethod
            $ctx.Response.StatusCode = $code
            $b = [System.Text.Encoding]::UTF8.GetBytes("ok")
            $ctx.Response.OutputStream.Write($b, 0, $b.Length)
            $ctx.Response.Close()
        } catch { $box["error"] = $_.Exception.Message }')
    [void]$ps.AddArgument($listener).AddArgument($code).AddArgument($box)
    $h = $ps.BeginInvoke()
    return @{ Url = "http://127.0.0.1:$port/services/T0AAAA/B0BBBB/SECRETX"; Listener = $listener; PS = $ps; Handle = $h; Box = $box }
}
function Stop-FakeSlack($fs) {
    [void]$fs.Handle.AsyncWaitHandle.WaitOne(5000)
    try { $fs.Listener.Stop() } catch { }
    try { $fs.PS.Stop() } catch { }
    $fs.PS.Dispose()
}
$fake = Start-FakeSlack 200
$res = Send-SlackPayload -WebhookUrl $fake.Url -Payload $pl
Stop-FakeSlack $fake
Check "200: Ok"                   "$($res.Ok)|$($res.Status)" "True|200"
Check "POST"                      $fake.Box["method"] "POST"
Check "content type"              $fake.Box["ctype"] "application/json; charset=utf-8"
$body = [byte[]]$fake.Box["body"]
$hasDot = $false; $hasDash = $false
for ($i = 0; $i -lt $body.Length - 2; $i++) {
    if ($body[$i] -eq 0xC2 -and $body[$i + 1] -eq 0xB7) { $hasDot = $true }
    if ($body[$i] -eq 0xE2 -and $body[$i + 1] -eq 0x80 -and $body[$i + 2] -eq 0x94) { $hasDash = $true }
}
Check "body is UTF-8 (dot, dash)" "$hasDot|$hasDash" "True|True"
$sentDoc = ConvertFrom-Json ([System.Text.Encoding]::UTF8.GetString($body))
Check "blocks arrive whole (depth)" $sentDoc.blocks[2].elements[0].text "<https://ucd.stan-proteomics.org|STAN dashboard> $dot Maintenance tab"
$fake = Start-FakeSlack 403
$res = Send-SlackPayload -WebhookUrl $fake.Url -Payload $pl
Stop-FakeSlack $fake
Check "403: not Ok, status 403"   "$($res.Ok)|$($res.Status)" "False|403"
$closed = "http://127.0.0.1:$(Get-FreePort)/services/T0AAAA/B0BBBB/SECRETX"
$threw = $false; $res = $null
try { $res = Send-SlackPayload -WebhookUrl $closed -Payload $pl } catch { $threw = $true }
Check "no route: no exception"    $threw "False"
Check "no route: not Ok"          $res.Ok "False"
Check "no route: status says why" ([string]::IsNullOrEmpty([string]$res.Status)) "False"
Check "no route: status has no URL secret" ([string]$res.Status).Contains("SECRETX") "False"
$threw = $false; $res = $null
try { $res = Send-SlackPayload -WebhookUrl "not a url" -Payload $pl } catch { $threw = $true }
Check "garbage URL: no exception" "$threw|$($res.Ok)" "False|False"
Check "scrub: webhook replaced"   (Get-ScrubbedText "boom https://hooks.slack.com/services/T0/B0/XYZ end") "boom <webhook> end"
Check "scrub: exact secret too"   (Get-ScrubbedText "at http://127.0.0.1:9/x/SECRETX" -Secret "http://127.0.0.1:9/x/SECRETX") "at <webhook>"

# ------------------------------------- 28. Hive's reader takes the tick's files
Write-Host ""
Write-Host "28. Hive's reader (stan.reports.hystar_events) accepts what the tick wrote"
$pyHost = Join-Path $TickRoot "py/TIMS-10878"
[void][System.IO.Directory]::CreateDirectory($pyHost)
foreach ($ff in (Get-EventFiles $evotipShare)) { Copy-Item -LiteralPath $ff -Destination $pyHost }
foreach ($ff in (Get-EventFiles $refusedShare)) { Copy-Item -LiteralPath $ff -Destination (Join-Path $pyHost ("r_" + [System.IO.Path]::GetFileName($ff))) }
Copy-Item -LiteralPath (Join-Path $evotipShare "heartbeat.json") -Destination $pyHost
[System.IO.File]::SetLastWriteTime((Join-Path $pyHost "heartbeat.json"), (Get-Date))
$py = @'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
from stan.reports.hystar_events import (load_new_events, suppression_keys, check_watcher_heartbeat,
                                        _heartbeat_times, _relay_payload)
host = Path(sys.argv[1])
evs = load_new_events(host)
landed, reported = _heartbeat_times(host / "heartbeat.json")
unposted = [e for e in evs if e.get("posted_by_pc") is not True]
print(json.dumps({
    "n": len(evs),
    "rejected": (host / "rejected").exists(),
    "types": sorted(e["type"] for e in evs),
    "stop_keys": min(len(suppression_keys(e)) for e in evs if e["type"] == "stop"),
    "unposted": len(unposted),
    "relay_text_kept": all(_relay_payload(e)["text"] == e["payload"]["text"] for e in unposted),
    "relay_blocks": [len(_relay_payload(e)["blocks"]) for e in unposted],
    "reported": reported.strftime("%Y%m%d%H%M%S") if reported else None,
    "landed": landed is not None,
    "down_alerts": len(check_watcher_heartbeat(host, datetime.now(timezone.utc))),
}))
'@
$pyCode = 1
$pyOut = $null
$pyJson = $null
$eap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
Push-Location $repoRoot
try {
    $pyOut = & python3 -c $py $pyHost 2>&1
    $pyCode = $LASTEXITCODE
} catch { $pyOut = $_.Exception.Message }
Pop-Location
$ErrorActionPreference = $eap
foreach ($line in @($pyOut)) { if ("$line".StartsWith("{")) { $pyJson = "$line" } }
if ($pyCode -eq 0 -and $pyJson) {
    $pr = ConvertFrom-Json $pyJson
    Check "Hive reads every event"           "$($pr.n)|$($pr.rejected)" "3|False"
    Check "types"                            ($pr.types -join ",") "stop,stop,stop"
    Check "each stop marks hourly keys"      ($pr.stop_keys -gt 0) "True"
    Check "unposted ones are relayable"      "$($pr.unposted)|$($pr.relay_text_kept)" "2|True"
    Check "relay keeps the PC's 3 blocks + note" ($pr.relay_blocks -join ",") "4,4"
    Check "heartbeat time parsed"            "$($pr.landed)|$($pr.reported)" "True|20261024020609"
    Check "fresh heartbeat: no down page"    $pr.down_alerts 0
}
else {
    Write-Host "  skip Hive reader cross-check (python3 with stan not importable): $pyOut"
}

# ------------------------------------------------- 29. review fix round 1
Write-Host ""
Write-Host "29. every file the watcher opens lets HyStar rename it (FileShare Delete)"
$opens = @()
foreach ($c in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    if ($c.GetCommandName() -eq "New-Object" -and $c.Extent.Text -match "FileStream") { $opens += $c.Extent.Text }
}
foreach ($m in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
    $tx = $m.Expression.Extent.Text
    $mn = $m.Member.Extent.Text
    if (($tx -match "FileStream\]$" -and $mn -eq "new") -or ($tx -match "IO\.File\]$" -and $mn -match "^Open")) { $opens += $m.Extent.Text }
}
Check "file opens found (logbook, journal, sample log, fingerprint)" ($opens.Count -ge 4) "True"
$noDelete = 0
foreach ($o in $opens) {
    if ($o -notmatch "FileShare\]::Delete") { $noDelete++; Write-Host "    no Delete share: $(($o -split "`n")[0])" }
}
Check "every open shares Delete"  $noDelete 0

Write-Host ""
Write-Host "30. a tick that cannot save its state never posts a stop twice"
if ($null -ne (Get-Command chflags -ErrorAction SilentlyContinue)) {
    # chflags uchg: state.json stays readable but cannot be replaced or
    # deleted, like a file held by AV/backup or owned by an elevated install
    $d = New-TickDirs "nosave"
    Write-Logbook $d.Logbook (Rec-Header)
    Reset-Posts
    $null = Run-Tick $d ([datetime]"2026-10-24 02:00:00")
    $seedLen = File-Length $d.Logbook
    $stFile = Join-Path $d.State "state.json"
    & chflags uchg $stFile
    try {
        # Hive's alerting goes quiet too: that note must not repeat either
        $hiveLog = Join-Path $d.Hive "cron_stan_alerts_20261024.log"
        [System.IO.File]::WriteAllText($hiveLog, "tick")
        [System.IO.File]::SetLastWriteTime($hiveLog, ([datetime]"2026-10-24 01:00:00"))
        Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:04:24") $EvotipMsg)
        $null = Run-Tick $d ([datetime]"2026-10-24 02:05:00")
        $null = Run-Tick $d ([datetime]"2026-10-24 02:06:00")
        $null = Run-Tick $d ([datetime]"2026-10-24 02:07:00")
        Check "state really was not saved"           (JStr $stFile "offset") $seedLen
        Check "one stop post across three ticks"     (Count-Posts "*queue STOPPED*") 1
        Check "one Hive-quiet note across three"     (Count-Posts "*gone quiet*") 1
        [System.IO.File]::Delete($hiveLog)
        Reset-Posts
        Check "one event file each (stop, quiet)"    (Event-Types $d.Share) "stop,hive_quiet"
        $log = Read-WatchLogText $d
        Check "watch.log: state not saved"           $log.Contains("state not saved") "True"
        Check "watch.log: already delivered"         $log.Contains("already delivered") "True"
        Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 02:20:00") $PressureMsg)
        $null = Run-Tick $d ([datetime]"2026-10-24 02:21:00")
        $null = Run-Tick $d ([datetime]"2026-10-24 02:22:00")
        Check "a later stop still pages, once"       $script:Posts.Count 1
        Check "and is filed once"                    (Get-EventFiles $d.Share).Count 3
    }
    finally { & chflags nouchg $stFile }

    # posted, then the tick could not file it anywhere (queue folder blocked,
    # share down) and could not save: a later tick files it, posted_by_pc
    # true with the first post's time, and does not post again
    $d = New-TickDirs "posted_not_filed"
    Write-Logbook $d.Logbook (Rec-Header)
    Reset-Posts
    $null = Run-Tick $d ([datetime]"2026-10-24 03:00:00")
    $stFile = Join-Path $d.State "state.json"
    & chflags uchg $stFile
    try {
        [System.IO.File]::WriteAllText((Join-Path $d.State "pending"), "a file where the queue folder should be")
        $blocker = Join-Path $d.Root "blocked"
        [System.IO.File]::WriteAllText($blocker, "x")
        $realShare = $d.Share
        $d.Share = Join-Path $blocker "TIMS-10878"
        Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 03:04:24") $EvotipMsg)
        $null = Run-Tick $d ([datetime]"2026-10-24 03:05:00")
        Check "posted while nothing could be filed"  $script:Posts.Count 1
        $d.Share = $realShare
        $null = Run-Tick $d ([datetime]"2026-10-24 03:06:00")
        Check "a later tick does not post it again"  $script:Posts.Count 1
        $files = Get-EventFiles $realShare
        Check "but files it"                         $files.Count 1
        if ($files.Count -ge 1) {
            Check "as posted by the PC, at the first post" "$(JStr $files[0] 'posted_by_pc')|$(JStr $files[0] 'posted_at')" "true|2026-10-24T03:05:00"
        }
    }
    finally { & chflags nouchg $stFile }
}
else {
    Write-Host "  skip (no chflags here to make state.json unwritable)"
}

Write-Host ""
Write-Host "31. a direct post does not wait for the share"
# The queue is non-empty and the share is down. The sender brings the share
# back when it is called: only if the flush comes AFTER the post does the old
# queued event reach the share in the same tick.
$d = New-TickDirs "post_first"
$script:BlockerPath = Join-Path $d.Root "blocked"
[System.IO.File]::WriteAllText($script:BlockerPath, "x")
$d.Share = Join-Path $script:BlockerPath "TIMS-10878"
$script:BlockedShare = $d.Share
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 04:00:00")
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 04:04:24") $EvotipMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 04:05:00")
$pendingDir = Join-Path $d.State "pending"
Check "share down: queued"                ([System.IO.Directory]::GetFiles($pendingDir, "*.json").Count) 1
$Restorer = {
    param($p)
    [void]$script:Posts.Add($p)
    if ([System.IO.File]::Exists($script:BlockerPath)) {
        [System.IO.File]::Delete($script:BlockerPath)
        [void][System.IO.Directory]::CreateDirectory($script:BlockedShare)
    }
    return @{ Ok = $true; Status = 200 }
}
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 04:20:00") $PressureMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 04:21:00") $Restorer
Check "the post went out"                 $script:Posts.Count 2
Check "flushed after it: queue empty"     ([System.IO.Directory]::GetFiles($pendingDir, "*.json").Count) 0
Check "both on the share, in one tick"    (Get-EventFiles $d.Share).Count 2
Check "heartbeat written after the posts" ([System.IO.File]::Exists((Join-Path $d.Share "heartbeat.json"))) "True"
# watch.log keeps the tick's steps in order: the post comes before any line
# about the share (a flush first would log "share unavailable" first)
$tickLines = @()
foreach ($ln in ((Read-WatchLogText $d) -split "`n")) { if ($ln.StartsWith("2026-10-24 04:21:00")) { $tickLines += $ln } }
$iPost = -1; $iShare = -1
for ($i = 0; $i -lt $tickLines.Count; $i++) {
    if ($iPost -lt 0 -and $tickLines[$i].Contains("posted (200)")) { $iPost = $i }
    if ($iShare -lt 0 -and $tickLines[$i].Contains("share")) { $iShare = $i }
}
Check "post logged before any share step" (($iPost -ge 0) -and ($iShare -gt $iPost)) "True"

Remove-Item -LiteralPath $TickRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
if ($Failures -gt 0) { Write-Host "$Failures FAILED"; exit 1 }
Write-Host "all ok"
exit 0
