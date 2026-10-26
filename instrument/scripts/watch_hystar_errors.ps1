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

# ----------------------------------------------------------------- top level
# Stub: later parts of this script fill this in. Running it does nothing.
return
