# flinders_copy_tray.ps1
#
# System-tray watcher for the timsTOF HT instrument PC (TIMS-10878).
#
# Watches D:\Data for Bruker .d acquisition directories, waits until each
# one has finished being written, then copies it to the timsTOF raw-data
# directory on the Flinders share using the archive's own month-folder
# naming convention (tTOF_HT\Aug26\<run>.d).
#
# Runs as a notification-area (system tray) icon with no console window.
# Leave it open; it polls quietly in the background.
#
#   Gray icon   = idle / nothing to do
#   Blue icon   = watching a .d that is still growing
#   Green icon  = copy in progress
#   Red icon    = last copy failed (see log)
#
# Completion detection (mirrors STAN's own rule -- see CLAUDE.md,
# "File stability detection -- vendor-specific"): a Bruker .d is a
# DIRECTORY, not a file, so we poll total directory size every 10 s and
# only copy after $StableSecs (default 60) with no change in byte count
# or file count. Never trust mtime alone on a .d.
#
# Low-impact by design:
#   - only .d directories newer than $LookbackHours are ever measured,
#     so a D:\Data holding years of acquisitions costs one cheap
#     top-level listing per tick, not a recursive walk
#   - robocopy runs with /IPG:20 (inter-packet gap) so the copy yields
#     network bandwidth to the instrument
#   - the robocopy child process is set to BelowNormal priority
#   - exactly one copy runs at a time, never concurrent
#   - the copy is spawned async and polled, so the tray icon stays
#     responsive during a multi-GB transfer
#
# Copy, never move. The source .d in D:\Data is never modified, renamed
# or deleted. A JSON state file records what has already been archived
# so a restart does not re-copy.
#
# Config is remembered at %USERPROFILE%\STAN\flinders_copy_config.json.
# Logs go to %USERPROFILE%\STAN\logs\flinders_copy_<date>.log, which is
# inside the tree that already syncs to the Hive mirror, so copy
# failures are diagnosable remotely without asking the operator for a
# screenshot.
#
# CLAUDE.md PowerShell 5.1 rules observed:
#   - No + string concatenation; all string building via interpolation
#   - No inline ternary if
#   - No Where-Object pipelines; explicit foreach throughout
#   - Join-Path instead of path string concatenation
#   - Full-file rewrite on every edit -- never patch single lines
#
# Usage:
#   flinders_copy_tray.bat                  (normal launch, hidden)
#   powershell -File flinders_copy_tray.ps1 -ShowConsole
#   powershell -File flinders_copy_tray.ps1 -Reconfigure
#   powershell -File flinders_copy_tray.ps1 -SourceDir "E:\Data"

param(
    [string] $SourceDir   = "",
    [string] $DestPath    = "",
    [int]    $StableSecs  = 0,
    [switch] $Reconfigure,
    [switch] $ShowConsole
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

# Capture the parameters FIRST. A script's parameters live in script
# scope, so the `$script:SourceDir = ""` initialiser further down is the
# same variable as `-SourceDir` and would silently discard whatever the
# operator passed. Copy them out before anything touches script scope.
$argSourceDir  = $SourceDir
$argDestPath   = $DestPath
$argStableSecs = $StableSecs

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# ------------------------------------------------------------------
# Hide the console window. The .bat launcher already passes
# -WindowStyle Hidden, but a console still flashes when the script is
# started by hand, and PowerShell ISE / direct invocation leaves one up.
# ------------------------------------------------------------------
if (-not $ShowConsole) {
    try {
        Add-Type -Name TrayWin -Namespace StanNative -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
"@
        $consoleHandle = [StanNative.TrayWin]::GetConsoleWindow()
        if ($consoleHandle -ne [IntPtr]::Zero) {
            [void] [StanNative.TrayWin]::ShowWindow($consoleHandle, 0)
        }
    } catch {}
}

# ------------------------------------------------------------------
# Tunables
# ------------------------------------------------------------------
$script:AppName        = "STAN Flinders Copier"
$script:InstrumentDir  = "tTOF_HT"          # timsTOF HT folder in the Flinders archive
$script:DefaultSource  = "D:\Data"          # matches instruments.yml watch_dir
$script:DefaultStable  = 60                 # matches instruments.yml stable_secs
$script:PollSeconds    = 10                 # matches STAN's .d size-check cadence
$script:LookbackHours  = 72                 # only consider recently-touched .d dirs
$script:Ipg            = 20                 # robocopy inter-packet gap (ms)
$script:RobocopyRetry  = 2
$script:RobocopyWait   = 10
$script:ShowConsoleMode = [bool] $ShowConsole

# Known Flinders share fingerprints, used to preselect the right drive.
$script:UncHints = @("protcore", "169.237.96.1", "flinders")

# Where the instrument dir might sit under whatever the operator mapped.
$script:DestSuffixes = @(
    (Join-Path "Data\raw_data" $script:InstrumentDir),
    (Join-Path "raw_data" $script:InstrumentDir),
    $script:InstrumentDir
)

# ------------------------------------------------------------------
# Resolve STAN's user dir. v0.2.347+ installs use %USERPROFILE%\STAN;
# legacy installs used %USERPROFILE%\.stan. Match whichever exists so
# the log lands in the tree that syncs to Hive.
# ------------------------------------------------------------------
$modernDir = Join-Path $env:USERPROFILE "STAN"
$legacyDir = Join-Path $env:USERPROFILE ".stan"
if (Test-Path -LiteralPath $modernDir) {
    $script:StanDir = $modernDir
} elseif (Test-Path -LiteralPath $legacyDir) {
    $script:StanDir = $legacyDir
} else {
    $script:StanDir = $modernDir
}
if (-not (Test-Path -LiteralPath $script:StanDir)) {
    New-Item -ItemType Directory -Path $script:StanDir -Force | Out-Null
}

$script:LogDir     = Join-Path $script:StanDir "logs"
if (-not (Test-Path -LiteralPath $script:LogDir)) {
    New-Item -ItemType Directory -Path $script:LogDir -Force | Out-Null
}
$script:ConfigPath = Join-Path $script:StanDir "flinders_copy_config.json"
$script:StatePath  = Join-Path $script:StanDir "flinders_copy_state.json"
$script:LogPath    = ""

# ------------------------------------------------------------------
# Mutable run state (initialised up front -- StrictMode 2.0)
# ------------------------------------------------------------------
$script:SourceDir   = ""
$script:DestRoot    = ""
$script:StableSecs  = $script:DefaultStable
$script:Paused      = $false
$script:Copied      = $null   # HashSet[string] of archived .d names
$script:Sizes       = @{}     # name -> stability record
$script:ActiveCopy  = $null   # in-flight copy record
$script:LastError   = ""
$script:CopyCount   = 0
$script:Notify      = $null
$script:Timer       = $null
$script:MenuStatus  = $null
$script:MenuPause   = $null
$script:IconIdle    = $null
$script:IconWatch   = $null
$script:IconCopy    = $null
$script:IconError   = $null

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
function Write-Log {
    param(
        [string] $Message,
        [string] $Level = "INFO"
    )
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $day = (Get-Date).ToString("yyyyMMdd")
    $script:LogPath = Join-Path $script:LogDir "flinders_copy_$day.log"
    $line = "$stamp [$Level] $Message"
    try {
        Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
    } catch {}
    if ($script:ShowConsoleMode) {
        Write-Host $line
    }
}

# ------------------------------------------------------------------
# JSON helpers. ConvertFrom-Json returns PSCustomObject; under
# StrictMode a missing property throws, so always go through this.
# ------------------------------------------------------------------
function Get-JsonProp {
    param(
        $Obj,
        [string] $Name
    )
    if ($null -eq $Obj) { return $null }
    $prop = $Obj.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

function Read-ConfigFile {
    if (-not (Test-Path -LiteralPath $script:ConfigPath)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $script:ConfigPath -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
        return (ConvertFrom-Json $raw)
    } catch {
        Write-Log "Config unreadable, ignoring: $($_.Exception.Message)" "WARN"
        return $null
    }
}

function Save-ConfigFile {
    $obj = @{
        sourceDir     = $script:SourceDir
        destRoot      = $script:DestRoot
        stableSecs    = $script:StableSecs
        lookbackHours = $script:LookbackHours
    }
    try {
        $json = ConvertTo-Json $obj
        Set-Content -LiteralPath $script:ConfigPath -Value $json -Encoding UTF8
    } catch {
        Write-Log "Could not save config: $($_.Exception.Message)" "WARN"
    }
}

function Read-StateFile {
    <#
        Returns the set of .d names already archived.

        Note the `return ,$set`. PowerShell unrolls collections on
        return, so a plain `return $set` hands back a String when the
        set holds one name and $null when it is empty -- not a HashSet.
        The symptoms are nasty and delayed: on a fresh install $null
        makes the first Get-Candidates throw under StrictMode, and after
        the first successful copy .Add() fails on a fixed-size result,
        so the same .d is copied again on every tick, forever. The
        leading comma wraps the set so exactly one object comes back.
        Covered by tests/test_flinders_copy_tray.ps1 section 6.
    #>
    $set = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    if (-not (Test-Path -LiteralPath $script:StatePath)) { return ,$set }
    try {
        $raw = Get-Content -LiteralPath $script:StatePath -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($raw)) { return ,$set }
        $obj = ConvertFrom-Json $raw
        $arr = Get-JsonProp $obj "copied"
        if ($null -ne $arr) {
            foreach ($name in @($arr)) {
                [void] $set.Add([string] $name)
            }
        }
    } catch {
        Write-Log "State file unreadable, starting fresh: $($_.Exception.Message)" "WARN"
    }
    return ,$set
}

function Save-StateFile {
    try {
        $names = @()
        foreach ($name in $script:Copied) { $names += $name }
        $obj = @{
            copied  = $names
            updated = (Get-Date).ToString("s")
        }
        $json = ConvertTo-Json $obj
        Set-Content -LiteralPath $script:StatePath -Value $json -Encoding UTF8
    } catch {
        Write-Log "Could not save state: $($_.Exception.Message)" "WARN"
    }
}

# ------------------------------------------------------------------
# Flinders destination discovery
# ------------------------------------------------------------------
function Get-MappedDrives {
    $out = @()
    try {
        $drives = Get-PSDrive -PSProvider FileSystem -ErrorAction Stop
    } catch {
        return $out
    }
    foreach ($drive in $drives) {
        $unc = $drive.DisplayRoot
        if ([string]::IsNullOrEmpty($unc)) { continue }
        $rec = New-Object PSObject
        Add-Member -InputObject $rec -MemberType NoteProperty -Name Letter -Value "$($drive.Name):"
        Add-Member -InputObject $rec -MemberType NoteProperty -Name Unc    -Value $unc
        $out += $rec
    }
    return $out
}

function Resolve-FlindersTarget {
    <#
        Accept anything the operator can plausibly give us -- a bare
        drive letter (Y:), the share root, or the full path to the
        instrument folder -- and return the actual tTOF_HT directory.
        Returns $null when it isn't there, so we never invent a tree
        in the wrong place.
    #>
    param([string] $Root)

    if ([string]::IsNullOrWhiteSpace($Root)) { return $null }
    $trimmed = $Root.Trim()
    if ($trimmed.EndsWith("\")) {
        $trimmed = $trimmed.TrimEnd("\")
    }
    if ($trimmed -match "^[A-Za-z]:$") {
        $trimmed = "$trimmed\"
    }

    $leaf = ""
    try { $leaf = Split-Path -Leaf $trimmed } catch { $leaf = "" }
    if ($leaf -ieq $script:InstrumentDir) {
        if (Test-Path -LiteralPath $trimmed -PathType Container) { return $trimmed }
        return $null
    }

    foreach ($suffix in $script:DestSuffixes) {
        $candidate = Join-Path $trimmed $suffix
        if (Test-Path -LiteralPath $candidate -PathType Container) { return $candidate }
    }
    return $null
}

function Show-DrivePicker {
    <#
        Ask which drive letter the Flinders share is mapped to. Shows
        every mapped network drive with its UNC target, marks the ones
        that actually contain tTOF_HT, and preselects the best guess.
    #>
    param([string] $Current)

    $mapped = @(Get-MappedDrives)

    $form = New-Object System.Windows.Forms.Form
    $form.Text = "STAN - where is the Flinders share?"
    $form.ClientSize = New-Object System.Drawing.Size(600, 400)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.TopMost = $true

    $prompt = New-Object System.Windows.Forms.Label
    $prompt.Text = "Which drive letter is the Flinders share mapped to on this PC?`r`nPick the drive that holds Data\raw_data\$($script:InstrumentDir)."
    $prompt.Location = New-Object System.Drawing.Point(14, 12)
    $prompt.Size = New-Object System.Drawing.Size(570, 40)
    $form.Controls.Add($prompt)

    $list = New-Object System.Windows.Forms.ListBox
    $list.Location = New-Object System.Drawing.Point(14, 58)
    $list.Size = New-Object System.Drawing.Size(570, 180)
    $list.Font = New-Object System.Drawing.Font("Consolas", 9)
    $form.Controls.Add($list)

    # Build the rows, remembering the resolved target for each.
    $targets = @()
    $preselect = -1
    $index = 0
    foreach ($drive in $mapped) {
        $resolved = Resolve-FlindersTarget $drive.Letter
        $note = "  --  no $($script:InstrumentDir) found"
        if ($null -ne $resolved) {
            $note = "  --  $($script:InstrumentDir) FOUND"
            if ($preselect -lt 0) { $preselect = $index }
        } else {
            $lower = $drive.Unc.ToLower()
            foreach ($hint in $script:UncHints) {
                if ($lower.Contains($hint)) {
                    if ($preselect -lt 0) { $preselect = $index }
                    break
                }
            }
        }
        [void] $list.Items.Add("$($drive.Letter)  ->  $($drive.Unc)$note")
        $targets += $resolved
        $index++
    }

    if ($list.Items.Count -eq 0) {
        [void] $list.Items.Add("(no mapped network drives found -- type a path below)")
        $targets += $null
    } elseif ($preselect -ge 0) {
        $list.SelectedIndex = $preselect
    }

    $manualLabel = New-Object System.Windows.Forms.Label
    $manualLabel.Text = "...or type a drive letter or full UNC path (used if filled in):"
    $manualLabel.Location = New-Object System.Drawing.Point(14, 250)
    $manualLabel.Size = New-Object System.Drawing.Size(570, 20)
    $form.Controls.Add($manualLabel)

    $manual = New-Object System.Windows.Forms.TextBox
    $manual.Location = New-Object System.Drawing.Point(14, 272)
    $manual.Size = New-Object System.Drawing.Size(570, 24)
    $manual.Font = New-Object System.Drawing.Font("Consolas", 9)
    if (-not [string]::IsNullOrWhiteSpace($Current)) {
        $manual.Text = $Current
    }
    $form.Controls.Add($manual)

    $status = New-Object System.Windows.Forms.Label
    $status.Location = New-Object System.Drawing.Point(14, 302)
    $status.Size = New-Object System.Drawing.Size(570, 36)
    $status.ForeColor = [System.Drawing.Color]::Firebrick
    $form.Controls.Add($status)

    $ok = New-Object System.Windows.Forms.Button
    $ok.Text = "Use this"
    $ok.Location = New-Object System.Drawing.Point(400, 348)
    $ok.Size = New-Object System.Drawing.Size(88, 30)
    $form.Controls.Add($ok)

    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = "Cancel"
    $cancel.Location = New-Object System.Drawing.Point(496, 348)
    $cancel.Size = New-Object System.Drawing.Size(88, 30)
    $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Controls.Add($cancel)
    $form.CancelButton = $cancel

    # Closure state for the result.
    $script:PickerResult = $null

    $ok.Add_Click({
        $chosen = $null
        $typed = $manual.Text
        if (-not [string]::IsNullOrWhiteSpace($typed)) {
            $chosen = Resolve-FlindersTarget $typed
            if ($null -eq $chosen) {
                $status.Text = "Couldn't find $($script:InstrumentDir) under '$typed'. Check the drive is connected."
                return
            }
        } else {
            $sel = $list.SelectedIndex
            if ($sel -lt 0) {
                $status.Text = "Pick a drive from the list, or type a path."
                return
            }
            $chosen = $targets[$sel]
            if ($null -eq $chosen) {
                $status.Text = "That drive has no $($script:InstrumentDir) folder. Pick another, or type the full path."
                return
            }
        }
        $script:PickerResult = $chosen
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    })

    [void] $form.ShowDialog()
    $form.Dispose()
    return $script:PickerResult
}

# ------------------------------------------------------------------
# Month folder resolution
#
# The Flinders archive nests by month (tTOF_HT\Aug26\<run>.d) but the
# spelling drifted over the years -- June26, jun25, JUL26, july26,
# March25, Mar26 all exist. So: derive the month from the run itself,
# then reuse whatever folder already represents that month rather than
# creating a second spelling of it.
# ------------------------------------------------------------------
function Get-RunStamp {
    <#
        Prefer the YYYYMMDD prefix Bruker puts in the run name
        (20260828_100spd_COH-46_S5-F6_1_24165.d) over the directory
        mtime, so a run acquired at 23:50 doesn't land in next month.
    #>
    param([System.IO.DirectoryInfo] $Dir)

    if ($Dir.Name -match "^(\d{8})") {
        $digits = $Matches[1]
        $parsed = [datetime]::MinValue
        $inv = [System.Globalization.CultureInfo]::InvariantCulture
        $styles = [System.Globalization.DateTimeStyles]::None
        $ok = [datetime]::TryParseExact($digits, "yyyyMMdd", $inv, $styles, [ref] $parsed)
        if ($ok) { return $parsed }
    }
    return $Dir.LastWriteTime
}

function ConvertTo-MonthDate {
    <#
        Turn a month folder name into a date, or $null if it isn't one.

        One format at a time on purpose. TryParseExact has a string[]
        overload, but PowerShell binds a PS array to the (string, string,
        ...) overload instead and stringifies it to "System.Object[]", so
        the array form silently matches NOTHING -- every folder reads as
        "not a month" and the reuse logic below quietly creates Jun26
        next to the existing June26. Verified against the real archive
        names in tests/test_flinders_copy_tray.ps1.
    #>
    param([string] $Name)

    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $styles = [System.Globalization.DateTimeStyles]::None
    $formats = @("MMMyy", "MMMMyy", "MMM-yy", "MMMM-yy", "MMM_yy", "MMMM_yy")
    foreach ($fmt in $formats) {
        $parsed = [datetime]::MinValue
        $ok = [datetime]::TryParseExact($Name, $fmt, $inv, $styles, [ref] $parsed)
        if ($ok) { return $parsed }
    }
    return $null
}

function Resolve-MonthDir {
    param([datetime] $Stamp)

    $inv = [System.Globalization.CultureInfo]::InvariantCulture
    $wanted = $Stamp.ToString("MMMyy", $inv)   # e.g. Aug26

    $existing = @()
    try {
        $existing = @(Get-ChildItem -LiteralPath $script:DestRoot -Directory -Force -ErrorAction Stop)
    } catch {
        $existing = @()
    }

    # Exact name, ignoring case (Aug26 / aug26 / AUG26).
    foreach ($dir in $existing) {
        if ($dir.Name -ieq $wanted) { return $dir.FullName }
    }
    # Same month + year spelled differently (June26 for Jun26).
    foreach ($dir in $existing) {
        $asDate = ConvertTo-MonthDate $dir.Name
        if ($null -eq $asDate) { continue }
        if ($asDate.Year -eq $Stamp.Year -and $asDate.Month -eq $Stamp.Month) {
            return $dir.FullName
        }
    }

    $target = Join-Path $script:DestRoot $wanted
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    Write-Log "Created month folder $target"
    return $target
}

# ------------------------------------------------------------------
# Directory measurement + stability
# ------------------------------------------------------------------
function Measure-Tree {
    param([string] $Path)

    $bytes = 0
    $files = 0
    try {
        $items = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction Stop)
    } catch {
        return $null
    }
    foreach ($item in $items) {
        $bytes += $item.Length
        $files += 1
    }
    $rec = New-Object PSObject
    Add-Member -InputObject $rec -MemberType NoteProperty -Name Bytes -Value $bytes
    Add-Member -InputObject $rec -MemberType NoteProperty -Name Files -Value $files
    return $rec
}

function Get-Candidates {
    <#
        Top-level .d directories in the watch dir that we haven't
        already archived and that were touched recently. Non-recursive
        and unfiltered by content -- this is the cheap pass that keeps
        the whole thing low-impact.
    #>
    $result = @()
    if (-not (Test-Path -LiteralPath $script:SourceDir -PathType Container)) {
        return $result
    }
    $cutoff = (Get-Date).AddHours(-1 * $script:LookbackHours)
    $dirs = @()
    try {
        $dirs = @(Get-ChildItem -LiteralPath $script:SourceDir -Directory -Force -ErrorAction Stop)
    } catch {
        Write-Log "Cannot list $($script:SourceDir): $($_.Exception.Message)" "WARN"
        return $result
    }
    foreach ($dir in $dirs) {
        if ($dir.Extension -ine ".d") { continue }
        if ($script:Copied.Contains($dir.Name)) { continue }
        if ($dir.LastWriteTime -lt $cutoff) { continue }
        $result += $dir
    }
    return $result
}

# ------------------------------------------------------------------
# The copy itself
# ------------------------------------------------------------------
function Start-CopyFor {
    param([System.IO.DirectoryInfo] $Dir)

    $stamp = Get-RunStamp $Dir
    $monthDir = Resolve-MonthDir $stamp
    $dest = Join-Path $monthDir $Dir.Name

    $before = Measure-Tree $Dir.FullName
    if ($null -eq $before) {
        Write-Log "Could not measure $($Dir.FullName) -- will retry next tick" "WARN"
        return
    }

    $roboLog = Join-Path $script:LogDir "flinders_copy_robocopy.log"
    $roboArgs = @()
    $roboArgs += "`"$($Dir.FullName)`""
    $roboArgs += "`"$dest`""
    $roboArgs += "/E"                              # include subdirs, empty ones too
    $roboArgs += "/Z"                              # restartable across a share drop
    $roboArgs += "/FFT"                            # 2 s time granularity (matches existing bat)
    $roboArgs += "/R:$($script:RobocopyRetry)"
    $roboArgs += "/W:$($script:RobocopyWait)"
    $roboArgs += "/IPG:$($script:Ipg)"             # throttle: yield bandwidth to the instrument
    $roboArgs += "/NP"                             # no per-file percentage spam
    $roboArgs += "/NFL"
    $roboArgs += "/NDL"
    $roboArgs += "/NJH"
    $roboArgs += "/LOG+:`"$roboLog`""

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "robocopy.exe"
    $psi.Arguments = ($roboArgs -join " ")
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    $proc = $null
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    } catch {
        $script:LastError = "Could not start robocopy: $($_.Exception.Message)"
        Write-Log $script:LastError "ERROR"
        Show-Balloon "Copy failed" $script:LastError $true
        return
    }
    try {
        $proc.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::BelowNormal
    } catch {}

    $mb = [math]::Round($before.Bytes / 1MB, 1)
    Write-Log "Copying $($Dir.Name) ($($before.Files) files, $mb MB) -> $dest"

    $active = New-Object PSObject
    Add-Member -InputObject $active -MemberType NoteProperty -Name Name    -Value $Dir.Name
    Add-Member -InputObject $active -MemberType NoteProperty -Name Source  -Value $Dir.FullName
    Add-Member -InputObject $active -MemberType NoteProperty -Name Dest    -Value $dest
    Add-Member -InputObject $active -MemberType NoteProperty -Name Before  -Value $before
    Add-Member -InputObject $active -MemberType NoteProperty -Name Proc    -Value $proc
    Add-Member -InputObject $active -MemberType NoteProperty -Name Started -Value (Get-Date)
    $script:ActiveCopy = $active
}

function Complete-ActiveCopy {
    <#
        Poll the in-flight robocopy. Returns immediately while it is
        still running so the tray icon never blocks on a multi-GB .d.
    #>
    $active = $script:ActiveCopy
    if (-not $active.Proc.HasExited) { return }

    $code = $active.Proc.ExitCode
    $active.Proc.Dispose()
    $script:ActiveCopy = $null

    $elapsed = [math]::Round(((Get-Date) - $active.Started).TotalSeconds, 0)

    # Robocopy: 0-7 are success-ish, 8 and above are real failures.
    if ($code -ge 8) {
        $script:LastError = "robocopy exit $code on $($active.Name)"
        Write-Log "FAILED $($active.Name) -- robocopy exit code $code after ${elapsed}s" "ERROR"
        Show-Balloon "Copy failed" "$($active.Name) -- robocopy exit $code. See log." $true
        return
    }

    # Verify before marking done: file count and byte total must match.
    # A mismatch leaves the run unrecorded so the next tick retries it.
    $after = Measure-Tree $active.Dest
    if ($null -eq $after) {
        $script:LastError = "Cannot read destination $($active.Dest)"
        Write-Log "FAILED $($active.Name) -- destination unreadable after copy" "ERROR"
        Show-Balloon "Copy failed" "$($active.Name) -- destination unreadable." $true
        return
    }
    if ($after.Files -ne $active.Before.Files -or $after.Bytes -ne $active.Before.Bytes) {
        $script:LastError = "Verify mismatch on $($active.Name)"
        $msg = "VERIFY MISMATCH $($active.Name) -- src $($active.Before.Files) files / $($active.Before.Bytes) B, dst $($after.Files) files / $($after.Bytes) B. Not marking done; will retry."
        Write-Log $msg "ERROR"
        Show-Balloon "Copy incomplete" "$($active.Name) did not verify. Will retry." $true
        return
    }

    [void] $script:Copied.Add($active.Name)
    Save-StateFile
    $script:CopyCount += 1
    $script:LastError = ""
    $mb = [math]::Round($after.Bytes / 1MB, 1)
    Write-Log "OK $($active.Name) -- $($after.Files) files, $mb MB in ${elapsed}s -> $($active.Dest)"
    Show-Balloon "Copied to Flinders" "$($active.Name) ($mb MB)" $false
}

# ------------------------------------------------------------------
# Tray plumbing
# ------------------------------------------------------------------
function New-DotIcon {
    param([System.Drawing.Color] $Color)

    $bmp = New-Object System.Drawing.Bitmap 16, 16
    $gfx = [System.Drawing.Graphics]::FromImage($bmp)
    $gfx.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $brush = New-Object System.Drawing.SolidBrush $Color
    $gfx.FillEllipse($brush, 1, 1, 14, 14)
    $penColor = [System.Drawing.Color]::FromArgb(90, 0, 0, 0)
    $pen = New-Object System.Drawing.Pen -ArgumentList @($penColor, [single] 1)
    $gfx.DrawEllipse($pen, 1, 1, 14, 14)
    $brush.Dispose()
    $pen.Dispose()
    $gfx.Dispose()
    $handle = $bmp.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($handle)
    $bmp.Dispose()
    return $icon
}

function Show-Balloon {
    param(
        [string] $Title,
        [string] $Text,
        [bool]   $IsError
    )
    if ($null -eq $script:Notify) { return }
    $kind = [System.Windows.Forms.ToolTipIcon]::Info
    if ($IsError) {
        $kind = [System.Windows.Forms.ToolTipIcon]::Error
    }
    try {
        $script:Notify.ShowBalloonTip(6000, $Title, $Text, $kind)
    } catch {}
}

function Set-TrayState {
    param(
        [string] $Summary,
        [string] $Mode
    )
    if ($null -eq $script:Notify) { return }

    $icon = $script:IconIdle
    if ($Mode -eq "copy")  { $icon = $script:IconCopy }
    if ($Mode -eq "watch") { $icon = $script:IconWatch }
    if ($Mode -eq "error") { $icon = $script:IconError }
    $script:Notify.Icon = $icon

    # NotifyIcon.Text is capped at 63 characters -- truncate, don't throw.
    $text = "$($script:AppName): $Summary"
    if ($text.Length -gt 63) {
        $text = $text.Substring(0, 60)
        $text = "$text..."
    }
    $script:Notify.Text = $text

    if ($null -ne $script:MenuStatus) {
        $script:MenuStatus.Text = $Summary
    }
}

# ------------------------------------------------------------------
# Main tick
# ------------------------------------------------------------------
function Invoke-Tick {
    if ($script:Paused) {
        Set-TrayState "paused" "idle"
        return
    }

    if ($null -ne $script:ActiveCopy) {
        $name = $script:ActiveCopy.Name
        Set-TrayState "copying $name" "copy"
        Complete-ActiveCopy
        return
    }

    $candidates = @(Get-Candidates)
    if ($candidates.Count -eq 0) {
        $script:Sizes = @{}
        $summary = "idle - $($script:CopyCount) copied this session"
        $mode = "idle"
        if (-not [string]::IsNullOrEmpty($script:LastError)) { $mode = "error" }
        Set-TrayState $summary $mode
        return
    }

    $now = Get-Date
    $ready = $null
    $growing = 0

    foreach ($dir in $candidates) {
        $measured = Measure-Tree $dir.FullName
        if ($null -eq $measured) { continue }

        $prior = $null
        if ($script:Sizes.ContainsKey($dir.Name)) {
            $prior = $script:Sizes[$dir.Name]
        }

        if ($null -eq $prior) {
            $rec = New-Object PSObject
            Add-Member -InputObject $rec -MemberType NoteProperty -Name Bytes  -Value $measured.Bytes
            Add-Member -InputObject $rec -MemberType NoteProperty -Name Files  -Value $measured.Files
            Add-Member -InputObject $rec -MemberType NoteProperty -Name Since  -Value $now
            $script:Sizes[$dir.Name] = $rec
            $growing += 1
            continue
        }

        if ($prior.Bytes -ne $measured.Bytes -or $prior.Files -ne $measured.Files) {
            $prior.Bytes = $measured.Bytes
            $prior.Files = $measured.Files
            $prior.Since = $now
            $growing += 1
            continue
        }

        $stableFor = ($now - $prior.Since).TotalSeconds
        if ($stableFor -ge $script:StableSecs) {
            if ($null -eq $ready) { $ready = $dir }
        } else {
            $growing += 1
        }
    }

    if ($null -ne $ready) {
        Set-TrayState "copying $($ready.Name)" "copy"
        Start-CopyFor $ready
        return
    }

    Set-TrayState "watching $growing acquiring" "watch"
}

# ------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------
Write-Log "---- $($script:AppName) starting (PID $PID) ----"

$config = Read-ConfigFile

# Source dir: parameter beats config beats default.
$script:SourceDir = $script:DefaultSource
$cfgSource = Get-JsonProp $config "sourceDir"
if (-not [string]::IsNullOrWhiteSpace($cfgSource)) { $script:SourceDir = $cfgSource }
if (-not [string]::IsNullOrWhiteSpace($argSourceDir)) { $script:SourceDir = $argSourceDir }

# Stability window: parameter beats config beats default.
$script:StableSecs = $script:DefaultStable
$cfgStable = Get-JsonProp $config "stableSecs"
if ($null -ne $cfgStable -and [int] $cfgStable -gt 0) { $script:StableSecs = [int] $cfgStable }
if ($argStableSecs -gt 0) { $script:StableSecs = $argStableSecs }

$cfgLookback = Get-JsonProp $config "lookbackHours"
if ($null -ne $cfgLookback -and [int] $cfgLookback -gt 0) { $script:LookbackHours = [int] $cfgLookback }

# Destination: parameter, then remembered value (re-validated), then ask.
$script:DestRoot = ""
if (-not [string]::IsNullOrWhiteSpace($argDestPath)) {
    $script:DestRoot = Resolve-FlindersTarget $argDestPath
}
if ([string]::IsNullOrWhiteSpace($script:DestRoot) -and -not $Reconfigure) {
    $cfgDest = Get-JsonProp $config "destRoot"
    if (-not [string]::IsNullOrWhiteSpace($cfgDest)) {
        # Re-validate every launch: the operator may have remapped the
        # letter to something else entirely since last time.
        $script:DestRoot = Resolve-FlindersTarget $cfgDest
        if ([string]::IsNullOrWhiteSpace($script:DestRoot)) {
            Write-Log "Remembered destination '$cfgDest' is gone or remapped -- asking again" "WARN"
        }
    }
}
if ([string]::IsNullOrWhiteSpace($script:DestRoot)) {
    $prefill = Get-JsonProp $config "destRoot"
    if ($null -eq $prefill) { $prefill = "" }
    $script:DestRoot = Show-DrivePicker $prefill
}
if ([string]::IsNullOrWhiteSpace($script:DestRoot)) {
    Write-Log "No Flinders destination chosen -- exiting" "WARN"
    [void] [System.Windows.Forms.MessageBox]::Show(
        "No Flinders destination was selected, so nothing will be copied.",
        $script:AppName)
    exit 1
}

Save-ConfigFile
$script:Copied = Read-StateFile

Write-Log "Source      : $($script:SourceDir)"
Write-Log "Destination : $($script:DestRoot)"
Write-Log "Stable secs : $($script:StableSecs)   Lookback: $($script:LookbackHours) h   IPG: $($script:Ipg) ms"
Write-Log "Already archived according to state file: $($script:Copied.Count)"

# ---- icons ----
$script:IconIdle  = New-DotIcon ([System.Drawing.Color]::FromArgb(150, 150, 150))
$script:IconWatch = New-DotIcon ([System.Drawing.Color]::FromArgb(60, 130, 220))
$script:IconCopy  = New-DotIcon ([System.Drawing.Color]::FromArgb(60, 175, 90))
$script:IconError = New-DotIcon ([System.Drawing.Color]::FromArgb(210, 65, 60))

# ---- context menu ----
$menu = New-Object System.Windows.Forms.ContextMenuStrip

$script:MenuStatus = New-Object System.Windows.Forms.ToolStripMenuItem
$script:MenuStatus.Text = "starting..."
$script:MenuStatus.Enabled = $false
[void] $menu.Items.Add($script:MenuStatus)
[void] $menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$menuCheck = New-Object System.Windows.Forms.ToolStripMenuItem
$menuCheck.Text = "Check now"
$menuCheck.Add_Click({ Invoke-Tick })
[void] $menu.Items.Add($menuCheck)

$menuBacklog = New-Object System.Windows.Forms.ToolStripMenuItem
$menuBacklog.Text = "Widen scan to 30 days"
$menuBacklog.Add_Click({
    $script:LookbackHours = 720
    Save-ConfigFile
    Write-Log "Lookback widened to 30 days by operator"
    Show-Balloon $script:AppName "Now scanning back 30 days for un-copied runs." $false
    Invoke-Tick
})
[void] $menu.Items.Add($menuBacklog)

$script:MenuPause = New-Object System.Windows.Forms.ToolStripMenuItem
$script:MenuPause.Text = "Pause"
$script:MenuPause.Add_Click({
    if ($script:Paused) {
        $script:Paused = $false
        $script:MenuPause.Text = "Pause"
        Write-Log "Resumed by operator"
    } else {
        $script:Paused = $true
        $script:MenuPause.Text = "Resume"
        Write-Log "Paused by operator"
    }
    Invoke-Tick
})
[void] $menu.Items.Add($script:MenuPause)
[void] $menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$menuLogs = New-Object System.Windows.Forms.ToolStripMenuItem
$menuLogs.Text = "Open log folder"
$menuLogs.Add_Click({ Start-Process explorer.exe $script:LogDir })
[void] $menu.Items.Add($menuLogs)

$menuDest = New-Object System.Windows.Forms.ToolStripMenuItem
$menuDest.Text = "Open Flinders folder"
$menuDest.Add_Click({ Start-Process explorer.exe $script:DestRoot })
[void] $menu.Items.Add($menuDest)

$menuDrive = New-Object System.Windows.Forms.ToolStripMenuItem
$menuDrive.Text = "Change Flinders drive..."
$menuDrive.Add_Click({
    $picked = Show-DrivePicker $script:DestRoot
    if (-not [string]::IsNullOrWhiteSpace($picked)) {
        $script:DestRoot = $picked
        Save-ConfigFile
        Write-Log "Destination changed to $picked"
        Show-Balloon $script:AppName "Now copying to $picked" $false
    }
})
[void] $menu.Items.Add($menuDrive)
[void] $menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$menuExit = New-Object System.Windows.Forms.ToolStripMenuItem
$menuExit.Text = "Exit"
$menuExit.Add_Click({
    if ($null -ne $script:ActiveCopy) {
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "A copy of $($script:ActiveCopy.Name) is still running. Exit anyway?",
            $script:AppName,
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Question)
        if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { return }
    }
    Write-Log "Exit requested by operator"
    $script:Timer.Stop()
    $script:Notify.Visible = $false
    $script:Notify.Dispose()
    [System.Windows.Forms.Application]::Exit()
})
[void] $menu.Items.Add($menuExit)

# ---- tray icon ----
$script:Notify = New-Object System.Windows.Forms.NotifyIcon
$script:Notify.Icon = $script:IconIdle
$script:Notify.Text = $script:AppName
$script:Notify.ContextMenuStrip = $menu
$script:Notify.Visible = $true
$script:Notify.Add_MouseDoubleClick({ Start-Process explorer.exe $script:LogDir })

# ---- poll timer ----
$script:Timer = New-Object System.Windows.Forms.Timer
$script:Timer.Interval = $script:PollSeconds * 1000
$script:Timer.Add_Tick({
    # One bad tick must never kill the tray app -- log and carry on.
    try {
        Invoke-Tick
    } catch {
        $script:LastError = $_.Exception.Message
        Write-Log "Tick error: $($_.Exception.Message)" "ERROR"
        Set-TrayState "error - see log" "error"
    }
})
$script:Timer.Start()

Show-Balloon $script:AppName "Watching $($script:SourceDir) -> $($script:DestRoot)" $false
Invoke-Tick

[System.Windows.Forms.Application]::Run()

Write-Log "---- $($script:AppName) stopped ----"
