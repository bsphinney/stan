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
      * A file already present at the same size is SKIPPED. So the first run
        is the big one (roughly 14 GB for 2023-onward) and every run after
        that copies only what is new.
      * Interrupting it is safe. Re-run and it picks up where it stopped --
        nothing is deleted, nothing on C: is ever written.
      * Files touched in the last 60 seconds are skipped: the Evosep may
        still be writing them, and half a trace is worse than no trace.

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
    [int]    $EveryMinutes = 60,
    [switch] $Recent,
    [switch] $Scheduled,
    [switch] $Uninstall
)
$ErrorActionPreference = 'Stop'

$TaskName = 'STAN Evosep log mirror'
$InstallDir = Join-Path $env:ProgramData 'STAN'
$InstalledPath = Join-Path $InstallDir 'copy_evosep_logs.ps1'
$LogPath = Join-Path $InstallDir 'copy_evosep_logs.log'

function Say($m, $c = 'Gray') {
    if ($Scheduled) {
        $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
        try { Add-Content -LiteralPath $LogPath -Value $line -EA SilentlyContinue } catch { }
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
    $argline = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstalledPath`" -Scheduled"
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

# -InstallOnly is the elevated half of the self-install re-launching itself.
# It is not in param() because nothing outside this script should pass it.
$installOnly = $false
foreach ($a in $MyInvocation.UnboundArguments) {
    if ("$a" -eq '-InstallOnly') { $installOnly = $true }
}

if ($Uninstall) { $null = Remove-Task; Pause-IfInteractive; exit 0 }
if ($installOnly) { $ok = Install-Task; if ($ok) { exit 0 } else { exit 1 } }

if ($Recent -and $Days -le 0) { $Days = 30 }

# THE SELF-INSTALL. Only when interactive and only when the task is absent,
# so a scheduled run never tries to reinstall itself and a second manual run
# is a no-op.
if (-not $Scheduled) {
    if (Test-Task) {
        Say "Automatic copy is already installed ('$TaskName', every $EveryMinutes min)." 'Green'
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

# Explicit foreach rather than a Where-Object pipeline: PS 5.1 pipelines here
# have bitten this repo before, and the loop is also measurably faster over a
# 100k-file tree.
$all = @(Get-ChildItem $Source -Recurse -File -EA SilentlyContinue)
$picked = New-Object 'System.Collections.Generic.List[object]'
foreach ($f in $all) {
    if ($f.LastWriteTime -gt $cut -and $f.LastWriteTime -lt $settled) { $picked.Add($f) }
}

if ($picked.Count -eq 0) {
    Say 'No settled files in the window.' 'Yellow'
    Pause-IfInteractive
    exit 0
}

# NEWEST FIRST. 2026-09-02: this was oldest-first for about an hour, on the
# reasoning that an interrupted run then leaves a contiguous history. That was
# the wrong optimisation. A full pull is ~16 GB and runs for hours, and the
# question actually being asked -- did the column change on 2026-07-31 show up
# in the pressure -- needs the LAST few months, which oldest-first delivers
# LAST. Newest-first puts the decision-relevant window on the share within
# minutes and lets the 2023 tail arrive whenever it arrives.
$files = @($picked | Sort-Object LastWriteTime -Descending)

Say ("Found {0:N0} file(s) in the source window." -f $files.Count)
Say ''

$madeDirs = New-Object 'System.Collections.Generic.HashSet[string]'
$srcLen = $Source.Length
$copied = 0; $skipped = 0; $failed = 0; $bytes = 0L
$capped = $false
$sw = [Diagnostics.Stopwatch]::StartNew()

foreach ($f in $files) {
    $rel = $f.FullName.Substring($srcLen).TrimStart('\')
    $dst = Join-Path $dest $rel

    # Already mirrored at the same size -- the common case on every run
    # after the first, so it is checked before anything expensive.
    $existing = Get-Item -LiteralPath $dst -EA SilentlyContinue
    if ($existing -and $existing.Length -eq $f.Length) { $skipped++; continue }

    if (($bytes + $f.Length) -gt ([long]$MaxMB * 1MB)) { $capped = $true; break }

    $parent = Split-Path $dst
    if (-not $madeDirs.Contains($parent)) {
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        [void]$madeDirs.Add($parent)
    }

    try {
        Copy-Item -LiteralPath $f.FullName -Destination $dst -Force
        $copied++; $bytes += $f.Length
    } catch {
        $failed++
        if ($failed -le 5) { Say ("  skip (unreadable): {0}" -f $f.FullName) 'DarkYellow' }
    }

    if ((($copied + $skipped) % 500) -eq 0) {
        $pct = [math]::Round(100.0 * ($copied + $skipped) / $files.Count, 1)
        Say ("  {0,5}% - {1:N0} new, {2:N0} already there, {3:N1} MB, {4:N0}s" -f $pct, $copied, $skipped, ($bytes / 1MB), $sw.Elapsed.TotalSeconds)
    }
}
$sw.Stop()

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
