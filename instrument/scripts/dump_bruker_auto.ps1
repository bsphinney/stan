<#
.SYNOPSIS
    Read the Bruker DB credential from HyStar's own config, then dump the
    database. One step, no typing.

.DESCRIPTION
    Bruker's Compass Server stores its PostgreSQL connection in a plaintext
    JBoss-style datasource file (compass-ds.xml) on this machine. That is the
    credential HyStar's own stack uses. This script reads it, connects with it,
    and hands off to dump_bruker_db.ps1 -- which is read-only and writes to the
    Quobyte share.

    Nothing here writes to the database, stops a service, or edits any Bruker
    file. It reads one config file and runs pg_dump.

    If the datasource file is not where expected, or the credential does not
    connect, it says so and stops -- it does not guess passwords.

.EXAMPLE
    .\dump_bruker_auto.ps1
    .\dump_bruker_auto.ps1 -IncludeData     # also dump sample-table rows
#>
[CmdletBinding()]
param(
    [switch] $IncludeData,
    # Explicit path to the datasource xml, if the search misses it.
    [string] $DsFile = ''
)

$ErrorActionPreference = 'Stop'
function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }

# -- find the datasource file -------------------------------------------------
$candidates = @()
if ($DsFile) { $candidates += $DsFile }
$candidates += @(
    'C:\Program Files\Bruker\Bruker Compass Server\etc\compass-ds.xml',
    'C:\Program Files (x86)\Bruker\Bruker Compass Server\etc\compass-ds.xml'
)
# plus anything named *-ds.xml under a Bruker install
foreach ($root in @('C:\Program Files\Bruker', 'C:\Program Files (x86)\Bruker')) {
    if (Test-Path $root) {
        $candidates += (Get-ChildItem $root -Recurse -File -Filter '*-ds.xml' -EA SilentlyContinue |
                        Select-Object -ExpandProperty FullName)
    }
}
$ds = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $ds) {
    Say "Could not find a Bruker datasource file (*-ds.xml)." 'Red'
    Say "If you know where it is, pass it:  .\dump_bruker_auto.ps1 -DsFile <path>" 'Yellow'
    exit 1
}
Say "datasource: $ds"

# -- parse the datasource: db/port + which security domain holds the login ----
$dbName = 'compass'; $dbPort = 5432; $secDomain = $null
try {
    [xml]$xml = Get-Content -LiteralPath $ds -Raw
    $node = $xml.SelectSingleNode('//*[local-name()="local-tx-datasource" or local-name()="datasource" or local-name()="xa-datasource"]')
    if (-not $node) { $node = $xml.DocumentElement }
    $url = ($node.SelectSingleNode('.//*[local-name()="connection-url"]')).'#text'
    if ($url -match ':(\d+)/([A-Za-z0-9_]+)') { $dbPort = [int]$Matches[1]; $dbName = $Matches[2] }
    $inlineU = ($node.SelectSingleNode('.//*[local-name()="user-name"]')).'#text'
    $inlineP = ($node.SelectSingleNode('.//*[local-name()="password"]')).'#text'
    $secDomain = ($node.SelectSingleNode('.//*[local-name()="security-domain"]')).'#text'
} catch {
    Say "Could not parse $ds as XML: $($_.Exception.Message)" 'Red'; exit 1
}

$dbUser = $null; $dbPass = $null
if ($inlineU) {
    # Some datasources carry the login inline.
    $dbUser = $inlineU.Trim(); $dbPass = if ($inlineP) { $inlineP.Trim() } else { '' }
} elseif ($secDomain) {
    # The common case here: the login lives in a JAAS security domain. Resolve
    # it from security-domains.xml IN PLACE -- the password stays on this
    # machine and is never printed.
    $secDomain = $secDomain.Trim()
    Say "datasource uses security-domain '$secDomain'; resolving it locally"
    $sdFile = $null
    foreach ($cand in @((Join-Path (Split-Path $ds) 'security-domains.xml'),
                        (Join-Path (Split-Path $ds) 'security-domains_legacy.xml'))) {
        if (Test-Path $cand) { $sdFile = $cand; break }
    }
    if (-not $sdFile) { Say "security-domains.xml not found beside $ds." 'Red'; exit 1 }
    try {
        [xml]$sx = Get-Content -LiteralPath $sdFile -Raw
        $dom = $sx.SelectSingleNode("//*[local-name()='security-domain' and @name='$secDomain']")
        if (-not $dom) { $dom = $sx.SelectSingleNode("//*[local-name()='application-policy' and @name='$secDomain']") }
        if (-not $dom) { Say "security-domain '$secDomain' not in $sdFile." 'Red'; exit 1 }
        # module-option name may be userName/user/username and password/pass.
        foreach ($mo in $dom.SelectNodes(".//*[local-name()='module-option']")) {
            $nm = ("$($mo.name)").ToLower(); $val = "$($mo.value)"; if (-not $val) { $val = $mo.'#text' }
            if ($nm -in @('username','user','user-name') -and -not $dbUser) { $dbUser = "$val".Trim() }
            if ($nm -in @('password','pass') -and $null -eq $dbPass) { $dbPass = "$val".Trim() }
        }
    } catch { Say "Could not parse ${sdFile}: $($_.Exception.Message)" 'Red'; exit 1 }
}

if (-not $dbUser) { Say "No database user found in the datasource or its security domain." 'Red'; exit 1 }
if ($null -eq $dbPass) { $dbPass = '' }

# A masked/vaulted password is not usable directly. Flag it rather than send a
# garbage string at the server.
if ($dbPass -match '^(MASK-|\$\{|VAULT::|CryptoData|ENC\()') {
    Say "The password is masked/vaulted, not plaintext:" 'Yellow'
    Say "   $dbPass" 'Yellow'
    Say "This cannot be used directly. Ask Bruker support for the DB credential," 'Yellow'
    Say "or export the sample table from HyStar instead -- the queue reconcile" 'Yellow'
    Say "works from that .xlsx without any database access." 'Yellow'
    exit 2
}
Say ("credential resolved: user={0}  db={1}  port={2}  password=<{3} chars>" -f $dbUser, $dbName, $dbPort, $dbPass.Length)

# -- verify it connects before handing off ------------------------------------
$pgDump = Get-ChildItem 'C:\Program Files\Bruker' -Filter pg_dump.exe -Recurse -EA SilentlyContinue |
          Select-Object -First 1 -ExpandProperty FullName
if (-not $pgDump) {
    foreach ($b in @("$env:ProgramFiles", 'C:\Program Files')) {
        $pgDump = Get-ChildItem $b -Filter pg_dump.exe -Recurse -EA SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
        if ($pgDump) { break }
    }
}
if (-not $pgDump) { Say "pg_dump.exe not found." 'Red'; exit 1 }
$psql = Join-Path (Split-Path $pgDump) 'psql.exe'

$env:PGPASSWORD = $dbPass
$ping = & $psql -h 127.0.0.1 -p $dbPort -U $dbUser -d $dbName -Atc 'SELECT 1;' 2>&1
if ($LASTEXITCODE -ne 0) {
    Say "That credential did not connect:" 'Red'
    Say ($ping | Out-String)
    Say "The datasource user may only reach the 'compass' database, or the" 'Yellow'
    Say "password may be obfuscated in the file rather than plaintext. Stop here" 'Yellow'
    Say "and ask whoever installed HyStar / Bruker support." 'Yellow'
    exit 1
}
Say "connected OK." 'Green'

# -- hand off to the read-only dump -------------------------------------------
$dump = Join-Path $PSScriptRoot 'dump_bruker_db.ps1'
if (-not (Test-Path $dump)) {
    Say "dump_bruker_db.ps1 is not next to this script ($PSScriptRoot)." 'Red'
    Say "Copy both files together." 'Yellow'
    exit 1
}
Say ''
Say "Running the dump as -PgUser $dbUser ..." 'Cyan'
$dumpArgs = @('-PgUser', $dbUser, '-Port', "$dbPort")
if ($IncludeData) { $dumpArgs += '-IncludeData' }
& $dump @dumpArgs
