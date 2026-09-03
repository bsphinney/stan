<#
.SYNOPSIS
    Find out how to authenticate to Bruker's local PostgreSQL - read-only.

.DESCRIPTION
    HyStar talks to this database constantly, so a working credential already
    exists on this machine. This looks in the three places it can be, and
    reports what it finds. It changes nothing.

      1. pg_hba.conf   - says what auth is required, and for which roles. If a
                         line reads `trust`, no password is needed at all and
                         the earlier prompt just meant the wrong username.
      2. pgpass.conf   - libpq's own stored-password file, if one exists.
      3. HyStar config - the connection string the application itself uses.

    NOTE. This reads configuration on a machine you administer, to reach your
    own lab's database. It does not attempt to guess, brute-force, or bypass
    anything: if the password is not already stored on this host, this script
    will not find one, and the answer is to ask whoever installed HyStar (or
    Bruker support) rather than to work around the authentication.

    Do NOT "fix" this by editing pg_hba.conf to `trust`. That weakens a live
    instrument, needs a server reload to take effect, and is not necessary -
    the credential exists.

.EXAMPLE
    .\find_bruker_db_login.ps1
#>
[CmdletBinding()]
param(
    [string] $PgData = 'D:\BrukerDBData',
    [string[]] $SearchRoots = @('C:\Program Files (x86)\Bruker Daltonik',
                                'C:\Program Files\Bruker Daltonik',
                                'C:\ProgramData\Bruker')
)

$ErrorActionPreference = 'SilentlyContinue'

# Mirror everything to a file on the share -- the console scrolls, a photo of
# it loses the top, and the interesting sections (config files, service
# account) print last. Start-Transcript captures all of it.
$stamp   = Get-Date -Format 'yyyyMMdd_HHmmss'
$outRoot = 'Y:\brett\bruker_db'
if (-not (Test-Path $outRoot)) {
    $outRoot = '\\128.120.208.42\proteomics-grp\brett\bruker_db'
}
try {
    if (-not (Test-Path $outRoot)) { New-Item -ItemType Directory -Path $outRoot -Force | Out-Null }
    $script:__t = Join-Path $outRoot "login_probe_$($env:COMPUTERNAME)_$stamp.txt"
    Start-Transcript -Path $script:__t -Force | Out-Null
} catch { $script:__t = $null }
function H($t) { Write-Host ''; Write-Host "== $t" -ForegroundColor Cyan }

H "1. pg_hba.conf - what authentication is actually required"
$hba = Join-Path $PgData 'pg_hba.conf'
if (Test-Path $hba) {
    $rules = Get-Content $hba | Where-Object { $_ -match '^\s*(local|host|hostssl)\s' }
    if ($rules) {
        $rules | ForEach-Object { Write-Host "   $_" }
        if ($rules -match '\btrust\b') {
            Write-Host ''
            Write-Host "   A 'trust' line exists -> no password needed for that role/address." -ForegroundColor Green
            Write-Host "   The prompt you saw means the USERNAME was wrong, not the password." -ForegroundColor Green
            Write-Host "   Try the roles named in the lines above with -PgUser." -ForegroundColor Green
        } else {
            Write-Host ''
            Write-Host "   No 'trust' line: a password IS required. See sections 2 and 3." -ForegroundColor Yellow
        }
    }
} else { Write-Host "   not found at $hba" -ForegroundColor Yellow }

H "2. pgpass.conf - libpq's stored password file"
# The server runs as NT AUTHORITY\NetworkService, so a pgpass belonging to
# the account that maintains it would sit in that service profile, not yours.
foreach ($p in @("$env:APPDATA\postgresql\pgpass.conf",
                 "$env:USERPROFILE\pgpass.conf",
                 "C:\ProgramData\postgresql\pgpass.conf",
                 "C:\Windows\ServiceProfiles\NetworkService\AppData\Roaming\postgresql\pgpass.conf",
                 "C:\Windows\ServiceProfiles\LocalService\AppData\Roaming\postgresql\pgpass.conf",
                 "C:\Program Files\Bruker\BrukerDBServer\pgpass.conf")) {
    if (Test-Path $p) {
        Write-Host "   FOUND $p" -ForegroundColor Green
        # host:port:db:user:password - show all but the secret.
        Get-Content $p | Where-Object { $_ -and $_ -notmatch '^\s*#' } | ForEach-Object {
            $f = $_.Split(':')
            if ($f.Count -ge 5) { Write-Host ("     host={0} port={1} db={2} user={3} password=<{4} chars>" -f $f[0],$f[1],$f[2],$f[3],$f[4].Length) }
        }
        Write-Host "   psql/pg_dump will use this automatically - just pass the right -U." -ForegroundColor Green
    }
}

H "3. Stored credentials - config files"
# NOTE the \b around UID. Without it, 'UID' matches the middle of 'GUID=' and
# every installer GUID in Bruker's setup.ini looks like a hit. That false
# positive is what the first run of this script reported.
$patterns = @(
    'connectionstring',
    'password\s*=', 'pwd\s*=',
    '\bUser\s*Id\s*=', '\bUsername\s*=', '\bUID\s*=',
    'Server\s*=\s*(localhost|127\.0\.0\.1)',
    'Port\s*=\s*5432', 'postgres'
)
$rx = ($patterns -join '|')
# 'C:\Program Files\Bruker' (no 'Daltonik') is where the registry says the
# PostgreSQL server actually lives -- BrukerDBServer\bin\pg_ctl.exe. The first
# pass never looked there.
$roots = @($SearchRoots) + @(
    'C:\Program Files\Bruker', 'C:\Program Files (x86)\Bruker',
    'C:\Program Files (x86)\Bruker Daltonik\HyStar',
    'C:\Program Files\Bruker Daltonik\HyStar',
    'C:\ProgramData\Bruker Daltonik', 'C:\ProgramData\HyStar',
    'C:\ProgramData\Bruker', 'C:\Bruker', 'D:\Bruker'
) | Select-Object -Unique

$hits = 0
foreach ($root in $roots) {
    if (-not (Test-Path $root)) { continue }
    Get-ChildItem $root -Recurse -File -Include *.config,*.xml,*.ini,*.json,*.cfg,*.conf,*.properties,*.dat,*.txt |
      Where-Object { $_.Length -lt 2MB } |
      Select-String -Pattern $rx -AllMatches |
      Where-Object {
          # Installer GUIDs and update manifests are noise, not credentials.
          $_.Line -notmatch 'GUID\s*=' -and $_.Path -notmatch '\\Setups\\'
      } |
      Select-Object -First 25 | ForEach-Object {
        $hits++
        Write-Host "   $($_.Path)" -ForegroundColor Green
        $line = $_.Line.Trim(); if ($line.Length -gt 170) { $line = $line.Substring(0,170) + '...' }
        Write-Host "     $line"
      }
}
if ($hits -eq 0) { Write-Host "   no connection strings in config files" -ForegroundColor Yellow }

H "3b. Stored credentials - Windows registry"
$rhits = 0
foreach ($key in @('HKLM:\SOFTWARE\Bruker Daltonik', 'HKLM:\SOFTWARE\WOW6432Node\Bruker Daltonik',
                   'HKCU:\SOFTWARE\Bruker Daltonik', 'HKLM:\SOFTWARE\PostgreSQL')) {
    if (-not (Test-Path $key)) { continue }
    Get-ChildItem $key -Recurse -EA SilentlyContinue | ForEach-Object {
        $props = Get-ItemProperty $_.PSPath -EA SilentlyContinue
        foreach ($n in $props.PSObject.Properties.Name) {
            if ($n -match 'pass|pwd|user|conn|port|host' -and $n -notmatch 'GUID') {
                $rhits++
                Write-Host "   $($_.PSPath -replace '^Microsoft\.PowerShell\.Core\\Registry::','')" -ForegroundColor Green
                Write-Host "     $n = $($props.$n)"
            }
        }
    }
}
if ($rhits -eq 0) { Write-Host "   nothing in the registry" -ForegroundColor Yellow }

H "3c. Which account runs the PostgreSQL service"
Get-CimInstance Win32_Service -EA SilentlyContinue |
  Where-Object { $_.PathName -match 'postgres|BrukerDBData' } |
  ForEach-Object {
    Write-Host "   $($_.Name): StartName=$($_.StartName)" -ForegroundColor Green
    Write-Host "     $($_.PathName)"
  }

H "4. Roles the server knows (only works if something above already lets you in)"
Write-Host "   Once you have a username that connects, list the rest with:"
Write-Host '     psql -h 127.0.0.1 -p <port> -U <user> -Atc "SELECT rolname FROM pg_roles;"'

Write-Host ''
Write-Host "Then re-run the dump with whatever worked:" -ForegroundColor Cyan
Write-Host '   $env:PGPASSWORD = "<password>"'
Write-Host '   .\dump_bruker_db.ps1 -PgUser <user>'
Write-Host ''
Write-Host "If nothing here yields a credential, ask whoever installed HyStar," -ForegroundColor Yellow
Write-Host "or Bruker support. Do not edit pg_hba.conf on a live instrument." -ForegroundColor Yellow

if ($script:__t) {
    try { Stop-Transcript | Out-Null } catch {}
    Write-Host ''
    Write-Host "Full output saved to:" -ForegroundColor Cyan
    Write-Host "   $script:__t"
    Write-Host "On Hive: /quobyte/proteomics-grp/brett/bruker_db/$(Split-Path $script:__t -Leaf)" -ForegroundColor Cyan
}
