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
# timsControl acquires into month folders already -- D:\Data\Aug26\,
# D:\Data\july26\, D:\Data\June26\ -- and Flinders uses the same
# names, because the archive has always been fed by a robocopy of the
# whole tree. So we MIRROR the local folder rather than working the
# month out from the filename: whatever the operator acquired into is
# where it lands.
#
#     D:\Data\Aug26\<run>.d   ->   <Flinders>\tTOF_HT\Aug26\<run>.d
#     D:\Data\<run>.d          ->   <Flinders>\tTOF_HT\<run>.d
#
# The one wrinkle is spelling. Both sides accumulated several
# spellings of the same month over the years -- June26, jun25, JUL26,
# july26, March25, Mar26 -- so before creating a folder we check
# whether one that MEANS that month is already there and use it. A
# local july26 lands in the archive's JUL26 if that is what exists,
# instead of making a second folder for the same month.
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
    [switch] $Install,      # -SetDest then -RegisterTask
    [switch] $SetDest,      # find + save the destination (needs the mapped drive)
    [switch] $RegisterTask, # create the scheduled task (needs administrator)
    [switch] $Verify,       # is the task actually there?
    [switch] $SkipBacklog,  # mark everything already on disk as done
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

function ConvertTo-Unc($Path) {
    # Store the UNC, not the drive letter. Mapped drives are per-session
    # and an elevated process cannot see them at all, so R:\... written
    # here would be meaningless to the installer's elevated half and
    # fragile for the task later. \\server\share\... always resolves.
    if ($Path -match "^([A-Za-z]):") {
        $drive = Get-PSDrive -Name $Matches[1] -ErrorAction SilentlyContinue
        if ($drive -and $drive.DisplayRoot) {
            $rest = $Path.Substring(2).TrimStart("\")
            return (Join-Path $drive.DisplayRoot $rest)
        }
    }
    return $Path
}

function Find-Dest {
    # Look through the mapped network drives for one that actually has
    # the instrument folder. Returns "" if none does.
    foreach ($drive in Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue) {
        if (-not $drive.DisplayRoot) { continue }
        $found = Resolve-Dest "$($drive.Name):"
        if ($found) { return $found }
    }
    return ""
}

function Get-MirrorDir {
    # Where this PC's state is mirrored for remote troubleshooting:
    # <share>\STAN\<hostname>\ on the proteomics-grp share. Found by
    # looking for the folder rather than trusting a drive letter, the
    # same way the Flinders destination is.
    $want = Join-Path "STAN" $env:COMPUTERNAME
    foreach ($drive in Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue) {
        if (-not $drive.DisplayRoot) { continue }
        $try = Join-Path "$($drive.Name):" $want
        if (Test-Path -LiteralPath $try -PathType Container) { return (ConvertTo-Unc $try) }
    }
    return ""
}

function Publish-Logs($Changed) {
    # Push the logs to the mirror so they can be read from Hive without
    # anyone touching this PC.
    #
    # We do this ourselves rather than relying on STAN's
    # sync_to_hive_mirror: that is Python, this script deliberately has
    # no Python dependency, and on TIMS-10878 the mirror's logs folder
    # had not been updated since 11 Aug 2026 anyway.
    #
    # The status file goes every pass -- it is one line, and its
    # timestamp is the proof the task is alive. The bulkier files only
    # go when something actually happened, so an idle instrument is not
    # pushing hundreds of KB over SMB every five minutes.
    #
    # Never allowed to break a pass: a dead share must not stop a copy.
    try {
        $dir = Get-MirrorDir
        if (-not $dir) { return }
        $target = Join-Path $dir "flinders"
        if (-not (Test-Path -LiteralPath $target)) {
            New-Item -ItemType Directory -Path $target -Force -ErrorAction Stop | Out-Null
        }
        $send = @($StatusFile)
        if ($Changed) { $send = @($StatusFile, $LogFile, $DoneFile, (Join-Path $LogDir "flinders_robocopy.log")) }
        foreach ($file in $send) {
            if (Test-Path -LiteralPath $file) {
                Copy-Item -LiteralPath $file -Destination $target -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {}
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

function Get-DestFolder($LocalName) {
    # Mirror the folder the operator acquired into. If the archive
    # already has a folder MEANING that month under a different
    # spelling, use that one instead of making a second -- a local
    # july26 goes into the archive's JUL26 if that is what is there.
    if (-not $LocalName) { return $Dest }
    $existing = @(Get-ChildItem -LiteralPath $Dest -Directory -Force -ErrorAction SilentlyContinue)
    foreach ($dir in $existing) {
        if ($dir.Name -eq $LocalName) { return $dir.FullName }   # -eq ignores case
    }
    $asDate = Get-MonthDate $LocalName
    if ($asDate) {
        foreach ($dir in $existing) {
            $other = Get-MonthDate $dir.Name
            if ($other -and $other.Year -eq $asDate.Year -and $other.Month -eq $asDate.Month) {
                return $dir.FullName
            }
        }
    }
    $new = Join-Path $Dest $LocalName
    New-Item -ItemType Directory -Path $new -Force | Out-Null
    Log "created folder $LocalName"
    return $new
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

function New-Candidate($Dir, $Parent) {
    # Rel is the path relative to D:\Data -- "Aug26\run.d", or just
    # "run.d" for one sitting at the root. It is both where the run
    # goes on Flinders and the key we remember it by, so two runs with
    # the same name in different months cannot be confused.
    $rel = $Dir.Name
    if ($Parent) { $rel = Join-Path $Parent $Dir.Name }
    $item = New-Object PSObject
    Add-Member -InputObject $item -MemberType NoteProperty -Name Dir    -Value $Dir
    Add-Member -InputObject $item -MemberType NoteProperty -Name Rel    -Value $rel
    Add-Member -InputObject $item -MemberType NoteProperty -Name Parent -Value $Parent
    return $item
}

function Test-StillWriting($Path) {
    # Is the instrument still writing into this .d?
    #
    # The size-across-passes check is the primary signal, but it has a
    # gap: a .d can sit almost unchanged for several minutes early in a
    # run -- during LC equilibration, before MS data starts flowing --
    # and copying then would archive a truncated run PERMANENTLY,
    # because it gets marked done and never looked at again.
    #
    # analysis.tdf_bin is the file that grows (~680 MB for a 100 SPD
    # HeLa run) and timsControl holds it open for the whole
    # acquisition, so if we cannot get an exclusive handle, something
    # still has it. That is an OS-level question, not a guess about
    # Bruker's format. We never actually hold the lock during a run --
    # the open fails outright -- and we release it immediately when it
    # succeeds.
    #
    # A wash can finish without ever writing one, so a missing
    # analysis.tdf_bin is NOT read as "still writing"; those fall back
    # to the size check alone.
    $bin = Join-Path $Path "analysis.tdf_bin"
    if (-not (Test-Path -LiteralPath $bin)) { return $false }
    try {
        $handle = [System.IO.File]::Open($bin, "Open", "Read", "None")
        $handle.Close()
        $handle.Dispose()
        return $false
    } catch {
        return $true
    }
}

function Get-Candidates {
    # Recently-touched .d folders we have not archived yet.
    #
    # timsControl acquires into month folders (D:\Data\Aug26\...), so
    # a top-level-only scan finds almost nothing -- which is exactly
    # what the first version of this did. We look one level down as
    # well, and never descend INTO a .d: it is the thing we are looking
    # for, and it is full of files.
    $out = @()
    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) { return $out }
    $cutoff = (Get-Date).AddHours(-$LookbackHours)

    $found = @()
    foreach ($top in @(Get-ChildItem -LiteralPath $SourceDir -Directory -Force -ErrorAction SilentlyContinue)) {
        if ($top.Extension -eq ".d") {
            $found += (New-Candidate $top "")
            continue
        }
        foreach ($sub in @(Get-ChildItem -LiteralPath $top.FullName -Directory -Force -ErrorAction SilentlyContinue)) {
            if ($sub.Extension -ne ".d") { continue }
            $found += (New-Candidate $sub $top.Name)
        }
    }

    foreach ($item in $found) {
        if ($item.Dir.LastWriteTime -lt $cutoff) { continue }
        # -contains ignores case, which Windows paths need. The bare
        # name is checked too, so a done list seeded from an older
        # top-level-only run still counts.
        if ($Done -contains $item.Rel) { continue }
        if ($Done -contains $item.Dir.Name) { continue }
        $out += $item
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

function Copy-Run($Item) {
    $Dir = $Item.Dir
    $target = Join-Path (Get-DestFolder $Item.Parent) $Dir.Name
    # /IPG:20 paces the transfer so it yields bandwidth back to the
    # instrument; /Z survives the share dropping mid-copy; /FFT matches
    # the timestamp granularity the existing copy_all_data bat uses.
    $roboLog = Join-Path $LogDir "flinders_robocopy.log"
    $roboArgs = @("`"$($Dir.FullName)`"", "`"$target`"",
                  "/E", "/Z", "/FFT", "/R:2", "/W:10", "/IPG:20",
                  "/NP", "/NFL", "/NDL", "/LOG+:`"$roboLog`"")
    Log "copying $($Item.Rel) -> $target"
    $proc = Start-Process robocopy.exe -ArgumentList $roboArgs -WindowStyle Hidden -PassThru
    try { $proc.PriorityClass = "BelowNormal" } catch {}
    $proc.WaitForExit()
    if ($proc.ExitCode -ge 8) {
        # robocopy: under 8 is success, 8 and up is a real failure.
        Log "FAILED $($Item.Rel) (robocopy exit $($proc.ExitCode))"
        return $false
    }
    Add-Content -LiteralPath $DoneFile -Value $Item.Rel
    Log "done $($Item.Rel)"
    return $true
}

# ---------------- install / uninstall ----------------

function Set-Destination {
    # Non-elevated on purpose: this is the half that needs to see the
    # operator's mapped drives.
    $installed = Join-Path $StanDir "flinders_copy.ps1"
    if ($PSCommandPath -ne $installed) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $installed -Force
    }
    Write-Host "  Installed script: $installed"

    Add-Type -AssemblyName Microsoft.VisualBasic
    $guess = ""
    if (Test-Path $DestFile) { $guess = Resolve-Dest (Get-Content -LiteralPath $DestFile -TotalCount 1) }
    if (-not $guess) { $guess = Find-Dest }
    $prompt = "Where is the timsTOF folder on Flinders?`r`n`r`nUsually a mapped drive, e.g.  R:\Data\raw_data\$InstrumentDir`r`nA drive letter on its own is fine."
    $answer = [Microsoft.VisualBasic.Interaction]::InputBox($prompt, "STAN Flinders Copy", $guess)
    $resolved = Resolve-Dest $answer
    if (-not $resolved) {
        Write-Host ""
        Write-Host "  No $InstrumentDir folder under '$answer'." -ForegroundColor Red
        Write-Host "  Check the Flinders drive is connected, then run this again."
        return $false
    }

    # Save the UNC so the task is not hostage to a drive mapping.
    $unc = ConvertTo-Unc $resolved
    if ($unc -ne $resolved) {
        if (Test-Path -LiteralPath $unc -PathType Container) {
            Write-Host "  Destination:      $resolved"
            Write-Host "                    stored as $unc"
            $resolved = $unc
        } else {
            Write-Host "  Destination:      $resolved (UNC $unc not reachable, keeping the letter)"
        }
    } else {
        Write-Host "  Destination:      $resolved"
    }
    Set-Content -LiteralPath $DestFile -Value $resolved
    Log "destination set to $resolved"
    return $true
}

function Test-Task {
    $found = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($found) { return $true }
    return $false
}

function Test-Elevated {
    $me = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent())
    return $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Register-Task {
    # Creating a task needs an elevated token, even for an account in
    # the Administrators group -- UAC hands a filtered one to a normal
    # process, which is why this failed the first time with
    # 0x80070005. Only THIS half elevates: an elevated process cannot
    # see the operator's mapped network drives, so the destination has
    # to have been found already, by Set-Destination, unelevated.
    $installed = Join-Path $StanDir "flinders_copy.ps1"
    if (-not (Test-Path -LiteralPath $installed)) {
        Write-Host "  $installed is missing -- run the destination step first." -ForegroundColor Red
        return $false
    }

    if (-not (Test-Elevated)) {
        Write-Host ""
        Write-Host "  Creating the scheduled task needs administrator rights."
        Write-Host "  Say Yes to the Windows prompt that is about to appear."
        # Start-Process quotes an array argument list properly, which is
        # why this re-launch lives here and not in the .bat.
        $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-File", $installed, "-RegisterTask")
        try {
            Start-Process powershell.exe -Verb RunAs -ArgumentList $relaunch -Wait -ErrorAction Stop
        } catch {
            Write-Host ""
            Write-Host "  The administrator prompt was refused or cancelled." -ForegroundColor Red
            Write-Host "  Right-click install_flinders_copy.bat and pick"
            Write-Host "  'Run as administrator' to try again."
            return $false
        }
        if (Test-Task) {
            Write-Host "  Scheduled task:   $TaskName, every $EveryMinutes minutes"
            return $true
        }
        Write-Host "  The task still is not there after elevating." -ForegroundColor Red
        return $false
    }

    # Runs as the logged-on user ON PURPOSE, never SYSTEM: the copy has
    # to happen in a session that can reach the Flinders share.
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$installed`""
    $atLogon = New-ScheduledTaskTrigger -AtLogOn
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries

    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action `
            -Trigger @($atLogon, $repeat) -Settings $settings -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Host ""
        Write-Host "  Could not create the scheduled task: $($_.Exception.Message)" -ForegroundColor Red
        return $false
    }

    # Never claim success without looking. The first version of this
    # printed "Done" after the registration had already failed.
    if (-not (Test-Task)) {
        Write-Host "  The task did not appear after registering." -ForegroundColor Red
        return $false
    }
    Write-Host "  Scheduled task:   $TaskName, every $EveryMinutes minutes"
    Log "registered scheduled task"
    return $true
}

function Uninstall-Task {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "  Removed scheduled task: $TaskName"
    Log "uninstalled scheduled task"
}

# ---------------- one pass ----------------

if ($Uninstall) { Uninstall-Task; exit 0 }

if ($Verify) {
    if (Test-Task) { Write-Host "  Scheduled task is registered."; exit 0 }
    Write-Host "  Scheduled task is NOT registered." -ForegroundColor Red
    exit 1
}

if ($SetDest) {
    if (Set-Destination) { exit 0 }
    exit 1
}

if ($RegisterTask) {
    if (Register-Task) { exit 0 }
    exit 1
}

if ($Install) {
    if (-not (Set-Destination)) { exit 1 }
    if (-not (Register-Task)) { exit 1 }
    exit 0
}

if ($SkipBacklog) {
    # Everything currently in D:\Data counts as already archived, so
    # only genuinely new acquisitions get copied from here on.
    $Done = @()
    if (Test-Path $DoneFile) { $Done = @(Get-Content -LiteralPath $DoneFile) }
    $Dest = "x"   # Get-Candidates does not need a real destination
    $marked = 0
    foreach ($item in Get-Candidates) {
        Add-Content -LiteralPath $DoneFile -Value $item.Rel
        $marked += 1
    }
    Write-Host "  Marked $marked existing run(s) as already archived."
    Write-Host "  Only new acquisitions will be copied."
    Log "skip-backlog marked $marked runs"
    exit 0
}

if (Test-Path $PauseFile) {
    Set-Status "paused (delete $PauseFile to resume)"
    exit 0
}

$Dest = ""
if (Test-Path $DestFile) {
    # Re-checked every pass: the share may be down or remapped.
    $Dest = Resolve-Dest (Get-Content -LiteralPath $DestFile -TotalCount 1)
}
if (-not $Dest) {
    # Self-heal: if the saved path has gone, look for the instrument
    # folder on whatever is mapped right now before giving up.
    $Dest = Find-Dest
    if ($Dest) { Log "saved destination unreachable, using $Dest instead" }
}
if (-not $Dest) {
    Set-Status "NO DESTINATION - Flinders drive missing or not set up"
    Log "destination unavailable, nothing copied"
    Publish-Logs $true
    exit 1
}

$Done = @()
if (Test-Path $DoneFile) { $Done = @(Get-Content -LiteralPath $DoneFile) }

$was = Read-Map $SizeFile
$now = @{}
$ready = @()
$growing = 0

foreach ($item in Get-Candidates) {
    $sig = Get-Sig $item.Dir.FullName
    $now[$item.Rel] = $sig
    # Two independent signals have to agree before we touch it: the
    # tree stopped changing, AND nothing holds the data file open.
    if ($was[$item.Rel] -eq $sig -and -not (Test-StillWriting $item.Dir.FullName)) {
        $ready += $item
    } else {
        $growing += 1
    }
}
Write-Map $SizeFile $now

$copied = 0
foreach ($item in $ready) {
    if (Copy-Run $item) { $copied += 1 }
}

$summary = "ok - $copied copied, $growing still acquiring, $($Done.Count) archived total"
if ($copied -gt 0) { Log $summary }
Set-Status $summary
Publish-Logs ($copied -gt 0)
exit 0
