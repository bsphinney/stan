# flinders_copy.ps1
#
# Copies finished timsTOF acquisitions from D:\Data to the timsTOF
# folder in the Flinders archive, filed under the run's own month.
#
# Runs as a Windows scheduled task every 5 minutes. Nothing stays
# resident between runs: the process lives for a few seconds, does its
# work, and exits. It cannot leak, hang or be left paused by accident,
# and it comes back by itself after a reboot.
#
# PURE POWERSHELL. No Python, no pip, no venv, no STAN install, no
# database. The only external program it runs is robocopy.exe, which
# ships with Windows. It reads D:\Data and writes to Flinders; it
# touches nothing else.
#
# Set it up (double-click install_flinders_copy.bat, or):
#     powershell -ExecutionPolicy Bypass -File flinders_copy.ps1 -Install
# Remove it:
#     powershell -ExecutionPolicy Bypass -File flinders_copy.ps1 -Uninstall
# Run one pass by hand and watch it:
#     powershell -ExecutionPolicy Bypass -File flinders_copy.ps1 -Show
#
# HOW IT KNOWS A RUN IS FINISHED
# A Bruker .d is a DIRECTORY, and its timestamp does not move when a
# file inside it grows, so mtime cannot be trusted. Instead each run
# records the file count and byte total of the whole tree, and a run is
# copied when that total is identical to what the previous pass saw
# five minutes earlier. Five minutes of no growth is a stronger signal
# than the 60 seconds the resident watcher uses, so a partial .d is
# less likely to be copied. The trade is up to ~10 minutes of latency
# between the end of acquisition and the copy, which does not matter
# for an archive.
#
# WHERE A RUN GOES
# The Flinders tTOF_HT folder is nested by month, and the spelling
# drifted over the years -- June26, jun25, JUL26, july26, March25 and
# Mar26 all exist. So we look for a folder that already means that
# month and use it, rather than adding another spelling beside it. The
# month comes from the run's own YYYYMMDD name prefix, so a run
# acquired at 23:50 on the 31st is not filed under the next month.
#
# LOAD ON AN ACQUIRING PC
# Per pass: one directory listing of D:\Data, then a recursive size
# total of only those .d folders touched in the last 72 hours. Listing
# a directory does not open the files, so it does not contend with
# acquisition. Copies run one at a time through robocopy at
# BelowNormal priority with /IPG:20, which paces the transfer so it
# yields network bandwidth back to the instrument.
#
# COPY ONLY. The source in D:\Data is never renamed, modified or
# deleted.
#
# FILES IT KEEPS, all under %USERPROFILE%\STAN\ :
#   flinders_dest.txt           the destination, asked once at install
#   flinders_pause.txt          create this to pause; delete to resume
#   logs\flinders_copy.log      appended only when something happens
#   logs\flinders_status.txt    overwritten every pass: proof of life
#   logs\flinders_copied.txt    one run name per line, already archived
#   logs\flinders_sizes.txt     last pass's tree sizes, for the compare
# Delete flinders_copied.txt to make it re-copy everything in range.
#
# The logs live under the tree that syncs to the Hive mirror, so a
# failure is diagnosable remotely without touching the instrument PC.
#
# CLAUDE.md PowerShell 5.1 rules observed: no + string concatenation,
# no inline ternary, no Where-Object pipelines, Join-Path for paths,
# whole-file rewrites only. Tests: tests/test_flinders_copy.ps1

param(
    [switch] $Install,
    [switch] $Uninstall,
    [switch] $Show
)

$ErrorActionPreference = "Continue"

# ---------------- settings ----------------
$SourceDir     = "D:\Data"    # where timsControl writes
$InstrumentDir = "tTOF_HT"    # the timsTOF folder in the Flinders archive
$LookbackHours = 72           # ignore .d folders older than this
$TaskName      = "STAN Flinders Copy"
$EveryMinutes  = 5

$StanDir = Join-Path $env:USERPROFILE "STAN"
$LegacyDir = Join-Path $env:USERPROFILE ".stan"
if ((-not (Test-Path $StanDir)) -and (Test-Path $LegacyDir)) { $StanDir = $LegacyDir }
$LogDir = Join-Path $StanDir "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$DestFile   = Join-Path $StanDir "flinders_dest.txt"
$PauseFile  = Join-Path $StanDir "flinders_pause.txt"
$LogFile    = Join-Path $LogDir  "flinders_copy.log"
$StatusFile = Join-Path $LogDir  "flinders_status.txt"
$DoneFile   = Join-Path $LogDir  "flinders_copied.txt"
$SizeFile   = Join-Path $LogDir  "flinders_sizes.txt"

function Log($Message) {
    # Only real events land here, never an idle pass -- at 288 passes a
    # day a chatty log would bury the one line that matters.
    $line = "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) $Message"
    try { Add-Content -LiteralPath $LogFile -Value $line } catch {}
    if ($Show) { Write-Host $line }
}

function Set-Status($Message) {
    # Overwritten every pass, so its timestamp is proof the task is
    # still running even when the log has been quiet for days.
    try {
        Set-Content -LiteralPath $StatusFile -Value "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))  $Message"
    } catch {}
    if ($Show) { Write-Host $Message }
}

# ---------------- destination ----------------

function Resolve-Dest($Root) {
    # Accept a drive letter, the share root, or the tTOF_HT folder
    # itself. Empty string if it is not there, so we never invent a
    # tree in the wrong place.
    if (-not $Root) { return "" }
    $r = $Root.Trim().TrimEnd("\")
    if ($r -match "^[A-Za-z]:$") { $r = "$r\" }
    if ((Split-Path -Leaf $r) -eq $InstrumentDir) {
        if (Test-Path -LiteralPath $r -PathType Container) { return $r }
        return ""
    }
    foreach ($suffix in @("Data\raw_data\$InstrumentDir", "raw_data\$InstrumentDir", $InstrumentDir)) {
        $try = Join-Path $r $suffix
        if (Test-Path -LiteralPath $try -PathType Container) { return $try }
    }
    return ""
}

# ---------------- month folder ----------------

function Get-MonthDate($Name) {
    # Is this folder name a month? Returns a date, or $null.
    # One format at a time: TryParseExact has a string[] overload, but
    # PowerShell binds a PS array to the single-format overload and
    # stringifies it to "System.Object[]", so the array form matches
    # nothing at all.
    foreach ($fmt in @("MMMyy", "MMMMyy")) {
        $d = [datetime]::MinValue
        if ([datetime]::TryParseExact($Name, $fmt,
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::None, [ref] $d)) {
            return $d
        }
    }
    return $null
}

function Get-MonthDir($Stamp) {
    # Reuse whatever folder already means this month, however it is
    # spelled. Only create one if there is genuinely none.
    $want = $Stamp.ToString("MMMyy", [System.Globalization.CultureInfo]::InvariantCulture)
    $existing = @(Get-ChildItem -LiteralPath $Dest -Directory -Force -ErrorAction SilentlyContinue)
    foreach ($dir in $existing) {
        if ($dir.Name -eq $want) { return $dir.FullName }
    }
    foreach ($dir in $existing) {
        $asDate = Get-MonthDate $dir.Name
        if ($asDate -and $asDate.Year -eq $Stamp.Year -and $asDate.Month -eq $Stamp.Month) {
            return $dir.FullName
        }
    }
    $new = Join-Path $Dest $want
    New-Item -ItemType Directory -Path $new -Force | Out-Null
    Log "created month folder $want"
    return $new
}

function Get-Stamp($Dir) {
    # Prefer the run's own date prefix over the folder mtime.
    if ($Dir.Name -match "^(\d{8})") {
        $d = [datetime]::MinValue
        if ([datetime]::TryParseExact($Matches[1], "yyyyMMdd",
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::None, [ref] $d)) {
            return $d
        }
    }
    return $Dir.LastWriteTime
}

# ---------------- looking at D:\Data ----------------

function Get-Sig($Path) {
    # "files/bytes" for the whole tree. A string, so comparing two of
    # them is just -eq and there is nothing to get wrong.
    $items = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue)
    $bytes = 0
    foreach ($item in $items) { $bytes += $item.Length }
    return "$($items.Count)/$bytes"
}

function Get-Candidates {
    # Recently-touched .d folders we have not archived yet. The cheap
    # pass: one top-level listing, no recursion, so a D:\Data holding
    # years of acquisitions costs nothing.
    $out = @()
    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) { return $out }
    $cutoff = (Get-Date).AddHours(-$LookbackHours)
    foreach ($dir in @(Get-ChildItem -LiteralPath $SourceDir -Directory -Force -ErrorAction SilentlyContinue)) {
        if ($dir.Extension -ne ".d") { continue }
        if ($dir.LastWriteTime -lt $cutoff) { continue }
        if ($Done -contains $dir.Name) { continue }   # -contains ignores case
        $out += $dir
    }
    return $out
}

function Read-Map($Path) {
    # name<TAB>signature, one per line. PowerShell does not enumerate a
    # Hashtable on return the way it unrolls a list, so this comes back
    # whole.
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)) {
        $split = $line.IndexOf("`t")
        if ($split -gt 0) { $map[$line.Substring(0, $split)] = $line.Substring($split + 1) }
    }
    return $map
}

function Write-Map($Path, $Map) {
    # Only current candidates are written back, so this cannot grow
    # without bound.
    $lines = @()
    foreach ($key in $Map.Keys) { $lines += "$key`t$($Map[$key])" }
    try { Set-Content -LiteralPath $Path -Value $lines } catch {}
}

function Copy-Run($Dir) {
    $target = Join-Path (Get-MonthDir (Get-Stamp $Dir)) $Dir.Name
    # /IPG:20 paces the transfer so it yields bandwidth back to the
    # instrument; /Z survives the share dropping mid-copy; /FFT matches
    # the timestamp granularity the existing copy_all_data bat uses.
    $roboLog = Join-Path $LogDir "flinders_robocopy.log"
    $roboArgs = @("`"$($Dir.FullName)`"", "`"$target`"",
                  "/E", "/Z", "/FFT", "/R:2", "/W:10", "/IPG:20",
                  "/NP", "/NFL", "/NDL", "/LOG+:`"$roboLog`"")
    Log "copying $($Dir.Name) -> $target"
    $proc = Start-Process robocopy.exe -ArgumentList $roboArgs -WindowStyle Hidden -PassThru
    try { $proc.PriorityClass = "BelowNormal" } catch {}
    $proc.WaitForExit()
    if ($proc.ExitCode -ge 8) {
        # robocopy: under 8 is success, 8 and up is a real failure.
        Log "FAILED $($Dir.Name) (robocopy exit $($proc.ExitCode))"
        return $false
    }
    Add-Content -LiteralPath $DoneFile -Value $Dir.Name
    Log "done $($Dir.Name)"
    return $true
}

# ---------------- install / uninstall ----------------

function Install-Task {
    # Copy ourselves somewhere stable first. A task pointing at a
    # script in someone's Downloads folder breaks the day it is tidied.
    $installed = Join-Path $StanDir "flinders_copy.ps1"
    if ($PSCommandPath -ne $installed) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $installed -Force
    }
    Write-Host "  Installed script: $installed"

    # Ask where Flinders is, guessing from the mapped network drives.
    Add-Type -AssemblyName Microsoft.VisualBasic
    $guess = ""
    if (Test-Path $DestFile) { $guess = Resolve-Dest (Get-Content -LiteralPath $DestFile -TotalCount 1) }
    if (-not $guess) {
        foreach ($drive in Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue) {
            if (-not $drive.DisplayRoot) { continue }
            $found = Resolve-Dest "$($drive.Name):"
            if ($found) { $guess = $found; break }
        }
    }
    $prompt = "Where is the timsTOF folder on Flinders?`r`n`r`nUsually a mapped drive, e.g.  Y:\Data\raw_data\$InstrumentDir`r`nA drive letter on its own is fine."
    $answer = [Microsoft.VisualBasic.Interaction]::InputBox($prompt, "STAN Flinders Copy", $guess)
    $resolved = Resolve-Dest $answer
    if (-not $resolved) {
        Write-Host ""
        Write-Host "  No $InstrumentDir folder under '$answer'." -ForegroundColor Red
        Write-Host "  Check the Flinders drive is connected, then run this again."
        return $false
    }
    Set-Content -LiteralPath $DestFile -Value $resolved
    Write-Host "  Destination:      $resolved"

    # Runs as the logged-on user ON PURPOSE. Mapped network drives are
    # per-session, so a task running as SYSTEM would not be able to see
    # the Flinders drive at all.
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$installed`""
    $atLogon = New-ScheduledTaskTrigger -AtLogOn
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger @($atLogon, $repeat) -Settings $settings -Force | Out-Null

    Write-Host "  Scheduled task:   $TaskName, every $EveryMinutes minutes"
    Log "installed scheduled task, destination $resolved"
    return $true
}

function Uninstall-Task {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "  Removed scheduled task: $TaskName"
    Log "uninstalled scheduled task"
}

# ---------------- one pass ----------------

if ($Uninstall) { Uninstall-Task; exit 0 }

if ($Install) {
    if (Install-Task) { exit 0 }
    exit 1
}

if (Test-Path $PauseFile) {
    Set-Status "paused (delete $PauseFile to resume)"
    exit 0
}

$Dest = ""
if (Test-Path $DestFile) {
    # Re-checked every pass: the operator may have remapped the letter.
    $Dest = Resolve-Dest (Get-Content -LiteralPath $DestFile -TotalCount 1)
}
if (-not $Dest) {
    Set-Status "NO DESTINATION - Flinders drive missing or not set up"
    Log "destination unavailable, nothing copied"
    exit 1
}

$Done = @()
if (Test-Path $DoneFile) { $Done = @(Get-Content -LiteralPath $DoneFile) }

$was = Read-Map $SizeFile
$now = @{}
$ready = @()
$growing = 0

foreach ($dir in Get-Candidates) {
    $sig = Get-Sig $dir.FullName
    $now[$dir.Name] = $sig
    if ($was[$dir.Name] -eq $sig) { $ready += $dir } else { $growing += 1 }
}
Write-Map $SizeFile $now

$copied = 0
foreach ($dir in $ready) {
    if (Copy-Run $dir) { $copied += 1 }
}

$summary = "ok - $copied copied, $growing still acquiring, $($Done.Count) archived total"
if ($copied -gt 0) { Log $summary }
Set-Status $summary
exit 0
