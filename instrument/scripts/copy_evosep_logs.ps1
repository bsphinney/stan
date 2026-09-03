<#
.SYNOPSIS
    Mirror the Evosep One procedure logs to the Quobyte share. Read-only on the source.

.DESCRIPTION
    The Evosep records Pressure [bar], Actual flow, Displacement and Setpoint
    per procedure, and writes them under
    C:\ProgramData\Evosep\EvosepOne\Procedure logs\<serial>\.
    Bruker's Compass database does NOT hold pressure (verified), so these logs
    are the only place a pressure trace exists -- which makes them the basis
    for column-clog trending and column-lifetime curves.

    2026-09-02: this now MIRRORS the whole history by default. The previous
    version defaulted to the last 30 days and needed -All for everything,
    which is why the first pull landed only 2026-08-14 onward and the column
    panel could not see past columns at all.

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

.EXAMPLE
    .\copy_evosep_logs.ps1               # everything, incremental
    .\copy_evosep_logs.ps1 -Days 30      # just the last 30 days
#>
[CmdletBinding()]
param(
    [string] $Source     = 'C:\ProgramData\Evosep\EvosepOne\Procedure logs',
    [string] $OutRoot    = 'Y:\brett\evosep_logs',
    [string] $OutRootUnc = '\\128.120.208.42\proteomics-grp\brett\evosep_logs',
    [int]    $Days       = 0,
    [int]    $MaxMB      = 60000,
    [switch] $Recent
)
$ErrorActionPreference = 'Stop'
function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }

if ($Recent -and $Days -le 0) { $Days = 30 }

if (-not (Test-Path $Source)) {
    Say "Source not found: $Source" 'Red'
    Say "Pass the right path with -Source if Evosep writes elsewhere." 'Yellow'
    Say ''
    Say 'Press any key to continue . . .'
    $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
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
if ($Days -gt 0) { Say "window: last $Days day(s)" } else { Say "window: entire history" }
Say ''
Say 'Scanning the source (this can take a minute on a long history)...'

$settled = (Get-Date).AddSeconds(-60)
$cut = if ($Days -gt 0) { (Get-Date).AddDays(-$Days) } else { [datetime]'1900-01-01' }

$files = @(Get-ChildItem $Source -Recurse -File -EA SilentlyContinue |
           Where-Object { $_.LastWriteTime -gt $cut -and $_.LastWriteTime -lt $settled })

if (-not $files -or $files.Count -eq 0) {
    Say 'No settled files in the window.' 'Yellow'
    Say ''
    Say 'Press any key to continue . . .'
    $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    exit 1
}

# NEWEST FIRST. 2026-09-02: this was oldest-first for about an hour, on the
# reasoning that an interrupted run then leaves a contiguous history. That was
# the wrong optimisation. A full pull is ~16 GB and runs for hours, and the
# question actually being asked -- did the column change on 2026-07-31 show up
# in the pressure -- needs the LAST few months, which oldest-first delivers
# LAST. Newest-first puts the decision-relevant window on the share within
# minutes and lets the 2023 tail arrive whenever it arrives.
$files = @($files | Sort-Object LastWriteTime -Descending)

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

    if (($bytes + $f.Length) -gt ([long]$MaxMB * 1MB)) {
        $capped = $true
        break
    }

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
    Say "    Re-run this script: it resumes where it stopped." 'Red'
    Say "    Or raise the cap with -MaxMB." 'Red'
    Say ''
}
Say ("Copied  {0:N0} new file(s), {1:N1} MB in {2:N0}s" -f $copied, ($bytes / 1MB), $sw.Elapsed.TotalSeconds) 'Green'
Say ("Skipped {0:N0} already mirrored" -f $skipped)
if ($failed -gt 0) { Say ("Failed  {0:N0} unreadable file(s)" -f $failed) 'Yellow' }
Say ''
Say "On Hive: /quobyte/proteomics-grp/brett/evosep_logs/$($env:COMPUTERNAME)_mirror" 'Cyan'
if (-not $capped -and $failed -eq 0) {
    Say 'Mirror is complete. Safe to close.' 'Green'
}
Say ''
Say 'Press any key to continue . . .'
$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
