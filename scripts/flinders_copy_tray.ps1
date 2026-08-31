# flinders_copy_tray.ps1
#
# Copies finished timsTOF acquisitions from D:\Data to the timsTOF
# folder in the Flinders archive, filed under the run's own month.
# Sits in the notification area; leave it running.
#
# PURE POWERSHELL. No Python, no pip, no venv, no STAN install, no
# database. The only external program it runs is robocopy.exe, which
# ships with Windows. It reads D:\Data and writes to Flinders; it
# touches nothing else on the PC.
#
# Deliberately small. This runs on a machine that is acquiring data,
# and every line here is a line that can misbehave at 3am.
#
# How it decides a run is finished: a Bruker .d is a DIRECTORY, so
# mtime lies -- the folder timestamp does not move when a file inside
# it grows. Instead we total the file count and byte size of the tree
# and wait for $StableSecs with no change. Same rule the STAN watcher
# uses (stable_secs: 60 in instruments.yml).
#
# Where a run goes: the Flinders tTOF_HT folder is nested by month and
# the spelling drifted over the years -- June26, jun25, JUL26, july26,
# March25, Mar26 all exist. So we look for a folder that already means
# that month and use it, rather than adding another spelling beside it.
# The month comes from the run's own YYYYMMDD name prefix, so a run
# acquired at 23:50 on the 31st is not filed under the next month.
#
# Load on the instrument PC:
#   - idle: one directory listing per minute, nothing else
#   - waiting on a run: totals the size of recent .d folders only
#     (listing a directory does not open the files, so it does not
#     contend with acquisition)
#   - copying: one robocopy at BelowNormal priority with /IPG:20, which
#     paces the transfer so it yields network bandwidth back to the
#     instrument. One copy at a time, spawned in the background and
#     polled, so the tray never freezes on a multi-GB run.
#
# Copy only. The source in D:\Data is never renamed, modified or
# deleted.
#
# Files it keeps, all under %USERPROFILE%\STAN\ :
#   flinders_dest.txt          the destination, asked once
#   logs\flinders_copy.log     what it did
#   logs\flinders_copied.txt   one run name per line, already archived
# Delete flinders_copied.txt to make it re-copy everything in range.
# Delete flinders_dest.txt (or use -AskDest) to change destination.
#
# CLAUDE.md PowerShell 5.1 rules observed: no + string concatenation,
# no inline ternary, no Where-Object pipelines, Join-Path for paths,
# whole-file rewrites only. Tests: tests/test_flinders_copy_tray.ps1

param(
    [switch] $ShowConsole,
    [switch] $AskDest
)

# No StrictMode on purpose. This runs unattended for weeks; a strict
# throw on some unexpected null is worse than carrying on.
$ErrorActionPreference = "Continue"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName Microsoft.VisualBasic

# ---------------- settings ----------------
$SourceDir     = "D:\Data"    # where timsControl writes
$InstrumentDir = "tTOF_HT"    # the timsTOF folder in the Flinders archive
$StableSecs    = 60           # no change for this long = acquisition finished
$PollSecs      = 60           # how often to look
$LookbackHours = 72           # ignore .d folders older than this

$StanDir = Join-Path $env:USERPROFILE "STAN"
$LegacyDir = Join-Path $env:USERPROFILE ".stan"
if ((-not (Test-Path $StanDir)) -and (Test-Path $LegacyDir)) { $StanDir = $LegacyDir }
$LogDir = Join-Path $StanDir "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$LogFile  = Join-Path $LogDir "flinders_copy.log"
$DoneFile = Join-Path $LogDir "flinders_copied.txt"
$DestFile = Join-Path $StanDir "flinders_dest.txt"

# ---------------- state ----------------
$Dest    = ""      # the tTOF_HT folder on Flinders
$Done    = @()     # run names already archived
$Sizes   = @{}     # run name -> "files/bytes" seen last tick
$Marks   = @{}     # run name -> when that size was first seen
$Job     = $null   # the running robocopy, if any
$JobName = ""
$Copies  = 0
$Paused  = $false

function Log($Message) {
    $line = "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) $Message"
    try { Add-Content -LiteralPath $LogFile -Value $line } catch {}
    if ($ShowConsole) { Write-Host $line }
}

# ---------------- destination ----------------

function Resolve-Dest($Root) {
    # Accept a drive letter, the share root, or the tTOF_HT folder
    # itself, and return the tTOF_HT folder. Empty string if not there,
    # so we never invent a tree in the wrong place.
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

function Ask-Dest {
    # Guess by looking through the mapped network drives for one that
    # actually has the instrument folder, then let the operator confirm
    # or correct it. Asked once; remembered in flinders_dest.txt.
    $guess = ""
    foreach ($drive in Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue) {
        if (-not $drive.DisplayRoot) { continue }
        $found = Resolve-Dest "$($drive.Name):"
        if ($found) { $guess = $found; break }
    }
    $prompt = "Where is the timsTOF folder on Flinders?`r`n`r`nUsually a mapped drive, e.g.  Y:\Data\raw_data\$InstrumentDir`r`nA drive letter on its own is fine."
    $answer = [Microsoft.VisualBasic.Interaction]::InputBox($prompt, "STAN - Flinders destination", $guess)
    return (Resolve-Dest $answer)
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
    # Reuse whatever folder already means this month, whatever it is
    # spelled like. Only create one if there is genuinely none.
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

# ---------------- watching ----------------

function Get-Sig($Path) {
    # "files/bytes" for the whole tree. A string, so comparing two of
    # them is just -eq and there is nothing to get wrong. Listing a
    # directory does not open the files, so this does not fight with
    # acquisition.
    $items = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue)
    $bytes = 0
    foreach ($item in $items) { $bytes += $item.Length }
    return "$($items.Count)/$bytes"
}

function Get-Candidates {
    # Recently-touched .d folders we have not archived yet. This is the
    # cheap pass: one top-level listing, no recursion, so a D:\Data
    # holding years of acquisitions costs nothing.
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

function Start-Copy($Dir) {
    $target = Join-Path (Get-MonthDir (Get-Stamp $Dir)) $Dir.Name
    # /IPG:20 paces the transfer so it yields bandwidth to the
    # instrument; /Z survives the share dropping; /FFT matches the
    # timestamp granularity the existing copy_all_data bat uses.
    # The log is quiet (no file or dir listing) but keeps the summary,
    # so a failure is diagnosable from the Hive mirror later.
    $roboLog = Join-Path $LogDir "flinders_robocopy.log"
    $roboArgs = @("`"$($Dir.FullName)`"", "`"$target`"",
                  "/E", "/Z", "/FFT", "/R:2", "/W:10", "/IPG:20",
                  "/NP", "/NFL", "/NDL", "/LOG+:`"$roboLog`"")
    $script:Job = Start-Process robocopy.exe -ArgumentList $roboArgs `
                      -WindowStyle Hidden -PassThru
    try { $script:Job.PriorityClass = "BelowNormal" } catch {}
    $script:JobName = $Dir.Name
    Log "copying $($Dir.Name) -> $target"
}

function Finish-Copy {
    if (-not $Job.HasExited) { return }
    $code = $Job.ExitCode
    $script:Job = $null
    if ($code -ge 8) {
        # robocopy: under 8 is success, 8 and up is a real failure.
        Log "FAILED $JobName (robocopy exit $code)"
        Notify "Copy failed" "$JobName - robocopy exit $code" $true
        return
    }
    Add-Content -LiteralPath $DoneFile -Value $JobName
    $script:Done += $JobName
    $script:Copies += 1
    Log "done $JobName"
    Notify "Copied to Flinders" $JobName $false
}

# ---------------- tray ----------------

function New-Dot($R, $G, $B) {
    $bmp = New-Object System.Drawing.Bitmap 16, 16
    $gfx = [System.Drawing.Graphics]::FromImage($bmp)
    $gfx.SmoothingMode = "AntiAlias"
    $gfx.FillEllipse((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb($R, $G, $B))), 1, 1, 14, 14)
    $gfx.Dispose()
    return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}

$IconIdle = New-Dot 150 150 150
$IconBusy = New-Dot 60 175 90
$IconBad  = New-Dot 210 65 60

function Notify($Title, $Text, $IsError) {
    $kind = "Info"
    if ($IsError) { $kind = "Error" }
    try { $Tray.ShowBalloonTip(6000, $Title, $Text, $kind) } catch {}
}

function Set-State($Summary, $Icon) {
    $Tray.Icon = $Icon
    $text = "STAN Flinders: $Summary"
    if ($text.Length -gt 63) { $text = $text.Substring(0, 63) }   # NotifyIcon caps at 63
    $Tray.Text = $text
    $Status.Text = $Summary
}

# ---------------- the loop ----------------

function Tick {
    if ($Paused) { Set-State "paused" $IconIdle; return }

    if ($Job) { Set-State "copying $JobName" $IconBusy; Finish-Copy; return }

    $now = Get-Date
    $waiting = 0
    foreach ($dir in Get-Candidates) {
        $sig = Get-Sig $dir.FullName
        if ($Sizes[$dir.Name] -ne $sig) {
            # still growing (or first time we have seen it)
            $Sizes[$dir.Name] = $sig
            $Marks[$dir.Name] = $now
            $waiting += 1
            continue
        }
        if (($now - $Marks[$dir.Name]).TotalSeconds -ge $StableSecs) {
            Set-State "copying $($dir.Name)" $IconBusy
            Start-Copy $dir
            return
        }
        $waiting += 1
    }

    if ($waiting -gt 0) { Set-State "$waiting acquiring" $IconIdle }
    else { Set-State "idle - $Copies copied" $IconIdle }
}

# ---------------- startup ----------------

Log "---- started (PID $PID) ----"

if ((Test-Path $DestFile) -and (-not $AskDest)) {
    # Re-check every launch: the operator may have remapped the letter.
    $Dest = Resolve-Dest (Get-Content -LiteralPath $DestFile -TotalCount 1)
    if (-not $Dest) { Log "saved destination is gone or remapped, asking again" }
}
if (-not $Dest) {
    $Dest = Ask-Dest
    if (-not $Dest) {
        [System.Windows.Forms.MessageBox]::Show(
            "That path has no $InstrumentDir folder, so nothing would be copied. Check the Flinders drive is connected, then start this again.",
            "STAN Flinders") | Out-Null
        Log "no usable destination, exiting"
        exit 1
    }
    Set-Content -LiteralPath $DestFile -Value $Dest
}

if (Test-Path $DoneFile) { $Done = @(Get-Content -LiteralPath $DoneFile) }
Log "watching $SourceDir -> $Dest ($($Done.Count) already archived)"

$Status = New-Object System.Windows.Forms.ToolStripMenuItem
$Status.Text = "starting"
$Status.Enabled = $false

$mLog = New-Object System.Windows.Forms.ToolStripMenuItem
$mLog.Text = "Open log folder"
$mLog.Add_Click({ Start-Process explorer.exe $LogDir })

$mPause = New-Object System.Windows.Forms.ToolStripMenuItem
$mPause.Text = "Pause"
$mPause.Add_Click({
    $script:Paused = -not $Paused
    if ($Paused) { $mPause.Text = "Resume" } else { $mPause.Text = "Pause" }
    Log "paused = $Paused"
    Tick
})

$mDest = New-Object System.Windows.Forms.ToolStripMenuItem
$mDest.Text = "Change destination..."
$mDest.Add_Click({
    $picked = Ask-Dest
    if ($picked) {
        $script:Dest = $picked
        Set-Content -LiteralPath $DestFile -Value $picked
        Log "destination changed to $picked"
    }
})

$mExit = New-Object System.Windows.Forms.ToolStripMenuItem
$mExit.Text = "Exit"
$mExit.Add_Click({
    if ($Job) {
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "$JobName is still copying. Exit anyway?", "STAN Flinders", "YesNo", "Question")
        if ($answer -ne "Yes") { return }
    }
    Log "---- stopped ----"
    $Timer.Stop()
    $Tray.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})

$Menu = New-Object System.Windows.Forms.ContextMenuStrip
$Menu.Items.AddRange(@($Status, (New-Object System.Windows.Forms.ToolStripSeparator),
                       $mLog, $mPause, $mDest,
                       (New-Object System.Windows.Forms.ToolStripSeparator), $mExit))

$Tray = New-Object System.Windows.Forms.NotifyIcon
$Tray.Icon = $IconIdle
$Tray.Text = "STAN Flinders"
$Tray.ContextMenuStrip = $Menu
$Tray.Visible = $true
$Tray.Add_MouseDoubleClick({ Start-Process explorer.exe $LogDir })

$Timer = New-Object System.Windows.Forms.Timer
$Timer.Interval = $PollSecs * 1000
$Timer.Add_Tick({
    # One bad tick must never take the tray down.
    try { Tick } catch { Log "error: $($_.Exception.Message)"; Set-State "error - see log" $IconBad }
})
$Timer.Start()

Tick
[System.Windows.Forms.Application]::Run()
