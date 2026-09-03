<#
.SYNOPSIS
    Locate the Evosep One logs on this instrument, and see whether they carry
    column pressure. Read-only.

.DESCRIPTION
    The Evosep driver ships NLog + log4net (seen beside EvosepOneDriver.dll), so
    its log destination is declared in a config file rather than guessed. This
    reads those configs, resolves the declared targets, then also sweeps the
    usual locations for Evosep log files and reports what it finds -- newest
    first, with a peek at whether any line mentions pressure.

    Bruker's Compass DATABASE does NOT record column pressure (checked: no
    pressure column exists anywhere in it). If a pressure trace is recorded at
    all, it is in these logs -- which is what this is for.

    Changes nothing. Writes a transcript to the share so the output can be read
    without squinting at a console.
#>
[CmdletBinding()]
param(
    [string] $OutRoot = 'Y:\brett\bruker_db',
    [string] $OutRootUnc = '\\128.120.208.42\proteomics-grp\brett\bruker_db',
    [int]    $Days = 30
)
$ErrorActionPreference = 'SilentlyContinue'

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$root = $OutRoot
if (-not (Test-Path $root)) { $root = $OutRootUnc }
try {
    if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root -Force | Out-Null }
    $script:__t = Join-Path $root "evosep_logs_$($env:COMPUTERNAME)_$stamp.txt"
    Start-Transcript -Path $script:__t -Force | Out-Null
} catch { $script:__t = $null }

function H($t) { Write-Host ''; Write-Host "== $t" -ForegroundColor Cyan }

H "1. Logging configs shipped with the Evosep / HyStar driver"
$cfgRoots = @(
  'C:\Program Files (x86)\Bruker Daltonik\HyStar',
  'C:\Program Files\Bruker Daltonik\HyStar',
  'C:\Program Files\Bruker', 'C:\Program Files (x86)\Bruker'
)
$declared = @()
foreach ($r in $cfgRoots) {
    if (-not (Test-Path $r)) { continue }
    Get-ChildItem $r -Recurse -File -Include NLog.config,*.nlog,log4net.config,*.exe.config,*.dll.config `
        -EA SilentlyContinue | Where-Object { $_.Length -lt 1MB } | ForEach-Object {
        $txt = Get-Content $_.FullName -Raw -EA SilentlyContinue
        if ($txt -match 'evosep|nlog|log4net') {
            # pull declared file targets: fileName="..." / <file value="..." />
            $m = [regex]::Matches($txt, '(?:fileName|file\s+value)\s*=\s*"([^"]+)"')
            if ($m.Count) {
                Write-Host "   $($_.FullName)" -ForegroundColor Green
                foreach ($x in $m) { Write-Host "     target: $($x.Groups[1].Value)"; $declared += $x.Groups[1].Value }
            }
        }
    }
}
if (-not $declared) { Write-Host "   no declared log targets found in driver configs" -ForegroundColor Yellow }

H "2. Evosep log files on disk (common locations + declared targets)"
$dirs = @(
  "$env:ProgramData\Evosep", "$env:ProgramData\Bruker Daltonik\HyStar",
  "$env:ProgramData\Bruker", "$env:LOCALAPPDATA\Evosep", "$env:APPDATA\Evosep",
  'C:\Program Files (x86)\Bruker Daltonik\HyStar\AgilentICF\EvosepOneDriver',
  'C:\Program Files (x86)\Bruker Daltonik\HyStar\Log',
  'C:\Program Files (x86)\Bruker Daltonik\HyStar\Logs',
  'D:\Logs', 'C:\Logs', 'C:\Evosep'
)
foreach ($t in $declared) {
    $d = Split-Path ($t -replace '\$\{[^}]+\}','') -Parent
    if ($d -and (Test-Path $d)) { $dirs += $d }
}
$cut = (Get-Date).AddDays(-$Days)
$found = @()
foreach ($d in ($dirs | Select-Object -Unique)) {
    if (-not (Test-Path $d)) { continue }
    Get-ChildItem $d -Recurse -File -Include *.log,*.txt,*.csv -EA SilentlyContinue |
      Where-Object { $_.LastWriteTime -gt $cut -and $_.Length -gt 0 } |
      Sort-Object LastWriteTime -Descending | Select-Object -First 12 | ForEach-Object {
        $found += $_
        Write-Host ("   {0,-70} {1,10:N0} KB  {2}" -f $_.FullName, ($_.Length/1KB), $_.LastWriteTime) -ForegroundColor Green
      }
}
if (-not $found) { Write-Host "   no recent log files in those locations" -ForegroundColor Yellow }

H "3. Do any of them mention pressure? (the actual question)"
$hit = $false
foreach ($f in ($found | Sort-Object LastWriteTime -Descending | Select-Object -First 8)) {
    $m = Select-String -Path $f.FullName -Pattern 'pressure|bar\b|psi\b|backpress' -EA SilentlyContinue |
         Select-Object -First 3
    if ($m) {
        $hit = $true
        Write-Host "   $($f.FullName)" -ForegroundColor Green
        foreach ($x in $m) {
            $line = $x.Line.Trim(); if ($line.Length -gt 160) { $line = $line.Substring(0,160) + '...' }
            Write-Host "     $line"
        }
    }
}
if (-not $hit) { Write-Host "   no pressure mentions in the newest logs" -ForegroundColor Yellow }

H "4. Widest sweep - any file named like an Evosep log anywhere on C:/D:"
Get-ChildItem 'C:\','D:\' -Recurse -File -Include *evosep*.log,*evosep*.txt,*evosep*.csv -EA SilentlyContinue |
  Where-Object { $_.Length -gt 0 } | Sort-Object LastWriteTime -Descending | Select-Object -First 10 |
  ForEach-Object { Write-Host ("   {0}  {1:N0} KB  {2}" -f $_.FullName, ($_.Length/1KB), $_.LastWriteTime) -ForegroundColor Green }

if ($script:__t) {
    try { Stop-Transcript | Out-Null } catch {}
    Write-Host ''
    Write-Host "Full output saved to: $script:__t" -ForegroundColor Cyan
    Write-Host "On Hive: /quobyte/proteomics-grp/brett/bruker_db/$(Split-Path $script:__t -Leaf)" -ForegroundColor Cyan
}
