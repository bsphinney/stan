<#
.SYNOPSIS
    Find HyStar / TimsControl instrument logs. READ-ONLY -- reports, copies nothing.

.DESCRIPTION
    Two things we want and cannot currently get:

      1. OVER-PRESSURE EVENTS with Bruker's own timestamps. The Evosep
         procedure logs give us the pressure trace, but no event record.
      2. COLUMN OVEN TEMPERATURE. Verified absent from the Evosep logs,
         HyStarMetadata.xml, analysis.tdf GlobalMetadata and the Compass
         extract. Every pressure figure currently ASSUMES 50 C. If HyStar
         records it, that assumption becomes a measurement.

    This script only looks and reports: where the logs are, how far back they
    go, how big they are, and whether pressure/temperature keywords appear in
    them. Nothing is copied or changed. Decide what to pull after seeing this.
#>
[CmdletBinding()]
param(
    [int] $SampleLines = 40
)
$ErrorActionPreference = 'Continue'
function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }

$roots = @(
    'C:\ProgramData\Bruker',
    'C:\ProgramData\Bruker Daltonics',
    'C:\ProgramData\Bruker Daltonik',
    'C:\BDAL',
    'C:\Users\Public\Documents\Bruker',
    'C:\Program Files\Bruker Daltonics',
    'C:\Program Files (x86)\Bruker Daltonics',
    'C:\Program Files\Bruker Daltonik',
    'C:\Program Files (x86)\Bruker Daltonik'
)

Say ''
Say '=== 1. Candidate roots ===' 'Cyan'
$found = @()
foreach ($r in $roots) {
    if (Test-Path $r) { Say "  FOUND  $r" 'Green'; $found += $r }
    else              { Say "  --     $r" 'DarkGray' }
}
if ($found.Count -eq 0) { Say 'No Bruker directories found. Pass -Verbose or tell Claude.' 'Red' }

Say ''
Say '=== 2. HyStar / TimsControl / Compass directories ===' 'Cyan'
$dirs = @()
foreach ($r in $found) {
    Get-ChildItem $r -Recurse -Directory -EA SilentlyContinue -Depth 3 |
        Where-Object { $_.Name -match '(?i)hystar|timscontrol|compass|log' } |
        ForEach-Object { $dirs += $_; Say ("  " + $_.FullName) }
}
if ($dirs.Count -eq 0) { Say '  none matched' 'Yellow' }

Say ''
Say '=== 3. Log-ish files, newest first ===' 'Cyan'
$logs = @()
foreach ($d in $dirs) {
    Get-ChildItem $d.FullName -File -EA SilentlyContinue |
        Where-Object { $_.Extension -match '(?i)\.(log|txt|xml|csv|tsv|json)$' } |
        ForEach-Object { $logs += $_ }
}
$logs = $logs | Sort-Object LastWriteTime -Descending
if ($logs.Count -eq 0) {
    Say '  no log files found' 'Yellow'
} else {
    Say ("  {0} file(s). Oldest {1}, newest {2}." -f $logs.Count,
         ($logs[-1].LastWriteTime.ToString('yyyy-MM-dd')),
         ($logs[0].LastWriteTime.ToString('yyyy-MM-dd')))
    $logs | Select-Object -First 25 | ForEach-Object {
        Say ("  {0,-12} {1,10:N0} KB  {2}" -f $_.LastWriteTime.ToString('yyyy-MM-dd'),
             ($_.Length/1KB), $_.FullName)
    }
    if ($logs.Count -gt 25) { Say ("  ... and {0} more" -f ($logs.Count - 25)) }
}

Say ''
Say '=== 4. Do they contain pressure / temperature ? ===' 'Cyan'
$keywords = 'pressure', 'overpressure', 'temperature', 'oven', 'toaster', 'column'
foreach ($f in ($logs | Select-Object -First 12)) {
    try {
        $head = Get-Content $f.FullName -TotalCount 4000 -EA Stop
        $hits = @()
        foreach ($k in $keywords) {
            $n = ($head | Select-String -SimpleMatch $k -EA SilentlyContinue).Count
            if ($n -gt 0) { $hits += ("{0}={1}" -f $k, $n) }
        }
        if ($hits.Count -gt 0) {
            Say ("  " + $f.Name + "  ->  " + ($hits -join '  ')) 'Green'
        }
    } catch { }
}

Say ''
Say '=== 5. Sample of the newest log carrying a pressure hit ===' 'Cyan'
foreach ($f in ($logs | Select-Object -First 12)) {
    try {
        $m = Get-Content $f.FullName -TotalCount 4000 -EA Stop |
             Select-String -Pattern '(?i)pressure|temperature' -EA SilentlyContinue |
             Select-Object -First $SampleLines
        if ($m) {
            Say ("  --- " + $f.FullName + " ---") 'Yellow'
            $m | ForEach-Object { Say ("    " + $_.Line.Trim()) }
            break
        }
    } catch { }
}

Say ''
Say 'Read-only: nothing was copied or changed.' 'Green'
Say ''
Say 'Press any key to continue . . .'
$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
