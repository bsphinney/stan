<#
.SYNOPSIS
    Copy Bruker's own database backups to the Quobyte share.

.DESCRIPTION
    Bruker's tooling writes its own PostgreSQL backups to D:\BrukerDBBackup.
    A backup was produced by a process that already authenticated, so reading
    it needs no database password -- it sidesteps the credential problem
    entirely.

    This copies the most recent backup files (newest first, up to a size cap)
    to Y:\brett\bruker_db\backup_<HOST>_<timestamp>\. It reads the source and
    writes to the share; it changes nothing on D:.

    An in-progress backup is skipped: a file still being written has a moving
    size, so we skip anything modified in the last 60 seconds.

.EXAMPLE
    .\copy_bruker_backup.ps1
    .\copy_bruker_backup.ps1 -All        # every backup, not just the newest set
#>
[CmdletBinding()]
param(
    [string] $Source   = 'D:\BrukerDBBackup',
    [string] $OutRoot  = 'Y:\brett\bruker_db',
    [string] $OutRootUnc = '\\128.120.208.42\proteomics-grp\brett\bruker_db',
    [int]    $MaxTotalMB = 4096,   # stop after this much, newest first
    [int]    $KeepNewest = 3,      # how many most-recent files (ignored with -All)
    [switch] $All
)

$ErrorActionPreference = 'Stop'
function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }

if (-not (Test-Path $Source)) {
    Say "Source not found: $Source" 'Red'
    Say "If Bruker writes backups elsewhere, pass it:  -Source <path>" 'Yellow'
    exit 1
}

$root = $OutRoot
try { if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root -Force | Out-Null } }
catch { $root = $OutRootUnc }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$out = Join-Path $root "backup_$($env:COMPUTERNAME)_$stamp"
New-Item -ItemType Directory -Path $out -Force | Out-Null
Say "source: $Source"
Say "target: $out"

# All files under the backup dir, newest first, excluding ones still being written.
$cut = (Get-Date).AddSeconds(-60)
$files = Get-ChildItem $Source -Recurse -File -EA SilentlyContinue |
         Where-Object { $_.LastWriteTime -lt $cut } |
         Sort-Object LastWriteTime -Descending
if (-not $files) { Say "No settled backup files in $Source (all modified in the last 60s?)." 'Yellow'; exit 1 }

if (-not $All) { $files = $files | Select-Object -First $KeepNewest }

$copied = 0; $bytes = 0L
foreach ($f in $files) {
    if (($bytes + $f.Length) -gt ($MaxTotalMB * 1MB)) {
        Say "size cap ${MaxTotalMB}MB reached, stopping at $copied files" 'Yellow'; break
    }
    $rel = $f.FullName.Substring($Source.Length).TrimStart('\')
    $dst = Join-Path $out $rel
    New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null
    Copy-Item $f.FullName $dst -Force
    $copied++; $bytes += $f.Length
    Say ("  {0}  ({1:N1} MB)" -f $rel, ($f.Length / 1MB))
}

Say ''
Say ("Copied $copied file(s), {0:N1} MB." -f ($bytes / 1MB)) 'Green'
Say "On Hive: /quobyte/proteomics-grp/brett/bruker_db/$(Split-Path $out -Leaf)" 'Cyan'
Say ''
Say 'Press any key to continue . . .'
$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
