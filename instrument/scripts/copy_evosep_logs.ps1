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

    IT USES ROBOCOPY, because Copy-Item in a loop is what made it slow.
    The PowerShell version did one directory listing against the SHARE per
    candidate folder plus a Copy-Item per file, single-threaded. Measured on
    TIMS-10878: 2,000 folders took 60 s and copied nothing -- 30 ms of SMB
    round-trip each, purely to confirm files that were already there. At
    that rate one full verification of 31,463 folders is ~16 hourly passes.

    Robocopy ships with Windows, does the same size/timestamp comparison
    natively, and does it across /MT threads. flinders_copy.ps1 already
    copies acquisitions this way; this is the same decision for the same
    reason.

    The folder walk that remains exists for one thing robocopy cannot do:
    exclude a procedure run that is STILL BEING WRITTEN. Robocopy's age
    filters are whole days, so it cannot express "settled for 60 seconds".
    Any run folder touched within the last minute is handed to robocopy as
    /XD and picked up next pass -- half a trace is worse than no trace.

    -Days becomes robocopy's /MAXAGE, so the scheduled hourly pass considers
    recent files only and a manual run still sweeps the whole history.

.PARAMETER Days
    Limit to files changed in the last N days. 0 (the default) means the
    entire history.

.PARAMETER Threads
    Robocopy /MT thread count. Higher hides more SMB latency; 16 is
    robocopy's own documented sweet spot for a network target.

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
    [string] $OutRoot     = 'Y:\STAN\evosep_logs',
    [string] $OutRootUnc  = '\\128.120.208.42\proteomics-grp\STAN\evosep_logs',
    [int]    $Days        = 0,
    # Robocopy threads. The copy is latency-bound against a network target,
    # not CPU-bound, so this is about hiding round-trips rather than cores.
    [int]    $Threads     = 16,
    [int]    $EveryMinutes = 60,
    [switch] $Recent,
    [switch] $Scheduled,
    [switch] $Uninstall,
    # Forget which folders are known complete and re-verify every one of
    # them against the share. For when the mirror has been touched by
    # something other than this script.
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
$ScriptVersion = '1.0.99'

$TaskName = 'STAN Evosep log mirror'
$InstallDir = Join-Path $env:ProgramData 'STAN'
$InstalledPath = Join-Path $InstallDir 'copy_evosep_logs.ps1'
$LogPath = Join-Path $InstallDir 'copy_evosep_logs.log'

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

# A procedure run still being written must not be mirrored half-copied.
# Robocopy's age filters are whole days and cannot express "settled for 60
# seconds", so the in-flight folders are found here and excluded by name.
# Only the newest handful can possibly be in flight, so this costs one
# local directory listing, not a walk.
$settled = (Get-Date).AddSeconds(-60)
$runDirs = New-Object 'System.Collections.Generic.List[object]'
foreach ($serial in @(Get-ChildItem -LiteralPath $Source -Directory -EA SilentlyContinue)) {
    foreach ($d in @(Get-ChildItem -LiteralPath $serial.FullName -Directory -EA SilentlyContinue)) {
        $runDirs.Add($d)
    }
}
Say ("Found {0:N0} procedure-run folder(s)." -f $runDirs.Count)

$excludes = New-Object 'System.Collections.Generic.List[string]'
foreach ($d in $runDirs) {
    if ($d.LastWriteTime -ge $settled) { $excludes.Add($d.FullName) }
}
if ($excludes.Count -gt 0) {
    Say ("{0} folder(s) still being written -- left for the next pass." -f $excludes.Count) 'Yellow'
}

# Robocopy does the comparison. /E every subdirectory, /XO never overwrite a
# newer destination, /MT for parallelism, /R:1 /W:2 so an unreadable file
# costs seconds rather than minutes. No /Z or /IPG: both are silently
# incompatible with /MT, and throughput matters more here than politeness
# because the payload is small text files rather than a 2 GB acquisition.
$roboLog = Join-Path $InstallDir 'copy_evosep_logs.robocopy.log'
$roboArgs = New-Object 'System.Collections.Generic.List[string]'
$roboArgs.Add("`"$Source`"")
$roboArgs.Add("`"$dest`"")
$roboArgs.Add("/E"); $roboArgs.Add("/XO")
$roboArgs.Add("/MT:$Threads")
$roboArgs.Add("/R:1"); $roboArgs.Add("/W:2")
$roboArgs.Add("/NP"); $roboArgs.Add("/NFL"); $roboArgs.Add("/NDL"); $roboArgs.Add("/NJH")
if ($Days -gt 0) { $roboArgs.Add("/MAXAGE:$Days") }
foreach ($x in $excludes) { $roboArgs.Add("/XD"); $roboArgs.Add("`"$x`"") }
$roboArgs.Add("/LOG+:`"$roboLog`"")

Say ''
Say ("Mirroring with robocopy ({0} threads{1})..." -f $Threads, $(if ($Days -gt 0) { ", last $Days day(s)" } else { ", entire history" }))
$sw = [Diagnostics.Stopwatch]::StartNew()
$proc = Start-Process robocopy.exe -ArgumentList $roboArgs -WindowStyle Hidden -PassThru
try { $proc.PriorityClass = 'BelowNormal' } catch { }
$proc.WaitForExit()
$sw.Stop()
$rc = $proc.ExitCode

# Robocopy exit codes are a BITMASK, not a status: 1 copied, 2 extras,
# 4 mismatched, 8 failed, 16 fatal. Anything under 8 is success, and
# treating a plain non-zero as failure would report every successful copy
# as an error.
Say ''
if ($rc -ge 8) {
    Say ("robocopy reported a failure (exit $rc) after {0:N0}s -- see $roboLog" -f $sw.Elapsed.TotalSeconds) 'Red'
} elseif ($rc -eq 0) {
    Say ("Nothing new. Mirror already current ({0:N0}s)." -f $sw.Elapsed.TotalSeconds) 'Green'
} else {
    Say ("Mirror updated in {0:N0}s (robocopy exit $rc)." -f $sw.Elapsed.TotalSeconds) 'Green'
}

Say ''
Say "On Hive: /quobyte/proteomics-grp/STAN/evosep_logs/$($env:COMPUTERNAME)_mirror" 'Cyan'
if ($excludes.Count -gt 0) {
    Say ("{0} in-flight folder(s) will arrive on the next pass." -f $excludes.Count) 'Yellow'
} elseif ($rc -lt 8) {
    Say 'Mirror is complete. Safe to close.' 'Green'
}
Pause-IfInteractive
