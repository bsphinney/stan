<#
.SYNOPSIS
    Mirror the Evosep One procedure logs to the Quobyte share, and keep
    doing it. Read-only on the source.

.DESCRIPTION
    The Evosep records Pressure [bar], Actual flow, Displacement and Setpoint
    per procedure, and writes them under
    C:\ProgramData\Evosep\EvosepOne\Procedure logs\<serial>\.
    Bruker's Compass database does NOT hold pressure (verified), so these logs
    are the only place a pressure trace exists -- which makes them the basis
    for column-clog trending and column-lifetime curves.

    IT INSTALLS ITSELF. Run it once (double-click the .bat). If the scheduled
    task is not already there it copies itself to a stable location, registers
    the task, and then does the copy. Every run after that finds the task and
    just copies. There is no flag to remember and no separate installer.

    Why that matters here: these logs reached Hive exactly twice, by hand, and
    then stopped -- 2026-09-03 was the last one. The extractor kept running
    against the frozen mirror and the column-health panel showed six-day-old
    numbers about a column that had already been replaced. The copy was the
    only manual link in an otherwise automatic chain, so it is the link that
    broke.

    How the mirror works
      * One stable destination, <share>\evosep_logs\<COMPUTERNAME>_mirror,
        instead of a new timestamped folder per run. Analysis always reads
        the same path and never has to be told which copy is newest.
      * IT WORKS PER PROCEDURE-RUN DIRECTORY, NOT PER FILE. The Evosep
        writes <method>_YYYY-MM-DD_HH-MM-SS\ with ~28 files inside, and
        31,463 of those directories is where the 880,903 files come from.
        The date is in the DIRECTORY NAME, so choosing what to consider
        costs a string match and no disk access at all. A run directory
        already mirrored with the same file count is skipped whole -- one
        listing instead of 28 network round-trips. A finished procedure run
        never changes, so "same count" is a sound completeness test.
      * Interrupting it is safe. Re-run and it picks up where it stopped --
        nothing is deleted, nothing on C: is ever written.
      * Files touched in the last 60 seconds are skipped: the Evosep may
        still be writing them, and half a trace is worse than no trace.

    NEWEST FIRST, BOUNDED, AND IT REMEMBERS. A folder proven complete is
    recorded in <ProgramData>\STAN\copy_evosep_logs.done and never checked
    again, because a finished procedure run is immutable. That is what turns
    a routine pass from "listing 31,463 folders on the share" into "listing
    the couple of dozen that are new". Each pass also stops after
    -MaxFolders folders: a cold mirror is ~16 GB and cannot finish in one
    hourly run, so it takes a bite newest-first, saves what it proved, and
    the next run resumes further back. -Recheck forgets the memory, for when
    the mirror has been touched by something other than this script.

    WHY THE OLD VERSION WAS SLOW. It did Get-ChildItem -Recurse over the
    whole tree, materialised 880,903 FileInfo objects, and then called
    Get-Item on the SHARE once per file to compare sizes. That is 880,903
    SMB round-trips to discover that nothing changed. The scheduled task
    passes -Days 30, so a routine hourly pass now examines a few dozen
    directory names and copies whatever is new.

.PARAMETER Days
    Limit to files changed in the last N days. 0 (the default) means the
    entire history.

.PARAMETER MaxMB
    Safety cap on a single run. Generous by design; if it is ever reached the
    script says so loudly rather than quietly truncating your history.

.PARAMETER Scheduled
    Set by the scheduled task only. Never type it: it suppresses the
    self-install and the "press any key", and sends output to a log file.

.EXAMPLE
    .\copy_evosep_logs.ps1               # everything, incremental, self-installing
    .\copy_evosep_logs.ps1 -Days 30      # just the last 30 days
    .\copy_evosep_logs.ps1 -Uninstall    # remove the scheduled task
#>
[CmdletBinding()]
param(
    [string] $Source      = 'C:\ProgramData\Evosep\EvosepOne\Procedure logs',
    [string] $OutRoot     = 'Y:\brett\evosep_logs',
    [string] $OutRootUnc  = '\\128.120.208.42\proteomics-grp\brett\evosep_logs',
    [int]    $Days        = 0,
    [int]    $MaxMB       = 60000,
    # Most folders a single pass will look at. A cold mirror is ~16 GB
    # and cannot finish in one hourly run; this bounds the pass and the
    # next one resumes further back, newest-first throughout.
    [int]    $MaxFolders  = 2000,
    [int]    $EveryMinutes = 60,
    [switch] $Recent,
    [switch] $Scheduled,
    [switch] $Uninstall,
    # Forget which folders are known complete and re-verify every one of
    # them against the share. For when the mirror has been touched by
    # something other than this script.
    [switch] $Recheck,
    # The elevated half of the self-install re-launches the script with
    # this. It MUST be a declared parameter: under [CmdletBinding()]
    # PowerShell rejects an unknown named parameter outright rather than
    # passing it through to $MyInvocation.UnboundArguments, so reading it
    # from there meant the elevated child died on parameter binding and
    # the task was never created. Not for typing.
    [switch] $InstallOnly
)
$ErrorActionPreference = 'Stop'

# Kept in step with stan/__init__.py by
# tests/test_instrument_copy_scripts.ps1, which fails if they drift.
# These scripts get copied to instrument PCs and then live there on
# their own, so "is the copy in front of me current?" has to be
# answerable without a git checkout.
$ScriptVersion = '1.0.98'

$TaskName = 'STAN Evosep log mirror'
$InstallDir = Join-Path $env:ProgramData 'STAN'
$InstalledPath = Join-Path $InstallDir 'copy_evosep_logs.ps1'
$LogPath = Join-Path $InstallDir 'copy_evosep_logs.log'
# Folders already verified complete on the share. A finished procedure run
# is immutable, so re-checking one is a wasted SMB round-trip -- and at
# 31,463 folders that is the whole cost of a pass. Remembering them turns a
# routine run into "check the couple of dozen that are new".
$StatePath = Join-Path $InstallDir 'copy_evosep_logs.done'

function Say($m, $c = 'Gray') {
    if ($Scheduled) {
        $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
        # Add-Content does not create the parent, and the whole call is
        # wrapped in try/catch -- so without this a scheduled run whose
        # install dir is missing logs absolutely nothing, silently. That is
        # the one situation where the log is the only way to find out what
        # happened.
        try {
            if (-not (Test-Path -LiteralPath $InstallDir)) {
                New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
            }
            Add-Content -LiteralPath $LogPath -Value $line -EA SilentlyContinue
        } catch { }
    } else {
        Write-Host $m -ForegroundColor $c
    }
}

function Pause-IfInteractive {
    # A scheduled run has no console. ReadKey there does not "wait for a key",
    # it throws or blocks forever holding the task open, which is why this is
    # gated rather than simply left at the end of the file.
    if ($Scheduled) { return }
    Say ''
    Say 'Press any key to continue . . .'
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { }
}

function Get-DoneSet {
    # Header line records the destination. A different mirror path means the
    # remembered folders say nothing about it, so the set is discarded rather
    # than trusted -- a stale "already done" is the one failure that would
    # silently skip real work.
    # `return ,$set`, with the comma, at EVERY exit. PowerShell unrolls a
    # collection on return: a plain `return $set` hands back $null when the
    # set is empty, which is precisely the cold-start case, and the caller
    # then dies on $done.Contains() with "You cannot call a method on a
    # null-valued expression". CLAUDE.md documents this and it still caught
    # me. The caller does not wrap in @(), so the comma is required.
    $set = New-Object 'System.Collections.Generic.HashSet[string]'
    if ($Recheck) { return ,$set }
    if (-not (Test-Path -LiteralPath $StatePath)) { return ,$set }
    try {
        $lines = @(Get-Content -LiteralPath $StatePath -EA SilentlyContinue)
    } catch { return ,$set }
    if ($lines.Count -eq 0) { return ,$set }
    if ($lines[0] -ne "dest=$dest") { return ,$set }
    for ($i = 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i]) { [void]$set.Add($lines[$i]) }
    }
    return ,$set
}

function Save-DoneSet($set) {
    try {
        if (-not (Test-Path -LiteralPath $InstallDir)) {
            New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
        }
        $out = New-Object 'System.Collections.Generic.List[string]'
        $out.Add("dest=$dest")
        foreach ($k in $set) { $out.Add($k) }
        Set-Content -LiteralPath $StatePath -Value $out -EA SilentlyContinue
    } catch { }
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

function Remove-Task {
    if (-not (Test-Task)) { Say "No scheduled task named '$TaskName'." 'Yellow'; return $true }
    if (-not (Test-Elevated)) {
        Say 'Removing the scheduled task needs administrator rights.' 'Yellow'
        $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Uninstall')
        try { Start-Process powershell.exe -Verb RunAs -ArgumentList $args -Wait -EA Stop }
        catch { Say 'The administrator prompt was refused.' 'Red'; return $false }
        return (-not (Test-Task))
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Say "Removed scheduled task '$TaskName'." 'Green'
    return $true
}

function Get-InstalledVersion {
    # Reads $ScriptVersion out of the installed copy without executing it.
    if (-not (Test-Path -LiteralPath $InstalledPath)) { return "" }
    foreach ($line in (Get-Content -LiteralPath $InstalledPath -EA SilentlyContinue)) {
        # No leading $ in the pattern: in a double-quoted PowerShell string
        # "$ScriptVersion" INTERPOLATES, so the pattern became
        # "^\s*1.0.96\s*=..." and matched nothing -- Get-InstalledVersion
        # always answered "" and the task reinstalled on every single run.
        if ($line -match "ScriptVersion\s*=\s*'([^']+)'") { return $Matches[1] }
    }
    return ""
}

function Install-Task {
    # Copy to a stable path first. Registering the task against wherever the
    # operator happened to double-click it from -- Downloads, a USB stick, a
    # share -- gives a task that works today and silently stops the moment
    # that folder moves.
    if (-not (Test-Path -LiteralPath $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }
    if ($PSCommandPath -ne $InstalledPath) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $InstalledPath -Force
    }

    if (-not (Test-Elevated)) {
        Say ''
        Say 'Setting up the automatic hourly copy needs administrator rights.'
        Say 'Say Yes to the Windows prompt that is about to appear.'
        $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                  '-File', $InstalledPath, '-InstallOnly')
        try {
            Start-Process powershell.exe -Verb RunAs -ArgumentList $args -Wait -EA Stop
        } catch {
            Say ''
            Say 'The administrator prompt was refused or cancelled.' 'Red'
            Say 'The copy below will still run, but it will not repeat by itself.' 'Yellow'
            return $false
        }
        if (Test-Task) { Say "Scheduled task '$TaskName' created, every $EveryMinutes minutes." 'Green'; return $true }
        Say 'The task still is not there after elevating.' 'Red'
        return $false
    }

    # Runs as the logged-on user ON PURPOSE, never SYSTEM: an elevated or
    # SYSTEM context cannot see the operator's mapped Y: drive. The UNC
    # fallback covers it either way, but the user context is what makes the
    # ordinary path work.
    # -Days 30 on the SCHEDULED pass only. An hourly job has no business
    # re-examining 31,463 folders back to 2023; a manual run keeps the
    # full-history default so it still backfills anything ever missed.
    $argline = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstalledPath`" -Scheduled -Days 30"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argline
    $atLogon = New-ScheduledTaskTrigger -AtLogOn
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action `
            -Trigger @($atLogon, $repeat) -Settings $settings -Force -EA Stop | Out-Null
    } catch {
        Say ''
        Say "Could not create the scheduled task: $($_.Exception.Message)" 'Red'
        return $false
    }
    # Never claim success without looking.
    if (-not (Test-Task)) { Say 'The task did not appear after registering.' 'Red'; return $false }
    Say "Scheduled task '$TaskName' created, every $EveryMinutes minutes." 'Green'
    return $true
}

Say "STAN Evosep log mirror  v$ScriptVersion" 'Cyan'
Say "  running from: $PSCommandPath"
Say ''

if ($Uninstall) { $null = Remove-Task; Pause-IfInteractive; exit 0 }
if ($InstallOnly) { $ok = Install-Task; if ($ok) { exit 0 } else { exit 1 } }

if ($Recent -and $Days -le 0) { $Days = 30 }

# THE SELF-INSTALL. Only when interactive and only when the task is absent,
# so a scheduled run never tries to reinstall itself and a second manual run
# is a no-op.
if (-not $Scheduled) {
    $installedVersion = Get-InstalledVersion
    if (Test-Task) {
        if ($installedVersion -eq $ScriptVersion) {
            Say "Automatic copy is up to date (v$ScriptVersion, every $EveryMinutes min)." 'Green'
        } else {
            # THE GAP THIS CLOSES. The scheduled task runs the COPY under
            # ProgramData, taken when it was first registered -- so updating
            # the share changed nothing about what actually ran, and the task
            # would have kept running its original version forever. Every
            # silent staleness problem in this system has had that shape, so
            # re-running the .bat now refreshes the installed copy and
            # re-registers rather than reporting "already installed".
            $was = $installedVersion
            if (-not $was) { $was = "unknown" }
            Say "Automatic copy is at v$was; this is v$ScriptVersion -- updating it." 'Yellow'
            $null = Install-Task
        }
    } else {
        Say "No automatic copy found -- setting one up." 'Yellow'
        $null = Install-Task
    }
    Say ''
}

if (-not (Test-Path $Source)) {
    Say "Source not found: $Source" 'Red'
    Say 'Pass the right path with -Source if Evosep writes elsewhere.' 'Yellow'
    Pause-IfInteractive
    exit 1
}

$root = $OutRoot
try {
    if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root -Force | Out-Null }
} catch {
    $root = $OutRootUnc
    if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root -Force | Out-Null }
}

$dest = Join-Path $root "$($env:COMPUTERNAME)_mirror"
if (-not (Test-Path $dest)) { New-Item -ItemType Directory -Path $dest -Force | Out-Null }

Say "source: $Source"
Say "mirror: $dest"
if ($Days -gt 0) { Say "window: last $Days day(s)" } else { Say 'window: entire history' }
Say ''
Say 'Scanning the source (this can take a minute on a long history)...'

$settled = (Get-Date).AddSeconds(-60)
if ($Days -gt 0) { $cut = (Get-Date).AddDays(-$Days) } else { $cut = [datetime]'1900-01-01' }

# Enumerate procedure-run directories, not files. The Evosep lays out
#     <serial>\<method>_YYYY-MM-DD_HH-MM-SS\<28 files>
# so one listing per serial gives every run, and the run's date is readable
# straight off the name -- no stat, no recursion, no 880k-element array.
$runDirs = New-Object 'System.Collections.Generic.List[object]'
foreach ($serial in @(Get-ChildItem -LiteralPath $Source -Directory -EA SilentlyContinue)) {
    foreach ($d in @(Get-ChildItem -LiteralPath $serial.FullName -Directory -EA SilentlyContinue)) {
        $runDirs.Add($d)
    }
}
Say ("Found {0:N0} procedure-run folder(s)." -f $runDirs.Count)

# Date from the directory NAME. Falling back to LastWriteTime keeps an
# unexpected layout working rather than silently skipping it.
$considered = New-Object 'System.Collections.Generic.List[object]'
foreach ($d in $runDirs) {
    $runDate = $d.LastWriteTime
    if ($d.Name -match '_(\d{4}-\d{2}-\d{2})_') {
        try { $runDate = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd', $null) } catch { }
    }
    if ($runDate -ge $cut) { $considered.Add($d) }
}
if ($Days -gt 0) {
    Say ("{0:N0} of them fall in the last {1} day(s)." -f $considered.Count, $Days)
}

$srcLen = $Source.Length
$copied = 0; $skippedDirs = 0; $skipped = 0; $failed = 0; $bytes = 0L
$capped = $false
$sw = [Diagnostics.Stopwatch]::StartNew()
$examined = 0
$verified = 0

# Folders proven complete on a previous run. Checking one costs an SMB
# listing, and a finished procedure run is immutable, so this is the whole
# difference between "look at 31,463 folders" and "look at the couple of
# dozen that are new".
$done = Get-DoneSet
$known = $done.Count
if ($known -gt 0) { Say ("{0:N0} folder(s) already known complete -- not re-checked." -f $known) }

# Work newest-first and cap it. A cold mirror is ~16 GB and cannot finish in
# one hourly pass, so rather than running for hours it takes a bite, saves
# what it proved, and the next run resumes further back. The newest data is
# on the share within minutes either way.
$todo = New-Object 'System.Collections.Generic.List[object]'
foreach ($d in @($considered | Sort-Object Name -Descending)) {
    if ($done.Contains($d.Name)) { $skippedDirs++; continue }
    $todo.Add($d)
}
if ($todo.Count -gt $MaxFolders) {
    Say ("{0:N0} folder(s) to check; doing the newest {1:N0} this pass, rest next run." -f `
         $todo.Count, $MaxFolders) 'Yellow'
}

foreach ($d in $todo) {
    if ($examined -ge $MaxFolders) { break }
    $examined++
    $rel = $d.FullName.Substring($srcLen).TrimStart('\', '/')
    $dstDir = Join-Path $dest $rel

    $srcFiles = @(Get-ChildItem -LiteralPath $d.FullName -File -EA SilentlyContinue)
    if ($srcFiles.Count -eq 0) { continue }

    # ONE listing of the destination folder, not a stat per file.
    $dstFiles = @(Get-ChildItem -LiteralPath $dstDir -File -EA SilentlyContinue)
    if ($dstFiles.Count -ge $srcFiles.Count) {
        $skipped += $srcFiles.Count
        [void]$done.Add($d.Name); $verified++
        continue
    }

    $haveNames = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($x in $dstFiles) { [void]$haveNames.Add($x.Name) }
    if (-not (Test-Path -LiteralPath $dstDir)) {
        New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
    }

    $unsettled = 0
    foreach ($f in $srcFiles) {
        if ($f.LastWriteTime -ge $settled) { $unsettled++; continue }
        if ($haveNames.Contains($f.Name)) { $skipped++; continue }
        if (($bytes + $f.Length) -gt ([long]$MaxMB * 1MB)) { $capped = $true; break }
        try {
            Copy-Item -LiteralPath $f.FullName -Destination (Join-Path $dstDir $f.Name) -Force
            $copied++; $bytes += $f.Length
        } catch {
            $failed++
            if ($failed -le 5) { Say ("  skip (unreadable): {0}" -f $f.FullName) 'DarkYellow' }
        }
    }
    if ($capped) { break }
    # Only remember it as done when the whole folder really is there. A run
    # still being written, or a file that would not copy, must be revisited.
    if ($unsettled -eq 0 -and $failed -eq 0) { [void]$done.Add($d.Name); $verified++ }

    if (($examined % 250) -eq 0) {
        Say ("  {0:N0}/{1:N0} folders - {2:N0} new file(s), {3:N1} MB, {4:N0}s" -f `
             $examined, $todo.Count, $copied, ($bytes / 1MB), $sw.Elapsed.TotalSeconds)
    }
}
$sw.Stop()
Save-DoneSet $done
$remaining = $todo.Count - $examined
if ($remaining -gt 0) {
    Say ("{0:N0} older folder(s) still to verify -- the next run continues from there." -f $remaining) 'Yellow'
}

Say ''
Say ("Checked {0:N0} folder(s) this pass; {1:N0} skipped from memory, {2:N0} newly verified." -f $examined, $skippedDirs, $verified)
Say ''
if ($capped) {
    Say "*** SIZE CAP ${MaxMB}MB REACHED - HISTORY IS INCOMPLETE ***" 'Red'
    Say '    Re-run this script: it resumes where it stopped.' 'Red'
    Say '    Or raise the cap with -MaxMB.' 'Red'
    Say ''
}
Say ("Copied  {0:N0} new file(s), {1:N1} MB in {2:N0}s" -f $copied, ($bytes / 1MB), $sw.Elapsed.TotalSeconds) 'Green'
Say ("Skipped {0:N0} already mirrored" -f $skipped)
if ($failed -gt 0) { Say ("Failed  {0:N0} unreadable file(s)" -f $failed) 'Yellow' }
Say ''
Say "On Hive: /quobyte/proteomics-grp/brett/evosep_logs/$($env:COMPUTERNAME)_mirror" 'Cyan'
if (-not $capped -and $failed -eq 0) { Say 'Mirror is complete. Safe to close.' 'Green' }
Pause-IfInteractive
