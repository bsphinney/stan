# test_hystar_watcher.ps1
#
# Tests for instrument/scripts/watch_hystar_errors.ps1: reading and parsing the
# logbook, deciding and formatting, the tick, and (sections 32-41) the install
# surface, the stored webhook, -Pause and the install test message.
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
    @("Evosep One: Object reference not set to an instance of an object.", "other", "Stopped"),
    # Final review #7: 8 real "High pressure pump(s) did not meet criteria"
    # stops were titled "Pressure limit"; it is a pump self-test, not a clog.
    @("Evosep One: High pressure pump(s) did not meet criteria (max displacement 9 uL):", "evosep_hw", "Evosep hardware fault"),
    @("Evosep One: Low pressure pump(s) did not meet criteria (max displacement 9 uL):", "evosep_hw", "Evosep hardware fault")
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
Check "vial at 02:04:24"        (Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 02:04:24") -Category "evotip") "S1-A5"
Check "newest folder > 90 min"  ((Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 04:00:00") -Category "overpressure") -eq $null) "True"
Check "older folder, its vial"  (Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-23 20:10:00") -Category "overpressure") "S1-B3"
Check "start after At ignored"  ((Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 02:00:00") -Category "overpressure") -eq $null) "True"
Check "missing root -> null"    ((Get-EvosepVial -ProcedureRoot (Join-Path $repoRoot "nope") -At ([datetime]"2026-10-24 02:04:24") -Category "evotip") -eq $null) "True"

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
# R18: an S: line only appears when someone is already restarting HyStar, so
# it is told, not paged: no @mention.
$rst = Format-RestartPayload -Event @{ At = [datetime]"2026-10-24 09:05:00" }
Check "restart (R18: no @mention)" $rst.text ":red_circle: TIMS-10878 HyStar restarted $dash no shutdown logged $dot 09:05"
$rstFn = $null
foreach ($fa in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($fa.Name -eq "Format-RestartPayload") { $rstFn = $fa }
}
$rstMention = $false
if ($null -ne $rstFn) { $rstMention = ($rstFn.Extent.Text -match 'MemberId|Get-MentionPrefix') }
Check "R18: Format-RestartPayload cannot mention anyone" $rstMention "False"
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
Check "restart with no shutdown: one post" (Count-Posts "*HyStar restarted*") 1
Check "R18: and it does not @mention"      (Count-Posts "*<@*HyStar restarted*") 0
Check "R18: nor does its event's payload"  ((Read-JsonFile (Get-EventFiles $d.Share)[0]).payload.text -match '<@') "False"
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

# =============================================================================
# Task 9: install, secrets, pause, release. Task Scheduler, the registry,
# DPAPI and ACLs do not exist here (macOS), so none of them is called for
# real: their calls are checked through the AST, the logic around them
# through small pure helpers, and the install flow with the few functions
# that would touch Windows (or wait for a person) stubbed. Task 10 checks the
# real calls on TIMS-10878.

function Get-FunctionAst([string]$name) {
    foreach ($fa in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        if ($fa.Name -eq $name) { return $fa }
    }
    return $null
}
# Unrolled on return: callers wrap it in @() (a "return ,$x" here would nest
# an empty result inside a one-element array).
function Find-Commands($node, [string]$cmd) {
    $found = New-Object System.Collections.ArrayList
    if ($null -ne $node) {
        foreach ($c in $node.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)) {
            if ($c.GetCommandName() -eq $cmd) { [void]$found.Add($c) }
        }
    }
    return $found.ToArray()
}
# What follows -Name in one command: the argument's text, "<switch>" for a
# bare switch, "" when the parameter is absent.
function Get-CommandArg($cmdAst, [string]$name) {
    $els = $cmdAst.CommandElements
    for ($i = 0; $i -lt $els.Count; $i++) {
        $e = $els[$i]
        if ($e -is [System.Management.Automation.Language.CommandParameterAst] -and $e.ParameterName -eq $name) {
            if ($null -ne $e.Argument) { return $e.Argument.Extent.Text }
            if (($i + 1) -lt $els.Count -and -not ($els[$i + 1] -is [System.Management.Automation.Language.CommandParameterAst])) {
                return $els[$i + 1].Extent.Text
            }
            return "<switch>"
        }
    }
    return ""
}
function Get-OwnerFunction($node) {
    $p = $node.Parent
    while ($null -ne $p) {
        if ($p -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $p.Name }
        $p = $p.Parent
    }
    return "<top level>"
}
function Get-FunctionParamDefault($fnAst, [string]$paramName) {
    if ($null -eq $fnAst -or $null -eq $fnAst.Body.ParamBlock) { return "" }
    foreach ($pp in $fnAst.Body.ParamBlock.Parameters) {
        if ($pp.Name.VariablePath.UserPath -eq $paramName -and $null -ne $pp.DefaultValue) {
            return $pp.DefaultValue.Extent.Text.Trim("'", '"')
        }
    }
    return ""
}
# Runs a scriptblock; returns @{ Value = <its output>; Text = <what it
# printed with Write-Host> }. The locals are odd names so the scriptblock
# sees the caller's variables, not these.
function Invoke-Captured([scriptblock]$__sb) {
    $__value = New-Object System.Collections.ArrayList
    $__text = New-Object System.Text.StringBuilder
    foreach ($__o in @(& $__sb 6>&1)) {
        if ($__o -is [System.Management.Automation.InformationRecord]) { [void]$__text.AppendLine("$($__o.MessageData)") }
        else { [void]$__value.Add($__o) }
    }
    return @{ Value = $__value.ToArray(); Text = $__text.ToString() }
}
function Last-Value($cap) {
    if ($cap.Value.Count -eq 0) { return "<none>" }
    return "$($cap.Value[$cap.Value.Count - 1])"
}
function Secure([string]$s) { return (ConvertTo-SecureString -String $s -AsPlainText -Force) }
function Run-HivePython([string]$code, [string[]]$pyArgs) {
    # The JSON line python printed, parsed; $null (and a note) when python3
    # with stan is not importable here.
    $out = $null; $exit = 1; $json = $null
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    Push-Location $repoRoot
    try { $out = & python3 -c $code @pyArgs 2>&1; $exit = $LASTEXITCODE } catch { $out = $_.Exception.Message }
    Pop-Location
    $ErrorActionPreference = $eap
    foreach ($line in @($out)) { if ("$line".StartsWith("{")) { $json = "$line" } }
    if ($exit -eq 0 -and $json) { return (ConvertFrom-Json $json) }
    Write-Host "  skip Hive cross-check (python3 with stan not importable): $out"
    return $null
}
$GoodHook = "https://hooks.slack.com/T0STAN/B0STAN/notarealtoken"
$env:COMPUTERNAME = "TIMS-10878"
$Now0 = [datetime]"2026-10-24 12:00:00"
$TestText = "<@U0ABC1234> STAN HyStar watcher v$ScriptVersion is installed on TIMS-10878. If your phone buzzed, queue-stop alerts will reach you."
$CLog = 'C:\BDalSystemData\HyStar\LogFiles\CompassHyStarErrorLogbook.log'
$DLog = 'D:\BDalSystemData\HyStar\LogFiles\CompassHyStarErrorLogbook.log'

# ------------------------------------------------------ 32. install surface
Write-Host ""
Write-Host "32. install surface: parameters, functions, and prompts only where a person is"
$declared = @{}
foreach ($pp in $ast.ParamBlock.Parameters) { $declared[$pp.Name.VariablePath.UserPath] = $pp.StaticType.Name }
Check "-Scheduled is a switch"      $declared["Scheduled"] "SwitchParameter"
Check "-Uninstall is a switch"      $declared["Uninstall"] "SwitchParameter"
Check "-Test is a switch"           $declared["Test"] "SwitchParameter"
Check "-InstallOnly is a switch"    $declared["InstallOnly"] "SwitchParameter"
Check "-Pause takes a string"       $declared["Pause"] "String"
Check "-SlackMemberId takes a string" $declared["SlackMemberId"] "String"
Check "-LogbookPath takes a string" $declared["LogbookPath"] "String"
$missingFns = @()
foreach ($w in @("Say", "Pause-IfInteractive", "Test-Task", "Test-Elevated", "Remove-Task",
                 "Get-InstalledVersion", "Install-Task", "Find-ErrorLogbook", "Save-Webhook",
                 "Get-Webhook", "Write-PauseFile", "Get-AutoLogonStatus", "Send-InstallTest",
                 "Invoke-Install", "Initialize-StateDir", "Set-SecretFileAcl")) {
    if ($null -eq (Get-FunctionAst $w)) { $missingFns += $w }
}
Check "install functions present" ($missingFns -join ",") ""
# A Read-Host reached by a scheduled run would hold the task open until its
# 5 min limit, every minute. The prompts live in two functions the install
# alone calls.
$readHosts = @(Find-Commands $ast "Read-Host")
$owners = @()
foreach ($rh in $readHosts) { $owners += (Get-OwnerFunction $rh) }
Check "Read-Host only in the two prompt functions" (($owners | Sort-Object -Unique) -join ",") "Read-MemberIdInput,Read-WebhookInput"
$secureAsk = $false
foreach ($rh in $readHosts) {
    if ((Get-OwnerFunction $rh) -eq "Read-WebhookInput" -and (Get-CommandArg $rh "AsSecureString") -eq "<switch>") { $secureAsk = $true }
}
Check "the webhook is asked for with -AsSecureString" $secureAsk "True"
# The top level dispatches -Scheduled first, and that branch exits: a tick
# never reaches the install, a prompt, or the key-press.
$top = @($ast.EndBlock.Statements)
$iSched = -1; $iInstall = -1
for ($i = 0; $i -lt $top.Count; $i++) {
    $s = $top[$i]
    if ($s -is [System.Management.Automation.Language.FunctionDefinitionAst]) { continue }
    if ($iSched -lt 0 -and $s -is [System.Management.Automation.Language.IfStatementAst] -and
        $s.Clauses[0].Item1.Extent.Text -eq '$Scheduled') { $iSched = $i }
    if ($iInstall -lt 0 -and $s.Extent.Text -match 'Invoke-Install|Pause-IfInteractive|Say ') { $iInstall = $i }
}
Check "-Scheduled is dispatched before any install step" (($iSched -ge 0) -and ($iSched -lt $iInstall)) "True"
$schedExits = $false
if ($iSched -ge 0) { $schedExits = ($top[$iSched].Clauses[0].Item2.Extent.Text -match '\bexit\b') }
Check "and that branch exits" $schedExits "True"
Check "the tick loads the stored webhook" (($iSched -ge 0) -and ($top[$iSched].Extent.Text -match 'Get-Webhook')) "True"

# ------------------------------------------------- 33. the scheduled task
Write-Host ""
Write-Host "33. the scheduled task: every minute, never stacked, not elevated, as the desktop's user"
$taskName = ""
foreach ($a in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.AssignmentStatementAst] }, $false)) {
    if ($a.Left.Extent.Text -eq '$TaskName') { $taskName = $a.Right.Extent.Text.Trim("'", '"') }
}
Check "task name"                          $taskName "STAN HyStar watcher"
$it = Get-FunctionAst "Install-Task"
$rep = ""; $once = $false; $logon = $false
foreach ($t in (Find-Commands $it "New-ScheduledTaskTrigger")) {
    $r = Get-CommandArg $t "RepetitionInterval"
    if ($r -ne "") { $rep = $r; $once = ((Get-CommandArg $t "Once") -eq "<switch>") }
    if ((Get-CommandArg $t "AtLogOn") -eq "<switch>") { $logon = $true }
}
Check "repeats every 1 minute"             $rep "(New-TimeSpan -Minutes 1)"
Check "the repeat is a -Once trigger"      $once "True"
Check "plus an AtLogOn trigger"            $logon "True"
$sets = @(Find-Commands $it "New-ScheduledTaskSettingsSet")
Check "one settings set"                   $sets.Count 1
if ($sets.Count -eq 1) {
    Check "MultipleInstances IgnoreNew"    (Get-CommandArg $sets[0] "MultipleInstances") "IgnoreNew"
    Check "StartWhenAvailable"             (Get-CommandArg $sets[0] "StartWhenAvailable") "<switch>"
    Check "ExecutionTimeLimit 5 min"       (Get-CommandArg $sets[0] "ExecutionTimeLimit") "(New-TimeSpan -Minutes 5)"
}
$regs = @(Find-Commands $it "Register-ScheduledTask")
Check "registered in one place"            $regs.Count 1
if ($regs.Count -eq 1) {
    Check "under the task name"            (Get-CommandArg $regs[0] "TaskName") '$TaskName'
    Check "with both triggers"             (Get-CommandArg $regs[0] "Trigger") '@($atLogon, $repeat)'
    Check "with an explicit principal"     (Get-CommandArg $regs[0] "Principal") '$principal'
}
$prs = @(Find-Commands $it "New-ScheduledTaskPrincipal")
Check "one principal"                      $prs.Count 1
if ($prs.Count -eq 1) {
    Check "runs only while that user is logged on" (Get-CommandArg $prs[0] "LogonType") "Interactive"
    Check "never elevated (R16)"           (Get-CommandArg $prs[0] "RunLevel") "Limited"
}
$acts = @(Find-Commands $it "New-ScheduledTaskAction")
Check "one action"                         $acts.Count 1
if ($acts.Count -eq 1) {
    # R17: what starts the tick is Get-TaskLaunch's choice (section 43).
    Check "R17: the action is the chosen launcher" "$(Get-CommandArg $acts[0] 'Execute')|$(Get-CommandArg $acts[0] 'Argument')" '$launch.Execute|$launch.Argument'
}
$gls = @(Find-Commands $it "Get-TaskLaunch")
$glOk = $false
foreach ($g in $gls) { if ((Get-CommandArg $g "ScriptPath") -eq '$InstalledPath' -and (Get-CommandArg $g "ShimPath") -eq '$shimPath') { $glOk = $true } }
Check "R17: for the installed copy and its shim" $glOk "True"
Check "R16: Install-Task prepares the state folder" (@(Find-Commands $it "Initialize-StateDir").Count -ge 1) "True"
$isd = Get-FunctionAst "Initialize-StateDir"
$isdText = ""
if ($isd) { $isdText = $isd.Extent.Text }
Check "R16: grants Modify"                 ($isdText -match "'Modify'") "True"
Check "R16: inherited by files and folders" ($isdText -match "'ContainerInherit, ObjectInherit'") "True"
# Code, not prose: its comment explains that the tick seeds those files.
$isdBad = @()
if ($isd) {
    foreach ($sc in $isd.FindAll({ $args[0] -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)) {
        if ($sc.Value -match 'state\.json|delivered\.txt') { $isdBad += $sc.Value }
    }
    foreach ($cn in @("Publish-JsonFile", "Write-Utf8File", "Set-Content", "Out-File", "Add-Content", "New-Item")) {
        if (@(Find-Commands $isd $cn).Count -gt 0) { $isdBad += $cn }
    }
}
Check "R16: creates no state file itself"  ($isdBad -join ",") ""

# -------------------------------------------------- 34. finding the logbook
Write-Host ""
Write-Host "34. finding the logbook: -LogbookPath, then C:, then D:"
Check "candidates in order"                (@(Get-LogbookCandidates -LogbookPath 'E:\elsewhere\x.log') -join "|") "E:\elsewhere\x.log|$CLog|$DLog"
Check "no -LogbookPath: C: then D:"        (@(Get-LogbookCandidates) -join "|") "$CLog|$DLog"
Check "a given C: path is not tried twice" (@(Get-LogbookCandidates -LogbookPath $CLog.ToLowerInvariant()) -join "|") "$($CLog.ToLowerInvariant())|$DLog"
$lbDir = Join-Path $TickRoot "lb"
[void][System.IO.Directory]::CreateDirectory($lbDir)
$lbFile = Join-Path $lbDir "CompassHyStarErrorLogbook.log"
Write-Logbook $lbFile (Rec-Header)
Check "an existing -LogbookPath is used"   (Find-ErrorLogbook -LogbookPath $lbFile) $lbFile
Check "nothing there: null"                ($null -eq (Find-ErrorLogbook -LogbookPath (Join-Path $lbDir "missing.log"))) "True"
Check "a folder is not a logbook"          ($null -eq (Find-ErrorLogbook -LogbookPath $lbDir)) "True"
$fe = Get-FunctionAst "Get-LogbookCandidates"
Check "C: before D: in what ships"         ($fe.Extent.Text.IndexOf($CLog) -lt $fe.Extent.Text.IndexOf($DLog) -and $fe.Extent.Text.IndexOf($CLog) -gt 0) "True"

# -------------------------------------------------------- 35. member ID
Write-Host ""
Write-Host "35. Slack member ID: the rule Hive uses"
Check "U id"                       (Test-SlackMemberId "U0ABC1234") "True"
Check "W id"                       (Test-SlackMemberId "W0ABC12") "True"
Check "lower case refused"         (Test-SlackMemberId "u0abc1234") "False"
Check "too short refused"          (Test-SlackMemberId "U12345") "False"
Check "another prefix refused"     (Test-SlackMemberId "B0ABC1234") "False"
Check "a mention is not an id"     (Test-SlackMemberId "<@U0ABC1234>") "False"
Check "a name is not an id"        (Test-SlackMemberId "Brett") "False"
Check "empty refused"              (Test-SlackMemberId "") "False"
$sm = Get-FunctionAst "Test-SlackMemberId"
$smText = ""
if ($sm) { $smText = $sm.Extent.Text }
Check "the same pattern as stan.notify" ($smText.Contains("'^[UW][A-Z0-9]{6,}$'")) "True"
Check "case-sensitive, like Python's re" ($smText -match '-cmatch') "True"

# ---------------------------------------------------------- 36. webhook
Write-Host ""
Write-Host "36. webhook: refused unless it is Slack's, DPAPI-stored, never in the script"
Check "a Slack webhook is accepted"     (Test-WebhookUrl $GoodHook) "True"
Check "http://example.com refused"      (Test-WebhookUrl "http://example.com") "False"
Check "plain http refused"              (Test-WebhookUrl "http://hooks.slack.com/T0/B0/x") "False"
Check "a lookalike host refused"        (Test-WebhookUrl "https://hooks.slack.com.evil.example/x") "False"
Check "the bare prefix refused"         (Test-WebhookUrl "https://hooks.slack.com/") "False"
Check "inner space refused"             (Test-WebhookUrl "https://hooks.slack.com/T0/B0 x") "False"
Check "empty refused"                   (Test-WebhookUrl "") "False"
$whDir = Join-Path $TickRoot "webhook"
[void][System.IO.Directory]::CreateDirectory($whDir)
$whFile = Join-Path $whDir "webhook.dpapi"
$r = Save-Webhook -Webhook (Secure "http://example.com") -StateDir $whDir
Check "Save-Webhook refuses http://example.com" $r.Ok "False"
Check "and says what it must start with" ([string]$r.Why).Contains("https://hooks.slack.com/") "True"
Check "nothing stored"                  ([System.IO.File]::Exists($whFile)) "False"
if (-not $IsWindows) {
    # The ACL step cannot run here (no Windows ACLs), which is the case of an
    # ACL that cannot be set: the file must not stay behind unprotected.
    $r = Save-Webhook -Webhook (Secure $GoodHook) -StateDir $whDir
    Check "unprotectable: not stored, not left behind" "$($r.Ok)|$([System.IO.File]::Exists($whFile))" "False|False"
    Check "the reason carries no URL" ([string]$r.Why).Contains("T0STAN") "False"
}
Check "Get-Webhook: none stored -> null" ($null -eq (Get-Webhook -StateDir $whDir)) "True"
Write-Utf8File -Path $whFile -Text "not a protected string"
Check "garbage -> null, no throw"       ($null -eq (Get-Webhook -StateDir $whDir)) "True"
# What ConvertFrom-SecureString wrote, Get-Webhook reads back. On Windows that
# is DPAPI (CurrentUser); here pwsh only encodes it, so this checks the
# plumbing, not the protection.
Write-Utf8File -Path $whFile -Text (ConvertFrom-SecureString (Secure $GoodHook))
Check "round trip"                      (Get-Webhook -StateDir $whDir) $GoodHook
Write-Utf8File -Path $whFile -Text (ConvertFrom-SecureString (Secure "https://example.com/x"))
Check "a stored non-Slack URL is not used" ($null -eq (Get-Webhook -StateDir $whDir)) "True"
$sw = Get-FunctionAst "Save-Webhook"
$cf = @(Find-Commands $sw "ConvertFrom-SecureString")
Check "Save-Webhook protects with ConvertFrom-SecureString" $cf.Count 1
if ($cf.Count -eq 1) {
    Check "with no key: DPAPI, CurrentUser" "$(Get-CommandArg $cf[0] 'Key')$(Get-CommandArg $cf[0] 'SecureKey')" ""
}
Check "into webhook.dpapi"              ($sw.Extent.Text.Contains("'webhook.dpapi'")) "True"
Check "then locks the file down"        (@(Find-Commands $sw "Set-SecretFileAcl").Count) 1
$gw = Get-FunctionAst "Get-Webhook"
$ct = @(Find-Commands $gw "ConvertTo-SecureString")
Check "Get-Webhook decrypts with ConvertTo-SecureString" $ct.Count 1
if ($ct.Count -eq 1) {
    Check "with no key, not as plain text" "$(Get-CommandArg $ct[0] 'Key')$(Get-CommandArg $ct[0] 'SecureKey')$(Get-CommandArg $ct[0] 'AsPlainText')" ""
}
Check "through the BSTR helper"         (@(Find-Commands $gw "Get-PlainText").Count) 1
$pt = Get-FunctionAst "Get-PlainText"
$zero = $false
if ($pt) {
    foreach ($tr in $pt.FindAll({ $args[0] -is [System.Management.Automation.Language.TryStatementAst] }, $true)) {
        if ($null -ne $tr.Finally -and $tr.Finally.Extent.Text -match 'ZeroFreeBSTR') { $zero = $true }
    }
}
Check "the BSTR is zeroed in a finally" $zero "True"
Check "Get-PlainText round trip"        (Get-PlainText (Secure "abc")) "abc"
$acl = Get-FunctionAst "Set-SecretFileAcl"
$aclText = ""
if ($acl) { $aclText = $acl.Extent.Text }
Check "ACL: inheritance cut, inherited entries dropped" ($aclText -match 'SetAccessRuleProtection\(\$true, \$false\)') "True"
Check "ACL: Administrators"             ($aclText.Contains("'S-1-5-32-544'")) "True"
Check "ACL: the account that stored it" ($aclText -match 'WindowsIdentity\]::GetCurrent\(\)\.User') "True"
$lits = @()
foreach ($sl in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
                               $args[0] -is [System.Management.Automation.Language.ExpandableStringExpressionAst] }, $true)) {
    if ($sl.Value -match 'hooks\.slack\.com/services') { $lits += $sl.Extent.Text }
}
Check "no string literal holds a webhook path" $lits.Count 0
Check "nor does any comment"            ($ast.Extent.Text -match 'hooks\.slack\.com/services') "False"

# ------------------------------------------------------------- 37. pause
Write-Host ""
Write-Host "37. -Pause: pause_until.json silences Hive's watcher-down page, nothing else"
function PU([string]$t, [datetime]$now = $Now0) {
    $u = ConvertTo-PauseUntil -Text $t -Now $now
    if ($null -eq $u) { return "<null>" }
    return $u.ToString("s")
}
Check "8h"                       (PU "8h") "2026-10-24T20:00:00"
Check "2d"                       (PU "2d") "2026-10-26T12:00:00"
Check "upper case and spaces"    (PU " 8H ") "2026-10-24T20:00:00"
Check "0h ends a pause"          (PU "0h") "2026-10-24T12:00:00"
Check "744h is the most"         (PU "744h" ([datetime]"2026-12-01 12:00:00")) "2027-01-01T12:00:00"
Check "31d is the most"          (PU "31d") "2026-11-24T12:00:00"
Check "745h refused"             (PU "745h") "<null>"
Check "32d refused"              (PU "32d") "<null>"
Check "no unit refused"          (PU "8") "<null>"
Check "minutes refused"          (PU "8m") "<null>"
Check "negative refused"         (PU "-1h") "<null>"
Check "words refused"            (PU "tomorrow") "<null>"
Check "empty refused"            (PU "") "<null>"
if ([TimeZoneInfo]::Local.Id -eq "America/Los_Angeles") {
    # DST ends Sun 2026-11-01 (02:00 -> 01:00). Hours are real hours; days
    # are calendar days.
    Check "8h across the fall-back is 8 real hours" (PU "8h" ([datetime]"2026-10-31 20:00:00")) "2026-11-01T03:00:00"
    Check "1d across it keeps the clock time"       (PU "1d" ([datetime]"2026-10-31 20:00:00")) "2026-11-01T20:00:00"
}
else {
    Write-Host "  skip DST cases (this machine is not on Pacific time)"
}
$pShare = Join-Path $TickRoot "pause_share/TIMS-10878"
$pf = Join-Path $pShare "pause_until.json"
$u = Set-WatcherPause -Duration "8h" -Now $Now0 -ShareDir $pShare -By "TIMS-10878\brett" -Reason "service visit"
Check "-Pause 8h returns the end"        $u.ToString("s") "2026-10-24T20:00:00"
Check "pause_until.json written"         ([System.IO.File]::Exists($pf)) "True"
Check "until = Now + 8 h, local ISO"     (JStr $pf "until") "2026-10-24T20:00:00"
Check "by"                               (JStr $pf "by") "TIMS-10878\brett"
Check "reason"                           (JStr $pf "reason") "service visit"
$pb = [System.IO.File]::ReadAllBytes($pf)
Check "UTF-8 without a BOM (R14)"        ($pb[0] -eq 0x7B) "True"
Check "no temp file left"                ([System.IO.File]::Exists((Join-Path $pShare "pause_until.tmp"))) "False"
[System.IO.File]::Delete($pf)
$u = Set-WatcherPause -Duration "soon" -Now $Now0 -ShareDir $pShare -By "x" -Reason "y"
Check "a bad duration: null, nothing written" "$($null -eq $u)|$([System.IO.File]::Exists($pf))" "True|False"
$null = Write-PauseFile -Until ([datetime]"2026-12-24 08:00:00") -ShareDir $pShare -By "b" -Reason "r"
Check "Write-PauseFile -Until"           (JStr $pf "until") "2026-12-24T08:00:00"
$null = Write-PauseFile -Until ([datetime]"2026-12-25 09:30:00") -ShareDir $pShare -By "b" -Reason "r"
Check "a second pause replaces the first" (JStr $pf "until") "2026-12-25T09:30:00"
$pBlock = Join-Path $TickRoot "pause_blocker"
Write-Utf8File -Path $pBlock -Text "x"
$threw = $false
try { $null = Set-WatcherPause -Duration "8h" -Now $Now0 -ShareDir (Join-Path $pBlock "TIMS-10878") -By "x" -Reason "y" } catch { $threw = $true }
Check "an unwritable share is an error, not a silent pass" $threw "True"
# Pausing silences Hive's down page only: the watcher still posts stops.
$d = New-TickDirs "paused"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 12:00:00")
$null = Set-WatcherPause -Duration "8h" -Now ([datetime]"2026-10-24 12:00:00") -ShareDir $d.Share -By "x" -Reason "y"
Add-Logbook $d.Logbook (Rec-Stop ([datetime]"2026-10-24 12:04:24") $EvotipMsg)
$null = Run-Tick $d ([datetime]"2026-10-24 12:05:00")
Check "paused: a stop still pages"       (Count-Posts "*queue STOPPED*") 1
Check "and is still filed"               (Get-EventFiles $d.Share).Count 1
Check "the tick leaves the pause alone"  (JStr (Join-Path $d.Share "pause_until.json") "until") "2026-10-24T20:00:00"

# --------------------------------------------------------- 38. auto-logon
Write-Host ""
Write-Host "38. auto-logon: on / off / unknown"
Check "1 -> on"                    (ConvertTo-AutoLogonStatus "1") "on"
Check "0 -> off"                   (ConvertTo-AutoLogonStatus "0") "off"
Check "absent -> off"              (ConvertTo-AutoLogonStatus $null) "off"
Check "anything else -> unknown"   (ConvertTo-AutoLogonStatus "yes") "unknown"
Check "an unreadable key -> unknown, no throw" (Get-AutoLogonStatus -RegistryPath "HKLM:\SOFTWARE\NoSuchKey\Winlogon") "unknown"
$al = Get-FunctionAst "Get-AutoLogonStatus"
Check "reads Winlogon"             (Get-FunctionParamDefault $al "RegistryPath") 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Check "reads AutoAdminLogon"       ($al.Extent.Text.Contains("'AutoAdminLogon'")) "True"

# ------------------------------------------------- 39. install test message
Write-Host ""
Write-Host "39. the install test message: direct, or a 'test' event Hive relays"
$tp = Format-TestPayload -MemberId "U0ABC1234" -HostName "TIMS-10878" -Version $ScriptVersion
Check "the text, word for word"          $tp.text $TestText
Check "blocks carry the same text"       $tp.blocks[0].text.text $TestText
Check "ASCII only"                       ([regex]::IsMatch($tp.text, '[^\x00-\x7F]')) "False"
function R16-Clean($d) {
    return "$([System.IO.File]::Exists((Join-Path $d.State 'state.json')))|$([System.IO.File]::Exists((Join-Path $d.State 'delivered.txt')))"
}
function Pending-Count($d) {
    $pd = Join-Path $d.State "pending"
    if (-not [System.IO.Directory]::Exists($pd)) { return 0 }
    return [System.IO.Directory]::GetFiles($pd, "*.json").Count
}
$t1 = New-TickDirs "installtest_direct"
Reset-Posts
$r = Send-InstallTest -MemberId "U0ABC1234" -HostName "TIMS-10878" -ShareDir $t1.Share -StateDir $t1.State `
    -Now $Now0 -Version $ScriptVersion -Sender $FakeSender
Check "posted: route direct"             $r.Route "direct"
Check "printed as 'direct'"              (Get-TestRouteText $r) "direct"
Check "one post, the test text"          "$($script:Posts.Count)|$($script:Posts[0].text)" "1|$TestText"
Check "direct: no event file"            (Get-EventFiles $t1.Share).Count 0
Check "R16: no state.json, no delivered.txt" (R16-Clean $t1) "False|False"

$t2 = New-TickDirs "installtest_refused"
Reset-Posts
$script:SendOk = $false; $script:SendStatus = 403
$r = Send-InstallTest -MemberId "U0ABC1234" -HostName "TIMS-10878" -ShareDir $t2.Share -StateDir $t2.State `
    -Now $Now0 -Version $ScriptVersion -Sender $FakeSender
Check "refused: route via Hive"          $r.Route "hive"
Check "printed as the brief says"        (Get-TestRouteText $r) "via Hive (the PC could not reach Slack)"
Check "the status is kept"               "$($r.Status)" "403"
$tf = @(Get-EventFiles $t2.Share)
Check "one event file on the share"      $tf.Count 1
if ($tf.Count -eq 1) {
    Check "type test"                    (JStr $tf[0] "type") "test"
    Check "posted_by_pc false"           (JStr $tf[0] "posted_by_pc") "false"
    Check "host"                         (JStr $tf[0] "host") "TIMS-10878"
    Check "station"                      (JStr $tf[0] "station") "tims10878"
    Check "at"                           (JStr $tf[0] "at") "2026-10-24T12:00:00"
    Check "watcher_version"              (JStr $tf[0] "watcher_version") $ScriptVersion
    Check "the payload is the test text" ((Read-JsonFile $tf[0]).payload.text) $TestText
    Check "a test id of its own"         ((JStr $tf[0] "id") -like "TIMS-10878_20261024T120000_test") "True"
    Check "UTF-8 without a BOM"          ([System.IO.File]::ReadAllBytes($tf[0])[0] -eq 0x7B) "True"
}
Check "nothing left in the queue"        (Pending-Count $t2) 0
Check "R16: no state.json, no delivered.txt" (R16-Clean $t2) "False|False"

$t3 = New-TickDirs "installtest_nohook"
Reset-Posts
$r = Send-InstallTest -MemberId "U0ABC1234" -HostName "TIMS-10878" -ShareDir $t3.Share -StateDir $t3.State `
    -Now $Now0 -Version $ScriptVersion
Check "no webhook: via Hive"             "$($r.Route)|$($script:Posts.Count)" "hive|0"
Check "and says why"                     "$($r.Status)" "no webhook stored"

$t4 = New-TickDirs "installtest_shareless"
$blocker = Join-Path $t4.Root "blocker"
Write-Utf8File -Path $blocker -Text "x"
Reset-Posts
$script:SendOk = $false; $script:SendStatus = "WebException: no route"
$r = Send-InstallTest -MemberId "U0ABC1234" -HostName "TIMS-10878" -ShareDir (Join-Path $blocker "TIMS-10878") `
    -StateDir $t4.State -Now $Now0 -Version $ScriptVersion -Sender $FakeSender
Check "share down too: queued here"      $r.Route "queued"
Check "printed as queued"                ((Get-TestRouteText $r) -match "queued on this PC") "True"
Check "one file queued"                  (Pending-Count $t4) 1
Check "R16: still no state.json, no delivered.txt" (R16-Clean $t4) "False|False"
# The first scheduled tick (share back) seeds its own state and delivers it.
Write-Logbook $t4.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $t4 ([datetime]"2026-10-24 12:01:00")
$qf = @(Get-EventFiles $t4.Share)
Check "the first tick delivers the queued test" "$($qf.Count)|$(Pending-Count $t4)" "1|0"
if ($qf.Count -eq 1) { Check "as a test event, unposted" "$(JStr $qf[0] 'type')|$(JStr $qf[0] 'posted_by_pc')" "test|false" }
Check "and nothing is re-posted from the PC" $script:Posts.Count 0

# Hive's side: the test event is relayed (not a failure: no hourly keys), the
# pause is honoured until it ends and is not mistaken for an event.
$hp = Join-Path $TickRoot "py9/TIMS-10878"
[void][System.IO.Directory]::CreateDirectory($hp)
foreach ($ff in (Get-EventFiles $t2.Share)) { Copy-Item -LiteralPath $ff -Destination $hp }
$null = Write-PauseFile -Until ([datetime]"2026-10-24 20:00:00") -ShareDir $hp -By "TIMS-10878\brett" -Reason "service visit"
$py9 = @'
import json, sys
from pathlib import Path
from stan.instrument_time import parse_local
from stan.reports.hystar_events import load_new_events, suppression_keys, _relay_payload, _paused_until
host = Path(sys.argv[1])
evs = load_new_events(host)
during = _paused_until(host, parse_local("2026-10-24T13:00:00"))
after = _paused_until(host, parse_local("2026-10-24T21:00:00"))
print(json.dumps({
    "n": len(evs),
    "rejected": (host / "rejected").exists(),
    "type": evs[0]["type"] if evs else None,
    "relayed": bool(evs) and evs[0].get("posted_by_pc") is not True,
    "keys": sum(len(suppression_keys(e)) for e in evs),
    "text_kept": all(_relay_payload(e)["text"] == e["payload"]["text"] for e in evs),
    "during": during.strftime("%Y%m%d%H%M%S") if during else None,
    "after": after is None,
}))
'@
$pr = Run-HivePython $py9 @($hp)
if ($null -ne $pr) {
    Check "Hive reads the test event, and only it" "$($pr.n)|$($pr.rejected)|$($pr.type)" "1|False|test"
    Check "Hive relays it (posted_by_pc false)"    $pr.relayed "True"
    Check "a test marks no hourly keys"            $pr.keys 0
    Check "relayed with the PC's text"             $pr.text_kept "True"
    Check "Hive: paused until 20:00"               $pr.during "20261024200000"
    Check "Hive: not paused after it"              $pr.after "True"
}

# --------------------------------------------- 40. who the watcher runs as
Write-Host ""
Write-Host "40. the account: the task's, the desktop's, the one holding the webhook"
Check "same name"                        (Test-SameAccount "TIMS-10878\brett" "TIMS-10878\brett") "True"
Check "case does not matter"             (Test-SameAccount "tims-10878\Brett" "TIMS-10878\brett") "True"
Check "a bare name matches its domain form" (Test-SameAccount "brett" "TIMS-10878\brett") "True"
Check "another user"                     (Test-SameAccount "TIMS-10878\labadmin" "TIMS-10878\brett") "False"
Check "same name, another domain"        (Test-SameAccount "UCDAVIS\brett" "TIMS-10878\brett") "False"
Check "empty is never the same"          (Test-SameAccount "" "TIMS-10878\brett") "False"
Check "no warning when they match"       (Get-AccountWarning -TaskAccount "brett" -Desktop "TIMS-10878\brett") ""
$wn = Get-AccountWarning -TaskAccount "labadmin" -Desktop "TIMS-10878\brett"
Check "a warning when they differ"       ($wn.Contains("labadmin") -and $wn.Contains("TIMS-10878\brett")) "True"
Check "a warning when it cannot be read" ((Get-AccountWarning -TaskAccount "" -Desktop "TIMS-10878\brett") -ne "") "True"

# ------------------------------------------------- 41. the install, stubbed
Write-Host ""
Write-Host "41. the install: refuses before registering anything; R16 leaves the tick's files to the tick"
$Stubbed = @{}
foreach ($n in @("Install-Task", "Get-InteractiveUser", "Get-CurrentAccount", "Get-TaskAccount", "Get-TaskExecute",
                 "Read-MemberIdInput", "Read-WebhookInput", "Save-Webhook", "Get-Webhook",
                 "Send-SlackPayload", "Get-AutoLogonStatus")) {
    $Stubbed[$n] = (Get-Item "function:$n").ScriptBlock
}
$script:HookAnswers = New-Object System.Collections.Queue
$script:MemberAnswers = New-Object System.Collections.Queue
$script:Slack = New-Object System.Collections.ArrayList
Set-Item function:script:Install-Task { $script:InstallCalls++; return $true }
Set-Item function:script:Get-InteractiveUser { return @{ Name = "TIMS-10878\brett"; Sid = "S-1-5-21-1-2-3-1001" } }
Set-Item function:script:Get-CurrentAccount { return $script:Me }
Set-Item function:script:Get-TaskAccount { return $script:TaskUser }
Set-Item function:script:Get-TaskExecute { return $script:TaskExe }
Set-Item function:script:Get-AutoLogonStatus { return "off" }
Set-Item function:script:Read-MemberIdInput {
    param([string]$Stored = "")
    $script:Prompts++
    if ($script:MemberAnswers.Count -gt 0) {
        $a = [string]$script:MemberAnswers.Dequeue()
        if ($a -ne "") { return $a }
    }
    return $Stored
}
Set-Item function:script:Read-WebhookInput {
    param([switch]$HaveStored)
    $script:Prompts++
    if ($script:HookAnswers.Count -gt 0) {
        $a = [string]$script:HookAnswers.Dequeue()
        if ($a -ne "") { return (Secure $a) }
    }
    return (New-Object System.Security.SecureString)
}
Set-Item function:script:Save-Webhook {
    param($Webhook, $StateDir)
    $plain = (Get-PlainText $Webhook).Trim()
    if (-not (Test-WebhookUrl $plain)) { return @{ Ok = $false; Why = "refused" } }
    $script:StoredHook = $plain
    return @{ Ok = $true; Why = "" }
}
Set-Item function:script:Get-Webhook { param($StateDir) return $script:StoredHook }
Set-Item function:script:Send-SlackPayload {
    param($WebhookUrl, $Payload)
    [void]$script:Slack.Add($Payload)
    if ($script:SlackOk) { return @{ Ok = $true; Status = 200 } }
    return @{ Ok = $false; Status = 404 }
}
function New-InstallCase([string]$name) {
    $d = New-TickDirs $name
    [void][System.IO.Directory]::CreateDirectory($d.State)
    # Share folders set in config.json are kept: the test event lands there.
    Write-Utf8File -Path (Join-Path $d.State "config.json") -Text (ConvertTo-Json -InputObject ([ordered]@{ shareDir = $d.Share; hiveLogsDir = $d.Hive }) -Depth 10)
    Write-Logbook $d.Logbook (Rec-Header)
    $script:InstallCalls = 0; $script:Prompts = 0; $script:StoredHook = $null
    $script:HookAnswers.Clear(); $script:MemberAnswers.Clear(); $script:Slack.Clear()
    $script:Me = "TIMS-10878\brett"; $script:TaskUser = "brett"; $script:SlackOk = $true
    $script:TaskExe = 'C:\Windows\System32\wscript.exe'
    return $d
}
function Config-Field($d, [string]$name) {
    $obj = Read-JsonFile (Join-Path $d.State "config.json")
    $p = $obj.PSObject.Properties[$name]
    if ($null -eq $p) { return "<absent>" }
    if ($null -eq $p.Value) { return "<null>" }
    return [string]$p.Value
}

$d = New-InstallCase "inst_nolog"
$miss = Join-Path $d.Logs "nope.log"
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -LogbookPath $miss -SlackMemberId "U0ABC1234" }
Check "no logbook: refused"                (Last-Value $cap) "False"
Check "no logbook: no task registered"     $script:InstallCalls 0
Check "no logbook: nothing asked"          $script:Prompts 0
Check "prints the -LogbookPath it tried"   ($cap.Text.Contains($miss)) "True"
Check "prints the C: path it tried"        ($cap.Text.Contains($CLog)) "True"
Check "prints the D: path it tried"        ($cap.Text.Contains($DLog)) "True"
Check "says nothing was installed"         ($cap.Text -match "Nothing was installed") "True"

$d = New-InstallCase "inst_badid"
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -LogbookPath $d.Logbook -SlackMemberId "brett" }
Check "bad -SlackMemberId: refused"        (Last-Value $cap) "False"
Check "... before any prompt or task"      "$($script:Prompts)|$($script:InstallCalls)" "0|0"
Check "... and says what an ID looks like" ($cap.Text -match "member ID") "True"

$d = New-InstallCase "inst_badid_asked"
foreach ($x in @("brett", "U1", "<@U0ABC1234>")) { $script:MemberAnswers.Enqueue($x) }
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -LogbookPath $d.Logbook }
Check "three bad answers: refused"         (Last-Value $cap) "False"
Check "... asked three times, no task"     "$($script:Prompts)|$($script:InstallCalls)" "3|0"

$d = New-InstallCase "inst_badhook"
foreach ($x in @("http://example.com", "http://example.com", "http://example.com")) { $script:HookAnswers.Enqueue($x) }
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -LogbookPath $d.Logbook -SlackMemberId "U0ABC1234" }
Check "http://example.com: install refused" (Last-Value $cap) "False"
Check "... no task registered"             $script:InstallCalls 0
Check "... says what it must start with"   ($cap.Text.Contains("https://hooks.slack.com/")) "True"
Check "... never echoes what was pasted"   ($cap.Text.Contains("example.com")) "False"
Check "... no config written"              (Config-Field $d "memberId") "<absent>"

$d = New-InstallCase "inst_nohook"
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -LogbookPath $d.Logbook -SlackMemberId "U0ABC1234" }
Check "nothing pasted, none stored: refused" "$(Last-Value $cap)|$($script:InstallCalls)" "False|0"

$d = New-InstallCase "inst_wronguser"
$script:Me = "TIMS-10878\labadmin"
$script:HookAnswers.Enqueue($GoodHook)
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -LogbookPath $d.Logbook -SlackMemberId "U0ABC1234" }
Check "run as another account: refused"    (Last-Value $cap) "False"
Check "... before asking for anything"     $script:Prompts 0
Check "... no task, no webhook stored"     "$($script:InstallCalls)|$($null -eq $script:StoredHook)" "0|True"
Check "... names both accounts"            ($cap.Text.Contains("labadmin") -and $cap.Text.Contains("TIMS-10878\brett")) "True"

$d = New-InstallCase "inst_ok"
$script:HookAnswers.Enqueue($GoodHook)
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -LogbookPath $d.Logbook -SlackMemberId "U0ABC1234" }
Check "installed"                          (Last-Value $cap) "True"
Check "the task registered once"           $script:InstallCalls 1
Check "config: logbook"                    (Config-Field $d "logbook") $d.Logbook
Check "config: member ID"                  (Config-Field $d "memberId") "U0ABC1234"
Check "config: the share folders kept"     "$(Config-Field $d 'shareDir')|$(Config-Field $d 'hiveLogsDir')" "$($d.Share)|$($d.Hive)"
Check "config: an unset path left to its default" (Config-Field $d "procedureRoot") "<null>"
$cfgText = [System.IO.File]::ReadAllText((Join-Path $d.State "config.json"))
Check "config.json holds no webhook"       ($cfgText -match "hooks\.slack\.com|T0STAN") "False"
Check "the webhook stored"                 $script:StoredHook $GoodHook
Check "the test went out, with the mention" "$($script:Slack.Count)|$($script:Slack[0].text)" "1|$TestText"
Check "printed: route direct"              ($cap.Text -match "Test message route: direct") "True"
Check "printed: the task's account"        ($cap.Text -match "runs as: brett") "True"
Check "no account warning"                 ($cap.Text -match "WARNING") "False"
Check "printed: auto-logon"                ($cap.Text -match "Auto-logon after a reboot: off") "True"
Check "R17 printed: no window"             ($cap.Text -match "through wscript\.exe: no window") "True"
Check "printed: the phone settings"        ($cap.Text -match "Do Not Disturb") "True"
Check "never prints the webhook"           ($cap.Text.Contains("T0STAN")) "False"
Check "R16: no state.json, no delivered.txt" (R16-Clean $d) "False|False"

# Run again when up to date: Enter keeps both answers; no task change.
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -LogbookPath "" -TaskReady }
Check "re-run: fine"                       (Last-Value $cap) "True"
Check "re-run: no task change"             $script:InstallCalls 1
Check "re-run: the remembered logbook"     (Config-Field $d "logbook") $d.Logbook
Check "re-run: Enter kept the member ID"   (Config-Field $d "memberId") "U0ABC1234"
Check "re-run: Enter kept the webhook"     $script:StoredHook $GoodHook
Check "re-run: the test again"             $script:Slack.Count 2
$cfgText = [System.IO.File]::ReadAllText((Join-Path $d.State "config.json"))
Check "re-run, a webhook stored: config.json still holds none" ($cfgText -match "hooks\.slack\.com|T0STAN") "False"

# R17: a task that starts powershell.exe directly is said loudly.
$script:TaskExe = "powershell.exe"
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -TaskReady }
Check "R17: a console-flashing task is a WARNING" ($cap.Text -match "WARNING.*powershell\.exe.*flash") "True"
$script:TaskExe = 'C:\Windows\System32\wscript.exe'

# The task turns out to run as someone else: said loudly.
$script:TaskUser = "labadmin"
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -TaskReady }
Check "a task under another account is a WARNING" ($cap.Text -match "WARNING.*labadmin") "True"
$script:TaskUser = "brett"

# Slack refuses the webhook: the test still reaches Brett, through Hive.
$d = New-InstallCase "inst_hive"
$script:HookAnswers.Enqueue($GoodHook)
$script:SlackOk = $false
$cap = Invoke-Captured { Invoke-Install -StateDir $d.State -LogbookPath $d.Logbook -SlackMemberId "U0ABC1234" }
Check "Slack refused: still installed"     (Last-Value $cap) "True"
Check "printed: via Hive"                  ($cap.Text -match "Test message route: via Hive \(the PC could not reach Slack\)") "True"
$hf = @(Get-EventFiles $d.Share)
$hfWhat = "$($hf.Count)"
if ($hf.Count -ge 1) { $hfWhat = "$hfWhat|$(JStr $hf[0] 'type')|$(JStr $hf[0] 'posted_by_pc')" }
Check "a test event for Hive to relay"     $hfWhat "1|test|false"
Check "R16: no state.json, no delivered.txt" (R16-Clean $d) "False|False"

foreach ($n in $Stubbed.Keys) { Set-Item "function:script:$n" $Stubbed[$n] }

# ------------------------------------------------------------- 42. the .bat
Write-Host ""
Write-Host "42. watch_hystar_errors.bat: what Brett double-clicks"
$batPath = Join-Path $repoRoot "instrument/scripts/watch_hystar_errors.bat"
Check "the .bat ships next to the script" ([System.IO.File]::Exists($batPath)) "True"
if ([System.IO.File]::Exists($batPath)) {
    $bb = [System.IO.File]::ReadAllBytes($batPath)
    $nonAscii = 0; $lf = 0; $crlf = 0
    for ($i = 0; $i -lt $bb.Length; $i++) {
        if ($bb[$i] -gt 0x7F) { $nonAscii++ }
        if ($bb[$i] -eq 0x0A) { $lf++; if ($i -gt 0 -and $bb[$i - 1] -eq 0x0D) { $crlf++ } }
    }
    Check ".bat is ASCII"                     $nonAscii 0
    # .gitattributes keeps *.bat byte-for-byte; CMD mis-reads goto labels in
    # a file with bare LF line ends.
    Check ".bat lines end CR LF"              "$($lf -gt 0)|$($crlf -eq $lf)" "True|True"
    $bat = [System.IO.File]::ReadAllText($batPath)
    Check "runs the script beside it"         ($bat.Contains('set "PS1=%~dp0watch_hystar_errors.ps1"')) "True"
    Check "with -NoProfile -ExecutionPolicy Bypass" ($bat -match 'powershell\.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %EXTRA%') "True"
    Check "/test -> -Test"                    ($bat -match '(?im)^if /i "%~1"=="/test" set "EXTRA=-Test"\r?$') "True"
    Check "/uninstall -> -Uninstall"          ($bat -match '(?im)^if /i "%~1"=="/uninstall" set "EXTRA=-Uninstall"\r?$') "True"
    Check "/pause 8h -> -Pause 8h"             ($bat -match '(?im)^if /i "%~1"=="/pause" set "EXTRA=-Pause %~2"\r?$') "True"
    Check "/pause with no length -> usage"    ($bat -match '(?im)^if /i "%~1"=="/pause" if "%~2"=="" goto :usage\r?$') "True"
    Check "an unknown option -> usage, not an install" ($bat -match '(?im)^if not "%~1"=="" if not defined EXTRA goto :usage\r?$') "True"
    # Every flag the .bat hands the script must be one it declares: under
    # [CmdletBinding()] an unknown one kills the run before a line of it.
    $declaredFlags = @()
    foreach ($pp in $ast.ParamBlock.Parameters) { $declaredFlags += ("-" + $pp.Name.VariablePath.UserPath) }
    $undeclared = @()
    foreach ($m in [regex]::Matches($bat, 'EXTRA=(-[A-Za-z]+)')) {
        if ($declaredFlags -notcontains $m.Groups[1].Value) { $undeclared += $m.Groups[1].Value }
    }
    Check "every flag it passes is declared"  ($undeclared -join ",") ""
    Check "the script is [CmdletBinding()]"   ($null -ne $ast.ParamBlock -and @($ast.ParamBlock.Attributes | ForEach-Object { $_.TypeName.Name }) -contains "CmdletBinding") "True"
}



# ------------------------------------------------ 43. R17: no window, ever
Write-Host ""
Write-Host "43. R17: a tick starts with no window (wscript.exe + .vbs shim), or the install says why not"
$sysRoot = Join-Path $TickRoot "Windows"
$sys32 = Join-Path $sysRoot "System32"
[void][System.IO.Directory]::CreateDirectory($sys32)
$fakeWscript = Join-Path $sys32 "wscript.exe"
$fakeVbs = Join-Path $sys32 "vbscript.dll"
Write-Utf8File -Path $fakeWscript -Text "x"
Write-Utf8File -Path $fakeVbs -Text "x"
$inst = 'C:\ProgramData\STAN\watch_hystar_errors.ps1'
$shimP = 'C:\ProgramData\STAN\watch_hystar_errors.vbs'
# A VBS string literal is well formed when every quote inside it is doubled.
function Get-RunLine([string]$shim) {
    foreach ($ln in ($shim -split "`r`n")) { if ($ln.StartsWith("WScript.Quit")) { return $ln } }
    return ""
}
function Test-VbsLiteral([string]$line) {
    $m = [regex]::Match($line, '\.Run\("(.*)", 0, True\)$')
    if (-not $m.Success) { return $false }
    return (-not $m.Groups[1].Value.Replace('""', '').Contains('"'))
}
$L = Get-TaskLaunch -ScriptPath $inst -ShimPath $shimP -SystemRoot $sysRoot
Check "wscript.exe starts it"                $L.Execute $fakeWscript
Check "//B //Nologo and the shim, quoted"    $L.Argument '//B //Nologo "C:\ProgramData\STAN\watch_hystar_errors.vbs"'
Check "no fallback reason"                   $L.Why ""
$wantRun = 'WScript.Quit CreateObject("WScript.Shell").Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\ProgramData\STAN\watch_hystar_errors.ps1"" -Scheduled", 0, True)'
Check "the shim: hidden (0), waits (True), the installed copy, -Scheduled" (Get-RunLine $L.Shim) $wantRun
$stmts = 0
foreach ($ln in ($L.Shim -split "`r`n")) { if ($ln.Trim() -ne "" -and -not $ln.StartsWith("'")) { $stmts++ } }
Check "one statement, the rest comments"     $stmts 1
Check "the VBS string literal is well formed" (Test-VbsLiteral (Get-RunLine $L.Shim)) "True"
Check "the shim is ASCII"                    ([regex]::IsMatch($L.Shim, '[^\x00-\x7F]')) "False"
Check "CRLF line ends throughout"            "$($L.Shim.Replace("`r`n", '').Contains("`n"))|$($L.Shim.EndsWith("`r`n"))" "False|True"
$instSp = 'C:\Program Data\STAN tools\watch_hystar_errors.ps1'
$shimSp = 'C:\Program Data\STAN tools\watch_hystar_errors.vbs'
$Ls = Get-TaskLaunch -ScriptPath $instSp -ShimPath $shimSp -SystemRoot $sysRoot
Check "spaces: the task argument quotes the shim" $Ls.Argument '//B //Nologo "C:\Program Data\STAN tools\watch_hystar_errors.vbs"'
Check "spaces: the shim doubles the quotes"  ((Get-RunLine $Ls.Shim).Contains('-File ""C:\Program Data\STAN tools\watch_hystar_errors.ps1"" -Scheduled')) "True"
Check "spaces: still a well-formed VBS literal" (Test-VbsLiteral (Get-RunLine $Ls.Shim)) "True"
$direct = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\ProgramData\STAN\watch_hystar_errors.ps1" -Scheduled'
[System.IO.File]::Delete($fakeVbs)
$Lf = Get-TaskLaunch -ScriptPath $inst -ShimPath $shimP -SystemRoot $sysRoot
Check "no VBScript: powershell.exe, no shim" "$($Lf.Execute)|$($null -eq $Lf.Shim)" "powershell.exe|True"
Check "no VBScript: the installed copy, -Scheduled" $Lf.Argument $direct
Check "no VBScript: the reason says so"      ($Lf.Why -match "VBScript") "True"
Write-Utf8File -Path $fakeVbs -Text "x"
[System.IO.File]::Delete($fakeWscript)
$Lw = Get-TaskLaunch -ScriptPath $inst -ShimPath $shimP -SystemRoot $sysRoot
Check "no wscript.exe: powershell.exe"       "$($Lw.Execute)|$($Lw.Why -match 'wscript\.exe')" "powershell.exe|True"
Write-Utf8File -Path $fakeWscript -Text "x"
Check "no SystemRoot: powershell.exe"        (Get-TaskLaunch -ScriptPath $inst -ShimPath $shimP -SystemRoot "").Execute "powershell.exe"
$La = Get-TaskLaunch -ScriptPath "C:\ProgramData\STAN$([char]0x00E9)\w.ps1" -ShimPath $shimP -SystemRoot $sysRoot
Check "a non-ASCII path: powershell.exe"     "$($La.Execute)|$($La.Why -match 'ASCII')" "powershell.exe|True"
$Lx = Get-TaskLaunch -ScriptPath $inst -ShimPath $shimP -SystemRoot $sysRoot -Fallback "the shim could not be written"
Check "-Fallback: powershell.exe, that reason" "$($Lx.Execute)|$($Lx.Why)|$($Lx.Argument)" "powershell.exe|the shim could not be written|$direct"
$itText = $it.Extent.Text
Check "Install-Task writes the shim as ASCII" ($itText -match 'WriteAllText\(\$shimPath, \$launch\.Shim, \[System\.Text\.Encoding\]::ASCII\)') "True"
Check "a failed shim write falls back"       ($itText -match 'Get-TaskLaunch -ScriptPath \$InstalledPath -ShimPath \$shimPath -Fallback') "True"
Check "Install-Task warns when it falls back" ($itText -match '(?s)if \(-not \[string\]::IsNullOrEmpty\(\$launch\.Why\)\) \{\s*Say "WARNING') "True"

# The update check looks at the launcher, not only the version: a task
# registered at this version without the shim (or with a stale one) is
# registered again.
$shimDir = Join-Path $TickRoot "shim"
[void][System.IO.Directory]::CreateDirectory($shimDir)
$shimFile = Join-Path $shimDir "watch_hystar_errors.vbs"
$Lc = Get-TaskLaunch -ScriptPath $inst -ShimPath $shimFile -SystemRoot $sysRoot
[System.IO.File]::WriteAllText($shimFile, $Lc.Shim, [System.Text.Encoding]::ASCII)
Check "as wanted, shim in place: current"    (Test-TaskLaunchCurrent -RegisteredExecute $Lc.Execute -Launch $Lc -ShimPath $shimFile) "True"
Check "registered as powershell.exe: re-register" (Test-TaskLaunchCurrent -RegisteredExecute "powershell.exe" -Launch $Lc -ShimPath $shimFile) "False"
[System.IO.File]::WriteAllText($shimFile, $Lc.Shim.Replace(", 0, True)", ", 1, False)"), [System.Text.Encoding]::ASCII)
Check "a shim that differs: re-register"     (Test-TaskLaunchCurrent -RegisteredExecute $Lc.Execute -Launch $Lc -ShimPath $shimFile) "False"
[System.IO.File]::Delete($shimFile)
Check "the shim gone: re-register"           (Test-TaskLaunchCurrent -RegisteredExecute $Lc.Execute -Launch $Lc -ShimPath $shimFile) "False"
Check "the task unreadable: no forced update" (Test-TaskLaunchCurrent -RegisteredExecute "" -Launch $Lc -ShimPath $shimFile) "True"
Check "no VBScript here, powershell.exe registered: current" (Test-TaskLaunchCurrent -RegisteredExecute "powershell.exe" -Launch $Lf -ShimPath $shimFile) "True"
$topText = ""
foreach ($s in $ast.EndBlock.Statements) {
    if (-not ($s -is [System.Management.Automation.Language.FunctionDefinitionAst])) { $topText = "$topText`n$($s.Extent.Text)" }
}
Check "the update check uses it"             ($topText -match '\$installedVersion -eq \$ScriptVersion -and \$launchOk') "True"
Check "on the registered task's program"     ($topText -match 'Test-TaskLaunchCurrent -RegisteredExecute \(Get-TaskExecute\)') "True"

# The 5-minute limit kills wscript.exe, not the PowerShell it started, and
# IgnoreNew then lets the next tick start: one tick at a time, by a lock.
$lockName = "Local\STAN_tick_test_$PID"
$ready = New-Object System.Threading.ManualResetEventSlim $false
$release = New-Object System.Threading.ManualResetEventSlim $false
$holder = [powershell]::Create().AddScript({
    param($n, $ready, $release)
    $m = New-Object System.Threading.Mutex($false, $n)
    $null = $m.WaitOne(0)
    $ready.Set()
    $null = $release.Wait(10000)
    $m.ReleaseMutex()
    $m.Dispose()
}).AddArgument($lockName).AddArgument($ready).AddArgument($release)
$hh = $holder.BeginInvoke()
$null = $ready.Wait(10000)
Check "a tick still running: the next one skips" (Enter-TickLock -Name $lockName) "False"
$release.Set()
$null = $holder.EndInvoke($hh)
$holder.Dispose()
Check "once it has ended: the next one runs" (Enter-TickLock -Name $lockName) "True"
if ($null -ne $script:WatchTickMutex) { $script:WatchTickMutex.ReleaseMutex(); $script:WatchTickMutex.Dispose(); $script:WatchTickMutex = $null }
$schedText = ""
if ($iSched -ge 0) { $schedText = $top[$iSched].Extent.Text }
$iLock = $schedText.IndexOf("Enter-TickLock")
$iTick = $schedText.IndexOf("Invoke-WatchTick")
Check "the scheduled branch takes the lock before the tick" (($iLock -ge 0) -and ($iLock -lt $iTick)) "True"
Check "and leaves without a tick when it is held" ($schedText -match '(?s)if \(-not \(Enter-TickLock\)\) \{.*?exit 0') "True"

# =============================================================================
# Final whole-branch review: the fixes (final-findings.md items 1-8).

# A procedure folder <root>\<serial>\<name> holding a journal.txt for Vial.
function New-Procedure([string]$root, [string]$name, [string]$vial) {
    $dir = Join-Path (Join-Path $root "S00230") $name
    [void][System.IO.Directory]::CreateDirectory($dir)
    $j = "53621`r`nVersion:1.0`r`nProcedure.Name:100 samples per day`r`nProcedure.Logname:$name`r`nProcedure.Vialposition:Slot1:5 ($vial)`r`n"
    [System.IO.File]::WriteAllText((Join-Path $dir "journal.txt"), $j)
}
# Hive's tick against <events root>, with a JSON alert memory that survives
# between runs and a fake Slack: what it relayed, recorded, and posted.
$HiveTickPy = @'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
import stan.reports.hystar_events as he
from stan.notify import AlertStore
posts = []
def fake_post(payload, webhook=None):
    posts.append(payload)
    return True
he.post_slack = fake_post
store = AlertStore(use_pg=False, path=Path(sys.argv[2]))
res = he.run_hystar_tick(Path(sys.argv[1]), store=store, now=datetime.now(timezone.utc))
print(json.dumps({
    "relayed": len(res["relayed"]), "recorded": len(res["recorded"]),
    "already": len(res["already"]), "errors": len(res["errors"]),
    "posts": len(posts),
    "mentions": sum(1 for p in posts if "<@" in str(p.get("text"))),
    "texts": [str(p.get("text")) for p in posts],
}))
'@

# ------------------------------------------ 44. R20: the vial, by category
Write-Host ""
Write-Host "44. R20: a vial only where it can be right (evotip 10 min; MS/pressure 90 min; else none)"
# The fixture's 100-samples-per-day_2026-10-24_02-03-36 holds S1-A5.
Check "evotip 48 s after the start: its vial"    (Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 02:04:24") -Category "evotip") "S1-A5"
Check "evotip exactly 10 min after: its vial"     (Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 02:13:36") -Category "evotip") "S1-A5"
Check "evotip 11 min after: none (the previous, finished sample)" ((Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 02:14:40") -Category "evotip") -eq $null) "True"
foreach ($c in @("overpressure", "connection_lost", "ms_error")) {
    Check "$($c) 56 min into the run: its vial"     (Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 03:00:00") -Category $c) "S1-A5"
    Check "$($c) 91 min after the start: none"      ((Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 03:34:40") -Category $c) -eq $null) "True"
}
foreach ($c in @("evosep_hw", "hystar_crash", "other", "")) {
    Check "'$($c)': never a vial, even 48 s after a start" ((Get-EvosepVial -ProcedureRoot $procRoot -At ([datetime]"2026-10-24 02:04:24") -Category $c) -eq $null) "True"
}
# Through the tick: each stop asks for its own category's window.
$d = New-TickDirs "r20"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 02:00:00")
Add-Logbook $d.Logbook ((Rec-Stop ([datetime]"2026-10-24 02:04:24") $FanMsg) + (Rec-Stop ([datetime]"2026-10-24 02:16:00") $EvotipMsg) + (Rec-Stop ([datetime]"2026-10-24 02:30:00") $PressureMsg))
$null = Run-Tick $d ([datetime]"2026-10-24 02:31:00")
$byTitle = @{}
foreach ($f in (Get-EventFiles $d.Share)) { $byTitle[(JStr $f "title")] = (JStr $f "vial") }
Check "tick: a fan fault 48 s after a start names no vial" $byTitle["Evosep hardware fault"] "<null>"
Check "tick: an Evotip 12 min after the start names none"  $byTitle["No Evotip"] "<null>"
Check "tick: a pressure stop 26 min into the run names it" $byTitle["Pressure limit"] "S1-A5"
Check "tick: the vial-less Evotip check line (R11)" (Count-Posts "*queue STOPPED*No Evotip*") 1
$evoPost = $null
foreach ($pp in $script:Posts) { if ($pp.text -like "*No Evotip*") { $evoPost = $pp } }
if ($null -ne $evoPost) {
    Check "tick: Evotip post says 'the Evotip position'" ($evoPost.blocks[1].text.text.Contains("the Evotip position")) "True"
}

# ------------------------------------------- 45. R20: DST, the repeated hour
Write-Host ""
Write-Host "45. R20: no vial or sample at an ambiguous local time (Sun 2026-11-01 01:00-02:00)"
$pacific = $null
foreach ($tzId in @("America/Los_Angeles", "Pacific Standard Time")) {
    if ($null -eq $pacific) {
        try { $pacific = [System.TimeZoneInfo]::FindSystemTimeZoneById($tzId) } catch { $pacific = $null }
    }
}
Check "Get-EvosepVial: -TimeZone defaults to the PC's zone"  (Get-FunctionParamDefault (Get-FunctionAst "Get-EvosepVial") "TimeZone") "[System.TimeZoneInfo]::Local"
Check "Get-HyStarSample: -TimeZone defaults to the PC's zone" (Get-FunctionParamDefault (Get-FunctionAst "Get-HyStarSample") "TimeZone") "[System.TimeZoneInfo]::Local"
if ($null -eq $pacific) {
    Write-Host "  skip (no Pacific time zone on this machine)"
}
else {
    # By name, 01:07 (the first, PDT, pass) outranks 01:05 (the second, PST)
    # for a 01:10 PST stop; either could be the right one, so neither is said.
    $dstProc = Join-Path $TickRoot "dst_procedures"
    New-Procedure $dstProc "100-samples-per-day_2026-11-01_01-05-00" "S1-C5"
    New-Procedure $dstProc "100-samples-per-day_2026-11-01_01-07-00" "S1-C7"
    $amb = [datetime]"2026-11-01 01:10:00"
    Check "01:10 on 2026-11-01 is ambiguous in Pacific time"   $pacific.IsAmbiguousTime($amb) "True"
    Check "vial: none at an ambiguous time"      ((Get-EvosepVial -ProcedureRoot $dstProc -At $amb -Category "evotip" -TimeZone $pacific) -eq $null) "True"
    Check "vial: found where the hour is not repeated" (Get-EvosepVial -ProcedureRoot $dstProc -At $amb -Category "evotip" -TimeZone ([System.TimeZoneInfo]::Utc)) "S1-C7"
    Check "vial: after the repeated hour, as usual" (Get-EvosepVial -ProcedureRoot $dstProc -At ([datetime]"2026-11-01 02:10:00") -Category "overpressure" -TimeZone $pacific) "S1-C7"
    $dstLog = Join-Path $TickRoot "dst_hystar.log"
    [System.IO.File]::WriteAllText($dstLog, "2026/11/01 01:06:00,100 PHASE.INJECTED D:\Data\dst_sample_S1-C7_1_4821.d`r`n", [System.Text.Encoding]::Unicode)
    Check "sample: none at an ambiguous time"    ((Get-HyStarSample -LogbookPath $dstLog -At $amb -TimeZone $pacific) -eq $null) "True"
    Check "sample: found where the hour is not repeated" (Get-HyStarSample -LogbookPath $dstLog -At $amb -TimeZone ([System.TimeZoneInfo]::Utc)) "dst_sample"
}

# ------------------------------------- 46. #8: list only the days that can match
Write-Host ""
Write-Host "46. the vial lookup lists only today's/yesterday's procedure names, as Test-InjectionSince does"
$gev = Get-FunctionAst "Get-EvosepVial"
$unfiltered = 0
foreach ($m in $gev.FindAll({ $args[0] -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
    if ($m.Member.Extent.Text -ne "GetDirectories") { continue }
    if ($m.Arguments.Count -lt 2 -and $m.Arguments[0].Extent.Text -ne '$ProcedureRoot') { $unfiltered++ }
}
Check "no serial folder is listed without a day pattern" $unfiltered 0
Check "Get-EvosepVial takes its patterns from Get-ProcedureDayPatterns" (@(Find-Commands $gev "Get-ProcedureDayPatterns").Count -ge 1) "True"
Check "and so does Test-InjectionSince" (@(Find-Commands (Get-FunctionAst "Test-InjectionSince") "Get-ProcedureDayPatterns").Count -ge 1) "True"
Check "patterns: one day"       (@(Get-ProcedureDayPatterns -From ([datetime]"2026-10-24 00:34:24") -To ([datetime]"2026-10-24 02:04:24")) -join ",") "*_2026-10-24_*"
Check "patterns: over midnight" (@(Get-ProcedureDayPatterns -From ([datetime]"2026-10-23 22:50:00") -To ([datetime]"2026-10-24 00:20:00")) -join ",") "*_2026-10-23_*,*_2026-10-24_*"
Check "patterns: > 2 days, all" (@(Get-ProcedureDayPatterns -From ([datetime]"2026-10-20 00:00:00") -To ([datetime]"2026-10-24 00:20:00")) -join ",") "*"
$midProc = Join-Path $TickRoot "midnight_procedures"
New-Procedure $midProc "100-samples-per-day_2026-10-23_23-50-00" "S1-D2"
New-Procedure $midProc "100-samples-per-day_2026-10-22_23-55-00" "S1-D9"
Check "a run started before midnight still names its vial" (Get-EvosepVial -ProcedureRoot $midProc -At ([datetime]"2026-10-24 00:20:00") -Category "overpressure") "S1-D2"

# --------------------------------------------- 47. R21: a UTC start boundary
Write-Host ""
Write-Host "47. R21: the every-minute trigger starts at a UTC time (the fall-back hour cannot pause it)"
$nb = [datetime]"2026-11-01 01:30:00"
$ub = Get-UtcStartBoundary -Now $nb
Check "boundary ends with Z"               $ub.EndsWith("Z") "True"
Check "boundary is plain ISO, seconds, Z"  ($ub -match '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') "True"
Check "a minute from now, in UTC"          $ub "$($nb.ToUniversalTime().AddMinutes(1).ToString("yyyy-MM-dd'T'HH:mm:ss", $Inv))Z"
$it = Get-FunctionAst "Install-Task"
$regOff = -1
foreach ($r in (Find-Commands $it "Register-ScheduledTask")) { $regOff = $r.Extent.StartOffset }
$utcOff = -1; $localSaved = -1; $localBack = $false
foreach ($a in $it.FindAll({ $args[0] -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
    $lt = $a.Left.Extent.Text; $rt = $a.Right.Extent.Text
    if ($lt -eq '$repeat.StartBoundary' -and $rt -match 'Get-UtcStartBoundary') { $utcOff = $a.Extent.StartOffset }
    if ($lt -eq '$localStart' -and $rt -eq '$repeat.StartBoundary') { $localSaved = $a.Extent.StartOffset }
    if ($lt -eq '$repeat.StartBoundary' -and $rt -eq '$localStart') { $localBack = $true }
}
Check "Install-Task sets the UTC boundary before registering" (($utcOff -ge 0) -and ($utcOff -lt $regOff)) "True"
Check "having kept the local one first"     (($localSaved -ge 0) -and ($localSaved -lt $utcOff)) "True"
Check "and falls back to it if refused"     $localBack "True"
$warnUtc = $false
foreach ($sc in $it.FindAll({ $args[0] -is [System.Management.Automation.Language.ExpandableStringExpressionAst] -or $args[0] -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)) {
    if ($sc.Value -match '^WARNING' -and $sc.Value -match 'UTC') { $warnUtc = $true }
}
Check "with a printed WARNING"              $warnUtc "True"
Check "still registered in one place"       (@(Find-Commands $it "Register-ScheduledTask").Count) 1
$ii = Get-FunctionAst "Invoke-Install"
Check "the install reads the trigger back"  (@(Find-Commands $ii "Get-TaskRepeatBoundary").Count -ge 1) "True"
$gtb = Get-FunctionAst "Get-TaskRepeatBoundary"
Check "from the registered task"            (@(Find-Commands $gtb "Get-ScheduledTask").Count -ge 1) "True"
$rb = Get-TriggerReadback -Boundary "2026-10-27T21:31:00Z"
Check "read-back: UTC is fine"              "$($rb.Color)|$($rb.Text.StartsWith('WARNING'))" "Gray|False"
$rb = Get-TriggerReadback -Boundary "2026-10-27T14:31:00"
Check "read-back: local time is a red WARNING" "$($rb.Color)|$($rb.Text.StartsWith('WARNING'))" "Red|True"
$rb = Get-TriggerReadback -Boundary ""
Check "read-back: unreadable says so"       "$($rb.Color)|$($rb.Text -match 'not read')" "Yellow|True"

# ---------------------------------- 48. #1: a state folder that cannot be written
Write-Host ""
Write-Host "48. state folder not writable: nothing posted directly, so nothing pages twice; Hive relays once"
if ($null -ne (Get-Command chmod -ErrorAction SilentlyContinue)) {
    $d = New-TickDirs "ro_state"
    $eventsRoot = Join-Path $d.Root "events"
    $d.Share = Join-Path $eventsRoot "TIMS-10878"
    [void][System.IO.Directory]::CreateDirectory($d.Share)
    $hiveStore = Join-Path $d.Root "hive_alert_state.json"
    Write-Logbook $d.Logbook (Rec-Header)
    Reset-Posts
    $t48 = [datetime]"2026-10-27 02:00:00"
    $null = Run-Tick $d $t48
    Check "seeded while the folder was writable" ([System.IO.File]::Exists((Join-Path $d.State "state.json"))) "True"
    & chmod 555 $d.State
    try {
        Add-Logbook $d.Logbook (Rec-Stop $t48.AddMinutes(1) $EvotipMsg)
        $ids = @{}; $allUnposted = $true; $hbErr = ""; $relayed = 0; $hivePosts = 0; $hiveRuns = 0
        for ($i = 1; $i -le 5; $i++) {
            $null = Run-Tick $d $t48.AddMinutes(1 + $i)
            foreach ($f in (Get-EventFiles $d.Share)) {
                $ids[(JStr $f "id")] = 1
                if ((JStr $f "posted_by_pc") -ne "false") { $allUnposted = $false }
            }
            $hbErr = JStr (Join-Path $d.Share "heartbeat.json") "last_error"
            $hr = Run-HivePython $HiveTickPy @($eventsRoot, $hiveStore)
            if ($null -ne $hr) { $hiveRuns++; $relayed += [int]$hr.relayed; $hivePosts += [int]$hr.posts }
        }
        Check "5 ticks: no direct post at all"        $script:Posts.Count 0
        Check "the stop reached the share every tick" ($ids.Count -ge 1) "True"
        Check "always the same event id"              $ids.Count 1
        Check "always posted_by_pc false"             $allUnposted "True"
        Check "heartbeat last_error names the folder" ($hbErr -match 'state folder') "True"
        Check "watch.log says so (it is still writable)" ((Read-WatchLogText $d) -match 'state folder') "True"
        if ($hiveRuns -eq 5) {
            Check "Hive relays it exactly once"       "$relayed|$hivePosts" "1|1"
        }
    }
    finally { & chmod 755 $d.State }
    # Writable again: the next tick posts directly as usual. It re-reads from
    # the last offset it could save, so a stop from the unwritable spell that
    # is under 2 h old is posted once more (known residual: nothing on the PC
    # could remember that Hive relayed it); older ones go into the summary.
    Add-Logbook $d.Logbook (Rec-Stop $t48.AddMinutes(20) $PressureMsg)
    $null = Run-Tick $d $t48.AddMinutes(21)
    $null = Run-Tick $d $t48.AddMinutes(22)
    Check "writable again: the next stop posts directly" (Count-Posts "*queue STOPPED*Pressure limit*") 1
    Check "the spell's stop: once more at most, then never" (Count-Posts "*No Evotip*") 1
}
else {
    Write-Host "  skip (no chmod here to make the state folder read-only)"
}

# ------------------------------------------------ 49. R19: the age cap
Write-Host ""
Write-Host "49. R19: stops over 2 h old when read are filed, not paged; one quiet summary per tick"
Check "2 h old exactly: not stale" (Test-StaleRecord -At ([datetime]"2026-10-24 10:00:00") -Now ([datetime]"2026-10-24 12:00:00")) "False"
Check "2 h 1 min old: stale"       (Test-StaleRecord -At ([datetime]"2026-10-24 09:59:00") -Now ([datetime]"2026-10-24 12:00:00")) "True"
Check "after now (clock back): not stale" (Test-StaleRecord -At ([datetime]"2026-10-24 12:30:00") -Now ([datetime]"2026-10-24 12:00:00")) "False"
$d = New-TickDirs "stale"
$eventsRoot = Join-Path $d.Root "events"
$d.Share = Join-Path $eventsRoot "TIMS-10878"
[void][System.IO.Directory]::CreateDirectory($d.Share)
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 06:00:00")
# The PC was off from 06:00 to 12:00; five stops 10 min apart were logged.
$old = ""
for ($i = 0; $i -lt 5; $i++) { $old = "$old$(Rec-Stop ([datetime]"2026-10-24 08:00:00").AddMinutes(10 * $i) $PressureMsg)" }
Add-Logbook $d.Logbook $old
$null = Run-Tick $d ([datetime]"2026-10-24 12:00:00")
Check "5 old stops: exactly one post"        $script:Posts.Count 1
Check "and it @mentions no one"              (Count-Posts "*<@*") 0
if ($script:Posts.Count -ge 1) {
    Check "the summary" $script:Posts[0].text ":large_orange_diamond: TIMS-10878 $dash 5 older stops while the watcher wasn't running (latest 08:40)"
}
$staleN = 0; $staleOk = $true; $sumFile = $null
foreach ($f in (Get-EventFiles $d.Share)) {
    $t = JStr $f "type"
    if ($t -eq "stop") {
        $staleN++
        if ((JStr $f "stale") -ne "true" -or (JStr $f "posted_by_pc") -ne "true" -or (JStr $f "posted_at") -ne "<null>") { $staleOk = $false }
    }
    if ($t -eq "stale_summary") { $sumFile = $f }
}
Check "5 stop files"                          $staleN 5
Check "each stale, posted_by_pc true, never posted" $staleOk "True"
Check "plus the summary's own file"           ($null -ne $sumFile) "True"
if ($null -ne $sumFile) {
    Check "summary: posted by the PC, counts 5" "$(JStr $sumFile 'posted_by_pc')|$(JStr $sumFile 'count')" "true|5"
}
$null = Run-Tick $d ([datetime]"2026-10-24 12:01:00")
Check "the next tick says nothing more"       $script:Posts.Count 1
$hr = Run-HivePython $HiveTickPy @($eventsRoot, (Join-Path $d.Root "hive_alert_state.json"))
if ($null -ne $hr) {
    Check "Hive records all six, relays none"   "$($hr.recorded)|$($hr.relayed)|$($hr.posts)" "6|0|0"
}
# No route to Slack: the stale stops are still never relayed one by one;
# Hive relays the one summary, unmentioned.
$d = New-TickDirs "stale_noroute"
$eventsRoot = Join-Path $d.Root "events"
$d.Share = Join-Path $eventsRoot "TIMS-10878"
[void][System.IO.Directory]::CreateDirectory($d.Share)
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-24 06:00:00")
Add-Logbook $d.Logbook $old
$script:SendOk = $false; $script:SendStatus = 404
$null = Run-Tick $d ([datetime]"2026-10-24 12:00:00")
Check "no route: the summary was tried once" $script:Posts.Count 1
$hr = Run-HivePython $HiveTickPy @($eventsRoot, (Join-Path $d.Root "hive_alert_state.json"))
if ($null -ne $hr) {
    Check "Hive relays only the summary"        "$($hr.relayed)|$($hr.recorded)|$($hr.posts)|$($hr.mentions)" "1|5|1|0"
}
# One old stop and one fresh one: the fresh one pages, the old one is summed
# up (a different day: the date is given).
$d = New-TickDirs "stale_mixed"
Write-Logbook $d.Logbook (Rec-Header)
Reset-Posts
$null = Run-Tick $d ([datetime]"2026-10-22 20:00:00")
Add-Logbook $d.Logbook ((Rec-Stop ([datetime]"2026-10-22 23:15:00") $FanMsg) + (Rec-Stop ([datetime]"2026-10-24 12:29:00") $PressureMsg))
$null = Run-Tick $d ([datetime]"2026-10-24 12:30:00")
Check "mixed: the fresh stop pages"          (Count-Posts "<@U0ABC1234> *queue STOPPED*Pressure limit*") 1
Check "mixed: the old one is summed up"      (Count-Posts ":large_orange_diamond: TIMS-10878 $dash 1 older stop while the watcher wasn't running (latest 2026-10-22 23:15)") 1
Check "mixed: nothing else"                  $script:Posts.Count 2

Remove-Item -LiteralPath $TickRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
if ($Failures -gt 0) { Write-Host "$Failures FAILED"; exit 1 }
Write-Host "all ok"
exit 0
