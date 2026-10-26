<#
.SYNOPSIS
    Read-only watcher for HyStar's error logbook (CompassHyStarErrorLogbook.log):
    @mentions Brett in Slack within a minute when HyStar stops the
    acquisition queue.

.DESCRIPTION
    Runs once a minute on the timsTOF instrument PC (PowerShell 5.1), tails the
    logbook and alerts on every EmergencyShutdown, the event that stops the
    acquisition queue. Each alert is posted to Slack directly when the PC can
    reach it, and always written as an event file on the share. Hive
    (scripts/cron_hystar_events.sh) relays whatever the PC could not post, and
    pages when the watcher's heartbeat stops.

    IT INSTALLS ITSELF. Double-click watch_hystar_errors.bat and say Yes to
    the administrator prompt. The install finds the logbook, asks for the
    Slack member ID and the Slack webhook, copies this script to
    C:\ProgramData\STAN, registers the scheduled task 'STAN HyStar watcher'
    (every minute, as the desktop's user, not elevated) and sends a test
    message with the @mention: the phone should buzz. Running the .bat again
    updates an older install, or re-checks the settings (Enter keeps each).

    NO WINDOW, EVER. The task starts wscript.exe on a small .vbs shim
    (C:\ProgramData\STAN\watch_hystar_errors.vbs) that runs each tick with
    no window, because powershell.exe -WindowStyle Hidden still flashes a
    console, 1,440 times a day, on the PC the operators use. Where
    wscript.exe or VBScript is missing, the task starts powershell.exe
    itself and the install says so in red. One tick runs at a time.

    THE WEBHOOK is asked for without echo and kept only in
    C:\ProgramData\STAN\hystar_watch\webhook.dpapi, encrypted with DPAPI for
    the account that installed it (the one the task runs as) and readable by
    that account and Administrators alone. It never appears in a log, an
    event file, config.json or on the share.

    STATE lives in C:\ProgramData\STAN\hystar_watch: config.json (non-secret
    settings, written at install), state.json and delivered.txt (the tick's
    own, seeded by its first run), pending\ (event files the share has not
    taken yet) and watch.log.

    Log format, as measured on real files (see the design spec, section 3):
      * UTF-16LE with a BOM (FF FE).
      * Every record starts with a DC3 byte (0x13) and ends with 0x11 CR LF.
      * A message may contain a bare LF (the Evotip text does). Exception
        records are followed by stack lines with no 0x13 prefix.
      * Record kinds by prefix: E: error, S: session start (HyStar restarted),
        H: header/footer, D: debug.
    Read-only: every Bruker and Evosep file is opened for reading with
    FileShare ReadWrite + Delete, so HyStar can keep writing it AND rename it
    (logbook rotation) while it is open here; nothing of theirs is written.

.PARAMETER Scheduled
    Set by the scheduled task only. Never type it: one tick, no console.

.PARAMETER Uninstall
    Remove the scheduled task. The state folder (config.json, the stored
    webhook) is left in place, and its path printed.

.PARAMETER Pause
    Planned downtime, in hours or days: 8h, 2d (up to 31d; 0h ends a pause).
    Writes pause_until.json next to the event files, and Hive does not page
    "watcher down" before then. Queue-stop alerts are not paused.

.PARAMETER Test
    Send the install test message again, and say which route it took.

.PARAMETER SlackMemberId
    The Slack member ID to @mention (U...), instead of being asked.

.PARAMETER LogbookPath
    HyStar's error logbook, when it is not under C:\ or D:\BDalSystemData.

.EXAMPLE
    watch_hystar_errors.bat              # install, update, or re-check
    watch_hystar_errors.bat /test        # the test message again
    watch_hystar_errors.bat /pause 8h    # service visit: no watcher-down page for 8 h
    watch_hystar_errors.bat /uninstall   # remove the scheduled task
#>
[CmdletBinding()]
param(
    [switch]$Scheduled,
    [switch]$Uninstall,
    [string]$Pause = "",
    [switch]$Test,
    [string]$SlackMemberId = "",
    [string]$LogbookPath = "",
    # The elevated half of the self-install re-launches the script with
    # this. It MUST be a declared parameter: under [CmdletBinding()]
    # PowerShell rejects an unknown named parameter outright, so the
    # elevated child would die on parameter binding. Not for typing.
    [switch]$InstallOnly
)

# Kept in step with stan/__init__.py by tests/test_hystar_watcher.ps1 and
# tests/test_instrument_copy_scripts.ps1, which fail if they drift. This
# script lives on the instrument PC on its own, so "is the copy in front of
# me current?" has to be answerable without a git checkout.
$ScriptVersion = '1.2.23'

function Read-NewLogText {
    <#
    Reads the bytes from Offset to the end of the file as UTF-16LE.
    Returns @{ Text; NewOffset; Length }. Length is the file size seen, so the
    caller can tell the file shrank (rotation) when Length < Offset.
    Offset 0 skips the 2-byte BOM; an odd Offset is rounded down to even.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [long]$Offset = 0
    )
    # Delete in the share mode: without it HyStar could not rename (rotate)
    # the logbook while this handle is open.
    $fs = New-Object System.IO.FileStream($Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
    try {
        $length = $fs.Length
        $start = $Offset
        if ($start -lt 0) { $start = 0 }
        if (($start % 2) -ne 0) { $start = $start - 1 }
        if ($start -lt 2) { $start = 2 }
        if ($start -gt $length) {
            return @{ Text = ""; NewOffset = $Offset; Length = $length }
        }
        $count = $length - $start
        if (($count % 2) -ne 0) { $count = $count - 1 }
        if ($count -le 0) {
            return @{ Text = ""; NewOffset = $start; Length = $length }
        }
        $buf = New-Object byte[] ([int]$count)
        [void]$fs.Seek($start, [System.IO.SeekOrigin]::Begin)
        $got = 0
        while ($got -lt $count) {
            $n = $fs.Read($buf, $got, ([int]$count - $got))
            if ($n -le 0) { break }
            $got += $n
        }
        if (($got % 2) -ne 0) { $got = $got - 1 }
        $text = [System.Text.Encoding]::Unicode.GetString($buf, 0, $got)
        return @{ Text = $text; NewOffset = ($start + $got); Length = $length }
    }
    finally {
        $fs.Dispose()
    }
}

function Clean-LogRecord {
    # Drops the DC3 record marker, the 0x11 line terminator and CRs; keeps
    # bare LFs (embedded newlines) so continuation lines stay visible.
    param([string]$Raw)
    $s = $Raw.Replace([string][char]0x11, "").Replace("`r", "")
    $s = $s.TrimStart([char]0x13)
    return $s.TrimEnd("`n")
}

function Split-LogRecords {
    <#
    Splits Text into whole records. Returns @{ Records = <string[]>;
    ConsumedChars = <int> }; the caller advances its offset by the bytes that
    ConsumedChars covers and leaves the rest for the next tick.

    The LAST record is held back (not consumed) unless the text ends with a
    newline AND it is not an EmergencyShutdown missing its closing !!!. A held
    record is released anyway once PendingSince is 2 minutes or more before
    Now, so one malformed record cannot block the log forever.
    Text before the first record start (the tail of a record cut by an
    offset) is debris and counts as consumed.
    #>
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
        $PendingSince = $null,
        $Now = $null
    )
    $records = New-Object System.Collections.ArrayList
    if ($Now -eq $null) { $Now = Get-Date }
    # A record starts at a line beginning with an optional DC3 byte, then E: S:
    # H: or D:, then a year (E:/S:/D: lines) or a run of # (H: lines). The
    # follow-on keeps a stray "D:\path" in a stack line from reading as a
    # record. Kept local: tests load only the functions, not top-level state.
    $startPattern = '(?m)^\x13?[EHSD]:(?=\d{4}/|#)'
    $matches2 = [regex]::Matches($Text, $startPattern)
    $endsWithNewline = ($Text.Length -gt 0) -and ($Text[$Text.Length - 1] -eq "`n")

    if ($matches2.Count -eq 0) {
        $consumed = 0
        if ($endsWithNewline) { $consumed = $Text.Length }
        return @{ Records = @(); ConsumedChars = $consumed }
    }

    $released = $false
    if ($PendingSince -ne $null) {
        if ((($Now - $PendingSince).TotalMinutes) -ge 2) { $released = $true }
    }

    $consumed = $Text.Length
    for ($i = 0; $i -lt $matches2.Count; $i++) {
        $from = $matches2[$i].Index
        $isLast = ($i -eq ($matches2.Count - 1))
        if ($isLast) { $to = $Text.Length } else { $to = $matches2[$i + 1].Index }
        $rec = Clean-LogRecord $Text.Substring($from, ($to - $from))
        if ($isLast) {
            $complete = $endsWithNewline
            if ($complete -and ($rec -match 'EmergencyShutdown') -and
                -not ($rec -match "(?s)EmergencyShutdown.*'\s*!!!")) {
                $complete = $false
            }
            if (-not $complete -and -not $released) {
                $consumed = $from
                break
            }
        }
        [void]$records.Add($rec)
    }
    return @{ Records = @($records.ToArray()); ConsumedChars = $consumed }
}

function ConvertFrom-LogRecord {
    <#
    Returns $null, or @{ Type = 'stop'|'restart'; At; Source; Message }.
    'stop' only for EmergencyShutdown lines (the "Critical error." line that
    precedes one describes the same event and is ignored). 'restart' for any
    S: line; the caller decides whether a shutdown preceded it.
    Never throws on odd bytes.
    #>
    param([string]$Record)
    if ([string]::IsNullOrEmpty($Record)) { return $null }
    $clean = Clean-LogRecord $Record
    # Any other control character (a stray DC3 mid-line) is dropped.
    $clean = [regex]::Replace($clean, '[\x00-\x09\x0B-\x1F]', '')
    $head = [regex]::Match($clean,
        '(?s)^([ES]):(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:,\d+)?\s*(.*)$')
    if (-not $head.Success) { return $null }
    $at = [datetime]::MinValue
    $ok = [datetime]::TryParseExact($head.Groups[2].Value, 'yyyy/MM/dd HH:mm:ss',
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::None, [ref]$at)
    if (-not $ok) { return $null }
    $rest = $head.Groups[3].Value

    if ($head.Groups[1].Value -eq 'S') {
        $msg = ([regex]::Replace($rest, '\s+', ' ')).Trim()
        return @{ Type = 'restart'; At = $at; Source = 'HyStar'; Message = $msg }
    }

    $sd = [regex]::Match($rest, "(?s)^!!!\s*EmergencyShutdown\s+Source=(\S+)\s+Message='(.*)$")
    if (-not $sd.Success) { return $null }
    $source = $sd.Groups[1].Value
    $body = $sd.Groups[2].Value
    $cut = [regex]::Match($body, "(?s)^(.*?)'\s*!!!")
    if ($cut.Success) { $body = $cut.Groups[1].Value }

    $stripped = $false
    foreach ($wrap in @(
            "^Critical error from ICF System! Error #\d+ -> '",
            "^Critical Error in ICF System - '")) {
        if (-not $stripped) {
            $m = [regex]::Match($body, $wrap)
            if ($m.Success) {
                $body = $body.Substring($m.Length)
                $stripped = $true
            }
        }
    }
    if ($stripped -and $body.EndsWith("'")) { $body = $body.Substring(0, $body.Length - 1) }
    $body = ([regex]::Replace($body, '\s+', ' ')).Trim()
    return @{ Type = 'stop'; At = $at; Source = $source; Message = $body }
}

function Get-StopCategory {
    <#
    Maps a shutdown message to @{ Category; Title; Check }. Patterns are tried
    in order. Check is the line shown under the message in the alert; for
    evotip it carries a literal {vial} placeholder the poster fills in.
    #>
    param([string]$Message)
    $generic = 'The queue stays stopped until someone restarts it.'
    $category = 'other'
    $title = 'Stopped'
    $check = $generic
    if ($Message -match 'No Evotip|Tip expected but not present') {
        $category = 'evotip'; $title = 'No Evotip'
        $check = '*Check:* the Evotip in {vial}, and that the tray matches the sample list. The queue stays stopped until someone restarts it.'
    }
    elseif ($Message -match 'did not meet criteria') {
        # "High pressure pump(s) did not meet criteria": a pump self-test, not
        # a pressure limit. Ahead of the pressure pattern, which it would
        # otherwise match on "High pressure" (8 real stops were mistitled).
        $category = 'evosep_hw'; $title = 'Evosep hardware fault'
    }
    elseif ($Message -match 'pressure limit exceeded|High pressure') {
        $category = 'overpressure'; $title = 'Pressure limit'
    }
    elseif ($Message -match 'OTOF MS.*not connected') {
        $category = 'connection_lost'; $title = 'MS not connected'
    }
    elseif ($Message -match 'Bruker OTOF MS') {
        $category = 'ms_error'; $title = 'MS software error'
    }
    elseif ($Message -match 'UCEERR_|RENDERTHREADFAILURE') {
        $category = 'hystar_crash'; $title = 'HyStar crashed'
    }
    elseif ($Message -match 'fan detection|power-rail|mcu reset|Unable to access autosampler|MoveToHome|did not meet criteria|Average pump flow') {
        $category = 'evosep_hw'; $title = 'Evosep hardware fault'
    }
    return @{ Category = $category; Title = $title; Check = $check }
}

function Get-DedupKey {
    # Lower-cased, digit runs -> N, whitespace collapsed: repeats of one fault
    # that differ only in counters or timings share a key.
    param([string]$Message)
    $k = $Message.ToLowerInvariant()
    $k = [regex]::Replace($k, '\d+', 'N')
    $k = [regex]::Replace($k, '\s+', ' ')
    return $k.Trim()
}

# ------------------------------------------------------- deciding / identifying
# Alert memory is a hashtable: @{ keys = @{ <dedup key> = @{ FirstAt; LastAt;
# Count; ReminderSent } }; lastShutdownAt = <datetime or $null> }. The caller
# saves it as JSON; PS 5.1 loads that back as PSCustomObjects and string dates,
# so ConvertTo-WatchState turns either shape into the hashtable form.

function ConvertTo-WatchDate {
    # datetime, ISO string, or ConvertTo-Json's /Date(ms)/ form -> datetime/$null.
    param($Value)
    if ($Value -eq $null) { return $null }
    if ($Value -is [datetime]) { return $Value }
    $t = "$Value".Trim()
    if ($t -eq "") { return $null }
    $m = [regex]::Match($t, '^/Date\((-?\d+)')
    if ($m.Success) {
        $epoch = New-Object DateTime(1970, 1, 1, 0, 0, 0, [System.DateTimeKind]::Utc)
        return $epoch.AddMilliseconds([double]$m.Groups[1].Value).ToLocalTime()
    }
    $out = [datetime]::MinValue
    $ok = [datetime]::TryParse($t, [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::None, [ref]$out)
    if ($ok) { return $out }
    return $null
}

function Get-StateField {
    param($State, [string]$Name)
    if ($State -eq $null) { return $null }
    if ($State -is [System.Collections.IDictionary]) {
        if ($State.Contains($Name)) { return $State[$Name] }
        return $null
    }
    $p = $State.PSObject.Properties[$Name]
    if ($p -ne $null) { return $p.Value }
    return $null
}

function ConvertTo-WatchState {
    <#
    Accepts $null, a hashtable, or a PSCustomObject from ConvertFrom-Json and
    returns the hashtable form. Other top-level fields are carried over.
    #>
    param($State)
    $out = @{ keys = @{}; lastShutdownAt = $null }
    if ($State -eq $null) { return $out }
    if ($State -is [System.Collections.IDictionary]) {
        foreach ($n in @($State.Keys)) { $out[[string]$n] = $State[$n] }
    }
    else {
        foreach ($p in $State.PSObject.Properties) { $out[$p.Name] = $p.Value }
    }
    $out['lastShutdownAt'] = ConvertTo-WatchDate $out['lastShutdownAt']
    $fixed = @{}
    $raw = $out['keys']
    $names = @()
    if ($raw -is [System.Collections.IDictionary]) { $names = @($raw.Keys) }
    elseif ($raw -ne $null) {
        foreach ($p in $raw.PSObject.Properties) { $names += $p.Name }
    }
    foreach ($n in $names) {
        $e = Get-StateField $raw ([string]$n)
        if ($e -eq $null) { continue }
        $rs = $false
        if ((Get-StateField $e 'ReminderSent') -eq $true) { $rs = $true }
        $cnt = 1
        $c = Get-StateField $e 'Count'
        if ($c -ne $null) { $cnt = [int]$c }
        $fixed[[string]$n] = @{
            FirstAt      = ConvertTo-WatchDate (Get-StateField $e 'FirstAt')
            LastAt       = ConvertTo-WatchDate (Get-StateField $e 'LastAt')
            Count        = $cnt
            ReminderSent = $rs
        }
    }
    $out['keys'] = $fixed
    return $out
}

function Get-AlertDecision {
    <#
    'new' | 'fold' | 'reminder'. Folds when Key last occurred under 5 min
    before At; one 'reminder' once a fold finds the burst 4 h or more old.
    Mutates State['keys'][Key] (State must be the hashtable form; run a loaded
    state through ConvertTo-WatchState first).
    #>
    param(
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][datetime]$At
    )
    if (-not $State.ContainsKey('keys') -or $State['keys'] -eq $null) { $State['keys'] = @{} }
    $entry = $null
    if ($State['keys'].ContainsKey($Key)) { $entry = $State['keys'][$Key] }
    if ($entry -ne $null -and $entry.LastAt -ne $null -and $entry.FirstAt -ne $null) {
        $gap = ($At - $entry.LastAt).TotalMinutes
        if ($gap -lt 5) {
            $entry.Count = [int]$entry.Count + 1
            $entry.LastAt = $At
            if ((($At - $entry.FirstAt).TotalHours -ge 4) -and (-not $entry.ReminderSent)) {
                $entry.ReminderSent = $true
                return 'reminder'
            }
            return 'fold'
        }
    }
    $State['keys'][$Key] = @{ FirstAt = $At; LastAt = $At; Count = 1; ReminderSent = $false }
    return 'new'
}

function Test-UnexpectedRestart {
    # True when no shutdown was logged in the 5 min before At.
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][datetime]$At
    )
    $last = ConvertTo-WatchDate (Get-StateField $State 'lastShutdownAt')
    if ($last -eq $null) { return $true }
    if ((($At - $last).TotalMinutes) -gt 5) { return $true }
    return $false
}

function Get-ProcedureDayPatterns {
    <#
    The folder-name patterns for Evosep procedures (<method>_yyyy-MM-dd_HH-mm-ss)
    that can have started between From and To: one per day, so a serial
    folder of 31,000 procedures costs a name filter, not a walk. More than
    2 days apart: '*', every name. Shared by Get-EvosepVial and
    Test-InjectionSince. Unrolled on return: callers wrap it in @().
    #>
    param(
        [Parameter(Mandatory = $true)][datetime]$From,
        [Parameter(Mandatory = $true)][datetime]$To
    )
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $patterns = New-Object System.Collections.ArrayList
    if (($To.Date - $From.Date).TotalDays -gt 2) { [void]$patterns.Add('*') }
    else {
        $day = $From.Date
        while ($day -le $To.Date) {
            [void]$patterns.Add("*_$($day.ToString('yyyy-MM-dd', $inv))_*")
            $day = $day.AddDays(1)
        }
    }
    return $patterns.ToArray()
}

function Get-EvosepVial {
    <#
    The vial of the Evosep procedure a stop interrupted, e.g. 'S1-A5', or
    $null. Ruling R20: a vial only where it can be right. The newest
    <method>_yyyy-MM-dd_HH-mm-ss folder (ProcedureRoot holds the serial
    folders, ...\Procedure logs: ruling R6) that started at or before At,
    and no earlier than Category allows:
      evotip                              10 min before At. The tip is
                                          picked up at the start, so an
                                          older folder is the previous,
                                          finished sample.
      overpressure, connection_lost,
      ms_error                            90 min (a run is under way)
      anything else                       never: a fan fault or a HyStar
                                          crash belongs to no one sample.
    Never at an ambiguous local time in TimeZone (DST fall-back repeats
    01:00-02:00; by name, a folder from the first pass outranks the right
    one from the second). Names are listed per day (Get-ProcedureDayPatterns)
    and the start comes from the NAME (no per-folder stat). Only the chosen
    folder's journal.txt is read. Never throws.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ProcedureRoot,
        [Parameter(Mandatory = $true)][datetime]$At,
        [string]$Category = "",
        [System.TimeZoneInfo]$TimeZone = [System.TimeZoneInfo]::Local
    )
    try {
        $minutes = 0
        if ($Category -eq 'evotip') { $minutes = 10 }
        elseif ($Category -eq 'overpressure' -or $Category -eq 'connection_lost' -or $Category -eq 'ms_error') { $minutes = 90 }
        if ($minutes -le 0) { return $null }
        if ($null -ne $TimeZone -and $TimeZone.IsAmbiguousTime($At)) { return $null }
        if (-not [System.IO.Directory]::Exists($ProcedureRoot)) { return $null }
        $inv = [System.Globalization.CultureInfo]::InvariantCulture
        $best = $null
        $bestStart = [datetime]::MinValue
        $earliest = $At.AddMinutes(-$minutes)
        $patterns = @(Get-ProcedureDayPatterns -From $earliest -To $At)
        foreach ($serial in [System.IO.Directory]::GetDirectories($ProcedureRoot)) {
            foreach ($pattern in $patterns) {
                foreach ($dir in [System.IO.Directory]::GetDirectories($serial, $pattern)) {
                    $name = [System.IO.Path]::GetFileName($dir)
                    $m = [regex]::Match($name, '_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$')
                    if (-not $m.Success) { continue }
                    $start = [datetime]::MinValue
                    $ok = [datetime]::TryParseExact($m.Groups[1].Value, 'yyyy-MM-dd_HH-mm-ss', $inv,
                        [System.Globalization.DateTimeStyles]::None, [ref]$start)
                    if (-not $ok) { continue }
                    if ($start -gt $At) { continue }
                    if ($start -lt $earliest) { continue }
                    if ($best -eq $null -or $start -gt $bestStart) { $best = $dir; $bestStart = $start }
                }
            }
        }
        if ($best -eq $null) { return $null }
        $journal = Join-Path $best 'journal.txt'
        if (-not [System.IO.File]::Exists($journal)) { return $null }
        $fs = New-Object System.IO.FileStream($journal,
            [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
            ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        try {
            $sr = New-Object System.IO.StreamReader($fs)
            $text = $sr.ReadToEnd()
        }
        finally { $fs.Dispose() }
        $v = [regex]::Match($text, 'Procedure\.Vialposition:.*\((S\d+-[A-H]\d+)\)')
        if ($v.Success) { return $v.Groups[1].Value }
        return $null
    }
    catch { return $null }
}

function Get-HyStarSample {
    <#
    Best effort, never throws. Tail-reads (last 256 KB, shared read) the
    CompassHyStarLogbook.log and looks for the newest timestamped line in the
    10 min before At that names a sample folder (...\name_S1-A5_1_1234.d or a
    plain name.d). The line format is UNCONFIRMED, so anything that does not
    match this strict shape gives $null (an alert with no sample is fine; a
    wrong sample is not). None at an ambiguous local time in TimeZone (DST
    fall-back, ruling R20): a line from either pass of the repeated hour
    could be the one.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$LogbookPath,
        [Parameter(Mandatory = $true)][datetime]$At,
        [System.TimeZoneInfo]$TimeZone = [System.TimeZoneInfo]::Local
    )
    try {
        if ($null -ne $TimeZone -and $TimeZone.IsAmbiguousTime($At)) { return $null }
        if (-not [System.IO.File]::Exists($LogbookPath)) { return $null }
        $fs = New-Object System.IO.FileStream($LogbookPath,
            [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
            ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        try {
            $len = $fs.Length
            $take = [long]262144
            if ($take -gt $len) { $take = $len }
            $start = $len - $take
            if (($start % 2) -ne 0) { $start = $start - 1 }
            $count = $len - $start
            if (($count % 2) -ne 0) { $count = $count - 1 }
            if ($count -le 0) { return $null }
            $buf = New-Object byte[] ([int]$count)
            [void]$fs.Seek($start, [System.IO.SeekOrigin]::Begin)
            $got = 0
            while ($got -lt $count) {
                $n = $fs.Read($buf, $got, ([int]$count - $got))
                if ($n -le 0) { break }
                $got += $n
            }
            if (($got % 2) -ne 0) { $got = $got - 1 }
            $text = [System.Text.Encoding]::Unicode.GetString($buf, 0, $got)
        }
        finally { $fs.Dispose() }
        $found = $null
        $foundAt = [datetime]::MinValue
        foreach ($line in ($text -split "`n")) {
            $m = [regex]::Match($line,
                '(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}).*[\\/]([^\\/\s''"]+)\.d(?![\w.])')
            if (-not $m.Success) { continue }
            $t = [datetime]::MinValue
            $ok = [datetime]::TryParseExact($m.Groups[1].Value, 'yyyy/MM/dd HH:mm:ss',
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::None, [ref]$t)
            if (-not $ok) { continue }
            if ($t -gt $At -or $t -lt $At.AddMinutes(-10)) { continue }
            if ($t -ge $foundAt) { $found = $m.Groups[2].Value; $foundAt = $t }
        }
        if ($found -eq $null) { return $null }
        # HyStar appends _<vial>_<n>_<id> to the sample name; drop it.
        $found = [regex]::Replace($found, '_S\d+-[A-H]\d+_\d+_\d+$', '')
        if ($found -eq "") { return $null }
        return $found
    }
    catch { return $null }
}

# ------------------------------------------------------------------ formatting
# U+00B7 and U+2014 are built at runtime: this file is pure ASCII so that
# PS 5.1 (which reads a BOM-less .ps1 as ANSI) cannot turn them into mojibake.

function Get-IdentityLine {
    # Same as Python identity_line: "{vial} . {sample} . {HH:mm}", gaps omitted.
    param($Vial, $Sample, $At)
    $sep = " $([char]0x00B7) "
    $parts = @()
    if (-not [string]::IsNullOrEmpty($Vial)) { $parts += [string]$Vial }
    if (-not [string]::IsNullOrEmpty($Sample)) { $parts += [string]$Sample }
    if ($At -ne $null) { $parts += ([datetime]$At).ToString('HH:mm') }
    return ($parts -join $sep)
}

function Get-MentionPrefix {
    param([string]$MemberId)
    if ([string]::IsNullOrEmpty($MemberId)) { return "" }
    return "<@$MemberId> "
}

function New-SectionBlock {
    param([string]$Text)
    return @{ type = 'section'; text = @{ type = 'mrkdwn'; text = $Text } }
}

function Format-StopPayload {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Event,
        [string]$MemberId = "",
        [Alias('Host')][string]$HostName = 'TIMS-10878'
    )
    $dash = [char]0x2014
    $dot = [char]0x00B7
    $title = [string]$Event.Title
    $at = [datetime]$Event.At
    $identity = Get-IdentityLine $Event.Vial $Event.Sample $at
    $mention = Get-MentionPrefix $MemberId
    $text = "$mention`:red_circle: $HostName queue STOPPED $dash $title $dot $identity"
    $head = "$mention`:red_circle: *$HostName queue STOPPED $dash $title*`n*$identity*"

    $generic = 'The queue stays stopped until someone restarts it.'
    $check = $generic
    if ($Event.Category -eq 'evotip') {
        if ([string]::IsNullOrEmpty($Event.Vial)) {
            $check = '*Check:* the Evotip position, and that the tray matches the sample list. The queue stays stopped until someone restarts it.'
        }
        else {
            $check = "*Check:* the Evotip in $($Event.Vial), and that the tray matches the sample list. The queue stays stopped until someone restarts it."
        }
    }
    $atText = $at.ToString('yyyy-MM-dd HH:mm:ss')
    $body = "> $($Event.Message)`n$check`n_HyStar stopped the queue at $atText $dot posted by the $HostName watcher_"
    $ctx = "<https://ucd.stan-proteomics.org|STAN dashboard> $dot Maintenance tab"
    $blocks = @(
        (New-SectionBlock $head),
        (New-SectionBlock $body),
        @{ type = 'context'; elements = @(, @{ type = 'mrkdwn'; text = $ctx }) }
    )
    return @{ text = $text; blocks = $blocks }
}

function Format-ReminderPayload {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Event,
        [string]$MemberId = "",
        [Alias('Host')][string]$HostName = 'TIMS-10878'
    )
    $dash = [char]0x2014
    $mention = Get-MentionPrefix $MemberId
    $since = ([datetime]$Event.FirstAt).ToString('HH:mm')
    $text = "$mention`:red_circle: $HostName still stopping $dash $($Event.Title): $($Event.Count) times since $since"
    return @{ text = $text; blocks = @(, (New-SectionBlock $text)) }
}

function Format-RestartPayload {
    # Ruling R18: told, never paged (no @mention). An S: line only appears
    # when someone is already restarting HyStar, so a planned restart is one
    # harmless message, and a reboot at night wakes Brett once (the
    # watcher-down page), not twice.
    param(
        [Parameter(Mandatory = $true)][hashtable]$Event,
        [Alias('Host')][string]$HostName = 'TIMS-10878'
    )
    $dash = [char]0x2014
    $dot = [char]0x00B7
    $hm = ([datetime]$Event.At).ToString('HH:mm')
    $text = ":red_circle: $HostName HyStar restarted $dash no shutdown logged $dot $hm"
    return @{ text = $text; blocks = @(, (New-SectionBlock $text)) }
}

function Format-StaleSummaryPayload {
    <#
    Ruling R19: the one message a tick sends for the stops it read more than
    2 h after they happened (Test-StaleRecord). No @mention: nobody is woken
    by history. "latest" is HH:mm, with the date when it was not today.
    #>
    param(
        [Parameter(Mandatory = $true)][int]$Count,
        [Parameter(Mandatory = $true)][datetime]$Latest,
        [Parameter(Mandatory = $true)][datetime]$Now,
        [Alias('Host')][string]$HostName = 'TIMS-10878'
    )
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $dash = [char]0x2014
    $what = "$Count older stops"
    if ($Count -eq 1) { $what = '1 older stop' }
    $when = $Latest.ToString('HH:mm', $inv)
    if ($Latest.Date -ne $Now.Date) { $when = $Latest.ToString('yyyy-MM-dd HH:mm', $inv) }
    $text = ":large_orange_diamond: $HostName $dash $what while the watcher wasn't running (latest $when)"
    return @{ text = $text; blocks = @(, (New-SectionBlock $text)) }
}

function Format-HiveQuietPayload {
    param(
        [datetime]$At = (Get-Date),
        [switch]$Back
    )
    if ($Back) {
        $text = ":white_check_mark: STAN's Hive alerting is back"
    }
    else {
        $text = ":large_orange_diamond: STAN's Hive alerting has gone quiet $([char]0x2014) last check $($At.ToString('HH:mm'))"
    }
    return @{ text = $text; blocks = @(, (New-SectionBlock $text)) }
}

# ======================================================================= tick
# One tick, once a minute: read what is new in the logbook, decide, deliver,
# report. StateDir (C:\ProgramData\STAN\hystar_watch) holds state.json, the
# queue of event files the share could not take (pending\) and watch.log.
# ShareDir (Y:\STAN\hystar_events\<COMPUTERNAME>) gets the event files and
# heartbeat.json, which Hive reads (stan/reports/hystar_events.py).
#
# Nothing written here may carry the Slack webhook: every line bound for a
# log, an event file or the heartbeat goes through Get-ScrubbedText.

function Get-ScrubbedText {
    # Replaces anything shaped like a Slack webhook (and the exact Secret, when
    # given) with <webhook>. Same pattern as stan.notify._scrub.
    param(
        [AllowNull()][AllowEmptyString()][string]$Text,
        [string]$Secret = ""
    )
    if ([string]::IsNullOrEmpty($Text)) { return "" }
    $out = $Text
    if (-not [string]::IsNullOrEmpty($Secret)) { $out = $out.Replace($Secret, '<webhook>') }
    return [regex]::Replace($out, 'https://hooks\.slack\.com/\S+', '<webhook>')
}

function ConvertTo-IsoLocal {
    # Instrument-local naive ISO (2026-10-24T02:04:24), as every STAN document
    # writes times. $null stays $null.
    param($Value)
    $d = ConvertTo-WatchDate $Value
    if ($null -eq $d) { return $null }
    return $d.ToString("yyyy-MM-dd'T'HH:mm:ss", [System.Globalization.CultureInfo]::InvariantCulture)
}

function Write-Utf8File {
    # UTF-8 WITHOUT a BOM (ruling R14). PS 5.1's Set-Content -Encoding UTF8
    # writes a BOM, and Hive's json.loads refuses one.
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding $false))
}

function Publish-JsonFile {
    <#
    Writes Text to Dir\<BaseName>.json by way of Dir\<BaseName>.tmp and a
    rename, so a reader that globs *.json (Hive) never sees half a file. The
    temp name must not end in .json for the same reason.
    -NoOverwrite: an existing target is left as it is and 'exists' comes back,
    because an event file, once on the share, may already have been relayed
    and is never rewritten. Returns 'written' or 'exists'; throws when the
    folder cannot be written.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Dir,
        [Parameter(Mandatory = $true)][string]$BaseName,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
        [switch]$NoOverwrite
    )
    $target = Join-Path $Dir "$BaseName.json"
    $tmp = Join-Path $Dir "$BaseName.tmp"
    if ($NoOverwrite -and [System.IO.File]::Exists($target)) { return 'exists' }
    Write-Utf8File -Path $tmp -Text $Text
    try {
        if ([System.IO.File]::Exists($target)) {
            if ($NoOverwrite) {
                [System.IO.File]::Delete($tmp)
                return 'exists'
            }
            try {
                # [NullString]: a plain $null reaches .NET as "" and Replace refuses it.
                [System.IO.File]::Replace($tmp, $target, [NullString]::Value)
            }
            catch {
                # Some SMB servers do not support ReplaceFile.
                [System.IO.File]::Delete($target)
                [System.IO.File]::Move($tmp, $target)
            }
        }
        else {
            [System.IO.File]::Move($tmp, $target)
        }
    }
    catch {
        try { [System.IO.File]::Delete($tmp) } catch { }
        throw
    }
    return 'written'
}

function Get-NextOffset {
    <#
    The logbook offset after one read (ruling R10): the byte the text started
    at, plus the bytes the splitter consumed. Read-NewLogText returns no start
    offset, and at offset 0 its text starts at byte 2 (after the BOM), so the
    start is NewOffset less the text's bytes (UTF-16: 2 per char; a bad code
    unit decodes to one U+FFFD, so the count holds).
    #>
    param(
        [Parameter(Mandatory = $true)]$Read,
        [Parameter(Mandatory = $true)][long]$ConsumedChars
    )
    $start = [long]$Read.NewOffset - (2 * [long]$Read.Text.Length)
    return ($start + (2 * $ConsumedChars))
}

function Get-LogFingerprint {
    <#
    SHA-256 of the first Bytes bytes of a file (fewer when it is shorter):
    @{ Hash; Length }, or $null when it cannot be read. HyStar's logbook opens
    with a header and a timestamped S: line, so its first 200 bytes tell one
    logbook file from the next. The share mode also lets HyStar rename the
    file (rotation) while it is open here.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Bytes = 200
    )
    try {
        $fs = New-Object System.IO.FileStream($Path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        try {
            $n = [int][Math]::Min([long]$Bytes, $fs.Length)
            $buf = New-Object byte[] $n
            $got = 0
            while ($got -lt $n) {
                $r = $fs.Read($buf, $got, $n - $got)
                if ($r -le 0) { break }
                $got += $r
            }
        }
        finally { $fs.Dispose() }
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { $h = $sha.ComputeHash($buf, 0, $got) }
        finally { $sha.Dispose() }
        return @{ Hash = ([System.BitConverter]::ToString($h)).Replace('-', ''); Length = $got }
    }
    catch { return $null }
}

function Find-RotatedLogbook {
    <#
    The file HyStar rotated the logbook to: among <name>_*.log beside it,
    newest first, the first whose opening bytes match the saved fingerprint.
    With no fingerprint saved, the newest. $null when there is none, so a
    stranger's old file is never replayed. Lists one folder, on rotation only.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$LogbookPath,
        $Hash = $null,
        $HashLength = $null
    )
    $dir = [System.IO.Path]::GetDirectoryName($LogbookPath)
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($LogbookPath)
    $ext = [System.IO.Path]::GetExtension($LogbookPath)
    $found = [System.IO.Directory]::GetFiles($dir, "$($stem)_*$ext")
    $n = $found.Length
    if ($n -eq 0) { return $null }
    $times = New-Object 'System.DateTime[]' $n
    $paths = New-Object 'System.String[]' $n
    for ($i = 0; $i -lt $n; $i++) {
        $paths[$i] = $found[$i]
        $times[$i] = [System.IO.File]::GetLastWriteTimeUtc($found[$i])
    }
    [Array]::Sort($times, $paths)
    $checked = 0
    for ($i = $n - 1; $i -ge 0; $i--) {
        if ([string]::IsNullOrEmpty([string]$Hash)) { return $paths[$i] }
        $fp = Get-LogFingerprint -Path $paths[$i] -Bytes ([int]$HashLength)
        if ($null -ne $fp -and $fp.Hash -eq [string]$Hash) { return $paths[$i] }
        $checked++
        if ($checked -ge 10) { break }
    }
    return $null
}

function Read-LogbookSegment {
    <#
    One read of one logbook file from Offset: @{ Records = <parsed records>;
    NewOffset; Length; Held; Consumed }. Held: a last record was left for the
    next tick. -Final releases it anyway (a rotated file will not grow).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [long]$Offset = 0,
        $PendingSince = $null,
        [Parameter(Mandatory = $true)][datetime]$Now,
        [switch]$Final
    )
    $r = Read-NewLogText -Path $Path -Offset $Offset
    $since = $PendingSince
    if ($Final) { $since = $Now.AddMinutes(-10) }
    $s = Split-LogRecords -Text $r.Text -PendingSince $since -Now $Now
    $parsed = New-Object System.Collections.ArrayList
    foreach ($rec in $s.Records) {
        $p = ConvertFrom-LogRecord -Record $rec
        if ($null -ne $p) { [void]$parsed.Add($p) }
    }
    return @{
        Records   = $parsed.ToArray()
        NewOffset = (Get-NextOffset -Read $r -ConsumedChars $s.ConsumedChars)
        Length    = $r.Length
        Held      = ($s.ConsumedChars -lt $r.Text.Length)
        Consumed  = $s.ConsumedChars
    }
}

function Get-LogbookRecords {
    <#
    Everything new in the logbook since the saved state, as parsed records in
    order: @{ Records; Seeded; Rotated }. Updates the State's offset, length,
    fingerprint and pendingSince only once every read has succeeded, so a
    failed read is simply retried next tick.

    First run (no offset saved, or the saved one is for another logbook):
    start at the current end and return nothing; history is never replayed.

    Rotation (the file is shorter than the saved offset, or its first bytes
    changed): finish the rotated file from the saved offset, then read the
    new file from 0. In that tick, records at or before lastRecordAt are
    dropped from the new file: the backstop against reading one file twice.
    #>
    param(
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][string]$LogbookPath,
        [Parameter(Mandatory = $true)][datetime]$Now,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Log
    )
    $out = New-Object System.Collections.ArrayList
    $len = (New-Object System.IO.FileInfo $LogbookPath).Length
    $savedOffset = $State['offset']
    if ($null -eq $savedOffset -or [string]$State['logbook'] -ne $LogbookPath) {
        $fp = Get-LogFingerprint -Path $LogbookPath
        $State['logbook'] = $LogbookPath
        $State['offset'] = [long]$len
        $State['length'] = [long]$len
        $State['pendingSince'] = $null
        $State['fingerprint'] = $null
        $State['fingerprintLength'] = $null
        if ($null -ne $fp) {
            $State['fingerprint'] = $fp.Hash
            $State['fingerprintLength'] = $fp.Length
        }
        [void]$Log.Add("first run: starting at the end of $LogbookPath (byte $len); history is not replayed")
        return @{ Records = @(); Seeded = $true; Rotated = $false }
    }
    $offset = [long]$savedOffset
    $savedHash = [string]$State['fingerprint']
    $savedHashLen = 0
    if ($null -ne $State['fingerprintLength']) { $savedHashLen = [int]$State['fingerprintLength'] }

    $rotated = $false
    if ($len -lt $offset) { $rotated = $true }
    elseif ($savedHash -ne "" -and $savedHashLen -gt 0) {
        $now_fp = Get-LogFingerprint -Path $LogbookPath -Bytes $savedHashLen
        if ($null -ne $now_fp -and ($now_fp.Length -ne $savedHashLen -or $now_fp.Hash -ne $savedHash)) {
            $rotated = $true
        }
    }

    $pendingSince = ConvertTo-WatchDate $State['pendingSince']
    # Clock went back (DST fall-back): restart the 2-min wait, not an hour's.
    if ($null -ne $pendingSince -and $pendingSince -gt $Now) { $pendingSince = $Now }
    if ($rotated) {
        [void]$Log.Add("logbook rotated (now $len bytes, saved offset $offset)")
        $old = Find-RotatedLogbook -LogbookPath $LogbookPath -Hash $savedHash -HashLength $savedHashLen
        if ($null -ne $old) {
            $seg = Read-LogbookSegment -Path $old -Offset $offset -Now $Now -Final
            foreach ($p in $seg.Records) { [void]$out.Add($p) }
            [void]$Log.Add("finished $([System.IO.Path]::GetFileName($old)) from byte $($offset): $($seg.Records.Count) record(s)")
        }
        else {
            [void]$Log.Add("rotated logbook not found next to $LogbookPath; anything written just before the rotation is missed")
        }
        $offset = 0
        $pendingSince = $null
    }

    $seg = Read-LogbookSegment -Path $LogbookPath -Offset $offset -PendingSince $pendingSince -Now $Now
    $backstop = ConvertTo-WatchDate $State['lastRecordAt']
    foreach ($p in $seg.Records) {
        if ($rotated -and $null -ne $backstop -and $p.At -le $backstop) {
            [void]$Log.Add("skipped a $($p.Type) at $(ConvertTo-IsoLocal $p.At): not after the last record seen")
            continue
        }
        [void]$out.Add($p)
    }

    # A held tail starts its 2-min clock when it first appears; progress
    # (consumed > 0) means the held record is a new one.
    if ($seg.Held) {
        if ($null -eq $pendingSince -or $seg.Consumed -gt 0) { $pendingSince = $Now }
    }
    else { $pendingSince = $null }

    $State['offset'] = [long]$seg.NewOffset
    $State['length'] = [long]$seg.Length
    $State['pendingSince'] = $pendingSince
    if ($rotated -or $savedHashLen -lt 200) {
        $fp = Get-LogFingerprint -Path $LogbookPath
        if ($null -ne $fp) {
            $State['fingerprint'] = $fp.Hash
            $State['fingerprintLength'] = $fp.Length
        }
    }
    return @{ Records = $out.ToArray(); Seeded = $false; Rotated = $rotated }
}

function Test-InjectionSince {
    <#
    True when an Evosep procedure (an injection attempt) started after Since
    and at or before Until. Ruling R13: a stop that repeats inside the 5-min
    fold window after such a start is a new stop (someone restarted the queue
    in between), not one more of a storm. Folder names only
    (<method>_yyyy-MM-dd_HH-mm-ss), listed per day by pattern, so a serial
    folder of 31,000 procedures costs a name filter, not a walk, and nothing
    is opened or stat'ed. Never throws.
    #>
    param(
        [string]$ProcedureRoot = "",
        $Since = $null,
        $Until = $null
    )
    try {
        if ([string]::IsNullOrEmpty($ProcedureRoot)) { return $false }
        $from = ConvertTo-WatchDate $Since
        $to = ConvertTo-WatchDate $Until
        if ($null -eq $from -or $null -eq $to) { return $false }
        if ($to -le $from) { return $false }
        if (-not [System.IO.Directory]::Exists($ProcedureRoot)) { return $false }
        $inv = [System.Globalization.CultureInfo]::InvariantCulture
        $patterns = @(Get-ProcedureDayPatterns -From $from -To $to)
        foreach ($serial in [System.IO.Directory]::GetDirectories($ProcedureRoot)) {
            foreach ($pattern in $patterns) {
                foreach ($dir in [System.IO.Directory]::GetDirectories($serial, $pattern)) {
                    $name = [System.IO.Path]::GetFileName($dir)
                    $m = [regex]::Match($name, '_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$')
                    if (-not $m.Success) { continue }
                    $start = [datetime]::MinValue
                    $ok = [datetime]::TryParseExact($m.Groups[1].Value, 'yyyy-MM-dd_HH-mm-ss', $inv,
                        [System.Globalization.DateTimeStyles]::None, [ref]$start)
                    if (-not $ok) { continue }
                    if ($start -gt $from -and $start -le $to) { return $true }
                }
            }
        }
        return $false
    }
    catch { return $false }
}

function New-WatchEvent {
    <#
    One event in the file contract Hive reads (schema 1). The id is
    <host>_<at yyyyMMddTHHmmss>_<n>, n a counter kept in State, so no two
    events share an id even within one second (Hive keys its memory on it).
    posted_by_pc / posted_at are filled in by Send-WatchEvent.
    #>
    param(
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][string]$Type,
        [Parameter(Mandatory = $true)][string]$HostName,
        [Parameter(Mandatory = $true)][datetime]$At,
        $Source = $null,
        $Category = $null,
        $Title = $null,
        $Message = $null,
        $Vial = $null,
        $Sample = $null,
        $DedupKey = $null,
        [int]$Count = 1,
        $FirstAt = $null,
        [Parameter(Mandatory = $true)]$Payload,
        [string]$Version = ""
    )
    $seq = 1
    if ($null -ne $State['eventSeq']) { $seq = [int]$State['eventSeq'] + 1 }
    $State['eventSeq'] = $seq
    $stamp = $At.ToString("yyyyMMdd'T'HHmmss", [System.Globalization.CultureInfo]::InvariantCulture)
    $station = [regex]::Replace($HostName.ToLowerInvariant(), '[^a-z0-9]', '')
    $first = ConvertTo-IsoLocal $FirstAt
    if ($null -eq $first) { $first = ConvertTo-IsoLocal $At }
    return [ordered]@{
        schema          = 1
        id              = "$($HostName)_$($stamp)_$seq"
        type            = $Type
        host            = $HostName
        station         = $station
        at              = (ConvertTo-IsoLocal $At)
        source          = $Source
        category        = $Category
        title           = $Title
        message         = $Message
        vial            = $Vial
        sample          = $Sample
        dedup_key       = $DedupKey
        count           = $Count
        first_at        = $first
        payload         = $Payload
        posted_by_pc    = $false
        posted_at       = $null
        watcher_version = $Version
    }
}

function Invoke-Sender {
    # Runs the sender on a payload; whatever it does (returns junk, throws),
    # the answer is @{ Ok = <bool>; Status = <scrubbed string> }.
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Sender,
        [Parameter(Mandatory = $true)]$Payload
    )
    try {
        $out = & $Sender $Payload
        $res = $null
        foreach ($o in @($out)) {
            if ($o -is [System.Collections.IDictionary] -and $o.Contains('Ok')) { $res = $o }
        }
        if ($null -eq $res) { return @{ Ok = $false; Status = 'the sender returned no result' } }
        return @{ Ok = ($res['Ok'] -eq $true); Status = (Get-ScrubbedText ([string]$res['Status'])) }
    }
    catch {
        $why = Get-ScrubbedText "$($_.Exception.GetType().Name): $($_.Exception.Message)"
        return @{ Ok = $false; Status = $why }
    }
}

function Read-DeliveryLedger {
    <#
    StateDir\delivered.txt: one line per step, "<key> TAB posted TAB <at>"
    right after a successful post, "<key> TAB done" once the event file is
    queued. Returns a hashtable key -> @{ Posted; PostedAt; Done }.

    Why: state.json is saved once, at the end of a tick. A tick that ends
    after a post but before that save (power loss, the task's time limit, or
    a state.json that cannot be replaced, which repeats every tick) re-reads
    the same records next tick and decides 'new' again. The event ids come
    out the same (same old state), so this ledger, written the moment each
    post succeeds, is what keeps a stop from paging Brett twice. Local only:
    it must not wait for the share. Trimmed to its last 1,000 lines (about
    500 events) once past 2,000.
    #>
    param([Parameter(Mandatory = $true)][string]$StateDir)
    $map = @{}
    try {
        $path = Join-Path $StateDir 'delivered.txt'
        if (-not [System.IO.File]::Exists($path)) { return $map }
        $lines = [System.IO.File]::ReadAllLines($path)
        foreach ($line in $lines) {
            $parts = $line.Split([char]9)
            if ($parts.Length -lt 2 -or [string]::IsNullOrEmpty($parts[0])) { continue }
            $e = $map[$parts[0]]
            if ($null -eq $e) {
                $e = @{ Posted = $false; PostedAt = $null; Done = $false }
                $map[$parts[0]] = $e
            }
            if ($parts[1] -eq 'posted') {
                $e.Posted = $true
                if ($parts.Length -ge 3 -and -not [string]::IsNullOrEmpty($parts[2])) { $e.PostedAt = $parts[2] }
            }
            elseif ($parts[1] -eq 'done') { $e.Done = $true }
        }
        if ($lines.Length -gt 2000) {
            $keep = New-Object System.Text.StringBuilder
            for ($i = $lines.Length - 1000; $i -lt $lines.Length; $i++) { [void]$keep.AppendLine($lines[$i]) }
            $tmp = Join-Path $StateDir 'delivered.tmp'
            Write-Utf8File -Path $tmp -Text $keep.ToString()
            try { [System.IO.File]::Replace($tmp, $path, [NullString]::Value) }
            catch { [System.IO.File]::Delete($tmp) }
        }
    }
    catch { }
    return $map
}

function Add-DeliveryLedger {
    <#
    Records one step for Key: in the in-memory Ledger first (so this process
    never posts the same event twice even when the file cannot be written),
    then appended to StateDir\delivered.txt. A write failure is logged.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][hashtable]$Ledger,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Step,
        $PostedAt = $null,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Log
    )
    $e = $Ledger[$Key]
    if ($null -eq $e) {
        $e = @{ Posted = $false; PostedAt = $null; Done = $false }
        $Ledger[$Key] = $e
    }
    $line = "$Key`t$Step"
    if ($Step -eq 'posted') {
        $e.Posted = $true
        $e.PostedAt = $PostedAt
        $line = "$Key`tposted`t$PostedAt"
    }
    else { $e.Done = $true }
    try {
        [System.IO.File]::AppendAllText((Join-Path $StateDir 'delivered.txt'), "$line$([Environment]::NewLine)",
            (New-Object System.Text.UTF8Encoding $false))
    }
    catch {
        [void]$Log.Add("delivered-ids ledger not written for $Key ($($_.Exception.GetType().Name)): a tick that also fails to save its state could post it again")
    }
}

function Get-PendingFiles {
    # The queued event files, oldest first (their names start with a counter).
    param([Parameter(Mandatory = $true)][string]$PendingDir)
    if (-not [System.IO.Directory]::Exists($PendingDir)) { return @() }
    $files = [System.IO.Directory]::GetFiles($PendingDir, '*.json')
    [Array]::Sort($files, [System.StringComparer]::Ordinal)
    return $files
}

function Save-EventFile {
    <#
    Puts one event file in the local queue, StateDir\pending\, numbered after
    whatever is already there. Invoke-PendingFlush moves the queue to the
    share, oldest first, once the tick's posts are done: a post never waits
    on the share (an unreachable share costs an SMB timeout), and the share
    sees events in order. Only when the local queue cannot take the file is
    the share written directly. Returns 'queued', 'written', 'exists' or
    'lost'.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Text,
        [string]$ShareDir = "",
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Log
    )
    $pendingDir = Join-Path $StateDir 'pending'
    try {
        $queued = @(Get-PendingFiles -PendingDir $pendingDir)
        [void][System.IO.Directory]::CreateDirectory($pendingDir)
        $next = 1
        foreach ($q in $queued) {
            $m = [regex]::Match([System.IO.Path]::GetFileName($q), '^(\d+)_')
            if ($m.Success -and ([int]$m.Groups[1].Value) -ge $next) { $next = [int]$m.Groups[1].Value + 1 }
        }
        $name = '{0:D6}_{1}' -f $next, $Id
        [void](Publish-JsonFile -Dir $pendingDir -BaseName $name -Text $Text)
        return 'queued'
    }
    catch {
        [void]$Log.Add("could not queue $Id locally ($($_.Exception.GetType().Name): $($_.Exception.Message)); trying the share")
    }
    if (-not [string]::IsNullOrEmpty($ShareDir)) {
        try {
            [void][System.IO.Directory]::CreateDirectory($ShareDir)
            return (Publish-JsonFile -Dir $ShareDir -BaseName $Id -Text $Text -NoOverwrite)
        }
        catch {
            [void]$Log.Add("share not writable for $Id either ($($_.Exception.GetType().Name): $($_.Exception.Message))")
        }
    }
    return 'lost'
}

function Invoke-PendingFlush {
    <#
    Moves queued event files to the share, oldest first, and stops at the
    first share failure so the share sees them in order. An event already
    there is not rewritten. A queued file that cannot be read here is set
    aside as .bad rather than holding every later event behind it forever.
    Returns @{ Flushed; Left; ShareDown }.
    #>
    param(
        [string]$ShareDir = "",
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Log
    )
    $files = @(Get-PendingFiles -PendingDir (Join-Path $StateDir 'pending'))
    $left = $files.Count
    $flushed = 0
    if ($left -eq 0 -or [string]::IsNullOrEmpty($ShareDir)) {
        return @{ Flushed = 0; Left = $left; ShareDown = $false }
    }
    try { [void][System.IO.Directory]::CreateDirectory($ShareDir) }
    catch {
        [void]$Log.Add("share unavailable ($($_.Exception.GetType().Name)); $left event file(s) queued")
        return @{ Flushed = 0; Left = $left; ShareDown = $true }
    }
    foreach ($f in $files) {
        $name = [System.IO.Path]::GetFileNameWithoutExtension($f)
        $id = $name.Substring($name.IndexOf('_') + 1)
        $text = $null
        try { $text = [System.IO.File]::ReadAllText($f) }
        catch {
            [void]$Log.Add("queued $id unreadable ($($_.Exception.GetType().Name)); set aside as .bad")
            try { [System.IO.File]::Move($f, "$f.bad") } catch { }
            $left--
            continue
        }
        try { $r = Publish-JsonFile -Dir $ShareDir -BaseName $id -Text $text -NoOverwrite }
        catch {
            [void]$Log.Add("share did not take $id ($($_.Exception.GetType().Name)); $left event file(s) queued")
            break
        }
        $left--
        $flushed++
        [void]$Log.Add("queued $id -> share ($r)")
        # On the share now; a copy left here would only be found 'exists'
        # (or, once Hive has moved it, rewritten and recorded as 'already').
        try { [System.IO.File]::Delete($f) }
        catch { [void]$Log.Add("could not remove queued $id ($($_.Exception.GetType().Name))") }
    }
    return @{ Flushed = $flushed; Left = $left; ShareDown = $false }
}

function Send-WatchEvent {
    <#
    Delivers one event: the direct Slack post first, so posted_by_pc is final
    before the file exists, then the event file into the local queue. A
    failed post is not retried from here: posted_by_pc false is Hive's cue to
    relay it. Skip: why no post is tried (an earlier post this tick failed).

    The delivery ledger is checked first (key: LedgerKey, else the event id).
    Done: nothing is posted or filed again. Posted but not yet filed: the
    file goes out as posted_by_pc true with the first post's time, and no
    second post. A successful post is recorded the moment Slack accepts it.
    -Stale (ruling R19, a record over 2 h old when read): never posted; filed
    with "stale": true and posted_by_pc true, so Hive records its keys and
    never relays it. The tick's one summary says it happened.
    Returns @{ Posted; Status; Filed; Tried }.
    #>
    param(
        [Parameter(Mandatory = $true)]$Event,
        [scriptblock]$Sender = $null,
        [string]$Skip = "",
        [string]$ShareDir = "",
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][datetime]$Now,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Log,
        [Parameter(Mandatory = $true)][hashtable]$Ledger,
        [string]$LedgerKey = "",
        [switch]$Stale
    )
    $key = $LedgerKey
    if ([string]::IsNullOrEmpty($key)) { $key = [string]$Event['id'] }
    $what = "$($Event['type'])"
    if ($null -ne $Event['title']) { $what = "$what '$($Event['title'])'" }
    $seen = $Ledger[$key]
    if ($null -ne $seen -and $seen.Done) {
        [void]$Log.Add("$what at $($Event['at']) ($key) already delivered: not posted or filed again")
        return @{ Posted = $false; Status = 'already delivered'; Filed = 'already'; Tried = $false }
    }
    $posted = $false
    $tried = $false
    $status = 'no webhook configured'
    if ($null -ne $seen -and $seen.Posted) {
        $posted = $true
        $status = 'posted on an earlier tick'
        $Event['posted_by_pc'] = $true
        $Event['posted_at'] = $seen.PostedAt
    }
    elseif ($Stale) {
        $status = 'over 2 h old when read: in the summary, not posted'
        $Event['posted_by_pc'] = $true
        $Event['stale'] = $true
    }
    else {
        if (-not [string]::IsNullOrEmpty($Skip)) { $status = $Skip }
        elseif ($null -ne $Sender) {
            $tried = $true
            $res = Invoke-Sender -Sender $Sender -Payload $Event['payload']
            $posted = $res.Ok
            $status = $res.Status
        }
        $Event['posted_by_pc'] = $posted
        if ($posted) {
            $Event['posted_at'] = ConvertTo-IsoLocal $Now
            Add-DeliveryLedger -StateDir $StateDir -Ledger $Ledger -Key $key -Step 'posted' -PostedAt $Event['posted_at'] -Log $Log
        }
    }
    $text = ConvertTo-Json -InputObject $Event -Depth 10
    $filed = Save-EventFile -Id $Event['id'] -Text $text -ShareDir $ShareDir -StateDir $StateDir -Log $Log
    if ($filed -ne 'lost') {
        Add-DeliveryLedger -StateDir $StateDir -Ledger $Ledger -Key $key -Step 'done' -Log $Log
    }
    $how = "posted ($status)"
    if ($Stale -and -not $posted) { $how = "not posted ($status); Hive records it and never relays it" }
    elseif (-not $posted) { $how = "NOT posted ($status); Hive will relay it" }
    [void]$Log.Add("$what at $($Event['at']) id $($Event['id']): $how; file $filed")
    return @{ Posted = ($posted -and $tried); Status = $status; Filed = $filed; Tried = $tried }
}

function Invoke-RecordAlert {
    <#
    Turns one parsed record into the event to deliver, or $null.
    Stop: category, dedup key, Get-AlertDecision; then ruling R13: a 'fold'
    (or 'reminder') becomes 'new' when an Evosep procedure started after the
    key's last occurrence, because the queue was restarted in between.
    lastShutdownAt is recorded for every stop, folded or not.
    Restart: an event only when no shutdown came in the 5 min before.
    #>
    param(
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)]$Record,
        [string]$ProcedureRoot = "",
        [string]$SampleLogPath = "",
        [Parameter(Mandatory = $true)][string]$HostName,
        [string]$MemberId = "",
        [string]$Version = "",
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Log
    )
    $at = [datetime]$Record.At
    if ($Record.Type -eq 'restart') {
        if (-not (Test-UnexpectedRestart -State $State -At $at)) {
            [void]$Log.Add("HyStar session start at $(ConvertTo-IsoLocal $at) follows a shutdown: no alert")
            return $null
        }
        $payload = Format-RestartPayload -Event @{ At = $at } -Host $HostName
        return (New-WatchEvent -State $State -Type 'restart' -HostName $HostName -At $at `
                -Source $Record.Source -Title 'HyStar restarted' -Message $Record.Message `
                -DedupKey (Get-DedupKey ([string]$Record.Message)) -Payload $payload -Version $Version)
    }
    if ($Record.Type -ne 'stop') { return $null }

    $cat = Get-StopCategory ([string]$Record.Message)
    $key = Get-DedupKey ([string]$Record.Message)
    # An empty message still stops the queue; it must not become an empty
    # key, which Get-AlertDecision refuses (and the stop would never page).
    if ([string]::IsNullOrEmpty($key)) { $key = "(no message) $($Record.Source)" }
    if ($null -eq $State['keys']) { $State['keys'] = @{} }
    $prevLast = $null
    if ($State['keys'].ContainsKey($key)) { $prevLast = $State['keys'][$key].LastAt }
    if ($null -ne $prevLast -and $at -lt $prevLast) {
        # The clock went back (DST fall-back repeats 01:00-02:00 local): a gap
        # that reads negative is not "under 5 min", so decide as for a new key.
        [void]$State['keys'].Remove($key)
        $prevLast = $null
    }
    $decision = Get-AlertDecision -State $State -Key $key -At $at
    if ($decision -ne 'new' -and $null -ne $prevLast) {
        if (Test-InjectionSince -ProcedureRoot $ProcedureRoot -Since $prevLast -Until $at) {
            $State['keys'][$key] = @{ FirstAt = $at; LastAt = $at; Count = 1; ReminderSent = $false }
            [void]$Log.Add("an injection started since the last '$($cat.Title)' at $(ConvertTo-IsoLocal $prevLast): a new stop, not a repeat")
            $decision = 'new'
        }
    }
    $lastStop = ConvertTo-WatchDate $State['lastShutdownAt']
    if ($null -eq $lastStop -or $at -gt $lastStop) { $State['lastShutdownAt'] = $at }

    $entry = $State['keys'][$key]
    if ($decision -eq 'fold') {
        [void]$Log.Add("'$($cat.Title)' at $(ConvertTo-IsoLocal $at) folded into the alert of $(ConvertTo-IsoLocal $entry.FirstAt) (x$($entry.Count))")
        return $null
    }
    if ($decision -eq 'reminder') {
        $payload = Format-ReminderPayload -Event @{ Title = $cat.Title; Count = $entry.Count; FirstAt = $entry.FirstAt } `
            -MemberId $MemberId -Host $HostName
        return (New-WatchEvent -State $State -Type 'reminder' -HostName $HostName -At $at `
                -Source $Record.Source -Category $cat.Category -Title $cat.Title -Message $Record.Message `
                -DedupKey $key -Count ([int]$entry.Count) -FirstAt $entry.FirstAt -Payload $payload -Version $Version)
    }
    $vial = $null
    if (-not [string]::IsNullOrEmpty($ProcedureRoot)) {
        $vial = Get-EvosepVial -ProcedureRoot $ProcedureRoot -At $at -Category $cat.Category
    }
    $sample = $null
    if (-not [string]::IsNullOrEmpty($SampleLogPath)) { $sample = Get-HyStarSample -LogbookPath $SampleLogPath -At $at }
    $info = @{
        Category = $cat.Category; Title = $cat.Title; At = $at
        Message = $Record.Message; Vial = $vial; Sample = $sample
    }
    $payload = Format-StopPayload -Event $info -MemberId $MemberId -Host $HostName
    return (New-WatchEvent -State $State -Type 'stop' -HostName $HostName -At $at `
            -Source $Record.Source -Category $cat.Category -Title $cat.Title -Message $Record.Message `
            -Vial $vial -Sample $sample -DedupKey $key -Count 1 -FirstAt $at -Payload $payload -Version $Version)
}

function Get-NewestFileTime {
    # LastWriteTimeUtc of the newest file matching Pattern in Dir, or $null.
    # UTC, so the repeated local hour at DST fall-back cannot skew an age.
    param([string]$Dir = "", [Parameter(Mandatory = $true)][string]$Pattern)
    if ([string]::IsNullOrEmpty($Dir)) { return $null }
    if (-not [System.IO.Directory]::Exists($Dir)) { return $null }
    $newest = $null
    foreach ($f in [System.IO.Directory]::GetFiles($Dir, $Pattern)) {
        $t = [System.IO.File]::GetLastWriteTimeUtc($f)
        if ($null -eq $newest -or $t -gt $newest) { $newest = $t }
    }
    return $newest
}

function Get-HiveWatchNote {
    <#
    Watches Hive's own alerting from off Hive. The newest
    cron_stan_alerts_*.log older than 45 min gives one 'hive_quiet'; once a
    fresh one appears, one 'hive_back'. Only with a sender: with no route to
    Slack the PC has no one to tell. A log folder it cannot see (share down)
    decides nothing. Returns @{ Kind; LastAt; Key } or $null; updates State.
    #>
    param(
        [Parameter(Mandatory = $true)][hashtable]$State,
        [string]$HiveLogsDir = "",
        [Parameter(Mandatory = $true)][datetime]$Now,
        [scriptblock]$Sender = $null
    )
    if ($null -eq $Sender) { return $null }
    $newestUtc = $null
    try { $newestUtc = Get-NewestFileTime -Dir $HiveLogsDir -Pattern 'cron_stan_alerts_*.log' }
    catch { return $null }
    if ($null -eq $newestUtc) { return $null }
    $newest = $newestUtc.ToLocalTime()
    $quiet = (($Now.ToUniversalTime() - $newestUtc).TotalMinutes -gt 45)
    $wasQuiet = ($State['hiveQuiet'] -eq $true)
    # Key: the same for every tick that decides about one quiet episode, so
    # the delivery ledger stops a repeat when state.json cannot be saved.
    if ($quiet -and -not $wasQuiet) {
        $State['hiveQuiet'] = $true
        $State['hiveQuietSince'] = $newest
        return @{ Kind = 'hive_quiet'; LastAt = $newest; Key = "hive_quiet:$(ConvertTo-IsoLocal $newest)" }
    }
    if ($wasQuiet -and -not $quiet) {
        $since = ConvertTo-IsoLocal $State['hiveQuietSince']
        $State['hiveQuiet'] = $false
        $State['hiveQuietSince'] = $null
        return @{ Kind = 'hive_back'; LastAt = $newest; Key = "hive_back:$since" }
    }
    return $null
}

function Read-WatchState {
    # state.json in the hashtable form, or $null when there is none. Throws
    # when it cannot be parsed; the tick then starts again at the end.
    param([Parameter(Mandatory = $true)][string]$StateDir)
    $path = Join-Path $StateDir 'state.json'
    if (-not [System.IO.File]::Exists($path)) { return $null }
    $raw = ConvertFrom-Json ([System.IO.File]::ReadAllText($path))
    if ($null -eq $raw) { return $null }
    $st = ConvertTo-WatchState $raw
    foreach ($n in @('lastRecordAt', 'pendingSince', 'hiveQuietSince')) {
        $st[$n] = ConvertTo-WatchDate $st[$n]
    }
    return $st
}

function Save-WatchState {
    <#
    Writes state.json (UTF-8, no BOM, temp + rename). Dates go out as local
    ISO strings: PS 5.1 would write them as /Date(ms)/. Keys not seen for a
    day are dropped: past 5 min a key only decides 'new', which a missing
    key decides too.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][datetime]$Now
    )
    $keys = [ordered]@{}
    $src = $State['keys']
    if ($null -ne $src) {
        # get_Keys(): a dedup key spelled "keys" must not shadow the property.
        foreach ($k in @($src.get_Keys())) {
            $e = $src[$k]
            $last = ConvertTo-WatchDate $e.LastAt
            if ($null -ne $last -and $last -lt $Now.AddDays(-1)) { continue }
            $keys[[string]$k] = [ordered]@{
                FirstAt      = (ConvertTo-IsoLocal $e.FirstAt)
                LastAt       = (ConvertTo-IsoLocal $last)
                Count        = [int]$e.Count
                ReminderSent = ($e.ReminderSent -eq $true)
            }
        }
    }
    $offset = $null
    if ($null -ne $State['offset']) { $offset = [long]$State['offset'] }
    $length = $null
    if ($null -ne $State['length']) { $length = [long]$State['length'] }
    $fpLen = $null
    if ($null -ne $State['fingerprintLength']) { $fpLen = [int]$State['fingerprintLength'] }
    $seq = 0
    if ($null -ne $State['eventSeq']) { $seq = [int]$State['eventSeq'] }
    $doc = [ordered]@{
        schema            = 1
        logbook           = $State['logbook']
        offset            = $offset
        length            = $length
        fingerprint       = $State['fingerprint']
        fingerprintLength = $fpLen
        lastRecordAt      = (ConvertTo-IsoLocal $State['lastRecordAt'])
        lastShutdownAt    = (ConvertTo-IsoLocal $State['lastShutdownAt'])
        pendingSince      = (ConvertTo-IsoLocal $State['pendingSince'])
        hiveQuiet         = ($State['hiveQuiet'] -eq $true)
        hiveQuietSince    = (ConvertTo-IsoLocal $State['hiveQuietSince'])
        eventSeq          = $seq
        keys              = $keys
    }
    [void](Publish-JsonFile -Dir $StateDir -BaseName 'state' -Text (ConvertTo-Json -InputObject $doc -Depth 10))
}

function Write-Heartbeat {
    # heartbeat.json on the share, every tick: what makes a dead watcher
    # visible. Hive ages it by the file's mtime; at/utc are for display.
    param(
        [Parameter(Mandatory = $true)][string]$ShareDir,
        [Parameter(Mandatory = $true)][datetime]$Now,
        [string]$Version = "",
        [string]$LogbookPath = "",
        $Offset = $null,
        $LastError = $null
    )
    [void][System.IO.Directory]::CreateDirectory($ShareDir)
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $doc = [ordered]@{
        at         = (ConvertTo-IsoLocal $Now)
        utc        = $Now.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", $inv)
        version    = $Version
        logbook    = $LogbookPath
        offset     = $Offset
        last_error = $LastError
    }
    [void](Publish-JsonFile -Dir $ShareDir -BaseName 'heartbeat' -Text (ConvertTo-Json -InputObject $doc -Depth 10))
}

function Write-WatchLog {
    # Appends Lines (scrubbed, each stamped) to StateDir\watch.log. Past 1 MB
    # the log becomes watch.log.1 first: this runs 1,440 times a day.
    # Never throws.
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][datetime]$Now,
        [AllowEmptyCollection()]$Lines = @()
    )
    try {
        $path = Join-Path $StateDir 'watch.log'
        if ([System.IO.File]::Exists($path)) {
            if ((New-Object System.IO.FileInfo $path).Length -gt 1048576) {
                $old = Join-Path $StateDir 'watch.log.1'
                if ([System.IO.File]::Exists($old)) { [System.IO.File]::Delete($old) }
                [System.IO.File]::Move($path, $old)
            }
        }
        $stamp = $Now.ToString('yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture)
        $sb = New-Object System.Text.StringBuilder
        foreach ($l in @($Lines)) {
            [void]$sb.AppendLine("$stamp $(Get-ScrubbedText ([string]$l))")
        }
        [System.IO.File]::AppendAllText($path, $sb.ToString(), (New-Object System.Text.UTF8Encoding $false))
    }
    catch { }
}

function Test-StaleRecord {
    # Ruling R19: a record read more than 2 h after its time (a reinstall
    # after a long uninstall, the PC off and then on) is history, not news.
    # A time after Now (the clock set back) is not old.
    param(
        [Parameter(Mandatory = $true)][datetime]$At,
        [Parameter(Mandatory = $true)][datetime]$Now
    )
    return (($Now - $At).TotalMinutes -gt 120)
}

function Test-StateDirWritable {
    <#
    "" when a small file can be created and removed in StateDir, else why
    not. state.json and delivered.txt both live there, so a full C: or a
    broken ACL loses both at once: every tick would then re-read the same
    records from the old offset, decide 'new' with the same ids, find no
    ledger line, and page the same stop again, every minute, while the
    heartbeat stayed fresh. Never throws.
    #>
    param([Parameter(Mandatory = $true)][string]$StateDir)
    $probe = Join-Path $StateDir "write_probe_$PID.tmp"
    try {
        [System.IO.File]::WriteAllText($probe, 'x')
        [System.IO.File]::Delete($probe)
        return ""
    }
    catch {
        $why = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
        try { if ([System.IO.File]::Exists($probe)) { [System.IO.File]::Delete($probe) } } catch { }
        return $why
    }
}

function Invoke-WatchTick {
    <#
    One full tick. Never throws. Returns a summary hashtable
    @{ Seeded; Records; Events; Posted; Filed; Queued; Errors }
    (Filed: event files that reached the share this tick; Queued: left in
    the local queue).

      1. state.json and the delivery ledger from StateDir (no state, or
         unreadable: start at the end of the logbook). StateDir must take a
         small file (Test-StateDirWritable): when it cannot, nothing is
         posted directly this tick, because nothing could remember the post.
         Every event goes to the share with posted_by_pc false instead; its
         id is the same every tick (the state is not saved), so Hive's own
         key relays each exactly once. Heartbeat last_error says so.
      2. new logbook records, through rotation (Get-LogbookRecords)
      3. each record decided (Invoke-RecordAlert); an event is posted through
         Sender (@{ Ok; Status }), recorded in the ledger, and its file put
         in the local queue. Nothing so far touches the share: a direct post
         never waits on an unreachable share (spec 4.1). A record more than
         2 h old (ruling R19) is filed as stale, never posted, and the tick
         then posts ONE summary of those stops, with no @mention.
      4. Hive's alerting watched (only with a Sender, and a StateDir that
         can remember the note; its logs are on the share, so after the
         stop posts)
      5. state saved, then the queue flushed to the share, oldest first,
         then heartbeat.json (skipped when the flush just found the share
         unreachable), then watch.log appended

    Sender: a scriptblock taking the payload hashtable and returning
    @{ Ok = <bool>; Status = <int|string> }, or $null for no direct post
    (every event then carries posted_by_pc false and Hive relays it).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][string]$LogbookPath,
        [string]$ShareDir = "",
        [string]$ProcedureRoot = "",
        [string]$HiveLogsDir = "",
        [datetime]$Now = (Get-Date),
        [scriptblock]$Sender = $null,
        [string]$MemberId = "",
        [string]$HostName = "",
        [string]$SampleLogPath = "",
        [string]$Version = ""
    )
    $summary = @{ Seeded = $false; Records = 0; Events = 0; Posted = 0; Filed = 0; Queued = 0; Errors = 0 }
    $log = New-Object System.Collections.ArrayList
    $lastError = $null
    $state = $null
    # After one failed post, the rest of this tick goes to files only (Hive
    # relays them): with no route, each post would wait out its 10 s timeout
    # and a backlog could outlast the scheduled task's time limit.
    $skipPost = ""
    # Set when StateDir cannot be written: the reason, for the heartbeat.
    $stateDirError = $null
    # Ruling R19: how many stops this tick read over 2 h late, and the newest.
    $staleStops = 0
    $staleLatest = $null
    try {
        if ([string]::IsNullOrEmpty($Version)) { $Version = [string]$ScriptVersion }
        if ([string]::IsNullOrEmpty($HostName)) { $HostName = [string]$env:COMPUTERNAME }
        if ([string]::IsNullOrEmpty($HostName)) { $HostName = 'TIMS-10878' }
        if ([string]::IsNullOrEmpty($SampleLogPath)) {
            $logDir = [System.IO.Path]::GetDirectoryName($LogbookPath)
            if (-not [string]::IsNullOrEmpty($logDir)) { $SampleLogPath = Join-Path $logDir 'CompassHyStarLogbook.log' }
        }
        [void][System.IO.Directory]::CreateDirectory($StateDir)
        $why = Test-StateDirWritable -StateDir $StateDir
        if (-not [string]::IsNullOrEmpty($why)) {
            $stateDirError = "state folder not writable ($why): nothing is posted directly; Hive relays every event"
            $skipPost = "not tried: the state folder cannot be written, so the post could not be remembered"
            [void]$log.Add($stateDirError)
        }
        try { $state = Read-WatchState -StateDir $StateDir }
        catch {
            [void]$log.Add("state.json unreadable ($($_.Exception.GetType().Name)); starting again at the end of the logbook")
            $state = $null
        }
        if ($null -eq $state) { $state = ConvertTo-WatchState $null }
        $ledger = Read-DeliveryLedger -StateDir $StateDir

        $records = @()
        try {
            if ([System.IO.File]::Exists($LogbookPath)) {
                $got = Get-LogbookRecords -State $state -LogbookPath $LogbookPath -Now $Now -Log $log
                $records = @($got.Records)
                $summary['Seeded'] = $got.Seeded
            }
            else {
                $lastError = "logbook not found: $LogbookPath"
                [void]$log.Add($lastError)
            }
        }
        catch {
            # Offsets are untouched, so the same bytes are read next tick.
            $summary['Errors'] = $summary['Errors'] + 1
            $lastError = "logbook not read: $($_.Exception.GetType().Name): $($_.Exception.Message)"
            [void]$log.Add($lastError)
        }

        foreach ($rec in $records) {
            $summary['Records'] = $summary['Records'] + 1
            $recAt = ConvertTo-WatchDate $rec.At
            $lastRec = ConvertTo-WatchDate $state['lastRecordAt']
            if ($null -ne $recAt -and ($null -eq $lastRec -or $recAt -gt $lastRec)) { $state['lastRecordAt'] = $recAt }
            try {
                $ev = Invoke-RecordAlert -State $state -Record $rec -ProcedureRoot $ProcedureRoot `
                    -SampleLogPath $SampleLogPath -HostName $HostName -MemberId $MemberId -Version $Version -Log $log
                if ($null -eq $ev) { continue }
                $stale = Test-StaleRecord -At ([datetime]$rec.At) -Now $Now
                $sent = Send-WatchEvent -Event $ev -Sender $Sender -Skip $skipPost -ShareDir $ShareDir `
                    -StateDir $StateDir -Now $Now -Log $log -Ledger $ledger -Stale:$stale
                if ($stale -and $ev['type'] -eq 'stop') {
                    $staleStops++
                    if ($null -eq $staleLatest -or [datetime]$rec.At -gt $staleLatest) { $staleLatest = [datetime]$rec.At }
                }
                $summary['Events'] = $summary['Events'] + 1
                if ($sent.Posted) { $summary['Posted'] = $summary['Posted'] + 1 }
                elseif ($sent.Tried) {
                    $lastError = "Slack post failed: $($sent.Status)"
                    $skipPost = "not tried: a post failed earlier this tick"
                }
                if ($sent.Filed -eq 'written' -or $sent.Filed -eq 'exists') { $summary['Filed'] = $summary['Filed'] + 1 }
            }
            catch {
                $summary['Errors'] = $summary['Errors'] + 1
                $lastError = "record at $(ConvertTo-IsoLocal $rec.At) failed: $($_.Exception.GetType().Name): $($_.Exception.Message)"
                [void]$log.Add($lastError)
            }
        }

        if ($staleStops -gt 0) {
            try {
                $payload = Format-StaleSummaryPayload -Count $staleStops -Latest $staleLatest -Now $Now -Host $HostName
                # At the newest stale stop, not Now: a tick that re-reads the
                # same records (its state not saved) makes the same id and
                # ledger key, so the summary is never sent twice.
                $ev = New-WatchEvent -State $state -Type 'stale_summary' -HostName $HostName -At $staleLatest `
                    -Source 'watcher' -Title 'Older stops' -Message $payload.text -Count $staleStops `
                    -Payload $payload -Version $Version
                $sent = Send-WatchEvent -Event $ev -Sender $Sender -Skip $skipPost -ShareDir $ShareDir `
                    -StateDir $StateDir -Now $Now -Log $log -Ledger $ledger `
                    -LedgerKey "stale_summary:$(ConvertTo-IsoLocal $staleLatest):$staleStops"
                $summary['Events'] = $summary['Events'] + 1
                if ($sent.Posted) { $summary['Posted'] = $summary['Posted'] + 1 }
                elseif ($sent.Tried) {
                    $lastError = "Slack post failed: $($sent.Status)"
                    $skipPost = "not tried: a post failed earlier this tick"
                }
                if ($sent.Filed -eq 'written' -or $sent.Filed -eq 'exists') { $summary['Filed'] = $summary['Filed'] + 1 }
            }
            catch {
                $summary['Errors'] = $summary['Errors'] + 1
                $lastError = "older-stops summary failed: $($_.Exception.GetType().Name): $($_.Exception.Message)"
                [void]$log.Add($lastError)
            }
        }

        try {
            # A note about Hive is the PC's to post, or no one's: with a
            # StateDir that cannot remember it, it would be filed afresh
            # (a new id) every tick, and Hive would relay every one.
            $hiveSender = $Sender
            if ($null -ne $stateDirError) { $hiveSender = $null }
            $note = Get-HiveWatchNote -State $state -HiveLogsDir $HiveLogsDir -Now $Now -Sender $hiveSender
            if ($null -ne $note) {
                if ($note.Kind -eq 'hive_quiet') {
                    $payload = Format-HiveQuietPayload -At $note.LastAt
                    $title = "STAN's Hive alerting has gone quiet"
                }
                else {
                    $payload = Format-HiveQuietPayload -Back
                    $title = "STAN's Hive alerting is back"
                }
                $ev = New-WatchEvent -State $state -Type $note.Kind -HostName $HostName -At $Now `
                    -Source 'watcher' -Title $title -Message $payload.text -Payload $payload -Version $Version
                $sent = Send-WatchEvent -Event $ev -Sender $Sender -Skip $skipPost -ShareDir $ShareDir `
                    -StateDir $StateDir -Now $Now -Log $log -Ledger $ledger -LedgerKey $note.Key
                $summary['Events'] = $summary['Events'] + 1
                if ($sent.Posted) { $summary['Posted'] = $summary['Posted'] + 1 }
            }
        }
        catch {
            $summary['Errors'] = $summary['Errors'] + 1
            $lastError = "Hive watch failed: $($_.Exception.GetType().Name): $($_.Exception.Message)"
            [void]$log.Add($lastError)
        }
    }
    catch {
        $summary['Errors'] = $summary['Errors'] + 1
        $lastError = "tick failed: $($_.Exception.GetType().Name): $($_.Exception.Message)"
        [void]$log.Add($lastError)
    }
    finally {
        if ($null -ne $state) {
            try { Save-WatchState -StateDir $StateDir -State $state -Now $Now }
            catch {
                $summary['Errors'] = $summary['Errors'] + 1
                $lastError = "state not saved: $($_.Exception.GetType().Name): $($_.Exception.Message)"
                [void]$log.Add($lastError)
            }
        }
        # Only now the share: everything above was local.
        $shareDown = $false
        try {
            $fl = Invoke-PendingFlush -ShareDir $ShareDir -StateDir $StateDir -Log $log
            $summary['Filed'] = $summary['Filed'] + $fl.Flushed
            $summary['Queued'] = $fl.Left
            $shareDown = $fl.ShareDown
        }
        catch {
            $summary['Errors'] = $summary['Errors'] + 1
            [void]$log.Add("queue not flushed: $($_.Exception.GetType().Name): $($_.Exception.Message)")
        }
        if ($shareDown) {
            [void]$log.Add("heartbeat not written: the share is unreachable")
        }
        elseif (-not [string]::IsNullOrEmpty($ShareDir)) {
            $offset = $null
            if ($null -ne $state) { $offset = $state['offset'] }
            $errText = $null
            if ($null -ne $lastError) { $errText = Get-ScrubbedText $lastError }
            # The unwritable state folder is the cause; "state not saved" is
            # only its echo. Anything else wrong this tick is kept beside it.
            if ($null -ne $stateDirError) {
                $errText = Get-ScrubbedText $stateDirError
                if ($null -ne $lastError -and -not ([string]$lastError).StartsWith('state not saved')) {
                    $errText = Get-ScrubbedText "$stateDirError; $lastError"
                }
            }
            try {
                Write-Heartbeat -ShareDir $ShareDir -Now $Now -Version $Version -LogbookPath $LogbookPath `
                    -Offset $offset -LastError $errText
            }
            catch {
                [void]$log.Add("heartbeat not written ($($_.Exception.GetType().Name): $($_.Exception.Message))")
            }
        }
        $off = ''
        if ($null -ne $state) { $off = $state['offset'] }
        # The version banner, once per tick, last so a quiet tick is one line.
        [void]$log.Add("watch_hystar_errors v$($Version): offset $off, $($summary['Records']) record(s), $($summary['Events']) event(s), $($summary['Posted']) posted, $($summary['Filed']) filed, $($summary['Queued']) queued, $($summary['Errors']) error(s)")
        Write-WatchLog -StateDir $StateDir -Now $Now -Lines $log
    }
    return $summary
}

function Send-SlackPayload {
    <#
    Posts Payload to a Slack incoming webhook. Returns @{ Ok; Status }:
    Status is the HTTP status code, or the error's type and message with the
    URL scrubbed out. Never throws, and never puts the URL in Status.
    The body is sent as UTF-8 bytes (ruling R14): PS 5.1 would otherwise
    encode a string body in the system code page and mangle the dot and the
    dash. -Depth 10: PS 5.1's default of 2 truncates Slack blocks.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$WebhookUrl,
        [Parameter(Mandatory = $true)]$Payload
    )
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = 'SilentlyContinue'
        $json = ConvertTo-Json -InputObject $Payload -Depth 10 -Compress
        $body = [System.Text.Encoding]::UTF8.GetBytes($json)
        $resp = Invoke-WebRequest -Uri $WebhookUrl -Method Post -ContentType 'application/json; charset=utf-8' `
            -Body $body -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
        $code = [int]$resp.StatusCode
        $ok = $false
        if ($code -ge 200 -and $code -lt 300) { $ok = $true }
        return @{ Ok = $ok; Status = $code }
    }
    catch {
        $caught = $_
        $code = 0
        try {
            $r = $caught.Exception.Response
            if ($null -ne $r) { $code = [int]$r.StatusCode }
        }
        catch { $code = 0 }
        if ($code -gt 0) { return @{ Ok = $false; Status = $code } }
        $why = Get-ScrubbedText -Text "$($caught.Exception.GetType().Name): $($caught.Exception.Message)" -Secret $WebhookUrl
        if ($why.Length -gt 300) { $why = $why.Substring(0, 300) }
        return @{ Ok = $false; Status = $why }
    }
}

function Get-WatchConfig {
    <#
    The watcher's paths: config.json in the state dir (non-secret, written at
    install: logbook, memberId, shareDir, procedureRoot, hiveLogsDir), with
    defaults for anything missing. The share is Y:\STAN when the operator's
    mapped drive is there, else its UNC path, the same folder; it is chosen
    afresh on every call unless config.json names a folder.
    -StoredOnly: only the values config.json itself holds, no defaults (what
    a re-install keeps).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [switch]$StoredOnly
    )
    $stored = @{}
    try {
        $path = Join-Path $StateDir 'config.json'
        if ([System.IO.File]::Exists($path)) {
            $doc = ConvertFrom-Json ([System.IO.File]::ReadAllText($path))
            foreach ($k in @('logbook', 'memberId', 'shareDir', 'procedureRoot', 'hiveLogsDir')) {
                $v = Get-StateField $doc $k
                if (-not [string]::IsNullOrEmpty([string]$v)) { $stored[$k] = [string]$v }
            }
        }
    }
    catch { }
    if ($StoredOnly) { return $stored }
    $cfg = @{
        logbook       = 'C:\BDalSystemData\HyStar\LogFiles\CompassHyStarErrorLogbook.log'
        memberId      = ''
        shareDir      = ''
        procedureRoot = 'C:\ProgramData\Evosep\EvosepOne\Procedure logs'
        hiveLogsDir   = ''
    }
    foreach ($k in @($stored.Keys)) { $cfg[$k] = $stored[$k] }
    # The share's defaults only for what config.json leaves unset. Join-Path
    # asks the drive's provider, so it throws on a drive that is not there:
    # Y: only once it is known to exist.
    if ([string]::IsNullOrEmpty($cfg['shareDir']) -or [string]::IsNullOrEmpty($cfg['hiveLogsDir'])) {
        $root = '\\128.120.208.42\proteomics-grp\STAN'
        if ([System.IO.Directory]::Exists('Y:\STAN')) { $root = 'Y:\STAN' }
        if ([string]::IsNullOrEmpty($cfg['shareDir'])) {
            $cfg['shareDir'] = (Join-Path (Join-Path $root 'hystar_events') ([string]$env:COMPUTERNAME))
        }
        if ([string]::IsNullOrEmpty($cfg['hiveLogsDir'])) { $cfg['hiveLogsDir'] = (Join-Path $root 'logs') }
    }
    return $cfg
}

# ===================================================================== install
# Everything below runs only when a person runs the script (the .bat), never
# under -Scheduled: the top level dispatches the tick first and exits.
#
# The install is split in two, like the copy scripts'. What needs
# administrator rights (the scheduled task, and the state folder's ACL) runs
# in an elevated copy of this script (-InstallOnly). What belongs to the
# desktop's user (the questions, config.json, the webhook, the test message)
# runs in the window the .bat opened, as that user. DPAPI ties the webhook to
# the account that stores it, and the task runs as the desktop's user, so a
# webhook stored from the elevated window would be unreadable to the task
# whenever the administrator prompt was answered with another account.

function Say($m, $c = 'Gray') {
    if ($Scheduled) {
        # A scheduled run has no console; the tick keeps its own log.
        Write-WatchLog -StateDir (Join-Path $env:ProgramData 'STAN\hystar_watch') -Now (Get-Date) -Lines @(, [string]$m)
        return
    }
    Write-Host $m -ForegroundColor $c
}

function Pause-IfInteractive {
    # A scheduled run has no console. ReadKey there does not "wait for a key",
    # it throws or blocks, holding the task open until its time limit, which
    # is why this is gated rather than simply left at the end of the file.
    if ($Scheduled) { return }
    Say ''
    Say 'Press any key to continue . . .'
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { }
}

function Test-Task {
    $t = Get-ScheduledTask -TaskName $TaskName -EA SilentlyContinue
    if ($t) { return $true }
    return $false
}

function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-InstalledVersion {
    # Reads $ScriptVersion out of the installed copy without executing it.
    if (-not (Test-Path -LiteralPath $InstalledPath)) { return "" }
    foreach ($line in (Get-Content -LiteralPath $InstalledPath -EA SilentlyContinue)) {
        # No leading $ in the pattern: in a double-quoted PowerShell string
        # "$ScriptVersion" INTERPOLATES, so the pattern would match nothing
        # and the task would reinstall on every single run.
        if ($line -match "ScriptVersion\s*=\s*'([^']+)'") { return $Matches[1] }
    }
    return ""
}

function Remove-Task {
    if (-not (Test-Task)) { Say "No scheduled task named '$TaskName'." 'Yellow'; return $true }
    if (-not (Test-Elevated)) {
        Say 'Removing the scheduled task needs administrator rights.' 'Yellow'
        # The installed copy is on C:, which the elevated window can see; a
        # mapped drive the .bat may sit on is not visible to it.
        $self = $PSCommandPath
        if (Test-Path -LiteralPath $InstalledPath) { $self = $InstalledPath }
        $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $self), '-Uninstall')
        try { Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -Wait -ErrorAction Stop }
        catch { Say 'The administrator prompt was refused.' 'Red'; return $false }
        return (-not (Test-Task))
    }
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop }
    catch { Say "Could not remove the scheduled task: $($_.Exception.Message)" 'Red'; return $false }
    Say "Removed scheduled task '$TaskName'." 'Green'
    return $true
}

function Get-CurrentAccount {
    # DOMAIN\user of this process.
    try { return [Security.Principal.WindowsIdentity]::GetCurrent().Name }
    catch { return "$env:USERDOMAIN\$env:USERNAME" }
}

function Get-InteractiveUser {
    <#
    The account that owns this session's desktop (explorer.exe), as
    @{ Name = 'DOMAIN\user'; Sid = 'S-1-5-...' }, or $null when it cannot be
    told. Answering the administrator prompt with another account's password
    changes who runs the elevated window, not whose desktop it is: this is
    the account the watcher has to run as.
    #>
    try {
        $session = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
        foreach ($p in @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'explorer.exe'" -ErrorAction Stop)) {
            if ([int]$p.SessionId -ne $session) { continue }
            $o = Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction Stop
            if ($o.ReturnValue -ne 0 -or [string]::IsNullOrEmpty([string]$o.User)) { continue }
            $sid = ''
            try {
                $s = Invoke-CimMethod -InputObject $p -MethodName GetOwnerSid -ErrorAction Stop
                if ($s.ReturnValue -eq 0) { $sid = [string]$s.Sid }
            }
            catch { }
            return @{ Name = "$($o.Domain)\$($o.User)"; Sid = $sid }
        }
    }
    catch { }
    return $null
}

function Test-SameAccount {
    # DOMAIN\user against DOMAIN\user, or a bare user name against either
    # (Task Scheduler may report a local account without its domain).
    param([string]$A = "", [string]$B = "")
    if ([string]::IsNullOrEmpty($A) -or [string]::IsNullOrEmpty($B)) { return $false }
    $x = $A.Trim().ToLowerInvariant()
    $y = $B.Trim().ToLowerInvariant()
    if ($x -eq $y) { return $true }
    if ($x.Contains('\') -and $y.Contains('\')) { return $false }
    $xn = $x.Substring($x.LastIndexOf('\') + 1)
    $yn = $y.Substring($y.LastIndexOf('\') + 1)
    return ($xn -eq $yn)
}

function Get-TaskAccount {
    # The account the scheduled task runs as, as Task Scheduler reports it;
    # "" when it cannot be read.
    try {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        return [string]$t.Principal.UserId
    }
    catch { return "" }
}

function Get-TaskExecute {
    # The program the scheduled task starts, as Task Scheduler reports it;
    # "" when it cannot be read.
    try {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        foreach ($a in @($t.Actions)) { return [string]$a.Execute }
    }
    catch { }
    return ""
}

function Get-AccountWarning {
    <#
    "" when the task runs as the desktop's user (Desktop), else what to tell
    the installer (ruling R16): a task under another account runs only while
    that account is logged on, and cannot read the webhook, which DPAPI ties
    to the account that stored it.
    #>
    param([string]$TaskAccount = "", [string]$Desktop = "")
    if ([string]::IsNullOrEmpty($TaskAccount)) {
        return "Could not read which account the task '$TaskName' runs as. It must be $Desktop (Task Scheduler shows it)."
    }
    if ([string]::IsNullOrEmpty($Desktop)) { return "" }
    if (Test-SameAccount $TaskAccount $Desktop) { return "" }
    return "WARNING: the task runs as $TaskAccount, but this desktop belongs to $Desktop. It will only run while $TaskAccount is logged on, and cannot read the webhook stored for $Desktop. Run /uninstall, then install again as $Desktop."
}

function Initialize-StateDir {
    <#
    Ruling R16. The install runs elevated; the every-minute task does not.
    Creates StateDir if it is missing and grants Account Modify on it,
    inherited by every file and folder below, so the task can always write
    its own state there. Creates no file in it: the first scheduled tick,
    running as Account, seeds state.json and delivered.txt itself and so
    owns them. Run elevated. Returns @{ Ok; Why }.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        $Account = $null
    )
    try {
        $who = $null
        if ($null -ne $Account -and -not [string]::IsNullOrEmpty([string]$Account.Sid)) {
            $who = New-Object System.Security.Principal.SecurityIdentifier([string]$Account.Sid)
        }
        elseif ($null -ne $Account -and -not [string]::IsNullOrEmpty([string]$Account.Name)) {
            $who = New-Object System.Security.Principal.NTAccount([string]$Account.Name)
        }
        else {
            return @{ Ok = $false; Why = 'no account to grant' }
        }
        [void][System.IO.Directory]::CreateDirectory($StateDir)
        $di = New-Object System.IO.DirectoryInfo $StateDir
        $sec = $di.GetAccessControl()
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($who,
            [System.Security.AccessControl.FileSystemRights]'Modify',
            [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow)
        $sec.AddAccessRule($rule)
        $di.SetAccessControl($sec)
        return @{ Ok = $true; Why = '' }
    }
    catch {
        return @{ Ok = $false; Why = "$($_.Exception.GetType().Name): $($_.Exception.Message)" }
    }
}

function Get-HiddenShimText {
    <#
    The .vbs the task hands to wscript.exe (ruling R17). Run(command, 0,
    True): 0 opens no window at all; True waits for the tick, so the task's
    IgnoreNew and its 5-minute limit still see a tick that is running.
    WScript.Quit passes the tick's exit code on to Task Scheduler. Inside a
    VBS string a quote is written twice, so the path is wrapped in "" "".
    ASCII, CRLF line ends.
    #>
    param([Parameter(Mandatory = $true)][string]$ScriptPath)
    $q = '""'
    $command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $q$ScriptPath$q -Scheduled"
    $lines = @(
        "' STAN HyStar watcher: runs one tick with no window at all.",
        "' Written by watch_hystar_errors.ps1 at install; a re-install rewrites it.",
        "' Run(..., 0, True): 0 = no window; True = wait for the tick to end, so the",
        "' task's IgnoreNew and its 5-minute limit still apply while it runs.",
        "WScript.Quit CreateObject(`"WScript.Shell`").Run(`"$command`", 0, True)"
    )
    $text = $lines -join "`r`n"
    return "$text`r`n"
}

function Get-TaskLaunch {
    <#
    What the scheduled task starts (ruling R17): wscript.exe //B //Nologo on
    the .vbs shim at ShimPath, so a tick never shows a window. Falls back to
    powershell.exe -WindowStyle Hidden (which flashes a console each run),
    with the reason in Why, when wscript.exe or VBScript is missing (VBScript
    is an optional feature on recent Windows 11), when a path is not plain
    ASCII (the shim is written as ASCII), or when Fallback gives a reason.
    Returns @{ Execute; Argument; Shim (its text, or $null); Why }. Writes
    nothing.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string]$ShimPath,
        [AllowEmptyString()][string]$SystemRoot = $env:SystemRoot,
        [string]$Fallback = ""
    )
    $why = $Fallback
    $wscript = ""
    if ([string]::IsNullOrEmpty($why)) {
        if ([string]::IsNullOrEmpty($SystemRoot)) { $why = 'SystemRoot is not set' }
        else {
            $sys32 = Join-Path $SystemRoot 'System32'
            $wscript = Join-Path $sys32 'wscript.exe'
            if (-not [System.IO.File]::Exists($wscript)) { $why = "$wscript is missing" }
            elseif (-not [System.IO.File]::Exists((Join-Path $sys32 'vbscript.dll'))) {
                $why = 'VBScript is not installed (an optional feature on recent Windows 11)'
            }
        }
    }
    $shim = $null
    if ([string]::IsNullOrEmpty($why)) {
        $shim = Get-HiddenShimText -ScriptPath $ScriptPath
        if ($shim -match '[^\x00-\x7F]' -or $ShimPath -match '[^\x00-\x7F]') {
            $why = 'the install path is not plain ASCII'
            $shim = $null
        }
    }
    if (-not [string]::IsNullOrEmpty($why)) {
        return @{
            Execute  = 'powershell.exe'
            Argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`" -Scheduled"
            Shim     = $null
            Why      = $why
        }
    }
    return @{ Execute = $wscript; Argument = "//B //Nologo `"$ShimPath`""; Shim = $shim; Why = '' }
}

function Test-TaskLaunchCurrent {
    <#
    Whether the registered task starts what this version would register
    (Launch, from Get-TaskLaunch): the same program and, for the shim, the
    same shim text on disk. False means register again, even when the
    version is the same. A registered program that cannot be read
    (RegisteredExecute "") forces nothing.
    #>
    param(
        [string]$RegisteredExecute = "",
        [Parameter(Mandatory = $true)]$Launch,
        [Parameter(Mandatory = $true)][string]$ShimPath
    )
    if ([string]::IsNullOrEmpty($RegisteredExecute)) { return $true }
    if (-not [string]::Equals($RegisteredExecute, [string]$Launch.Execute, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    if ($null -eq $Launch.Shim) { return $true }
    try {
        if (-not [System.IO.File]::Exists($ShimPath)) { return $false }
        return ([System.IO.File]::ReadAllText($ShimPath) -ceq [string]$Launch.Shim)
    }
    catch { return $false }
}

function Get-UtcStartBoundary {
    <#
    Ruling R21: the repeating trigger's StartBoundary as a UTC ISO time with
    a Z, a minute after Now (Task Scheduler's "synchronize across time
    zones"). A local -Once boundary repeating every minute can have its next
    run computed as 02:00 when the clocks go back at 02:00, which leaves an
    hour without ticks: a false watcher-down page, and late alerts.
    #>
    param([Parameter(Mandatory = $true)][datetime]$Now)
    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $utc = $Now.ToUniversalTime().AddMinutes(1).ToString("yyyy-MM-dd'T'HH:mm:ss", $inv)
    return "$($utc)Z"
}

function Get-TaskRepeatBoundary {
    # The StartBoundary of the registered task's repeating trigger, as Task
    # Scheduler reports it; "" when it cannot be read.
    try {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        foreach ($tr in @($t.Triggers)) {
            $rep = $tr.Repetition
            if ($null -ne $rep -and -not [string]::IsNullOrEmpty([string]$rep.Interval)) {
                return [string]$tr.StartBoundary
            }
        }
    }
    catch { }
    return ""
}

function Get-TriggerReadback {
    <#
    What the install says about the repeating trigger it read back (R21):
    @{ Text; Color }. UTC (ends with Z) is as intended; a local boundary
    means Task Scheduler refused UTC and the install fell back.
    #>
    param([AllowEmptyString()][string]$Boundary = "")
    if ([string]::IsNullOrEmpty($Boundary)) {
        return @{ Text = "Could not read the every-minute trigger back. In Task Scheduler, its start time should end in Z (UTC)."; Color = 'Yellow' }
    }
    if ($Boundary.EndsWith('Z')) {
        return @{ Text = "The every-minute trigger runs on UTC (from $Boundary): the clocks going back cannot pause it."; Color = 'Gray' }
    }
    return @{ Text = "WARNING: the every-minute trigger runs on local time (from $Boundary). On the night the clocks go back it may pause for an hour, and Hive will page 'watcher down'."; Color = 'Red' }
}

function Enter-TickLock {
    <#
    One tick at a time. The task's 5-minute limit ends wscript.exe, not the
    PowerShell it started, and IgnoreNew then lets the next tick start
    beside a tick that is still running. A named mutex, held until this
    process exits, makes the next one leave instead. $false when another
    tick holds it; $true otherwise (also when the lock cannot be made at
    all: a tick that runs beats one that never does).
    #>
    param([string]$Name = 'Local\STAN_HyStar_watcher_tick')
    $m = $null
    try { $m = New-Object System.Threading.Mutex($false, $Name) }
    catch { return $true }
    $got = $false
    # An exception here is AbandonedMutexException: its last holder died
    # without releasing it, and it is ours now.
    try { $got = $m.WaitOne(0) }
    catch { $got = $true }
    if (-not $got) {
        $m.Dispose()
        return $false
    }
    $script:WatchTickMutex = $m
    return $true
}

function Install-Task {
    <#
    Copies this script to C:\ProgramData\STAN and registers the scheduled
    task. Not elevated: re-launches the installed copy elevated with
    -InstallOnly (one administrator prompt) and checks the task appeared.
    Elevated: prepares the state folder (R16), then registers the task to
    run every minute as the desktop's user, not elevated, and only while
    that user is logged on (HyStar needs the session anyway). Returns
    $true when the task exists afterwards.
    #>
    # Copy to a stable path first. A task registered against wherever the
    # .bat was double-clicked from (Downloads, a USB stick, a share) works
    # today and silently stops the day that folder moves.
    if (-not (Test-Path -LiteralPath $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }
    if ($PSCommandPath -ne $InstalledPath) {
        try { Copy-Item -LiteralPath $PSCommandPath -Destination $InstalledPath -Force -ErrorAction Stop }
        catch {
            Say "Could not copy the watcher to $InstalledPath ($($_.Exception.Message))." 'Red'
            return $false
        }
    }

    if (-not (Test-Elevated)) {
        Say ''
        Say 'Setting up the every-minute task needs administrator rights.'
        Say 'Say Yes to the Windows prompt that is about to appear.'
        $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $InstalledPath), '-InstallOnly')
        try {
            Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -Wait -ErrorAction Stop
        }
        catch {
            Say ''
            Say 'The administrator prompt was refused or cancelled.' 'Red'
            return $false
        }
        if (Test-Task) { Say "Scheduled task '$TaskName' created: every minute." 'Green'; return $true }
        Say 'The task still is not there after elevating.' 'Red'
        return $false
    }

    $desk = Get-InteractiveUser
    if ($null -eq $desk) {
        $desk = @{ Name = (Get-CurrentAccount); Sid = '' }
        Say "Could not tell whose desktop this is; the task will run as $($desk.Name)." 'Yellow'
    }
    $prep = Initialize-StateDir -StateDir $WatchStateDir -Account $desk
    if (-not $prep.Ok) {
        Say "Could not give $($desk.Name) write access to $WatchStateDir ($($prep.Why))." 'Red'
        Say 'Without it the every-minute task could not keep its state, so it was not created.' 'Red'
        return $false
    }
    # Runs as the desktop's user ON PURPOSE, never SYSTEM and never elevated:
    # SYSTEM cannot see the mapped Y: drive or read the user's DPAPI secret,
    # and an elevated run would own state files the next run cannot replace.
    # Ruling R17: no window, ever. powershell.exe -WindowStyle Hidden still
    # flashes a console (and can take focus from HyStar) 1,440 times a day,
    # so the task starts wscript.exe on a .vbs shim that runs the tick with
    # no window at all.
    $shimPath = Join-Path $InstallDir 'watch_hystar_errors.vbs'
    $launch = Get-TaskLaunch -ScriptPath $InstalledPath -ShimPath $shimPath
    if ($null -ne $launch.Shim) {
        try { [System.IO.File]::WriteAllText($shimPath, $launch.Shim, [System.Text.Encoding]::ASCII) }
        catch {
            $launch = Get-TaskLaunch -ScriptPath $InstalledPath -ShimPath $shimPath -Fallback "could not write $shimPath ($($_.Exception.GetType().Name))"
        }
    }
    if (-not [string]::IsNullOrEmpty($launch.Why)) {
        Say "WARNING: no hidden launcher ($($launch.Why)). The task starts powershell.exe itself, so a console window may flash every minute." 'Red'
    }
    $action = New-ScheduledTaskAction -Execute $launch.Execute -Argument $launch.Argument
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $desk.Name
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    # Ruling R21: a UTC start (Z), so the hour repeated when the clocks go
    # back cannot pause the ticks. The local boundary is kept: if Task
    # Scheduler refuses the UTC one, the task is registered with it instead,
    # with a warning (and Invoke-Install reads the trigger back either way).
    $localStart = $repeat.StartBoundary
    $repeat.StartBoundary = Get-UtcStartBoundary -Now (Get-Date)
    # IgnoreNew: a slow tick is skipped, never stacked. 5 min: a tick is a
    # stat, a seek and a few KB; one that runs longer is stuck. The shim
    # waits for the tick, so both see it running; but the limit ends
    # wscript.exe, not the PowerShell it started, so the tick also takes a
    # lock (Enter-TickLock) and a still-running one is never joined by the
    # next. A tick that hangs writes no heartbeat: Hive pages after 10 min.
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId $desk.Name -LogonType Interactive -RunLevel Limited
    $registered = $false
    $why = ''
    foreach ($attempt in @('utc', 'local')) {
        if ($attempt -eq 'local') {
            Say "WARNING: Task Scheduler refused a UTC start for the every-minute trigger ($why). Registering it on local time instead: on the night the clocks go back it may pause for an hour." 'Red'
            $repeat.StartBoundary = $localStart
        }
        try {
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $repeat) `
                -Settings $settings -Principal $principal -Force -ErrorAction Stop | Out-Null
            $registered = $true
            break
        }
        catch { $why = $_.Exception.Message }
    }
    if (-not $registered) {
        Say ''
        Say "Could not create the scheduled task: $why" 'Red'
        return $false
    }
    # Never claim success without looking.
    if (-not (Test-Task)) { Say 'The task did not appear after registering.' 'Red'; return $false }
    Say "Scheduled task '$TaskName' created: every minute, as $($desk.Name)." 'Green'
    return $true
}

function Get-LogbookCandidates {
    # Where the install looks for HyStar's error logbook, in order: the path
    # given (or remembered in config.json), then C:, then D:. No duplicates.
    param([string]$LogbookPath = "")
    $list = New-Object System.Collections.ArrayList
    foreach ($c in @($LogbookPath,
            'C:\BDalSystemData\HyStar\LogFiles\CompassHyStarErrorLogbook.log',
            'D:\BDalSystemData\HyStar\LogFiles\CompassHyStarErrorLogbook.log')) {
        if ([string]::IsNullOrEmpty($c)) { continue }
        $dup = $false
        foreach ($have in $list) {
            if ([string]::Equals([string]$have, $c, [System.StringComparison]::OrdinalIgnoreCase)) { $dup = $true }
        }
        if (-not $dup) { [void]$list.Add($c) }
    }
    return $list.ToArray()
}

function Find-ErrorLogbook {
    # The first candidate (Get-LogbookCandidates) that is a file, or $null.
    # Install only: the scheduled tick reads the path from config.json and
    # never searches.
    param([string]$LogbookPath = "")
    foreach ($c in @(Get-LogbookCandidates -LogbookPath $LogbookPath)) {
        if ([System.IO.File]::Exists($c)) { return $c }
    }
    return $null
}

function Test-SlackMemberId {
    # The rule Hive applies (stan.notify._MENTION_ID), case and all: a member
    # ID is what makes Slack notify the phone.
    param([AllowNull()][AllowEmptyString()][string]$MemberId)
    if ([string]::IsNullOrEmpty($MemberId)) { return $false }
    return ($MemberId -cmatch '^[UW][A-Z0-9]{6,}$')
}

function Test-WebhookUrl {
    # A Slack incoming webhook: https://hooks.slack.com/ and something after
    # it, no whitespace. Anything else is refused, so a mistyped paste is
    # caught at install, not at the first failure.
    param([AllowNull()][AllowEmptyString()][string]$Url)
    if ([string]::IsNullOrEmpty($Url)) { return $false }
    if ($Url -match '\s') { return $false }
    $prefix = 'https://hooks.slack.com/'
    if ($Url.Length -le $prefix.Length) { return $false }
    return $Url.StartsWith($prefix, [System.StringComparison]::Ordinal)
}

function Get-PlainText {
    # SecureString -> string, through a BSTR that is zeroed and freed
    # whatever happens. "" for $null.
    param($Secure)
    if ($null -eq $Secure) { return "" }
    $bstr = [IntPtr]::Zero
    try {
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
        return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        if ($bstr -ne [IntPtr]::Zero) { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
}

function Test-WebhookSecure {
    # Test-WebhookUrl on a SecureString (what Read-Host -AsSecureString gave).
    param($Secure)
    return (Test-WebhookUrl ((Get-PlainText $Secure).Trim()))
}

function Set-SecretFileAcl {
    # Path readable by the current user (who stored it, and who alone can
    # decrypt it) and Administrators, nobody else: inheritance cut, every
    # inherited entry dropped. Throws when it cannot be set.
    param([Parameter(Mandatory = $true)][string]$Path)
    $self = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $sec = New-Object System.Security.AccessControl.FileSecurity
    $sec.SetAccessRuleProtection($true, $false)
    foreach ($who in @($self, $admins)) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($who,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow)
        $sec.AddAccessRule($rule)
    }
    (New-Object System.IO.FileInfo $Path).SetAccessControl($sec)
}

function Save-Webhook {
    <#
    Stores the webhook in StateDir\webhook.dpapi: ConvertFrom-SecureString
    with no key is DPAPI in the CurrentUser scope, so only this Windows
    account on this PC can decrypt it (the task runs as this account). The
    file's ACL then admits this account and Administrators only. Refused,
    with nothing written, unless it starts with https://hooks.slack.com/; a
    file whose ACL cannot be set is removed again. Returns @{ Ok; Why }; Why
    never carries the URL.
    #>
    param(
        [Parameter(Mandatory = $true)][System.Security.SecureString]$Webhook,
        [Parameter(Mandatory = $true)][string]$StateDir
    )
    $plain = (Get-PlainText $Webhook).Trim()
    if (-not (Test-WebhookUrl $plain)) {
        return @{ Ok = $false; Why = 'not a Slack incoming webhook: it must start with https://hooks.slack.com/' }
    }
    $path = Join-Path $StateDir 'webhook.dpapi'
    try {
        [void][System.IO.Directory]::CreateDirectory($StateDir)
        $secure = ConvertTo-SecureString -String $plain -AsPlainText -Force
        $blob = ConvertFrom-SecureString -SecureString $secure
        if ([System.IO.File]::Exists($path)) { [System.IO.File]::Delete($path) }
        Write-Utf8File -Path $path -Text $blob
        Set-SecretFileAcl -Path $path
        return @{ Ok = $true; Why = '' }
    }
    catch {
        $why = Get-ScrubbedText -Text "$($_.Exception.GetType().Name): $($_.Exception.Message)" -Secret $plain
        try {
            if ([System.IO.File]::Exists($path)) { [System.IO.File]::Delete($path) }
        }
        catch { }
        return @{ Ok = $false; Why = $why }
    }
}

function Get-Webhook {
    <#
    The stored webhook, decrypted, or $null: none stored, stored by another
    account (DPAPI, CurrentUser: only the account that stored it can read
    it), unreadable, or not a Slack webhook. Never throws, never logs it.
    #>
    param([Parameter(Mandatory = $true)][string]$StateDir)
    try {
        $path = Join-Path $StateDir 'webhook.dpapi'
        if (-not [System.IO.File]::Exists($path)) { return $null }
        $blob = ([System.IO.File]::ReadAllText($path)).Trim()
        if ($blob -eq '') { return $null }
        $secure = ConvertTo-SecureString -String $blob -ErrorAction Stop
        $url = (Get-PlainText $secure).Trim()
        if (Test-WebhookUrl $url) { return $url }
    }
    catch { }
    return $null
}

function Save-WatchConfig {
    <#
    config.json in StateDir (non-secret, never the webhook): logbook and
    memberId as the install found them. shareDir, procedureRoot and
    hiveLogsDir are kept when config.json already sets them, else written as
    null, so Get-WatchConfig keeps choosing Y:\STAN or its UNC path on every
    tick rather than freezing whichever one answered during the install.
    UTF-8 without a BOM, temp file and rename.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][string]$LogbookPath,
        [Parameter(Mandatory = $true)][string]$MemberId
    )
    $stored = Get-WatchConfig -StateDir $StateDir -StoredOnly
    $doc = [ordered]@{
        logbook       = $LogbookPath
        memberId      = $MemberId
        shareDir      = $null
        procedureRoot = $null
        hiveLogsDir   = $null
    }
    foreach ($k in @('shareDir', 'procedureRoot', 'hiveLogsDir')) {
        if ($stored.ContainsKey($k)) { $doc[$k] = $stored[$k] }
    }
    [void][System.IO.Directory]::CreateDirectory($StateDir)
    [void](Publish-JsonFile -Dir $StateDir -BaseName 'config' -Text (ConvertTo-Json -InputObject $doc -Depth 10))
}

# ------------------------------------------------------------------- pause
# Planned downtime (service visit, holiday, planned shutdown): Hive does not
# page "watcher down" until pause_until.json says so. The watcher never reads
# that file, so a pause never silences a queue-stop alert.

function ConvertTo-PauseUntil {
    <#
    '8h' (hours) or '2d' (days) after Now, up to 31 days; $null for anything
    else. Hours are real elapsed hours (counted in UTC, so 8h across a DST
    change is still 8 hours); days are calendar days (the same clock time).
    '0h' ends a pause now.
    #>
    param(
        [AllowNull()][AllowEmptyString()][string]$Text,
        [Parameter(Mandatory = $true)][datetime]$Now
    )
    if ([string]::IsNullOrEmpty($Text)) { return $null }
    $m = [regex]::Match($Text.Trim(), '^(\d{1,4})([hHdD])$')
    if (-not $m.Success) { return $null }
    $n = [int]$m.Groups[1].Value
    if ($m.Groups[2].Value.ToLowerInvariant() -eq 'd') {
        if ($n -gt 31) { return $null }
        return $Now.AddDays($n)
    }
    if ($n -gt 744) { return $null }
    return $Now.ToUniversalTime().AddHours($n).ToLocalTime()
}

function Write-PauseFile {
    <#
    pause_until.json in the share's event folder (ShareDir; by default the
    one Get-WatchConfig resolves, Y:\STAN or its UNC path):
    {until, by, reason}, until in instrument-local naive ISO, UTF-8 without
    a BOM (R14), temp file and rename. Replaces an earlier pause. Returns its
    path; throws when the share cannot be written.
    #>
    param(
        [Parameter(Mandatory = $true)][datetime]$Until,
        [string]$ShareDir = "",
        [string]$By = "",
        [string]$Reason = ""
    )
    $dir = $ShareDir
    if ([string]::IsNullOrEmpty($dir)) {
        $dir = (Get-WatchConfig -StateDir (Join-Path $env:ProgramData 'STAN\hystar_watch')).shareDir
    }
    [void][System.IO.Directory]::CreateDirectory($dir)
    $doc = [ordered]@{
        until  = (ConvertTo-IsoLocal $Until)
        by     = $By
        reason = $Reason
    }
    [void](Publish-JsonFile -Dir $dir -BaseName 'pause_until' -Text (ConvertTo-Json -InputObject $doc -Depth 10))
    return (Join-Path $dir 'pause_until.json')
}

function Set-WatcherPause {
    # -Pause: Duration against Now, then pause_until.json. Returns the end,
    # or $null (nothing written) for a duration it does not understand.
    # Throws when the share cannot be written.
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Duration,
        [Parameter(Mandatory = $true)][datetime]$Now,
        [string]$ShareDir = "",
        [string]$By = "",
        [string]$Reason = ""
    )
    $until = ConvertTo-PauseUntil -Text $Duration -Now $Now
    if ($null -eq $until) { return $null }
    [void](Write-PauseFile -Until $until -ShareDir $ShareDir -By $By -Reason $Reason)
    return $until
}

# -------------------------------------------------------------- auto-logon

function ConvertTo-AutoLogonStatus {
    # Winlogon's AutoAdminLogon value: '1' on; absent or '0' off; else unknown.
    param($Value)
    if ($null -eq $Value) { return 'off' }
    $t = ([string]$Value).Trim()
    if ($t -eq '1') { return 'on' }
    if ($t -eq '0' -or $t -eq '') { return 'off' }
    return 'unknown'
}

function Get-AutoLogonStatus {
    # 'on', 'off' or 'unknown': whether the PC logs a user on by itself after
    # a reboot. The task runs only while its user is logged on.
    param([string]$RegistryPath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon')
    try {
        if (-not (Test-Path -LiteralPath $RegistryPath)) { return 'unknown' }
        $item = Get-ItemProperty -LiteralPath $RegistryPath -ErrorAction Stop
        $v = $null
        $prop = $item.PSObject.Properties['AutoAdminLogon']
        if ($null -ne $prop) { $v = $prop.Value }
        return (ConvertTo-AutoLogonStatus $v)
    }
    catch { return 'unknown' }
}

# ---------------------------------------------------------- the test message

function Format-TestPayload {
    param(
        [string]$MemberId = "",
        [Alias('Host')][string]$HostName = 'TIMS-10878',
        [string]$Version = ""
    )
    $text = "$(Get-MentionPrefix $MemberId)STAN HyStar watcher v$Version is installed on $HostName. If your phone buzzed, queue-stop alerts will reach you."
    return @{ text = $text; blocks = @(, (New-SectionBlock $text)) }
}

function Send-InstallTest {
    <#
    The test message with the @mention, so "does my phone buzz?" is answered
    on day one. Posted directly when it can be (Sender, else WebhookUrl);
    otherwise it becomes a 'test' event with posted_by_pc false, which Hive
    relays like any other, through the tick's own local queue and flush.
    Touches neither state.json nor the delivered-ids ledger (R16: the tick's
    first run seeds those, as the task's account).
    Returns @{ Route = 'direct' | 'hive' | 'queued' | 'lost'; Status; Lines }.
    #>
    param(
        [string]$WebhookUrl = "",
        [string]$MemberId = "",
        [Parameter(Mandatory = $true)][string]$HostName,
        [string]$ShareDir = "",
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][datetime]$Now,
        [string]$Version = "",
        [scriptblock]$Sender = $null
    )
    $payload = Format-TestPayload -MemberId $MemberId -HostName $HostName -Version $Version
    $status = 'no webhook stored'
    $res = $null
    if ($null -ne $Sender) { $res = Invoke-Sender -Sender $Sender -Payload $payload }
    elseif (-not [string]::IsNullOrEmpty($WebhookUrl)) { $res = Send-SlackPayload -WebhookUrl $WebhookUrl -Payload $payload }
    if ($null -ne $res) {
        if ($res.Ok -eq $true) { return @{ Route = 'direct'; Status = $res.Status; Lines = @() } }
        $status = Get-ScrubbedText -Text ([string]$res.Status) -Secret $WebhookUrl
    }
    $log = New-Object System.Collections.ArrayList
    $ev = New-WatchEvent -State @{ eventSeq = 0 } -Type 'test' -HostName $HostName -At $Now -Source 'watcher' `
        -Title 'Install test' -Message $payload.text -Payload $payload -Version $Version
    $stamp = $Now.ToString("yyyyMMdd'T'HHmmss", [System.Globalization.CultureInfo]::InvariantCulture)
    $ev['id'] = "$($HostName)_$($stamp)_test"
    $ev['posted_by_pc'] = $false
    $text = ConvertTo-Json -InputObject $ev -Depth 10
    $filed = Save-EventFile -Id $ev['id'] -Text $text -ShareDir $ShareDir -StateDir $StateDir -Log $log
    $route = 'lost'
    if ($filed -eq 'written' -or $filed -eq 'exists') { $route = 'hive' }
    elseif ($filed -eq 'queued') {
        $fl = Invoke-PendingFlush -ShareDir $ShareDir -StateDir $StateDir -Log $log
        if ($fl.Left -eq 0) { $route = 'hive' }
        else { $route = 'queued' }
    }
    $lines = @()
    foreach ($l in $log) { $lines += (Get-ScrubbedText -Text ([string]$l) -Secret $WebhookUrl) }
    return @{ Route = $route; Status = $status; Lines = $lines }
}

function Get-TestRouteText {
    param([Parameter(Mandatory = $true)]$Result)
    $r = [string]$Result.Route
    if ($r -eq 'direct') { return 'direct' }
    if ($r -eq 'hive') { return 'via Hive (the PC could not reach Slack)' }
    if ($r -eq 'queued') {
        return 'via Hive once the share is reachable (the PC could not reach Slack or the share: it is queued on this PC)'
    }
    return 'NOT SENT: Slack, the share and the queue on this PC all failed'
}

function Show-TestResult {
    param([Parameter(Mandatory = $true)]$Result)
    $color = 'Green'
    if ($Result.Route -ne 'direct') { $color = 'Yellow' }
    if ($Result.Route -eq 'lost') { $color = 'Red' }
    Say ''
    Say "Test message route: $(Get-TestRouteText $Result)" $color
    if ($Result.Route -ne 'direct') {
        Say "  direct post: $($Result.Status)"
        foreach ($l in @($Result.Lines)) { Say "  $l" }
        if ($Result.Route -eq 'hive') { Say '  Hive posts it within a minute or two.' }
    }
    Say ''
    Say 'Your phone should buzz with an @mention. If it does not, check once:'
    Say '  * Slack on the phone: notifications on for mentions'
    Say "  * Slack's notification schedule does not silence nights"
    Say "  * the phone's sleep / Do Not Disturb mode lets Slack through"
}

# --------------------------------------------------------------- questions
# The only two places this script waits for a person. Never reached under
# -Scheduled.

function Read-MemberIdInput {
    # Asks for the Slack member ID. Enter keeps Stored.
    param([string]$Stored = "")
    Say ''
    Say 'Your Slack member ID: in Slack, open your profile, click the three dots, then "Copy member ID".'
    $prompt = 'Slack member ID'
    if (-not [string]::IsNullOrEmpty($Stored)) { $prompt = "Slack member ID (Enter keeps $Stored)" }
    $answer = Read-Host -Prompt $prompt
    if ([string]::IsNullOrEmpty($answer)) { return $Stored }
    return $answer.Trim()
}

function Read-WebhookInput {
    # Asks for the webhook without echoing it. An empty answer keeps the
    # stored one (when HaveStored).
    param([switch]$HaveStored)
    Say ''
    Say 'Paste the Slack incoming webhook (it starts with https://hooks.slack.com/).'
    Say 'It is not shown as you paste, and it is never logged or copied to the share.'
    if ($HaveStored) { Say 'One is stored already: press Enter to keep it.' }
    return (Read-Host -Prompt 'Slack webhook' -AsSecureString)
}

# ------------------------------------------------------------- the install

function Invoke-Install {
    <#
    The install, run by a person from the .bat. In order, and nothing is
    registered or stored until every answer has checked out:
      1. find the logbook (-LogbookPath, else the one config.json remembers,
         then C:, then D:); none: print every path tried and stop
      2. this window must be the desktop's user's: the webhook is encrypted
         for whoever stores it, and the task runs as the desktop's user
      3. the Slack member ID (-SlackMemberId, else asked; Enter keeps the
         stored one), checked as Hive checks it
      4. the webhook (asked, never shown; Enter keeps the stored one); it
         must start with https://hooks.slack.com/
      5. the scheduled task (Install-Task, one administrator prompt), unless
         TaskReady (installed and up to date already)
      6. config.json and the webhook, stored as the desktop's user
      7. the account the task runs as (R16: warned when it is not the
         desktop's), auto-logon, then the test message and its route
    Never creates state.json or delivered.txt (R16). Returns $true when the
    watcher is installed.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$StateDir,
        [string]$LogbookPath = "",
        [string]$SlackMemberId = "",
        [switch]$TaskReady
    )
    $stored = Get-WatchConfig -StateDir $StateDir -StoredOnly

    $hint = $LogbookPath
    if ([string]::IsNullOrEmpty($hint) -and $stored.ContainsKey('logbook')) { $hint = [string]$stored['logbook'] }
    $logbook = Find-ErrorLogbook -LogbookPath $hint
    if ($null -eq $logbook) {
        Say "Could not find HyStar's error logbook. Tried:" 'Red'
        foreach ($c in @(Get-LogbookCandidates -LogbookPath $hint)) { Say "  $c" 'Red' }
        Say 'Nothing was installed. If HyStar writes it somewhere else, run:' 'Yellow'
        Say '  powershell -ExecutionPolicy Bypass -File watch_hystar_errors.ps1 -LogbookPath <path to CompassHyStarErrorLogbook.log>' 'Yellow'
        return $false
    }
    Say "HyStar error logbook: $logbook" 'Green'

    $me = Get-CurrentAccount
    $desk = Get-InteractiveUser
    $deskName = $me
    if ($null -ne $desk) { $deskName = [string]$desk.Name }
    if (-not (Test-SameAccount $me $deskName)) {
        Say "This window runs as $me, but this desktop belongs to $deskName." 'Red'
        Say "The watcher runs as $deskName, and only the account that stores the webhook can read it." 'Red'
        Say "Install it as ${deskName}: double-click the .bat as that user and say Yes to the prompt" 'Red'
        Say "(not 'Run as administrator' with another account). Nothing was installed." 'Red'
        return $false
    }
    if ($null -eq $desk) { Say "Could not tell whose desktop this is; installing for $me." 'Yellow' }

    $member = ""
    if (-not [string]::IsNullOrEmpty($SlackMemberId)) {
        $member = $SlackMemberId.Trim()
        if (-not (Test-SlackMemberId $member)) {
            Say "'$member' is not a Slack member ID (one looks like U0123ABCD). Nothing was installed." 'Red'
            return $false
        }
    }
    else {
        $storedMember = ""
        if ($stored.ContainsKey('memberId')) { $storedMember = [string]$stored['memberId'] }
        for ($try = 1; $try -le 3; $try++) {
            $answer = Read-MemberIdInput -Stored $storedMember
            if (Test-SlackMemberId $answer) { $member = $answer; break }
            Say "'$answer' is not a Slack member ID (one looks like U0123ABCD)." 'Red'
        }
        if ([string]::IsNullOrEmpty($member)) {
            Say 'No Slack member ID: nothing was installed.' 'Red'
            return $false
        }
    }

    $haveStored = ($null -ne (Get-Webhook -StateDir $StateDir))
    $newHook = $null
    $hookOk = $false
    for ($try = 1; $try -le 3; $try++) {
        $sec = Read-WebhookInput -HaveStored:$haveStored
        if ($null -eq $sec -or $sec.Length -eq 0) {
            if ($haveStored) { $hookOk = $true; break }
            Say 'Nothing was pasted.' 'Red'
            continue
        }
        if (Test-WebhookSecure $sec) { $newHook = $sec; $hookOk = $true; break }
        Say 'That is not a Slack incoming webhook: it must start with https://hooks.slack.com/' 'Red'
    }
    if (-not $hookOk) {
        Say 'No webhook: nothing was installed.' 'Red'
        return $false
    }

    if (-not $TaskReady) {
        if (-not (Install-Task)) {
            Say 'The scheduled task was not created, so nothing was stored.' 'Red'
            return $false
        }
    }

    try { Save-WatchConfig -StateDir $StateDir -LogbookPath $logbook -MemberId $member }
    catch {
        Say "Could not write config.json in $StateDir ($($_.Exception.GetType().Name): $($_.Exception.Message))." 'Red'
        return $false
    }
    Say "Settings saved in $(Join-Path $StateDir 'config.json') (member ID $member)."
    if ($null -ne $newHook) {
        $saved = Save-Webhook -Webhook $newHook -StateDir $StateDir
        if ($saved.Ok) { Say "Webhook stored, encrypted for $me and readable only by it and Administrators." 'Green' }
        else {
            Say "The webhook was NOT stored: $($saved.Why)" 'Red'
            Say 'Every alert will reach Slack through Hive instead, up to a minute later.' 'Yellow'
        }
    }

    Say ''
    $taskUser = Get-TaskAccount
    if (-not [string]::IsNullOrEmpty($taskUser)) {
        Say "The watcher runs as: $taskUser (every minute, while that account is logged on)."
    }
    $warn = Get-AccountWarning -TaskAccount $taskUser -Desktop $deskName
    if (-not [string]::IsNullOrEmpty($warn)) {
        $warnColor = 'Yellow'
        if ($warn.StartsWith('WARNING')) { $warnColor = 'Red' }
        Say $warn $warnColor
    }
    # R17, read back from the registered task: the elevated window that chose
    # the launcher has closed, and its warning with it.
    $exe = Get-TaskExecute
    if ($exe -like '*wscript.exe') { Say 'Each tick starts through wscript.exe: no window appears.' }
    elseif (-not [string]::IsNullOrEmpty($exe)) {
        Say "WARNING: each tick starts $exe itself (no wscript.exe or VBScript here), so a console window may flash every minute." 'Red'
    }
    # R21, read back the same way: UTC, or the local fallback with a warning.
    $trig = Get-TriggerReadback -Boundary (Get-TaskRepeatBoundary)
    Say $trig.Text $trig.Color
    $auto = Get-AutoLogonStatus
    Say "Auto-logon after a reboot: $auto"
    if ($auto -eq 'off') {
        Say "  After a reboot nobody is logged on, so the watcher waits until someone logs on; Hive pages 'watcher down' after 10 minutes." 'Yellow'
    }
    elseif ($auto -eq 'unknown') { Say '  (Winlogon could not be read.)' 'Yellow' }

    $cfg = Get-WatchConfig -StateDir $StateDir
    $url = Get-Webhook -StateDir $StateDir
    if ($null -eq $url) { Say "No webhook $me can read is stored: the test goes through Hive." 'Yellow' }
    $result = Send-InstallTest -WebhookUrl ([string]$url) -MemberId $member -HostName ([string]$env:COMPUTERNAME) `
        -ShareDir $cfg['shareDir'] -StateDir $StateDir -Now (Get-Date) -Version ([string]$ScriptVersion)
    Show-TestResult $result
    return $true
}

function Invoke-PauseCommand {
    # -Pause 8h: write the pause, say when it ends. Returns the exit code.
    param(
        [Parameter(Mandatory = $true)][string]$Duration,
        [Parameter(Mandatory = $true)][string]$StateDir
    )
    $cfg = Get-WatchConfig -StateDir $StateDir
    $now = Get-Date
    $until = $null
    try {
        $until = Set-WatcherPause -Duration $Duration -Now $now -ShareDir $cfg['shareDir'] -By (Get-CurrentAccount) `
            -Reason "planned downtime: -Pause $Duration on $env:COMPUTERNAME"
    }
    catch {
        Say "Could not write the pause to $($cfg['shareDir']) ($($_.Exception.GetType().Name): $($_.Exception.Message))." 'Red'
        return 1
    }
    if ($null -eq $until) {
        Say "'$Duration' is not a pause length. Use hours or days: 8h, 2d (up to 31d; 0h ends a pause)." 'Red'
        return 1
    }
    if ($until -le $now) { Say "Pause ended: Hive pages 'watcher down' again from now on." 'Green' }
    else {
        $when = $until.ToString('ddd yyyy-MM-dd HH:mm', [System.Globalization.CultureInfo]::InvariantCulture)
        Say "Paused until $when. Hive will not page 'watcher down' before then." 'Green'
    }
    Say 'Queue-stop alerts still go out as usual: the watcher itself never pauses.'
    Say "  ($(Join-Path $cfg['shareDir'] 'pause_until.json'))"
    return 0
}

function Invoke-TestCommand {
    # -Test: the install test message again. Returns the exit code.
    param([Parameter(Mandatory = $true)][string]$StateDir)
    $cfg = Get-WatchConfig -StateDir $StateDir
    if (-not (Test-SlackMemberId $cfg['memberId'])) {
        Say 'No Slack member ID is stored: install first (double-click watch_hystar_errors.bat).' 'Red'
        return 1
    }
    $url = Get-Webhook -StateDir $StateDir
    if ($null -eq $url) { Say "No webhook $(Get-CurrentAccount) can read is stored: the test goes through Hive." 'Yellow' }
    $result = Send-InstallTest -WebhookUrl ([string]$url) -MemberId $cfg['memberId'] -HostName ([string]$env:COMPUTERNAME) `
        -ShareDir $cfg['shareDir'] -StateDir $StateDir -Now (Get-Date) -Version ([string]$ScriptVersion)
    Show-TestResult $result
    if ($result.Route -eq 'lost') { return 1 }
    return 0
}

# ----------------------------------------------------------------- top level
$TaskName = 'STAN HyStar watcher'
$InstallDir = Join-Path $env:ProgramData 'STAN'
$InstalledPath = Join-Path $InstallDir 'watch_hystar_errors.ps1'
$WatchStateDir = Join-Path $InstallDir 'hystar_watch'
$InstalledShim = Join-Path $InstallDir 'watch_hystar_errors.vbs'

# THE TICK, first and on its own: a scheduled run never reaches a question,
# the install or the key-press below. No GetNewClosure for the sender: a
# closure would not see this script's functions.
if ($Scheduled) {
    # A tick still running (one the task's time limit could not end) is
    # never joined by the next: that one leaves at once.
    if (-not (Enter-TickLock)) {
        Write-WatchLog -StateDir $WatchStateDir -Now (Get-Date) -Lines @(, "watch_hystar_errors v$($ScriptVersion): skipped, the previous tick is still running")
        exit 0
    }
    $watchConfig = Get-WatchConfig -StateDir $WatchStateDir
    # $null when none is stored (or this account cannot decrypt it): then
    # every event carries posted_by_pc false and Hive relays it.
    $script:WatchWebhook = Get-Webhook -StateDir $WatchStateDir
    $watchSender = $null
    if (-not [string]::IsNullOrEmpty($script:WatchWebhook)) {
        $watchSender = { param($p) Send-SlackPayload -WebhookUrl $script:WatchWebhook -Payload $p }
    }
    $null = Invoke-WatchTick -StateDir $WatchStateDir -LogbookPath $watchConfig['logbook'] `
        -ShareDir $watchConfig['shareDir'] -ProcedureRoot $watchConfig['procedureRoot'] `
        -HiveLogsDir $watchConfig['hiveLogsDir'] -Now (Get-Date) -Sender $watchSender `
        -MemberId $watchConfig['memberId']
    exit 0
}

Say "STAN HyStar watcher  v$ScriptVersion" 'Cyan'
Say "  running from: $PSCommandPath"
Say ''

if ($Uninstall) {
    $removed = Remove-Task
    Say ''
    Say "Left in place: $WatchStateDir (state, config.json and the stored webhook)."
    Say 'Delete that folder as well to remove them.'
    if ($removed) { Say "Hive pages 'watcher down' in about 10 minutes unless it was paused first (/pause)." 'Yellow' }
    Pause-IfInteractive
    if ($removed) { exit 0 }
    exit 1
}
if ($InstallOnly) {
    $ok = Install-Task
    if ($ok) { exit 0 }
    # This elevated window closes on exit: keep the reason on screen.
    Pause-IfInteractive
    exit 1
}
if (-not [string]::IsNullOrEmpty($Pause)) {
    $code = Invoke-PauseCommand -Duration $Pause -StateDir $WatchStateDir
    Pause-IfInteractive
    exit $code
}
if ($Test) {
    $code = Invoke-TestCommand -StateDir $WatchStateDir
    Pause-IfInteractive
    exit $code
}

# THE INSTALL. The task is registered only when it is absent, older than
# this script, or not started the way this script would start it (the R17
# launcher, and the shim on disk): the version alone would leave a task
# registered without the shim at this version as it is. A re-run of an
# up-to-date install re-checks the settings (Enter keeps each) and sends the
# test again, without touching the task.
$installedVersion = Get-InstalledVersion
$ok = $false
if (Test-Task) {
    $launchOk = Test-TaskLaunchCurrent -RegisteredExecute (Get-TaskExecute) `
        -Launch (Get-TaskLaunch -ScriptPath $InstalledPath -ShimPath $InstalledShim) -ShimPath $InstalledShim
    if ($installedVersion -eq $ScriptVersion -and $launchOk) {
        Say "The watcher is installed and up to date (v$ScriptVersion, every minute)." 'Green'
        Say 'Checking its settings (Enter keeps each one), then the test message.'
        $ok = Invoke-Install -StateDir $WatchStateDir -LogbookPath $LogbookPath -SlackMemberId $SlackMemberId -TaskReady
    }
    elseif ($installedVersion -eq $ScriptVersion) {
        Say "The watcher is v$ScriptVersion, but its task does not start the way this version starts it -- registering it again." 'Yellow'
        $ok = Invoke-Install -StateDir $WatchStateDir -LogbookPath $LogbookPath -SlackMemberId $SlackMemberId
    }
    else {
        # The task runs the copy under ProgramData, taken when it was
        # registered: updating the .bat's folder changes nothing that runs
        # until the installed copy is refreshed and re-registered.
        $was = $installedVersion
        if (-not $was) { $was = 'unknown' }
        Say "The watcher is at v$was; this is v$ScriptVersion -- updating it." 'Yellow'
        $ok = Invoke-Install -StateDir $WatchStateDir -LogbookPath $LogbookPath -SlackMemberId $SlackMemberId
    }
}
else {
    Say 'No watcher installed -- setting it up.' 'Yellow'
    $ok = Invoke-Install -StateDir $WatchStateDir -LogbookPath $LogbookPath -SlackMemberId $SlackMemberId
}
Say ''
Say 'Also: watch_hystar_errors.bat /test, /pause 8h, /uninstall'
Pause-IfInteractive
if ($ok) { exit 0 }
exit 1
