<#
.SYNOPSIS
    Read-only watcher for HyStar's error logbook (CompassHyStarErrorLogbook.log).

.DESCRIPTION
    Runs once a minute on the timsTOF instrument PC (PowerShell 5.1), tails the
    logbook and alerts on every EmergencyShutdown, the event that stops the
    acquisition queue.

    THIS FILE IS BUILT IN PARTS. This part holds only the reading and parsing
    functions; the top level is a stub that returns. Later parts add the
    state file, de-duplication, the Slack post and the share writer.

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

# ----------------------------------------------------------------- top level
# Stub: later parts of this script fill this in. Running it does nothing.
return
