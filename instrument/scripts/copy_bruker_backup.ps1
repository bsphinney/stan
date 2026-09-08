<#
.SYNOPSIS
    Mirror Bruker's own database backups to the Quobyte share, and keep
    doing it. Read-only on the source.

.DESCRIPTION
    Bruker's tooling writes its own PostgreSQL backups to D:\BrukerDBBackup
    on a schedule of its own (daily at 18:00 here). A backup was produced by
    a process that already authenticated, so reading it needs no database
    password -- it sidesteps the credential problem entirely.

    IT INSTALLS ITSELF. Run it once (double-click the .bat). If the scheduled
    task is not already there it copies itself to a stable location,
    registers the task, and then does the copy. Every run after that finds
    the task and just copies. No flag to remember, no separate installer.

    DESTINATION CHANGED 2026-09-08, AND THIS IS THE POINT OF THE REWRITE.
    This used to write to Y:\brett\bruker_db\backup_<HOST>_<timestamp>\.
    The Hive extractor does not look there. cron_bruker_maintenance.sh runs

        find /quobyte/proteomics-grp/brett/BrukerDBBackup -name '*.backup'

    and takes the newest, then derives the snapshot date from the PARENT
    DIRECTORY NAME (2026-08-31_180000 -> 2026-08-31). So the copy has to
    land under BrukerDBBackup and has to preserve Bruker's own
    daily/monthly/yearly/<stamp>/ layout. Scheduling the old version would
    have copied backups faithfully to a folder nothing reads, and the panel
    would have stayed frozen while every part appeared to be working.

    Incremental, like the Evosep mirror: a file already present at the same
    size is skipped, so the first run is the big one and later runs move
    only the new nightly backup. Files touched in the last 60 seconds are
    skipped because a backup still being written has a moving size.

.PARAMETER Scheduled
    Set by the scheduled task only. Never type it: it suppresses the
    self-install and the "press any key", and sends output to a log file.

.EXAMPLE
    .\copy_bruker_backup.ps1              # incremental, self-installing
    .\copy_bruker_backup.ps1 -Uninstall   # remove the scheduled task
#>
[CmdletBinding()]
param(
    [string] $Source       = 'D:\BrukerDBBackup',
    [string] $OutRoot      = 'Y:\brett\BrukerDBBackup',
    [string] $OutRootUnc   = '\\128.120.208.42\proteomics-grp\brett\BrukerDBBackup',
    [int]    $MaxTotalMB   = 8192,
    [int]    $EveryMinutes = 240,
    [switch] $All,
    [switch] $Scheduled,
    [switch] $Uninstall,
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
$ScriptVersion = '1.0.96'

$TaskName = 'STAN Bruker backup mirror'
$InstallDir = Join-Path $env:ProgramData 'STAN'
$InstalledPath = Join-Path $InstallDir 'copy_bruker_backup.ps1'
$LogPath = Join-Path $InstallDir 'copy_bruker_backup.log'

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
    # A scheduled run has no console; ReadKey there holds the task open
    # rather than waiting for a key.
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
    # Stable path first: a task registered against Downloads or a USB stick
    # works today and stops silently the moment that folder moves.
    if (-not (Test-Path -LiteralPath $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }
    if ($PSCommandPath -ne $InstalledPath) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $InstalledPath -Force
    }

    if (-not (Test-Elevated)) {
        Say ''
        Say 'Setting up the automatic copy needs administrator rights.'
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

    # Logged-on user, never SYSTEM: a SYSTEM context cannot see the mapped
    # Y: drive. The UNC fallback covers it, but the user context is what
    # makes the ordinary path work.
    $argline = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$InstalledPath`" -Scheduled"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argline
    $atLogon = New-ScheduledTaskTrigger -AtLogOn
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(3) `
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
    if (-not (Test-Task)) { Say 'The task did not appear after registering.' 'Red'; return $false }
    Say "Scheduled task '$TaskName' created, every $EveryMinutes minutes." 'Green'
    return $true
}

Say "STAN Bruker backup mirror  v$ScriptVersion" 'Cyan'
Say "  running from: $PSCommandPath"
Say ''

if ($Uninstall) { $null = Remove-Task; Pause-IfInteractive; exit 0 }
if ($InstallOnly) { $ok = Install-Task; if ($ok) { exit 0 } else { exit 1 } }

if (-not $Scheduled) {
    if (Test-Task) {
        Say "Automatic copy is already installed ('$TaskName', every $EveryMinutes min)." 'Green'
    } else {
        Say 'No automatic copy found -- setting one up.' 'Yellow'
        $null = Install-Task
    }
    Say ''
}

if (-not (Test-Path $Source)) {
    Say "Source not found: $Source" 'Red'
    Say 'If Bruker writes backups elsewhere, pass it:  -Source <path>' 'Yellow'
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

Say "source: $Source"
Say "mirror: $root"

# Explicit foreach rather than a Where-Object pipeline (PS 5.1 pipeline
# behaviour has bitten this repo before).
$cut = (Get-Date).AddSeconds(-60)
$all = @(Get-ChildItem $Source -Recurse -File -EA SilentlyContinue)
$picked = New-Object 'System.Collections.Generic.List[object]'
foreach ($f in $all) {
    if ($f.LastWriteTime -lt $cut) { $picked.Add($f) }
}
if ($picked.Count -eq 0) {
    Say "No settled backup files in $Source (all modified in the last 60s?)." 'Yellow'
    Pause-IfInteractive
    exit 0
}

# Newest first, so the file the Hive extractor actually wants -- the most
# recent compass.backup -- lands before any size cap can bite.
$files = @($picked | Sort-Object LastWriteTime -Descending)

$srcLen = $Source.Length
$madeDirs = New-Object 'System.Collections.Generic.HashSet[string]'
$copied = 0; $skipped = 0; $failed = 0; $bytes = 0L
$capped = $false

foreach ($f in $files) {
    # Preserve Bruker's own daily/<stamp>/ layout: the extractor reads the
    # snapshot date off the parent directory name, so a flattened copy would
    # arrive dated wrong even though the bytes were right.
    $rel = $f.FullName.Substring($srcLen).TrimStart('\')
    $dst = Join-Path $root $rel

    $existing = Get-Item -LiteralPath $dst -EA SilentlyContinue
    if ($existing -and $existing.Length -eq $f.Length) { $skipped++; continue }

    if (($bytes + $f.Length) -gt ([long]$MaxTotalMB * 1MB)) { $capped = $true; break }

    $parent = Split-Path $dst
    if (-not $madeDirs.Contains($parent)) {
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        [void]$madeDirs.Add($parent)
    }

    try {
        Copy-Item -LiteralPath $f.FullName -Destination $dst -Force
        $copied++; $bytes += $f.Length
        Say ("  {0}  ({1:N1} MB)" -f $rel, ($f.Length / 1MB))
    } catch {
        $failed++
        if ($failed -le 5) { Say ("  skip (unreadable): {0}" -f $f.FullName) 'DarkYellow' }
    }
}

Say ''
if ($capped) {
    Say "*** SIZE CAP ${MaxTotalMB}MB REACHED - not everything was copied ***" 'Yellow'
    Say '    Re-run: it resumes where it stopped. Or raise -MaxTotalMB.' 'Yellow'
}
Say ("Copied  {0:N0} new file(s), {1:N1} MB." -f $copied, ($bytes / 1MB)) 'Green'
Say ("Skipped {0:N0} already mirrored" -f $skipped)
if ($failed -gt 0) { Say ("Failed  {0:N0} unreadable file(s)" -f $failed) 'Yellow' }
Say ''
Say 'On Hive: /quobyte/proteomics-grp/brett/BrukerDBBackup' 'Cyan'
Say 'The nightly extractor reads the newest *.backup from there.' 'Cyan'
Pause-IfInteractive
