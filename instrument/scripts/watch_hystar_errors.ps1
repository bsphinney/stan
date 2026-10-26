<#
.SYNOPSIS
    Read-only watcher for HyStar's error logbook (CompassHyStarErrorLogbook.log).

.DESCRIPTION
    Runs once a minute on the timsTOF instrument PC (PowerShell 5.1), tails the
    logbook and alerts on every EmergencyShutdown, the event that stops the
    acquisition queue.

    THIS FILE IS BUILT IN PARTS. Present: reading and parsing the logbook,
    deciding and formatting alerts, and the tick (state, rotation, the Slack
    post, event files and heartbeat on the share, watching Hive's alerting).
    Under -Scheduled the top level runs one tick. Still to come: the install
    surface (scheduled task, config, the stored webhook, -Pause, -Test).

    Log format, as measured on real files (see the design spec, section 3):
      * UTF-16LE with a BOM (FF FE).
      * Every record starts with a DC3 byte (0x13) and ends with 0x11 CR LF.
      * A message may contain a bare LF (the Evotip text does). Exception
        records are followed by stack lines with no 0x13 prefix.
      * Record kinds by prefix: E: error, S: session start (HyStar restarted),
        H: header/footer, D: debug.
    Read-only: the file is opened with FileShare.ReadWrite and never written.
#>
param(
    [switch]$Scheduled
)

$ScriptVersion = '1.2.22'

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
    $fs = New-Object System.IO.FileStream($Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::ReadWrite)
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

function Get-EvosepVial {
    <#
    ProcedureRoot holds the serial folders (...\Procedure logs). The newest
    <method>_yyyy-MM-dd_HH-mm-ss folder starting within 90 min before At wins;
    its start comes from the folder NAME (no per-folder stat). Only that
    folder's journal.txt is read. Returns e.g. 'S1-A5', or $null.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ProcedureRoot,
        [Parameter(Mandatory = $true)][datetime]$At
    )
    try {
        if (-not [System.IO.Directory]::Exists($ProcedureRoot)) { return $null }
        $best = $null
        $bestStart = [datetime]::MinValue
        $earliest = $At.AddMinutes(-90)
        foreach ($serial in [System.IO.Directory]::GetDirectories($ProcedureRoot)) {
            foreach ($dir in [System.IO.Directory]::GetDirectories($serial)) {
                $name = [System.IO.Path]::GetFileName($dir)
                $m = [regex]::Match($name, '_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$')
                if (-not $m.Success) { continue }
                $start = [datetime]::MinValue
                $ok = [datetime]::TryParseExact($m.Groups[1].Value, 'yyyy-MM-dd_HH-mm-ss',
                    [System.Globalization.CultureInfo]::InvariantCulture,
                    [System.Globalization.DateTimeStyles]::None, [ref]$start)
                if (-not $ok) { continue }
                if ($start -gt $At) { continue }
                if ($start -lt $earliest) { continue }
                if ($best -eq $null -or $start -gt $bestStart) { $best = $dir; $bestStart = $start }
            }
        }
        if ($best -eq $null) { return $null }
        $journal = Join-Path $best 'journal.txt'
        if (-not [System.IO.File]::Exists($journal)) { return $null }
        $fs = New-Object System.IO.FileStream($journal,
            [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
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
    wrong sample is not).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$LogbookPath,
        [Parameter(Mandatory = $true)][datetime]$At
    )
    try {
        if (-not [System.IO.File]::Exists($LogbookPath)) { return $null }
        $fs = New-Object System.IO.FileStream($LogbookPath,
            [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
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
    param(
        [Parameter(Mandatory = $true)][hashtable]$Event,
        [string]$MemberId = "",
        [Alias('Host')][string]$HostName = 'TIMS-10878'
    )
    $dash = [char]0x2014
    $dot = [char]0x00B7
    $mention = Get-MentionPrefix $MemberId
    $hm = ([datetime]$Event.At).ToString('HH:mm')
    $text = "$mention`:red_circle: $HostName HyStar restarted $dash no shutdown logged $dot $hm"
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
        $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
        $fs = New-Object System.IO.FileStream($Path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, $share)
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
        $patterns = New-Object System.Collections.ArrayList
        if (($to.Date - $from.Date).TotalDays -gt 2) { [void]$patterns.Add('*') }
        else {
            $day = $from.Date
            while ($day -le $to.Date) {
                [void]$patterns.Add("*_$($day.ToString('yyyy-MM-dd', $inv))_*")
                $day = $day.AddDays(1)
            }
        }
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
    Puts one event file on the share; when the share cannot take it, or older
    events are still queued (order matters), queues it in StateDir\pending\
    instead. Returns 'written', 'exists', 'queued' or 'lost' (not even the
    local queue could take it).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Text,
        [string]$ShareDir = "",
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Log
    )
    $pendingDir = Join-Path $StateDir 'pending'
    $queued = @(Get-PendingFiles -PendingDir $pendingDir)
    if ($queued.Count -eq 0 -and -not [string]::IsNullOrEmpty($ShareDir)) {
        try {
            [void][System.IO.Directory]::CreateDirectory($ShareDir)
            return (Publish-JsonFile -Dir $ShareDir -BaseName $Id -Text $Text -NoOverwrite)
        }
        catch {
            [void]$Log.Add("share not writable for $Id ($($_.Exception.GetType().Name): $($_.Exception.Message)); queued")
        }
    }
    try {
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
        [void]$Log.Add("could not queue $Id either ($($_.Exception.GetType().Name): $($_.Exception.Message))")
        return 'lost'
    }
}

function Invoke-PendingFlush {
    <#
    Moves queued event files to the share, oldest first, and stops at the
    first share failure so the share sees them in order. An event already
    there is not rewritten. A queued file that cannot be read here is set
    aside as .bad rather than holding every later event behind it forever.
    Returns how many are still queued.
    #>
    param(
        [string]$ShareDir = "",
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Log
    )
    $files = @(Get-PendingFiles -PendingDir (Join-Path $StateDir 'pending'))
    $left = $files.Count
    if ($left -eq 0 -or [string]::IsNullOrEmpty($ShareDir)) { return $left }
    try { [void][System.IO.Directory]::CreateDirectory($ShareDir) }
    catch {
        [void]$Log.Add("share still unavailable; $left event file(s) queued")
        return $left
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
            [void]$Log.Add("share still unavailable ($($_.Exception.GetType().Name)); $left event file(s) queued")
            break
        }
        $left--
        [void]$Log.Add("queued $id -> share ($r)")
        # On the share now; a copy left here would only be found 'exists'
        # (or, once Hive has moved it, rewritten and recorded as 'already').
        try { [System.IO.File]::Delete($f) }
        catch { [void]$Log.Add("could not remove queued $id ($($_.Exception.GetType().Name))") }
    }
    return $left
}

function Send-WatchEvent {
    <#
    Delivers one event: the direct Slack post first, so posted_by_pc is final
    before the file exists, then the event file. A failed post is not retried
    from here: posted_by_pc false is Hive's cue to relay it. Skip: why no post
    is tried (an earlier post this tick failed).
    Returns @{ Posted; Status; Filed; Tried }.
    #>
    param(
        [Parameter(Mandatory = $true)]$Event,
        [scriptblock]$Sender = $null,
        [string]$Skip = "",
        [string]$ShareDir = "",
        [Parameter(Mandatory = $true)][string]$StateDir,
        [Parameter(Mandatory = $true)][datetime]$Now,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()]$Log
    )
    $posted = $false
    $status = 'no webhook configured'
    if (-not [string]::IsNullOrEmpty($Skip)) { $status = $Skip }
    elseif ($null -ne $Sender) {
        $res = Invoke-Sender -Sender $Sender -Payload $Event['payload']
        $posted = $res.Ok
        $status = $res.Status
    }
    $Event['posted_by_pc'] = $posted
    if ($posted) { $Event['posted_at'] = ConvertTo-IsoLocal $Now }
    $text = ConvertTo-Json -InputObject $Event -Depth 10
    $filed = Save-EventFile -Id $Event['id'] -Text $text -ShareDir $ShareDir -StateDir $StateDir -Log $Log
    $how = "posted ($status)"
    if (-not $posted) { $how = "NOT posted ($status); Hive will relay it" }
    $what = "$($Event['type'])"
    if ($null -ne $Event['title']) { $what = "$what '$($Event['title'])'" }
    [void]$Log.Add("$what at $($Event['at']) id $($Event['id']): $how; file $filed")
    $tried = ($null -ne $Sender -and [string]::IsNullOrEmpty($Skip))
    return @{ Posted = $posted; Status = $status; Filed = $filed; Tried = $tried }
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
        $payload = Format-RestartPayload -Event @{ At = $at } -MemberId $MemberId -Host $HostName
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
    if (-not [string]::IsNullOrEmpty($ProcedureRoot)) { $vial = Get-EvosepVial -ProcedureRoot $ProcedureRoot -At $at }
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
    decides nothing. Returns @{ Kind; LastAt } or $null; updates State.
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
    if ($quiet -and -not $wasQuiet) {
        $State['hiveQuiet'] = $true
        $State['hiveQuietSince'] = $newest
        return @{ Kind = 'hive_quiet'; LastAt = $newest }
    }
    if ($wasQuiet -and -not $quiet) {
        $State['hiveQuiet'] = $false
        $State['hiveQuietSince'] = $null
        return @{ Kind = 'hive_back'; LastAt = $newest }
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

function Invoke-WatchTick {
    <#
    One full tick. Never throws. Returns a summary hashtable
    @{ Seeded; Records; Events; Posted; Filed; Queued; Errors }.

      1. state.json from StateDir (missing or unreadable: start at the end)
      2. queued event files go to the share first, oldest first
      3. new logbook records, through rotation (Get-LogbookRecords)
      4. each record decided (Invoke-RecordAlert); an event is posted through
         Sender (@{ Ok; Status }) and filed on the share or queued
      5. Hive's alerting watched (only with a Sender)
      6. state saved, heartbeat.json written, watch.log appended

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
    try {
        if ([string]::IsNullOrEmpty($Version)) { $Version = [string]$ScriptVersion }
        if ([string]::IsNullOrEmpty($HostName)) { $HostName = [string]$env:COMPUTERNAME }
        if ([string]::IsNullOrEmpty($HostName)) { $HostName = 'TIMS-10878' }
        if ([string]::IsNullOrEmpty($SampleLogPath)) {
            $logDir = [System.IO.Path]::GetDirectoryName($LogbookPath)
            if (-not [string]::IsNullOrEmpty($logDir)) { $SampleLogPath = Join-Path $logDir 'CompassHyStarLogbook.log' }
        }
        [void][System.IO.Directory]::CreateDirectory($StateDir)
        try { $state = Read-WatchState -StateDir $StateDir }
        catch {
            [void]$log.Add("state.json unreadable ($($_.Exception.GetType().Name)); starting again at the end of the logbook")
            $state = $null
        }
        if ($null -eq $state) { $state = ConvertTo-WatchState $null }

        $null = Invoke-PendingFlush -ShareDir $ShareDir -StateDir $StateDir -Log $log

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
                $sent = Send-WatchEvent -Event $ev -Sender $Sender -Skip $skipPost -ShareDir $ShareDir -StateDir $StateDir -Now $Now -Log $log
                $summary['Events'] = $summary['Events'] + 1
                if ($sent.Posted) { $summary['Posted'] = $summary['Posted'] + 1 }
                elseif ($sent.Tried) {
                    $lastError = "Slack post failed: $($sent.Status)"
                    $skipPost = "not tried: a post failed earlier this tick"
                }
                if ($sent.Filed -eq 'queued') { $summary['Queued'] = $summary['Queued'] + 1 }
                elseif ($sent.Filed -ne 'lost') { $summary['Filed'] = $summary['Filed'] + 1 }
            }
            catch {
                $summary['Errors'] = $summary['Errors'] + 1
                $lastError = "record at $(ConvertTo-IsoLocal $rec.At) failed: $($_.Exception.GetType().Name): $($_.Exception.Message)"
                [void]$log.Add($lastError)
            }
        }

        try {
            $note = Get-HiveWatchNote -State $state -HiveLogsDir $HiveLogsDir -Now $Now -Sender $Sender
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
                $sent = Send-WatchEvent -Event $ev -Sender $Sender -Skip $skipPost -ShareDir $ShareDir -StateDir $StateDir -Now $Now -Log $log
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
        if (-not [string]::IsNullOrEmpty($ShareDir)) {
            $offset = $null
            if ($null -ne $state) { $offset = $state['offset'] }
            $errText = $null
            if ($null -ne $lastError) { $errText = Get-ScrubbedText $lastError }
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
    mapped drive is there, else its UNC path, the same folder.
    #>
    param([Parameter(Mandatory = $true)][string]$StateDir)
    $root = '\\128.120.208.42\proteomics-grp\STAN'
    if ([System.IO.Directory]::Exists('Y:\STAN')) { $root = 'Y:\STAN' }
    $cfg = @{
        logbook       = 'C:\BDalSystemData\HyStar\LogFiles\CompassHyStarErrorLogbook.log'
        memberId      = ''
        shareDir      = (Join-Path (Join-Path $root 'hystar_events') ([string]$env:COMPUTERNAME))
        procedureRoot = 'C:\ProgramData\Evosep\EvosepOne\Procedure logs'
        hiveLogsDir   = (Join-Path $root 'logs')
    }
    try {
        $path = Join-Path $StateDir 'config.json'
        if ([System.IO.File]::Exists($path)) {
            $doc = ConvertFrom-Json ([System.IO.File]::ReadAllText($path))
            foreach ($k in @('logbook', 'memberId', 'shareDir', 'procedureRoot', 'hiveLogsDir')) {
                $v = Get-StateField $doc $k
                if (-not [string]::IsNullOrEmpty([string]$v)) { $cfg[$k] = [string]$v }
            }
        }
    }
    catch { }
    return $cfg
}

# ----------------------------------------------------------------- top level
# Without -Scheduled nothing runs yet: the install surface (-Uninstall,
# -Pause, -Test, config.json, the DPAPI-protected webhook) is the next part of
# this script. Under -Scheduled: one tick, then exit.
if (-not $Scheduled) { return }

$watchStateDir = Join-Path $env:ProgramData 'STAN\hystar_watch'
$watchConfig = Get-WatchConfig -StateDir $watchStateDir
# The install part stores the webhook (DPAPI, CurrentUser) and loads it here.
# Until it does there is no direct post: every event carries posted_by_pc
# false and Hive relays it. No GetNewClosure: a closure would not see this
# script's functions.
$script:WatchWebhook = $null
$watchSender = $null
if (-not [string]::IsNullOrEmpty($script:WatchWebhook)) {
    $watchSender = { param($p) Send-SlackPayload -WebhookUrl $script:WatchWebhook -Payload $p }
}
$null = Invoke-WatchTick -StateDir $watchStateDir -LogbookPath $watchConfig.logbook `
    -ShareDir $watchConfig.shareDir -ProcedureRoot $watchConfig.procedureRoot `
    -HiveLogsDir $watchConfig.hiveLogsDir -Now (Get-Date) -Sender $watchSender `
    -MemberId $watchConfig.memberId
exit 0
